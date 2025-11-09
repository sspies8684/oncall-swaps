from __future__ import annotations

from typing import Dict, Optional
from uuid import UUID

from oncall_swap.domain.models import Participant
from oncall_swap.ports.directory import ParticipantDirectoryPort


class InMemoryParticipantDirectory(ParticipantDirectoryPort):
    def __init__(self) -> None:
        self._by_email: Dict[str, Participant] = {}
        self._by_id: Dict[UUID, Participant] = {}

    def get_by_email(self, email: str) -> Optional[Participant]:
        return self._by_email.get(email.lower())

    def upsert(self, participant: Participant) -> Participant:
        normalized_email = participant.email.lower()
        self._by_email[normalized_email] = participant
        self._by_id[participant.id] = participant
        return participant
