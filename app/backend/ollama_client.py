"""Thin async wrapper over the Ollama HTTP API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import Settings


class OllamaError(RuntimeError):
    """Ollama was reached but refused the request."""


class OllamaUnavailable(RuntimeError):
    """Ollama could not be reached at all."""


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.ollama_host.rstrip("/"),
            timeout=httpx.Timeout(
                settings.request_timeout, connect=settings.connect_timeout
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def version(self) -> str:
        try:
            response = await self._client.get("/api/version")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(str(exc)) from exc
        return response.json().get("version", "unknown")

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(str(exc)) from exc

        models = []
        for entry in response.json().get("models", []):
            details = entry.get("details") or {}
            name = entry.get("name", "")
            models.append(
                {
                    "name": name,
                    "parameter_size": details.get("parameter_size"),
                    "family": details.get("family"),
                    "size_bytes": entry.get("size"),
                    # Cloud-hosted models report a zero on-disk size.
                    "is_cloud": name.endswith("-cloud") or ":cloud" in name,
                }
            )
        models.sort(key=lambda m: (m["is_cloud"], m["name"]))
        return models

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        think: bool = False,
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat chunks as decoded Ollama payloads.

        Models that have no thinking mode reject the `think` field outright, so
        the first rejection retries once with the field dropped.
        """
        options: dict[str, Any] = {"num_ctx": self._settings.num_ctx}
        if temperature is not None:
            options["temperature"] = temperature

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
            "options": options,
        }

        emitted = False
        try:
            async for chunk in self._stream(payload):
                emitted = True
                yield chunk
        except OllamaError as exc:
            if emitted or "think" not in str(exc).lower():
                raise
            payload.pop("think", None)
            async for chunk in self._stream(payload):
                yield chunk

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise OllamaError(_error_message(body, response.status_code))
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise OllamaError(str(chunk["error"]))
                    yield chunk
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(str(exc)) from exc


def _error_message(body: bytes, status_code: int) -> str:
    try:
        return str(json.loads(body).get("error", body.decode("utf-8", "replace")))
    except json.JSONDecodeError:
        return f"Ollama returned HTTP {status_code}: {body.decode('utf-8', 'replace')[:300]}"
