# Resilient HTTP Client 0.4.0

为 Requests、HTTPX 和 aiohttp 提供轻量的 retry client/session factory。

三个后端都保持各自客户端、Response 和异常体系不变，并提供尽量一致的默认策略：

- 首次发送失败后最多重试 3 次。
- 默认 status/read retry 只允许 `GET`、`HEAD`、`OPTIONS`；Requests/aiohttp 的
  connect retry 是例外，详见后端对比。
- 默认状态码为 `429`、`500`、`502`、`503`、`504`。
- 默认指数退避系数为 `0.5`，并处理 `Retry-After`。
- 每次 factory 默认都创建独立客户端和连接池；HTTPX 显式注入 transport 时由调用方
  保证不复用。
- timeout 必须由 APP 主动配置；省略时不设置隐藏 timeout。

根包继续保持原来的 Requests API。HTTPX 和 aiohttp 使用独立子模块，避免三个
`Retry` 类型和同名 factory 混在一起：

```python
# Requests
from resilient_http import Retry, create_retry, create_session

# HTTPX
from resilient_http.httpx import (
    Retry,
    create_async_client,
    create_client,
    create_retry,
)

# aiohttp
from resilient_http.aiohttp import Retry, create_retry, create_session
```

## 安装

Requests 后端是基础安装的一部分：

```bash
uv add resilient-http-client
```

按需安装 HTTPX、aiohttp 或全部后端：

```bash
uv add 'resilient-http-client[httpx]'
uv add 'resilient-http-client[aiohttp]'
uv add 'resilient-http-client[all]'
```

项目支持 Python 3.9 及以上版本。

## Requests quickstart

```python
import requests

from resilient_http import create_retry, create_session


try:
    with create_session(create_retry(), timeout=(3, 20)) as session:
        response = session.get("https://api.example.com/health")
        response.raise_for_status()
        data = response.json()
except requests.RequestException:
    # 记录最终失败、转换业务异常或执行降级。
    raise
```

详细说明：[Requests 使用指南](docs/usage.md)。

## HTTPX quickstart

同步 Client：

```python
import httpx

from resilient_http.httpx import create_client, create_retry


try:
    with create_client(create_retry(), timeout=(3, 20)) as client:
        response = client.get("https://api.example.com/health")
        response.raise_for_status()
        data = response.json()
except httpx.HTTPError:
    raise
```

异步 Client 使用同一份策略：

```python
import httpx

from resilient_http.httpx import create_async_client, create_retry


async def fetch_health() -> dict:
    try:
        async with create_async_client(
            create_retry(),
            timeout=(3, 20),
        ) as client:
            response = await client.get("https://api.example.com/health")
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        raise
```

详细说明：[HTTPX 使用指南](docs/httpx.md)。

## aiohttp quickstart

```python
import aiohttp

from resilient_http.aiohttp import create_retry, create_session


async def fetch_health() -> dict:
    try:
        async with create_session(
            create_retry(),
            timeout=(3, 20),
        ) as session:
            async with session.get(
                "https://api.example.com/health",
            ) as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError:
        raise
```

详细说明：[aiohttp 使用指南](docs/aiohttp.md)。

## 选择后端前必须知道的差异

相似的 factory 不会掩盖底层库的真实语义：

- Requests 和 aiohttp 使用 `urllib3.util.Retry`，支持 `connect`、`read`、
  `status`、`other` 分类预算；HTTPX 使用 `httpx_retries.Retry`，只有 `total`。
- Requests/aiohttp 的 connect retry 不受 `allowed_methods` 限制；HTTPX 的
  `allowed_methods` 会同时限制状态和所有 transport 异常 retry。
- Requests 和 aiohttp 的 GET 默认自动跟随 redirect（HEAD 默认不跟随）；HTTPX
  默认不跟随。
- 三个后端的 timeout 字段和单次覆盖方式不同。
- 收到响应头后，在消费响应体期间发生的错误都不会被这些 transport/middleware
  自动 retry。

完整矩阵和迁移建议见[三个后端对比](docs/comparison.md)。

## 设计边界

本项目有意不增加自定义 Client、配置模型、业务异常、attempt 统计、base URL、
流量控制、熔断或总 deadline。APP 继续使用 Requests、HTTPX、aiohttp 的原生
request/Response API 处理认证、Cookie、redirect、日志和业务异常转换。factory
主要接收 retry 与 timeout；HTTPX factory 另提供可选的原生底层 `transport` 注入，
用于 proxy、TLS、HTTP/2 或连接池配置。其他高级客户端参数继续直接组合底层客户端
及其 retry 接入点。

## 本地开发

项目使用 uv 管理依赖和 lockfile：

```bash
uv sync --all-extras
uv lock --check
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

自动修复和格式化：

```bash
uv run ruff check . --fix
uv run ruff format .
```

测试使用本地 HTTP server 或受控 transport，不访问公网。
