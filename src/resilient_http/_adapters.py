"""Requests adapters that observe attempts without making retry decisions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional, cast
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .config import PoolConfig


class AttemptState:
    """Physical-send count for one logical client request."""

    __slots__ = ("attempts", "origin", "retries")

    def __init__(self) -> None:
        self.attempts = 0
        self.origin: Optional[tuple[str, str, int]] = None
        self.retries = 0


_ATTEMPT_STATE: ContextVar[Optional[AttemptState]] = ContextVar(
    "resilient_http_attempt_state",
    default=None,
)


@contextmanager
def track_attempts() -> Iterator[AttemptState]:
    """Track adapter sends and urllib3 retries for the current context."""

    state = AttemptState()
    token = _ATTEMPT_STATE.set(state)
    try:
        yield state
    finally:
        _ATTEMPT_STATE.reset(token)


class _ObservedRetry(Retry):
    """Delegate to urllib3 Retry while recording each scheduled re-attempt."""

    @classmethod
    def from_retry(cls, retry: Retry) -> _ObservedRetry:
        return cls(
            total=retry.total,
            connect=retry.connect,
            read=retry.read,
            redirect=retry.redirect,
            status=retry.status,
            other=retry.other,
            allowed_methods=retry.allowed_methods,
            status_forcelist=retry.status_forcelist,
            backoff_factor=retry.backoff_factor,
            backoff_max=retry.backoff_max,
            raise_on_redirect=retry.raise_on_redirect,
            raise_on_status=retry.raise_on_status,
            history=retry.history,
            respect_retry_after_header=retry.respect_retry_after_header,
            remove_headers_on_redirect=retry.remove_headers_on_redirect,
            backoff_jitter=retry.backoff_jitter,
            retry_after_max=retry.retry_after_max,
        )

    def increment(self, *args: Any, **kwargs: Any) -> _ObservedRetry:
        retry = super().increment(*args, **kwargs)
        return cast(_ObservedRetry, retry)

    def sleep(self, response: Any = None) -> None:
        super().sleep(response)
        state = _ATTEMPT_STATE.get()
        if state is not None:
            # urllib3 calls sleep immediately before recursively sending the
            # accepted retry. Count only after sleep succeeds so a malformed
            # Retry-After header is not reported as a physical attempt.
            state.attempts += 1
            state.retries += 1


def _origin(url: str) -> Optional[tuple[str, str, int]]:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or hostname is None:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, hostname.lower(), port


class _ObservedHTTPAdapter(HTTPAdapter):
    """Count logical sends and redirects; urllib3 counts its internal retries."""

    def send(
        self,
        request: requests.PreparedRequest,
        **kwargs: Any,
    ) -> requests.Response:
        state = _ATTEMPT_STATE.get()
        if state is not None:
            request_origin = _origin(request.url or "")
            if state.origin is None:
                state.origin = request_origin
            elif request_origin is not None and request_origin != state.origin:
                raise requests.exceptions.InvalidURL(
                    "cross-origin redirects are disabled by the organization HTTP client",
                    request=request,
                )
            state.attempts += 1
        return super().send(request, **kwargs)


def build_adapter(retry: Retry, pool: PoolConfig) -> HTTPAdapter:
    """Create one organization-standard HTTPAdapter."""

    return _ObservedHTTPAdapter(
        max_retries=_ObservedRetry.from_retry(retry),
        pool_connections=pool.connections,
        pool_maxsize=pool.maxsize,
        pool_block=pool.block,
    )
