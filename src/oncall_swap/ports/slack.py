from __future__ import annotations

from typing import Iterable, Protocol
from uuid import UUID

from oncall_swap.domain.models import Participant, SwapOffer, TimeWindow


class SlackNotificationPort(Protocol):
    """Output port for notifying Slack users about offer state changes."""

    def announce_offer(self, offer: SwapOffer) -> None:
        ...

    def notify_direct_swap(self, offer: SwapOffer, participant: Participant, window: TimeWindow) -> None:
        ...

    def notify_ring_candidate(self, offer: SwapOffer, candidate: Participant) -> None:
        ...

    def notify_ring_completion(self, offer: SwapOffer) -> None:
        ...

    def notify_ring_update(self, offer: SwapOffer) -> None:
        ...


class SlackPromptPort(Protocol):
    """Interaction port to solicit responses from potential participants."""

    def prompt_cover_request(
        self,
        offer_id: UUID,
        candidates: Iterable[Participant],
        window: TimeWindow,
        available_alternatives: Iterable[TimeWindow],
        need_owner: Participant,
    ) -> None:
        ...

