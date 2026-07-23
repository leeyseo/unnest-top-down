from datetime import datetime, timezone
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from langflow.schema.serialize import UUIDstr
from langflow.services.database.models.user_component_visibility.model import (
    ComponentVisibilityRead,
    ComponentVisibilityUpdate,
    UserComponentVisibility,
)


async def get_component_visibility(db: AsyncSession, user_id: UUID | UUIDstr) -> UserComponentVisibility | None:
    stmt = select(UserComponentVisibility).where(UserComponentVisibility.user_id == user_id)
    return (await db.exec(stmt)).first()


def effective_component_visibility(
    user_id: UUID | UUIDstr,
    visibility: UserComponentVisibility | None,
) -> ComponentVisibilityRead:
    if visibility is None:
        return ComponentVisibilityRead(user_id=user_id)
    return ComponentVisibilityRead.model_validate(visibility, from_attributes=True)


async def save_component_visibility(
    db: AsyncSession,
    user_id: UUID | UUIDstr,
    updated_by: UUID | UUIDstr,
    update: ComponentVisibilityUpdate,
) -> UserComponentVisibility | None:
    visibility = await get_component_visibility(db, user_id)
    if not update.hidden_bundles and not update.hidden_components:
        if visibility is not None:
            await db.delete(visibility)
            await db.flush()
        return None

    now = datetime.now(timezone.utc)
    if visibility is None:
        visibility = UserComponentVisibility(
            user_id=user_id,
            hidden_bundles=update.hidden_bundles,
            hidden_components=update.hidden_components,
            updated_by=updated_by,
            created_at=now,
            updated_at=now,
        )
        db.add(visibility)
    else:
        visibility.hidden_bundles = update.hidden_bundles
        visibility.hidden_components = update.hidden_components
        visibility.updated_by = updated_by
        visibility.updated_at = now

    await db.flush()
    await db.refresh(visibility)
    return visibility
