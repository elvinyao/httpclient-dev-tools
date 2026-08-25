# aiohttp 后端详细使用指南

aiohttp 后端返回原生 `aiohttp.ClientSession`，通过 aiohttp 3.13 client middleware
执行异步 retry。策略继续使用根包相同的 `urllib3.util.Retry`，因此常用参数、分类
预算和 Requests 后端保持一致；实际 I/O、Response、异常和 redirect 则保留
aiohttp 原生行为。

相关文档：[Requests 后端](usage.md)、[HTTPX 后端](httpx.md)、
[三个后端对比](comparison.md)。

## 1. 安装与公开 API

```bash
uv add 'resilient-http-client[aiohttp]'
```

```python
from resilient_http.aiohttp import Retry, create_retry, create_session
```

- `Retry` 是 `urllib3.util.Retry`。
- `create_retry()` 与根包的 Requests helper 是同一个函数。
- `create_session()` 返回原生 `aiohttp.ClientSession`，不是自定义子类。

公开 API 有意保持很小。middleware 是实现细节，不应由 APP 直接导入。

## 2. 推荐用法

`ClientSession` 必须在正在运行的 event loop 中创建，并用 `async with` 关闭：

```python
import aiohttp

from resilient_http.aiohttp import create_retry, create_session


async def get_user(user_id: str) -> dict:
    retry = create_retry()

    try:
        async with create_session(
            retry,
            timeout=(3, 20),
        ) as session:
            async with session.get(
                f"https://api.example.com/v1/users/{user_id}",
            ) as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError:
        # 这里看到的是内部 retries 结束后的最终 aiohttp 异常。
        raise
```

退避通过 `asyncio.sleep()` 完成，不阻塞事件循环。取消当前 task 会正常中断等待；
middleware 不吞掉取消异常。

## 3. Retry 策略

aiohttp 复用 Requests 后端的 helper：

```python
retry = create_retry(
    total=3,
    connect=None,
    read=None,
    status=None,
    other=0,
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    status_forcelist=frozenset({429, 500, 502, 503, 504}),
    backoff_factor=0.5,
    raise_on_status=False,
)
```

helper 还固定设置：

```python
redirect = 0
respect_retry_after_header = True
```

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `total` | `3` | 所有分类共同的总 retry 上限 |
| `connect` | `None` | 建连阶段错误的分类上限 |
| `read` | `None` | 连接建立后、返回响应头前错误的分类上限 |
| `status` | `None` | 可重试状态响应的分类上限 |
| `other` | `0` | 其他错误的分类上限；默认不重试 |
| `allowed_methods` | `GET/HEAD/OPTIONS` | 限制 read 和 status retry 的 method |
| `status_forcelist` | `429/500/502/503/504` | 强制重试状态码 |
| `backoff_factor` | `0.5` | urllib3 指数退避系数 |
| `raise_on_status` | `False` | 状态耗尽时返回末次 Response |

`connect/read/status=None` 表示不增加单独的分类限制，不是无限 retry；仍共同受
`total=3` 约束。`total=3` 表示首次发送后最多 retry 3 次，即最多发送 4 次。

每个逻辑请求从原始策略创建自己的不可变 Retry/history 状态。同一 Session 中一个
请求已经用掉的次数不会影响下一个请求，传入的 `retry.history` 也不会被修改。

### 3.1 aiohttp 异常如何进入 urllib3 分类

middleware 只用映射后的异常计算预算；最终仍向 APP 抛原始 aiohttp 异常。

| aiohttp 故障 | Retry 分类 | 默认是否重试 | 受 `allowed_methods` 限制 |
|---|---|---:|---:|
| `ConnectionTimeoutError`、普通 `ClientConnectorError`（DNS、拒绝连接等） | connect | 是 | 否 |
| 已建连后的 `ClientConnectionError`（断开、部分 protocol/read 错误） | read | 是 | 是 |
| TLS、证书、fingerprint 错误 | other | 否，`other=0` | 不适用 |
| `ClientPayloadError` | other | 否，`other=0` | 不适用 |
| 其他 `ClientError` | other | 否，`other=0` | 不适用 |

connect failure 被认为发生在请求成功发送之前，因此即使 method 是 POST，也可能按
`connect` 预算 retry。HTTPX 后端不同：它的 method gate 会阻止 POST 的 connect
retry。

read failure 可能发生在服务端已经执行请求之后。默认 POST 不进行 read/status
retry；只有远端操作幂等且 body 可重放时，才应把写方法加入集合。

> **urllib3 语义：** `allowed_methods=None` 或空集合都表示允许所有方法，不是
> “关闭 method retry”。要关闭全部 retry，请设置 `total=0`；要关闭某一类，请
> 设置相应分类预算或状态集合。

### 3.2 aiohttp 自身的额外连接 retry

aiohttp 会对部分幂等请求提供一次透明的持久连接 retry。如果它包在 middleware
外面，可能让整份 Retry 策略重新开始。factory 会关闭这次额外 retry，使
`total` 和分类预算保持准确；APP 看到的 retry 次数只由传入策略决定。

## 4. 状态响应、退避和 `Retry-After`

默认状态行为：

- `429/500/502/503/504` 在 method 允许时重试。
- 普通 400/401/403/404 和不在集合内的 5xx 立即返回。
- `raise_on_status=False` 时，状态预算耗尽后返回最终原生
  `aiohttp.ClientResponse`。
- APP 调用 `response.raise_for_status()` 后，最终 4xx/5xx 抛
  `aiohttp.ClientResponseError`。

```python
async with create_session(create_retry(), timeout=(3, 20)) as session:
    async with session.get(url) as response:
        response.raise_for_status()
        payload = await response.json()
```

如果创建 Retry 时设置 `raise_on_status=True`，可重试状态耗尽时 middleware 会
直接调用 aiohttp 的 `response.raise_for_status()`，因此抛出的仍是
`aiohttp.ClientResponseError`，不会把 urllib3 `MaxRetryError` 暴露给 APP。
这个选项不会让普通、未配置重试的 400 自动抛异常；调用方仍应统一检查最终状态。

### 4.1 指数退避

aiohttp 使用 urllib3 计算等待时间，再通过 `asyncio.sleep()` 异步等待。默认
`backoff_factor=0.5` 且连续 retry 3 次时，典型 sleep 序列约为 `0s、1s、2s`。
高级需求可以直接创建原生 Retry，配置 `backoff_max`、`backoff_jitter` 等参数。

### 4.2 `Retry-After`

`respect_retry_after_header=True` 时，middleware 优先采用 urllib3 解析的等待值：

- `Retry-After` 可按 urllib3 规则让 `413`、`429`、`503` 触发 retry，即使状态不在
  `status_forcelist` 中。
- method 仍必须被 `allowed_methods` 允许。
- 等待不受 connect/read timeout 限制；若使用原生 `ClientTimeout(total=...)`，
  aiohttp 的总 timeout 会覆盖整个逻辑请求并可中断这段等待。
- 当前 urllib3 默认 `retry_after_max` 为 21600 秒。需要更短上限时直接创建 Retry。
- 无法解析或超过允许上限的 header 会抛
  `urllib3.exceptions.InvalidHeader`。这是少数可能暴露 urllib3 policy 异常的情况。

## 5. Timeout 映射

factory 接受 Requests 风格的常用值：

```python
def create_session(retry, *, timeout=None) -> aiohttp.ClientSession: ...
```

### 5.1 factory 参数

| factory 的 `timeout` | 创建的 `aiohttp.ClientTimeout` |
|---|---|
| `None` | `total/connect/sock_connect/sock_read` 全为 `None` |
| `5` | `connect=5`、`sock_connect=5`、`sock_read=5`、`total=None` |
| `(3, 20)` | `connect=3`、`sock_connect=3`、`sock_read=20`、`total=None` |
| `(None, 20)` | 只限制 socket read |
| `(3, None)` | 只限制连接阶段 |
| `aiohttp.ClientTimeout(...)` | 原对象不变，支持全部原生字段 |

`timeout=None` 会显式覆盖 aiohttp 原生的 5 分钟 total/30 秒 socket-connect
默认值，以便与 Requests factory 一样不设置隐藏 timeout。生产环境应主动配置。

Requests 风格的数字或 tuple 中，每个非 `None` 值必须是有限且大于 0 的数字；bool、
0、负数、NaN 和无穷大都会在创建 Session 前被拒绝。这可以避免 aiohttp 把 0/负数
解释成关闭某阶段 timeout。需要完全采用 aiohttp 的高级语义时，直接传原生
`aiohttp.ClientTimeout`，factory 会原样保留。

`connect` 同时包含等待连接池连接和建立连接的阶段；factory 也把同一个值写入
`sock_connect`。`sock_read` 是两次 socket 数据读取之间的等待上限，不是完整下载
的总时间。这些 connect/read 阶段限制会应用于每一次物理 retry attempt；它们
不会把多次 attempts 和 backoff 合并成一个总计时器。

### 5.2 单次请求覆盖使用 aiohttp 原生规则

factory 支持的 `(connect, read)` tuple 只用于创建 Session。单次请求的 `timeout=`
由 aiohttp 原生 API 解析，不能直接传这个 tuple：

```python
async with create_session(create_retry(), timeout=(3, 20)) as session:
    # 省略：使用 Session 的 connect/read 配置。
    normal = await session.get(url)

    # 单次数字是覆盖整个 middleware retry 循环的累计 total timeout，
    # 不是 connect/read 各 5 秒。
    bounded = await session.get(url, timeout=5)

    # 精细覆盖使用 ClientTimeout。
    custom = await session.get(
        url,
        timeout=aiohttp.ClientTimeout(
            total=30,
            connect=2,
            sock_connect=2,
            sock_read=10,
        ),
    )

    # 单次显式 None 关闭该请求 timeout。
    unbounded = await session.get(url, timeout=None)

    normal.release()
    bounded.release()
    custom.release()
    unbounded.release()
```

普通业务代码更推荐对每个 Response 使用 `async with`，上例显式 `release()` 只是为了
集中展示覆盖形式。

### 5.3 aiohttp 可以设置真正的 total timeout

factory 的数字和二元组有意设置 `total=None`，使其与 Requests 的 connect/read
语义接近。需要限制整个逻辑请求时传原生对象：

```python
timeout = aiohttp.ClientTimeout(
    total=30,
    connect=3,
    sock_connect=3,
    sock_read=10,
)

async with create_session(create_retry(), timeout=timeout) as session:
    async with session.get(url) as response:
        response.raise_for_status()
```

aiohttp 的 `total` 是整个高层请求的累计限制，覆盖 retry attempts、middleware
backoff、redirect 和响应体消费。这是 Requests/HTTPX factory 本身没有的能力。

## 6. Redirect

aiohttp 的 GET/普通 request 默认 `allow_redirects=True`，HEAD 默认
`allow_redirects=False`；redirect 上限默认 `max_redirects=10`：

```python
async with create_session(create_retry(), timeout=(3, 20)) as session:
    async with session.get(
        url,
        allow_redirects=False,
    ) as response:
        ...
```

`create_retry()` 的 `redirect=0` 只关闭 urllib3 Retry 自身的 redirect 计数；redirect
仍由 aiohttp 管理。每个 redirect hop 会形成新的 aiohttp request 并获得完整 retry
预算，因此自动跳转可能让总发送次数超过 `total + 1`。历史响应位于
`response.history`。原生 `ClientTimeout.total` 会覆盖整个 redirect chain。

## 7. Response body、streaming 与重放

aiohttp Response 默认以流式方式提供 body。必须读取、释放或关闭 Response：

```python
async with create_session(create_retry(), timeout=(3, 20)) as session:
    async with session.get(download_url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(64 * 1024):
            await process(chunk)
```

middleware 在收到响应头后就把 Response 返回给 APP。此后 `response.read()`、
`response.json()` 或 `response.content` 迭代期间发生的 read timeout、reset、截断
不会透明 retry。context manager 会负责释放或关闭连接。

状态 retry 发生时，middleware 会先 release 被隐藏的 Response，避免它长期占用
连接池。

middleware 会再次调用同一个 aiohttp request。bytes 等稳定、已缓冲 body 可以
重放；异步生成器、文件流和其他 streaming upload 可能已经被消费。启用 POST、
PUT、PATCH 或 DELETE retry 前，应确认操作幂等、使用稳定幂等键，并确保 body 可
重复读取。

## 8. 原生异常与业务转换

transport retry 耗尽时，middleware 重新抛最后一次原始 aiohttp 异常，而不是
urllib3 映射异常。常见类型包括：

- `aiohttp.ConnectionTimeoutError`
- `aiohttp.ClientConnectorError`
- `aiohttp.ServerDisconnectedError`
- `aiohttp.SocketTimeoutError`
- `aiohttp.ClientSSLError`
- `aiohttp.ClientPayloadError`

URL、redirect 和 HTTP 状态也继续使用 aiohttp 原生异常，例如
`aiohttp.InvalidURL`、`aiohttp.TooManyRedirects`、
`aiohttp.ClientResponseError`。

```python
try:
    async with create_session(create_retry(), timeout=(3, 20)) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            result = await response.json()
except aiohttp.ClientResponseError as error:
    status = error.status
    # 按最终 HTTP 状态记录日志并转换业务异常。
    raise
except (aiohttp.ServerTimeoutError, aiohttp.ClientConnectionError):
    # transport retries 已结束。
    raise
except aiohttp.ClientError:
    raise
```

唯一重要例外是 Retry policy 自身的 `Retry-After` 解析错误，它可能抛
`urllib3.exceptions.InvalidHeader`，需要严格边界捕获时应单独考虑。

## 9. Session 和连接池生命周期

每次调用 `create_session()` 都创建独立的原生 ClientSession、connector 和连接池。
不同调用不共享 Cookie、连接、关闭状态或 retry history。

应在一组相关请求内复用 Session：

```python
async with create_session(create_retry(), timeout=(3, 20)) as session:
    async with session.get(first_url) as first:
        first.raise_for_status()
        first_data = await first.json()

    async with session.get(second_url) as second:
        second.raise_for_status()
        second_data = await second.json()
```

同一 Session 会共享连接池，但 retry 不保证沿用同一条 TCP 连接；失效连接可以被
丢弃并重新建立。

ClientSession 绑定到创建它的 event loop，不应跨 event loop、线程或进程传递。
忘记 `await session.close()` 会产生资源警告并泄漏连接；优先使用 `async with`。

factory 有意只接收 retry 和 timeout，不转发 `base_url`、自定义 connector、默认
auth、trace config 或其他 ClientSession 构造参数。headers、auth、proxy、TLS 等
单次请求选项仍可使用 aiohttp 原生 API；如果必须控制 Session 构造和 connector，
应在应用中单独设计 middleware 组合，而不是依赖本项目的私有 middleware。

单次请求的 `middlewares=` 会**替换** Session middleware，不是追加。传入空 tuple
可以显式绕过 retry：

```python
async with create_session(create_retry(), timeout=(3, 20)) as session:
    async with session.get(url, middlewares=()) as response:
        # 这一请求使用 aiohttp 原生行为，不执行本项目 retry。
        ...
```

传入只包含 APP 自定义 middleware 的列表同样会移除 retry。由于本项目的 retry
middleware 是私有实现，普通调用应省略这个参数；如果需要自定义 middleware
组合，应在更高层明确设计顺序和 retry 边界。

## 10. 生产检查清单

- 安装了 `[aiohttp]` extra，并在运行中的 event loop 内创建 Session。
- 显式配置 timeout；需要严格总 SLA 时使用 `ClientTimeout(total=...)`。
- `total`、`connect`、`read`、`status`、`other` 符合调用链预算。
- 理解 connect retry 不受 `allowed_methods` 限制。
- 最终 Response 会调用 `raise_for_status()` 或显式检查 `response.status`。
- 已评估 backoff、`Retry-After` 与 `retry_after_max`。
- 每个 Response 都被读取、release 或关闭。
- 可重试写操作有稳定幂等键，body 确实可重放。
- Session 不跨 event loop 使用，退出时一定关闭。
