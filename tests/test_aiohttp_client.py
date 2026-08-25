"""Behavioral tests for the native aiohttp retry session factory."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Optional, TypeVar, cast

import aiohttp
import pytest
from aiohttp import web
from urllib3.exceptions import InvalidHeader

import resilient_http.aiohttp as aiohttp_client
from resilient_http import Retry, create_retry

# ruff: noqa: RUF002, SIM117

_T = TypeVar("_T")


def run(coroutine: Awaitable[_T]) -> _T:
    """Run one isolated async scenario without requiring a pytest plugin."""

    return asyncio.run(coroutine)


@dataclass(frozen=True)
class ResponseSpec:
    """One response emitted by ScriptedServer."""

    status: int
    body: bytes = b""
    headers: Optional[Mapping[str, str]] = None
    delay: float = 0.0


class ScriptedServer:
    """Small local aiohttp server with deterministic sequential responses."""

    def __init__(self, responses: Collection[ResponseSpec]) -> None:
        self._responses = list(responses)
        self._runner: Optional[web.AppRunner] = None
        self.requests: list[tuple[str, bytes]] = []
        self.url = ""

    async def __aenter__(self) -> ScriptedServer:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/resource"
        return self

    async def __aexit__(self, *args: object) -> None:
        assert self._runner is not None
        await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.requests.append((request.method, body))
        if not self._responses:
            raise AssertionError("scripted server received an unexpected request")
        spec = self._responses.pop(0)
        if spec.delay:
            await asyncio.sleep(spec.delay)
        return web.Response(status=spec.status, body=spec.body, headers=spec.headers)


class TruncatedBodyServer:
    """Raw server that closes after sending fewer bytes than Content-Length."""

    def __init__(self) -> None:
        self._server: Optional[asyncio.AbstractServer] = None
        self.requests = 0
        self.url = ""

    async def __aenter__(self) -> TruncatedBodyServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/resource"
        return self

    async def __aexit__(self, *args: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@dataclass(frozen=True)
class FakeRequest:
    """Request fields consumed by the retry middleware in unit scenarios."""

    method: str
    url: str = "http://example.test/resource"


class FakeResponse:
    """Response fields and lifecycle hooks consumed by the middleware."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: Mapping[str, str] = {}
        self.released = False

    def release(self) -> None:
        self.released = True

    def raise_for_status(self) -> None:
        raise AssertionError("raise_for_status was not expected")


async def invoke_failing_middleware(
    retry: Retry,
    error: aiohttp.ClientError,
    *,
    method: str = "GET",
) -> int:
    """Return handler calls after the middleware re-raises the same error."""

    middleware = aiohttp_client._RetryMiddleware(retry)
    request = cast("aiohttp.ClientRequest", FakeRequest(method))
    calls = 0

    async def handler(_: aiohttp.ClientRequest) -> aiohttp.ClientResponse:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error)) as caught:
        await middleware(request, handler)

    assert caught.value is error
    return calls


def test_module_reexports_the_shared_retry_api() -> None:
    """场景：APP 切换到 aiohttp 模块；预期：继续使用同一个 Retry 类型和 create_retry helper。"""

    assert aiohttp_client.Retry is Retry
    assert aiohttp_client.create_retry is create_retry
    assert aiohttp_client.__all__ == ["Retry", "create_retry", "create_session"]


def test_factory_returns_independent_native_sessions_and_connectors() -> None:
    """场景：连续创建两个 aiohttp Session；预期：均为原生类型且连接池、关闭状态完全独立。"""

    async def scenario() -> None:
        first = aiohttp_client.create_session(create_retry(total=0))
        second = aiohttp_client.create_session(create_retry(total=0))
        try:
            assert type(first) is aiohttp.ClientSession
            assert type(second) is aiohttp.ClientSession
            assert first is not second
            assert first.connector is not second.connector
            assert first._retry_connection is False
            assert second._retry_connection is False

            await first.close()
            assert first.closed
            assert not second.closed
        finally:
            await first.close()
            await second.close()

    run(scenario())


@pytest.mark.parametrize("invalid_retry", [None, 3, object()])
def test_factory_rejects_non_retry_values(invalid_retry: object) -> None:
    """场景：传入非 urllib3 Retry；预期：在创建资源前立即给出清楚的 TypeError。"""

    with pytest.raises(TypeError, match=r"urllib3\.util\.Retry"):
        aiohttp_client.create_session(cast("Any", invalid_retry))


def test_factory_guards_the_private_aiohttp_retry_connection_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """场景：aiohttp 不再声明内部 retry 开关；预期：创建 Session 资源前明确失败。"""

    attrs = aiohttp.ClientSession.ATTRS - {"_retry_connection"}
    monkeypatch.setattr(aiohttp.ClientSession, "ATTRS", attrs)

    with pytest.raises(RuntimeError, match=r"ClientSession\._retry_connection"):
        aiohttp_client.create_session(create_retry(total=0))


def test_timeout_none_disables_aiohttp_native_total_timeout() -> None:
    """场景：省略 factory timeout；预期：与 Requests 一样不设置隐藏的总 timeout。"""

    async def scenario() -> None:
        async with aiohttp_client.create_session(create_retry(total=0)) as session:
            assert session.timeout.total is None
            assert session.timeout.connect is None
            assert session.timeout.sock_connect is None
            assert session.timeout.sock_read is None

    run(scenario())


@pytest.mark.parametrize(
    ("configured", "connect", "read"),
    [
        (2.5, 2.5, 2.5),
        ((1.0, 4.0), 1.0, 4.0),
        ((None, 4.0), None, 4.0),
        ((1.0, None), 1.0, None),
    ],
    ids=["number", "pair", "read-only", "connect-only"],
)
def test_requests_style_timeout_is_converted_to_client_timeout(
    configured: Any,
    connect: Optional[float],
    read: Optional[float],
) -> None:
    """场景：APP 复用 Requests 风格 timeout；预期：映射到每次尝试的 aiohttp connect/read 字段。"""

    async def scenario() -> None:
        async with aiohttp_client.create_session(
            create_retry(total=0),
            timeout=configured,
        ) as session:
            assert session.timeout.total is None
            assert session.timeout.connect == connect
            assert session.timeout.sock_connect == connect
            assert session.timeout.sock_read == read

    run(scenario())


def test_native_client_timeout_is_preserved_for_advanced_use() -> None:
    """场景：APP 需要 aiohttp 总 deadline；预期：原生 ClientTimeout 原样交给 Session。"""

    async def scenario() -> None:
        timeout = aiohttp.ClientTimeout(total=12, connect=2, sock_read=5)
        async with aiohttp_client.create_session(
            create_retry(total=0),
            timeout=timeout,
        ) as session:
            assert session.timeout is timeout

    run(scenario())


def test_request_timeout_can_override_or_disable_the_session_default() -> None:
    """场景：单次请求覆盖 Session timeout；预期：数字覆盖，显式 None 关闭该次限制。"""

    async def scenario() -> None:
        responses = [
            ResponseSpec(200, delay=0.05),
            ResponseSpec(200, delay=0.05),
            ResponseSpec(200, delay=0.05),
        ]
        async with ScriptedServer(responses) as server:
            async with aiohttp_client.create_session(
                create_retry(total=0),
                timeout=0.01,
            ) as session:
                with pytest.raises(aiohttp.ServerTimeoutError):
                    await session.get(server.url)

                async with session.get(server.url, timeout=0.2) as overridden:
                    assert overridden.status == 200

                async with session.get(server.url, timeout=None) as disabled:
                    assert disabled.status == 200

    run(scenario())


@pytest.mark.parametrize("timeout", [(1,), (1, 2, 3), "1"])
def test_invalid_timeout_shapes_are_rejected(timeout: object) -> None:
    """场景：timeout 不是受支持的数字、二元组或 ClientTimeout；预期：立即拒绝。"""

    async def scenario() -> None:
        with pytest.raises(TypeError, match="timeout"):
            aiohttp_client.create_session(
                create_retry(total=0),
                timeout=cast("Any", timeout),
            )

    run(scenario())


@pytest.mark.parametrize(
    "timeout",
    [False, (False, 1), (1, False), ("1", 1), (1, object())],
    ids=["false", "false-connect", "false-read", "string-connect", "object-read"],
)
def test_requests_style_timeout_rejects_non_numeric_values(timeout: object) -> None:
    """场景：Requests 风格 timeout 含 bool 或非数字；预期：立即抛 TypeError。"""

    with pytest.raises(TypeError, match="timeout"):
        aiohttp_client._create_timeout(cast("Any", timeout))


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        (0, 1),
        (-1, 1),
        (float("nan"), 1),
        (float("inf"), 1),
        (1, 0),
        (1, -1),
        (1, float("nan")),
        (1, float("inf")),
    ],
    ids=[
        "zero",
        "negative",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "zero-connect",
        "negative-connect",
        "nan-connect",
        "infinite-connect",
        "zero-read",
        "negative-read",
        "nan-read",
        "infinite-read",
    ],
)
def test_requests_style_timeout_rejects_non_positive_or_non_finite_values(timeout: object) -> None:
    """场景：Requests 风格 timeout 越界；预期：立即抛 ValueError。"""

    with pytest.raises(ValueError, match="finite and greater than 0"):
        aiohttp_client._create_timeout(cast("Any", timeout))


def test_transient_status_is_retried_then_succeeds() -> None:
    """场景：GET 首次 503、随后 200；预期：同一原生 Session 内完成 retry。"""

    async def scenario() -> None:
        async with ScriptedServer([ResponseSpec(503), ResponseSpec(200, b"ok")]) as server:
            retry = create_retry(total=1, status=1, backoff_factor=0)
            async with aiohttp_client.create_session(retry, timeout=(1, 1)) as session:
                async with session.get(server.url) as response:
                    assert response.status == 200
                    assert await response.read() == b"ok"

            assert server.requests == [("GET", b""), ("GET", b"")]
            assert retry.history == ()

    run(scenario())


def test_intermediate_retry_response_is_released() -> None:
    """场景：middleware 隐藏一次 503；预期：只 release 中间响应，最终响应仍交给调用方管理。"""

    async def scenario() -> None:
        middleware = aiohttp_client._RetryMiddleware(create_retry(total=1, status=1, backoff_factor=0))
        request = cast("aiohttp.ClientRequest", FakeRequest("GET"))
        first = FakeResponse(503)
        final = FakeResponse(200)
        responses = [first, final]

        async def handler(_: aiohttp.ClientRequest) -> aiohttp.ClientResponse:
            return cast("aiohttp.ClientResponse", responses.pop(0))

        result = await middleware(request, handler)

        assert result is final
        assert first.released
        assert not final.released

    run(scenario())


def test_exhausted_status_returns_final_response_by_default() -> None:
    """场景：503 持续到预算耗尽；预期：raise_on_status=False 返回最终原生 Response。"""

    async def scenario() -> None:
        async with ScriptedServer([ResponseSpec(503), ResponseSpec(503, b"last")]) as server:
            retry = create_retry(total=1, status=1, backoff_factor=0)
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                async with session.get(server.url) as response:
                    assert response.status == 503
                    assert await response.read() == b"last"

            assert len(server.requests) == 2

    run(scenario())


def test_exhausted_status_raises_native_aiohttp_error_when_configured() -> None:
    """场景：raise_on_status=True 且 503 耗尽；预期：抛 ClientResponseError，不泄漏 urllib3 MaxRetryError。"""

    async def scenario() -> None:
        async with ScriptedServer([ResponseSpec(503), ResponseSpec(503)]) as server:
            retry = create_retry(total=1, status=1, backoff_factor=0, raise_on_status=True)
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                with pytest.raises(aiohttp.ClientResponseError) as caught:
                    await session.get(server.url)

            assert caught.value.status == 503
            assert len(server.requests) == 2

    run(scenario())


def test_exhausted_forced_redirect_status_raises_native_aiohttp_error() -> None:
    """场景：302 被显式配置为可重试状态；预期：耗尽后 release 并抛保留 302 的原生异常。"""

    async def scenario() -> None:
        responses = [
            ResponseSpec(302, headers={"Location": "/first"}),
            ResponseSpec(302, headers={"Location": "/last"}),
        ]
        async with ScriptedServer(responses) as server:
            retry = create_retry(
                total=1,
                status=1,
                status_forcelist=frozenset({302}),
                backoff_factor=0,
                raise_on_status=True,
            )
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                with pytest.raises(aiohttp.ClientResponseError) as caught:
                    await session.get(server.url, allow_redirects=False)

                assert caught.value.status == 302
                assert len(session.connector._acquired) == 0

            assert len(server.requests) == 2

    run(scenario())


def test_default_policy_does_not_retry_post_status() -> None:
    """场景：默认 POST 返回 503；预期：allowed_methods 阻止可能有副作用的状态重放。"""

    async def scenario() -> None:
        async with ScriptedServer([ResponseSpec(503), ResponseSpec(200)]) as server:
            async with aiohttp_client.create_session(create_retry(total=1), timeout=1) as session:
                async with session.post(server.url, data=b"payload") as response:
                    assert response.status == 503

            assert server.requests == [("POST", b"payload")]

    run(scenario())


def test_explicitly_allowed_post_replays_bytes_body() -> None:
    """场景：APP 明确允许 POST 且 body 为 bytes；预期：503 后两次发送相同 payload。"""

    async def scenario() -> None:
        async with ScriptedServer([ResponseSpec(503), ResponseSpec(200)]) as server:
            retry = create_retry(
                total=1,
                status=1,
                allowed_methods=frozenset({"POST"}),
                backoff_factor=0,
            )
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                async with session.post(server.url, data=b"payload") as response:
                    assert response.status == 200

            assert server.requests == [
                ("POST", b"payload"),
                ("POST", b"payload"),
            ]

    run(scenario())


def test_retry_after_can_trigger_retry_without_status_forcelist() -> None:
    """场景：429 只带 Retry-After；预期：复用 urllib3 规则触发一次不阻塞事件循环的 retry。"""

    async def scenario() -> None:
        responses = [
            ResponseSpec(429, headers={"Retry-After": "0"}),
            ResponseSpec(200),
        ]
        async with ScriptedServer(responses) as server:
            retry = create_retry(total=1, status=1, status_forcelist=(), backoff_factor=0)
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                async with session.get(server.url) as response:
                    assert response.status == 200

            assert len(server.requests) == 2

    run(scenario())


def test_invalid_retry_after_releases_response_and_raises_policy_error() -> None:
    """场景：Retry-After 无法解析；预期：沿用 Retry 的 InvalidHeader，且不发第二次请求。"""

    async def scenario() -> None:
        responses = [ResponseSpec(429, headers={"Retry-After": "not-a-date"})]
        async with ScriptedServer(responses) as server:
            retry = create_retry(total=1, status=1, status_forcelist=())
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                with pytest.raises(InvalidHeader):
                    await session.get(server.url)

            assert len(server.requests) == 1

    run(scenario())


def test_connect_retry_ignores_allowed_methods_and_preserves_exception() -> None:
    """场景：POST 在建连阶段超时；预期：connect retry 不受 allowed_methods 限制，耗尽后仍抛原异常。"""

    error = aiohttp.ConnectionTimeoutError("synthetic connect timeout")
    retry = create_retry(
        total=3,
        connect=1,
        allowed_methods=frozenset({"GET"}),
        backoff_factor=0,
    )

    assert run(invoke_failing_middleware(retry, error, method="POST")) == 2


def test_read_retry_respects_allowed_methods() -> None:
    """场景：POST 发出后连接断开；预期：read retry 被 allowed_methods 禁止。"""

    error = aiohttp.ServerDisconnectedError("synthetic disconnect")
    retry = create_retry(
        total=3,
        read=3,
        allowed_methods=frozenset({"GET"}),
        backoff_factor=0,
    )

    assert run(invoke_failing_middleware(retry, error, method="POST")) == 1


def test_read_budget_limits_attempts_and_preserves_original_exception() -> None:
    """场景：GET 持续在响应头前断开；预期：read=2 允许两次 retry，最后抛同一个 aiohttp 异常。"""

    error = aiohttp.ServerDisconnectedError("synthetic disconnect")
    retry = create_retry(total=5, read=2, backoff_factor=0)

    assert run(invoke_failing_middleware(retry, error)) == 3


def test_other_zero_prevents_payload_error_retry() -> None:
    """场景：请求 payload 失败且 other=0；预期：保守默认策略立即返回原生 aiohttp 异常。"""

    error = aiohttp.ClientPayloadError("synthetic payload failure")
    retry = create_retry(total=3, other=0, backoff_factor=0)

    assert run(invoke_failing_middleware(retry, error)) == 1


def test_backoff_uses_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """场景：连续 read errors 产生退避；预期：调用 asyncio.sleep，不阻塞 event loop。"""

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(aiohttp_client.asyncio, "sleep", fake_sleep)
    error = aiohttp.ServerDisconnectedError("synthetic disconnect")
    retry = create_retry(total=2, read=2, backoff_factor=0.5)

    assert run(invoke_failing_middleware(retry, error)) == 3
    assert delays == [1.0]


def test_each_logical_request_has_an_independent_retry_history() -> None:
    """场景：同一 Session 连续发两个逻辑请求；预期：每个请求都获得完整 retry 预算。"""

    async def scenario() -> None:
        responses = [
            ResponseSpec(503),
            ResponseSpec(200),
            ResponseSpec(503),
            ResponseSpec(200),
        ]
        async with ScriptedServer(responses) as server:
            retry = create_retry(total=1, status=1, backoff_factor=0)
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                async with session.get(server.url) as first:
                    assert first.status == 200
                async with session.get(server.url) as second:
                    assert second.status == 200

            assert len(server.requests) == 4
            assert retry.history == ()

    run(scenario())


def test_request_level_middlewares_can_explicitly_bypass_session_retry() -> None:
    """场景：单次请求传 middlewares=()；预期：遵循 aiohttp 原生规则替换 Session middleware。"""

    async def scenario() -> None:
        async with ScriptedServer([ResponseSpec(503), ResponseSpec(200)]) as server:
            retry = create_retry(total=1, status=1, backoff_factor=0)
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                async with session.get(server.url, middlewares=()) as response:
                    assert response.status == 503

            assert len(server.requests) == 1

    run(scenario())


def test_response_body_failure_after_headers_is_not_retried() -> None:
    """场景：响应头已收到但 body 中途截断；预期：读取抛 ClientPayloadError，不透明重放请求。"""

    async def scenario() -> None:
        async with TruncatedBodyServer() as server:
            retry = create_retry(total=3, read=3, backoff_factor=0)
            async with aiohttp_client.create_session(retry, timeout=1) as session:
                async with session.get(server.url) as response:
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await response.read()

            assert server.requests == 1

    run(scenario())
