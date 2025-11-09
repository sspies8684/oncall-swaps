from typing import Optional
from uuid import UUID

from oncall_swap.domain.models import SwapOffer


class OfferRepository:
    """Abstract storage for swap offers."""

    def add(self, offer: SwapOffer) -> None:
        raise NotImplementedError

    def get(self, offer_id: UUID) -> Optional[SwapOffer]:
        raise NotImplementedError

    def update(self, offer: SwapOffer) -> None:
        raise NotImplementedError
