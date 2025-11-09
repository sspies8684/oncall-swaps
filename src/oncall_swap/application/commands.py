from __future__ import annotations

from datetime import datetime
from typing import List
from uuid import UUID

from pydantic import BaseModel, Field


class TimeWindowDTO(BaseModel):
    start: datetime
    end: datetime


class CreateOfferCommand(BaseModel):
    requester_email: str
    let_window: TimeWindowDTO
    search_windows: List[TimeWindowDTO]
    schedule_id: str = Field(..., description="Opsgenie schedule identifier")


class AcceptCoverCommand(BaseModel):
    offer_id: UUID
    participant_email: str
    covers_window: TimeWindowDTO
    needs_windows: List[TimeWindowDTO] = Field(default_factory=list)

