from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collector import background_poller, collector
from .config import get_settings

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="NPU Who", version="1.1.0")
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


class KillRequest(BaseModel):
    pid: int = Field(..., gt=1, description="Occupant PID from snapshot")
    force: bool = Field(False, description="Use SIGKILL immediately")
    server_id: str = Field("", description="Target machine id, e.g. a2")


@app.on_event("startup")
def _startup() -> None:
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=background_poller, args=(_stop,), daemon=True)
    _thread.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    _stop.set()


@app.get("/api/health")
def health():
    return {"ok": True, **collector.status()}


@app.get("/api/snapshot")
def snapshot():
    return collector.get()


@app.post("/api/refresh")
def refresh():
    return collector.refresh()


@app.post("/api/kill")
def kill(req: KillRequest):
    try:
        result = collector.kill_process(req.pid, force=req.force, server_id=req.server_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        snap = collector.refresh()
        result["snapshot"] = snap
    except Exception as exc:
        result["refresh_error"] = str(exc)
    return result


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def main() -> None:
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "app.main:app",
        host=cfg["listen_host"],
        port=cfg["listen_port"],
        reload=False,
    )


if __name__ == "__main__":
    main()
