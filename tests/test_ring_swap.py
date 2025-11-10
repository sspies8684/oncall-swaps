from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional
from uuid import UUID

import pytest

from oncall_swap.application.commands import AcceptCoverCommand, CreateOfferCommand, TimeWindowDTO
from oncall_swap.application.services import SwapNegotiationService
from oncall_swap.domain.models import Participant, SwapOffer, TimeWindow
from oncall_swap.domain.time import Instant
from oncall_swap.infrastructure.directory.in_memory import InMemoryParticipantDirectory
from oncall_swap.infrastructure.persistence.in_memory import InMemoryOfferRepository
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort
from oncall_swap.ports.slack import SlackNotificationPort, SlackPromptPort

# Fixed test instant: Nov 10, 2025 at 8:00 AM UTC
TEST_NOW = Instant(at=datetime(2025, 11, 10, 8, 0, 0, tzinfo=timezone.utc))


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
        self.prompt_requests: List[tuple[UUID, List[str], str]] = []
        self.direct_swaps: List[tuple[Participant, TimeWindow]] = []
        self.ring_completions: int = 0
        self.last_direct_swap_assignments: List[OnCallAssignment] = []

    def announce_offer(self, offer: SwapOffer) -> None:
        self.announcements.append(offer)

    def notify_direct_swap(
        self,
        offer: SwapOffer,
        participant: Participant,
        window: TimeWindow,
        all_assignments: Optional[List[OnCallAssignment]] = None,
    ) -> None:
        self.direct_swaps.append((participant, window))
        if all_assignments:
            self.last_direct_swap_assignments = all_assignments

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
    # Use fixed base date: Nov 10, 2025 at 9:00 AM UTC
    base = datetime(2025, 11, 10, 9, 0, 0, tzinfo=timezone.utc)
    start = base + timedelta(days=day_offset)
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

    offer = service.create_offer(command, now=TEST_NOW)

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

    # Direct swap closing ring swap chain returns None
    assert ring_result is None
    assert override_port.applied  # overrides applied
    assert len(slack.direct_swaps) == 1  # Direct swap notification


def test_ring_and_direct_swap_resolution():
    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    slack = DummySlack()
    override_port = FakeOverridePort()

    requester = directory.upsert(Participant(email="p1@example.com"))
    ring_participant = directory.upsert(Participant(email="p3@example.com"))
    direct_participant = directory.upsert(Participant(email="p4@example.com"))
    helper_participant = directory.upsert(Participant(email="p2@example.com"))

    let_window = make_window(0)  # T1
    direct_trade = make_window(4)  # T5
    ring_need_window = make_window(2)  # T3

    # Schedule: helper on T5 (search), ring participant on T3, direct participant on T5 (search)
    schedule_port = FakeSchedulePort(
        assignments=[
            OnCallAssignment(participant=helper_participant, window=dto_to_window(direct_trade)),
            OnCallAssignment(participant=ring_participant, window=dto_to_window(ring_need_window)),
            OnCallAssignment(participant=direct_participant, window=dto_to_window(direct_trade)),
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
        search_windows=[direct_trade],
    )

    offer = service.create_offer(command, now=TEST_NOW)

    # Ring participant covers let window, needs T3.
    service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=ring_participant.email,
            covers_window=let_window,
            needs_windows=[ring_need_window],
        )
    )

    # Search participant covers T3 (new need), wants initial search window.
    ring_result = service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=direct_participant.email,
            covers_window=ring_need_window,
            needs_windows=[direct_trade],
        )
    )

    # Direct swap closing ring swap chain returns None
    assert ring_result is None
    assert len(slack.direct_swaps) == 1  # Direct swap notification


def test_ring_candidate_then_direct_swap_closes_offer():
    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    slack = DummySlack()
    override_port = FakeOverridePort()

    requester = directory.upsert(Participant(email="p1@example.com"))
    ring_participant = directory.upsert(Participant(email="p3@example.com"))
    direct_participant = directory.upsert(Participant(email="p2@example.com"))

    let_window = make_window(0)  # T1
    search_window = make_window(4)  # T5
    ring_need_window = make_window(2)  # T3

    schedule_port = FakeSchedulePort(
        assignments=[
            OnCallAssignment(participant=direct_participant, window=dto_to_window(search_window)),
            OnCallAssignment(participant=ring_participant, window=dto_to_window(ring_need_window)),
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

    offer = service.create_offer(
        CreateOfferCommand(
            requester_email=requester.email,
            schedule_id="primary",
            let_window=let_window,
            search_windows=[search_window],
        ),
        now=TEST_NOW,
    )

    # Ring participant volunteers first and asks for coverage.
    service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=ring_participant.email,
            covers_window=let_window,
            needs_windows=[ring_need_window],
        )
    )

    # Before the ring need is satisfied, a direct swapper takes the let window.
    direct_result = service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=direct_participant.email,
            covers_window=let_window,
            needs_windows=[search_window],
        )
    )

    assert direct_result is not None
    notified_participant, _ = slack.direct_swaps[-1]
    assert notified_participant.email == direct_participant.email
    assert slack.ring_completions == 0


def test_ring_swap_then_direct_swap_to_let_window():
    """
    Test scenario:
    - P1 offers: let_window = T1, search_windows = [T5]
    - P2 creates ring swap: covers T1, needs T10 (outside search_windows)
    - P3 makes direct swap: covers T1, needs T5 (in search_windows)
    
    Expected result:
    - P2's ring swap commitment is cancelled (direct swap takes precedence)
    - Simple direct swap: P3 covers T1, P1 covers T5
    - Notification should show P3 as the participant
    """
    directory = InMemoryParticipantDirectory()
    repository = InMemoryOfferRepository()
    slack = DummySlack()
    override_port = FakeOverridePort()

    p1 = directory.upsert(Participant(email="p1@example.com"))
    p2 = directory.upsert(Participant(email="p2@example.com"))
    p3 = directory.upsert(Participant(email="p3@example.com"))

    let_window = make_window(0)  # T1
    search_window = make_window(4)  # T5
    ring_need_window = make_window(9)  # T10 (outside search_windows)

    schedule_port = FakeSchedulePort(assignments=[])

    service = SwapNegotiationService(
        repository=repository,
        directory=directory,
        schedule_port=schedule_port,
        override_port=override_port,
        slack_notifications=slack,
        slack_prompts=slack,
    )

    # P1 creates offer
    offer = service.create_offer(
        CreateOfferCommand(
            requester_email=p1.email,
            schedule_id="primary",
            let_window=let_window,
            search_windows=[search_window],
        ),
        now=TEST_NOW,
    )

    # P2 creates ring swap: covers T1, needs T10
    service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=p2.email,
            covers_window=let_window,
            needs_windows=[ring_need_window],
        )
    )

    # Verify ring swap was created
    offer = service.get_offer(offer.id)
    assert len(offer.partial_commitments) == 1
    assert offer.partial_commitments[0].from_participant.email == p2.email
    # Compare dates since let_window is a DTO
    commitment_window = offer.partial_commitments[0].window
    assert commitment_window.start.date() == dto_to_window(let_window).start.date()
    assert commitment_window.end.date() == dto_to_window(let_window).end.date()
    assert len(offer.outstanding_needs) == 1
    assert offer.outstanding_needs[0].owner.email == p2.email
    assert offer.outstanding_needs[0].window.start.date() == dto_to_window(ring_need_window).start.date()
    assert offer.outstanding_needs[0].window.end.date() == dto_to_window(ring_need_window).end.date()

    # P3 makes direct swap: covers T1, needs T5
    direct_result = service.accept_cover(
        AcceptCoverCommand(
            offer_id=offer.id,
            participant_email=p3.email,
            covers_window=let_window,
            needs_windows=[search_window],
        )
    )

    # Verify offer is fulfilled
    offer = service.get_offer(offer.id)
    assert offer.status.value == "fulfilled"
    assert len(offer.outstanding_needs) == 0
    assert len(offer.partial_commitments) == 0  # P2's commitment should be cancelled

    # Verify overrides were applied correctly - should be simple 2-way swap
    assert len(override_port.applied) == 2
    
    # Find assignments
    p3_assignment = next(a for a in override_port.applied if a.participant.email == p3.email)
    p1_assignment = next(a for a in override_port.applied if a.participant.email == p1.email)

    # P3 should cover let_window (T1)
    assert p3_assignment.window.start.date() == dto_to_window(let_window).start.date()
    assert p3_assignment.window.end.date() == dto_to_window(let_window).end.date()
    
    # P1 should cover what P3 needs (T5)
    assert p1_assignment.window.start.date() == dto_to_window(search_window).start.date()
    assert p1_assignment.window.end.date() == dto_to_window(search_window).end.date()

    # Verify notification shows P3 as the participant
    assert len(slack.direct_swaps) == 1
    notified_participant, notified_window = slack.direct_swaps[0]
    assert notified_participant.email == p3.email
    assert notified_window.start.date() == dto_to_window(search_window).start.date()
    assert notified_window.end.date() == dto_to_window(search_window).end.date()

    # Verify no assignments were passed (simple direct swap, not ring closure)
    assert len(slack.last_direct_swap_assignments) == 0

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

    # Create a window that's in the past relative to TEST_NOW
    past_start = TEST_NOW.to_datetime() - timedelta(days=1)
    past_window = TimeWindowDTO(start=past_start, end=past_start + timedelta(hours=12))

    command = CreateOfferCommand(
        requester_email="p1@example.com",
        schedule_id="primary",
        let_window=past_window,
        search_windows=[make_window(2)],
    )

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        service.create_offer(command, now=TEST_NOW)


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
    # Create a window that's in the past relative to TEST_NOW
    past_start = TEST_NOW.to_datetime() - timedelta(days=1)
    past_search = TimeWindowDTO(start=past_start, end=past_start + timedelta(hours=12))

    command = CreateOfferCommand(
        requester_email="p1@example.com",
        schedule_id="primary",
        let_window=future_let,
        search_windows=[past_search],
    )

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        service.create_offer(command, now=TEST_NOW)
