from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List

from oncall_swap.domain.models import Participant, TimeWindow


@dataclass(frozen=True)
class OnCallAssignment:
    participant: Participant
    window: TimeWindow


class OpsgenieSchedulePort:
    """Reads on-call information from Opsgenie."""

    def list_oncall(self, schedule_id: str, start: datetime, end: datetime) -> List[OnCallAssignment]:
        raise NotImplementedError


class OpsgenieOverridePort:
    """Writes overrides back to Opsgenie."""

    def apply_override(self, schedule_id: str, participant: Participant, window: TimeWindow) -> None:
        raise NotImplementedError

    def apply_overrides(self, schedule_id: str, assignments: Iterable[OnCallAssignment]) -> None:
        for assignment in assignments:
            self.apply_override(schedule_id, assignment.participant, assignment.window)
