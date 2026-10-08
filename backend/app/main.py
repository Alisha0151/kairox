"""Kairox API (FastAPI). Run:  uvicorn app.main:app --port 8000

All routes come from ``app.api.ROUTES`` so the FastAPI and stdlib servers can never drift apart.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .api import ROUTES, Context

ctx = Context()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx.start_background()
    yield
    ctx.stop()


app = FastAPI(title="Kairox", version="1.0.0", lifespan=lifespan,
              description="Causal time-travel debugging & recovery platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _register(method: str, path: str, fn):
    async def endpoint(request: Request):
        try:
            body = await request.json() if method == "POST" else {}
        except Exception:
            body = {}
        status, obj = await run_in_threadpool(fn, ctx, dict(request.path_params), dict(request.query_params), body)
        return JSONResponse(obj, status_code=status)

    app.add_api_route(path, endpoint, methods=[method], name=fn.__name__, summary=f"{method} {path}")


for _m, _p, _fn in ROUTES:
    _register(_m, _p, _fn)


@app.websocket("/ws")
async def live(ws: WebSocket):
    """Pushes {type: events|incident, ...} messages so the dashboard updates instantly."""
    await ws.accept()
    loop, q = asyncio.get_running_loop(), asyncio.Queue(maxsize=200)

    def push(msg: dict):
        loop.call_soon_threadsafe(lambda: q.full() or q.put_nowait(msg))

    ctx.bus.subs.append(push)
    try:
        while True:
            await ws.send_text(json.dumps(await q.get()))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ctx.bus.subs.remove(push)
