"""Narrow local shutdown signal for the Windows launcher; no Docker socket access."""

import json
import logging
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/system", tags=["Local application control"])
log = logging.getLogger("ticketflow.control")
FRESH_SECONDS = 8
MAX_CONTROLLER_BYTES = 8192
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
UNAVAILABLE = "Start TicketFlow with Start-App.cmd to enable its shutdown button."


class ShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = Field(strict=True)


def _directory():
    value = os.environ.get("TICKETFLOW_CONTROL_DIR")
    return Path(value) if value else None


def _controller():
    directory = _directory()
    if directory is None:
        return None
    try:
        path = directory / "controller.json"
        if path.stat().st_size > MAX_CONTROLLER_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            return None
        session = value.get("session_id")
        if not isinstance(session, str) or str(uuid.UUID(session)) != session.lower():
            return None
        port = value.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        updated = value.get("updated_at")
        if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated):
            return None
        age = time.time() - updated
        if not -2 <= age <= FRESH_SECONDS:
            return None
        if value.get("state") not in {"starting", "ready", "stopping", "stopped", "error"}:
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


@router.get("/control")
def system_control():
    controller = _controller()
    if controller is None:
        return {"enabled": False, "state": "unavailable", "detail": UNAVAILABLE}
    state = controller["state"]
    details = {
        "starting": "TicketFlow is starting. Shutdown will be available when it is ready.",
        "ready": "Stop TicketFlow while keeping saved bookings.",
        "stopping": "TicketFlow is stopping. Saved bookings are preserved.",
        "stopped": "TicketFlow has stopped. Use Start-App.cmd to start it again.",
        "error": "TicketFlow could not stop. Run Stop-App.cmd or retry the launcher.",
    }
    return {"enabled": state == "ready", "state": state, "detail": details[state]}


def _authorize(request, port):
    """Reject browser cross-origin and DNS-rebinding requests before any write."""
    try:
        origin = urlsplit(request.headers.get("origin", ""))
        target = urlsplit(str(request.url))
        same_origin = (
            origin.scheme == target.scheme == "http"
            and origin.hostname == target.hostname
            and origin.hostname in LOOPBACK_HOSTS
            and (origin.port or 80) == (target.port or 80) == port
            and not origin.username and not origin.password
            and origin.path in {"", "/"} and not origin.query and not origin.fragment
        )
    except ValueError:
        same_origin = False
    if (not same_origin or request.headers.get("x-ticketflow-control") != "shutdown"
            or request.headers.get("sec-fetch-site") not in {None, "same-origin"}):
        raise HTTPException(403, "Use the shutdown button from this local TicketFlow page.")


@router.post("/shutdown", status_code=202)
def request_shutdown(body: ShutdownRequest, request: Request):
    if not body.confirm:
        raise HTTPException(400, "Confirm that you want to stop TicketFlow.")
    controller = _controller()
    if controller is None or controller["state"] not in {"ready", "stopping"}:
        raise HTTPException(503, UNAVAILABLE)
    _authorize(request, controller["port"])
    directory = _directory()
    temporary = None
    try:
        # Repeated clicks write the same session intent. A new launcher session
        # ignores this file, so an old tab cannot stop a later app incarnation.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix="shutdown-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"session_id": controller["session_id"], "requested_at": time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "shutdown-request.json")
    except OSError as error:
        log.warning("Local shutdown signal unavailable: %s", type(error).__name__)
        raise HTTPException(503, "The shutdown control is unavailable. Run Stop-App.cmd instead.") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"accepted": True, "state": "stopping",
            "detail": "TicketFlow is stopping. Your saved bookings will be kept."}
