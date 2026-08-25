# HTTPX 后端详细使用指南

HTTPX 后端同时支持同步 `httpx.Client` 和异步 `httpx.AsyncClient`。它使用
`httpx-retries` 0.4.6 的原生 `Retry` 与 `RetryTransport`，不包装 Response，
也不把 HTTPX 异常转换为 Requests 或业务异常。

相关文档：[Requests 后端](usage.md)、[aiohttp 后端](aiohttp.md)、
[三个后端对比](comparison.md)。

## 1. 安装与公开 API

```bash
uv add 'resilient-http-client[httpx]'
```

HTTPX API 位于独立子模块：

```python
from resilient_http.httpx import (
    Retry,
    create_async_client,
    create_client,
    create_retry,
)
```

- `Retry` 是原生 `httpx_retries.Retry`，不是 `urllib3.util.Retry`。
- `create_retry()` 创建一份保守的常用策略。
- `create_client()` 返回原生同步 `httpx.Client`。
- `create_async_client()` 返回原生异步 `httpx.AsyncClient`。

不要从根包导入 Retry 再传给 HTTPX factory。根包的 `Retry` 属于 urllib3，类型不
兼容；factory 会在创建连接池前抛出清楚的 `TypeError`。

## 2. 推荐用法

### 2.1 同步 Client

```python
import httpx

from resilient_http.httpx import create_client, create_retry


def get_user(user_id: str) -> dict:
    retry = create_retry()

    try:
        with create_client(retry, timeout=(3, 20)) as client:
            response = client.get(
                f"https://api.example.com/v1/users/{user_id}",
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        # 这里看到的是所有内部 attempts 结束后的最终 HTTPX 异常。
        raise
```

### 2.2 异步 Client

```python
import httpx

from resilient_http.httpx import create_async_client, create_retry


async def get_user(user_id: str) -> dict:
    retry = create_retry()

    try:
        async with create_async_client(
            retry,
            timeout=(3, 20),
        ) as client:
            response = await client.get(
                f"https://api.example.com/v1/users/{user_id}",
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        raise
```

同步和异步 factory 使用相同 Retry 配置。同步退避会阻塞当前线程；异步退避通过
`asyncio.sleep()` 完成，不阻塞事件循环。

## 3. `create_retry()`

helper 的完整常用参数如下：

```python
retry = create_retry(
    total=3,
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    status_forcelist=frozenset({429, 500, 502, 503, 504}),
    retry_on_exceptions=(
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    ),
    backoff_factor=0.5,
)
```

helper 还固定设置：

```python
respect_retry_after_header = True
backoff_jitter = 0.0
```

参数说明：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `total` | `3` | 首次发送之后允许的总重试次数 |
| `allowed_methods` | `GET/HEAD/OPTIONS` | 哪些方法可以进入整个 RetryTransport 重试循环 |
| `status_forcelist` | `429/500/502/503/504` | 哪些最终响应状态触发重试 |
| `retry_on_exceptions` | 六种常见 transport 异常 | 哪些 transport 异常触发重试 |
| `backoff_factor` | `0.5` | 指数退避缩放系数 |

`total=3` 表示首次发送后最多 retry 3 次，即最多发送 4 次。每个逻辑请求从
`attempts_made=0` 开始；一个请求已经消耗的次数不会影响同一 Client 中的下一请求。

### 3.1 HTTPX 只有总预算

`httpx_retries.Retry` 0.4.6 只有 `total`，没有 urllib3 的 `connect`、`read`、
`status`、`other` 分类预算。状态响应和所有允许的异常共同消耗同一个总预算，不能
配置“最多 connect retry 3 次，但 status retry 1 次”。

如果业务必须使用分类预算，请选择 Requests/aiohttp，或在业务层实现明确状态机；
不要把 urllib3 Retry 传给 HTTPX factory。

### 3.2 `allowed_methods` 会拦截所有 retry

HTTPX 的 `RetryTransport` 在进入重试循环之前先检查 method。因此
`allowed_methods` 同时限制：

- 状态码 retry。
- connect timeout、DNS、连接拒绝等建连异常 retry。
- read timeout、断开连接和其他配置过的 transport 异常 retry。

所以默认策略下，POST 即使在建立连接前失败也不会 retry。这一点与 Requests 和
aiohttp 不同；后两者的 urllib3 connect 分类不受 `allowed_methods` 限制。

helper 会拒绝空的 `allowed_methods`，因为 httpx-retries 0.4.6 会把空集合恢复成
其上游默认集合，容易意外扩大重试范围。要关闭全部 retry，请使用：

```python
retry = create_retry(total=0)
```

### 3.3 禁用状态 retry

`None` 或空集合会被 helper 解释成“禁用所有 HTTP 状态 retry”：

```python
retry = create_retry(status_forcelist=())
```

内部策略会使用一个不可能出现的状态值保存这个意图。这是为了避免
httpx-retries 0.4.6 把空集合替换成自己的默认状态集合。

### 3.4 默认异常范围

默认重试：

- `httpx.ConnectError`
- `httpx.ConnectTimeout`
- `httpx.ReadError`
- `httpx.ReadTimeout`
- `httpx.RemoteProtocolError`
- `httpx.ProxyError`

默认不重试未列出的异常，例如 `httpx.WriteError`、`httpx.WriteTimeout`、
`httpx.PoolTimeout`、URL 错误或程序自身异常。TLS/证书握手失败在 HTTPX 中可能
包装为已列入的 `httpx.ConnectError`，这种情况仍可能 retry，应按最终实际异常类型
判断。需要扩大范围时必须同时确认 method 幂等性和 body 可重放：

```python
retry = create_retry(
    retry_on_exceptions=(
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
        httpx.WriteTimeout,
    ),
)
```

显式传入 `None` 或空集合表示禁用全部 transport 异常 retry，避免
httpx-retries 把 `None` 恢复为更宽的上游默认异常集合：

```python
retry = create_retry(retry_on_exceptions=None)
```

这不会关闭状态码 retry；要关闭全部 retry，请设置 `total=0`。

## 4. 退避和 `Retry-After`

忽略 `Retry-After` 时，httpx-retries 0.4.6 使用：

```text
backoff_factor * 2 ** attempts_made
```

helper 关闭 jitter，因此默认 `backoff_factor=0.5` 的前三次 retry 典型等待约为
`1s、2s、4s`。原生 Retry 默认将每次等待限制在 `max_backoff_wait=120s`。

`respect_retry_after_header=True` 时，重试响应上的有效 `Retry-After` 优先于指数退避，
并同样受 `max_backoff_wait` 限制。需要注意：

- HTTPX 只有在状态已位于 `status_forcelist` 时才会 retry；`Retry-After` 本身不会
  让其他状态进入 retry。
- 无法解析的 header 会记录 warning 并退回指数退避，不会抛 policy 异常。
- timeout 不会自动截断同步 sleep；异步 Client 的 sleep 可以被外层取消。
- helper 的固定 jitter 为 0。大量实例同时访问同一上游时，建议直接创建原生
  Retry 并配置 jitter。

高级配置示例：

```python
import httpx

from resilient_http.httpx import Retry, create_client


retry = Retry(
    total=5,
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    status_forcelist=frozenset({429, 500, 502, 503, 504}),
    retry_on_exceptions=(
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    ),
    backoff_factor=0.5,
    backoff_jitter=0.25,
    max_backoff_wait=30,
    respect_retry_after_header=True,
)

with create_client(retry, timeout=(3, 20)) as client:
    response = client.get("https://api.example.com/health")
```

在 httpx-retries 0.4.6 中，`backoff_jitter` 是 `0` 到 `1` 的比例；它与 urllib3
的“附加随机秒数”语义不同。

## 5. Timeout

factory 的主要签名是：

```python
def create_client(retry, *, timeout=None, transport=None) -> httpx.Client: ...


def create_async_client(retry, *, timeout=None, transport=None) -> httpx.AsyncClient: ...
```

timeout 可传数字、`httpx.Timeout`，或 2/3/4 元 tuple。factory 会把 tuple 主动
规范化为 `httpx.Timeout` 的 connect/read/write/pool 字段；其他长度立即抛
`TypeError`。

### 5.1 默认值与覆盖

`timeout=None` 会明确关闭 HTTPX 原生的 5 秒默认 timeout，以便与 Requests
factory 的“没有隐藏 timeout”约定一致：

```python
with create_client(create_retry()) as client:
    # connect/read/write/pool 均没有 timeout。
    response = client.get(url)
```

生产环境应主动配置：

```python
with create_client(create_retry(), timeout=(3, 20)) as client:
    # 2 元 tuple：connect=3、read=20，write/pool 不设限制。
    normal = client.get(url)

    # 单次数字覆盖：四个阶段都为 5 秒。
    bounded = client.get(url, timeout=5)

    # 单次显式 None：四个阶段均关闭 timeout。
    unbounded = client.get(url, timeout=None)
```

需要独立控制四个阶段时使用原生类型：

```python
timeout = httpx.Timeout(
    connect=3,
    read=20,
    write=10,
    pool=2,
)

with create_client(create_retry(), timeout=timeout) as client:
    response = client.get(url)
```

### 5.2 timeout 不是整个 retry 的 deadline

HTTPX timeout 分别限制 connect、read、write 和 pool 操作，并在每次物理 attempt
中重新适用。多个 attempts、backoff 和 `Retry-After` 会累加；`timeout=20` 不代表
整个逻辑请求一定在 20 秒内结束。

需要总 SLA 时，应在更高层使用同步 deadline，或在异步调用外使用 Python 3.11+
的 `asyncio.timeout()`/应用框架的取消机制。Python 3.9/3.10 可以使用
`asyncio.wait_for()`。无论采用哪种方式，都要评估取消时上游操作是否可能已经执行。

## 6. 状态响应与原生异常

httpx-retries 0.4.6 没有 urllib3 的 `raise_on_status`。状态 retry 耗尽后总是返回
最后一个原生 `httpx.Response`：

```python
with create_client(create_retry(), timeout=(3, 20)) as client:
    response = client.get(url)
    response.raise_for_status()
```

- 普通 400/403 等未配置状态立即返回。
- 429/500/502/503/504 先 retry，耗尽后返回末次响应。
- `response.raise_for_status()` 对最终 4xx/5xx 抛
  `httpx.HTTPStatusError`。
- transport retry 耗尽后重新抛最后一次原生 HTTPX 异常，例如
  `httpx.ConnectError` 或 `httpx.ReadTimeout`。

业务层可以只在最终结果失败时记录一次日志：

```python
try:
    with create_client(create_retry(), timeout=(3, 20)) as client:
        response = client.get(url)
        response.raise_for_status()
except httpx.HTTPStatusError as error:
    status = error.response.status_code
    # 按最终状态映射业务异常。
    raise
except (httpx.TimeoutException, httpx.NetworkError):
    # 所有内部 transport attempts 已结束。
    raise
except httpx.HTTPError:
    raise
```

## 7. Redirect

HTTPX Client 默认 `follow_redirects=False`，与 Requests/aiohttp 默认自动跟随不同：

```python
with create_client(create_retry(), timeout=(3, 20)) as client:
    response = client.get(url, follow_redirects=True)
```

redirect 由 HTTPX Client 层管理，不消耗某个物理请求内部的 Retry `total`。如果启用
自动 redirect，每个 redirect hop 都是新的 transport 请求，并获得完整 retry
预算，因此总发送次数可能明显超过 `total + 1`。历史响应位于
`response.history`。

## 8. Streaming 与 body replay

HTTPX 使用显式 stream context：

```python
with create_client(create_retry(), timeout=(3, 20)) as client:
    with client.stream("GET", download_url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            process(chunk)
```

异步版本使用 `async with client.stream(...)` 和 `async for`。

`RetryTransport` 只能看到 transport 返回响应头之前的异常。响应头已返回后，在
`.read()`、`.iter_bytes()` 或异步 body 消费期间发生的 timeout、reset、截断不会
透明 retry。context manager 会确保 Response 被关闭或连接归还。

RetryTransport 会重新发送同一个 `httpx.Request`。bytes、JSON 等已缓冲 body
通常可重放；generator、iterator、文件流和其他 streaming upload 可能已经被消费，
不能假定可重放。允许 POST/PUT/PATCH/DELETE retry 前，必须同时确认：

1. 远端操作幂等，或使用稳定的幂等键。
2. 每次 attempt 的 body 内容完全相同且可再次读取。
3. 首次请求可能已执行但响应丢失时，再次发送仍安全。

## 9. Client 与连接池生命周期

默认情况下，每次 factory 调用都会创建新的 Client、`RetryTransport`、底层 HTTP
transport 和连接池。两个默认 factory 调用不共享 Cookie、认证、连接池或关闭状态。

```python
with create_client(create_retry(), timeout=(3, 20)) as client:
    first = client.get(first_url)
    second = client.get(second_url)
```

同一 Client 中的多个请求共享 Client 状态和连接池，但每个逻辑请求都有独立 retry
计数。连接可能复用，也可能在失效后由后续 attempt 新建。

异步 Client 必须关闭：

```python
async with create_async_client(create_retry(), timeout=(3, 20)) as client:
    response = await client.get(url)
```

不要为每个小请求都新建 Client；应在一个明确的业务调用、worker 或应用生命周期
内复用，并遵循 HTTPX 对线程、任务和 event loop 的原生约束。

### 9.1 注入原生 transport

proxy、TLS/mTLS、HTTP/2、连接上限、UDS 或 local address 等底层配置可通过
`transport=` 注入。factory 负责只包装一层 `RetryTransport`：

```python
import httpx

from resilient_http.httpx import create_client, create_retry


transport = httpx.HTTPTransport(
    proxy="http://proxy.example.com:8080",
    verify=True,
    limits=httpx.Limits(max_connections=50),
)

with create_client(
    create_retry(),
    transport=transport,
    timeout=(3, 20),
) as client:
    response = client.get("https://api.example.com/health")
```

异步 factory 对应传 `httpx.AsyncHTTPTransport`。只能传尚未包装的同步/异步原生
transport；传错类型或已经是 `RetryTransport` 会立即抛 `TypeError`，防止双层 retry。

transport 的所有权会转交给返回的 Client；关闭 Client 会一并关闭它。因此不要把同一
transport 传给多个 factory，也不要在 Client 存活期间自行关闭。

> **环境 proxy：** factory 必须把自定义 `RetryTransport` 交给 HTTPX Client，因而
> HTTPX Client 层的 `HTTP_PROXY`/`HTTPS_PROXY` 自动 mount 发现不会生效。需要 proxy
> 时应像上例一样显式构造 `HTTPTransport(proxy=...)`。这不等同于关闭 HTTPTransport
> 对证书环境变量等其他 `trust_env` 行为。

factory 不转发 `base_url`、默认 headers/auth/cookies 或 event hooks 等 Client 层
参数。需要这些参数时，直接用 `httpx.Client`/`AsyncClient` 与原生
`httpx_retries.RetryTransport` 组合；不要再把已经包装的 RetryTransport 传回本
factory。

## 10. 生产检查清单

- 安装了 `[httpx]` extra，并从 `resilient_http.httpx` 导入 Retry。
- 显式配置了 timeout，理解它不是总 deadline。
- 接受只有 `total`、没有分类预算的限制。
- 已确认 `allowed_methods` 会同时限制 connect 和 status retry。
- 最终 Response 会调用 `raise_for_status()` 或显式检查状态。
- 已评估指数退避、`Retry-After` 和 `max_backoff_wait`。
- streaming response 总在 context manager 中消费或关闭。
- 可重试写请求使用幂等键，且 body 确实可重放。
- 依赖环境 proxy 时改为显式注入配置了 proxy 的原生 transport。
- async APP 使用 `create_async_client()`，不会在事件循环中运行同步 Client。
