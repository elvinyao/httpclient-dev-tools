# Resilient HTTP Client 详细使用指南

本文面向在业务 APP 中使用 `resilient-http-client` 的开发者，说明 0.3.0
版本的配置方式、实际重试语义、timeout、异常处理和生产环境注意事项。

## 1. 这个库负责什么

这个库是 Requests 和 urllib3 Retry 之间的一层很薄的 factory，只公开三个入口：

```python
from resilient_http import Retry, create_retry, create_session
```

- `Retry`：原生 `urllib3.util.Retry`，供高级配置直接使用。
- `create_retry()`：用少量常用参数创建一份保守的 Retry 策略。
- `create_session()`：创建新的 `requests.Session`，为 HTTP 和 HTTPS 挂载 Retry，
  并可选择配置 Session 默认 timeout。

这个库不负责 base URL、业务异常、日志、限流、总 deadline、异步请求或逐状态码的
不同重试次数。这些能力应由业务客户端或其他独立组件提供。

## 2. 安装

使用 uv：

```bash
uv add resilient-http-client
```

如果正在本地联调尚未发布的源码，可以从业务项目中添加本地路径：

```bash
uv add --editable /path/to/namagi-dev-tools
```

支持 Python 3.9 及以上版本。

## 3. 推荐的最简用法

```python
import requests

from resilient_http import create_retry, create_session


retry = create_retry()

try:
    with create_session(retry, timeout=(3, 20)) as session:
        response = session.get("https://api.example.com/v1/users/123")
        response.raise_for_status()
        user = response.json()
except requests.RequestException as error:
    # 记录日志、告警、降级或转换为业务异常。
    raise
```

这里有四个重要行为：

1. `create_retry()` 默认最多重试 3 次；不考虑自动 redirect 时，单个 HTTP
   请求最多发送 4 次。
2. `(3, 20)` 分别是每一次物理尝试的 connect timeout 和 read timeout。
3. 状态重试耗尽后默认返回最后一个 Response；APP 主动调用
   `response.raise_for_status()` 才会对 4xx/5xx 抛出 `HTTPError`。
4. 退出 `with` 时 Session 和连接池会被关闭。

`retry` 是 `create_session()` 的必填参数。库不会隐藏一个调用方看不到的默认重试
策略。

## 4. `create_retry()` 参数

完整的 helper 调用如下：

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

参数含义：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `total` | `3` | 首次发送之后允许的总重试次数；它是所有分类共同的总上限 |
| `connect` | `None` | DNS、连接拒绝、connect timeout 等连接阶段错误的分类上限 |
| `read` | `None` | 请求发出后、收到完整响应头前发生的 read/protocol 错误分类上限 |
| `status` | `None` | 可重试 HTTP 状态响应的分类上限 |
| `other` | `0` | 无法归入前述分类的错误上限；默认立即停止，避免意外重试 |
| `allowed_methods` | `GET/HEAD/OPTIONS` | 允许进行状态和 read retry 的 HTTP 方法 |
| `status_forcelist` | `429/500/502/503/504` | 在方法允许时触发状态重试的状态码 |
| `backoff_factor` | `0.5` | 指数退避的缩放系数 |
| `raise_on_status` | `False` | 状态重试耗尽时返回最终 Response，而不是直接抛 `RetryError` |

`connect=None`、`read=None`、`status=None` 表示没有额外设置该分类的独立上限，
不是无限重试；它们仍然共同受 `total=3` 限制。

每次调用 `create_retry()` 都会返回一个独立的原生 Retry 对象。Retry 在执行时采用
不可变式更新：urllib3 会为每个逻辑请求创建带有独立 history 的新对象，不会把前
一个请求用掉的次数累积到后一个请求。

### 4.1 重试次数如何计算

`total` 指的是 retry 次数，不包括首次发送：

| `total` | 最多 retry 次数 | 单个 redirect hop 最多发送次数 |
|---:|---:|---:|
| `0` | 0 | 1 |
| `1` | 1 | 2 |
| `3` | 3 | 4 |

这里的发送次数不包含 Requests 自动 redirect 产生的新请求。每个 redirect hop
都可能执行自己的 retry，因此一个包含多个跳转的高层 `session.get()` 调用可能发送
超过表中的次数。

分类上限与 `total` 同时生效，先耗尽的限制会停止重试。例如：

```python
retry = create_retry(
    total=5,
    connect=3,
    read=1,
    status=2,
)
```

- 一串连接错误最多重试 3 次。
- 一串 read 错误最多重试 1 次。
- 一串可重试状态响应最多重试 2 次。
- 混合故障无论如何最多总计重试 5 次。

urllib3 Retry 不支持“500 重试 2 次、429 重试 5 次”这种逐状态码次数配置。如果
确实需要，应在更高层实现明确的业务状态机，同时谨慎处理重复请求和总 deadline。

### 4.2 默认会重试哪些故障

| 故障或结果 | 默认是否重试 | 是否受 `allowed_methods` 限制 |
|---|---:|---:|
| DNS 失败、连接拒绝、connect timeout | 是，受 `connect`/`total` 限制 | 否 |
| proxy 建连失败 | 是，受相应连接分类和 `total` 限制 | 否 |
| 收到响应头前的 read timeout、连接 reset、远端提前关闭 | 是，受 `read`/`total` 限制 | 是 |
| `429/500/502/503/504` 响应 | 是，受 `status`/`total` 限制 | 是 |
| 其他普通 4xx，例如 `400/401/403/404` | 否 | 不适用 |
| 未列入策略的其他 5xx，例如 `501/505` | 否 | 不适用 |
| TLS/SSL 等 other 错误 | 默认否，因为 `other=0` | 不适用 |
| 响应头已收到、进入 body 消费阶段后的 timeout、reset 或截断 | 否 | 不适用 |

connect failure 发生在请求成功发送到服务端之前，因此它不受
`allowed_methods` 限制。也就是说，默认策略下 POST 遇到连接阶段失败仍可能重试；
POST 的状态响应和 read failure 默认不会重试。

read failure 可能发生在服务端已经接收并执行请求之后。只有在远端操作幂等、请求体
可重放时，才应把写操作加入 `allowed_methods`。

> **注意：** `allowed_methods=None` 和空集合在 urllib3 中都代表允许所有 HTTP
> 方法，不是“使用默认方法集合”或“禁用所有方法”。不要用空集合关闭重试；如果
> 只想采用本项目默认值，请省略这个参数。要关闭某类 retry，请设置对应的
> `read`、`status`、`status_forcelist` 或 `total`。

### 4.3 指数退避

urllib3 的退避底数固定为 2，`backoff_factor` 是缩放系数，不是指数本身。忽略
`Retry-After` 和 jitter 时，连续错误的典型等待时间为：

```text
backoff_factor * 2 ** (连续错误次数 - 1)
```

第一次 retry 通常不等待。使用默认 `backoff_factor=0.5` 且连续重试 3 次时，
典型 sleep 序列是约 `0s、1s、2s`。实际请求耗时还包括 DNS、建连、发送、读取和
服务端响应时间。

大量实例同时访问同一服务时，建议用原生 Retry 增加 jitter，避免所有实例在相同
时间再次发起请求。

### 4.4 `Retry-After`

`create_retry()` 固定使用 `respect_retry_after_header=True`。对适用响应，urllib3
会优先按服务端 `Retry-After` 等待，而不是使用普通指数退避。

需要特别注意：

- 带有有效 `Retry-After` 的 `413`、`429`、`503` 可能触发重试；因此即使默认
  `status_forcelist` 没有 `413`，带该 header 的 413 仍可能重试。
- `Retry-After` 等待不受 connect/read timeout 限制。
- 在当前 urllib3 依赖范围内，原生 `retry_after_max` 默认可接受最长 21600 秒，
  即 6 小时。
- 无法解析的 `Retry-After` 不会被忽略，而会由 Requests 抛出原生
  `requests.exceptions.InvalidHeader`。

如果业务不能接受服务端指定的长等待，应直接创建原生 Retry，设置更小的
`retry_after_max`，或关闭 `respect_retry_after_header`；如果需要严格控制一次
业务调用的总耗时，还必须在更高层实现总 deadline。

## 5. 常见 Retry 配置

### 5.1 减少重试和退避

```python
retry = create_retry(
    total=2,
    status=2,
    backoff_factor=0.2,
)
```

### 5.2 完全关闭 retry，但仍使用 Session factory

```python
retry = create_retry(total=0)

with create_session(retry, timeout=(3, 20)) as session:
    response = session.get(url)
```

### 5.3 允许带幂等键的 POST 状态重试

只有服务端明确支持幂等键，并且 body 可以重新发送时才这样配置：

```python
retry = create_retry(
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS", "POST"}),
)

with create_session(retry, timeout=(3, 20)) as session:
    response = session.post(
        "https://api.example.com/v1/payments",
        headers={"Idempotency-Key": operation_id},
        json={"amount": 1000, "currency": "JPY"},
    )
    response.raise_for_status()
```

幂等键必须在同一次逻辑操作的所有 attempts 中保持相同。不要在 retry 时生成新的
key。

### 5.4 使用原生 Retry 高级参数

helper 有意只覆盖常用参数。需要 backoff 上限、jitter 或 Retry-After 控制时，
直接使用公开导出的原生 Retry：

```python
from resilient_http import Retry, create_session


retry = Retry(
    total=5,
    connect=5,
    read=2,
    redirect=0,
    status=3,
    other=0,
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    status_forcelist=frozenset({429, 500, 502, 503, 504}),
    backoff_factor=0.5,
    backoff_max=10.0,
    backoff_jitter=0.25,
    respect_retry_after_header=True,
    retry_after_max=30,
    raise_on_status=False,
)

with create_session(retry, timeout=(3, 20)) as session:
    response = session.get(url)
```

`create_session()` 不会包装或修改传入的 Retry。

## 6. `create_session()` 和 Session 生命周期

主要签名在 Python 3.9 兼容写法下是：

```python
def create_session(
    retry: Retry,
    *,
    timeout: Optional[Union[float, tuple[Optional[float], Optional[float]]]] = None,
) -> requests.Session: ...
```

每次调用都会创建：

- 一个全新的 Session。
- 分别用于 `http://` 和 `https://` 的两个全新 HTTPAdapter。
- 由这些 Adapter 管理的全新连接池。

不同的 `create_session()` 调用不会共享 Cookie、headers、auth、hooks、Adapter 或
连接池。关闭第一个 Session 不会影响第二个：

```python
first = create_session(retry)
second = create_session(retry)

assert first is not second

first.close()
try:
    response = second.get(url, timeout=(3, 20))
finally:
    second.close()
```

推荐用 `with` 管理生命周期：

```python
with create_session(retry, timeout=(3, 20)) as session:
    first = session.get("https://api.example.com/v1/first")
    first.raise_for_status()

    second = session.get("https://api.example.com/v1/second")
    second.raise_for_status()
```

同一个 `with` 中的请求有意共享 Session 状态和连接池，并可能复用同一 origin 的
空闲 TCP 连接。Session 并不代表固定使用一条 TCP 连接；连接失效后，retry 可以
丢弃旧连接并建立新连接。

一个 Session 不应在不受控制的多个线程之间共享，也不应序列化后传到其他进程。
通常应为每个 worker 或清晰的业务调用范围创建并关闭自己的 Session。

## 7. Timeout 的配置和真实含义

### 7.1 Session 默认值和单次覆盖

```python
with create_session(create_retry(), timeout=(3, 20)) as session:
    # 省略 timeout：使用 Session 默认值 (3, 20)。
    normal = session.get("https://api.example.com/normal")

    # 显式传值：只覆盖本次请求。
    fast = session.get(
        "https://api.example.com/fast",
        timeout=(1, 5),
    )

    # 显式传 None：只关闭本次请求的 timeout。
    unbounded = session.get(
        "https://api.example.com/long-running",
        timeout=None,
    )
```

规则汇总：

| Session 创建方式 | 单次请求写法 | 最终行为 |
|---|---|---|
| `create_session(retry)` | 省略 `timeout` | Requests 不设置 connect/read timeout，可能无限等待 |
| `create_session(retry, timeout=None)` | 省略 `timeout` | Requests 不设置 connect/read timeout，可能无限等待 |
| `create_session(retry, timeout=(3, 20))` | 省略 `timeout` | 使用 `(3, 20)` |
| 同上 | `timeout=5` | 本次 connect/read 均使用 5 秒 |
| 同上 | `timeout=(1, 10)` | 本次 connect 为 1 秒、read 为 10 秒 |
| 同上 | `timeout=None` | 本次关闭 timeout |

直接使用 `session.send(prepared_request, ...)` 时同样遵循省略、覆盖和显式 `None`
规则。

生产环境通常不建议使用 `timeout=None`。它只适合少数明确不设置 Requests
connect/read timeout、并接受可能无限等待的操作，不应作为普通 API 调用的默认
选择。

### 7.2 timeout 不是总 deadline

- 数字 timeout 同时用于 connect 和 read。
- tuple 使用 `(connect_timeout, read_timeout)` 顺序。
- timeout 分别应用于每一次物理 retry attempt。
- read timeout 是 socket 在读取期间等待数据的限制，不是下载完整响应体的总时间。
- backoff sleep、`Retry-After` 和多次 attempts 不共享一个总计时器。

因此：

```python
retry = create_retry(total=3, backoff_factor=0.5)
```

配合 `timeout=(3, 20)` 并不代表调用会在 23 秒内结束。最多 4 次发送、每次的
connect/read 等待、退避和 `Retry-After` 都可能累加，使总耗时远大于 23 秒。

如果调用必须在固定 SLA 内完成，应在业务层增加总 deadline，并确保取消或超时后
不会继续进行无意义的 retry。

## 8. HTTP 状态和异常处理

这个库保留 Requests 原生 Response 和异常，不定义业务异常。

### 8.1 `raise_on_status=False` 的推荐流程

```python
with create_session(create_retry(), timeout=(3, 20)) as session:
    response = session.get(url)
    response.raise_for_status()
```

- 非重试状态，例如 400 或 403，会立即返回 Response。
- 可重试状态，例如 503，会先执行策略允许的 retry。
- 可重试状态耗尽后，返回最后一个 Response。
- `response.raise_for_status()` 对最终 4xx/5xx 抛出
  `requests.exceptions.HTTPError`。
- 1xx、2xx 和 3xx 不会被 `raise_for_status()` 当作错误。

这种模式便于 APP 根据最终状态码统一记录日志和转换业务异常。

### 8.2 `raise_on_status=True`

```python
retry = create_retry(raise_on_status=True)
```

此时状态重试耗尽通常会在 `session.get()` 内直接抛
`requests.exceptions.RetryError`，调用方不一定能得到最终 Response。如果业务
需要根据最终的 4xx/5xx 状态做异常映射，通常保持默认 `False` 更简单。

### 8.3 transport、policy 和 redirect 异常

transport 失败时，调用方通常拿不到可用的 HTTP Response，
`response.raise_for_status()` 也不会执行，因为 `session.get()` 已经抛出异常。
常见 transport 类型包括：

- `requests.exceptions.ConnectTimeout`
- `requests.exceptions.ConnectionError`
- `requests.exceptions.ReadTimeout`
- `requests.exceptions.ProxyError`
- `requests.exceptions.SSLError`

policy 或 redirect 也可能让 `session.get()` 直接失败：

- `requests.exceptions.RetryError`
- `requests.exceptions.TooManyRedirects`

状态 retry 耗尽产生的 `RetryError` 可能发生在已经收到多个 HTTP 响应之后；
`TooManyRedirects` 也表示已经收到一串 redirect 响应。它们不是连接错误，不能假定
与 transport 异常有相同的处理方式。

URL 配置错误还可能产生 `MissingSchema`、`InvalidSchema` 或 `InvalidURL`。它们都
属于 `requests.exceptions.RequestException` 体系，边界层可以先统一捕获
`RequestException`，再按业务需要细分。

### 8.4 在 APP 中记录最终失败并转换异常

下面是一个示例策略：最终 5xx 记录 critical，最终 4xx 记录 error，连接或 timeout
也视为系统错误。具体分类应按业务语义调整，例如 401、403、404 和 429 在不同系统
中可能有不同含义。

```python
import logging

import requests

from resilient_http import create_retry, create_session


logger = logging.getLogger(__name__)


class BusinessApiError(Exception):
    pass


class SystemApiError(Exception):
    pass


def get_user(user_id: str) -> dict:
    retry = create_retry()

    try:
        with create_session(retry, timeout=(3, 20)) as session:
            response = session.get(
                f"https://api.example.com/v1/users/{user_id}",
            )
            response.raise_for_status()
            return response.json()
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None

        if status is not None and 400 <= status < 500:
            logger.error(
                "Upstream rejected the request",
                extra={"status_code": status},
                exc_info=True,
            )
            raise BusinessApiError(f"upstream returned HTTP {status}") from error

        logger.critical(
            "Upstream server failed after retries",
            extra={"status_code": status},
            exc_info=True,
        )
        raise SystemApiError(f"upstream returned HTTP {status}") from error
    except (requests.Timeout, requests.ConnectionError) as error:
        logger.critical(
            "Upstream connection failed after retries",
            exc_info=True,
        )
        raise SystemApiError("upstream is unavailable") from error
    except requests.RequestException as error:
        logger.error("Unexpected HTTP client error", exc_info=True)
        raise SystemApiError("HTTP request failed") from error
```

这段代码只在所有内部 retries 结束后记录一次最终失败，避免每次 attempt 都产生一
条业务告警。日志中不应直接写入完整 URL query、Authorization/Cookie header、请求
body 或未经限制的响应 body，以免泄漏凭据和个人信息。

## 9. Redirect

`create_retry()` 的 `redirect=0` 关闭的是 urllib3 Retry 层的 redirect 计数。
Requests 仍按其原生规则处理 redirect：

```python
with create_session(create_retry(), timeout=(3, 20)) as session:
    response = session.get(
        "https://api.example.com/start",
        allow_redirects=False,
    )
```

- 用 `allow_redirects=False` 禁止本次自动跳转。
- 用 `session.max_redirects` 设置 Requests redirect 上限。
- 已发生的 redirect 可以从 `response.history` 查看。
- Requests redirect 不计入 urllib3 Retry history。
- 本项目不额外禁止跨 origin redirect。

如果请求包含敏感凭据，应理解 Requests 在 redirect 时的 header、auth 和 method
处理规则，并在高安全场景中主动关闭自动跳转或校验目标 origin。

## 10. `stream=True` 和响应体

默认 `stream=False` 时，Requests 在返回前读取 body。使用 `stream=True` 时，
必须在 Session 关闭前消费完整 body 或关闭 Response：

```python
with create_session(create_retry(), timeout=(3, 20)) as session:
    with session.get(
        "https://api.example.com/large-file",
        stream=True,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                process(chunk)
```

如果既不消费也不关闭 Response，连接不能及时归还连接池，会逐渐损害连接复用。

urllib3 收到响应头并进入 body 消费阶段后，timeout、reset 或 body 截断不会重新
进入 HTTPAdapter retry 循环。`stream=False` 时，错误可能在 `session.get()` 返回
Response 之前抛出；`stream=True` 时，错误通常在 APP 调用 `iter_content()`、
`iter_lines()` 或读取 `response.raw` 时抛出。APP 必须自行处理中途失败，并决定
是否从头下载、使用 Range 续传或放弃。

## 11. 请求体能否安全重放

本项目不会检查 body 是否可以 replay。以下 body 在 retry 时可能已经被消费：

- generator 或 iterator
- 未 rewind 的文件对象
- streaming upload
- 动态产生且每次内容不同的数据

即使 body 可以重新读取，写操作也只有在服务端幂等时才能安全 retry。启用 POST、
PUT、PATCH 或 DELETE retry 前，应同时满足：

1. 操作本身幂等，或者服务端提供可靠的幂等键。
2. 每次 attempt 使用相同的幂等键。
3. body 可以 rewind 或确定地重新生成。
4. 业务能够接受首次请求已成功、但客户端没有收到响应时再次发送。

## 12. 使用 Requests 原生 Session 能力

`create_session()` 返回 `requests.Session`，headers、认证、Cookie、TLS 和 proxy
仍直接使用 Requests API：

```python
with create_session(create_retry(), timeout=(3, 20)) as session:
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "inventory-app/1.0",
        }
    )
    session.auth = ("user", "password")
    session.verify = "/path/to/ca-bundle.pem"
    session.trust_env = False
    session.max_redirects = 10

    response = session.get("https://api.example.com/v1/inventory")
    response.raise_for_status()
```

本项目使用 `HTTPAdapter` 的原生连接池默认值。如果必须配置 pool size、blocking、
自定义 TLS 或专用 Adapter，直接使用 Requests Session/Adapter API 会更清楚；
此时不必继续扩展这个最小 factory。

## 13. 生产环境检查清单

上线前建议逐项确认：

- 为 Session 或每次请求显式配置了 timeout。
- `total` 和分类上限符合调用链的整体 SLA。
- 已评估 backoff 与 `Retry-After` 可能带来的最长等待。
- `allowed_methods` 只包含业务上真正安全、幂等的方法。
- 可重试写操作使用稳定幂等键，body 可以 replay。
- 使用 `raise_on_status=False` 时，调用方确实调用了 `raise_for_status()` 或显式
  检查 `response.status_code`。
- `stream=True` 的 Response 总会被消费或关闭。
- 最终失败被记录并转换为业务需要的异常；日志不会泄漏敏感数据。
- Session 生命周期清晰，不作为跨线程或跨进程的全局单例。
- 业务需要限流、熔断或总 deadline 时，已经在这一层之外实现。

## 14. 常见问题

### 同一个 Session 中的第二个请求会继承第一个请求已用掉的 retry 次数吗？

不会。两个逻辑请求共享连接池，但 urllib3 为每个请求维护独立 Retry history。

### retry 一定会使用同一条 TCP 连接吗？

不一定。可用连接可能被复用；失效连接会被丢弃，后续 attempt 可以建立新连接。

### 为什么收到 500 后没有自动抛异常？

默认 `raise_on_status=False`。状态 retry 完成后会返回最终 Response，请调用
`response.raise_for_status()` 或自行检查状态码。

### 为什么 POST 在连接失败时仍然 retry？

connect failure 发生在请求成功发送之前，不受 `allowed_methods` 限制。POST 的状态
和 read retry 默认被禁止。

### timeout 是 20 秒，为什么整个调用超过了 20 秒？

read timeout 针对每次 attempt 的读取等待，不是所有 attempts、退避和
`Retry-After` 的总 deadline。

### 可以给 429 和 500 配置不同 retry 次数吗？

urllib3 Retry 不直接支持逐状态码次数。需要时应由业务层实现明确状态机。

### 可以在 asyncio 中直接调用吗？

不建议。Requests、urllib3 retry 和 backoff sleep 都是同步阻塞的。异步 APP 应
使用线程隔离，或选择原生异步 HTTP 客户端。

### 这个库有流量控制或熔断吗？

没有。连接池不等于 QPS、并发、令牌桶限流或熔断器，这些能力应使用独立组件。
