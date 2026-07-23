from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from macro_platform.api.dependencies import DatabaseDep

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(database: DatabaseDep) -> JSONResponse:
    is_ready = await database.ready()
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ok" if is_ready else "not_ready", "database": is_ready},
    )
