from __future__ import annotations

import socket
import threading
import unittest
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import connection
from urllib3.exceptions import MaxRetryError
from urllib3.util import Retry

import resilient_http
from resilient_http import create_session


class ScriptedServer:
    """Small real HTTP server that returns statuses in order."""

    def __init__(self, statuses: Sequence[int]) -> None:
        if not statuses:
            raise ValueError("statuses cannot be empty")
        self._statuses = tuple(statuses)
        self._index = 0
        self._lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer
        self._thread: threading.Thread

    def __enter__(self) -> ScriptedServer:
        scripted = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)

                with scripted._lock:
                    scripted.requests.append((self.command, self.path))
                    index = min(scripted._index, len(scripted._statuses) - 1)
                    status = scripted._statuses[index]
                    scripted._index += 1

                payload = str(status).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()

            do_GET = _respond
            do_POST = _respond

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/resource"

    def __exit__(self, *args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def status_retry(
    *,
    total: int,
    raise_on_status: bool,
    allowed_methods: frozenset[str] = frozenset({"GET"}),
) -> Retry:
    return Retry(
        total=total,
        connect=0,
        read=0,
        redirect=0,
        status=total,
        other=0,
        allowed_methods=allowed_methods,
        status_forcelist={503},
        backoff_factor=0,
        raise_on_status=raise_on_status,
    )


class PublicApiTests(unittest.TestCase):
    def test_only_retry_and_create_session_are_public(self) -> None:
        self.assertEqual(resilient_http.__all__, ["Retry", "create_session"])
        self.assertIs(resilient_http.Retry, Retry)
        self.assertIs(resilient_http.create_session, create_session)

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
        for name in old_symbols:
            with self.subTest(name=name):
                self.assertFalse(hasattr(resilient_http, name))


class SessionIsolationTests(unittest.TestCase):
    def test_each_create_session_has_independent_adapters_and_pools(self) -> None:
        retry = Retry(total=1)
        first = create_session(retry)
        second = create_session(retry)
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        self.assertIsNot(first, second)
        for url in ("http://example.test", "https://example.test"):
            with self.subTest(url=url):
                first_adapter = first.get_adapter(url)
                second_adapter = second.get_adapter(url)
                self.assertIsInstance(first_adapter, HTTPAdapter)
                self.assertIsInstance(second_adapter, HTTPAdapter)
                self.assertIsNot(first_adapter, second_adapter)
                self.assertIsNot(first_adapter.poolmanager, second_adapter.poolmanager)
                self.assertIs(first_adapter.max_retries, retry)
                self.assertIs(second_adapter.max_retries, retry)

    def test_closing_one_session_does_not_affect_another(self) -> None:
        first = create_session(Retry(total=0))
        second = create_session(Retry(total=0))
        self.addCleanup(second.close)

        first.close()
        with ScriptedServer([200]) as server:
            response = second.get(server.url, timeout=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"200")


class RetryIntegrationTests(unittest.TestCase):
    def test_get_retries_503_then_returns_200(self) -> None:
        with (
            ScriptedServer([503, 200]) as server,
            create_session(status_retry(total=1, raise_on_status=True)) as session,
        ):
            response = session.get(server.url, timeout=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.requests, [("GET", "/resource"), ("GET", "/resource")])

    def test_retry_history_is_independent_for_each_logical_request(self) -> None:
        retry = status_retry(total=1, raise_on_status=True)
        with ScriptedServer([503, 200, 503, 200]) as server, create_session(retry) as session:
            first = session.get(server.url, timeout=1)
            second = session.get(server.url, timeout=1)

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(retry.history, ())

    def test_exhausted_status_retry_returns_final_response_when_not_raising(self) -> None:
        with (
            ScriptedServer([503]) as server,
            create_session(status_retry(total=2, raise_on_status=False)) as session,
        ):
            response = session.get(server.url, timeout=1)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual({method for method, _ in server.requests}, {"GET"})

    def test_post_is_not_retried_for_status_by_default(self) -> None:
        retry = Retry(
            total=2,
            connect=0,
            read=0,
            redirect=0,
            status=2,
            other=0,
            status_forcelist={503},
            backoff_factor=0,
            raise_on_status=False,
        )
        with ScriptedServer([503, 200]) as server, create_session(retry) as session:
            response = session.post(server.url, data=b"payload", timeout=1)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(server.requests, [("POST", "/resource")])

    def test_connect_retry_exhaustion_raises_native_connection_error(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            host, port = reservation.getsockname()

            retry = Retry(
                total=1,
                connect=1,
                read=0,
                redirect=0,
                status=0,
                other=0,
                backoff_factor=0,
            )
            with (
                create_session(retry) as session,
                self.assertRaises(requests.exceptions.ConnectionError) as caught,
            ):
                session.get(f"http://{host}:{port}/refused", timeout=0.2)

        self.assertTrue(any(isinstance(value, MaxRetryError) for value in caught.exception.args))

    def test_first_connect_failure_is_retried_and_second_connect_succeeds(self) -> None:
        real_create_connection = connection.create_connection
        call_count = 0

        def fail_once_then_connect(*args: Any, **kwargs: Any) -> socket.socket:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionRefusedError("synthetic first connection failure")
            return real_create_connection(*args, **kwargs)

        retry = Retry(
            total=1,
            connect=1,
            read=0,
            redirect=0,
            status=0,
            other=0,
            backoff_factor=0,
        )
        with (
            ScriptedServer([200]) as server,
            create_session(retry) as session,
            patch(
                "urllib3.connection.connection.create_connection",
                side_effect=fail_once_then_connect,
            ),
        ):
            response = session.get(server.url, timeout=1)

        self.assertEqual(call_count, 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.requests, [("GET", "/resource")])


if __name__ == "__main__":
    unittest.main()
