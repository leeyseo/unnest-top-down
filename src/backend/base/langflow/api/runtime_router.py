"""Router profile used by the on-premise runtime image."""

from fastapi import APIRouter

from langflow.api.v1.login import router as login_router
from langflow.api.v1.runtime import router as runtime_api_router

router = APIRouter()
router.include_router(login_router, prefix="/api/v1")
router.include_router(runtime_api_router)
