from __future__ import annotations

import io
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, get_type_hints

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

import resilient_http
from resilient_http import (
    BusinessHttpError,
    ErrorMappingPolicy,
    ErrorMappingRule,
    HttpClient,
    HttpClientConfig,
    NonReplayableRequestError,
    PoolConfig,
    RetryConfig,
    SystemHttpError,
    TimeoutConfig,
    create_session,
    retry_from_dict,
)


class InventoryUnavailable(SystemHttpError):
    pass


class PermissionDenied(BusinessHttpError):
    pass


ResponseSpec = tuple[int, dict[str, str], bytes]


class ScriptedServer:
    def __init__(self, responses: list[ResponseSpec]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self._index = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer
        self._thread: threading.Thread

    def __enter__(self) -> ScriptedServer:
        scripted = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                with scripted._lock:
                    scripted.requests.append(
                        (
                            self.command,
                            self.path,
                            body,
                            dict(self.headers.items()),
                        )
                    )
                    index = min(scripted._index, len(scripted.responses) - 1)
                    status, headers, payload = scripted.responses[index]
                    scripted._index += 1

                self.send_response(status)
                normalized_headers = {key.lower() for key in headers}
                for key, value in headers.items():
                    self.send_header(key, value)
                if "content-length" not in normalized_headers:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
                    self.wfile.flush()
                if headers.get("Connection", "").lower() == "close":
                    self.close_connection = True

            do_GET = _respond
            do_POST = _respond
            do_PUT = _respond
            do_PATCH = _respond
            do_DELETE = _respond
            do_HEAD = _respond

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, *args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class RecordingSession(requests.Session):
    def __init__(self, statuses: tuple[int, ...] = (200,)) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.statuses = iter(statuses)
        self.was_closed = False

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        self.calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = next(self.statuses)
        response.url = url
        response.request = requests.Request(method, url).prepare()
        response._content = b"ok"
        return response

    def close(self) -> None:
        self.was_closed = True
        super().close()


def retry_config(
    total: int,
    *,
    allowed_methods: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"}),
    status_forcelist: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
    connect: int | None = None,
    read: int | None = None,
    status: int | None = None,
) -> RetryConfig:
    return RetryConfig(
        total=total,
        connect=connect,
        read=read,
        status=status,
        other=0,
        allowed_methods=allowed_methods,
        status_forcelist=status_forcelist,
        backoff_factor=0,
        backoff_max=1,
        backoff_jitter=0,
        retry_after_max=1,
    )


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_safe_and_disable_retries(self) -> None:
        config = HttpClientConfig()

        self.assertEqual(config.timeout, TimeoutConfig(connect=10, read=10))
        self.assertEqual(config.pool, PoolConfig())
        self.assertEqual(config.retry.total, 0)
        self.assertEqual(
            config.retry.allowed_methods,
            frozenset({"GET", "HEAD", "OPTIONS"}),
        )
        self.assertEqual(
            config.retry.status_forcelist,
            frozenset({429, 500, 502, 503, 504}),
        )
        self.assertEqual(config.retry.other, 0)
        self.assertFalse(config.retry.raise_on_status)
        self.assertFalse(config.retry.raise_on_redirect)

    def test_dict_builds_requests_urllib3_and_pool_configuration(self) -> None:
        config = HttpClientConfig.from_dict(
            {
                "base_url": "https://api.example.com/v1",
                "timeout": {"connect": 2, "read": 7},
                "headers": {"X-App": "inventory"},
                "follow_redirects": True,
                "max_redirects": 4,
                "verify": "/tmp/ca.pem",
                "trust_env": False,
                "pool": {
                    "connections": 8,
                    "maxsize": 32,
                    "block": True,
                },
                "retry": {
                    "total": 4,
                    "connect": 2,
                    "read": 1,
                    "status": 3,
                    "other": 0,
                    "allowed_methods": ["get", "post"],
                    "status_forcelist": [429, 503],
                    "backoff_factor": 0.25,
                    "backoff_max": 9,
                    "backoff_jitter": 0.75,
                    "respect_retry_after_header": False,
                    "retry_after_max": 15,
                },
            }
        )

        self.assertEqual(config.base_url, "https://api.example.com/v1")
        self.assertEqual(config.timeout, TimeoutConfig(connect=2, read=7))
        self.assertEqual(
            config.pool,
            PoolConfig(connections=8, maxsize=32, block=True),
        )
        self.assertEqual(config.retry.total, 4)
        self.assertEqual(config.retry.connect, 2)
        self.assertEqual(config.retry.read, 1)
        self.assertEqual(config.retry.status, 3)
        self.assertEqual(config.retry.allowed_methods, frozenset({"GET", "POST"}))
        self.assertEqual(config.retry.status_forcelist, frozenset({429, 503}))
        self.assertEqual(config.retry.backoff_factor, 0.25)
        self.assertEqual(config.retry.backoff_max, 9)
        self.assertEqual(config.retry.backoff_jitter, 0.75)
        self.assertFalse(config.retry.respect_retry_after_header)
        self.assertEqual(config.retry.retry_after_max, 15)
        self.assertFalse(config.retry.raise_on_status)

    def test_direct_urllib3_retry_is_normalized_for_terminal_mapping(self) -> None:
        retry = Retry(
            total=2,
            allowed_methods={"GET"},
            status_forcelist={503},
            raise_on_status=True,
            raise_on_redirect=True,
        )
        config = HttpClientConfig(retry=retry)

        self.assertIsNot(config.retry, retry)
        self.assertFalse(config.retry.raise_on_status)
        self.assertFalse(config.retry.raise_on_redirect)
        self.assertEqual(config.retry.redirect, 0)

    def test_retry_from_dict_uses_organization_defaults(self) -> None:
        retry = retry_from_dict({"total": 2})

        self.assertIsInstance(retry, Retry)
        self.assertEqual(retry.total, 2)
        self.assertEqual(retry.allowed_methods, frozenset({"GET", "HEAD", "OPTIONS"}))
        self.assertIn(500, retry.status_forcelist)
        self.assertEqual(retry.retry_after_max, 60)

    def test_empty_status_list_is_allowed_but_empty_methods_are_rejected(self) -> None:
        retry = retry_from_dict({"total": 1, "status_forcelist": []})
        self.assertEqual(retry.status_forcelist, frozenset())

        with self.assertRaisesRegex(ValueError, "allowed_methods cannot be empty"):
            retry_from_dict({"total": 1, "allowed_methods": []})

    def test_raw_retry_cannot_enable_every_method_implicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty collection"):
            HttpClientConfig(retry=Retry(total=1, allowed_methods=None))

    def test_custom_retry_subclasses_are_rejected_instead_of_silently_changed(self) -> None:
        class CustomRetry(Retry):
            pass

        with self.assertRaisesRegex(TypeError, "subclasses are not supported"):
            HttpClientConfig(retry=CustomRetry(total=1))

    def test_configuration_is_strict(self) -> None:
        invalid_values = (
            {"unknown": True},
            {"retry": {"total": True}},
            {"retry": {"backoff_factor": float("nan")}},
            {"retry": {"allowed_methods": "GET"}},
            {"timeout": 0},
            {"pool": {"maxsize": 0}},
            {"base_url": "api.example.com"},
            {"base_url": "https://user:secret@example.com"},
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                HttpClientConfig.from_dict(value)

    def test_error_mapping_uses_requests_exception_names(self) -> None:
        config = HttpClientConfig.from_dict(
            {
                "error_mapping": {
                    "rules": [
                        {
                            "name": "connect-timeout",
                            "exceptions": ["ConnectTimeout"],
                            "raise_as": "system",
                        }
                    ]
                }
            }
        )

        rule = config.error_mapping.rules[0]
        self.assertEqual(rule.exception_types, (requests.ConnectTimeout,))

    def test_public_type_hints_resolve_on_python_39(self) -> None:
        for target in (
            TimeoutConfig,
            PoolConfig,
            RetryConfig,
            HttpClientConfig,
            HttpClient.request,
        ):
            with self.subTest(target=target):
                self.assertTrue(get_type_hints(target))

    def test_httpx_async_and_vendor_api_are_no_longer_public(self) -> None:
        self.assertFalse(hasattr(resilient_http, "AsyncHttpClient"))
        self.assertNotIn("AsyncHttpClient", resilient_http.__all__)
        self.assertNotIn("_vendor", resilient_http.__all__)


class SessionTests(unittest.TestCase):
    def test_create_session_mounts_retry_adapters_for_both_schemes(self) -> None:
        config = HttpClientConfig(
            retry=retry_config(2),
            pool=PoolConfig(connections=3, maxsize=9, block=True),
        )
        session = create_session(config)
        self.addCleanup(session.close)

        for url in ("http://example.com", "https://example.com"):
            with self.subTest(url=url):
                adapter = session.get_adapter(url)
                self.assertIsInstance(adapter, HTTPAdapter)
                self.assertIsInstance(adapter.max_retries, Retry)
                self.assertEqual(adapter.max_retries.total, 2)
                self.assertEqual(adapter._pool_connections, 3)
                self.assertEqual(adapter._pool_maxsize, 9)
                self.assertTrue(adapter._pool_block)

    def test_create_session_applies_headers_tls_environment_and_redirect_limit(self) -> None:
        session = create_session(
            HttpClientConfig(
                headers={"X-App": "orders"},
                verify="/tmp/ca.pem",
                trust_env=False,
                max_redirects=5,
            )
        )
        self.addCleanup(session.close)

        self.assertEqual(session.headers["X-App"], "orders")
        self.assertEqual(session.verify, "/tmp/ca.pem")
        self.assertFalse(session.trust_env)
        self.assertEqual(session.max_redirects, 5)

    def test_default_timeout_redirect_and_base_url_are_applied_per_request(self) -> None:
        session = RecordingSession()
        client = HttpClient(
            HttpClientConfig(
                base_url="https://api.example.com/v1",
                timeout={"connect": 2, "read": 8},
                follow_redirects=False,
            ),
            session_factory=lambda: session,
        )
        self.addCleanup(client.close)

        response = client.get("/users?active=true")

        self.assertEqual(response.status_code, 200)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://api.example.com/v1/users?active=true")
        self.assertEqual(kwargs["timeout"], (2.0, 8.0))
        self.assertFalse(kwargs["allow_redirects"])

    def test_per_request_timeout_and_redirect_override_are_preserved(self) -> None:
        session = RecordingSession()
        client = HttpClient(
            HttpClientConfig(),
            session_factory=lambda: session,
        )
        self.addCleanup(client.close)

        client.get(
            "https://example.com",
            timeout=(1, 2),
            allow_redirects=True,
        )

        kwargs = session.calls[0][2]
        self.assertEqual(kwargs["timeout"], (1, 2))
        self.assertTrue(kwargs["allow_redirects"])

    def test_raw_session_and_compatibility_alias_reference_same_session(self) -> None:
        session = RecordingSession()
        client = HttpClient(HttpClientConfig(), session_factory=lambda: session)
        self.addCleanup(client.close)

        self.assertIs(client.raw_session, session)
        self.assertIs(client.raw_client, session)

    def test_context_manager_closes_owned_session_and_close_is_idempotent(self) -> None:
        session = RecordingSession()
        with HttpClient(
            HttpClientConfig(),
            session_factory=lambda: session,
        ) as client:
            self.assertFalse(session.was_closed)

        self.assertTrue(session.was_closed)
        client.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            client.get("https://example.com")


class HttpClientIntegrationTests(unittest.TestCase):
    def test_status_retry_succeeds_after_two_failures(self) -> None:
        with (
            ScriptedServer(
                [
                    (503, {}, b"unavailable"),
                    (503, {}, b"unavailable"),
                    (200, {}, b"ok"),
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(2),
                )
            ) as client,
        ):
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(server.requests), 3)

    def test_retry_exhaustion_maps_final_503_with_exact_attempts(self) -> None:
        with (
            ScriptedServer([(503, {}, b"unavailable")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(2),
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/health")

        self.assertEqual(len(server.requests), 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception.response, requests.Response)

    def test_final_non_retryable_status_is_reclassified_without_false_exhaustion(
        self,
    ) -> None:
        with (
            ScriptedServer(
                [
                    (503, {}, b"unavailable"),
                    (400, {}, b"invalid"),
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(2),
                )
            ) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/orders")

        self.assertEqual(len(server.requests), 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retry_exhausted)

    def test_post_status_is_not_retried_by_default(self) -> None:
        with (
            ScriptedServer([(503, {}, b"unavailable")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(2),
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.post("/orders", json={"sku": "A"})

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retry_exhausted)

    def test_post_can_be_explicitly_retried_with_replayable_body(self) -> None:
        with (
            ScriptedServer(
                [
                    (503, {}, b"unavailable"),
                    (200, {}, b"ok"),
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(
                        1,
                        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS", "POST"}),
                    ),
                )
            ) as client,
        ):
            response = client.post("/orders", data=io.BytesIO(b"payload"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual([item[2] for item in server.requests], [b"payload", b"payload"])

    def test_one_shot_body_is_rejected_before_any_send(self) -> None:
        with (
            ScriptedServer([(200, {}, b"ok")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(1),
                )
            ) as client,
            self.assertRaises(NonReplayableRequestError) as caught,
        ):
            client.post("/upload", data=(part for part in (b"a", b"b")))

        self.assertEqual(server.requests, [])
        self.assertEqual(caught.exception.attempts, 0)

    def test_one_shot_body_is_allowed_when_retries_are_disabled(self) -> None:
        session = RecordingSession()
        client = HttpClient(
            HttpClientConfig(retry=retry_config(0)),
            session_factory=lambda: session,
        )
        self.addCleanup(client.close)

        response = client.post(
            "https://example.com/upload",
            data=(part for part in (b"a", b"b")),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 1)

    def test_one_shot_body_is_rejected_when_redirects_can_replay_it(self) -> None:
        with (
            ScriptedServer([(307, {"Location": "/target"}, b"")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    follow_redirects=True,
                    retry=retry_config(0),
                )
            ) as client,
            self.assertRaises(NonReplayableRequestError) as caught,
        ):
            client.put("/upload", data=(part for part in (b"a", b"b")))

        self.assertEqual(server.requests, [])
        self.assertEqual(caught.exception.attempts, 0)

    def test_retry_after_header_can_trigger_retry_without_status_forcelist(
        self,
    ) -> None:
        with (
            ScriptedServer(
                [
                    (429, {"Retry-After": "0"}, b"slow down"),
                    (200, {}, b"ok"),
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(
                        1,
                        status=1,
                        status_forcelist=frozenset(),
                    ),
                )
            ) as client,
        ):
            response = client.get("/quota")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(server.requests), 2)

    def test_invalid_retry_after_does_not_count_an_unsent_attempt(self) -> None:
        with (
            ScriptedServer([(429, {"Retry-After": "not-a-date"}, b"slow down")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(
                        1,
                        status=1,
                        status_forcelist=frozenset(),
                    ),
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/quota")

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertIsInstance(caught.exception.cause, requests.exceptions.InvalidHeader)

    def test_mapping_disabled_returns_final_error_response(self) -> None:
        with (
            ScriptedServer([(503, {}, b"unavailable")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(1),
                    enable_error_mapping=False,
                )
            ) as client,
        ):
            response = client.get("/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(server.requests), 2)

    def test_custom_status_mapping_rule_wins(self) -> None:
        policy = ErrorMappingPolicy(
            rules=(
                ErrorMappingRule(
                    name="permission-denied",
                    status_codes=frozenset({403}),
                    raise_as=PermissionDenied,
                ),
            )
        )
        with (
            ScriptedServer([(403, {}, b"denied")]) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    error_mapping=policy,
                )
            ) as client,
            self.assertRaises(PermissionDenied) as caught,
        ):
            client.get("/admin")

        self.assertEqual(caught.exception.rule_name, "permission-denied")
        self.assertEqual(caught.exception.status_code, 403)

    def test_connect_exhaustion_maps_system_error_with_exact_attempts(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        with (
            HttpClient(
                HttpClientConfig(
                    base_url=f"http://127.0.0.1:{port}",
                    timeout=0.2,
                    trust_env=False,
                    retry=retry_config(
                        1,
                        connect=1,
                        read=0,
                        status=0,
                        status_forcelist=frozenset(),
                    ),
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/offline")

        self.assertEqual(caught.exception.attempts, 2)
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception.cause, requests.ConnectionError)

    def test_mapping_disabled_propagates_requests_exception(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        with (
            HttpClient(
                HttpClientConfig(
                    base_url=f"http://127.0.0.1:{port}",
                    timeout=0.2,
                    trust_env=False,
                    retry=retry_config(0),
                    enable_error_mapping=False,
                )
            ) as client,
            self.assertRaises(requests.ConnectionError),
        ):
            client.get("/offline")

    def test_invalid_absolute_url_cannot_escape_configured_base_url(self) -> None:
        with (
            HttpClient(
                HttpClientConfig(
                    base_url="https://api.example.com",
                    trust_env=False,
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("https://other.example.com/secrets")

        self.assertEqual(caught.exception.attempts, 0)
        self.assertIsInstance(caught.exception.cause, requests.exceptions.InvalidURL)

    def test_error_url_removes_credentials_query_and_fragment(self) -> None:
        with ScriptedServer([(400, {}, b"invalid")]) as server:
            address = server.base_url.removeprefix("http://")
            url = f"http://user:secret@{address}/orders?token=secret#fragment"
            with (
                HttpClient(
                    HttpClientConfig(trust_env=False),
                ) as client,
                self.assertRaises(BusinessHttpError) as caught,
            ):
                client.get(url)

        self.assertNotIn("user", caught.exception.url)
        self.assertNotIn("secret", caught.exception.url)
        self.assertNotIn("token", caught.exception.url)
        self.assertNotIn("fragment", caught.exception.url)
        self.assertTrue(caught.exception.url.endswith("/orders"))

    def test_redirect_sends_are_included_in_attempt_count(self) -> None:
        with (
            ScriptedServer(
                [
                    (503, {}, b"unavailable"),
                    (302, {"Location": "/final"}, b""),
                    (400, {}, b"invalid"),
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    follow_redirects=True,
                    retry=retry_config(1),
                )
            ) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/start")

        self.assertEqual(len(server.requests), 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertFalse(caught.exception.retry_exhausted)

    def test_cross_origin_redirect_is_blocked_before_forwarding_headers(self) -> None:
        with (
            ScriptedServer([(200, {}, b"target")]) as target,
            ScriptedServer([(302, {"Location": f"{target.base_url}/target"}, b"")]) as origin,
            HttpClient(
                HttpClientConfig(
                    base_url=origin.base_url,
                    headers={"X-Api-Key": "secret"},
                    trust_env=False,
                    follow_redirects=True,
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/start")

        self.assertEqual(len(origin.requests), 1)
        self.assertEqual(origin.requests[0][3]["X-Api-Key"], "secret")
        self.assertEqual(target.requests, [])
        self.assertEqual(caught.exception.attempts, 1)
        self.assertIsInstance(caught.exception.cause, requests.exceptions.InvalidURL)

    def test_follow_redirects_false_returns_redirect_response(self) -> None:
        with (
            ScriptedServer(
                [
                    (302, {"Location": "/final"}, b""),
                    (200, {}, b"ok"),
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    follow_redirects=False,
                )
            ) as client,
        ):
            response = client.get("/start")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(server.requests), 1)

    def test_body_read_failure_after_headers_is_not_retried(self) -> None:
        with (
            ScriptedServer(
                [
                    (
                        200,
                        {
                            "Content-Length": "20",
                            "Connection": "close",
                        },
                        b"short",
                    )
                ]
            ) as server,
            HttpClient(
                HttpClientConfig(
                    base_url=server.base_url,
                    trust_env=False,
                    retry=retry_config(2, read=2),
                )
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/truncated")

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retry_exhausted)
        self.assertIsInstance(
            caught.exception.cause,
            requests.exceptions.ChunkedEncodingError,
        )


if __name__ == "__main__":
    unittest.main()
