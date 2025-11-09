from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from oncall_swap.application.commands import AcceptCoverCommand, CreateOfferCommand, TimeWindowDTO
from oncall_swap.domain.models import (
    Participant,
    RingSwap,
    RingSwapCommitment,
    SwapOffer,
    TimeWindow,
    WindowNeed,
)
from oncall_swap.ports.directory import ParticipantDirectoryPort
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort
from oncall_swap.ports.persistence import OfferRepository
from oncall_swap.ports.slack import SlackNotificationPort, SlackPromptPort


class OfferNotFoundError(Exception):
    pass


class OfferNotActiveError(Exception):
    pass


class SwapNegotiationService:
    """Application service orchestrating the swap lifecycle."""

    def __init__(
        self,
        repository: OfferRepository,
        directory: ParticipantDirectoryPort,
        schedule_port: OpsgenieSchedulePort,
        override_port: OpsgenieOverridePort,
        slack_notifications: Optional[SlackNotificationPort] = None,
        slack_prompts: Optional[SlackPromptPort] = None,
    ) -> None:
        self.repository = repository
        self.directory = directory
        self.schedule_port = schedule_port
        self.override_port = override_port
        self.slack_notifications = slack_notifications
        self.slack_prompts = slack_prompts

    def create_offer(self, command: CreateOfferCommand) -> SwapOffer:
        requester = self._ensure_participant(command.requester_email)
        let_window = self._to_window(command.let_window)
        search_windows = [self._to_window(w) for w in command.search_windows]

        offer = SwapOffer.create(
            requester=requester,
            schedule_id=command.schedule_id,
            let_window=let_window,
            search_windows=search_windows,
        )
        self.repository.add(offer)

        notifications, prompts = self._require_slack_ports()
        notifications.announce_offer(offer)
        self._prompt_initial_participants(offer)
        return offer

    def accept_cover(self, command: AcceptCoverCommand):
        offer = self._get_offer(command.offer_id)
        if not offer.is_active():
            raise OfferNotActiveError(f"Offer {offer.id} is no longer active (status={offer.status}).")

        participant = self._ensure_participant(command.participant_email)
        covers_window = self._to_window(command.covers_window)
        needs_windows = [self._to_window(w) for w in command.needs_windows]

        # Direct swap detection
        if self._is_direct_swap(offer, covers_window, needs_windows):
            swap = offer.record_direct_swap(participant=participant, swap_window=needs_windows[0])
            self.repository.update(offer)
            self._apply_direct_override(offer, swap.participant, swap.in_exchange_for)
            notifications, _ = self._require_slack_ports()
            notifications.notify_direct_swap(offer, swap.participant, swap.in_exchange_for)
            return swap

        need = offer.resolve_need(covers_window)
        if not need:
            raise ValueError(f"No outstanding need matches window {covers_window}")

        offer.add_commitment(participant, need)

        if covers_window.to_tuple() == offer.let_window.to_tuple():
            offer.record_ring_candidate(participant, needs_windows)
            notifications, _ = self._require_slack_ports()
            notifications.notify_ring_candidate(offer, participant)
        else:
            offer.add_available_windows(needs_windows)
            offer.outstanding_needs.extend(
                WindowNeed(owner=participant, window=window, created_by_offer=False) for window in needs_windows
            )

        self.repository.update(offer)
        ring = self._try_complete_ring(offer)

        if ring:
            assignments = [
                OnCallAssignment(participant=commitment.from_participant, window=commitment.window)
                for commitment in ring.commitments
            ]
            self.override_port.apply_overrides(offer.schedule_id, assignments)
            notifications, _ = self._require_slack_ports()
            notifications.notify_ring_completion(offer)
            self.repository.update(offer)
            return ring

        notifications, _ = self._require_slack_ports()
        notifications.notify_ring_update(offer)
        self.repository.update(offer)
        return None

    # Helpers -----------------------------------------------------------------

    def _prompt_initial_participants(self, offer: SwapOffer) -> None:
        horizon_start = min([offer.let_window.start] + [w.start for w in offer.search_windows])
        horizon_end = max([offer.let_window.end] + [w.end for w in offer.search_windows])
        assignments = self.schedule_port.list_oncall(
            schedule_id=offer.schedule_id,
            start=horizon_start,
            end=horizon_end,
        )

        search_participants = []
        ring_participants = []
        for assignment in assignments:
            if assignment.participant.id == offer.requester.id:
                continue
            if any(assignment.window.to_tuple() == w.to_tuple() for w in offer.search_windows):
                search_participants.append(assignment.participant)
            else:
                ring_participants.append(assignment.participant)

        _, prompts = self._require_slack_ports()
        if search_participants:
            prompts.prompt_cover_request(
                offer.id,
                search_participants,
                offer.let_window,
                offer.search_windows,
            )
        if ring_participants:
            prompts.prompt_cover_request(
                offer.id,
                ring_participants,
                offer.let_window,
                offer.available_windows,
            )

    def _apply_direct_override(self, offer: SwapOffer, participant: Participant, swap_window: TimeWindow) -> None:
        assignments = [
            OnCallAssignment(participant=participant, window=offer.let_window),
            OnCallAssignment(participant=offer.requester, window=swap_window),
        ]
        self.override_port.apply_overrides(offer.schedule_id, assignments)

    def _try_complete_ring(self, offer: SwapOffer) -> Optional[RingSwap]:
        if not offer.partial_commitments:
            return None

        if not offer.outstanding_needs:
            # Edge case: someone covered without requiring anything.
            return self._finalize_ring(offer, [])

        closable_needs = [
            need for need in offer.outstanding_needs if need.window.to_tuple() in {w.to_tuple() for w in offer.search_windows}
        ]
        if not closable_needs:
            return None
        if len(closable_needs) != len(offer.outstanding_needs):
            # Some needs still require third-party coverage.
            return None

        final_commitments = [
            RingSwapCommitment(
                from_participant=offer.requester,
                to_participant=need.owner,
                window=need.window,
            )
            for need in closable_needs
        ]
        return self._finalize_ring(offer, final_commitments)

    def _finalize_ring(self, offer: SwapOffer, final_commitments: List[RingSwapCommitment]) -> Optional[RingSwap]:
        commitments = offer.partial_commitments + final_commitments
        unique_participants = {c.from_participant.id for c in commitments}
        if len(commitments) < 3 or len(unique_participants) < 3:
            return None
        ring = RingSwap(commitments=commitments)
        offer.record_ring_swap(ring)
        return ring

    def _is_direct_swap(self, offer: SwapOffer, covers_window: TimeWindow, needs_windows: List[TimeWindow]) -> bool:
        if covers_window.to_tuple() != offer.let_window.to_tuple():
            return False
        if len(needs_windows) != 1:
            return False
        return needs_windows[0].to_tuple() in {w.to_tuple() for w in offer.search_windows}

    def _get_offer(self, offer_id: UUID) -> SwapOffer:
        offer = self.repository.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"Offer {offer_id} not found")
        return offer

    def _ensure_participant(self, email: str) -> Participant:
        existing = self.directory.get_by_email(email)
        if existing:
            return existing
        participant = Participant(email=email)
        return self.directory.upsert(participant)

    def get_upcoming_windows(
        self,
        schedule_id: str,
        participant_email: str,
        *,
        horizon_days: int = 30,
        now: Optional[datetime] = None,
    ) -> List[TimeWindow]:
        """Return future on-call windows for the given participant within the horizon."""

        window_start = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_end = window_start + timedelta(days=horizon_days)
        assignments = self.schedule_port.list_oncall(schedule_id, window_start, window_end)
        target_email = participant_email.lower()
        return [
            assignment.window
            for assignment in assignments
            if assignment.participant.email.lower() == target_email
        ]

    @staticmethod
    def _to_window(dto: TimeWindowDTO) -> TimeWindow:
        return TimeWindow(start=dto.start, end=dto.end)

    def _require_slack_ports(self) -> tuple[SlackNotificationPort, SlackPromptPort]:
        if self.slack_notifications is None or self.slack_prompts is None:
            raise RuntimeError("Slack ports are not configured for SwapNegotiationService.")
        return self.slack_notifications, self.slack_prompts

