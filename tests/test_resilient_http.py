from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Union

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import connection
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NameResolutionError,
    NewConnectionError,
    ReadTimeoutError,
    ResponseError,
)
from urllib3.exceptions import ProxyError as Urllib3ProxyError
from urllib3.exceptions import SSLError as Urllib3SSLError
from urllib3.util import Retry

import resilient_http
from resilient_http import create_session

# 中文测试说明使用中文全角标点，以保持注释的自然可读性。
# ruff: noqa: RUF002, RUF003


@dataclass(frozen=True)
class ResponseSpec:
    """描述本地测试服务器对一次请求采取的确定性动作。"""

    status: int = 200
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    delay_before_headers: float = 0
    delay_before_body: float = 0
    disconnect_before_headers: bool = False
    declared_length: Optional[int] = None


ServerAction = Union[int, ResponseSpec]
RecordedRequest = tuple[str, str, bytes]


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """测试故意制造断连，因此不把预期的 handler 异常输出到 stderr。"""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


class ScriptedServer:
    """真实监听 loopback 的 HTTP server，按顺序执行预设响应。"""

    def __init__(self, actions: Sequence[ServerAction]) -> None:
        if not actions:
            raise ValueError("actions cannot be empty")

        self._actions = tuple(actions)
        self._index = 0
        self._lock = threading.Lock()
        self.requests: list[RecordedRequest] = []
        self._server: Optional[QuietThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> ScriptedServer:
        """启动服务器；每个实例使用操作系统分配的独立端口。"""

        scripted = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                request_body = self.rfile.read(content_length) if content_length else b""

                with scripted._lock:
                    scripted.requests.append((self.command, self.path, request_body))
                    index = min(scripted._index, len(scripted._actions) - 1)
                    action = scripted._actions[index]
                    scripted._index += 1

                if isinstance(action, int):
                    spec = ResponseSpec(status=action, body=str(action).encode("ascii"))
                else:
                    spec = action

                if spec.disconnect_before_headers:
                    # 在收到请求后、发送响应头前关闭连接，稳定模拟 remote reset/disconnect。
                    self.close_connection = True
                    with suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return

                if spec.delay_before_headers:
                    time.sleep(spec.delay_before_headers)

                response_body = b"" if self.command == "HEAD" else spec.body
                declared_length = len(response_body) if spec.declared_length is None else spec.declared_length
                self.send_response(spec.status)
                for name, value in spec.headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(declared_length))
                self.end_headers()

                if spec.delay_before_body:
                    time.sleep(spec.delay_before_body)

                if response_body:
                    try:
                        self.wfile.write(response_body)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # read-timeout 用例中的客户端已经主动关闭第一条超时连接。
                        return

                if declared_length > len(response_body):
                    # Content-Length 大于实际 body，并立即送出 EOF，模拟下载中途断线。
                    self.close_connection = True
                    with suppress(OSError):
                        self.connection.shutdown(socket.SHUT_WR)

            do_DELETE = _respond
            do_GET = _respond
            do_HEAD = _respond
            do_OPTIONS = _respond
            do_PATCH = _respond
            do_POST = _respond
            do_PUT = _respond

            def log_message(self, *args: Any) -> None:
                return None

        self._server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        """返回服务器的稳定测试资源 URL。"""

        if self._server is None:
            raise RuntimeError("server has not been started")
        host, port = self._server.server_address
        return f"http://{host}:{port}/resource"

    def close(self) -> None:
        """停止 server 并等待 accept loop 退出，避免测试间泄漏线程或端口。"""

        if self._server is None or self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._server = None
        self._thread = None


ServerFactory = Callable[[Sequence[ServerAction]], ScriptedServer]


@pytest.fixture
def server_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[ServerFactory]:
    """提供自动清理的真实 HTTP server，并确保 loopback 请求不经过环境代理。"""

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    servers: list[ScriptedServer] = []

    def create(actions: Sequence[ServerAction]) -> ScriptedServer:
        server = ScriptedServer(actions).start()
        servers.append(server)
        return server

    yield create

    for server in reversed(servers):
        server.close()


def make_retry(
    *,
    total: int,
    connect: int = 0,
    read: Union[bool, int] = 0,
    status: int = 0,
    other: int = 0,
    allowed_methods: Optional[frozenset[str]] = frozenset({"GET"}),
    status_forcelist: Sequence[int] = (),
    raise_on_status: bool = False,
) -> Retry:
    """建立零退避 Retry，让测试只验证次数和分类，不为 sleep 增加时间。"""

    return Retry(
        total=total,
        connect=connect,
        read=read,
        redirect=0,
        status=status,
        other=other,
        allowed_methods=allowed_methods,
        status_forcelist=status_forcelist,
        backoff_factor=0,
        raise_on_status=raise_on_status,
    )


def create_test_session(retry: Retry) -> requests.Session:
    """创建受测 Session 并禁用环境代理，保证 CI 的 HTTP_PROXY 不会改变异常分类。"""

    session = create_session(retry)
    session.trust_env = False
    return session


def request_methods(server: ScriptedServer) -> list[str]:
    """提取 server 实际收到的方法，便于清晰断言 retry 次数。"""

    return [method for method, _, _ in server.requests]


def contains_exception(error: BaseException, expected_type: type[BaseException]) -> bool:
    """递归查看异常参数和 cause/context，验证 Requests 保留的 urllib3 根因。"""

    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, expected_type):
            return True
        pending.extend(value for value in current.args if isinstance(value, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def test_only_retry_and_create_session_are_public() -> None:
    """场景：使用者从包根入口导入；预期：仅暴露原生 Retry 与 factory，旧 API 不再可见。"""

    assert resilient_http.__all__ == ["Retry", "create_session"]
    assert resilient_http.Retry is Retry
    assert resilient_http.create_session is create_session

    old_symbols = (
        "AsyncHttpClient",
        "BaseHttpError",
        "BusinessHttpError",
        "ErrorMappingPolicy",
        "ErrorMappingRule",
        "HttpClient",
        "HttpClientConfig",
        "NonReplayableRequestError",
        "PoolConfig",
        "RetryConfig",
        "SystemHttpError",
        "TimeoutConfig",
        "retry_from_dict",
    )
    assert all(not hasattr(resilient_http, name) for name in old_symbols)


@pytest.mark.parametrize("invalid_retry", [None, 0, object()], ids=["none", "integer", "plain-object"])
def test_create_session_rejects_non_retry_values(invalid_retry: object) -> None:
    """场景：调用方传入非 urllib3 Retry；预期：factory 立即报 TypeError，不创建半配置 Session。"""

    with pytest.raises(TypeError, match=r"urllib3\.util\.Retry"):
        create_session(invalid_retry)  # type: ignore[arg-type]


def test_each_create_session_has_independent_adapters_and_pools() -> None:
    """场景：同一 Retry 配置创建两次；预期：Session、两种 Adapter 与 pool 均独立，配置对象不被包装。"""

    retry = Retry(total=1)
    with create_test_session(retry) as first, create_test_session(retry) as second:
        assert first is not second

        for url in ("http://example.test", "https://example.test"):
            first_adapter = first.get_adapter(url)
            second_adapter = second.get_adapter(url)
            assert isinstance(first_adapter, HTTPAdapter)
            assert isinstance(second_adapter, HTTPAdapter)
            assert first_adapter is not second_adapter
            assert first_adapter.poolmanager is not second_adapter.poolmanager
            assert first_adapter.max_retries is retry
            assert second_adapter.max_retries is retry


def test_closing_one_session_does_not_affect_another(server_factory: ServerFactory) -> None:
    """场景：先关闭一个 factory 结果；预期：另一 Session 的独立 pool 仍能连接真实 server 并读取响应。"""

    first = create_test_session(Retry(total=0))
    second = create_test_session(Retry(total=0))
    first.close()
    server = server_factory([200])

    try:
        response = second.get(server.url, timeout=1)
    finally:
        second.close()

    assert response.status_code == 200
    assert response.content == b"200"


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_configured_transient_http_status_is_retried_then_succeeds(
    status: int,
    server_factory: ServerFactory,
) -> None:
    """场景：GET 先返回已配置的临时 HTTP 错误再返回 200；预期：状态重试一次并交付成功响应。"""

    retryable_statuses = (408, 429, 500, 502, 503, 504)
    retry = make_retry(
        total=1,
        status=1,
        status_forcelist=retryable_statuses,
        raise_on_status=True,
    )
    server = server_factory([status, 200])

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == 200
    assert request_methods(server) == ["GET", "GET"]


def test_retry_after_header_can_trigger_retry_without_status_forcelist(
    monkeypatch: pytest.MonkeyPatch,
    server_factory: ServerFactory,
) -> None:
    """场景：429 带合法 Retry-After 且 forcelist 为空；预期：urllib3 尊重 header、等待指定秒数后重试。"""

    sleep_calls: list[float] = []
    monkeypatch.setattr("urllib3.util.retry.time.sleep", sleep_calls.append)
    server = server_factory(
        [
            ResponseSpec(status=429, headers=(("Retry-After", "1"),)),
            ResponseSpec(status=200, body=b"ok"),
        ]
    )
    retry = make_retry(total=1, status=1)

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == 200
    assert request_methods(server) == ["GET", "GET"]
    assert sleep_calls == [1]


def test_retry_after_header_is_ignored_when_support_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    server_factory: ServerFactory,
) -> None:
    """场景：429 只因 Retry-After 才可能重试，但 APP 禁用了该能力；预期：不 sleep、不 retry，返回首次 429。"""

    sleep_calls: list[float] = []
    monkeypatch.setattr("urllib3.util.retry.time.sleep", sleep_calls.append)
    server = server_factory(
        [
            ResponseSpec(status=429, headers=(("Retry-After", "1"),)),
            ResponseSpec(status=200, body=b"must-not-be-used"),
        ]
    )
    retry = Retry(
        total=1,
        connect=0,
        read=0,
        redirect=0,
        status=1,
        other=0,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=(),
        backoff_factor=0,
        respect_retry_after_header=False,
        raise_on_status=False,
    )

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == 429
    assert request_methods(server) == ["GET"]
    assert sleep_calls == []


def test_invalid_retry_after_header_raises_native_invalid_header(
    server_factory: ServerFactory,
) -> None:
    """场景：429 的 Retry-After 既不是秒数也不是 HTTP 日期；预期：Requests 抛 InvalidHeader，不能静默 retry。"""

    server = server_factory(
        [
            ResponseSpec(status=429, headers=(("Retry-After", "not-a-valid-value"),)),
            ResponseSpec(status=200, body=b"must-not-hide-invalid-header"),
        ]
    )
    retry = make_retry(total=1, status=1)

    with create_test_session(retry) as session, pytest.raises(requests.exceptions.InvalidHeader):
        session.get(server.url, timeout=1)

    assert request_methods(server) == ["GET"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
def test_unconfigured_http_error_is_not_retried_and_raise_for_status_is_native(
    status: int,
    server_factory: ServerFactory,
) -> None:
    """场景：响应是未列入 forcelist 的 4xx/5xx；预期：只请求一次，HTTPError 由 raise_for_status 原生抛出。"""

    retry = make_retry(
        total=3,
        status=3,
        status_forcelist=(408, 429, 500, 502, 503, 504),
    )
    server = server_factory([status, 200])

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == status
    assert request_methods(server) == ["GET"]
    with pytest.raises(requests.HTTPError) as caught:
        response.raise_for_status()
    assert caught.value.response is response


def test_exhausted_status_retry_returns_final_response_when_not_raising(
    server_factory: ServerFactory,
) -> None:
    """场景：503 持续到两次 retry 耗尽且 raise_on_status=False；预期：返回第三个 503 供业务层处理。"""

    retry = make_retry(
        total=2,
        status=2,
        status_forcelist=(503,),
        raise_on_status=False,
    )
    server = server_factory([503])

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == 503
    assert request_methods(server) == ["GET", "GET", "GET"]
    with pytest.raises(requests.HTTPError) as caught:
        response.raise_for_status()
    assert caught.value.response is response


def test_exhausted_status_retry_raises_retry_error_when_configured(
    server_factory: ServerFactory,
) -> None:
    """场景：503 重试耗尽且 raise_on_status=True；预期：Requests 抛 RetryError，而不是返回最后响应。"""

    retry = make_retry(
        total=2,
        status=2,
        status_forcelist=(503,),
        raise_on_status=True,
    )
    server = server_factory([503])

    with create_test_session(retry) as session, pytest.raises(requests.exceptions.RetryError) as caught:
        session.get(server.url, timeout=1)

    assert request_methods(server) == ["GET", "GET", "GET"]
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, ResponseError)


def test_status_budget_limits_attempts_even_when_total_is_larger(
    server_factory: ServerFactory,
) -> None:
    """场景：total=5 但 status=1；预期：状态分类预算先耗尽，只发生初次请求加一次状态重试。"""

    retry = make_retry(
        total=5,
        status=1,
        status_forcelist=(503,),
        raise_on_status=False,
    )
    server = server_factory([503])

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == 503
    assert request_methods(server) == ["GET", "GET"]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_allowed_idempotent_method_retries_status(
    method: str,
    server_factory: ServerFactory,
) -> None:
    """场景：幂等方法在 allowed_methods 且首次为 503；预期：urllib3 自动重试并取得 200。"""

    retry = make_retry(
        total=1,
        status=1,
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        status_forcelist=(503,),
    )
    server = server_factory([503, 200])

    with create_test_session(retry) as session:
        response = session.request(method, server.url, timeout=1)

    assert response.status_code == 200
    assert request_methods(server) == [method, method]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_non_allowed_method_does_not_retry_status(
    method: str,
    server_factory: ServerFactory,
) -> None:
    """场景：非幂等方法未加入 allowed_methods 且返回 503；预期：不重放请求，直接返回首次错误响应。"""

    retry = make_retry(
        total=2,
        status=2,
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        status_forcelist=(503,),
    )
    server = server_factory([503, 200])

    with create_test_session(retry) as session:
        response = session.request(method, server.url, data=b"payload", timeout=1)

    assert response.status_code == 503
    assert request_methods(server) == [method]


def test_explicitly_allowed_post_retries_replayable_body(server_factory: ServerFactory) -> None:
    """场景：APP 明确允许 POST 且 body 为 bytes；预期：503 后重发同一 payload，并在第二次得到 200。"""

    retry = make_retry(
        total=1,
        status=1,
        allowed_methods=frozenset({"POST"}),
        status_forcelist=(503,),
    )
    server = server_factory([503, 200])

    with create_test_session(retry) as session:
        response = session.post(server.url, data=b"payload", timeout=1)

    assert response.status_code == 200
    assert server.requests == [
        ("POST", "/resource", b"payload"),
        ("POST", "/resource", b"payload"),
    ]


def test_retry_history_is_independent_for_each_logical_request(
    server_factory: ServerFactory,
) -> None:
    """场景：同一 Session 连续执行两个 503→200 请求；预期：两者各有独立预算，传入 Retry 的 history 不变。"""

    retry = make_retry(
        total=1,
        status=1,
        status_forcelist=(503,),
        raise_on_status=True,
    )
    server = server_factory([503, 200, 503, 200])

    with create_test_session(retry) as session:
        first = session.get(server.url, timeout=1)
        second = session.get(server.url, timeout=1)

    assert (first.status_code, second.status_code) == (200, 200)
    assert request_methods(server) == ["GET", "GET", "GET", "GET"]
    assert retry.history == ()


def test_connection_refused_retries_until_connect_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：每次 TCP 建连都返回 ECONNREFUSED；预期：按 connect 预算重试两次后包装为原生 ConnectionError。"""

    attempts = 0

    def refuse_connection(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("synthetic persistent connection refusal")

    monkeypatch.setattr(connection, "create_connection", refuse_connection)
    retry = make_retry(total=2, connect=2)

    with create_test_session(retry) as session, pytest.raises(requests.ConnectionError) as caught:
        session.get("http://connection-refused.invalid/resource", timeout=0.2)

    assert attempts == 3
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, NewConnectionError)


def test_first_connect_failure_is_retried_on_a_new_physical_connection(
    monkeypatch: pytest.MonkeyPatch,
    server_factory: ServerFactory,
) -> None:
    """场景：首次建连被明确拒绝、第二次可连接；预期：retry 新建 TCP 连接并让同一逻辑 GET 成功。"""

    server = server_factory([200])
    real_create_connection = connection.create_connection
    attempts = 0

    def fail_once_then_connect(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError("synthetic first connection failure")
        return real_create_connection(*args, **kwargs)

    monkeypatch.setattr(connection, "create_connection", fail_once_then_connect)
    retry = make_retry(total=1, connect=1)

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert attempts == 2
    assert response.status_code == 200
    # 第一次在 TCP 建立前失败，所以真实 server 只会收到第二次尝试。
    assert request_methods(server) == ["GET"]


def test_connect_retry_is_not_blocked_by_allowed_methods_for_post(
    monkeypatch: pytest.MonkeyPatch,
    server_factory: ServerFactory,
) -> None:
    """场景：POST 首次在发送任何 bytes 前建连失败；预期：connect retry 不受 allowed_methods 限制并可安全重试。"""

    server = server_factory([200])
    real_create_connection = connection.create_connection
    attempts = 0

    def fail_once_then_connect(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError("synthetic first connection failure")
        return real_create_connection(*args, **kwargs)

    monkeypatch.setattr(connection, "create_connection", fail_once_then_connect)
    retry = make_retry(
        total=1,
        connect=1,
        allowed_methods=frozenset({"GET"}),
    )

    with create_test_session(retry) as session:
        response = session.post(server.url, data=b"payload", timeout=1)

    assert attempts == 2
    assert response.status_code == 200
    assert server.requests == [("POST", "/resource", b"payload")]


def test_dns_resolution_failure_uses_connect_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：DNS 每次都返回 gaierror；预期：按 connect 预算尝试三次，最终保留 MaxRetryError 根因。"""

    attempts = 0

    def fail_dns(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        raise socket.gaierror(socket.EAI_NONAME, "synthetic name resolution failure")

    monkeypatch.setattr(connection, "create_connection", fail_dns)
    retry = make_retry(total=2, connect=2)

    with create_test_session(retry) as session, pytest.raises(requests.ConnectionError) as caught:
        session.get("http://does-not-resolve.invalid/resource", timeout=0.2)

    assert attempts == 3
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, NameResolutionError)


def test_connect_timeout_uses_connect_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：TCP connect 每次超时；预期：连接类预算耗尽后抛 Requests ConnectTimeout，共尝试两次。"""

    attempts = 0

    def time_out_connect(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        raise socket.timeout("synthetic connect timeout")

    monkeypatch.setattr(connection, "create_connection", time_out_connect)
    retry = make_retry(total=1, connect=1)

    with create_test_session(retry) as session, pytest.raises(requests.ConnectTimeout) as caught:
        session.get("http://connect-timeout.invalid/resource", timeout=0.2)

    assert attempts == 2
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, ConnectTimeoutError)


def test_proxy_connection_failure_uses_connect_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：HTTP proxy 在 TCP 建连阶段持续拒绝连接；预期：按 connect 预算重试，最终抛 Requests ProxyError。"""

    attempts = 0

    def refuse_proxy_connection(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("synthetic proxy connection refusal")

    monkeypatch.setattr(connection, "create_connection", refuse_proxy_connection)
    retry = make_retry(total=1, connect=1)

    with create_test_session(retry) as session, pytest.raises(requests.exceptions.ProxyError) as caught:
        session.proxies = {"http": "http://proxy.invalid:3128"}
        session.get("http://upstream.invalid/resource", timeout=0.2)

    assert attempts == 2
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, Urllib3ProxyError)
    assert contains_exception(caught.value, NewConnectionError)


@pytest.mark.parametrize(("other", "expected_attempts"), [(0, 1), (1, 2)])
def test_tls_handshake_error_uses_other_retry_budget(
    other: int,
    expected_attempts: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：HTTPS TLS handshake 持续失败；预期：由 other 而非 connect/read 预算控制次数，最终抛 SSLError。"""

    attempts = 0

    def fail_tls_handshake(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise Urllib3SSLError("synthetic TLS handshake failure")

    monkeypatch.setattr("urllib3.connection.HTTPSConnection.connect", fail_tls_handshake)
    retry = make_retry(total=2, other=other)

    with create_test_session(retry) as session, pytest.raises(requests.exceptions.SSLError) as caught:
        session.get("https://tls-failure.invalid/resource", timeout=0.2)

    assert attempts == expected_attempts
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, Urllib3SSLError)


def test_remote_disconnect_before_headers_is_retried_for_get(
    server_factory: ServerFactory,
) -> None:
    """场景：server 收到 GET 后在响应头前断连；预期：这是 read/protocol 错误，可按 read 预算重试成功。"""

    server = server_factory(
        [
            ResponseSpec(disconnect_before_headers=True),
            ResponseSpec(status=200, body=b"ok"),
        ]
    )
    retry = make_retry(total=1, read=1)

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=1)

    assert response.status_code == 200
    assert response.content == b"ok"
    assert request_methods(server) == ["GET", "GET"]


def test_persistent_remote_disconnect_exhausts_read_retry(
    server_factory: ServerFactory,
) -> None:
    """场景：每次均在响应头前断连；预期：初次加一次 read retry 后抛 ConnectionError，根因含远端断连。"""

    server = server_factory([ResponseSpec(disconnect_before_headers=True)])
    retry = make_retry(total=1, read=1)

    with create_test_session(retry) as session, pytest.raises(requests.ConnectionError) as caught:
        session.get(server.url, timeout=1)

    assert request_methods(server) == ["GET", "GET"]
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, RemoteDisconnected)


def test_remote_disconnect_is_not_retried_for_non_allowed_post(
    server_factory: ServerFactory,
) -> None:
    """场景：POST 发出后在响应头前断连且不在 allowed_methods；预期：为防止重复副作用，不执行 read retry。"""

    server = server_factory(
        [
            ResponseSpec(disconnect_before_headers=True),
            ResponseSpec(status=200, body=b"must-not-be-used"),
        ]
    )
    retry = make_retry(
        total=2,
        read=2,
        allowed_methods=frozenset({"GET"}),
    )

    with create_test_session(retry) as session, pytest.raises(requests.ConnectionError):
        session.post(server.url, data=b"payload", timeout=1)

    assert server.requests == [("POST", "/resource", b"payload")]


def test_read_timeout_before_headers_is_retried_then_succeeds(
    server_factory: ServerFactory,
) -> None:
    """场景：首次响应头慢于 read timeout、第二次立即成功；预期：GET 使用 read 预算重试新请求。"""

    server = server_factory(
        [
            ResponseSpec(status=200, body=b"late", delay_before_headers=0.2),
            ResponseSpec(status=200, body=b"ok"),
        ]
    )
    retry = make_retry(total=1, read=1)

    with create_test_session(retry) as session:
        response = session.get(server.url, timeout=(0.2, 0.05))

    assert response.status_code == 200
    assert response.content == b"ok"
    assert request_methods(server) == ["GET", "GET"]


def test_read_timeout_is_not_retried_when_read_retry_is_disabled(
    server_factory: ServerFactory,
) -> None:
    """场景：响应头超时且 read=False；预期：立即抛原生 ReadTimeout，只发送一次 GET，不消耗 total 预算。"""

    server = server_factory(
        [
            ResponseSpec(status=200, body=b"late", delay_before_headers=0.2),
            ResponseSpec(status=200, body=b"must-not-be-used"),
        ]
    )
    retry = make_retry(total=2, read=False)

    with create_test_session(retry) as session, pytest.raises(requests.ReadTimeout):
        session.get(server.url, timeout=(0.2, 0.05))

    assert request_methods(server) == ["GET"]


def test_read_timeout_exhaustion_raises_native_requests_error(
    server_factory: ServerFactory,
) -> None:
    """场景：两次响应头都超过 read timeout；预期：read 预算耗尽后抛 Requests 连接类异常并保留重试根因。"""

    slow = ResponseSpec(status=200, body=b"late", delay_before_headers=0.2)
    server = server_factory([slow, slow])
    retry = make_retry(total=1, read=1)

    with create_test_session(retry) as session, pytest.raises(requests.ConnectionError) as caught:
        session.get(server.url, timeout=(0.2, 0.05))

    assert request_methods(server) == ["GET", "GET"]
    assert contains_exception(caught.value, MaxRetryError)
    assert contains_exception(caught.value, ReadTimeoutError)


def test_incomplete_response_body_is_not_retried_after_headers(
    server_factory: ServerFactory,
) -> None:
    """场景：响应头声明 100 bytes 但 body 中途断线；预期：读取阶段抛 ChunkedEncodingError，且不透明重放请求。"""

    server = server_factory(
        [
            ResponseSpec(status=200, body=b"short", declared_length=100),
            ResponseSpec(status=200, body=b"would-hide-the-error"),
        ]
    )
    retry = make_retry(total=2, read=2)

    with create_test_session(retry) as session, pytest.raises(requests.exceptions.ChunkedEncodingError):
        session.get(server.url, timeout=1)

    # Requests 在 Adapter 返回 Response 后消费 body，此时已越过 urllib3 的自动 retry 边界。
    assert request_methods(server) == ["GET"]


def test_response_body_read_timeout_is_not_retried_after_headers(
    server_factory: ServerFactory,
) -> None:
    """场景：响应头已到达但 body 超过 read timeout；预期：抛原生 ConnectionError，且不重放已获响应的请求。"""

    server = server_factory(
        [
            ResponseSpec(status=200, body=b"late", delay_before_body=0.2),
            ResponseSpec(status=200, body=b"must-not-hide-timeout"),
        ]
    )
    retry = make_retry(total=2, read=2)

    with create_test_session(retry) as session, pytest.raises(requests.ConnectionError) as caught:
        session.get(server.url, timeout=(0.2, 0.05))

    assert contains_exception(caught.value, ReadTimeoutError)
    assert request_methods(server) == ["GET"]


@pytest.mark.parametrize(
    ("url", "error_type"),
    [
        ("example.com/resource", requests.exceptions.MissingSchema),
        ("http://", requests.exceptions.InvalidURL),
        ("ftp://example.com/resource", requests.exceptions.InvalidSchema),
    ],
    ids=["missing-schema", "missing-host", "unsupported-scheme"],
)
def test_request_url_validation_errors_are_native_and_do_not_retry(
    url: str,
    error_type: type[requests.RequestException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：URL 缺 scheme/host 或使用未挂载 scheme；预期：Requests 校验立即报原生异常，不进入建连 retry。"""

    attempts = 0
    real_create_connection = connection.create_connection

    def counted_create_connection(*args: Any, **kwargs: Any) -> socket.socket:
        nonlocal attempts
        attempts += 1
        return real_create_connection(*args, **kwargs)

    monkeypatch.setattr(connection, "create_connection", counted_create_connection)
    with create_test_session(Retry(total=3)) as session, pytest.raises(error_type):
        session.get(url, timeout=0.2)

    assert attempts == 0


def test_redirect_loop_uses_requests_native_too_many_redirects(
    server_factory: ServerFactory,
) -> None:
    """场景：真实 server 永久 302 回同一路径；预期：由 Requests redirect 上限抛 TooManyRedirects，不混入状态重试。"""

    redirect = ResponseSpec(status=302, headers=(("Location", "/resource"),))
    server = server_factory([redirect])

    with create_test_session(Retry(total=0, redirect=0)) as session:
        session.max_redirects = 2
        with pytest.raises(requests.TooManyRedirects):
            session.get(server.url, timeout=1)

    # 首次响应加两次 Requests 层 redirect，第三个 302 触发上限。
    assert request_methods(server) == ["GET", "GET", "GET"]
