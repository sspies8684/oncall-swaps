from typing import Optional

from oncall_swap.domain.models import Participant


class ParticipantDirectoryPort:
    """Resolves cross-system identities for participants."""

    def get_by_email(self, email: str) -> Optional[Participant]:
        raise NotImplementedError

    def upsert(self, participant: Participant) -> Participant:
        raise NotImplementedError
