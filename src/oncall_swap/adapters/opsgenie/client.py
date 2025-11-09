from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import httpx

from oncall_swap.domain.models import Participant, TimeWindow
from oncall_swap.ports.directory import ParticipantDirectoryPort
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort

logger = logging.getLogger(__name__)


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
        logger.info(f"Initialized OpsgenieClient with base_url={base_url}, timeout={timeout}")

    def list_oncall(self, schedule_id: str, start: datetime, end: datetime) -> List[OnCallAssignment]:
        try:
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
        except httpx.TimeoutException as e:
            logger.error(f"Timeout connecting to Opsgenie API: {e}")
            raise ConnectionError(f"Timeout connecting to Opsgenie API: {e}") from e
        except httpx.ConnectError as e:
            logger.error(f"Failed to connect to Opsgenie API: {e}")
            raise ConnectionError(f"Failed to connect to Opsgenie API: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Opsgenie API returned error status {e.response.status_code}: {e.response.text}")
            raise ConnectionError(f"Opsgenie API error: {e.response.status_code} - {e.response.text}") from e
        except Exception as e:
            logger.error(f"Unexpected error calling Opsgenie API: {e}", exc_info=True)
            raise ConnectionError(f"Unexpected error calling Opsgenie API: {e}") from e

    def apply_overrides(self, schedule_id: str, assignments: List[OnCallAssignment]) -> None:
        """Apply multiple overrides to a schedule."""
        for assignment in assignments:
            self.apply_override(schedule_id, assignment.participant, assignment.window)

    def apply_override(self, schedule_id: str, participant: Participant, window: TimeWindow) -> None:
        try:
            payload = {
                "startDate": window.start.isoformat(),
                "endDate": window.end.isoformat(),
                "user": {
                    "username": participant.email,
                },
            }
            response = self.client.post(f"/v2/schedules/{schedule_id}/overrides", json=payload)
            response.raise_for_status()
            logger.info(f"Applied override for {participant.email} on schedule {schedule_id}")
        except httpx.TimeoutException as e:
            logger.error(f"Timeout applying override to Opsgenie: {e}")
            raise ConnectionError(f"Timeout applying override to Opsgenie: {e}") from e
        except httpx.ConnectError as e:
            logger.error(f"Failed to connect to Opsgenie API when applying override: {e}")
            raise ConnectionError(f"Failed to connect to Opsgenie API: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Opsgenie API returned error status {e.response.status_code}: {e.response.text}")
            raise ConnectionError(f"Opsgenie API error: {e.response.status_code} - {e.response.text}") from e
        except Exception as e:
            logger.error(f"Unexpected error applying override to Opsgenie: {e}", exc_info=True)
            raise ConnectionError(f"Unexpected error applying override to Opsgenie: {e}") from e
