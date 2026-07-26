# Resilient HTTP Client 0.2.0

一个基于 Requests 和 urllib3 的最小同步 HTTP Session factory。

本项目只做一件事：接收调用方明确创建的 `urllib3.util.Retry`，返回一个已为
`http://` 和 `https://` 挂载 Retry Adapter 的独立 `requests.Session`。

公开 API 只有：

```python
from resilient_http import Retry, create_session
```

没有自定义 HTTP Client、配置模型、业务异常、attempt 统计、base URL、默认
timeout、redirect 策略或流量控制。

## 安装和开发

业务 APP 安装：

```bash
uv add resilient-http-client
```

本地开发：

```bash
uv sync
uv lock --check
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

最低 Python 版本验证：

```bash
uv run --python 3.9 python -m unittest discover -s tests -v
```

项目支持 Python 3.9 及以上版本，并使用 uv 管理依赖和 lockfile。Requests 与
urllib3 都是直接依赖，因为本项目直接公开并使用 `urllib3.util.Retry`。

## 完整示例

`retry` 是 `create_session` 的必填参数。本项目不提供隐藏的默认重试策略：

```python
import requests

from resilient_http import Retry, create_session


retry = Retry(
    # 首次请求之后最多再重试 3 次。
    total=3,
    connect=3,
    read=1,
    status=2,
    other=0,
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    status_forcelist=frozenset({429, 500, 502, 503, 504}),
    backoff_factor=0.5,
    backoff_max=30.0,
    backoff_jitter=0.5,
    respect_retry_after_header=True,
    # 耗尽状态重试后返回最后一个 Response，由 APP 统一处理。
    raise_on_status=False,
)

try:
    with create_session(retry) as session:
        response = session.get(
            "https://api.example.com/v1/users/123",
            # Requests 没有默认 timeout；每个调用都应明确传入。
            timeout=(3, 20),
        )
        response.raise_for_status()
        user = response.json()
except requests.RequestException as error:
    print(f"HTTP request failed: {error}")
```

`create_session` 的签名为：

```python
def create_session(retry: Retry) -> requests.Session: ...
```

它不会修改传入的 Retry，也不会替业务 APP 决定哪些方法、状态码或错误应该重试。
Adapter 保存的是重试策略；urllib3 在每个逻辑请求中创建独立的 Retry/history
状态，因此同一 Session 内先前请求的 attempts 不会消耗后续请求的重试次数。

## Retry 语义

重试行为完全采用 urllib3：

- `total` 是首次发送之后的总重试上限。`total=3` 表示最多发送 4 次。
- `connect`、`read`、`status` 和 `other` 是分类上限，同时受 `total` 约束。
- `allowed_methods` 控制适合按方法重试的条件。
- `status_forcelist` 控制哪些响应状态触发强制重试。
- `backoff_factor`、`backoff_max` 和 `backoff_jitter` 控制指数退避。
- `respect_retry_after_header=True` 时，urllib3 会处理适用响应的
  `Retry-After`。

urllib3 的指数底数固定为 2。`backoff_jitter` 是随机附加的秒数，不是比例。

推荐保持 `other=0`，并默认只重试确定安全的方法。不要因为 `PUT`、`DELETE` 或
`POST` 在某个业务中“通常可重试”就直接加入集合；必须同时确认远端幂等语义、幂等
键和请求体可重放。

`raise_on_status=False` 让 urllib3 在状态重试耗尽后返回最后一个 Response。
业务 APP 随后调用 `response.raise_for_status()`，会得到 Requests 原生
`HTTPError`。如果设置为 `True`，状态重试耗尽时可能直接得到 Requests
`RetryError`。

本项目不支持“500 重试 2 次、429 重试 5 次”这种逐状态次数配置；这是 urllib3
Retry 自身的能力边界。

## Session 和连接池生命周期

每次调用 `create_session(retry)` 都会创建一个全新的 Session：

- Cookie、headers、auth、hooks 和其他 Session 状态互不共享。
- HTTP/HTTPS Adapter 和底层连接池互不共享。
- 关闭一个 Session 不会关闭其他 factory 调用创建的 Session。
- 本项目没有全局 Session、连接池缓存或单例。

应在同一个 `with` 块中复用 Session：

```python
with create_session(retry) as session:
    first = session.get(
        "https://api.example.com/v1/first",
        timeout=(3, 20),
    )
    first.raise_for_status()

    second = session.get(
        "https://api.example.com/v1/second",
        timeout=(3, 20),
    )
    second.raise_for_status()
```

这两个请求使用同一组 Adapter 和连接池，并可能复用同一 origin 的空闲 TCP
连接。若为每个请求单独调用 `create_session`，就失去了这种连接复用。

Session 复用不等于固定使用同一条 TCP 连接。连接失败或连接已不可用时，urllib3
可以丢弃旧连接；同一 Session 内的下一次 retry attempt 可以从同一连接池重新建立
TCP 连接。Retry 属于原逻辑请求，而不是某一条固定的物理连接。

`create_session` 使用 Requests `HTTPAdapter` 的原生连接池默认值，不公开
pool 参数。若 APP 必须自定义 Adapter、连接池规模或底层 TLS 行为，应直接使用
Requests 的 Session/Adapter API，而不是继续给这个最小 factory 增加配置层。

## Timeout

Requests 默认没有 timeout，本项目也不添加默认值。每一次调用都应明确传入：

```python
response = session.get(
    "https://api.example.com/health",
    timeout=(3, 20),
)
```

tuple 分别表示 connect timeout 和 read timeout。它们作用于每一次物理尝试，
不是覆盖全部 retry、backoff、DNS 和响应读取过程的端到端 deadline。因此总耗时
可能明显大于单次 timeout。

## stream=True

默认 `stream=False` 时，Requests 会在返回前读取 response body，连接随后可以
归还连接池。

使用 `stream=True` 时，必须消费完整 body 或显式关闭 Response：

```python
with create_session(retry) as session:
    with session.get(
        "https://api.example.com/large-file",
        timeout=(3, 20),
        stream=True,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            process(chunk)
```

如果既不消费 body 也不关闭 Response，该连接不能及时归还池中，会逐渐损害连接
复用。

Response 已经返回之后，`iter_content()`、`iter_lines()` 或 `response.raw`
阶段发生的读取错误不再进入 HTTPAdapter 的 retry 循环。此类错误由 APP 按
Requests 原生异常处理。

## Requests 原生异常和状态处理

本项目不定义 Business/System 异常，也不转换 Requests 异常：

- DNS、连接、TLS、timeout 等失败抛出相应的
  `requests.exceptions.RequestException` 子类。
- `raise_on_status=False` 时，最终 4xx/5xx Response 不会自动抛异常。
- 调用 `response.raise_for_status()` 后，4xx/5xx 抛出
  `requests.exceptions.HTTPError`。
- APP 可以捕获 `requests.RequestException`，再按自己的业务规范记录日志、
  告警、降级或转换异常。

本项目不承诺统一的 attempts 或 `retry_exhausted` 元数据。需要诊断响应重试历史
时，可以在兼容场景下查看 `response.raw.retries.history`，但业务逻辑不应依赖
Requests/urllib3 的内部对象布局。

## Redirect

Redirect 完全由 Requests 原生行为管理：

```python
response = session.get(
    "https://api.example.com/start",
    allow_redirects=False,
    timeout=(3, 20),
)
```

`allow_redirects`、`session.max_redirects`、redirect history 和跨 origin 行为均
遵循 Requests。这个 factory 不阻止跨 origin redirect，也不统计 redirect
attempts。

Requests 的 Session redirect 处理与 urllib3 Retry 的 `redirect` 计数不是同一
层；不要用 Retry 的 redirect 参数替代 Requests 的 redirect 配置。

## 请求体和同步边界

本项目不检查请求体是否可重放。generator、iterator、stream 或文件在 retry 时
可能已经被消费。允许带 body 的方法重试之前，APP 必须证明 body 可 rewind 或可以
重新生成，并确认远端操作幂等。

Requests 和 urllib3 Retry 都是同步阻塞的，包括 backoff sleep。本项目不提供
async API，也不应直接在 asyncio event loop 中执行；异步 APP 应使用线程隔离或
选择原生异步 HTTP 客户端。

连接池不是 QPS、并发或令牌桶限流器。本项目没有流量控制功能。

## 配置 Session 原生能力

`create_session` 返回普通 `requests.Session`，可以直接使用 Requests API：

```python
with create_session(retry) as session:
    session.headers.update({"User-Agent": "inventory-app/1.0"})
    session.auth = ("user", "password")
    session.verify = "/path/to/ca-bundle.pem"
    session.trust_env = False
    session.max_redirects = 10

    response = session.get(
        "https://api.example.com/v1/inventory",
        timeout=(3, 20),
    )
    response.raise_for_status()
```

Cookie、proxy、client certificate、hooks 和其他行为同样直接遵循 Requests。

## 从 0.1.x 迁移

0.2.0 是一次有意的 breaking simplification。旧 API 不再提供兼容别名。

删除的公开入口包括：

- `HttpClient`
- `HttpClientConfig`
- `TimeoutConfig`
- `PoolConfig`
- `RetryConfig`
- `retry_from_dict`
- `ErrorMappingRule`
- `ErrorMappingPolicy`
- `BaseHttpError`
- `BusinessHttpError`
- `SystemHttpError`
- `NonReplayableRequestError`
- `raw_session` / `raw_client`

典型迁移：

```python
# 0.1.x
with HttpClient(config) as client:
    response = client.get("/users/123")

# 0.2.0
retry = Retry(
    total=3,
    allowed_methods={"GET", "HEAD", "OPTIONS"},
    status_forcelist={429, 500, 502, 503, 504},
    raise_on_status=False,
)

with create_session(retry) as session:
    response = session.get(
        "https://api.example.com/v1/users/123",
        timeout=(3, 20),
    )
    response.raise_for_status()
```

迁移时需要显式处理：

- 原来的 `base_url` 不再拼接；传入完整绝对 URL。
- 原来的默认 timeout 不再存在；每个请求传 `timeout=`。
- 原来的 headers、TLS、环境、Cookie 和 redirect 配置改为设置 Session 原生属性。
- 原来的 PoolConfig 被删除；factory 使用 HTTPAdapter 原生池配置。
- 原来的 Business/System 异常改为 Requests 原生异常和
  `response.raise_for_status()`。
- 原来的 attempts、`retry_exhausted` 和 URL 脱敏元数据不再提供。
- 原来的不可重放 body 检查被删除；调用方承担幂等性和 replay 安全。
- 原来的 cross-origin redirect 限制被删除；遵循 Requests 原生 redirect。
- 更早 HTTPX 版本中的 `AsyncHttpClient` 和 vendored `httpx-retries` 也不会恢复。

如业务 APP 仍需要 base URL、领域异常、结构化日志、限流或 async，它们应位于各自
业务客户端或独立组件中，而不是重新加入这个 Session factory。
