from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from oncall_swap.domain.time import Instant


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimeWindow(BaseModel):
    """Represents a closed-open time interval [start, end)."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_non_empty(self) -> "TimeWindow":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self

    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def intersection(self, other: "TimeWindow") -> Optional["TimeWindow"]:
        if not self.overlaps(other):
            return None
        return TimeWindow(start=max(self.start, other.start), end=min(self.end, other.end))

    def to_tuple(self) -> Tuple[datetime, datetime]:
        return self.start, self.end


class Participant(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    email: str
    slack_user_id: Optional[str] = None
    opsgenie_user_id: Optional[str] = None


class OfferStatus(str, Enum):
    ACTIVE = "active"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class DirectSwap(BaseModel):
    """Represents a successful two-way swap agreement."""

    participant: Participant
    covers_window: TimeWindow
    in_exchange_for: TimeWindow


class WindowNeed(BaseModel):
    """Tracks a window that still requires coverage and who currently owns it."""

    owner: Participant
    window: TimeWindow
    created_by_offer: bool = False


class RingCandidate(BaseModel):
    """A participant that can cover the offerer's let window but requires coverage for their own."""

    participant: Participant
    covers_window: TimeWindow
    needs_windows: List[TimeWindow]
    created_at: datetime = Field(default_factory=utcnow)


class RingSwapCommitment(BaseModel):
    """Represents a single leg in a ring swap."""

    from_participant: Participant
    to_participant: Participant
    window: TimeWindow


class RingSwap(BaseModel):
    """Represents a ring swap closure with at least three participants."""

    commitments: List[RingSwapCommitment]

    @model_validator(mode="after")
    def validate_ring_chain(self) -> "RingSwap":
        commitments: Sequence[RingSwapCommitment] = self.commitments or ()
        if len(commitments) < 3:
            raise ValueError("Ring swap requires at least three commitments")
        participants = {c.from_participant.id for c in commitments}
        if len(participants) != len(commitments):
            raise ValueError("Each commitment must originate from a unique participant")
        return self


class SwapOffer(BaseModel):
    """Aggregate root capturing the lifecycle of an on-call swap offer."""

    id: UUID = Field(default_factory=uuid4)
    requester: Participant
    schedule_id: str
    let_window: TimeWindow
    search_windows: List[TimeWindow]
    created_at: datetime = Field(default_factory=utcnow)
    status: OfferStatus = OfferStatus.ACTIVE

    # Dynamic state
    available_windows: List[TimeWindow] = Field(default_factory=list)
    direct_agreements: List[DirectSwap] = Field(default_factory=list)
    ring_candidates: List[RingCandidate] = Field(default_factory=list)
    ring_swaps: List[RingSwap] = Field(default_factory=list)
    outstanding_needs: List[WindowNeed] = Field(default_factory=list)
    partial_commitments: List[RingSwapCommitment] = Field(default_factory=list)

    def __init__(self, **data):
        super().__init__(**data)
        if not self.available_windows:
            self.available_windows = list(self.search_windows)
        if not self.outstanding_needs:
            self.outstanding_needs = [WindowNeed(owner=self.requester, window=self.let_window, created_by_offer=True)]

    def add_available_windows(self, windows: Iterable[TimeWindow]) -> None:
        for window in windows:
            if not any(existing.to_tuple() == window.to_tuple() for existing in self.available_windows):
                self.available_windows.append(window)

    def find_need(self, window: TimeWindow) -> Optional[WindowNeed]:
        for need in self.outstanding_needs:
            if need.window.to_tuple() == window.to_tuple():
                return need
        return None

    def resolve_need(self, window: TimeWindow) -> Optional[WindowNeed]:
        need = self.find_need(window)
        if need:
            self.outstanding_needs = [
                existing for existing in self.outstanding_needs if existing.window.to_tuple() != window.to_tuple()
            ]
        return need

    def add_commitment(self, coverer: Participant, need: WindowNeed) -> RingSwapCommitment:
        commitment = RingSwapCommitment(
            from_participant=coverer,
            to_participant=need.owner,
            window=need.window,
        )
        self.partial_commitments.append(commitment)
        return commitment

    def record_direct_swap(self, participant: Participant, swap_window: TimeWindow) -> DirectSwap:
        swap = DirectSwap(participant=participant, covers_window=self.let_window, in_exchange_for=swap_window)
        self.direct_agreements.append(swap)
        self.status = OfferStatus.FULFILLED
        self.outstanding_needs = []
        self.partial_commitments = []
        return swap

    def record_ring_candidate(
        self, participant: Participant, needs_windows: Sequence[TimeWindow]
    ) -> RingCandidate:
        candidate = RingCandidate(participant=participant, covers_window=self.let_window, needs_windows=list(needs_windows))
        self.ring_candidates.append(candidate)
        self.add_available_windows(candidate.needs_windows)
        self.outstanding_needs.extend(
            WindowNeed(owner=participant, window=need, created_by_offer=False) for need in needs_windows
        )
        return candidate

    def record_ring_swap(self, ring: RingSwap) -> RingSwap:
        self.ring_swaps.append(ring)
        self.status = OfferStatus.FULFILLED
        self.outstanding_needs = []
        self.partial_commitments = []
        return ring

    def is_active(self) -> bool:
        return self.status == OfferStatus.ACTIVE

    def cancel(self) -> None:
        self.status = OfferStatus.CANCELLED

    # Factory -----------------------------------------------------------------

    class TimeWindowInPastError(ValueError):
        pass

    @classmethod
    def create(
        cls,
        *,
        requester: Participant,
        schedule_id: str,
        let_window: TimeWindow,
        search_windows: Sequence[TimeWindow],
        now: Optional[Instant] = None,
    ) -> "SwapOffer":
        instant = (now or Instant.utc_now()).to_datetime()
        cls._ensure_future(let_window, instant, "let window")
        for window in search_windows:
            cls._ensure_future(window, instant, "search window")
        return cls(
            requester=requester,
            schedule_id=schedule_id,
            let_window=let_window,
            search_windows=list(search_windows),
        )

    @staticmethod
    def _ensure_future(window: TimeWindow, instant: datetime, label: str) -> None:
        window_start = window.start if window.start.tzinfo else window.start.replace(tzinfo=timezone.utc)
        comparison = window_start.astimezone(timezone.utc)
        if comparison < instant:
            raise SwapOffer.TimeWindowInPastError(
                f"{label} starting at {window.start.isoformat()} is before the current instant {instant.isoformat()}."
            )

