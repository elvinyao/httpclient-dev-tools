# Resilient HTTP Client

一个面向多个 Python APP 的轻量 HTTP 客户端：

- HTTP 发送、连接池由 HTTPX 负责。
- 所有重试均由内置的 `httpx-retries 0.4.6` 源码快照负责，本项目不再实现重试循环。
- 可选的异常映射层将最终失败转换为业务系统可处理的异常。
- 同时提供同步 `HttpClient` 和异步 `AsyncHttpClient`。
- 支持 Python 3.9。

## 安装与开发

项目使用 uv 管理：

```bash
uv sync
```

运行测试和代码检查：

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
```

自动修复与格式化：

```bash
uv run ruff check . --fix
uv run ruff format .
```

依赖统一通过 `uv add`/`uv remove` 管理，并提交更新后的 `uv.lock`。业务 APP
只需要依赖本项目，不需要再安装单独的 `httpx-retries` distribution；HTTPX
仍是正常的运行时依赖。

## Vendored httpx-retries

项目在私有命名空间 `resilient_http._vendor.httpx_retries` 中内置上游
`httpx-retries 0.4.6`，因为这是最后一个支持 Python 3.9 的版本。业务代码
不得直接导入 `_vendor`；稳定入口是 `from resilient_http import Retry`。

vendored Python 源码保持上游原样，来源、版本、commit、更新流程和 MIT
许可证保存在：

- `src/resilient_http/_vendor/httpx_retries/VENDORED.md`
- `src/resilient_http/_vendor/httpx_retries/LICENSE`

构建出的 wheel 不会提供顶层 `httpx_retries` 包，避免和业务 APP 的其他依赖
发生同名覆盖。

## 重试次数的语义

`httpx-retries` 使用 `total` 表示“首次请求之后最多再重试几次”：

```text
Retry(total=0)  = 不重试，最多发送 1 次
Retry(total=2)  = 最多重试 2 次，最多发送 3 次
Retry(total=n)  = 最多重试 n 次，最多发送 n + 1 次
```

`HttpClientConfig` 默认使用 `Retry(total=0)`，即默认不重试。请注意，上游
直接创建 `Retry()` 时默认是 `total=10`。

## 直接传入 Retry

需要 Python 对象配置时，使用本项目公开的 `Retry`：

```python
import httpx

from resilient_http import HttpClient, HttpClientConfig, Retry


retry = Retry(
    total=3,
    allowed_methods={"GET", "HEAD", "PUT", "DELETE"},
    status_forcelist={429, 500, 502, 503, 504},
    retry_on_exceptions=(
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ConnectError,
    ),
    backoff_factor=0.5,
    max_backoff_wait=30,
    backoff_jitter=1.0,
    respect_retry_after_header=True,
)

config = HttpClientConfig(
    base_url="https://api.example.com",
    retry=retry,
)

with HttpClient(config) as client:
    response = client.get("/users/123")
```

`allowed_methods`、`status_forcelist` 和 `retry_on_exceptions` 共用同一个
`total`。0.4.6 不支持“500 重试 2 次、429 重试 5 次”这类按条件设置不同次数
的策略。

指数退避也完全采用上游语义：基础值为
`backoff_factor * 2 ** attempts_made`，然后应用 `backoff_jitter` 比例并受
`max_backoff_wait` 限制。大于 0 的有效 `Retry-After` 会优先于指数退避，同样受
`max_backoff_wait` 限制；`backoff_factor=0` 时，除 `Retry-After` 外不主动
等待。

如果未显式指定集合，上游 0.4.6 的默认值为：

- 方法：`HEAD`、`GET`、`PUT`、`DELETE`、`OPTIONS`、`TRACE`
- 状态码：`429`、`502`、`503`、`504`，不包含 `500`
- 异常：`TimeoutException`、`NetworkError`、`RemoteProtocolError`

`POST` 和 `PATCH` 默认不会重试。只有服务端支持幂等键或重复执行确实安全时，
才应将它们加入 `allowed_methods`。

传入的 `Retry` 必须是尚未执行 `increment()` 的新对象，即
`attempts_made=0`。为避免把程序错误当作网络错误重试，本客户端还要求
`retry_on_exceptions` 中的类型继承 `httpx.RequestError`。

0.4.6 在 Python 3.9 中用固定的标准 HTTP method enum 判断重试方法，因此
`PROPFIND` 等扩展方法即使在 `total=0` 时也会在发送前抛出 `ValueError`。
本客户端保持这一上游限制，只面向该版本支持的标准方法。

## 使用 dict 配置

来自 JSON、YAML 或环境配置的 mapping 可以直接传给客户端：

```python
from resilient_http import HttpClient


config = {
    "base_url": "https://api.example.com",
    "timeout": {
        "default": 10,
        "connect": 3,
        "read": 20,
    },
    "retry": {
        "total": 3,
        "allowed_methods": ["GET", "HEAD", "PUT", "DELETE"],
        "status_forcelist": [429, 500, 502, 503, 504],
        "retry_on_exceptions": [
            "ConnectTimeout",
            "ReadTimeout",
            "ConnectError",
        ],
        "backoff_factor": 0.5,
        "max_backoff_wait": 30,
        "backoff_jitter": 1.0,
        "respect_retry_after_header": True,
    },
}

with HttpClient(config) as client:
    response = client.get("/users/123")
```

dict 中的 retry 字段与 `Retry` 0.4.6 构造参数同名。异常既可以使用受支持的
HTTPX 异常名称，也可以在 Python mapping 中直接传入 `httpx.RequestError`
子类。

省略集合字段表示使用上游默认值。若只想重试异常，可显式设置
`"status_forcelist": []`；若只想重试状态码，可设置
`"retry_on_exceptions": []`。由于 0.4.6 对空方法集合会错误地恢复默认方法，
`allowed_methods` 不接受空集合；完全禁用重试请使用 `total=0` 并省略该字段。

## 可选业务异常映射

异常层级如下：

```text
BaseHttpError
├── BusinessHttpError
└── SystemHttpError
    └── NonReplayableRequestError
```

映射后的异常提供 `method`、已移除认证信息/query/fragment 的 `url`、
`attempts`、`status_code`、`rule_name`、`retry_exhausted`、`response` 和
`cause`。`attempts` 统计该逻辑请求实际经过 Transport 的发送次数，包括重试
和 redirect。`retry_exhausted=True` 只表示最终条件本来可重试且已用完大于
0 的重试预算；`total=0`、遇到不可重试的最终条件，或错误发生在 Transport
返回后的 body 读取阶段时为 `False`。

### 开启映射

`enable_error_mapping=True` 是默认值。内置的 `RetryTransport` 完成全部重试后：

- 最终 HTTP 4xx 默认抛出 `BusinessHttpError`。
- 最终 HTTP 5xx 默认抛出 `SystemHttpError`。
- 最终 `httpx.RequestError` 默认抛出 `SystemHttpError`。
- `ErrorMappingPolicy` 中第一条匹配的规则可以覆盖默认异常类型。

重试策略与异常映射策略互相独立：是否重试只由 `Retry` 决定，最终抛出什么
异常只由 `ErrorMappingPolicy` 决定。

```python
import httpx

from resilient_http import (
    BusinessHttpError,
    ErrorMappingPolicy,
    ErrorMappingRule,
    HttpClient,
    HttpClientConfig,
    Retry,
    SystemHttpError,
)


mapping = ErrorMappingPolicy(
    rules=(
        ErrorMappingRule(
            name="permission-denied",
            status_codes=frozenset({401, 403}),
            raise_as=BusinessHttpError,
        ),
        ErrorMappingRule(
            name="upstream-timeout",
            exception_types=(httpx.ConnectTimeout, httpx.ReadTimeout),
            raise_as=SystemHttpError,
        ),
    ),
)

config = HttpClientConfig(
    base_url="https://api.example.com",
    retry=Retry(
        total=2,
        status_forcelist={429, 500, 502, 503, 504},
    ),
    enable_error_mapping=True,
    error_mapping=mapping,
)

with HttpClient(config) as client:
    try:
        client.get("/admin")
    except BusinessHttpError as error:
        handle_business_failure(error)
    except SystemHttpError as error:
        trigger_fallback_or_alert(error)
```

dict 配置的写法：

```python
config = {
    "base_url": "https://api.example.com",
    "retry": {
        "total": 2,
        "status_forcelist": [429, 500, 502, 503, 504],
    },
    "enable_error_mapping": True,
    "error_mapping": {
        "rules": [
            {
                "name": "client-error",
                "status_codes": [400, 401, 403, 404],
                "raise_as": "business",
            },
            {
                "name": "network-error",
                "exceptions": ["ConnectTimeout", "ReadTimeout"],
                "raise_as": "system",
            },
        ],
        "default_business_error": "business",
        "default_system_error": "system",
    },
}
```

### 关闭映射

不希望公共客户端转换最终 HTTPX 结果时，设置：

```python
config = HttpClientConfig(
    retry=Retry(total=2),
    enable_error_mapping=False,
)
```

此时：

- 最终 4xx/5xx 作为普通 `httpx.Response` 返回。
- 最终网络错误保留原始 `httpx.RequestError`。
- 重试仍然由内置的 `RetryTransport` 执行。
- 通过 `HttpClient`/`AsyncHttpClient` 请求时，one-shot 请求体安全检查仍然有效。

公共客户端不自行决定日志等级。APP 可以在映射开启时根据
`BusinessHttpError`、`SystemHttpError` 或具体子类统一记录日志和告警；映射
关闭时则按原生 HTTPX 状态码与异常处理。

本客户端会清理自身异常文本里的敏感 URL 部分，但 vendored 上游代码的 DEBUG
日志可能包含完整 request URL。生产环境不要直接开启该 logger 的 DEBUG 输出，
或在日志管道中先过滤 token、query 和认证信息。

## 自定义业务异常

自定义类型必须继承 `BaseHttpError`。推荐继承 `BusinessHttpError` 或
`SystemHttpError`，并保留父类构造器签名：

```python
from resilient_http import (
    ErrorMappingPolicy,
    ErrorMappingRule,
    HttpClient,
    HttpClientConfig,
    SystemHttpError,
)


class InventoryUnavailable(SystemHttpError):
    pass


mapping = ErrorMappingPolicy(
    rules=(
        ErrorMappingRule(
            name="inventory-unavailable",
            status_codes=frozenset({503}),
            raise_as=InventoryUnavailable,
        ),
    ),
)

with HttpClient(
    HttpClientConfig(
        base_url="https://inventory.example.com",
        retry={
            "total": 2,
            "status_forcelist": [503],
        },
        error_mapping=mapping,
    )
) as client:
    client.get("/stock")
```

耗尽重试后会抛出 `InventoryUnavailable`，同时仍可由
`SystemHttpError` 或 `BaseHttpError` 捕获。JSON/YAML 配置只能使用
`"business"`、`"system"` 等内置类型名称；自定义异常类应通过 Python 配置
传入。

## one-shot 请求体安全边界

`RetryTransport` 会重复发送同一个 request，但不会验证 body 是否能够重放。
当 `total > 0` 且当前 HTTP 方法允许重试时，本客户端会在首次发送前拒绝：

- generator、iterator 或一次性 byte stream
- `data` 中的一次性 iterator
- 包含 open file/stream 的 multipart `files`

这类请求会抛出 `NonReplayableRequestError`，防止首次有内容、重试时却发送
空 body。优先使用 `bytes`、字符串、JSON 或完全位于内存中的 multipart
内容。

此检查只能识别常见的一次性对象，不能证明任意自定义 stream 一定可重放。
业务侧仍需确保允许重试的方法、幂等键和请求体都满足重复发送要求。

## Vendored 0.4.6 的 response body 限制

vendored `httpx-retries 0.4.6` 只有 Transport 级重试。Transport 在 HTTPX 读取完整
response body 之前已经返回，因此：

- 连接、发送以及收到响应头之前的可重试异常可以重试。
- 可重试 HTTP 状态码可以重试。
- 开始读取最终 response body 后才发生的 `ReadTimeout`、
  `RemoteProtocolError` 等异常无法由 `RetryTransport` 重试。

后续版本提供的 `retry_request` / `aretry_request` helper 可以覆盖部分
read-phase 错误，但它们不支持 Python 3.9，因此本项目不使用这些 API。

## 异步客户端

```python
from resilient_http import AsyncHttpClient


async with AsyncHttpClient(config) as client:
    response = await client.get("/users/123")
```

同步和异步客户端使用相同的 `Retry`、dict 配置、异常映射与安全边界。

`raw_client` 属性是高级 escape hatch。它仍使用同一个 `RetryTransport`，
但会绕过异常映射和 one-shot 请求体检查；不要通过它发送可能重试的 stream、
generator 或 open file，否则上游可能在后续 attempt 中发送空 body。

## Transport 与未来限流

当前只实现重试和可选异常映射，不提供内置流量控制。未来可以把限流 Transport
作为内层 Transport，再由 `RetryTransport` 包装：

```text
HttpClient
└── RetryTransport          唯一重试层
    └── RateLimitTransport  每次物理发送都经过限流
        └── HTTPTransport
```

这样首次发送和每次重试都会计入限流。不要再叠加第二个重试 Transport，否则
实际发送次数会相乘，且监控中的尝试次数会失真。

## 从旧 API 迁移

旧版本的自研重试 API 已删除，主要映射如下：

| 旧配置 | 新配置 |
| --- | --- |
| `retry_policy=RetryPolicy(...)` | `retry=Retry(...)` 或 flat retry dict |
| `retry.rules` / `RetryRule` | 删除；重试条件直接配置在同一个 `Retry` 中 |
| `max_attempts` | `total=max_attempts-1` |
| `retry_methods` | `allowed_methods` |
| `status_codes` | `status_forcelist` |
| `exceptions` / `exception_types` | `retry_on_exceptions` |
| `BackoffConfig.initial_delay` | `backoff_factor`，公式由上游 0.4.6 决定 |
| `BackoffConfig.max_delay` | `max_backoff_wait` |
| `BackoffConfig.jitter` | `backoff_jitter`，从加性秒数改为 `0..1` 比例 |
| `respect_retry_after` | `respect_retry_after_header` |
| `RetryRule.raise_as` | `ErrorMappingRule.raise_as` |
| 默认业务/系统异常 | `ErrorMappingPolicy` 的两个 default 字段 |

迁移示例：

```text
旧 max_attempts=1  → 新 total=0
旧 max_attempts=3  → 新 total=2
```

旧规则可以为不同错误设置不同次数；新方案完全采用 0.4.6 的单一 `Retry`
策略，因此必须选择一个统一的 `total`。异常分类不再混在重试规则中，而是
迁移到独立、可关闭的 `ErrorMappingPolicy`。

旧规则中的状态码和异常如果同时影响“是否重试”与“最终异常类型”，迁移时也
要拆成两份：分别放进 `Retry` 和 `ErrorMappingRule`。退避参数也不是数值
原样改名：0.4.6 的第一次指数等待为 `backoff_factor * 2`，要保持旧的首次
等待 `initial_delay`，可从 `backoff_factor=initial_delay/2` 开始评估；
上游 multiplier 固定为 2，且有效 `Retry-After` 会替代而不是取两者较大值。
