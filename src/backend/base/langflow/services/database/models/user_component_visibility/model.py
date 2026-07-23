from datetime import datetime, timezone

import sqlalchemy as sa
from pydantic import field_validator
from sqlalchemy import Column, ForeignKey
from sqlmodel import Field, SQLModel

from langflow.schema.serialize import UUIDstr
from langflow.services.database.models.user.model import UserRead

MAX_VISIBILITY_KEYS = 1000
MAX_VISIBILITY_KEY_LENGTH = 255


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ComponentVisibilityFields(SQLModel):
    hidden_bundles: list[str] = Field(default_factory=list, sa_column=Column(sa.JSON, nullable=False))
    hidden_components: list[str] = Field(default_factory=list, sa_column=Column(sa.JSON, nullable=False))

    @field_validator("hidden_bundles", "hidden_components")
    @classmethod
    def normalize_keys(cls, values: list[str]) -> list[str]:
        if len(values) > MAX_VISIBILITY_KEYS:
            msg = "At most 1000 visibility keys may be stored"
            raise ValueError(msg)
        normalized = {value.strip() for value in values if value.strip()}
        if any(len(value) > MAX_VISIBILITY_KEY_LENGTH for value in normalized):
            msg = "Visibility keys may not exceed 255 characters"
            raise ValueError(msg)
        return sorted(normalized)


class UserComponentVisibility(ComponentVisibilityFields, table=True):  # type: ignore[call-arg]
    __tablename__ = "user_component_visibility"

    user_id: UUIDstr = Field(sa_column=Column(sa.Uuid(), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True))
    updated_by: UUIDstr | None = Field(
        default=None,
        sa_column=Column(sa.Uuid(), ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=Column(sa.DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=Column(sa.DateTime(timezone=True), nullable=False),
    )


class ComponentVisibilityUpdate(ComponentVisibilityFields):
    pass


class ComponentVisibilityRead(ComponentVisibilityFields):
    user_id: UUIDstr
    updated_by: UUIDstr | None = None
    updated_at: datetime | None = None


class ComponentVisibilitySummary(SQLModel):
    hidden_bundle_count: int = 0
    hidden_component_count: int = 0


class AdminUserRead(UserRead):
    component_visibility: ComponentVisibilitySummary = Field(default_factory=ComponentVisibilitySummary)
