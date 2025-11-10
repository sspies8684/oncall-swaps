from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from oncall_swap.application.commands import AcceptCoverCommand, CreateOfferCommand, TimeWindowDTO
from oncall_swap.domain.models import (
    OfferStatus,
    Participant,
    RingSwap,
    RingSwapCommitment,
    SwapOffer,
    TimeWindow,
    WindowNeed,
)
from oncall_swap.domain.time import Instant
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
        self.logger = logging.getLogger(__name__)

    def create_offer(self, command: CreateOfferCommand, now: Optional[Instant] = None) -> SwapOffer:
        requester = self._ensure_participant(command.requester_email)
        let_window = self._to_window(command.let_window)
        search_windows = [self._to_window(w) for w in command.search_windows]

        offer = SwapOffer.create(
            requester=requester,
            schedule_id=command.schedule_id,
            let_window=let_window,
            search_windows=search_windows,
            now=now,
        )
        self.repository.add(offer)

        notifications, _ = self._require_slack_ports()
        notifications.announce_offer(offer)
        return offer

    def accept_cover(self, command: AcceptCoverCommand):
        offer = self._get_offer(command.offer_id)
        if not offer.is_active():
            raise OfferNotActiveError(f"Offer {offer.id} is no longer active (status={offer.status}).")

        participant = self._ensure_participant(command.participant_email)
        covers_window = self._to_window(command.covers_window)
        needs_window = self._to_window(command.needs_windows[0]) if command.needs_windows else None

        if not needs_window:
            raise ValueError("Must specify a window to receive in return")

        # Check if this is a direct swap (needs_window is in search_windows)
        # Any direct swap closes the negotiation
        search_window_dates = {w.start.date() for w in offer.search_windows}
        is_direct = needs_window.start.date() in search_window_dates

        if is_direct:
            # Direct swap: close immediately and apply overrides
            # Check if this covers the original let_window (by date overlap)
            let_window_start_date = offer.let_window.start.date()
            let_window_end_date = offer.let_window.end.date()
            covers_window_start_date = covers_window.start.date()
            covers_window_end_date = covers_window.end.date()
            
            covers_let_window = (
                covers_window_start_date <= let_window_end_date
                and let_window_start_date <= covers_window_end_date
            )
            
            if covers_let_window:
                # Direct swap covering the original let_window
                # If someone already committed via ring swap, we should cancel that commitment
                # and create a simple direct swap instead, since a direct swap takes precedence
                # Check for any commitment covering let_window (could be from this participant or another)
                # Use date overlap to find commitments
                let_commitment = next(
                    (c for c in offer.partial_commitments 
                     if (c.window.start.date() <= let_window_end_date
                         and let_window_start_date <= c.window.end.date())),
                    None,
                )
                
                if let_commitment:
                    # Cancel the ring swap commitment - direct swap takes precedence
                    # This could be the current participant's own commitment or someone else's
                    commitment_participant = let_commitment.from_participant
                    
                    # Trace the entire ring swap chain and remove all related needs and commitments
                    # Start with the commitment participant
                    participants_to_clean = {commitment_participant.id}
                    commitments_to_check = [let_commitment]
                    checked_commitments = set()
                    
                    # Recursively find all participants in the chain by following commitments
                    while commitments_to_check:
                        current_commit = commitments_to_check.pop(0)
                        if id(current_commit) in checked_commitments:
                            continue
                        checked_commitments.add(id(current_commit))
                        
                        # Find what the commitment participant needs
                        for need in offer.outstanding_needs:
                            if (need.owner.id == current_commit.from_participant.id 
                                and not need.created_by_offer):
                                # Find who committed to cover this need
                                for commit in offer.partial_commitments:
                                    if (commit.window.start.date() <= need.window.end.date()
                                        and need.window.start.date() <= commit.window.end.date()):
                                        # This commitment covers the need
                                        if commit.from_participant.id not in participants_to_clean:
                                            participants_to_clean.add(commit.from_participant.id)
                                            commitments_to_check.append(commit)
                    
                    # Remove all needs from the chain
                    offer.outstanding_needs = [
                        n for n in offer.outstanding_needs 
                        if not (n.owner.id in participants_to_clean and not n.created_by_offer)
                    ]
                    
                    # Remove all commitments from the chain (including the let_window commitment)
                    offer.partial_commitments = [
                        c for c in offer.partial_commitments
                        if c.from_participant.id not in participants_to_clean
                    ]
                
                # Create simple direct swap: participant covers let_window, requester covers needs_window
                # Note: record_direct_swap doesn't require the need to exist, it just records the swap
                swap = offer.record_direct_swap(participant=participant, swap_window=needs_window)
                self.repository.update(offer)
                self._apply_direct_override(offer, swap.participant, swap.in_exchange_for)
                notifications, _ = self._require_slack_ports()
                notifications.notify_direct_swap(offer, swap.participant, swap.in_exchange_for)
                return swap
            
            # Direct swap covering a ring need (not let_window)
            need = offer.find_need(covers_window)
            if not need:
                # Check if this might be covering let_window with different times/dates
                # (e.g., let_window is Nov 9, covers_window is Nov 9-10)
                if (covers_window_start_date <= let_window_end_date
                    and let_window_start_date <= covers_window_end_date):
                    # This overlaps with let_window, check if there's a commitment
                    let_commitment = next(
                        (c for c in offer.partial_commitments 
                         if c.window.start.date() == let_window_start_date),
                        None,
                    )
                    if let_commitment:
                        # Cancel commitment and create direct swap
                        offer.partial_commitments = [
                            c for c in offer.partial_commitments 
                            if not (c.window.start.date() == let_window_start_date)
                        ]
                        offer.outstanding_needs = [
                            n for n in offer.outstanding_needs 
                            if not (n.owner.id == let_commitment.from_participant.id and not n.created_by_offer)
                        ]
                        swap = offer.record_direct_swap(participant=participant, swap_window=needs_window)
                        self.repository.update(offer)
                        self._apply_direct_override(offer, swap.participant, swap.in_exchange_for)
                        notifications, _ = self._require_slack_ports()
                        notifications.notify_direct_swap(offer, swap.participant, swap.in_exchange_for)
                        return swap
                
                # Debug: log what needs exist
                self.logger.debug(
                    "No need found for window %s. Outstanding needs: %s",
                    covers_window,
                    [(n.window.start, n.window.end) for n in offer.outstanding_needs],
                )
                raise ValueError(f"No outstanding need matches window {covers_window}")

            # Ring swap closure - build full chain of assignments
            # Start with: participant covers covers_window
            assignments = [
                OnCallAssignment(participant=participant, window=covers_window),
            ]
            
            # Track participants already in the chain to avoid duplicates
            participants_in_chain = {participant.id}
            
            # Find who covers the let_window (the original need)
            # If there are multiple commitments, pick one that doesn't create a cycle
            let_commitment = None
            for commit in offer.partial_commitments:
                if (commit.window.start.date() <= offer.let_window.end.date()
                    and offer.let_window.start.date() <= commit.window.end.date()):
                    # Prefer a commitment from someone not already in the chain
                    if commit.from_participant.id not in participants_in_chain:
                        let_commitment = commit
                        break
            # If all commitments are from people already in chain, use the first one
            if not let_commitment:
                let_commitment = next(
                    (c for c in offer.partial_commitments
                     if (c.window.start.date() <= offer.let_window.end.date()
                         and offer.let_window.start.date() <= c.window.end.date())),
                    None,
                )
            
            if let_commitment and let_commitment.from_participant.id not in participants_in_chain:
                # Add the commitment: let_commitment.from_participant covers let_window
                assignments.append(
                    OnCallAssignment(participant=let_commitment.from_participant, window=offer.let_window)
                )
                participants_in_chain.add(let_commitment.from_participant.id)
                
                # Find what let_commitment.from_participant needs
                let_participant_need = next(
                    (n for n in offer.outstanding_needs if n.owner.id == let_commitment.from_participant.id),
                    None,
                )
                if let_participant_need and let_participant_need.window.to_tuple() == covers_window.to_tuple():
                    # This need is being covered by the current participant
                    # The requester will cover needs_window
                    if offer.requester.id not in participants_in_chain:
                        assignments.append(
                            OnCallAssignment(participant=offer.requester, window=needs_window)
                        )
                else:
                    # The requester covers needs_window
                    if offer.requester.id not in participants_in_chain:
                        assignments.append(
                            OnCallAssignment(participant=offer.requester, window=needs_window)
                        )
            else:
                # No valid commitment for let_window (or would create duplicate), requester covers needs_window
                if offer.requester.id not in participants_in_chain:
                    assignments.append(
                        OnCallAssignment(participant=offer.requester, window=needs_window)
                    )

            # Apply overrides for ring swap closure
            self.override_port.apply_overrides(offer.schedule_id, assignments)
            offer.status = OfferStatus.FULFILLED
            offer.outstanding_needs = []
            self.repository.update(offer)
            notifications, _ = self._require_slack_ports()
            notifications.notify_direct_swap(offer, participant, needs_window, all_assignments=assignments)
            return None

        # Ring swap: needs_window is not in search_windows
        need = offer.find_need(covers_window)
        
        # Special case: if covers_window matches let_window and there's already a commitment,
        # allow creating another commitment (multiple people can commit to cover let_window)
        if not need:
            # Check if this is covering let_window
            if (covers_window.start.date() <= offer.let_window.end.date()
                and offer.let_window.start.date() <= covers_window.end.date()):
                # Check if there's already a commitment for let_window
                existing_commitment = next(
                    (c for c in offer.partial_commitments
                     if (c.window.start.date() <= offer.let_window.end.date()
                         and offer.let_window.start.date() <= c.window.end.date())),
                    None,
                )
                if existing_commitment:
                    # Multiple people can commit to cover let_window
                    # Create a need for let_window if it doesn't exist (it was resolved by first commitment)
                    # But we'll use the original let_window need concept
                    # Actually, we should create a commitment without resolving a need
                    # Since the need was already resolved, we'll create a new need entry for tracking
                    need = WindowNeed(owner=offer.requester, window=offer.let_window, created_by_offer=True)
                    # Don't add it to outstanding_needs since it's already been committed to
                    # Just create the commitment
                    offer.add_commitment(participant, need)
                    # Add new need for what the participant wants
                    if needs_window.start.date() not in {n.window.start.date() for n in offer.outstanding_needs}:
                        offer.outstanding_needs.append(
                            WindowNeed(owner=participant, window=needs_window, created_by_offer=False)
                        )
                    self.repository.update(offer)
                    notifications, _ = self._require_slack_ports()
                    notifications.notify_ring_update(offer)
                    return None
        
        if not need:
            # Debug: log what needs exist
            self.logger.debug(
                "No need found for window %s. Outstanding needs: %s",
                covers_window,
                [(n.window.start, n.window.end) for n in offer.outstanding_needs],
            )
            raise ValueError(f"No outstanding need matches window {covers_window}")

        # Add commitment and create new need
        offer.add_commitment(participant, need)
        offer.resolve_need(covers_window)  # Remove the covered need

        # Add new need for what the participant wants
        if needs_window.start.date() not in {n.window.start.date() for n in offer.outstanding_needs}:
            offer.outstanding_needs.append(
                WindowNeed(owner=participant, window=needs_window, created_by_offer=False)
            )

        self.repository.update(offer)
        notifications, _ = self._require_slack_ports()
        notifications.notify_ring_update(offer)
        return None

    # Helpers -----------------------------------------------------------------

    def _apply_direct_override(self, offer: SwapOffer, participant: Participant, swap_window: TimeWindow) -> None:
        assignments = [
            OnCallAssignment(participant=participant, window=offer.let_window),
            OnCallAssignment(participant=offer.requester, window=swap_window),
        ]
        self.override_port.apply_overrides(offer.schedule_id, assignments)


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

    def get_offer(self, offer_id: UUID) -> SwapOffer:
        return self._get_offer(offer_id)

    @staticmethod
    def _to_window(dto: TimeWindowDTO) -> TimeWindow:
        return TimeWindow(start=dto.start, end=dto.end)

    def _require_slack_ports(self) -> tuple[SlackNotificationPort, Optional[SlackPromptPort]]:
        if self.slack_notifications is None:
            raise RuntimeError("Slack notifications port is not configured for SwapNegotiationService.")
        return self.slack_notifications, self.slack_prompts


