"""Liveness and readiness probes."""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import error_response
from app.readiness import ReadinessRegistry

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/readyz", response_model=None)
def readyz(request: Request) -> dict[str, Any] | JSONResponse:
    """Readiness: every registered dependency check passes."""
    registry: ReadinessRegistry = request.app.state.readiness
    checks = registry.run()
    failed = sorted(name for name, result in checks.items() if result != "ok")
    if failed:
        return error_response(503, "not_ready", f"Unavailable: {', '.join(failed)}", checks=checks)
    return {"status": "ok", "checks": checks}
