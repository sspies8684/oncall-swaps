from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Instant(BaseModel):
    """Represents a timezone-aware point in time (UTC-normalized)."""

    value: datetime = Field(alias="at")
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _normalize_timezone(self) -> "Instant":
        moment = self.value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = moment.astimezone(timezone.utc)
        object.__setattr__(self, "value", moment)
        return self

    @classmethod
    def utc_now(cls) -> "Instant":
        return cls(at=datetime.now(timezone.utc))

    def to_datetime(self) -> datetime:
        return self.value
