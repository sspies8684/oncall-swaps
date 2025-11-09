from datetime import datetime, timezone

from oncall_swap.domain.time import Instant


def test_instant_normalizes_naive_datetime_to_utc():
    naive = datetime(2025, 1, 1, 12, 0, 0)
    instant = Instant(at=naive)
    assert instant.to_datetime().tzinfo == timezone.utc
    assert instant.to_datetime().hour == 12


def test_instant_converts_aware_datetime_to_utc():
    aware = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    instant = Instant(at=aware)
    assert instant.to_datetime().tzinfo == timezone.utc
    assert instant.to_datetime() == aware.astimezone(timezone.utc)
