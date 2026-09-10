"""FastAPI app: serves the chat UI and proxies streaming chat to Ollama."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import corpus
from .planning_api import router as planning_router
from .config import get_settings
from .ollama_client import OllamaClient, OllamaError, OllamaUnavailable
from .store import Store
from .schemas import ChatRequest, Health, ModelInfo

logger = logging.getLogger("catpc")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ollama = OllamaClient(settings)
    app.state.store = Store(settings.store_path)
    yield
    await app.state.ollama.aclose()
    app.state.store.close()


app = FastAPI(title="Standards assistant", lifespan=lifespan)
app.include_router(planning_router)


@app.get("/api/health", response_model=Health)
async def health() -> Health:
    try:
        version = await app.state.ollama.version()
    except OllamaUnavailable as exc:
        return Health(
            ollama_reachable=False,
            host=settings.ollama_host,
            detail=f"No response from Ollama. Start it with `ollama serve`. ({exc})",
        )
    return Health(
        ollama_reachable=True,
        host=settings.ollama_host,
        version=version,
        default_model=settings.default_model,
    )


@app.get("/api/models", response_model=list[ModelInfo])
async def models() -> list[ModelInfo]:
    try:
        return [ModelInfo(**entry) for entry in await app.state.ollama.list_models()]
    except OllamaUnavailable:
        return []


@app.get("/api/corpus")
async def corpus_summary() -> dict[str, Any]:
    try:
        return corpus.summary()
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    model = request.model or settings.default_model
    messages = [{"role": "system", "content": settings.system_prompt}]
    messages += [m.model_dump() for m in request.messages]

    async def events() -> AsyncIterator[str]:
        yield _sse("start", {"model": model})
        try:
            async for chunk in app.state.ollama.chat(
                model=model,
                messages=messages,
                think=request.think,
                temperature=request.temperature,
            ):
                message = chunk.get("message") or {}
                if thinking := message.get("thinking"):
                    yield _sse("thinking", {"text": thinking})
                if content := message.get("content"):
                    yield _sse("token", {"text": content})
                if chunk.get("done"):
                    yield _sse("done", _stats(chunk))
        except (OllamaError, OllamaUnavailable) as exc:
            logger.warning("chat failed for model %s: %s", model, exc)
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stats(chunk: dict[str, Any]) -> dict[str, Any]:
    """Token counts and wall time, in units the UI can show without arithmetic."""
    eval_count = chunk.get("eval_count")
    eval_duration = chunk.get("eval_duration")  # nanoseconds
    tokens_per_second = None
    if eval_count and eval_duration:
        tokens_per_second = round(eval_count / (eval_duration / 1e9), 1)
    return {
        "prompt_tokens": chunk.get("prompt_eval_count"),
        "response_tokens": eval_count,
        "tokens_per_second": tokens_per_second,
        "total_seconds": round(chunk["total_duration"] / 1e9, 2)
        if chunk.get("total_duration")
        else None,
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(settings.frontend_dir / "planner.html")


@app.get("/chat")
async def general_chat() -> FileResponse:
    return FileResponse(settings.frontend_dir / "index.html")


app.mount("/", StaticFiles(directory=settings.frontend_dir), name="frontend")
