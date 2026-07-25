from __future__ import annotations

import unittest
from datetime import datetime, timezone

import httpx

from resilient_http import (
    AsyncHttpClient,
    BackoffConfig,
    BaseHttpError,
    BusinessHttpError,
    HttpClient,
    HttpClientConfig,
    NonReplayableRequestError,
    RetryPolicy,
    RetryRule,
    SystemHttpError,
)


def config_with(*rules: RetryRule) -> HttpClientConfig:
    return HttpClientConfig(
        base_url="https://example.test",
        retry_policy=RetryPolicy(rules=rules),
    )


class UpstreamUnavailable(SystemHttpError):
    pass


class HttpClientTests(unittest.TestCase):
    def test_success_returns_httpx_response(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with HttpClient(config_with(), transport=transport) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_client_accepts_configuration_dict_directly(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with HttpClient(
            {
                "base_url": "https://example.test",
                "timeout": 5,
                "retry": {"rules": []},
            },
            transport=transport,
        ) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)

    def test_status_rule_controls_attempts_and_backoff(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        rule = RetryRule(
            name="internal-error",
            status_codes=frozenset({500}),
            max_attempts=4,
            backoff=BackoffConfig(
                initial_delay=0.5,
                multiplier=2,
                max_delay=1.5,
            ),
            raise_as=SystemHttpError,
        )
        client = HttpClient(
            config_with(rule),
            transport=httpx.MockTransport(handler),
            sleep=sleeps.append,
            random_value=lambda: 0.0,
        )

        with self.assertRaises(SystemHttpError) as caught:
            client.get("/unstable")
        client.close()

        self.assertEqual(calls, 4)
        self.assertEqual(sleeps, [0.5, 1.0, 1.5])
        self.assertEqual(caught.exception.attempts, 4)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.rule_name, "internal-error")
        self.assertTrue(caught.exception.retry_exhausted)
        self.assertIsInstance(caught.exception, BaseHttpError)

    def test_different_conditions_have_different_attempt_counts(self) -> None:
        counts = {429: 0, 503: 0}

        def make_handler(status_code: int):
            def handler(request: httpx.Request) -> httpx.Response:
                counts[status_code] += 1
                return httpx.Response(status_code)

            return handler

        policy = RetryPolicy(
            rules=(
                RetryRule(
                    name="rate-limit",
                    status_codes=frozenset({429}),
                    max_attempts=5,
                    backoff=BackoffConfig(initial_delay=0, max_delay=0),
                ),
                RetryRule(
                    name="unavailable",
                    status_codes=frozenset({503}),
                    max_attempts=2,
                    backoff=BackoffConfig(initial_delay=0, max_delay=0),
                ),
            )
        )
        config = HttpClientConfig(
            base_url="https://example.test",
            retry_policy=policy,
        )

        with (
            HttpClient(config, transport=httpx.MockTransport(make_handler(429))) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.get("/")

        with (
            HttpClient(config, transport=httpx.MockTransport(make_handler(503))) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.get("/")

        self.assertEqual(counts, {429: 5, 503: 2})

    def test_unmatched_4xx_is_business_error_without_retry(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(403)

        with (
            HttpClient(config_with(), transport=httpx.MockTransport(handler)) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/admin")

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retry_exhausted)

    def test_unmatched_5xx_is_system_error_without_retry(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(501)

        with (
            HttpClient(config_with(), transport=httpx.MockTransport(handler)) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/unsupported")

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.status_code, 501)
        self.assertEqual(caught.exception.attempts, 1)

    def test_post_is_not_retried_unless_rule_explicitly_allows_it(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        default_rule = RetryRule(
            name="unavailable",
            status_codes=frozenset({503}),
            max_attempts=3,
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
        )
        with (
            HttpClient(
                config_with(default_rule),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.post("/orders", json={"amount": 100})

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.attempts, 1)

        calls = 0
        post_rule = RetryRule(
            name="idempotent-post",
            status_codes=frozenset({503}),
            max_attempts=3,
            retry_methods=frozenset({"POST"}),
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
        )
        with (
            HttpClient(
                config_with(post_rule),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.post("/orders", json={"amount": 100})

        self.assertEqual(calls, 3)
        self.assertEqual(caught.exception.attempts, 3)

    def test_one_shot_request_body_is_rejected_before_retry_can_corrupt_it(
        self,
    ) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        def body():
            yield b"important-data"

        rule = RetryRule(
            name="put-retry",
            status_codes=frozenset({503}),
            max_attempts=2,
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
        )
        with (
            HttpClient(
                config_with(rule),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(NonReplayableRequestError) as caught,
        ):
            client.put("/objects/1", content=body())

        self.assertEqual(calls, 0)
        self.assertEqual(caught.exception.attempts, 0)
        self.assertIsInstance(caught.exception, SystemHttpError)

    def test_replayable_bytes_body_is_sent_identically_on_each_attempt(self) -> None:
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            return httpx.Response(503)

        rule = RetryRule(
            name="put-retry",
            status_codes=frozenset({503}),
            max_attempts=2,
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
        )
        with (
            HttpClient(
                config_with(rule),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.put("/objects/1", content=b"important-data")

        self.assertEqual(bodies, [b"important-data", b"important-data"])

    def test_transport_errors_are_retried_and_wrapped(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("timed out", request=request)

        rule = RetryRule(
            name="connect-timeout",
            exception_types=(httpx.ConnectTimeout,),
            max_attempts=3,
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
        )
        with (
            HttpClient(
                config_with(rule),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("/slow")

        self.assertEqual(calls, 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertIsInstance(caught.exception.cause, httpx.ConnectTimeout)
        self.assertIsInstance(caught.exception.__cause__, httpx.ConnectTimeout)

    def test_rule_can_raise_custom_application_exception(self) -> None:
        rule = RetryRule(
            name="inventory-down",
            status_codes=frozenset({503}),
            max_attempts=2,
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
            raise_as=UpstreamUnavailable,
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(503))

        with (
            HttpClient(config_with(rule), transport=transport) as client,
            self.assertRaises(UpstreamUnavailable) as caught,
        ):
            client.get("/inventory")

        self.assertEqual(caught.exception.attempts, 2)
        self.assertIsInstance(caught.exception, SystemHttpError)

    def test_retry_after_header_overrides_exponential_delay(self) -> None:
        sleeps: list[float] = []
        rule = RetryRule(
            name="rate-limit",
            status_codes=frozenset({429}),
            max_attempts=2,
            backoff=BackoffConfig(
                initial_delay=0.25,
                multiplier=2,
                max_delay=10,
                respect_retry_after=True,
            ),
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "7"}))
        with (
            HttpClient(
                config_with(rule),
                transport=transport,
                sleep=sleeps.append,
                random_value=lambda: 0.0,
            ) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.get("/")

        self.assertEqual(sleeps, [7.0])

    def test_retry_after_never_shortens_exponential_delay(self) -> None:
        sleeps: list[float] = []
        rule = RetryRule(
            name="rate-limit",
            status_codes=frozenset({429}),
            max_attempts=2,
            backoff=BackoffConfig(
                initial_delay=5,
                multiplier=2,
                max_delay=10,
                respect_retry_after=True,
            ),
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "1"}))
        with (
            HttpClient(
                config_with(rule),
                transport=transport,
                sleep=sleeps.append,
                random_value=lambda: 0.0,
            ) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.get("/")

        self.assertEqual(sleeps, [5.0])

    def test_retry_after_http_date_is_supported(self) -> None:
        sleeps: list[float] = []
        rule = RetryRule(
            name="rate-limit-date",
            status_codes=frozenset({429}),
            max_attempts=2,
            backoff=BackoffConfig(
                initial_delay=1,
                max_delay=30,
                respect_retry_after=True,
            ),
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                headers={"Retry-After": "Sat, 25 Jul 2026 00:00:10 GMT"},
            )
        )
        with (
            HttpClient(
                config_with(rule),
                transport=transport,
                sleep=sleeps.append,
                random_value=lambda: 0.0,
                now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
            ) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.get("/")

        self.assertEqual(sleeps, [10.0])

    def test_extremely_large_retry_after_is_capped_without_leaking_overflow(
        self,
    ) -> None:
        sleeps: list[float] = []
        huge_value = "9" * 5000
        rule = RetryRule(
            name="huge-retry-after",
            status_codes=frozenset({429}),
            max_attempts=2,
            backoff=BackoffConfig(initial_delay=1, max_delay=8),
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                headers={"Retry-After": huge_value},
            )
        )

        with (
            HttpClient(
                config_with(rule),
                transport=transport,
                sleep=sleeps.append,
            ) as client,
            self.assertRaises(SystemHttpError),
        ):
            client.get("/")

        self.assertEqual(sleeps, [8.0])

    def test_failure_is_reclassified_on_every_attempt(self) -> None:
        responses = iter((500, 400))
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(next(responses))

        policy = RetryPolicy(
            rules=(
                RetryRule(
                    name="server-error",
                    status_codes=frozenset({500}),
                    max_attempts=4,
                    backoff=BackoffConfig(initial_delay=0, max_delay=0),
                    raise_as=SystemHttpError,
                ),
                RetryRule(
                    name="bad-request",
                    status_codes=frozenset({400}),
                    max_attempts=1,
                    raise_as=BusinessHttpError,
                ),
            )
        )
        config = HttpClientConfig(
            base_url="https://example.test",
            retry_policy=policy,
        )
        with (
            HttpClient(
                config,
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/")

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.rule_name, "bad-request")

    def test_exception_url_is_sanitized(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(403))
        config = HttpClientConfig(base_url="https://user:pass@example.test")

        with (
            HttpClient(config, transport=transport) as client,
            self.assertRaises(BusinessHttpError) as caught,
        ):
            client.get("/private?token=secret")

        self.assertEqual(caught.exception.url, "https://example.test/private")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("pass", str(caught.exception))

    def test_unexpected_runtime_error_is_not_wrapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("programming bug")

        with (
            HttpClient(
                config_with(),
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaisesRegex(RuntimeError, "programming bug"),
        ):
            client.get("/")

    def test_invalid_url_is_wrapped_as_system_error(self) -> None:
        config = HttpClientConfig()
        with (
            HttpClient(config) as client,
            self.assertRaises(SystemHttpError) as caught,
        ):
            client.get("https://example.test:invalid/")

        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.url, "<invalid-url>")
        self.assertIsInstance(caught.exception.cause, httpx.InvalidURL)

    def test_from_dict_builds_rules_and_httpx_exception_types(self) -> None:
        config = HttpClientConfig.from_dict(
            {
                "base_url": "https://example.test",
                "timeout": {"connect": 2, "read": 5},
                "retry": {
                    "rules": [
                        {
                            "name": "timeout",
                            "exceptions": ["ConnectTimeout", "ReadTimeout"],
                            "max_attempts": 3,
                            "backoff": {
                                "initial": 0.25,
                                "multiplier": 3,
                                "max": 4,
                            },
                            "raise_as": "system",
                        },
                        {
                            "name": "bad-request",
                            "status_codes": [400],
                            "max_attempts": 1,
                            "raise_as": "business",
                        },
                    ]
                },
            }
        )

        self.assertEqual(config.base_url, "https://example.test")
        self.assertIsInstance(config.timeout, httpx.Timeout)
        self.assertEqual(len(config.retry_policy.rules), 2)
        self.assertEqual(
            config.retry_policy.rules[0].exception_types,
            (httpx.ConnectTimeout, httpx.ReadTimeout),
        )
        self.assertIs(
            config.retry_policy.rules[1].raise_as,
            BusinessHttpError,
        )

    def test_context_manager_closes_underlying_client(self) -> None:
        with HttpClient(
            config_with(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        ) as client:
            self.assertFalse(client.raw_client.is_closed)
        self.assertTrue(client.raw_client.is_closed)


class AsyncHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_client_retries_and_then_succeeds(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503 if calls < 3 else 200)

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        rule = RetryRule(
            name="unavailable",
            status_codes=frozenset({503}),
            max_attempts=3,
            backoff=BackoffConfig(
                initial_delay=0.25,
                multiplier=2,
                max_delay=2,
            ),
        )
        async with AsyncHttpClient(
            config_with(rule),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
            random_value=lambda: 0.0,
        ) as client:
            response = await client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertTrue(client.raw_client.is_closed)

    async def test_async_final_business_error_uses_same_contract(self) -> None:
        async with AsyncHttpClient(
            config_with(),
            transport=httpx.MockTransport(lambda request: httpx.Response(403)),
        ) as client:
            with self.assertRaises(BusinessHttpError) as caught:
                await client.get("/admin?token=secret")

        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn("secret", caught.exception.url)

    async def test_async_transport_error_is_retried_and_wrapped(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("timed out", request=request)

        async def fake_sleep(delay: float) -> None:
            return None

        rule = RetryRule(
            name="connect-timeout",
            exception_types=(httpx.ConnectTimeout,),
            max_attempts=2,
            backoff=BackoffConfig(initial_delay=0, max_delay=0),
        )
        async with AsyncHttpClient(
            config_with(rule),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        ) as client:
            with self.assertRaises(SystemHttpError) as caught:
                await client.get("/")

        self.assertEqual(calls, 2)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertIsInstance(caught.exception.__cause__, httpx.ConnectTimeout)


class ConfigurationTests(unittest.TestCase):
    def test_backoff_uses_multiplier_jitter_and_cap(self) -> None:
        backoff = BackoffConfig(
            initial_delay=1,
            multiplier=3,
            max_delay=5,
            jitter=2,
        )

        self.assertEqual(
            backoff.delay_for_retry(1, random_value=0.5),
            2.0,
        )
        self.assertEqual(
            backoff.delay_for_retry(2, random_value=0.5),
            4.0,
        )
        self.assertEqual(
            backoff.delay_for_retry(3, random_value=0.5),
            5.0,
        )

    def test_invalid_or_empty_rules_are_rejected_early(self) -> None:
        with self.assertRaises(ValueError):
            RetryRule(name="empty")
        with self.assertRaises(ValueError):
            RetryRule(
                name="bad-attempts",
                status_codes=frozenset({500}),
                max_attempts=0,
            )
        with self.assertRaises(ValueError):
            RetryRule.from_dict(
                {
                    "name": "typo",
                    "status_codes": [500],
                    "max_atempts": 3,
                }
            )
        with self.assertRaises(TypeError):
            RetryRule(
                name="non-integer",
                status_codes=frozenset({500}),
                max_attempts=2.5,
            )
        with self.assertRaises(ValueError):
            BackoffConfig(initial_delay=float("nan"))


if __name__ == "__main__":
    unittest.main()
