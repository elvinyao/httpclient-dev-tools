"""HTTPX retry/client factory tests."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Union

import httpx
import pytest
from httpx_retries import Retry, RetryTransport

import resilient_http.httpx as httpx_client
from resilient_http.httpx import create_async_client, create_client, create_retry

# ruff: noqa: RUF002


@dataclass(frozen=True)
class ResponseSpec:
    """One response returned by the local sequence server."""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class SequenceServer:
    """Small local HTTP server that returns responses in a fixed order."""

    def __init__(self, responses: Sequence[ResponseSpec]) -> None:
        if not responses:
            raise ValueError("responses must not be empty")

        self._responses = deque(responses)
        self._last_response = responses[-1]
        self._lock = threading.Lock()
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self._respond()

            def do_HEAD(self) -> None:
                self._respond(include_body=False)

            def do_OPTIONS(self) -> None:
                self._respond()

            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)
                self._respond()

            def _respond(self, *, include_body: bool = True) -> None:
                with owner._lock:
                    owner.requests += 1
                    response = owner._responses.popleft() if owner._responses else owner._last_response

                body = response.body if include_body else b""
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address
        self.url = f"http://{host}:{port}/resource"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> SequenceServer:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class TimeoutRecordingTransport(httpx.BaseTransport):
    """Record effective HTTPX timeout extensions without network access."""

    def __init__(self) -> None:
        self.timeouts: list[dict[str, Optional[float]]] = []
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.timeouts.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, content=b"ok")

    def close(self) -> None:
        self.closed = True


class AsyncTimeoutRecordingTransport(httpx.AsyncBaseTransport):
    """Asynchronous effective-timeout recorder."""

    def __init__(self) -> None:
        self.timeouts: list[dict[str, Optional[float]]] = []
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.timeouts.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, content=b"ok")

    async def aclose(self) -> None:
        self.closed = True


class ScriptedTransport(httpx.BaseTransport):
    """Run a deterministic sequence of response/exception callbacks."""

    def __init__(self, actions: Sequence[Callable[[httpx.Request], httpx.Response]]) -> None:
        self.actions = deque(actions)
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.actions.popleft()(request)


class BodyFailureStream(httpx.SyncByteStream):
    """Fail after response headers have already left the transport layer."""

    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self) -> Any:
        yield b"partial"
        raise httpx.ReadTimeout("body read timed out", request=self.request)


def success(request: httpx.Request) -> httpx.Response:
    """Return a successful synthetic response."""

    return httpx.Response(200, content=b"ok")


def connect_error(request: httpx.Request) -> httpx.Response:
    """Raise a native HTTPX connect exception."""

    raise httpx.ConnectError("synthetic connect failure", request=request)


def test_httpx_module_has_small_backend_specific_public_api() -> None:
    """场景：导入 HTTPX 子模块；预期：只公开 Retry 与三个 factory。"""

    assert httpx_client.__all__ == ["Retry", "create_async_client", "create_client", "create_retry"]
    assert httpx_client.Retry is Retry


def test_create_retry_has_conservative_requests_aligned_defaults() -> None:
    """场景：使用 helper 默认值；预期：安全方法、常见瞬时状态和保守异常均被配置。"""

    retry = create_retry()

    assert type(retry) is Retry
    assert retry.total == 3
    assert retry.attempts_made == 0
    assert retry.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})
    assert retry.status_forcelist == frozenset({429, 500, 502, 503, 504})
    assert retry.retryable_exceptions == (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    )
    assert retry.backoff_factor == 0.5
    assert retry.backoff_jitter == 0.0
    assert retry.respect_retry_after_header is True


def test_create_retry_accepts_supported_policy_overrides() -> None:
    """场景：覆盖 helper 的常用选项；预期：返回原生 HTTPX Retry 并完整保留配置。"""

    retry = create_retry(
        total=1,
        allowed_methods=frozenset({"POST"}),
        status_forcelist=frozenset({418}),
        retry_on_exceptions=(httpx.ConnectTimeout,),
        backoff_factor=0.25,
    )

    assert retry.total == 1
    assert retry.allowed_methods == frozenset({"POST"})
    assert retry.status_forcelist == frozenset({418})
    assert retry.retryable_exceptions == (httpx.ConnectTimeout,)
    assert retry.backoff_factor == 0.25


@pytest.mark.parametrize("status_forcelist", [None, (), frozenset()])
def test_empty_status_policy_does_not_restore_httpx_retries_defaults(
    status_forcelist: Optional[Sequence[int]],
) -> None:
    """场景：APP 禁用状态重试；预期：绕过 httpx-retries 将空集合替换为默认值的行为。"""

    retry = create_retry(status_forcelist=status_forcelist)

    assert retry.status_forcelist == frozenset({-1})
    assert retry.increment().status_forcelist == frozenset({-1})


@pytest.mark.parametrize("retry_on_exceptions", [None, (), frozenset()])
def test_empty_exception_policy_does_not_restore_httpx_retries_defaults(
    retry_on_exceptions: Optional[Sequence[type[Exception]]],
) -> None:
    """场景：APP 显式禁用异常重试；预期：None 和空集合都不会恢复上游默认异常。"""

    retry = create_retry(retry_on_exceptions=retry_on_exceptions)

    assert retry.retryable_exceptions == ()
    assert retry.increment().retryable_exceptions == ()


@pytest.mark.parametrize("retry_on_exceptions", [None, ()])
def test_disabled_exception_policy_does_not_retry_transport_error(
    retry_on_exceptions: Optional[Sequence[type[Exception]]],
) -> None:
    """场景：底层抛默认可重试 ConnectError；预期：显式禁用后只调用 transport 一次。"""

    transport = ScriptedTransport([connect_error, success])
    retry = create_retry(
        total=3,
        retry_on_exceptions=retry_on_exceptions,
        backoff_factor=0,
    )

    with create_client(retry, transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get("http://connect.test/resource")

    assert transport.calls == 1


def test_empty_allowed_methods_is_rejected_instead_of_silently_using_upstream_defaults() -> None:
    """场景：传入空 allowed_methods；预期：明确报错而不是意外扩大重试方法。"""

    with pytest.raises(ValueError, match="allowed_methods must not be empty"):
        create_retry(allowed_methods=())


def test_each_create_retry_call_returns_independent_policy() -> None:
    """场景：连续调用 helper；预期：各请求可从独立的零 attempt 策略开始。"""

    first = create_retry()
    second = create_retry()

    assert first is not second
    assert first.attempts_made == second.attempts_made == 0
    assert first.increment().attempts_made == 1
    assert second.attempts_made == 0


@pytest.mark.parametrize("factory", [create_client, create_async_client])
@pytest.mark.parametrize("invalid_retry", [None, object(), 3])
def test_client_factories_reject_non_retry_values(factory: Callable[..., object], invalid_retry: object) -> None:
    """场景：factory 收到错误策略类型；预期：在创建连接池前抛出清楚的 TypeError。"""

    with pytest.raises(TypeError, match=r"httpx_retries\.Retry"):
        factory(invalid_retry)  # type: ignore[arg-type]


def test_sync_factory_creates_independent_transport_stacks_and_pools() -> None:
    """场景：创建两个同步 Client；预期：RetryTransport、底层 transport 和连接池完全独立。"""

    first = create_client(create_retry(total=0))
    second = create_client(create_retry(total=0))
    try:
        assert first is not second
        assert isinstance(first._transport, RetryTransport)
        assert isinstance(second._transport, RetryTransport)
        assert first._transport is not second._transport
        assert first._transport._sync_transport is not second._transport._sync_transport
        assert first._transport._async_transport is None
        assert second._transport._async_transport is None
    finally:
        first.close()
        second.close()


def test_async_factory_creates_independent_transport_stacks_and_pools() -> None:
    """场景：创建两个异步 Client；预期：RetryTransport、底层 transport 和连接池完全独立。"""

    async def scenario() -> None:
        first = create_async_client(create_retry(total=0))
        second = create_async_client(create_retry(total=0))
        try:
            assert first is not second
            assert isinstance(first._transport, RetryTransport)
            assert isinstance(second._transport, RetryTransport)
            assert first._transport is not second._transport
            assert first._transport._async_transport is not second._transport._async_transport
            assert first._transport._sync_transport is None
            assert second._transport._sync_transport is None
        finally:
            await first.aclose()
            await second.aclose()

    asyncio.run(scenario())


def test_sync_factory_wraps_injected_transport_and_client_owns_close() -> None:
    """场景：注入自定义同步底层 transport；预期：只包装一次并随 Client 一起关闭。"""

    transport = TimeoutRecordingTransport()

    with create_client(create_retry(total=0), transport=transport) as client:
        assert isinstance(client._transport, RetryTransport)
        assert client._transport._sync_transport is transport
        assert client._transport._async_transport is None
        response = client.get("http://transport.test/resource")
        assert response.status_code == 200

    assert transport.closed is True


def test_async_factory_wraps_injected_transport_and_client_owns_close() -> None:
    """场景：注入自定义异步底层 transport；预期：只包装一次并随 AsyncClient 一起关闭。"""

    async def scenario() -> AsyncTimeoutRecordingTransport:
        transport = AsyncTimeoutRecordingTransport()
        async with create_async_client(create_retry(total=0), transport=transport) as client:
            assert isinstance(client._transport, RetryTransport)
            assert client._transport._async_transport is transport
            assert client._transport._sync_transport is None
            response = await client.get("http://transport.test/resource")
            assert response.status_code == 200
        return transport

    assert asyncio.run(scenario()).closed is True


@pytest.mark.parametrize("invalid_transport", [AsyncTimeoutRecordingTransport(), object()])
def test_sync_factory_rejects_async_or_unknown_transport(invalid_transport: object) -> None:
    """场景：同步 factory 收到异步或未知 transport；预期：在包装前给出同步类型错误。"""

    with pytest.raises(TypeError, match=r"httpx\.BaseTransport.*create_client"):
        create_client(create_retry(total=0), transport=invalid_transport)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid_transport", [TimeoutRecordingTransport(), object()])
def test_async_factory_rejects_sync_or_unknown_transport(invalid_transport: object) -> None:
    """场景：异步 factory 收到同步或未知 transport；预期：在包装前给出异步类型错误。"""

    with pytest.raises(TypeError, match=r"httpx\.AsyncBaseTransport.*create_async_client"):
        create_async_client(create_retry(total=0), transport=invalid_transport)  # type: ignore[arg-type]


def test_factories_reject_an_already_wrapped_retry_transport() -> None:
    """场景：APP 误传 RetryTransport；预期：拒绝双层包装，要求原生底层 transport。"""

    sync_base = TimeoutRecordingTransport()
    sync_retry_transport = RetryTransport(transport=sync_base, retry=create_retry(total=0))
    with pytest.raises(TypeError, match=r"unwrapped.*not RetryTransport"):
        create_client(create_retry(total=0), transport=sync_retry_transport)
    sync_retry_transport.close()

    async def scenario() -> AsyncTimeoutRecordingTransport:
        async_base = AsyncTimeoutRecordingTransport()
        async_retry_transport = RetryTransport(transport=async_base, retry=create_retry(total=0))
        with pytest.raises(TypeError, match=r"unwrapped.*not RetryTransport"):
            create_async_client(create_retry(total=0), transport=async_retry_transport)
        await async_retry_transport.aclose()
        return async_base

    assert asyncio.run(scenario()).closed is True
    assert sync_base.closed is True


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ((1.0, 2.0), {"connect": 1.0, "read": 2.0, "write": None, "pool": None}),
        ((1.0, 2.0, 3.0), {"connect": 1.0, "read": 2.0, "write": 3.0, "pool": None}),
        ((1.0, 2.0, 3.0, 4.0), {"connect": 1.0, "read": 2.0, "write": 3.0, "pool": 4.0}),
    ],
    ids=["pair", "triple", "quadruple"],
)
def test_timeout_tuples_are_normalized_to_public_httpx_fields(
    configured: Any,
    expected: dict[str, Optional[float]],
) -> None:
    """场景：factory 收到 2/3/4 元 tuple；预期：主动转换为公开 Timeout 四字段。"""

    timeout = httpx_client._normalize_timeout(configured)

    assert isinstance(timeout, httpx.Timeout)
    assert {
        "connect": timeout.connect,
        "read": timeout.read,
        "write": timeout.write,
        "pool": timeout.pool,
    } == expected


@pytest.mark.parametrize("factory", [create_client, create_async_client], ids=["sync", "async"])
@pytest.mark.parametrize("timeout", [(), (1.0,), (1.0, 2.0, 3.0, 4.0, 5.0)])
def test_invalid_timeout_tuple_lengths_are_rejected(
    factory: Callable[..., object],
    timeout: tuple[float, ...],
) -> None:
    """场景：timeout tuple 长度不受支持；预期：创建 transport 前立即抛清楚的 TypeError。"""

    with pytest.raises(TypeError, match="2, 3, or 4"):
        factory(create_retry(total=0), timeout=timeout)


def test_sync_timeout_default_override_and_explicit_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：同步 Client 配置默认 timeout；预期：省略、覆盖和显式 None 均采用 HTTPX 原生语义。"""

    recorder = TimeoutRecordingTransport()
    monkeypatch.setattr(httpx_client._httpx, "HTTPTransport", lambda: recorder)

    with create_client(create_retry(total=0), timeout=(0.25, 0.75)) as client:
        client.get("http://timeout.test/default")
        client.get("http://timeout.test/override", timeout=0.125)
        client.get("http://timeout.test/disabled", timeout=None)

    assert recorder.timeouts == [
        {"connect": 0.25, "read": 0.75, "write": None, "pool": None},
        {"connect": 0.125, "read": 0.125, "write": 0.125, "pool": 0.125},
        {"connect": None, "read": None, "write": None, "pool": None},
    ]
    assert recorder.closed is True


def test_sync_factory_none_disables_httpx_builtin_five_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：factory timeout=None；预期：禁用 HTTPX 自带的五秒默认值，与 Requests 版本一致。"""

    recorder = TimeoutRecordingTransport()
    monkeypatch.setattr(httpx_client._httpx, "HTTPTransport", lambda: recorder)

    with create_client(create_retry(total=0)) as client:
        client.get("http://timeout.test/unbounded")

    assert recorder.timeouts == [{"connect": None, "read": None, "write": None, "pool": None}]


def test_async_timeout_default_override_and_explicit_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：异步 Client 配置默认 timeout；预期：覆盖规则与同步 factory 一致。"""

    recorder = AsyncTimeoutRecordingTransport()
    monkeypatch.setattr(httpx_client._httpx, "AsyncHTTPTransport", lambda: recorder)

    async def scenario() -> None:
        async with create_async_client(create_retry(total=0), timeout=(0.25, 0.75)) as client:
            await client.get("http://timeout.test/default")
            await client.get("http://timeout.test/override", timeout=0.125)
            await client.get("http://timeout.test/disabled", timeout=None)

    asyncio.run(scenario())

    assert recorder.timeouts == [
        {"connect": 0.25, "read": 0.75, "write": None, "pool": None},
        {"connect": 0.125, "read": 0.125, "write": 0.125, "pool": 0.125},
        {"connect": None, "read": None, "write": None, "pool": None},
    ]
    assert recorder.closed is True


def test_sync_status_retry_uses_one_client_and_returns_success() -> None:
    """场景：本地服务依次返回 503、200；预期：同一同步 Client 内完成 retry。"""

    server_context = SequenceServer([ResponseSpec(503), ResponseSpec(200, b"ok")])
    client_context = create_client(create_retry(total=1, backoff_factor=0), timeout=1)
    with server_context as server, client_context as client:
        response = client.get(server.url)

    assert response.status_code == 200
    assert response.content == b"ok"
    assert server.requests == 2


def test_async_status_retry_uses_one_client_and_returns_success() -> None:
    """场景：本地服务依次返回 503、200；预期：同一异步 Client 内完成非阻塞 retry。"""

    async def request(url: str) -> httpx.Response:
        async with create_async_client(create_retry(total=1, backoff_factor=0), timeout=1) as client:
            return await client.get(url)

    with SequenceServer([ResponseSpec(503), ResponseSpec(200, b"ok")]) as server:
        response = asyncio.run(request(server.url))

    assert response.status_code == 200
    assert response.content == b"ok"
    assert server.requests == 2


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_status_retry_exhaustion_returns_final_native_response(asynchronous: bool) -> None:
    """场景：服务持续返回 503；预期：耗尽后返回末次 Response，由 APP 决定是否 raise_for_status。"""

    async def async_request(url: str) -> httpx.Response:
        async with create_async_client(create_retry(total=1, backoff_factor=0), timeout=1) as client:
            return await client.get(url)

    with SequenceServer([ResponseSpec(503), ResponseSpec(503)]) as server:
        if asynchronous:
            response = asyncio.run(async_request(server.url))
        else:
            with create_client(create_retry(total=1, backoff_factor=0), timeout=1) as client:
                response = client.get(server.url)

    assert response.status_code == 503
    assert server.requests == 2
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_default_post_does_not_retry_status(asynchronous: bool) -> None:
    """场景：POST 返回可重试 503；预期：默认安全方法集合防止重复业务副作用。"""

    async def async_request(url: str) -> httpx.Response:
        async with create_async_client(create_retry(total=3, backoff_factor=0), timeout=1) as client:
            return await client.post(url, content=b"payload")

    with SequenceServer([ResponseSpec(503), ResponseSpec(200)]) as server:
        if asynchronous:
            response = asyncio.run(async_request(server.url))
        else:
            with create_client(create_retry(total=3, backoff_factor=0), timeout=1) as client:
                response = client.post(server.url, content=b"payload")

    assert response.status_code == 503
    assert server.requests == 1


def test_sync_retry_after_is_used_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：503 携带 Retry-After；预期：同步 retry 采用服务端等待值。"""

    sleeps: list[float] = []

    def record_sleep(self: Retry, response: Union[httpx.Response, Exception]) -> None:
        headers = response.headers if isinstance(response, httpx.Response) else {}
        sleeps.append(self._calculate_sleep(headers))

    monkeypatch.setattr(Retry, "sleep", record_sleep)

    responses = [
        ResponseSpec(503, headers={"Retry-After": "2"}),
        ResponseSpec(200),
    ]
    client_context = create_client(create_retry(total=1, backoff_factor=0), timeout=1)
    with SequenceServer(responses) as server, client_context as client:
        response = client.get(server.url)

    assert response.status_code == 200
    assert sleeps == [2.0]


def test_async_retry_after_uses_async_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：异步 503 携带 Retry-After；预期：调用 Retry.asleep，不阻塞事件循环线程。"""

    sleeps: list[float] = []

    async def record_sleep(self: Retry, response: Union[httpx.Response, Exception]) -> None:
        headers = response.headers if isinstance(response, httpx.Response) else {}
        sleeps.append(self._calculate_sleep(headers))

    monkeypatch.setattr(Retry, "asleep", record_sleep)

    async def request(url: str) -> httpx.Response:
        async with create_async_client(create_retry(total=1, backoff_factor=0), timeout=1) as client:
            return await client.get(url)

    responses = [
        ResponseSpec(503, headers={"Retry-After": "2"}),
        ResponseSpec(200),
    ]
    with SequenceServer(responses) as server:
        response = asyncio.run(request(server.url))

    assert response.status_code == 200
    assert sleeps == [2.0]


def test_retryable_connect_error_recovers_and_preserves_native_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：GET 首次建连失败、第二次成功；预期：重试后返回原生 HTTPX Response。"""

    transport = ScriptedTransport([connect_error, success])
    monkeypatch.setattr(httpx_client._httpx, "HTTPTransport", lambda: transport)

    with create_client(create_retry(total=1, backoff_factor=0), timeout=1) as client:
        response = client.get("http://connect.test/resource")

    assert response.status_code == 200
    assert transport.calls == 2


def test_connect_retry_exhaustion_raises_native_httpx_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：GET 持续建连失败；预期：耗尽后直接抛原生 ConnectError，不包装业务异常。"""

    transport = ScriptedTransport([connect_error, connect_error])
    monkeypatch.setattr(httpx_client._httpx, "HTTPTransport", lambda: transport)

    with (
        create_client(create_retry(total=1, backoff_factor=0), timeout=1) as client,
        pytest.raises(
            httpx.ConnectError,
            match="synthetic connect failure",
        ),
    ):
        client.get("http://connect.test/resource")

    assert transport.calls == 2


def test_post_connect_error_is_not_retried_by_httpx_retry_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：POST 建连失败；预期：httpx-retries 的方法 gate 使默认 POST 只发送一次。"""

    transport = ScriptedTransport([connect_error, success])
    monkeypatch.setattr(httpx_client._httpx, "HTTPTransport", lambda: transport)

    with create_client(create_retry(total=3, backoff_factor=0), timeout=1) as client, pytest.raises(httpx.ConnectError):
        client.post("http://connect.test/resource", content=b"payload")

    assert transport.calls == 1


def test_response_body_failure_after_headers_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：200 响应头后读取 body 超时；预期：错误发生在 transport 外，不透明重放请求。"""

    def body_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BodyFailureStream(request))

    transport = ScriptedTransport([body_failure, success])
    monkeypatch.setattr(httpx_client._httpx, "HTTPTransport", lambda: transport)

    with (
        create_client(create_retry(total=3, backoff_factor=0), timeout=1) as client,
        pytest.raises(
            httpx.ReadTimeout,
            match="body read timed out",
        ),
    ):
        client.get("http://body.test/resource")

    assert transport.calls == 1


def test_create_retry_rejects_bare_string_allowed_methods() -> None:
    """场景：create_retry 传入单字符串 allowed_methods；预期：立即抛 TypeError。"""

    with pytest.raises(TypeError, match="must be a collection of method names, not a single string"):
        create_retry(allowed_methods="GET")  # type: ignore[arg-type]


@pytest.mark.parametrize("factory", [create_client, create_async_client], ids=["sync", "async"])
@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, "10", [1, 2]],
    ids=["bool-true", "bool-false", "string", "list"],
)
def test_client_factories_reject_invalid_timeout_shapes(
    factory: Callable[..., Any],
    invalid_timeout: Any,
) -> None:
    """场景：factory 传入非法 timeout 类型；预期：立即抛 TypeError。"""

    with pytest.raises(TypeError, match="timeout"):
        factory(create_retry(total=0), timeout=invalid_timeout)


@pytest.mark.parametrize("factory", [create_client, create_async_client], ids=["sync", "async"])
@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1, float("nan"), float("inf"), (0, 1), (1, 0), (1, 2, -1)],
    ids=["zero", "negative", "nan", "inf", "zero-connect", "zero-read", "negative-write"],
)
def test_client_factories_reject_invalid_timeout_values(
    factory: Callable[..., Any],
    invalid_timeout: Any,
) -> None:
    """场景：factory 传入 <= 0 或非有限的数字；预期：立即抛 ValueError。"""

    with pytest.raises(ValueError, match="finite and greater than 0"):
        factory(create_retry(total=0), timeout=invalid_timeout)

