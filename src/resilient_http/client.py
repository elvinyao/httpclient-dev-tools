"""HTTPX clients with upstream retries and optional domain-error mapping."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, NoReturn, Optional, Union

import httpx

from ._vendor.httpx_retries import Retry, RetryTransport
from .config import ErrorMappingRule, HttpClientConfig
from .exceptions import BaseHttpError, NonReplayableRequestError

ConfigInput = Union[HttpClientConfig, Mapping[str, Any]]
Url = Union[str, httpx.URL]

_REQUEST_STATE_EXTENSION = "resilient_http.request_state"


class _RequestState:
    """Mutable state shared by retries and redirect requests."""

    __slots__ = ("attempts", "transport_completed")

    def __init__(self) -> None:
        self.attempts = 0
        self.transport_completed = False


def _request_state(
    request: httpx.Request,
    *,
    create: bool = False,
) -> Optional[_RequestState]:
    state = request.extensions.get(_REQUEST_STATE_EXTENSION)
    if isinstance(state, _RequestState):
        return state
    if not create:
        return None

    state = _RequestState()
    request.extensions[_REQUEST_STATE_EXTENSION] = state
    return state


def _prepare_request_extensions(kwargs: dict[str, Any]) -> None:
    raw_extensions = kwargs.get("extensions")
    if raw_extensions is None:
        extensions: dict[str, Any] = {}
    elif isinstance(raw_extensions, Mapping):
        extensions = dict(raw_extensions)
    else:
        raise TypeError("request extensions must be a mapping")

    # Never trust or mutate caller-owned extension state.
    extensions[_REQUEST_STATE_EXTENSION] = _RequestState()
    kwargs["extensions"] = extensions


def _config_from_value(config: ConfigInput) -> HttpClientConfig:
    if isinstance(config, HttpClientConfig):
        return config
    if isinstance(config, Mapping):
        return HttpClientConfig.from_dict(config)
    raise TypeError("config must be an HttpClientConfig or a mapping")


def _configured_retry(config: HttpClientConfig) -> Retry:
    retry = config.retry
    if not isinstance(retry, Retry):  # Defensive: __post_init__ normalizes it.
        raise TypeError("config.retry must be a resilient_http.Retry")
    return retry


class _AttemptTrackingTransport(httpx.BaseTransport):
    """Observe physical sends without making any retry decisions."""

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        state = _request_state(request, create=True)
        assert state is not None
        state.attempts += 1
        state.transport_completed = False
        response = self._transport.handle_request(request)
        state.transport_completed = True
        return response

    def close(self) -> None:
        self._transport.close()


class _AsyncAttemptTrackingTransport(httpx.AsyncBaseTransport):
    """Asynchronous physical-send observer."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        state = _request_state(request, create=True)
        assert state is not None
        state.attempts += 1
        state.transport_completed = False
        response = await self._transport.handle_async_request(request)
        state.transport_completed = True
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def _sync_retry_transport(
    retry: Retry,
    transport: Optional[httpx.BaseTransport],
) -> RetryTransport:
    if isinstance(transport, RetryTransport):
        raise ValueError(
            "transport must be the underlying transport, not RetryTransport; "
            "this client installs exactly one vendored retry layer"
        )
    if transport is not None and not isinstance(transport, httpx.BaseTransport):
        raise TypeError("transport must be an httpx.BaseTransport")
    inner = transport if transport is not None else httpx.HTTPTransport()
    return RetryTransport(
        transport=_AttemptTrackingTransport(inner),
        retry=retry,
    )


def _async_retry_transport(
    retry: Retry,
    transport: Optional[httpx.AsyncBaseTransport],
) -> RetryTransport:
    if isinstance(transport, RetryTransport):
        raise ValueError(
            "transport must be the underlying transport, not RetryTransport; "
            "this client installs exactly one vendored retry layer"
        )
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise TypeError("transport must be an httpx.AsyncBaseTransport")
    inner = transport if transport is not None else httpx.AsyncHTTPTransport()
    return RetryTransport(
        transport=_AsyncAttemptTrackingTransport(inner),
        retry=retry,
    )


def _method_can_retry(retry: Retry, method: str) -> bool:
    if retry.total <= retry.attempts_made:
        return False
    has_retryable_status = any(
        isinstance(code, int) and 100 <= code <= 599 for code in retry.status_forcelist
    )
    if not has_retryable_status and not retry.retryable_exceptions:
        return False
    try:
        return retry.is_retryable_method(method)
    except ValueError:
        return False


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


def _non_replayable_body_reason(kwargs: Mapping[str, Any]) -> Optional[str]:
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
    retry: Retry,
    method: str,
    url: Url,
    kwargs: Mapping[str, Any],
) -> None:
    if not _method_can_retry(retry, method):
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


def _safe_url(url: Url) -> str:
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
        return "<invalid-url>"


def _request_from_error(error: Exception) -> Optional[httpx.Request]:
    if not isinstance(error, httpx.RequestError):
        return None
    try:
        return error.request
    except RuntimeError:
        return None


def _request_details(
    fallback_method: str,
    fallback_url: Url,
    *,
    response: Optional[httpx.Response] = None,
    error: Optional[Exception] = None,
) -> tuple[str, str]:
    request: Optional[httpx.Request] = None
    if response is not None:
        try:
            request = response.request
        except RuntimeError:
            request = None
    elif error is not None:
        request = _request_from_error(error)

    if request is not None:
        return request.method, _safe_url(request.url)
    return fallback_method.upper(), _safe_url(fallback_url)


def _attempts_from_request(request: Optional[httpx.Request]) -> int:
    if request is None:
        return 1
    state = _request_state(request)
    if state is not None and state.attempts >= 1:
        return state.attempts
    return 1


def _attempts_for_response(response: httpx.Response) -> int:
    try:
        request = response.request
    except RuntimeError:
        request = None
    return _attempts_from_request(request)


def _attempts_for_error(error: Exception) -> int:
    return _attempts_from_request(_request_from_error(error))


def _transport_completed_for_error(error: Exception) -> bool:
    request = _request_from_error(error)
    if request is None:
        return False
    state = _request_state(request)
    return state is not None and state.transport_completed


def _retry_budget_exhausted(retry: Retry, attempts: int) -> bool:
    remaining_retries = retry.total - retry.attempts_made
    return remaining_retries > 0 and attempts >= remaining_retries + 1


def _status_retry_exhausted(
    retry: Retry,
    method: str,
    status_code: int,
    attempts: int,
) -> bool:
    if not _retry_budget_exhausted(retry, attempts):
        return False
    try:
        return retry.is_retryable_method(method) and retry.is_retryable_status_code(status_code)
    except ValueError:
        return False


def _exception_retry_exhausted(
    retry: Retry,
    method: str,
    error: Exception,
    attempts: int,
) -> bool:
    if not _retry_budget_exhausted(retry, attempts):
        return False
    try:
        return retry.is_retryable_method(method) and retry.is_retryable_exception(error)
    except ValueError:
        return False


def _raise_failure(
    error_type: type[BaseHttpError],
    *,
    retry: Retry,
    fallback_method: str,
    fallback_url: Url,
    attempts: int,
    rule: Optional[ErrorMappingRule],
    response: Optional[httpx.Response] = None,
    cause: Optional[Exception] = None,
) -> NoReturn:
    method, url = _request_details(
        fallback_method,
        fallback_url,
        response=response,
        error=cause,
    )
    status_code = response.status_code if response is not None else None
    rule_name = rule.name if rule is not None else None

    if status_code is not None:
        retry_exhausted = _status_retry_exhausted(
            retry,
            method,
            status_code,
            attempts,
        )
        reason = f"HTTP {status_code}"
    elif cause is not None:
        if _transport_completed_for_error(cause):
            retry_exhausted = False
        else:
            retry_exhausted = _exception_retry_exhausted(
                retry,
                method,
                cause,
                attempts,
            )
        reason = type(cause).__name__
    else:
        retry_exhausted = False
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
    """Synchronous HTTPX client using the vendored retry transport."""

    def __init__(
        self,
        config: ConfigInput,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = _config_from_value(config)
        self.retry = _configured_retry(self.config)

        client_options: dict[str, Any] = {
            "timeout": self.config.timeout,
            "headers": self.config.headers,
            "follow_redirects": self.config.follow_redirects,
            "transport": _sync_retry_transport(self.retry, transport),
        }
        if self.config.base_url:
            client_options["base_url"] = self.config.base_url
        self._client = httpx.Client(**client_options)

    @property
    def raw_client(self) -> httpx.Client:
        """Underlying HTTPX client; mapping and replay-safety checks are bypassed."""

        return self._client

    def request(
        self,
        method: str,
        url: Url,
        **kwargs: Any,
    ) -> httpx.Response:
        normalized_method = method.upper()
        _reject_non_replayable_body(
            self.retry,
            normalized_method,
            url,
            kwargs,
        )
        _prepare_request_extensions(kwargs)

        try:
            # This is intentionally the only logical request call. RetryTransport
            # owns every physical retry, retry decision, and backoff.
            response = self._client.request(normalized_method, url, **kwargs)
        except httpx.RequestError as error:
            if not self.config.enable_error_mapping:
                raise
            rule = self.config.error_mapping.for_exception(error)
            error_type = (
                rule.raise_as
                if rule is not None
                else self.config.error_mapping.default_system_error
            )
            _raise_failure(
                error_type,
                retry=self.retry,
                fallback_method=normalized_method,
                fallback_url=url,
                attempts=_attempts_for_error(error),
                rule=rule,
                cause=error,
            )
        except httpx.InvalidURL as error:
            if not self.config.enable_error_mapping:
                raise
            _raise_failure(
                self.config.error_mapping.default_system_error,
                retry=self.retry,
                fallback_method=normalized_method,
                fallback_url=url,
                attempts=1,
                rule=None,
                cause=error,
            )

        if not response.is_error or not self.config.enable_error_mapping:
            return response

        rule = self.config.error_mapping.for_status(response.status_code)
        if rule is not None:
            error_type = rule.raise_as
        elif 400 <= response.status_code < 500:
            error_type = self.config.error_mapping.default_business_error
        else:
            error_type = self.config.error_mapping.default_system_error

        _raise_failure(
            error_type,
            retry=self.retry,
            fallback_method=normalized_method,
            fallback_url=url,
            attempts=_attempts_for_response(response),
            rule=rule,
            response=response,
        )

    def get(self, url: Url, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: Url, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: Url, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: Url, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: Url, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncHttpClient:
    """Asynchronous HTTPX client using the vendored retry transport."""

    def __init__(
        self,
        config: ConfigInput,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = _config_from_value(config)
        self.retry = _configured_retry(self.config)

        client_options: dict[str, Any] = {
            "timeout": self.config.timeout,
            "headers": self.config.headers,
            "follow_redirects": self.config.follow_redirects,
            "transport": _async_retry_transport(self.retry, transport),
        }
        if self.config.base_url:
            client_options["base_url"] = self.config.base_url
        self._client = httpx.AsyncClient(**client_options)

    @property
    def raw_client(self) -> httpx.AsyncClient:
        """Underlying HTTPX client; mapping and replay-safety checks are bypassed."""

        return self._client

    async def request(
        self,
        method: str,
        url: Url,
        **kwargs: Any,
    ) -> httpx.Response:
        normalized_method = method.upper()
        _reject_non_replayable_body(
            self.retry,
            normalized_method,
            url,
            kwargs,
        )
        _prepare_request_extensions(kwargs)

        try:
            # RetryTransport owns all physical asynchronous attempts.
            response = await self._client.request(
                normalized_method,
                url,
                **kwargs,
            )
        except httpx.RequestError as error:
            if not self.config.enable_error_mapping:
                raise
            rule = self.config.error_mapping.for_exception(error)
            error_type = (
                rule.raise_as
                if rule is not None
                else self.config.error_mapping.default_system_error
            )
            _raise_failure(
                error_type,
                retry=self.retry,
                fallback_method=normalized_method,
                fallback_url=url,
                attempts=_attempts_for_error(error),
                rule=rule,
                cause=error,
            )
        except httpx.InvalidURL as error:
            if not self.config.enable_error_mapping:
                raise
            _raise_failure(
                self.config.error_mapping.default_system_error,
                retry=self.retry,
                fallback_method=normalized_method,
                fallback_url=url,
                attempts=1,
                rule=None,
                cause=error,
            )

        if not response.is_error or not self.config.enable_error_mapping:
            return response

        rule = self.config.error_mapping.for_status(response.status_code)
        if rule is not None:
            error_type = rule.raise_as
        elif 400 <= response.status_code < 500:
            error_type = self.config.error_mapping.default_business_error
        else:
            error_type = self.config.error_mapping.default_system_error

        _raise_failure(
            error_type,
            retry=self.retry,
            fallback_method=normalized_method,
            fallback_url=url,
            attempts=_attempts_for_response(response),
            rule=rule,
            response=response,
        )

    async def get(self, url: Url, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: Url, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: Url, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: Url, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: Url, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
