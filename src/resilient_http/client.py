"""Synchronous and asynchronous policy-driven HTTPX clients."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import (
    Any,
    Callable,
    NoReturn,
    Union,
)

import httpx

from .config import HttpClientConfig, RetryPolicy, RetryRule
from .exceptions import BaseHttpError, NonReplayableRequestError

Sleep = Callable[[float], None]
AsyncSleep = Callable[[float], Awaitable[None]]
RandomValue = Callable[[], float]
Now = Callable[[], datetime]
ConfigInput = Union[HttpClientConfig, Mapping[str, Any]]


def _config_from_value(config: ConfigInput) -> HttpClientConfig:
    if isinstance(config, HttpClientConfig):
        return config
    return HttpClientConfig.from_dict(config)


def _method_can_ever_retry(policy: RetryPolicy, method: str) -> bool:
    normalized = method.upper()
    return any(rule.max_attempts > 1 and normalized in rule.retry_methods for rule in policy.rules)


def _is_one_shot(value: Any) -> bool:
    return isinstance(
        value,
        (
            Iterator,
            AsyncIterator,
            httpx.SyncByteStream,
            httpx.AsyncByteStream,
        ),
    )


def _files_are_replayable(files: Any) -> bool:
    if isinstance(files, Mapping):
        values = list(files.values())
    elif isinstance(files, (list, tuple)):
        values = []
        for item in files:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                return False
            values.append(item[1])
    else:
        return False

    for value in values:
        payload = value
        if isinstance(value, tuple):
            if len(value) < 2:
                return False
            payload = value[1]
        if not isinstance(payload, (bytes, bytearray, memoryview, str)):
            return False
    return True


def _non_replayable_body_reason(kwargs: Mapping[str, Any]) -> str | None:
    content = kwargs.get("content")
    if content is not None and _is_one_shot(content):
        return "content is a one-shot iterator or stream"

    data = kwargs.get("data")
    if data is not None and _is_one_shot(data):
        return "data is a one-shot iterator or stream"

    files = kwargs.get("files")
    if files is not None and not _files_are_replayable(files):
        return "files contain an open stream instead of in-memory data"

    return None


def _reject_non_replayable_body(
    policy: RetryPolicy,
    method: str,
    url: str | httpx.URL,
    kwargs: Mapping[str, Any],
) -> None:
    if not _method_can_ever_retry(policy, method):
        return
    reason = _non_replayable_body_reason(kwargs)
    if reason is None:
        return

    safe_url = _safe_url(url)
    raise NonReplayableRequestError(
        f"{method.upper()} {safe_url} cannot be retried safely: {reason}",
        method=method.upper(),
        url=safe_url,
        attempts=0,
    )


def _request_details(
    fallback_method: str,
    fallback_url: str | httpx.URL,
    *,
    response: httpx.Response | None = None,
    error: Exception | None = None,
) -> tuple[str, str]:
    request: httpx.Request | None = None
    if response is not None:
        request = response.request
    elif isinstance(error, httpx.RequestError):
        request = error.request

    if request is not None:
        return request.method, _safe_url(request.url)
    return fallback_method.upper(), _safe_url(fallback_url)


def _safe_url(url: str | httpx.URL) -> str:
    """Remove credentials, query values, and fragments from error metadata."""

    try:
        parsed = httpx.URL(url)
        return str(
            parsed.copy_with(
                username=None,
                password=None,
                query=None,
                fragment=None,
            )
        )
    except (TypeError, ValueError, httpx.InvalidURL):
        # The original value may itself be an invalid URL. Avoid echoing it,
        # since it could contain credentials or query secrets.
        return "<invalid-url>"


def _retry_after_seconds(
    response: httpx.Response | None,
    now: Now,
) -> float | None:
    if response is None:
        return None

    value = response.headers.get("Retry-After")
    if value is None:
        return None

    stripped = value.strip()
    if stripped.isdigit():
        try:
            return float(int(stripped))
        except (OverflowError, ValueError):
            # The backoff policy will cap infinity to max_delay.
            return float("inf")

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current = now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (target - current).total_seconds())


def _delay(
    rule: RetryRule,
    retry_number: int,
    response: httpx.Response | None,
    random_value: RandomValue,
    now: Now,
) -> float:
    retry_after = _retry_after_seconds(response, now) if rule.backoff.respect_retry_after else None
    return rule.backoff.delay_for_retry(
        retry_number,
        retry_after=retry_after,
        random_value=random_value(),
    )


def _raise_failure(
    error_type: type[BaseHttpError],
    *,
    fallback_method: str,
    fallback_url: str | httpx.URL,
    attempts: int,
    rule: RetryRule | None,
    response: httpx.Response | None = None,
    cause: Exception | None = None,
) -> NoReturn:
    method, url = _request_details(
        fallback_method,
        fallback_url,
        response=response,
        error=cause,
    )
    status_code = response.status_code if response is not None else None
    rule_name = rule.name if rule is not None else None
    retry_exhausted = rule is not None and rule.max_attempts > 1 and method.upper() in rule.retry_methods and attempts >= rule.max_attempts

    if status_code is not None:
        reason = f"HTTP {status_code}"
    elif cause is not None:
        reason = type(cause).__name__
    else:
        reason = "unknown HTTP failure"

    message = "{} {} failed with {} after {} attempt{}".format(
        method,
        url,
        reason,
        attempts,
        "" if attempts == 1 else "s",
    )
    raised = error_type(
        message,
        method=method,
        url=url,
        attempts=attempts,
        rule_name=rule_name,
        status_code=status_code,
        retry_exhausted=retry_exhausted,
        response=response,
        cause=cause,
    )
    if cause is not None:
        raise raised from cause
    raise raised


class HttpClient:
    """Synchronous client that converts HTTPX failures into domain-safe errors."""

    def __init__(
        self,
        config: ConfigInput,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
        random_value: RandomValue = random.random,
        now: Now = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = _config_from_value(config)
        self._sleep = sleep
        self._random_value = random_value
        self._now = now

        client_options: dict[str, Any] = {
            "timeout": self.config.timeout,
            "headers": self.config.headers,
            "follow_redirects": self.config.follow_redirects,
        }
        if self.config.base_url:
            client_options["base_url"] = self.config.base_url
        if transport is not None:
            client_options["transport"] = transport
        self._client = httpx.Client(**client_options)

    @property
    def raw_client(self) -> httpx.Client:
        """The underlying HTTPX client for advanced, non-retry configuration."""

        return self._client

    def request(
        self,
        method: str,
        url: str | httpx.URL,
        **kwargs: Any,
    ) -> httpx.Response:
        attempt = 0
        normalized_method = method.upper()
        policy = self.config.retry_policy
        _reject_non_replayable_body(policy, normalized_method, url, kwargs)

        while True:
            attempt += 1
            try:
                response = self._client.request(normalized_method, url, **kwargs)
            except httpx.RequestError as error:
                rule = policy.for_exception(error)
                if rule is not None and rule.can_retry(normalized_method, attempt):
                    delay = _delay(
                        rule,
                        attempt,
                        None,
                        self._random_value,
                        self._now,
                    )
                    if delay > 0:
                        self._sleep(delay)
                    continue

                error_type = rule.raise_as if rule is not None else policy.default_system_error
                _raise_failure(
                    error_type,
                    fallback_method=normalized_method,
                    fallback_url=url,
                    attempts=attempt,
                    rule=rule,
                    cause=error,
                )
            except httpx.InvalidURL as error:
                _raise_failure(
                    policy.default_system_error,
                    fallback_method=normalized_method,
                    fallback_url=url,
                    attempts=attempt,
                    rule=None,
                    cause=error,
                )

            if not response.is_error:
                return response

            rule = policy.for_status(response.status_code)
            if rule is not None and rule.can_retry(normalized_method, attempt):
                try:
                    delay = _delay(
                        rule,
                        attempt,
                        response,
                        self._random_value,
                        self._now,
                    )
                finally:
                    response.close()
                if delay > 0:
                    self._sleep(delay)
                continue

            if rule is not None:
                error_type = rule.raise_as
            elif 400 <= response.status_code < 500:
                error_type = policy.default_business_error
            else:
                error_type = policy.default_system_error

            _raise_failure(
                error_type,
                fallback_method=normalized_method,
                fallback_url=url,
                attempts=attempt,
                rule=rule,
                response=response,
            )

    def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncHttpClient:
    """Asynchronous equivalent of :class:`HttpClient`."""

    def __init__(
        self,
        config: ConfigInput,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        random_value: RandomValue = random.random,
        now: Now = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = _config_from_value(config)
        self._sleep = sleep
        self._random_value = random_value
        self._now = now

        client_options: dict[str, Any] = {
            "timeout": self.config.timeout,
            "headers": self.config.headers,
            "follow_redirects": self.config.follow_redirects,
        }
        if self.config.base_url:
            client_options["base_url"] = self.config.base_url
        if transport is not None:
            client_options["transport"] = transport
        self._client = httpx.AsyncClient(**client_options)

    @property
    def raw_client(self) -> httpx.AsyncClient:
        return self._client

    async def request(
        self,
        method: str,
        url: str | httpx.URL,
        **kwargs: Any,
    ) -> httpx.Response:
        attempt = 0
        normalized_method = method.upper()
        policy = self.config.retry_policy
        _reject_non_replayable_body(policy, normalized_method, url, kwargs)

        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    normalized_method,
                    url,
                    **kwargs,
                )
            except httpx.RequestError as error:
                rule = policy.for_exception(error)
                if rule is not None and rule.can_retry(normalized_method, attempt):
                    delay = _delay(
                        rule,
                        attempt,
                        None,
                        self._random_value,
                        self._now,
                    )
                    if delay > 0:
                        await self._sleep(delay)
                    continue

                error_type = rule.raise_as if rule is not None else policy.default_system_error
                _raise_failure(
                    error_type,
                    fallback_method=normalized_method,
                    fallback_url=url,
                    attempts=attempt,
                    rule=rule,
                    cause=error,
                )
            except httpx.InvalidURL as error:
                _raise_failure(
                    policy.default_system_error,
                    fallback_method=normalized_method,
                    fallback_url=url,
                    attempts=attempt,
                    rule=None,
                    cause=error,
                )

            if not response.is_error:
                return response

            rule = policy.for_status(response.status_code)
            if rule is not None and rule.can_retry(normalized_method, attempt):
                try:
                    delay = _delay(
                        rule,
                        attempt,
                        response,
                        self._random_value,
                        self._now,
                    )
                finally:
                    await response.aclose()
                if delay > 0:
                    await self._sleep(delay)
                continue

            if rule is not None:
                error_type = rule.raise_as
            elif 400 <= response.status_code < 500:
                error_type = policy.default_business_error
            else:
                error_type = policy.default_system_error

            _raise_failure(
                error_type,
                fallback_method=normalized_method,
                fallback_url=url,
                attempts=attempt,
                rule=rule,
                response=response,
            )

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
