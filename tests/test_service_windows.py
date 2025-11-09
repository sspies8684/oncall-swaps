from datetime import datetime, timedelta, timezone
from typing import List

from oncall_swap.application.services import SwapNegotiationService
from oncall_swap.domain.models import Participant, TimeWindow
from oncall_swap.infrastructure.directory.in_memory import InMemoryParticipantDirectory
from oncall_swap.infrastructure.persistence.in_memory import InMemoryOfferRepository
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort


class FakeSchedulePort(OpsgenieSchedulePort):
    def __init__(self, assignments: List[OnCallAssignment]) -> None:
        self.assignments = assignments

    def list_oncall(self, schedule_id: str, start: datetime, end: datetime) -> List[OnCallAssignment]:
        return [
            assignment
            for assignment in self.assignments
            if assignment.window.start >= start and assignment.window.end <= end
        ]


class DummyOverridePort(OpsgenieOverridePort):
    def apply_override(self, schedule_id: str, participant: Participant, window: TimeWindow) -> None:  # noqa: D401
        """No-op override port for testing."""
        return


def test_get_upcoming_windows_filters_by_email():
    now = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    p1 = Participant(email="p1@example.com")
    p2 = Participant(email="p2@example.com")

    assignments = [
        OnCallAssignment(
            participant=p1,
            window=TimeWindow(
                start=now + timedelta(days=1),
                end=now + timedelta(days=1, hours=12),
            ),
        ),
        OnCallAssignment(
            participant=p2,
            window=TimeWindow(
                start=now + timedelta(days=2),
                end=now + timedelta(days=2, hours=12),
            ),
        ),
    ]

    service = SwapNegotiationService(
        repository=InMemoryOfferRepository(),
        directory=InMemoryParticipantDirectory(),
        schedule_port=FakeSchedulePort(assignments),
        override_port=DummyOverridePort(),
    )

    windows = service.get_upcoming_windows(
        schedule_id="primary",
        participant_email="p1@example.com",
        horizon_days=7,
        now=now,
    )

    assert len(windows) == 1
    assert windows[0].start == assignments[0].window.start
    assert windows[0].end == assignments[0].window.end
