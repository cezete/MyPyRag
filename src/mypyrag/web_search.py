"""Bounded asynchronous client for the private SearXNG search backend."""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from mypyrag.config import Config

_MAX_WARNINGS = 10
_MAX_WARNING_CHARS = 300


class WebSearchError(Exception):
    """A safe, structured failure suitable for returning through MCP."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class SearxngSearchClient:
    """Single-provider web search client; no page fetching or result reranking."""

    def __init__(self, config: Config, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.web_search_backend_url.rstrip("/"),
            follow_redirects=False,
            timeout=config.web_search_timeout_seconds,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: str, max_results: int | None = None) -> dict[str, Any]:
        started = time.monotonic()
        selected_query = query.strip()
        if not self._config.web_search_enabled:
            raise WebSearchError(
                "web_search_disabled", "Web search is disabled by configuration.", retryable=False
            )
        if not self._config.web_search_backend_url:
            raise WebSearchError(
                "web_search_not_configured",
                "The web search backend URL is not configured.",
                retryable=False,
            )
        if not selected_query:
            raise WebSearchError(
                "invalid_input", "query must not be empty", retryable=False
            )
        if len(selected_query) > self._config.web_search_max_query_chars:
            raise WebSearchError(
                "invalid_input",
                f"query must not exceed {self._config.web_search_max_query_chars} characters",
                retryable=False,
            )
        limit = (
            self._config.web_search_default_results if max_results is None else max_results
        )
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise WebSearchError(
                "invalid_input", "max_results must be an integer", retryable=False
            )
        if not 1 <= limit <= self._config.web_search_max_results:
            raise WebSearchError(
                "invalid_input",
                f"max_results must be between 1 and {self._config.web_search_max_results}",
                retryable=False,
            )

        payload = await self._request(selected_query)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise WebSearchError(
                "malformed_response",
                "The web search provider returned an invalid results list.",
                retryable=False,
            )
        warnings = _provider_warnings(payload.get("unresponsive_engines"))
        if not raw_results and warnings:
            raise WebSearchError(
                "upstream_unavailable",
                "All responding web search engines failed or were unavailable.",
                retryable=True,
            )

        results: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in raw_results:
            if len(results) >= limit:
                break
            if not isinstance(item, dict):
                continue
            normalized_url = _valid_url(item.get("url"))
            if normalized_url is None or normalized_url in seen:
                continue
            seen.add(normalized_url)
            title = _plain_text(item.get("title"), self._config.web_search_max_snippet_chars)
            snippet = _plain_text(
                item.get("content"), self._config.web_search_max_snippet_chars
            )
            results.append(
                {
                    "rank": len(results) + 1,
                    "title": title,
                    "url": normalized_url,
                    "snippet": snippet,
                }
            )

        response: dict[str, Any] = {
            "status": "ok" if results else "no_results",
            "query": selected_query,
            "provider": "searxng",
            "result_count": len(results),
            "results": results,
            "timings_ms": {"total": round((time.monotonic() - started) * 1000, 1)},
        }
        if warnings:
            response["warnings"] = warnings
        return response

    async def _request(self, query: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._config.web_search_timeout_seconds
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSearchError(
                    "provider_timeout", "The web search provider timed out.", retryable=True
                )
            try:
                async with asyncio.timeout(remaining):
                    response = await self._client.send(
                        self._client.build_request(
                            "GET", "/search", params={"q": query, "format": "json"}
                        ),
                        stream=True,
                        follow_redirects=False,
                    )
                    try:
                        if response.status_code >= 400:
                            retryable = response.status_code == 429 or response.status_code >= 500
                            if retryable and attempt == 0:
                                delay = _retry_after_seconds(response)
                                if delay <= deadline - time.monotonic():
                                    if delay:
                                        await asyncio.sleep(delay)
                                    continue
                            raise WebSearchError(
                                "provider_http_error",
                                f"The web search provider returned HTTP {response.status_code}.",
                                retryable=retryable,
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self._config.web_search_max_response_bytes:
                                raise WebSearchError(
                                    "response_too_large",
                                    "The web search provider response exceeded the configured "
                                    "size limit.",
                                    retryable=False,
                                )
                    finally:
                        await response.aclose()
            except (TimeoutError, httpx.TimeoutException) as exc:
                if attempt == 0:
                    continue
                raise WebSearchError(
                    "provider_timeout", "The web search provider timed out.", retryable=True
                ) from exc
            except httpx.RequestError as exc:
                if attempt == 0:
                    continue
                raise WebSearchError(
                    "provider_connection_error",
                    "The web search provider is unreachable.",
                    retryable=True,
                ) from exc

            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebSearchError(
                    "malformed_response",
                    "The web search provider returned malformed or non-JSON content.",
                    retryable=False,
                ) from exc
            if not isinstance(decoded, dict):
                raise WebSearchError(
                    "malformed_response",
                    "The web search provider returned an invalid JSON object.",
                    retryable=False,
                )
            return decoded
        raise AssertionError("unreachable")


def _valid_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    netloc = parsed.hostname.casefold()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, parsed.query, ""))


def _plain_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    rendered = " ".join(parser.parts)
    return re.sub(r"\s+", " ", html.unescape(rendered)).strip()[:maximum]


def _provider_warnings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    warnings: list[str] = []
    for item in value[:_MAX_WARNINGS]:
        if isinstance(item, (list, tuple)):
            rendered = ": ".join(str(part) for part in item[:2])
        elif isinstance(item, dict):
            rendered = ": ".join(str(part) for part in list(item.values())[:2])
        else:
            rendered = str(item)
        rendered = re.sub(r"\s+", " ", rendered).strip()[:_MAX_WARNING_CHARS]
        if rendered:
            warnings.append(rendered)
    return warnings


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return 0.0
    try:
        return max(0.0, min(float(value), 5.0))
    except ValueError:
        try:
            target = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return max(0.0, min(target - time.time(), 5.0))
