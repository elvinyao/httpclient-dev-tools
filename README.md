# Resilient HTTP Client

一个供多个 Python APP 共用的组织级同步 HTTP Client 规范层。

- HTTP 发送、连接复用和连接池由 [Requests](https://requests.readthedocs.io/) 负责。
- 重试判断、指数退避和 `Retry-After` 由
  [`urllib3.util.Retry`](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html#urllib3.util.Retry)
  负责。
- 本项目负责严格配置、安全默认值、Session 生命周期、最终异常映射和必要的请求安全检查。
- 默认把最终 4xx 映射为业务异常，把最终 5xx 和 Requests 网络异常映射为系统异常。
- 只提供同步客户端，支持 Python 3.9 及以上版本。

本项目不是新的 Retry 实现，也不复制或内置 Requests/urllib3 源码。

## 安装和开发

业务 APP 从组织包源安装：

```bash
uv add resilient-http-client
```

本地联调也可以添加项目目录：

```bash
uv add --editable ../namagi-dev-tools
```

开发环境使用 uv 管理：

```bash
uv sync
uv lock --check
```

运行测试、Ruff 检查和构建：

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv build
```

自动修复和格式化：

```bash
uv run ruff check . --fix
uv run ruff format .
```

最低版本验证：

```bash
uv run --python 3.9 python -m unittest discover -s tests -v
```

依赖通过 `uv add`、`uv remove` 管理，并提交同步更新后的 `pyproject.toml` 和
`uv.lock`。

## Python 3.9 兼容策略

项目声明：

```toml
requires-python = ">=3.9"
dependencies = [
    "requests>=2.32.5,<3",
    "urllib3>=2.6.3,<3",
]
```

Requests 和 urllib3 都是直接依赖：本项目直接导入 `urllib3.util.Retry`，不能只
依赖 Requests 的传递依赖。

uv 根据每个发行版的 `Requires-Python` 为不同 Python 版本生成分叉解析。当前
`uv.lock` 在 Python 3.9 使用 Requests 2.32.5 和 urllib3 2.6.3，在 Python
3.10 及以上使用允许范围内的较新版本。每次升级依赖后都应至少重新运行 Python
3.9 测试和默认开发版本测试。

Ruff 的目标版本为 `py39`。公开类型注解避免依赖只能在 Python 3.10 及以上正确
求值的写法。

## 快速开始：完整 dict 配置

来自 JSON、YAML 或环境配置的 mapping 可以直接传给 `HttpClient`。未知字段、
错误类型以及不安全的空方法集合会在启动阶段失败，而不是等到第一次请求时才暴露。

```python
from resilient_http import BusinessHttpError, HttpClient, SystemHttpError


config = {
    "base_url": "https://api.example.com/v1",
    "timeout": {
        "connect": 3,
        "read": 20,
    },
    "headers": {
        "User-Agent": "inventory-app/1.0",
        "X-App": "inventory",
    },
    "follow_redirects": False,
    "max_redirects": 10,
    "verify": True,
    "trust_env": True,
    "pool": {
        "connections": 10,
        "maxsize": 20,
        "block": False,
    },
    "retry": {
        "total": 3,
        "connect": 3,
        "read": 1,
        "status": 2,
        "other": 0,
        "allowed_methods": ["GET", "HEAD", "OPTIONS"],
        "status_forcelist": [429, 500, 502, 503, 504],
        "backoff_factor": 0.5,
        "backoff_max": 30,
        "backoff_jitter": 0.5,
        "respect_retry_after_header": True,
        "retry_after_max": 60,
    },
    "enable_error_mapping": True,
    "error_mapping": {
        "rules": [
            {
                "name": "permission-denied",
                "status_codes": [401, 403],
                "raise_as": "business",
            },
            {
                "name": "upstream-timeout",
                "exceptions": ["ConnectTimeout", "ReadTimeout"],
                "raise_as": "system",
            },
        ],
        "default_business_error": "business",
        "default_system_error": "system",
    },
}

with HttpClient(config) as client:
    try:
        response = client.get("/users/123")
    except BusinessHttpError as error:
        print("request rejected:", error.status_code)
    except SystemHttpError as error:
        print("upstream unavailable:", error.cause)
    else:
        user = response.json()
```

`HttpClient` 应作为长生命周期对象复用，而不是为每个请求重新创建。Context
Manager 会关闭其拥有的 Session；`close()` 可以重复调用。关闭后再次请求会抛出
`RuntimeError`。

## 使用 dataclass 配置

需要 Python 对象配置时，使用公开的 frozen dataclass：

```python
from resilient_http import (
    HttpClient,
    HttpClientConfig,
    PoolConfig,
    RetryConfig,
    TimeoutConfig,
)


config = HttpClientConfig(
    base_url="https://api.example.com/v1",
    timeout=TimeoutConfig(connect=3, read=20),
    pool=PoolConfig(connections=10, maxsize=20, block=False),
    retry=RetryConfig(
        total=3,
        connect=3,
        read=1,
        status=2,
    ),
)

with HttpClient(config) as client:
    response = client.get("/health")
```

也可以把一个显式的 `urllib3.util.Retry`（本项目同时导出为
`resilient_http.Retry`）传给 `HttpClientConfig`。规范层会验证它，并强制
`redirect=0`、`raise_on_redirect=False` 和 `raise_on_status=False`，确保重定向
仍由 Requests 管理，最终响应仍可进入统一异常映射。一般业务配置优先使用
`RetryConfig` 或 dict。自定义 `Retry` 子类不会被接受，避免规范层的 attempt
观察逻辑静默覆盖子类自己的重试语义。

`retry_from_dict(mapping)` 可用于只构造一个经过规范化的 urllib3 `Retry`。

## 默认值

不提供配置时使用以下默认值：

| 配置 | 默认值 |
| --- | --- |
| `base_url` | `""` |
| connect/read timeout | `10.0` 秒 / `10.0` 秒 |
| `follow_redirects` | `False` |
| `max_redirects` | `10` |
| `verify` | `True` |
| `trust_env` | `True` |
| pool connections/maxsize/block | `10` / `10` / `False` |
| retry `total` | `0`，默认不重试 |
| retry methods | `GET`、`HEAD`、`OPTIONS` |
| retry statuses | `429`、`500`、`502`、`503`、`504` |
| retry `other` | `0` |
| backoff factor/max/jitter | `0.5` / `30.0` 秒 / `0.5` 秒 |
| `respect_retry_after_header` | `True` |
| `retry_after_max` | `60` 秒 |
| `enable_error_mapping` | `True` |

默认 Retry 方法、状态码和退避参数在 `total=0` 时不会产生重试。只设置
`"retry": {"total": 3}` 即可启用这组组织安全默认。

`verify` 还可以是非空 CA bundle 路径。`trust_env=True` 保留 Requests 对环境
代理、认证和证书相关配置的默认处理。

## 重试次数和分类语义

`total` 表示首次发送之后最多允许的重试次数：

```text
total=0  -> 最多发送 1 次
total=2  -> 最多发送 3 次
total=n  -> 最多发送 n + 1 次
```

`connect`、`read`、`status` 和 `other` 是分类上限，所有分类同时受 `total`
总上限约束：

- `connect`：通常发生在远端收到请求之前，例如建连失败。
- `read`：urllib3 在 Adapter 尚未返回时识别的读取/协议错误。
- `status`：方法允许且状态码位于 `status_forcelist` 中的响应。
- `other`：不能归入以上分类的错误；默认固定为 `0`，避免意外重复有副作用的请求。

分类值为 `None` 时使用 `total` 的预算。urllib3 不支持为每个状态码配置不同次数；
例如“500 重试 2 次、429 重试 5 次”不能仅通过本客户端配置表达。

connect 类错误被认为发生在请求发出之前，因此可能不受 `allowed_methods` 限制。
status/read 重试受方法集合限制。默认不对 `POST`、`PUT`、`PATCH`、`DELETE`
进行 status/read 重试。只有远端接口确实幂等、使用了幂等键，并且请求体可以安全
重放时，才应显式加入这些方法。

`allowed_methods=[]` 不表示禁用重试：urllib3 的空集合语义容易退化为允许任意
方法，因此本项目直接拒绝空方法集合。完全关闭重试请使用 `total=0`。
`status_forcelist=[]` 是合法的，可用于关闭基于状态码的强制重试。

## 指数退避、jitter 和 Retry-After

退避完全由 urllib3 实现，指数底数固定为 2，不能单独配置。默认
`backoff_factor=0.5` 时，连续重试的基础等待大致为：

```text
0、1、2、4、8 ... 秒
```

每次再增加 `random.uniform(0, backoff_jitter)` 秒，最后受 `backoff_max`
限制。这里的 `backoff_jitter` 是秒数，不是比例。

`respect_retry_after_header=True` 时，urllib3 会优先遵守适用状态响应中的
`Retry-After`，但本项目默认把单次等待限制在 `retry_after_max=60` 秒。即使某个
状态不在 `status_forcelist` 中，带有效 `Retry-After` 的 413、429 或 503 仍可能
触发 urllib3 的重试判断。

最终状态耗尽后不会由 urllib3 抛出 `RetryError`：规范层固定
`raise_on_status=False`，取得最后一个 Response 后再执行 Business/System 映射。

## 不可重放的请求体

重试和 307/308 redirect 都可能使同一请求体发送多次。若当前 Retry 配置可能重新
发送请求，或者本次请求启用了 redirect，本客户端会在发送前检查 `data=`：

- `str`、`bytes`、`bytearray`、`memoryview`、mapping、list 和 tuple 视为可重放。
- 同时支持 `tell()` 和 `seek()` 且可以回到当前位置的文件对象视为可重放。
- generator、iterator 和不能 rewind 的 stream 会在发送前抛出
  `NonReplayableRequestError`，此时 `attempts == 0`。

完全关闭重试后不会执行这项拒绝检查。

这个保护主要针对 `data=`。复杂 multipart `files=`、自定义对象以及底层
`raw_session` 调用仍由 APP 负责保证可重放。即使方法在语义上幂等，也不代表一次性
请求体可以安全重复发送。

## Business/System 异常映射

公开异常层级：

```text
BaseHttpError
├── BusinessHttpError
└── SystemHttpError
    └── NonReplayableRequestError
```

默认映射发生在所有 urllib3 重试完成之后：

- 最终 400–499：`BusinessHttpError`。
- 最终 500–599：`SystemHttpError`。
- `requests.RequestException`，例如 `ConnectionError`、`ConnectTimeout`、
  `ReadTimeout`、`SSLError`：`SystemHttpError`。
- 300–399 不属于错误；`follow_redirects=False` 时直接返回 3xx Response。

`ErrorMappingPolicy` 中第一条匹配的规则优先，可以按最终状态码或 Requests
异常类型选择自定义 `BaseHttpError` 子类。dict 配置中的异常名称来自
`requests.exceptions`，`raise_as` 使用 `"business"` 或 `"system"`；Python
dataclass 配置可以直接传入自定义异常类。

映射后的异常提供：

- `method`
- 已移除用户名、密码、query 和 fragment 的 `url`
- `attempts`
- `status_code`
- `rule_name`
- `retry_exhausted`
- `response`
- `cause`

`attempts` 包括 urllib3 内部重试和 Requests redirect 产生的实际发送。
`retry_exhausted=True` 表示最终条件原本可以重试、至少发生过一次重试且预算已经
耗尽；不可重试的最终条件、`total=0` 和响应头返回后的 body 读取错误不会被误标为
耗尽。

本项目不根据异常类型直接写业务日志。APP 可以捕获 `BusinessHttpError` 和
`SystemHttpError`，自行决定使用 error、critical、告警或降级。异常消息不会包含
query 值或响应 body；APP 记录 `response`、`cause` 时仍需执行自己的敏感信息策略。

设置 `enable_error_mapping=False` 后，最终 4xx/5xx Response 会直接返回，
Requests 网络异常会保持原类型抛出。

## base_url 安全规则

配置了 `base_url` 时：

- 必须是带 host 的绝对 `http://` 或 `https://` URL。
- 不允许包含用户名、密码、query 或 fragment。
- 每次请求的 `url` 必须是相对 URL；绝对 URL 和 `//other-host/path` 会被拒绝，
  避免调用方绕过已配置的 host。
- 启用 redirect 后也只允许同 scheme、host 和有效端口的同源跳转；跨域跳转会在
  第二个请求发出前被拒绝，避免自定义认证 header 被转发到另一个服务。HTTP 升级到
  HTTPS 也属于跨源，需要 APP 直接使用最终 HTTPS URL。
- 请求路径开头的 `/` 会被移除后再拼接，因此
  `base_url=https://api.example.com/v1` 与 `/users` 会得到
  `https://api.example.com/v1/users`，不会意外丢失 `/v1`。

相对路径中的 `..` 仍遵循标准 URL 归一化规则，可能离开 `base_url` 的 path
前缀；不要把未经校验的用户输入直接作为相对路径。

未配置 `base_url` 时，应向客户端传入完整绝对 URL。最终异常中的 URL 会自动移除
认证信息、query 和 fragment。

## Session、raw_session 和 create_session

`HttpClient` 创建并拥有一个可复用的 Requests Session，同时为 `http://` 和
`https://` 安装带 urllib3 Retry 的 `HTTPAdapter`。

`raw_session` 暴露这个底层 Session；`raw_client` 是迁移期兼容别名。直接调用
它时仍会使用已挂载的 retry、连接池、headers、TLS、环境和 redirect limit 配置，
但会绕过：

- `HttpClient` 的默认 timeout 注入
- `base_url` 解析和 host 限制
- 同源 redirect 保护
- 不可重放 body 检查
- Business/System 异常映射
- 统一 attempt 元数据

因此业务请求应继续通过 `HttpClient.request/get/post/...`。`raw_session`
主要用于设置 Requests 原生的 auth、cookies、proxies 或调试状态，不应作为另一套
业务调用入口。

通过 `session_factory` 注入的 Session 也由 `HttpClient` 接管并在关闭时关闭；
factory 必须返回 `requests.Session`。若自定义 Session 覆写了 `request()`、
`send()` 或 adapter 流程，它也可能绕过 attempt 统计和同源 redirect 保护；这个
接缝主要用于测试或保持 Requests 标准发送链的受控扩展。

`create_session(config)` 适用于明确只需要组织配置的底层 Requests Session：

```python
from resilient_http import create_session


session = create_session(config)
try:
    response = session.get(
        "https://api.example.com/health",
        timeout=(3, 20),
    )
finally:
    session.close()
```

调用方拥有 `create_session` 的返回值，并且必须显式提供 timeout。这个入口不提供
`HttpClient` 的 base URL、同源 redirect 保护、body 安全检查或异常映射。

## 运行边界

### 同步和异步

Requests 和 urllib3 Retry 都是同步阻塞的，包括退避期间的 sleep。本项目不提供
`AsyncHttpClient`，也不应直接在 asyncio event loop 中调用。异步 APP 可以把整个
同步调用放入受控线程池，或者继续使用独立的异步 HTTPX 客户端。

### Timeout 不是总 deadline

默认 `(10.0, 10.0)` 分别是每一次物理尝试的 connect timeout 和 read timeout。
Requests 没有这里的 write timeout、pool timeout 或涵盖全部重试与 backoff 的总
deadline。DNS、多个地址、每次重试和每段退避都可能增加总耗时。调用方可以通过
每次请求的 `timeout=` 覆盖默认值，但如果业务需要端到端 deadline，应在更上层实现。

### 流式读取

urllib3 Retry 发生在 `HTTPAdapter` 内部。Response headers 已返回之后的 body
读取失败不会重新进入 Adapter：

- 默认 `stream=False` 时，这类 Requests 异常仍发生在 `HttpClient.request`
  返回前，会映射为 `SystemHttpError`，但不会自动重试。
- `stream=True` 时，`iter_content()`、`iter_lines()` 或 `response.raw` 的异常
  发生在客户端返回之后，不会被本项目映射或重试。

使用 `stream=True` 时必须消费完 body 或关闭 Response，连接才会归还连接池。

### 连接池不是限流器

`PoolConfig.connections` 是 Adapter 缓存的连接池数量，`maxsize` 是每个池保留的
连接数。默认 `block=False` 时，`maxsize` 不是并发硬上限；`block=True` 时池耗尽
会等待，而 Requests 没有通过本配置暴露 pool acquisition timeout。

连接池不能实现 QPS、并发或令牌桶流控。当前版本没有流量控制功能；若未来增加，
必须明确是限制一次逻辑请求，还是包括 urllib3 内部每一次物理 retry attempt。

## 从旧 HTTPX 版本迁移

这个版本已经从 HTTPX/httpx-retries 架构迁移到 Requests/urllib3：

- 删除 `AsyncHttpClient`；现在只有同步 `HttpClient`。
- 删除 vendored `httpx-retries` 和私有 `_vendor` 命名空间。
- 不再依赖 `httpx`，Response 和网络异常类型改为 Requests 类型。
- `Retry` 现在是 `urllib3.util.Retry`，不再是 httpx-retries 的类。
- 推荐把原来的 `Retry(...)` 配置迁移为 `RetryConfig(...)` 或 dict。
- `retry_on_exceptions` 不再存在；改用 urllib3 的 `connect`、`read`、`status`
  和 `other` 分类预算。
- `max_backoff_wait` 改为 `backoff_max`。
- `backoff_jitter` 从旧实现的比例语义变为 urllib3 的随机附加秒数。
- timeout 只保留 Requests 支持的 connect/read；旧 write/pool timeout 不再存在。
- `raw_client` 暂时保留为 `raw_session` 的兼容别名，新代码应使用
  `raw_session`。
- 测试注入从 HTTPX transport 改为返回 `requests.Session` 的
  `session_factory`。

旧代码不得继续导入 `resilient_http._vendor`，也不能把同步 Requests 客户端当作
HTTPX AsyncClient 的无缝替代。业务 APP 应显式评估同步执行模型、异常类型和
timeout 总耗时变化。

## 公开入口

稳定公开入口位于 `resilient_http`：

- `HttpClient`
- `HttpClientConfig`
- `TimeoutConfig`
- `PoolConfig`
- `RetryConfig`
- `Retry`
- `retry_from_dict`
- `create_session`
- `ErrorMappingRule`
- `ErrorMappingPolicy`
- `BaseHttpError`
- `BusinessHttpError`
- `SystemHttpError`
- `NonReplayableRequestError`

业务代码不应导入以下划线开头的内部模块或类。
