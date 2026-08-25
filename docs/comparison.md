# Requests、HTTPX、aiohttp 后端对比

三个后端提供相似的 factory 形状和保守默认值，但不会为了表面一致而隐藏底层库的
真实差异。选择后端时，应先看现有 APP 的并发模型和异常体系，再确认 retry 预算、
method gate、timeout 和 redirect 是否符合业务预期。

- [Requests 详细指南](usage.md)
- [HTTPX 详细指南](httpx.md)
- [aiohttp 详细指南](aiohttp.md)

## 1. 快速选择

| 场景 | 推荐后端 | 原因 |
|---|---|---|
| 已有同步 Requests APP | Requests | 改动最小，直接使用成熟的 urllib3 Retry |
| 新同步 APP，希望使用 HTTPX API | HTTPX sync | 原生 HTTPX Client/异常；策略只有 total，较简单 |
| 同时需要同步和异步且希望 API 接近 | HTTPX | 同一 Retry 可用于 Client 和 AsyncClient |
| 现有 asyncio/aiohttp APP | aiohttp | 原生 ClientSession、connector、Response 和异常 |
| 异步且必须区分 connect/read/status 预算 | aiohttp | 复用 urllib3 分类预算，HTTPX 只有 total |
| 需要客户端原生累计 total timeout | aiohttp | `ClientTimeout(total=...)` 覆盖完整高层请求 |

如果 APP 已经围绕某个客户端建立认证、proxy、tracing、测试替身和异常映射，通常
不应只因为 retry 而更换底层客户端。

## 2. API 与实现矩阵

| 项目 | Requests | HTTPX | aiohttp |
|---|---|---|---|
| 安装 | 基础包 | `[httpx]` extra | `[aiohttp]` extra |
| import | `resilient_http` | `resilient_http.httpx` | `resilient_http.aiohttp` |
| Retry 类型 | `urllib3.util.Retry` | `httpx_retries.Retry` | `urllib3.util.Retry` |
| factory | `create_session` | `create_client` / `create_async_client` | `create_session` |
| 返回类型 | `requests.Session` | `httpx.Client` / `AsyncClient` | `aiohttp.ClientSession` |
| 同步/异步 | 同步 | 两者都有 | 异步 |
| retry 接入点 | `HTTPAdapter` | `RetryTransport` | client middleware |
| 每次 factory | 新 Session、Adapter、pool | 默认新 Client、transport、pool | 新 Session、connector、pool |
| 默认 retry 次数 | 3；最多发送 4 次 | 3；最多发送 4 次 | 3；最多发送 4 次 |
| 默认 methods | `GET/HEAD/OPTIONS` | `GET/HEAD/OPTIONS` | `GET/HEAD/OPTIONS` |
| 默认状态 | `429/500/502/503/504` | `429/500/502/503/504` | `429/500/502/503/504` |
| 默认退避系数 | 0.5 | 0.5 | 0.5 |
| 状态耗尽默认 | 返回最终 Response | 返回最终 Response | 返回最终 Response |

相同名称的 `Retry` 类型不能互换。尤其不要把根包的 urllib3 Retry 传给 HTTPX
factory。

## 3. Retry 预算

### Requests 与 aiohttp：总预算 + 分类预算

两者支持：

```python
create_retry(
    total=5,
    connect=3,
    read=1,
    status=2,
    other=0,
)
```

分类预算和 `total` 同时生效，先耗尽的限制停止 retry。混合故障也共同消耗总预算。
`connect/read/status=None` 表示不增加该分类的独立限制，仍受 `total` 限制。

### HTTPX：只有总预算

HTTPX 0.4.6 的 `httpx_retries.Retry` 只有 `total`：

```python
from resilient_http.httpx import create_retry


retry = create_retry(total=5)
```

状态响应和所有允许的 transport 异常共同使用这一个预算。无法分别设置 connect、
read、status 次数，也没有 urllib3 的 `other` 分类。

HTTPX helper 中，`retry_on_exceptions=None` 或空集合表示禁用异常 retry；状态 retry
仍由 `status_forcelist` 控制。关闭全部 retry 的统一写法仍是 `total=0`。

## 4. `allowed_methods` 的关键差异

| 故障 | Requests | HTTPX | aiohttp |
|---|---:|---:|---:|
| connect timeout、DNS、连接拒绝 | **不受** methods 限制 | **受** methods 限制 | **不受** methods 限制 |
| 响应头前 read/protocol failure | 受限制 | 受限制 | 受限制 |
| `status_forcelist` 状态 | 受限制 | 受限制 | 受限制 |

默认 methods 不包含 POST，因此：

- Requests/aiohttp 的 POST 在 connect 阶段失败时仍可能 retry；它们认为请求尚未
  成功发出。
- HTTPX 的 POST 不进入 RetryTransport retry 循环，connect failure 也不会 retry。
- 三者默认都不会对 POST 的可重试状态执行 retry。

urllib3 中 `allowed_methods=None` 和空集合都表示允许所有方法。HTTPX helper 则拒绝
空集合，因为 httpx-retries 0.4.6 会把空集合恢复成自己的上游默认值。关闭全部
retry 的统一写法是 `total=0`。

## 5. `Retry-After` 与 backoff

| 行为 | Requests | HTTPX | aiohttp |
|---|---|---|---|
| 等待方式 | 阻塞当前线程 | sync 阻塞；async 非阻塞 | `asyncio.sleep`，非阻塞 |
| header 能否单独触发 retry | 可对 urllib3 适用状态触发 | 不能；状态必须在 force list | 可对 urllib3 适用状态触发 |
| 适用的额外状态 | `413/429/503` | 无额外状态 | `413/429/503` |
| 无效 header | Requests `InvalidHeader` | warning 后退回 backoff | urllib3 `InvalidHeader` |
| 默认等待上限 | `retry_after_max=21600s` | `max_backoff_wait=120s` | `retry_after_max=21600s` |
| helper jitter | urllib3 默认 0 附加秒数 | 固定比例 0 | urllib3 默认 0 附加秒数 |

两种 Retry 的 jitter 含义也不同：urllib3 `backoff_jitter` 是附加的随机秒数；
httpx-retries 0.4.6 的 `backoff_jitter` 是 `0..1` 的比例。

默认 `backoff_factor=0.5` 的典型序列不同：urllib3 通常约为 `0s、1s、2s`；
httpx-retries 0.4.6 约为 `1s、2s、4s`。三个后端都应把 backoff 和
`Retry-After` 纳入整体 SLA 评估。

## 6. Redirect

| 行为 | Requests | HTTPX | aiohttp |
|---|---|---|---|
| GET 默认 | 自动跟随 | 不跟随 | 自动跟随 |
| HEAD 默认 | 不跟随 | 不跟随 | 不跟随 |
| 单次开关 | `allow_redirects=` | `follow_redirects=` | `allow_redirects=` |
| 历史 | `response.history` | `response.history` | `response.history` |

Requests/aiohttp 的 helper 固定 `redirect=0`，只关闭 urllib3 Retry 的 redirect
计数；原生客户端仍管理 redirect。HTTPX Retry 没有 redirect 预算，redirect 也由
Client 层管理。

每个 redirect hop 是新的底层请求，并获得新的 retry 预算。因此开启自动跳转后，
一次高层调用的总发送次数可能超过 `total + 1`。aiohttp 默认
`max_redirects=10`；Requests 使用 `session.max_redirects`；HTTPX 使用自身原生
redirect 上限和异常。

## 7. Timeout 映射

### 7.1 factory 默认与常用值

| factory 参数 | Requests | HTTPX | aiohttp |
|---|---|---|---|
| 省略 / `None` | 不设置 connect/read timeout | 明确关闭 HTTPX 原生 5 秒 timeout | 明确关闭 aiohttp 原生 5 分钟/30 秒 timeout |
| `timeout=5` | connect/read 都为 5 秒 | connect/read/write/pool 都为 5 秒 | connect/sock_connect/sock_read 为 5 秒；total=None |
| `timeout=(3, 20)` | connect=3、read=20 | connect=3、read=20；write/pool=None | connect/sock_connect=3、sock_read=20；total=None |
| 原生高级类型 | urllib3/Requests 支持的原生值 | `httpx.Timeout` | `aiohttp.ClientTimeout` |

factory 的 `timeout=None` 都是为了避免底层客户端各自不同的隐藏默认值。生产环境
通常应显式配置 timeout。

aiohttp factory 的 Requests 风格数字/tuple 只接受有限且大于 0 的非 bool 数值；
需要 aiohttp 自身的特殊 timeout 值时应传原生 `ClientTimeout`。HTTPX factory 会将
2/3/4 元 tuple 规范化为四个阶段字段，并拒绝其他 tuple 长度。

### 7.2 单次覆盖

- Requests：省略继承 Session factory 值；数字/tuple 覆盖；显式 `None` 关闭。
- HTTPX：省略继承 Client 值；数字/`httpx.Timeout`/tuple 覆盖；显式 `None` 关闭。
- aiohttp：省略继承 Session 值；单次参数遵循 aiohttp 原生规则。数字表示
  覆盖 middleware retries 的累计 **total timeout**，不是 connect/read 各自
  timeout；精细覆盖要传 `aiohttp.ClientTimeout`。显式 `None` 会为该次请求关闭
  timeout。

aiohttp factory 的 `(connect, read)` tuple 只适用于创建 Session，不能直接作为
`session.get(..., timeout=(...))` 的单次覆盖。

### 7.3 是否有总 deadline

- Requests/HTTPX 的 factory timeout 是每次物理 attempt 的阶段限制。retry、
  backoff、`Retry-After` 和 redirect 会累加，没有内建总 deadline。
- aiohttp 的数字/tuple factory 配置同样设置 `total=None`。但 APP 可以传
  `aiohttp.ClientTimeout(total=...)`，让 aiohttp 对一个高层请求的 retry、backoff、
  redirect 和 body 消费施加累计限制。

## 8. 状态与异常

| 最终结果 | Requests | HTTPX | aiohttp |
|---|---|---|---|
| 4xx/5xx Response 检查 | `response.raise_for_status()` | `response.raise_for_status()` | `response.raise_for_status()` |
| 状态异常 | `requests.HTTPError` | `httpx.HTTPStatusError` | `aiohttp.ClientResponseError` |
| connect/read 耗尽 | Requests 原生异常 | HTTPX 原生异常 | 原始 aiohttp 异常 |
| `raise_on_status=True` | 可能抛 `requests.RetryError` | 不支持该选项 | 抛 aiohttp `ClientResponseError` |

HTTPX Retry 没有 `raise_on_status`，状态耗尽总是返回最后一个 Response。aiohttp
middleware 只在内部把异常映射到 urllib3 分类；耗尽后重新抛原始 aiohttp 异常。
aiohttp 的无效 `Retry-After` 是少数例外，会暴露
`urllib3.exceptions.InvalidHeader`。

业务层应只在所有内部 attempts 结束后记录一次最终失败，再按各后端原生异常转换
业务异常。不要编写一个同时捕获三套异常的通用函数；应在各业务客户端边界分别处理。

## 9. Streaming、响应体和请求 body

三个实现的 retry 接入点都在“响应头返回之前”：

- Requests：urllib3 `HTTPAdapter`。
- HTTPX：`RetryTransport.handle_request()`。
- aiohttp：client middleware 调用底层 handler。

因此收到响应头之后，在消费 body 时发生的 timeout、reset 或截断都不会透明 retry：

| 后端 | 推荐 response 生命周期 |
|---|---|
| Requests | `with session.get(..., stream=True) as response` |
| HTTPX | `with client.stream(...)` / `async with client.stream(...)` |
| aiohttp | `async with session.get(...) as response` |

APP 必须在 context 内消费或关闭 Response，否则会占用连接池。

retry 还可能重新发送 request body。bytes、JSON 等已缓冲内容通常可重放；generator、
iterator、文件流、异步生成器和 streaming upload 不能假定可重放。允许写方法 retry
前必须同时确认远端幂等、幂等键稳定、body 可再次读取。

## 10. 生命周期与并发模型

| 项目 | Requests | HTTPX | aiohttp |
|---|---|---|---|
| context | `with` | `with` / `async with` | `async with` |
| 创建位置 | 普通同步代码 | sync 或 running loop | 必须在 running event loop |
| 关闭 | `session.close()` | `client.close()` / `await aclose()` | `await session.close()` |
| event loop | 不适用 | AsyncClient 遵循 HTTPX 约束 | Session 绑定创建时的 loop |

同一 Client/Session 内的请求共享连接池和客户端状态，但 retry history 独立。默认
factory 调用创建完全独立的 pool；关闭一个不会影响另一个。HTTPX 可注入一个未包装
的原生 sync/async transport 来配置 proxy、TLS、HTTP/2 或连接池；其所有权会转交给
Client，不能在多个 factory 之间复用。默认 HTTPX RetryTransport 也会绕过 Client
层的环境 proxy 自动发现，因此依赖 proxy 时应显式配置底层 transport。

aiohttp 还有一个容易忽略的原生规则：单次请求传入 `middlewares=` 会**替换**
Session 的 middleware 列表，不是追加。`middlewares=()` 或任何不包含本项目 retry
middleware 的列表都会绕过 retry。由于 retry middleware 是私有实现，普通 APP
应省略这个单次参数。

## 11. 迁移检查清单

从 Requests 迁移到 HTTPX：

- 删除 `connect/read/status/other` 分类参数，只保留 `total`。
- 重新评估 POST connect failure：HTTPX 默认不 retry。
- 显式决定是否 `follow_redirects=True`。
- 把 `requests.RequestException` 映射改为 `httpx.HTTPError` 体系。
- streaming 改用 `client.stream()`。
- 重新评估 backoff 序列、jitter 和 `Retry-After` 上限。

从 Requests 迁移到 aiohttp：

- 业务调用、生命周期和 body 消费全部改为 async。
- Retry 参数基本可复用，connect/read/status 分类仍然存在。
- 单次 timeout tuple 不能照搬；改用 `aiohttp.ClientTimeout`。
- 需要总 deadline 时明确设置 `ClientTimeout(total=...)`。
- 异常映射改为 aiohttp 类型，并考虑 policy `InvalidHeader`。
- 不要传 request-level `middlewares` 覆盖 Session retry。

在任何后端中，都应重新验证：实际发送次数、非幂等写操作、body replay、redirect
链、timeout 总耗时、最终异常和连接池关闭行为。
