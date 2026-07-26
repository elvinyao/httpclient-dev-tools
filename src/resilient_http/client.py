"""Synchronous organization-level HTTP client built on Requests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Callable, NoReturn, Optional, Union
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from urllib3.exceptions import MaxRetryError
from urllib3.util import Retry

from ._adapters import AttemptState, build_adapter, track_attempts
from .config import ErrorMappingRule, HttpClientConfig, TimeoutConfig
from .exceptions import BaseHttpError, NonReplayableRequestError

ConfigInput = Union[HttpClientConfig, Mapping[str, Any]]
SessionFactory = Callable[[], requests.Session]


def _config_from_value(config: ConfigInput) -> HttpClientConfig:
    if isinstance(config, HttpClientConfig):
        return config
    if isinstance(config, Mapping):
        return HttpClientConfig.from_dict(config)
    raise TypeError("config must be an HttpClientConfig or a mapping")


def _configured_retry(config: HttpClientConfig) -> Retry:
    retry = config.retry
    if not isinstance(retry, Retry):  # Defensive: __post_init__ normalizes it.
        raise TypeError("config.retry must be an urllib3 Retry")
    return retry


def create_session(
    config: ConfigInput,
    *,
    session_factory: SessionFactory = requests.Session,
) -> requests.Session:
    """Create one reusable Requests Session with organization defaults."""

    resolved = _config_from_value(config)
    session = session_factory()
    if not isinstance(session, requests.Session):
        raise TypeError("session_factory must return a requests.Session")

    session.headers.update(resolved.headers)
    session.verify = resolved.verify
    session.trust_env = resolved.trust_env
    session.max_redirects = resolved.max_redirects
    session.mount("http://", build_adapter(_configured_retry(resolved), resolved.pool))
    session.mount("https://", build_adapter(_configured_retry(resolved), resolved.pool))
    return session


def _resolve_url(config: HttpClientConfig, url: str) -> str:
    if not isinstance(url, str):
        raise TypeError("url must be a string")

    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise requests.exceptions.InvalidURL("url must be a valid URL") from error

    if not config.base_url:
        return url
    if parsed.scheme or parsed.netloc:
        raise requests.exceptions.InvalidURL("url must be relative when HttpClientConfig.base_url is configured")

    # Stripping the leading slash makes base paths stable: base_url=/api and
    # url=/users resolve to /api/users rather than escaping to /users.
    return urljoin(f"{config.base_url}/", url.lstrip("/"))


def _safe_url(url: str) -> str:
    """Remove credentials, query values, and fragments from error metadata."""

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "<invalid-url>"

    if hostname is None:
        safe_netloc = ""
    else:
        safe_host = f"[{hostname}]" if ":" in hostname else hostname
        safe_netloc = f"{safe_host}:{port}" if port is not None else safe_host
    return urlunsplit(
        (
            parsed.scheme,
            safe_netloc,
            parsed.path,
            "",
            "",
        )
    )


def _request_details(
    fallback_method: str,
    fallback_url: str,
    *,
    response: Optional[requests.Response] = None,
    error: Optional[requests.RequestException] = None,
) -> tuple[str, str]:
    prepared: Optional[requests.PreparedRequest] = None
    if response is not None:
        prepared = response.request
    elif error is not None:
        prepared = error.request
        if prepared is None and error.response is not None:
            prepared = error.response.request

    if prepared is not None:
        return prepared.method or fallback_method, _safe_url(prepared.url or fallback_url)
    return fallback_method.upper(), _safe_url(fallback_url)


def _retry_limit(retry: Retry, category: str) -> int:
    total = retry.total
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        return 0

    category_limit = getattr(retry, category)
    if category_limit is None:
        return total
    if isinstance(category_limit, bool) or not isinstance(category_limit, int):
        return 0
    return max(0, min(total, category_limit))


def _method_is_retryable(retry: Retry, method: str) -> bool:
    methods = retry.allowed_methods
    return bool(methods) and method.upper() in methods


def _retry_may_resend_body(retry: Retry, method: str) -> bool:
    if _retry_limit(retry, "connect") > 0 or _retry_limit(retry, "other") > 0:
        return True
    if not _method_is_retryable(retry, method):
        return False
    return _retry_limit(retry, "read") > 0 or _retry_limit(retry, "status") > 0


def _is_replayable_file(value: Any) -> bool:
    tell = getattr(value, "tell", None)
    seek = getattr(value, "seek", None)
    if not callable(tell) or not callable(seek):
        return False
    try:
        position = tell()
        seek(position)
    except (OSError, ValueError):
        return False
    return True


def _non_replayable_body_reason(kwargs: Mapping[str, Any]) -> Optional[str]:
    data = kwargs.get("data")
    if data is None or isinstance(
        data,
        (str, bytes, bytearray, memoryview, Mapping, list, tuple),
    ):
        return None
    if hasattr(data, "read"):
        if _is_replayable_file(data):
            return None
        return "data is a stream that cannot be rewound"
    if isinstance(data, Iterator):
        return "data is a one-shot iterator"
    try:
        if iter(data) is data:
            return "data is a one-shot iterable"
    except TypeError:
        return None
    return None


def _reject_non_replayable_body(
    retry: Retry,
    method: str,
    url: str,
    kwargs: Mapping[str, Any],
    *,
    redirects_enabled: bool,
) -> None:
    if not redirects_enabled and not _retry_may_resend_body(retry, method):
        return
    reason = _non_replayable_body_reason(kwargs)
    if reason is None:
        return

    safe_url = _safe_url(url)
    raise NonReplayableRequestError(
        f"{method} {safe_url} cannot be retried safely: {reason}",
        method=method,
        url=safe_url,
        attempts=0,
    )


def _contains_max_retry_error(error: BaseException) -> bool:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, MaxRetryError):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        pending.extend(value for value in current.args if isinstance(value, BaseException))
    return False


def _status_retry_exhausted(
    retry: Retry,
    method: str,
    response: requests.Response,
    state: AttemptState,
) -> bool:
    if state.retries == 0 or _retry_limit(retry, "status") == 0:
        return False
    has_retry_after = "Retry-After" in response.headers
    return retry.is_retry(
        method,
        response.status_code,
        has_retry_after=has_retry_after,
    )


def _raise_failure(
    error_type: type[BaseHttpError],
    *,
    fallback_method: str,
    fallback_url: str,
    attempts: int,
    retry_exhausted: bool,
    rule: Optional[ErrorMappingRule],
    response: Optional[requests.Response] = None,
    cause: Optional[requests.RequestException] = None,
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
        reason = f"HTTP {status_code}"
    elif cause is not None:
        reason = type(cause).__name__
    else:
        reason = "unknown HTTP failure"

    suffix = (
        "before sending"
        if attempts == 0
        else "after {} attempt{}".format(
            attempts,
            "" if attempts == 1 else "s",
        )
    )
    message = f"{method} {url} failed with {reason} {suffix}"
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
    """Reusable synchronous Requests client with standardized policy."""

    def __init__(
        self,
        config: ConfigInput,
        *,
        session_factory: SessionFactory = requests.Session,
    ) -> None:
        self.config = _config_from_value(config)
        self.retry = _configured_retry(self.config)
        self._session = create_session(
            self.config,
            session_factory=session_factory,
        )
        self._closed = False

    @property
    def raw_session(self) -> requests.Session:
        """Underlying Session; direct calls bypass timeout and error mapping."""

        return self._session

    @property
    def raw_client(self) -> requests.Session:
        """Compatibility alias for ``raw_session``."""

        return self._session

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        if self._closed:
            raise RuntimeError("HttpClient is closed")
        if not isinstance(method, str) or not method.strip():
            raise TypeError("method must be a non-empty string")

        normalized_method = method.strip().upper()
        resolved_url = url

        timeout = self.config.timeout
        if not isinstance(timeout, TimeoutConfig):  # Defensive normalization.
            raise TypeError("config.timeout must be a TimeoutConfig")
        kwargs.setdefault("timeout", timeout.as_requests_value())
        kwargs.setdefault("allow_redirects", self.config.follow_redirects)

        with track_attempts() as state:
            try:
                resolved_url = _resolve_url(self.config, url)
                _reject_non_replayable_body(
                    self.retry,
                    normalized_method,
                    resolved_url,
                    kwargs,
                    redirects_enabled=bool(kwargs["allow_redirects"]),
                )
                response = self._session.request(
                    normalized_method,
                    resolved_url,
                    **kwargs,
                )
                if state.attempts == 0:
                    # A custom Session may return a synthetic/cached Response
                    # without entering the mounted adapter pipeline.
                    state.attempts = 1
            except requests.RequestException as error:
                if not self.config.enable_error_mapping:
                    raise
                rule = self.config.error_mapping.for_exception(error)
                error_type = rule.raise_as if rule is not None else self.config.error_mapping.default_system_error
                _raise_failure(
                    error_type,
                    fallback_method=normalized_method,
                    fallback_url=resolved_url,
                    attempts=state.attempts,
                    retry_exhausted=(state.retries > 0 and _contains_max_retry_error(error)),
                    rule=rule,
                    cause=error,
                )

            if not response.ok and self.config.enable_error_mapping:
                rule = self.config.error_mapping.for_status(response.status_code)
                if rule is not None:
                    error_type = rule.raise_as
                elif 400 <= response.status_code < 500:
                    error_type = self.config.error_mapping.default_business_error
                else:
                    error_type = self.config.error_mapping.default_system_error

                _raise_failure(
                    error_type,
                    fallback_method=normalized_method,
                    fallback_url=resolved_url,
                    attempts=state.attempts,
                    retry_exhausted=_status_retry_exhausted(
                        self.retry,
                        normalized_method,
                        response,
                        state,
                    ),
                    rule=rule,
                    response=response,
                )
            return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        if not self._closed:
            self._session.close()
            self._closed = True

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
