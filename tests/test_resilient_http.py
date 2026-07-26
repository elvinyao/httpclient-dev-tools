from __future__ import annotations

import unittest
from typing import get_type_hints
from unittest.mock import patch

import httpx
from httpx_retries import Retry, RetryTransport

import resilient_http
from resilient_http import (
    AsyncHttpClient,
    BaseHttpError,
    BusinessHttpError,
    ErrorMappingPolicy,
    ErrorMappingRule,
    HttpClient,
    HttpClientConfig,
    NonReplayableRequestError,
    SystemHttpError,
)


class UpstreamUnavailable(SystemHttpError):
    pass


def retry_for(
    *,
    total: int,
    allowed_methods: tuple[str, ...] = ("GET",),
    status_forcelist: tuple[int, ...] = (503,),
    retry_on_exceptions: tuple[type[Exception], ...] = (httpx.ConnectTimeout,),
) -> Retry:
    return Retry(
        total=total,
        allowed_methods=allowed_methods,
        status_forcelist=status_forcelist,
        retry_on_exceptions=retry_on_exceptions,
        backoff_factor=0,
        backoff_jitter=0,
    )


def client_config(
    *,
    retry: Retry | None = None,
    enable_error_mapping: bool = True,
    error_mapping: ErrorMappingPolicy | None = None,
) -> HttpClientConfig:
    return HttpClientConfig(
        base_url="https://example.test",
        retry=retry if retry is not None else Retry(total=0),
        enable_error_mapping=enable_error_mapping,
        error_mapping=error_mapping if error_mapping is not None else ErrorMappingPolicy(),
    )


class BodyReadTimeoutStream(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self):
        raise httpx.ReadTimeout("body read timed out", request=self.request)
        yield b""


class AsyncBodyReadTimeoutStream(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    async def __aiter__(self):
        raise httpx.ReadTimeout("body read timed out", request=self.request)
        yield b""


class HttpClientTests(unittest.TestCase):
    def test_retry_transport_retries_status_and_then_succeeds(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503 if calls < 3 else 200, json={"calls": calls})

        with HttpClient(
            client_config(retry=retry_for(total=2)),
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertIsInstance(client.raw_client._transport, RetryTransport)
            response = client.get("/unstable")

        self.assertEqual(calls, 3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"calls": 3})

    def test_retry_exhaustion_is_mapped_after_all_transport_attempts(self) -> None:
        calls = 0
        mapping = ErrorMappingPolicy(
            rules=(
                ErrorMappingRule(
                    name="inventory-unavailable",
                    status_codes=frozenset({503}),
                    raise_as=UpstreamUnavailable,
                ),
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        with (
            HttpClient(
                client_config(
                    retry=retry_for(total=2),
                    error_mapping=mapping,
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(UpstreamUnavailable) as caught,
        ):
            client.get("/inventory")

        self.assertEqual(calls, 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.rule_name, "inventory-unavailable")
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception, BaseHttpError)

    def test_caller_extensions_cannot_forge_attempt_count(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        with (
            HttpClient(
                client_config(retry=retry_for(total=1)),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get(
                "/",
                extensions={
                    "resilient_http.attempts": 999,
                    "resilient_http.request_state": object(),
                },
            )

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)

    def test_transport_exception_is_retried_and_mapped_with_cause(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("connect timed out", request=request)

        with (
            HttpClient(
                client_config(retry=retry_for(total=2)),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/slow")

        self.assertEqual(calls, 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception.cause, httpx.ConnectTimeout)
        self.assertIs(caught.exception.__cause__, caught.exception.cause)

    def test_transport_exception_uses_custom_error_mapping_rule(self) -> None:
        calls = 0
        mapping = ErrorMappingPolicy(
            rules=(
                ErrorMappingRule(
                    name="inventory-timeout",
                    exception_types=(httpx.ConnectTimeout,),
                    raise_as=UpstreamUnavailable,
                ),
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("connect timed out", request=request)

        with (
            HttpClient(
                client_config(
                    retry=retry_for(total=1),
                    error_mapping=mapping,
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(UpstreamUnavailable) as caught,
        ):
            client.get("/inventory")

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.rule_name, "inventory-timeout")
        self.assertIsInstance(caught.exception.cause, httpx.ConnectTimeout)

    def test_final_condition_is_reclassified_without_false_exhaustion(self) -> None:
        statuses = iter((503, 400))
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(next(statuses))

        with (
            HttpClient(
                client_config(retry=retry_for(total=3)),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/")

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retry_exhausted)

    def test_non_retryable_post_is_sent_once(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        with (
            HttpClient(
                client_config(retry=retry_for(total=3, allowed_methods=("GET",))),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.post("/orders", json={"amount": 100})

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retry_exhausted)

    def test_post_is_retried_when_upstream_retry_allows_it(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        with (
            HttpClient(
                client_config(
                    retry=retry_for(
                        total=1,
                        allowed_methods=("POST",),
                    )
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.post("/orders", json={"amount": 100})

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertTrue(caught.exception.retry_exhausted)

    @patch("httpx_retries.retry.time.sleep")
    def test_retry_after_wait_is_delegated_to_httpx_retries(
        self,
        upstream_sleep,
    ) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                503,
                headers={"Retry-After": "7"},
            )

        with HttpClient(
            client_config(
                retry=retry_for(total=1),
                enable_error_mapping=False,
            ),
            transport=httpx.MockTransport(handler),
        ) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, 2)
        upstream_sleep.assert_called_once_with(7.0)

    def test_mapping_disabled_returns_final_error_response(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": "unavailable"})

        with HttpClient(
            client_config(
                retry=retry_for(total=2),
                enable_error_mapping=False,
            ),
            transport=httpx.MockTransport(handler),
        ) as client:
            response = client.get("/")

        self.assertEqual(calls, 3)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "unavailable"})

    def test_mapping_disabled_propagates_last_httpx_exception_unchanged(self) -> None:
        calls = 0
        raised_errors: list[httpx.ConnectTimeout] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            error = httpx.ConnectTimeout("connect timed out", request=request)
            raised_errors.append(error)
            raise error

        with (
            HttpClient(
                client_config(
                    retry=retry_for(total=1),
                    enable_error_mapping=False,
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(httpx.ConnectTimeout) as caught,
        ):
            client.get("/")

        self.assertEqual(calls, 2)
        self.assertIs(caught.exception, raised_errors[-1])
        self.assertNotIsInstance(caught.exception, BaseHttpError)

    def test_invalid_url_respects_error_mapping_switch(self) -> None:
        invalid_url = "https://example.test:invalid/"

        with (
            HttpClient(client_config()) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get(invalid_url)

        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.url, "<invalid-url>")
        self.assertIsInstance(caught.exception.cause, httpx.InvalidURL)

        with (
            HttpClient(
                client_config(enable_error_mapping=False),
            ) as client,
            self.assertRaises(httpx.InvalidURL),
        ):
            client.get(invalid_url)

    def test_default_mapping_sanitizes_business_error_metadata(self) -> None:
        config = HttpClientConfig(
            base_url="https://user:pass@example.test",
            retry=Retry(total=0),
        )

        with (
            HttpClient(
                config,
                transport=httpx.MockTransport(lambda request: httpx.Response(403)),
            ) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/private?token=secret")

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.url, "https://example.test/private")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("pass", str(caught.exception))

    def test_one_shot_body_is_rejected_before_retry_transport_sends_it(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        def body():
            yield b"important-data"

        with (
            HttpClient(
                client_config(
                    retry=retry_for(
                        total=1,
                        allowed_methods=("PUT",),
                    )
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(NonReplayableRequestError) as caught,
        ):
            client.put("/objects/1", content=body())

        self.assertEqual(calls, 0)
        self.assertEqual(caught.exception.attempts, 0)

    def test_one_shot_body_is_allowed_when_no_retry_condition_exists(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        def body():
            yield b"sent-once"

        config = HttpClientConfig.from_dict(
            {
                "base_url": "https://example.test",
                "retry": {
                    "total": 2,
                    "allowed_methods": ["PUT"],
                    "status_forcelist": [],
                    "retry_on_exceptions": [],
                },
            }
        )
        with HttpClient(
            config,
            transport=httpx.MockTransport(handler),
        ) as client:
            response = client.put("/objects/1", content=body())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 1)

    def test_replayable_body_is_identical_on_each_transport_attempt(self) -> None:
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            return httpx.Response(503)

        with (
            HttpClient(
                client_config(
                    retry=retry_for(
                        total=1,
                        allowed_methods=("PUT",),
                    )
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.put("/objects/1", content=b"important-data")

        self.assertEqual(bodies, [b"important-data", b"important-data"])

    def test_body_phase_timeout_is_mapped_but_not_retried_outside_transport(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=BodyReadTimeoutStream(request))

        with (
            HttpClient(
                client_config(
                    retry=retry_for(
                        total=3,
                        retry_on_exceptions=(httpx.ReadTimeout,),
                    )
                ),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/stream")

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception.cause, httpx.ReadTimeout)

    def test_redirect_body_timeout_does_not_claim_retry_exhaustion(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"Location": "/final"},
                )
            return httpx.Response(
                200,
                stream=BodyReadTimeoutStream(request),
            )

        config = HttpClientConfig(
            base_url="https://example.test",
            follow_redirects=True,
            retry=retry_for(
                total=1,
                retry_on_exceptions=(httpx.ReadTimeout,),
            ),
        )
        with (
            HttpClient(
                config,
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/start")

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertFalse(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception.cause, httpx.ReadTimeout)

    def test_unexpected_runtime_error_is_not_mapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("programming bug")

        with (
            HttpClient(
                client_config(retry=retry_for(total=2)),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaisesRegex(RuntimeError, "programming bug"),
        ):
            client.get("/")

    def test_prebuilt_retry_transport_is_rejected(self) -> None:
        nested = RetryTransport(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            retry=retry_for(total=1),
        )
        try:
            with self.assertRaises(ValueError):
                HttpClient(client_config(), transport=nested)
        finally:
            nested.close()

    def test_context_manager_closes_underlying_client(self) -> None:
        with HttpClient(
            client_config(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        ) as client:
            self.assertFalse(client.raw_client.is_closed)
            self.assertEqual(client.get("/").status_code, 200)

        self.assertTrue(client.raw_client.is_closed)


class AsyncHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_retry_transport_retries_and_context_closes(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503 if calls < 3 else 200)

        async with AsyncHttpClient(
            client_config(retry=retry_for(total=2)),
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertIsInstance(client.raw_client._transport, RetryTransport)
            self.assertFalse(client.raw_client.is_closed)
            response = await client.get("/")

        self.assertEqual(calls, 3)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(client.raw_client.is_closed)

    async def test_async_exhaustion_is_mapped_with_attempts(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        async with AsyncHttpClient(
            client_config(retry=retry_for(total=1)),
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(SystemHttpError) as caught:
                await client.get("/")

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertTrue(caught.exception.retry_exhausted)

    async def test_async_mapping_disabled_propagates_raw_exception(self) -> None:
        calls = 0
        raised_errors: list[httpx.ConnectTimeout] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            error = httpx.ConnectTimeout("connect timed out", request=request)
            raised_errors.append(error)
            raise error

        async with AsyncHttpClient(
            client_config(
                retry=retry_for(total=1),
                enable_error_mapping=False,
            ),
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(httpx.ConnectTimeout) as caught:
                await client.get("/")

        self.assertEqual(calls, 2)
        self.assertIs(caught.exception, raised_errors[-1])

    async def test_async_one_shot_body_is_rejected_before_send(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        async def body():
            yield b"important-data"

        async with AsyncHttpClient(
            client_config(
                retry=retry_for(
                    total=1,
                    allowed_methods=("PUT",),
                )
            ),
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(NonReplayableRequestError) as caught:
                await client.put("/objects/1", content=body())

        self.assertEqual(calls, 0)
        self.assertEqual(caught.exception.attempts, 0)

    async def test_async_body_phase_timeout_is_not_retried(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                stream=AsyncBodyReadTimeoutStream(request),
            )

        async with AsyncHttpClient(
            client_config(
                retry=retry_for(
                    total=3,
                    retry_on_exceptions=(httpx.ReadTimeout,),
                )
            ),
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(SystemHttpError) as caught:
                await client.get("/stream")

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception.cause, httpx.ReadTimeout)

    async def test_async_prebuilt_retry_transport_is_rejected(self) -> None:
        nested = RetryTransport(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            retry=retry_for(total=1),
        )
        try:
            with self.assertRaises(ValueError):
                AsyncHttpClient(client_config(), transport=nested)
        finally:
            await nested.aclose()


class ConfigurationTests(unittest.TestCase):
    def test_dict_builds_upstream_retry_and_error_mapping_strictly(self) -> None:
        config = HttpClientConfig.from_dict(
            {
                "base_url": "https://example.test",
                "timeout": {"connect": 2, "read": 5},
                "follow_redirects": True,
                "enable_error_mapping": False,
                "retry": {
                    "total": 2,
                    "allowed_methods": ["GET", "PUT"],
                    "status_forcelist": [429, 500, 503],
                    "retry_on_exceptions": [
                        "ConnectTimeout",
                        "ReadTimeout",
                    ],
                    "backoff_factor": 0,
                    "max_backoff_wait": 9,
                    "backoff_jitter": 0,
                    "respect_retry_after_header": True,
                },
                "error_mapping": {
                    "rules": [
                        {
                            "name": "forbidden",
                            "status_codes": [403],
                            "raise_as": "business",
                        }
                    ]
                },
            }
        )

        self.assertIsInstance(config.retry, Retry)
        self.assertEqual(config.retry.total, 2)
        self.assertTrue(config.retry.is_retryable_method("GET"))
        self.assertTrue(config.retry.is_retryable_method("PUT"))
        self.assertFalse(config.retry.is_retryable_method("POST"))
        self.assertEqual(config.retry.status_forcelist, frozenset({429, 500, 503}))
        self.assertEqual(
            config.retry.retryable_exceptions,
            (httpx.ConnectTimeout, httpx.ReadTimeout),
        )
        self.assertEqual(config.retry.max_backoff_wait, 9)
        self.assertFalse(config.enable_error_mapping)
        self.assertEqual(config.error_mapping.rules[0].name, "forbidden")
        self.assertIs(
            config.error_mapping.rules[0].raise_as,
            BusinessHttpError,
        )

    def test_dict_rejects_legacy_unknown_and_ambiguous_values(self) -> None:
        invalid_configs = (
            {"retry_policy": {}},
            {"retry": {"rules": []}},
            {"retry": {"allowed_methods": []}},
            {"retry": {"retry_on_exceptions": "ConnectTimeout"}},
            {"retry": {"retry_on_exceptions": ["UnknownHttpxError"]}},
            {"retry": {"total": True}},
            {"retry": {"status_forcelist": [500.5]}},
            {"retry": {"backoff_factor": float("nan")}},
            {"retry": {"max_backoff_wait": float("inf")}},
            {"retry": {"backoff_jitter": True}},
            {"enable_error_mapping": "false"},
            {"follow_redirects": "true"},
            {"base_url": None},
            {"timeout": True},
            {"headers": None},
            {"error_mapping": None},
            {"error_mapping": {"rules": "not-a-list"}},
            {
                "error_mapping": {
                    "rules": [
                        {
                            "status_codes": [403],
                            "raise_as": "business",
                        }
                    ]
                }
            },
            {"unknown": True},
        )

        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises((TypeError, ValueError)):
                HttpClientConfig.from_dict(config)

    def test_empty_status_forcelist_configures_exception_only_retries(
        self,
    ) -> None:
        config = HttpClientConfig.from_dict(
            {
                "retry": {
                    "total": 2,
                    "status_forcelist": [],
                    "retry_on_exceptions": ["ConnectTimeout"],
                }
            }
        )

        self.assertFalse(config.retry.is_retryable_status_code(503))
        request = httpx.Request("GET", "https://example.test")
        error = httpx.ConnectTimeout("timeout", request=request)
        self.assertTrue(config.retry.is_retryable_exception(error))

    def test_exception_only_policy_survives_retry_transport_increments(
        self,
    ) -> None:
        config = HttpClientConfig.from_dict(
            {
                "base_url": "https://example.test",
                "enable_error_mapping": False,
                "retry": {
                    "total": 2,
                    "status_forcelist": [],
                    "retry_on_exceptions": ["ConnectTimeout"],
                    "backoff_factor": 0,
                    "backoff_jitter": 0,
                },
            }
        )
        status_calls = 0

        def status_handler(request: httpx.Request) -> httpx.Response:
            nonlocal status_calls
            status_calls += 1
            return httpx.Response(503)

        with HttpClient(
            config,
            transport=httpx.MockTransport(status_handler),
        ) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(status_calls, 1)

        exception_calls = 0

        def exception_handler(request: httpx.Request) -> httpx.Response:
            nonlocal exception_calls
            exception_calls += 1
            raise httpx.ConnectTimeout("timeout", request=request)

        with (
            HttpClient(
                config,
                transport=httpx.MockTransport(exception_handler),
            ) as client,
            self.assertRaises(httpx.ConnectTimeout),
        ):
            client.get("/")

        self.assertEqual(exception_calls, 3)

    def test_direct_retry_instance_is_preserved(self) -> None:
        retry = retry_for(total=4)
        config = HttpClientConfig(retry=retry)

        self.assertIs(config.retry, retry)

    def test_incremented_retry_instance_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "attempts_made"):
            HttpClientConfig(retry=retry_for(total=2).increment())

    def test_default_configuration_disables_retries(self) -> None:
        config = HttpClientConfig()

        self.assertEqual(config.retry.total, 0)

    def test_direct_timeout_object_is_validated(self) -> None:
        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                HttpClientConfig(timeout=httpx.Timeout(value))

    def test_public_type_hints_resolve_on_python_39(self) -> None:
        targets = (
            HttpClientConfig,
            ErrorMappingRule,
            BaseHttpError.__init__,
            HttpClient.request,
            AsyncHttpClient.request,
        )

        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(get_type_hints(target))

    def test_legacy_retry_types_are_not_public(self) -> None:
        self.assertFalse(hasattr(resilient_http, "BackoffConfig"))
        self.assertFalse(hasattr(resilient_http, "RetryPolicy"))
        self.assertFalse(hasattr(resilient_http, "RetryRule"))


if __name__ == "__main__":
    unittest.main()
