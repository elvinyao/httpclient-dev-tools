# Resilient HTTP Client

一个基于 HTTPX 的轻量公共客户端。HTTPX 只负责发送请求和连接池；本包用一层明确的策略统一处理：

- 不同状态码或网络异常使用不同的最大尝试次数
- 指数退避、上限、jitter 和 `Retry-After`
- 默认只重试幂等 HTTP 方法
- 重试耗尽后，不泄漏 HTTPX 异常，而是抛出业务系统可处理的异常
- 同步 `HttpClient` 和异步 `AsyncHttpClient` 使用相同策略

## 安装

```bash
pip install -e .
```

## 最简配置

```python
from resilient_http import HttpClient, HttpClientConfig


config = HttpClientConfig.from_dict({
    "base_url": "https://api.example.com",
    "timeout": {
        "connect": 3,
        "read": 20,
    },
    "retry": {
        "rules": [
            {
                "name": "rate-limit",
                "status_codes": [429],
                "max_attempts": 5,
                "backoff": {
                    "initial": 1,
                    "multiplier": 2,
                    "max": 30,
                    "jitter": 0.5,
                    "respect_retry_after": True,
                },
                "raise_as": "system",
            },
            {
                "name": "server-error",
                "status_codes": [500, 502, 503, 504],
                "max_attempts": 3,
                "backoff": {
                    "initial": 0.5,
                    "multiplier": 2,
                    "max": 10,
                },
                "raise_as": "system",
            },
            {
                "name": "network-error",
                "exceptions": [
                    "ConnectTimeout",
                    "ReadTimeout",
                    "ConnectError",
                ],
                "max_attempts": 4,
                "backoff": {
                    "initial": 0.5,
                    "multiplier": 2,
                    "max": 10,
                },
                "raise_as": "system",
            },
            {
                "name": "known-client-error",
                "status_codes": [400, 401, 403, 404],
                "max_attempts": 1,
                "raise_as": "business",
            },
        ]
    },
})

with HttpClient(config) as client:
    response = client.get("/users/123")
```

如果配置已经来自 JSON/YAML 等映射对象，也可以直接传给客户端；构造时仍会
执行同样的严格校验：

```python
with HttpClient(config_dict) as client:
    response = client.get("/users/123")
```

`max_attempts` 包含首次请求：

```text
max_attempts = 1：发送 1 次，不重试
max_attempts = 3：最多发送 3 次，即首次请求 + 2 次重试
```

规则按声明顺序匹配，第一条匹配规则生效。因此具体规则应放在宽泛规则前面。

## 异常层级

```text
BaseHttpError
├── BusinessHttpError
└── SystemHttpError
    └── NonReplayableRequestError
```

未匹配规则时：

- HTTP 4xx 转换为 `BusinessHttpError`
- HTTP 5xx 和 HTTPX `RequestError` 转换为 `SystemHttpError`

所有异常都携带：

- `method`
- `url`
- `attempts`
- `rule_name`
- `status_code`
- `response`
- `cause`
- `retry_exhausted`

业务 APP 可以在统一层处理：

```python
from resilient_http import BusinessHttpError, SystemHttpError

try:
    response = client.get("/users/123")
except BusinessHttpError as error:
    # 参数、认证、权限或资源状态等调用方问题
    handle_business_failure(error)
except SystemHttpError as error:
    # 网络、超时或上游系统问题
    trigger_fallback_or_alert(error)
```

公共客户端只负责分类并抛出异常，不自行决定日志等级。每个 APP 可以根据
`BusinessHttpError`、`SystemHttpError`、具体子类及 `retry_exhausted`
统一记录日志、告警或触发降级，避免公共层和业务层重复记录同一个失败。

### 使用业务自己的异常类型

规则也可以直接指定 `BaseHttpError` 子类：

```python
from resilient_http import (
    BackoffConfig,
    HttpClient,
    HttpClientConfig,
    RetryPolicy,
    RetryRule,
    SystemHttpError,
)


class InventoryUnavailable(SystemHttpError):
    pass


rule = RetryRule(
    name="inventory-unavailable",
    status_codes=frozenset({503}),
    max_attempts=3,
    backoff=BackoffConfig(initial_delay=0.5, multiplier=2),
    raise_as=InventoryUnavailable,
)

config = HttpClientConfig(
    base_url="https://inventory.example.com",
    retry_policy=RetryPolicy(rules=(rule,)),
)
```

耗尽后会直接抛出 `InventoryUnavailable`，同时它仍然可以被
`SystemHttpError` 或 `BaseHttpError` 捕获。

## POST 与其他非幂等请求

默认允许重试的方法是：

```text
GET, HEAD, OPTIONS, PUT, DELETE
```

`POST` 和 `PATCH` 即使匹配了状态码，也默认只尝试一次。只有服务端支持
幂等键或可以证明重复执行安全时，才应显式开启：

```python
RetryRule(
    name="idempotent-order-create",
    status_codes=frozenset({503}),
    max_attempts=3,
    retry_methods=frozenset({"POST"}),
)
```

请求体必须是可重复发送的。若当前方法存在可重试规则，而请求使用一次性
iterator/stream 或 open file，客户端会在真正发送前抛出
`NonReplayableRequestError`，避免第一次带数据、第二次却静默发送空 body。
可以直接使用 `bytes`、字符串、JSON 或其他可重建的数据。

## 异步客户端

```python
from resilient_http import AsyncHttpClient


async with AsyncHttpClient(config) as client:
    response = await client.get("/users/123")
```

同步和异步客户端共用相同的配置与异常类型。

## 设计约束

- 本包实现唯一的重试层，不要同时配置 HTTPX transport retries，否则实际请求次数会相乘。
- `Retry-After` 支持秒数和 HTTP-date；结果仍受该规则的 `max_delay` 限制。
- `Retry-After` 不会缩短已计算出的指数退避时间。
- 失败响应在再次尝试前会关闭，以便连接及时回到连接池。
- 常规 HTTP 4xx 默认不重试；如果某个上游有特殊语义，可显式增加规则。
- URL 在异常信息中会移除用户名、密码、query 和 fragment；完整响应仍可由
  业务代码通过 `error.response` 按需读取。
- 每次失败都会重新匹配规则；`attempts` 是整个逻辑请求的全局发送序号，
  不会在规则变化时重新从 1 计数。
- HTTPX 的 timeout 是每次 attempt 的 timeout，不是包括全部重试与等待的
  总 deadline。
- 直接传给 `raise_as` 的自定义异常类必须继承 `BaseHttpError`，并保留其
  构造器签名；推荐只增加方法，不覆盖 `__init__`。

## 运行测试

```bash
python -m unittest discover -s tests -v
```
