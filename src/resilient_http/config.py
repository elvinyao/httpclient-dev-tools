"""Strict configuration for the organization-level Requests client."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union
from urllib.parse import urlsplit

import requests
from urllib3.util import Retry

from .exceptions import BaseHttpError, BusinessHttpError, SystemHttpError

RequestsErrorType = type[requests.RequestException]
RaisedErrorType = type[BaseHttpError]

DEFAULT_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_REQUESTS_ERROR_TYPES: dict[str, RequestsErrorType] = {
    name: getattr(requests.exceptions, name)
    for name in (
        "RequestException",
        "ConnectionError",
        "HTTPError",
        "URLRequired",
        "TooManyRedirects",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "InvalidURL",
        "InvalidHeader",
        "InvalidSchema",
        "MissingSchema",
        "ProxyError",
        "SSLError",
        "ChunkedEncodingError",
        "ContentDecodingError",
        "StreamConsumedError",
        "RetryError",
        "UnrewindableBodyError",
    )
    if hasattr(requests.exceptions, name)
}

_RAISED_ERROR_TYPES: dict[str, RaisedErrorType] = {
    "business": BusinessHttpError,
    "businesshttperror": BusinessHttpError,
    "system": SystemHttpError,
    "systemhttperror": SystemHttpError,
}


def _unknown_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown {where} configuration key(s): {names}")


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _strict_integer(
    value: Any,
    name: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _optional_retry_count(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    return _strict_integer(value, name, minimum=0)


def _finite_number(
    value: Any,
    name: str,
    *,
    minimum: Optional[float] = None,
    exclusive_minimum: bool = False,
    maximum: Optional[float] = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")

    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None:
        if exclusive_minimum and number <= minimum:
            raise ValueError(f"{name} must be greater than {minimum}")
        if not exclusive_minimum and number < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return number


def _iterable_values(value: Any, name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Iterable):
        raise TypeError(f"{name} must be an iterable, not a string or mapping")
    return tuple(value)


def _ordered_values(value: Any, name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")
    return tuple(value)


def _resolve_requests_error_type(
    value: Union[str, RequestsErrorType],
) -> RequestsErrorType:
    if isinstance(value, str):
        try:
            return _REQUESTS_ERROR_TYPES[value]
        except KeyError as error:
            supported = ", ".join(sorted(_REQUESTS_ERROR_TYPES))
            raise ValueError(f"Unknown Requests exception {value!r}; supported names: {supported}") from error

    if not isinstance(value, type) or not issubclass(value, requests.RequestException):
        raise TypeError("Exception types must be names or subclasses of requests.RequestException")
    return value


def _resolve_raised_error_type(
    value: Union[str, RaisedErrorType],
    name: str = "raise_as",
) -> RaisedErrorType:
    if isinstance(value, str):
        try:
            return _RAISED_ERROR_TYPES[value.replace("_", "").lower()]
        except KeyError as error:
            raise ValueError(f"{name} must be 'business', 'system', or a BaseHttpError subclass") from error

    if not isinstance(value, type) or not issubclass(value, BaseHttpError):
        raise TypeError(f"{name} must be a subclass of BaseHttpError")
    return value


@dataclass(frozen=True)
class TimeoutConfig:
    """Per-attempt Requests connect and read timeouts in seconds."""

    connect: float = 10.0
    read: float = 10.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "connect",
            _finite_number(
                self.connect,
                "timeout.connect",
                minimum=0,
                exclusive_minimum=True,
            ),
        )
        object.__setattr__(
            self,
            "read",
            _finite_number(
                self.read,
                "timeout.read",
                minimum=0,
                exclusive_minimum=True,
            ),
        )

    def as_requests_value(self) -> tuple[float, float]:
        return (self.connect, self.read)

    @classmethod
    def from_value(cls, value: Any) -> TimeoutConfig:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            _unknown_keys(value, {"default", "connect", "read"}, "timeout")
            default = _finite_number(
                value.get("default", 10.0),
                "timeout.default",
                minimum=0,
                exclusive_minimum=True,
            )
            return cls(
                connect=value.get("connect", default),
                read=value.get("read", default),
            )
        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                raise ValueError("timeout sequence must contain connect and read values")
            return cls(connect=value[0], read=value[1])

        timeout = _finite_number(
            value,
            "timeout",
            minimum=0,
            exclusive_minimum=True,
        )
        return cls(connect=timeout, read=timeout)


@dataclass(frozen=True)
class PoolConfig:
    """Requests HTTPAdapter connection-pool settings."""

    connections: int = 10
    maxsize: int = 10
    block: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "connections",
            _strict_integer(self.connections, "pool.connections", minimum=1),
        )
        object.__setattr__(
            self,
            "maxsize",
            _strict_integer(self.maxsize, "pool.maxsize", minimum=1),
        )
        object.__setattr__(self, "block", _strict_bool(self.block, "pool.block"))

    @classmethod
    def from_value(cls, value: Any) -> PoolConfig:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pool must be a PoolConfig or mapping")
        _unknown_keys(value, {"connections", "maxsize", "block"}, "pool")
        return cls(
            connections=value.get("connections", 10),
            maxsize=value.get("maxsize", 10),
            block=value.get("block", False),
        )


@dataclass(frozen=True)
class RetryConfig:
    """Organization defaults used to construct ``urllib3.util.Retry``."""

    total: int = 0
    connect: Optional[int] = None
    read: Optional[int] = None
    status: Optional[int] = None
    other: int = 0
    allowed_methods: frozenset[str] = field(default_factory=lambda: DEFAULT_RETRY_METHODS)
    status_forcelist: frozenset[int] = field(default_factory=lambda: DEFAULT_RETRY_STATUSES)
    backoff_factor: float = 0.5
    backoff_max: float = 30.0
    backoff_jitter: float = 0.5
    respect_retry_after_header: bool = True
    retry_after_max: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total",
            _strict_integer(self.total, "retry.total", minimum=0),
        )
        for name in ("connect", "read", "status"):
            object.__setattr__(
                self,
                name,
                _optional_retry_count(getattr(self, name), f"retry.{name}"),
            )
        object.__setattr__(
            self,
            "other",
            _strict_integer(self.other, "retry.other", minimum=0),
        )

        raw_methods = _iterable_values(self.allowed_methods, "retry.allowed_methods")
        if not raw_methods:
            raise ValueError("retry.allowed_methods cannot be empty; use total=0 to disable retries")
        methods = []
        for index, method in enumerate(raw_methods):
            if not isinstance(method, str) or not method.strip():
                raise TypeError(f"retry.allowed_methods[{index}] must be a non-empty string")
            methods.append(method.strip().upper())
        object.__setattr__(self, "allowed_methods", frozenset(methods))

        raw_statuses = _iterable_values(
            self.status_forcelist,
            "retry.status_forcelist",
        )
        statuses = frozenset(
            _strict_integer(
                code,
                f"retry.status_forcelist[{index}]",
                minimum=400,
                maximum=599,
            )
            for index, code in enumerate(raw_statuses)
        )
        object.__setattr__(self, "status_forcelist", statuses)

        object.__setattr__(
            self,
            "backoff_factor",
            _finite_number(
                self.backoff_factor,
                "retry.backoff_factor",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "backoff_max",
            _finite_number(
                self.backoff_max,
                "retry.backoff_max",
                minimum=0,
                exclusive_minimum=True,
            ),
        )
        object.__setattr__(
            self,
            "backoff_jitter",
            _finite_number(
                self.backoff_jitter,
                "retry.backoff_jitter",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "respect_retry_after_header",
            _strict_bool(
                self.respect_retry_after_header,
                "retry.respect_retry_after_header",
            ),
        )
        object.__setattr__(
            self,
            "retry_after_max",
            _strict_integer(
                self.retry_after_max,
                "retry.retry_after_max",
                minimum=0,
            ),
        )

    def build(self) -> Retry:
        return Retry(
            total=self.total,
            connect=self.connect,
            read=self.read,
            redirect=0,
            status=self.status,
            other=self.other,
            allowed_methods=self.allowed_methods,
            status_forcelist=self.status_forcelist,
            backoff_factor=self.backoff_factor,
            backoff_max=self.backoff_max,
            raise_on_redirect=False,
            raise_on_status=False,
            respect_retry_after_header=self.respect_retry_after_header,
            backoff_jitter=self.backoff_jitter,
            retry_after_max=self.retry_after_max,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RetryConfig:
        if not isinstance(data, Mapping):
            raise TypeError("retry must be a mapping")
        _unknown_keys(
            data,
            {
                "total",
                "connect",
                "read",
                "status",
                "other",
                "allowed_methods",
                "status_forcelist",
                "backoff_factor",
                "backoff_max",
                "backoff_jitter",
                "respect_retry_after_header",
                "retry_after_max",
            },
            "retry",
        )
        return cls(
            total=data.get("total", 0),
            connect=data.get("connect"),
            read=data.get("read"),
            status=data.get("status"),
            other=data.get("other", 0),
            allowed_methods=data.get("allowed_methods", DEFAULT_RETRY_METHODS),
            status_forcelist=data.get("status_forcelist", DEFAULT_RETRY_STATUSES),
            backoff_factor=data.get("backoff_factor", 0.5),
            backoff_max=data.get("backoff_max", 30.0),
            backoff_jitter=data.get("backoff_jitter", 0.5),
            respect_retry_after_header=data.get(
                "respect_retry_after_header",
                True,
            ),
            retry_after_max=data.get("retry_after_max", 60),
        )


RetryInput = Union[RetryConfig, Retry, Mapping[str, Any]]


def _validate_retry_instance(retry: Retry) -> Retry:
    if type(retry) is not Retry:
        raise TypeError(
            "custom urllib3 Retry subclasses are not supported; use RetryConfig or an unmodified urllib3.util.Retry"
        )
    _strict_integer(retry.total, "retry.total", minimum=0)
    for name in ("connect", "read", "status", "other"):
        _optional_retry_count(getattr(retry, name), f"retry.{name}")

    methods = retry.allowed_methods
    if methods is None or not methods:
        raise ValueError(
            "retry.allowed_methods must be a non-empty collection; use RetryConfig for organization-safe defaults"
        )
    for index, method in enumerate(methods):
        if not isinstance(method, str) or not method.strip():
            raise TypeError(f"retry.allowed_methods[{index}] must be a non-empty string")

    for index, code in enumerate(retry.status_forcelist or ()):
        _strict_integer(
            code,
            f"retry.status_forcelist[{index}]",
            minimum=400,
            maximum=599,
        )

    _finite_number(retry.backoff_factor, "retry.backoff_factor", minimum=0)
    _finite_number(
        retry.backoff_max,
        "retry.backoff_max",
        minimum=0,
        exclusive_minimum=True,
    )
    _finite_number(retry.backoff_jitter, "retry.backoff_jitter", minimum=0)
    _strict_bool(
        retry.respect_retry_after_header,
        "retry.respect_retry_after_header",
    )
    _strict_integer(
        retry.retry_after_max,
        "retry.retry_after_max",
        minimum=0,
    )

    # The policy layer must receive the final response so it can map 4xx/5xx.
    # Requests, rather than urllib3, owns redirects.
    return retry.new(
        redirect=0,
        raise_on_redirect=False,
        raise_on_status=False,
    )


def retry_from_dict(data: Mapping[str, Any]) -> Retry:
    """Build an organization-safe ``urllib3.util.Retry`` policy."""

    return RetryConfig.from_dict(data).build()


def _retry_from_value(value: RetryInput) -> Retry:
    if isinstance(value, RetryConfig):
        return value.build()
    if isinstance(value, Retry):
        return _validate_retry_instance(value)
    if isinstance(value, Mapping):
        return retry_from_dict(value)
    raise TypeError("retry must be a RetryConfig, urllib3 Retry, or mapping")


@dataclass(frozen=True)
class ErrorMappingRule:
    """Map one terminal HTTP condition to an application-facing exception."""

    name: str
    raise_as: RaisedErrorType
    status_codes: frozenset[int] = field(default_factory=frozenset)
    exception_types: tuple[RequestsErrorType, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Error-mapping rule name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())

        raw_statuses = _iterable_values(self.status_codes, "status_codes")
        status_codes = frozenset(
            _strict_integer(
                code,
                f"status_codes[{index}]",
                minimum=400,
                maximum=599,
            )
            for index, code in enumerate(raw_statuses)
        )
        object.__setattr__(self, "status_codes", status_codes)

        raw_exceptions = _iterable_values(
            self.exception_types,
            "exception_types",
        )
        exception_types = tuple(_resolve_requests_error_type(value) for value in raw_exceptions)
        object.__setattr__(self, "exception_types", exception_types)

        if not status_codes and not exception_types:
            raise ValueError("An error-mapping rule must define status_codes and/or exception_types")
        object.__setattr__(
            self,
            "raise_as",
            _resolve_raised_error_type(self.raise_as),
        )

    def matches_status(self, status_code: int) -> bool:
        return status_code in self.status_codes

    def matches_exception(self, error: requests.RequestException) -> bool:
        return bool(self.exception_types) and isinstance(
            error,
            self.exception_types,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ErrorMappingRule:
        if not isinstance(data, Mapping):
            raise TypeError("An error-mapping rule must be a mapping")
        _unknown_keys(
            data,
            {
                "name",
                "status_codes",
                "exceptions",
                "exception_types",
                "raise_as",
            },
            "error-mapping rule",
        )
        missing = [key for key in ("name", "raise_as") if key not in data]
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"An error-mapping rule must define required key(s): {names}")
        if "exceptions" in data and "exception_types" in data:
            raise ValueError("Use only one of exceptions or exception_types")

        status_values = _iterable_values(
            data.get("status_codes", ()),
            "error-mapping status_codes",
        )
        raw_errors = data.get(
            "exception_types",
            data.get("exceptions", ()),
        )
        error_values = _iterable_values(
            raw_errors,
            "error-mapping exceptions",
        )

        return cls(
            name=data["name"],
            raise_as=_resolve_raised_error_type(data["raise_as"]),
            status_codes=frozenset(status_values),
            exception_types=tuple(_resolve_requests_error_type(value) for value in error_values),
        )


@dataclass(frozen=True)
class ErrorMappingPolicy:
    """Ordered terminal-error mappings; the first matching rule wins."""

    rules: tuple[ErrorMappingRule, ...] = field(default_factory=tuple)
    default_business_error: RaisedErrorType = BusinessHttpError
    default_system_error: RaisedErrorType = SystemHttpError

    def __post_init__(self) -> None:
        rules = _ordered_values(self.rules, "error_mapping.rules")
        for index, rule in enumerate(rules):
            if not isinstance(rule, ErrorMappingRule):
                raise TypeError(f"error_mapping.rules[{index}] must be an ErrorMappingRule")
        object.__setattr__(self, "rules", rules)

        business_error = _resolve_raised_error_type(
            self.default_business_error,
            "default_business_error",
        )
        if not issubclass(business_error, BusinessHttpError):
            raise TypeError("default_business_error must inherit BusinessHttpError")
        object.__setattr__(self, "default_business_error", business_error)

        system_error = _resolve_raised_error_type(
            self.default_system_error,
            "default_system_error",
        )
        if not issubclass(system_error, SystemHttpError):
            raise TypeError("default_system_error must inherit SystemHttpError")
        object.__setattr__(self, "default_system_error", system_error)

    def for_status(self, status_code: int) -> Optional[ErrorMappingRule]:
        return next(
            (rule for rule in self.rules if rule.matches_status(status_code)),
            None,
        )

    def for_exception(
        self,
        error: requests.RequestException,
    ) -> Optional[ErrorMappingRule]:
        return next(
            (rule for rule in self.rules if rule.matches_exception(error)),
            None,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ErrorMappingPolicy:
        if not isinstance(data, Mapping):
            raise TypeError("error_mapping must be a mapping")
        _unknown_keys(
            data,
            {
                "rules",
                "default_business_error",
                "default_system_error",
            },
            "error mapping",
        )

        raw_rules = _ordered_values(
            data.get("rules", ()),
            "error_mapping.rules",
        )
        rules = []
        for index, rule in enumerate(raw_rules):
            if isinstance(rule, ErrorMappingRule):
                rules.append(rule)
            elif isinstance(rule, Mapping):
                rules.append(ErrorMappingRule.from_dict(rule))
            else:
                raise TypeError(f"error_mapping.rules[{index}] must be a mapping or ErrorMappingRule")

        return cls(
            rules=tuple(rules),
            default_business_error=_resolve_raised_error_type(
                data.get("default_business_error", "business"),
                "default_business_error",
            ),
            default_system_error=_resolve_raised_error_type(
                data.get("default_system_error", "system"),
                "default_system_error",
            ),
        )


def _base_url_from_value(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("base_url must be a string")
    if not value:
        return ""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("base_url must be a valid absolute HTTP(S) URL") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base_url port must be between 1 and 65535")
    return value.rstrip("/")


def _headers_from_value(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("headers must be a mapping")

    headers = {}
    for key, header_value in value.items():
        if not isinstance(key, str) or not isinstance(header_value, str):
            raise TypeError("headers keys and values must be strings")
        headers[key] = header_value
    return headers


def _verify_from_value(value: Any) -> Union[bool, str]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value:
        return value
    raise TypeError("verify must be a boolean or a non-empty CA bundle path")


@dataclass(frozen=True)
class HttpClientConfig:
    """Top-level organization HTTP-client configuration."""

    base_url: str = ""
    timeout: Union[
        TimeoutConfig,
        float,
        tuple[float, float],
        Mapping[str, Any],
    ] = field(default_factory=TimeoutConfig)
    headers: Mapping[str, str] = field(default_factory=dict)
    follow_redirects: bool = False
    max_redirects: int = 10
    verify: Union[bool, str] = True
    trust_env: bool = True
    pool: Union[PoolConfig, Mapping[str, Any]] = field(default_factory=PoolConfig)
    retry: RetryInput = field(default_factory=RetryConfig)
    enable_error_mapping: bool = True
    error_mapping: Union[ErrorMappingPolicy, Mapping[str, Any]] = field(default_factory=ErrorMappingPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "base_url",
            _base_url_from_value(self.base_url),
        )
        object.__setattr__(
            self,
            "timeout",
            TimeoutConfig.from_value(self.timeout),
        )
        object.__setattr__(
            self,
            "headers",
            _headers_from_value(self.headers),
        )
        object.__setattr__(
            self,
            "follow_redirects",
            _strict_bool(self.follow_redirects, "follow_redirects"),
        )
        object.__setattr__(
            self,
            "max_redirects",
            _strict_integer(self.max_redirects, "max_redirects", minimum=0),
        )
        object.__setattr__(self, "verify", _verify_from_value(self.verify))
        object.__setattr__(
            self,
            "trust_env",
            _strict_bool(self.trust_env, "trust_env"),
        )
        object.__setattr__(self, "pool", PoolConfig.from_value(self.pool))
        object.__setattr__(self, "retry", _retry_from_value(self.retry))
        object.__setattr__(
            self,
            "enable_error_mapping",
            _strict_bool(
                self.enable_error_mapping,
                "enable_error_mapping",
            ),
        )

        if isinstance(self.error_mapping, ErrorMappingPolicy):
            mapping = self.error_mapping
        elif isinstance(self.error_mapping, Mapping):
            mapping = ErrorMappingPolicy.from_dict(self.error_mapping)
        else:
            raise TypeError("error_mapping must be an ErrorMappingPolicy or mapping")
        object.__setattr__(self, "error_mapping", mapping)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HttpClientConfig:
        if not isinstance(data, Mapping):
            raise TypeError("HTTP client configuration must be a mapping")
        if "retry_policy" in data:
            raise ValueError(
                "retry_policy was removed; configure urllib3 Retry under retry "
                "and terminal exception mapping under error_mapping"
            )

        _unknown_keys(
            data,
            {
                "base_url",
                "timeout",
                "headers",
                "follow_redirects",
                "max_redirects",
                "verify",
                "trust_env",
                "pool",
                "retry",
                "enable_error_mapping",
                "error_mapping",
            },
            "HTTP client",
        )

        return cls(
            base_url=data.get("base_url", ""),
            timeout=data.get("timeout", 10.0),
            headers=data.get("headers", {}),
            follow_redirects=data.get("follow_redirects", False),
            max_redirects=data.get("max_redirects", 10),
            verify=data.get("verify", True),
            trust_env=data.get("trust_env", True),
            pool=data.get("pool", {}),
            retry=data.get("retry", {}),
            enable_error_mapping=data.get("enable_error_mapping", True),
            error_mapping=data.get("error_mapping", {}),
        )
