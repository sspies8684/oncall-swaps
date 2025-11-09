from __future__ import annotations

from typing import Dict, Optional
from uuid import UUID

from oncall_swap.domain.models import SwapOffer
from oncall_swap.ports.persistence import OfferRepository


class InMemoryOfferRepository(OfferRepository):
    def __init__(self) -> None:
        self._storage: Dict[UUID, SwapOffer] = {}

    def add(self, offer: SwapOffer) -> None:
        self._storage[offer.id] = offer

    def get(self, offer_id: UUID) -> Optional[SwapOffer]:
        return self._storage.get(offer_id)

    def update(self, offer: SwapOffer) -> None:
        self._storage[offer.id] = offer
