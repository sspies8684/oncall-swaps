from datetime import datetime, timedelta, timezone
from typing import Iterable, List
from uuid import UUID

import pytest

from oncall_swap.application.commands import AcceptCoverCommand, CreateOfferCommand, TimeWindowDTO
from oncall_swap.application.services import SwapNegotiationService
from oncall_swap.domain.models import Participant, SwapOffer, TimeWindow
from oncall_swap.infrastructure.directory.in_memory import InMemoryParticipantDirectory
from oncall_swap.infrastructure.persistence.in_memory import InMemoryOfferRepository
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort
from oncall_swap.ports.slack import SlackNotificationPort, SlackPromptPort


class FakeSchedulePort(OpsgenieSchedulePort):
    def __init__(self, assignments: List[OnCallAssignment]) -> None:
        self.assignments = assignments

    def list_oncall(self, schedule_id: str, start: datetime, end: datetime) -> List[OnCallAssignment]:
        return self.assignments


class FakeOverridePort(OpsgenieOverridePort):
    def __init__(self) -> None:
        self.applied: List[OnCallAssignment] = []

    def apply_override(self, schedule_id: str, participant: Participant, window: TimeWindow) -> None:
        self.applied.append(OnCallAssignment(participant=participant, window=window))


class DummySlack(SlackNotificationPort, SlackPromptPort):
    def __init__(self) -> None:
        self.announcements: List[SwapOffer] = []
        self.prompt_requests: List[tuple[UUID, List[str]]] = []
        self.direct_swaps: List[str] = []
        self.ring_completions: int = 0

    def announce_offer(self, offer: SwapOffer) -> None:
        self.announcements.append(offer)

    def notify_direct_swap(self, offer: SwapOffer, participant: Participant, window: TimeWindow) -> None:
        self.direct_swaps.append(participant.email)

    def notify_ring_candidate(self, offer: SwapOffer, candidate: Participant) -> None:
        pass

    def notify_ring_completion(self, offer: SwapOffer) -> None:
        self.ring_completions += 1

    def notify_ring_update(self, offer: SwapOffer) -> None:
        pass

    def prompt_cover_request(
        self,
        offer_id: UUID,
        candidates: Iterable[Participant],
        window: TimeWindow,
        available_alternatives: Iterable[TimeWindow],
        need_owner: Participant,
    ) -> None:
        self.prompt_requests.append((offer_id, [c.email for c in candidates], need_owner.email))


def make_window(day_offset: int) -> TimeWindowDTO:
    start = datetime(2025, 11, 10, 9, 0) + timedelta(days=day_offset)
    end = start + timedelta(hours=12)
    return TimeWindowDTO(start=start, end=end)


def dto_to_window(dto: TimeWindowDTO) -> TimeWindow:
    return TimeWindow(start=dto.start, end=dto.end)


def test_ring_swap_resolution():
    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    slack = DummySlack()
    override_port = FakeOverridePort()

    # Participants
    requester = directory.upsert(Participant(email="p1@example.com"))
    p3 = directory.upsert(Participant(email="p3@example.com"))
    p4 = directory.upsert(Participant(email="p4@example.com"))

    let_window = make_window(0)
    search_window = make_window(3)  # T3
    ring_window = make_window(14)  # T14

    schedule_port = FakeSchedulePort(
        assignments=[
            OnCallAssignment(participant=p4, window=dto_to_window(search_window)),
            OnCallAssignment(participant=p3, window=dto_to_window(ring_window)),
        ]
    )

    service = SwapNegotiationService(
        repository=repository,
        directory=directory,
        schedule_port=schedule_port,
        override_port=override_port,
        slack_notifications=slack,
        slack_prompts=slack,
    )

    command = CreateOfferCommand(
        requester_email=requester.email,
        schedule_id="primary",
        let_window=let_window,
        search_windows=[search_window],
    )

    offer = service.create_offer(command)

    # P3 covers let window, needs T14.
    service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=p3.email,
            covers_window=let_window,
            needs_windows=[ring_window],
        )
    )

    # P4 covers T14, wants T3 (search window).
    ring_result = service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=p4.email,
            covers_window=ring_window,
            needs_windows=[search_window],
        )
    )

    assert ring_result is not None
    assert len(ring_result.commitments) == 3
    assert override_port.applied  # overrides applied
    assert slack.ring_completions == 1


def test_create_offer_rejects_past_let_window():
    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    slack = DummySlack()
    schedule_port = FakeSchedulePort(assignments=[])
    override_port = FakeOverridePort()

    service = SwapNegotiationService(
        repository=repository,
        directory=directory,
        schedule_port=schedule_port,
        override_port=override_port,
        slack_notifications=slack,
        slack_prompts=slack,
    )

    past_start = datetime.now(timezone.utc) - timedelta(days=1)
    past_window = TimeWindowDTO(start=past_start, end=past_start + timedelta(hours=12))

    command = CreateOfferCommand(
        requester_email="p1@example.com",
        schedule_id="primary",
        let_window=past_window,
        search_windows=[make_window(2)],
    )

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        service.create_offer(command)


def test_create_offer_rejects_past_search_window():
    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    slack = DummySlack()
    schedule_port = FakeSchedulePort(assignments=[])
    override_port = FakeOverridePort()

    service = SwapNegotiationService(
        repository=repository,
        directory=directory,
        schedule_port=schedule_port,
        override_port=override_port,
        slack_notifications=slack,
        slack_prompts=slack,
    )

    future_let = make_window(1)
    past_start = datetime.now(timezone.utc) - timedelta(days=1)
    past_search = TimeWindowDTO(start=past_start, end=past_start + timedelta(hours=12))

    command = CreateOfferCommand(
        requester_email="p1@example.com",
        schedule_id="primary",
        let_window=future_let,
        search_windows=[past_search],
    )

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        service.create_offer(command)
