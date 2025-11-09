from __future__ import annotations

from datetime import datetime
from typing import List

import httpx

from oncall_swap.domain.models import Participant, TimeWindow
from oncall_swap.ports.directory import ParticipantDirectoryPort
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort


class OpsgenieClient(OpsgenieSchedulePort, OpsgenieOverridePort):
    """HTTP client for Opsgenie schedule and override APIs."""

    def __init__(
        self,
        api_key: str,
        directory: ParticipantDirectoryPort,
        base_url: str = "https://api.opsgenie.com",
        timeout: float = 10.0,
    ) -> None:
        self.directory = directory
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"GenieKey {api_key}",
                "Content-Type": "application/json",
            },
        )

    def list_oncall(self, schedule_id: str, start: datetime, end: datetime) -> List[OnCallAssignment]:
        response = self.client.get(
            f"/v2/schedules/{schedule_id}/timeline",
            params={
                "intervalUnit": "minutes",
                "interval": int((end - start).total_seconds() // 60),
                "date": start.isoformat(),
            },
        )
        response.raise_for_status()
        data = response.json()
        rotations = data.get("data", {}).get("rotations", [])
        assignments: List[OnCallAssignment] = []
        for rotation in rotations:
            for entry in rotation.get("periods", []):
                coverage_start = datetime.fromisoformat(entry["startDate"])
                coverage_end = datetime.fromisoformat(entry["endDate"])
                if coverage_end <= start or coverage_start >= end:
                    continue
                covering_user = entry.get("recipient", {})
                email = covering_user.get("contact", {}).get("email")
                if not email:
                    continue
                participant = self.directory.upsert(Participant(email=email, opsgenie_user_id=covering_user.get("id")))
                assignments.append(
                    OnCallAssignment(
                        participant=participant,
                        window=TimeWindow(start=coverage_start, end=coverage_end),
                    )
                )
        return assignments

    def apply_override(self, schedule_id: str, participant: Participant, window: TimeWindow) -> None:
        payload = {
            "startDate": window.start.isoformat(),
            "endDate": window.end.isoformat(),
            "user": {
                "username": participant.email,
            },
        }
        response = self.client.post(f"/v2/schedules/{schedule_id}/overrides", json=payload)
        response.raise_for_status()
