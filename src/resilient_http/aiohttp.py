"""Create native aiohttp sessions configured with urllib3 retry policies."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Union, cast

import aiohttp
from urllib3.exceptions import ConnectTimeoutError, HTTPError, MaxRetryError, ProtocolError
from urllib3.util import Retry

from .client import create_retry

_TimeoutPair = tuple[Optional[float], Optional[float]]
_Timeout = Union[float, _TimeoutPair, aiohttp.ClientTimeout]
_Handler = Callable[[aiohttp.ClientRequest], Awaitable[aiohttp.ClientResponse]]


@dataclass(frozen=True)
class _RetryResponse:
    """The small response interface consumed by urllib3 Retry."""

    status: int
    headers: Mapping[str, str]

    def get_redirect_location(self) -> None:
        """Redirects remain aiohttp's responsibility, not Retry's."""

        return None


def _as_retry_error(error: aiohttp.ClientError) -> Exception:
    """Map aiohttp transport errors to urllib3's retry categories.

    The mapped exception is used only for ``Retry.increment`` accounting. The
    caller always receives the original aiohttp exception when retries are
    exhausted.
    """

    # TLS and payload failures are deliberately "other", matching the
    # conservative Requests policy where other=0 by default.
    if isinstance(
        error,
        (
            aiohttp.ClientConnectorCertificateError,
            aiohttp.ClientConnectorSSLError,
            aiohttp.ClientSSLError,
            aiohttp.ServerFingerprintMismatch,
            aiohttp.ClientPayloadError,
        ),
    ):
        return error

    # These failures happen before a connection is established. urllib3 does
    # not apply allowed_methods to its connect category.
    if isinstance(error, (aiohttp.ConnectionTimeoutError, aiohttp.ClientConnectorError)):
        return ConnectTimeoutError(str(error))

    # Once a connection exists, a retry may duplicate a request that reached
    # the server. Map these to read/protocol so allowed_methods is enforced.
    if isinstance(error, aiohttp.ClientConnectionError):
        return ProtocolError(str(error))

    return error


def _increment_for_error(
    retry: Retry,
    request: aiohttp.ClientRequest,
    error: aiohttp.ClientError,
) -> Optional[Retry]:
    """Return the next immutable Retry state, or None when exhausted."""

    mapped_error = _as_retry_error(error)
    try:
        return retry.increment(
            method=request.method,
            url=str(request.url),
            error=mapped_error,
        )
    except (HTTPError, aiohttp.ClientError):
        # Retry.increment may re-raise the mapped error when a category is
        # disabled, or MaxRetryError when a counter is exhausted. Neither
        # should replace the original aiohttp exception.
        return None


def _increment_for_response(
    retry: Retry,
    request: aiohttp.ClientRequest,
    response: _RetryResponse,
) -> Optional[Retry]:
    """Return the next immutable Retry state, or None when exhausted."""

    try:
        return retry.increment(
            method=request.method,
            url=str(request.url),
            response=cast(Any, response),
        )
    except MaxRetryError:
        return None


def _retry_delay(retry: Retry, response: Optional[_RetryResponse] = None) -> float:
    """Compute Retry-After or exponential backoff without blocking the loop."""

    if retry.respect_retry_after_header and response is not None:
        retry_after = retry.get_retry_after(cast(Any, response))
        if retry_after:
            return retry_after
    return retry.get_backoff_time()


def _raise_for_retry_status(response: aiohttp.ClientResponse) -> None:
    """Raise aiohttp's native status error for an exhausted status policy."""

    if response.status >= 400:
        # Preserve aiohttp's normal error construction and release behavior for
        # the status range understood by ClientResponse.raise_for_status().
        response.raise_for_status()
        return  # pragma: no cover - raise_for_status always raises here

    # urllib3 permits callers to put any integer in status_forcelist, including
    # redirects. aiohttp considers 3xx successful for raise_for_status(), so
    # construct the same native exception explicitly after releasing the
    # response hidden by the exhausted retry policy.
    response.release()
    raise aiohttp.ClientResponseError(
        response.request_info,
        response.history,
        status=response.status,
        message=response.reason or "",
        headers=response.headers,
    )


class _RetryMiddleware:
    """aiohttp client middleware backed by an immutable urllib3 Retry."""

    __slots__ = ("_policy",)

    def __init__(self, policy: Retry) -> None:
        self._policy = policy

    async def __call__(
        self,
        request: aiohttp.ClientRequest,
        handler: _Handler,
    ) -> aiohttp.ClientResponse:
        retry = self._policy

        while True:
            try:
                response = await handler(request)
            except aiohttp.ClientError as error:
                next_retry = _increment_for_error(retry, request, error)
                if next_retry is None:
                    raise

                delay = _retry_delay(next_retry)
                if delay > 0:
                    await asyncio.sleep(delay)
                retry = next_retry
                continue

            retry_response = _RetryResponse(
                status=response.status,
                headers=response.headers,
            )
            if not retry.is_retry(
                request.method,
                response.status,
                has_retry_after="Retry-After" in response.headers,
            ):
                return response

            next_retry = _increment_for_response(retry, request, retry_response)
            if next_retry is None:
                if retry.raise_on_status:
                    _raise_for_retry_status(response)
                return response

            try:
                delay = _retry_delay(next_retry, retry_response)
            finally:
                # A response hidden by a retry must never retain a connection.
                # release() reuses a fully received connection and closes an
                # unread one, following aiohttp's native lifecycle rules.
                response.release()

            if delay > 0:
                await asyncio.sleep(delay)
            retry = next_retry


def _validate_timeout_value(value: object, *, name: str) -> Optional[float]:
    """Return one normalized positive finite Requests-style timeout value."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number or None")

    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be finite and greater than 0") from error

    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be finite and greater than 0")
    return normalized


def _create_timeout(timeout: Optional[_Timeout]) -> aiohttp.ClientTimeout:
    """Translate the Requests-style timeout helper to aiohttp settings."""

    if isinstance(timeout, aiohttp.ClientTimeout):
        return timeout

    if timeout is None:
        # Passing None to ClientSession selects aiohttp's five-minute default;
        # an explicit ClientTimeout is required to match Requests' no-timeout
        # behavior.
        return aiohttp.ClientTimeout(total=None)

    if isinstance(timeout, tuple):
        if len(timeout) != 2:
            raise TypeError("timeout tuple must contain (connect, read)")
        connect, read = timeout
        connect = _validate_timeout_value(connect, name="connect timeout")
        read = _validate_timeout_value(read, name="read timeout")
        return aiohttp.ClientTimeout(
            total=None,
            connect=connect,
            sock_connect=connect,
            sock_read=read,
        )

    if isinstance(timeout, bool):
        raise TypeError("timeout must be a number, (connect, read) tuple, ClientTimeout, or None")

    if isinstance(timeout, (int, float)):
        value = _validate_timeout_value(timeout, name="timeout")
        assert value is not None
        return aiohttp.ClientTimeout(
            total=None,
            connect=value,
            sock_connect=value,
            sock_read=value,
        )

    raise TypeError("timeout must be a number, (connect, read) tuple, ClientTimeout, or None")


def _require_retry_connection_switch() -> None:
    """Ensure aiohttp still exposes the private retry switch we rely on."""

    attrs = getattr(aiohttp.ClientSession, "ATTRS", ())
    try:
        supported = "_retry_connection" in attrs
    except TypeError:
        supported = False

    if not supported:
        raise RuntimeError(
            "this aiohttp version does not expose ClientSession._retry_connection; "
            "retry budgets cannot be enforced safely"
        )


def create_session(
    retry: Retry,
    *,
    timeout: Optional[_Timeout] = None,
) -> aiohttp.ClientSession:
    """Return a native aiohttp ClientSession with async urllib3 retries.

    The factory must be called while an event loop is running, just like the
    native ``aiohttp.ClientSession`` constructor. Each call owns an independent
    connector and connection pool.
    """

    if not isinstance(retry, Retry):
        raise TypeError("retry must be an urllib3.util.Retry")

    _require_retry_connection_switch()

    session = aiohttp.ClientSession(
        timeout=_create_timeout(timeout),
        middlewares=(_RetryMiddleware(retry),),
    )

    # aiohttp performs an additional transparent retry for selected failures
    # on idempotent methods. It wraps the middleware and would restart the
    # entire Retry policy after exhaustion, so disable it to keep total and
    # per-category budgets exact. aiohttp exposes no public switch in 3.x.
    session._retry_connection = False
    return session


__all__ = [
    "Retry",
    "create_retry",
    "create_session",
]
