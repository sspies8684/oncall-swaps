from datetime import datetime, timedelta, timezone

import pytest

from oncall_swap.domain.models import Participant, SwapOffer, TimeWindow
from oncall_swap.domain.time import Instant


def _window(start: datetime, hours: int = 8) -> TimeWindow:
    return TimeWindow(start=start, end=start + timedelta(hours=hours))


def test_rejects_let_window_in_past():
    now = Instant(at=datetime(2025, 1, 10, tzinfo=timezone.utc))
    past_start = datetime(2025, 1, 9, 8, tzinfo=timezone.utc)

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        SwapOffer.create(
            requester=Participant(email="p1@example.com"),
            schedule_id="primary",
            let_window=_window(past_start),
            search_windows=[_window(past_start + timedelta(days=2))],
            now=now,
        )


def test_rejects_search_window_in_past():
    now = Instant(at=datetime(2025, 1, 10, tzinfo=timezone.utc))
    future_start = datetime(2025, 1, 12, 8, tzinfo=timezone.utc)
    past_search = datetime(2025, 1, 9, 8, tzinfo=timezone.utc)

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        SwapOffer.create(
            requester=Participant(email="p1@example.com"),
            schedule_id="primary",
            let_window=_window(future_start),
            search_windows=[_window(past_search)],
            now=now,
        )


def test_allows_future_windows_relative_to_instant():
    now = Instant(at=datetime(2025, 1, 10, tzinfo=timezone.utc))
    let_start = datetime(2025, 1, 11, 8, tzinfo=timezone.utc)
    search_start = datetime(2025, 1, 13, 8, tzinfo=timezone.utc)

    offer = SwapOffer.create(
        requester=Participant(email="p1@example.com"),
        schedule_id="primary",
        let_window=_window(let_start),
        search_windows=[_window(search_start)],
        now=now,
    )

    assert offer.let_window.start == let_start


def test_rejects_naive_let_window_in_past():
    now = Instant(at=datetime(2025, 1, 10, tzinfo=timezone.utc))
    past_start = datetime(2025, 1, 9, 8)  # naive datetime

    with pytest.raises(SwapOffer.TimeWindowInPastError):
        SwapOffer.create(
            requester=Participant(email="p1@example.com"),
            schedule_id="primary",
            let_window=_window(past_start),
            search_windows=[_window(past_start + timedelta(days=2))],
            now=now,
        )


def test_accepts_naive_future_windows_with_injected_now():
    now = Instant(at=datetime(2025, 1, 10, tzinfo=timezone.utc))
    let_start = datetime(2025, 1, 11, 8)  # naive
    search_start = datetime(2025, 1, 12, 8)  # naive

    offer = SwapOffer.create(
        requester=Participant(email="p1@example.com"),
        schedule_id="primary",
        let_window=_window(let_start),
        search_windows=[_window(search_start)],
        now=now,
    )

    assert offer.let_window.start == let_start
