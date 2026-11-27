"""docwatch.collect.fetch —— 资源级条件抓取。

把一个 URL 变成「bytes + ETag + 大小 + 到底变没变」，并把失败按类型分清楚
（超时 / 403 / 5xx / 解析失败），让上层只关心结果、不关心 socket。

特性对齐 CONTRACT §2 与任务书：
- 条件请求：`If-None-Match` / `If-Modified-Since`，304 时一个字节正文都不传
- 浏览器 UA、`Accept-Encoding: gzip`、最多 3 跳重定向
- 按「线程 × 主机」复用 TLS 连接（keep-alive），默认 6 并发
- 资源抓取额外返回 sha256 / 大小 / ETag / 原始字节，便于内容寻址存储

只依赖标准库；不 import watch_docs.py（那是 v1 的入口，行为不要互相牵连）。
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
import zlib

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_ANY = "*/*"

TIMEOUT = 20.0          # 单次请求超时（秒）
MAX_REDIRECTS = 3       # 最多跟几跳重定向
WORKERS = 6             # 默认并发
RETRIES = 1             # 网络类错误重试次数（4xx/5xx 不重试，别打站点）
MAX_BODY_BYTES = 8 * 1024 * 1024       # 解压后正文上限
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024 # 压缩正文上限
_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL = 300.0

# 只允许抓官方站点；任何重定向到列表外的域名都会被拒绝。
ALLOWED_HOSTS = (
    "api-docs.deepseek.com",
    "www.deepseek.com",
    "deepseek.com",
    "fe-static.deepseek.com",
)


# ------------------------------------------------------------------ 错误类型

class FetchError(Exception):
    """所有抓取失败的基类：一定带 url 和一句人话原因。"""

    kind = "error"
    retriable = False

    def __init__(self, url: str, reason: str, status: int = 0) -> None:
        self.url = url
        self.reason = reason
        self.status = status
        super().__init__(f"{self.kind}: {url} — {reason}")


class NetworkError(FetchError):
    """连接被拒/断开/DNS 解析失败等。"""

    kind = "network"
    retriable = True


class FetchTimeout(FetchError):
    """超时（连接或读取）。"""

    kind = "timeout"
    retriable = True


class TooManyRedirects(FetchError):
    """重定向超过 3 跳（或 3xx 但没有 Location）——通常是登录墙/风控跳转。"""

    kind = "redirect"


class HTTPStatusError(FetchError):
    """非 2xx 状态码。status 里有原始状态码。"""

    kind = "http"

    def __init__(self, url: str, status: int, snippet: str = "") -> None:
        reason = f"HTTP {status}"
        if snippet:
            reason += f"：{snippet}"
        super().__init__(url, reason, status=status)


class Forbidden(HTTPStatusError):
    """401/403 —— 被当成爬虫挡了。"""

    kind = "forbidden"


class RateLimited(HTTPStatusError):
    """429 —— 被限流。"""

    kind = "rate_limited"
    retriable = True


class ServerError(HTTPStatusError):
    """5xx —— 站点自己挂了。"""

    kind = "server"
    retriable = True


class ParseError(FetchError):
    """拿到了响应但内容用不了（gzip 解不开、sitemap 没 <loc>、编码不认识）。"""

    kind = "parse"


class UnsafeURLError(FetchError):
    """URL 不在官方白名单、不是 https、或解析到私网地址（SSRF 防护）。"""

    kind = "unsafe_url"


class TooLarge(FetchError):
    """响应体超过上限（防超大响应 / 解压炸弹）。"""

    kind = "too_large"


def _host_allowed(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for allowed in ALLOWED_HOSTS:
        candidate = allowed.lower().rstrip(".")
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def _validate_url(url: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError(url, f"URL 解析失败：{exc}") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeURLError(url, "只允许 https")
    if parts.username or parts.password:
        raise UnsafeURLError(url, "URL 不允许包含账号密码")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeURLError(url, "URL 含控制字符")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not _host_allowed(host):
        raise UnsafeURLError(url, f"域名不在允许列表：{host or '(空)'}")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeURLError(url, f"端口不合法：{exc}") from exc
    if port != 443:
        raise UnsafeURLError(url, f"只允许 443 端口，收到 {port}")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise UnsafeURLError(url, "不允许使用 IP 字面量")
    except ValueError:
        pass
    return parts


def _resolve_public(host: str) -> list[str]:
    """解析域名，只允许公网地址；解析结果缓存 5 分钟。"""
    now = time.monotonic()
    cached = _DNS_CACHE.get(host)
    if cached and now - cached[0] < _DNS_TTL:
        return list(cached[1])
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkError(host, f"DNS 解析失败：{exc}") from exc
    ips: list[str] = []
    for _family, _socktype, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if not ip.is_global:
            raise UnsafeURLError(host, f"域名解析到非公网地址：{raw_ip}")
        if raw_ip not in ips:
            ips.append(raw_ip)
    if not ips:
        raise NetworkError(host, "DNS 没有返回可用地址")
    _DNS_CACHE[host] = (now, ips)
    return ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """把 TCP 连接钉在已校验的 IP 上，SNI 和证书仍按原域名校验。"""

    def __init__(self, host: str, ip: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, 443, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def error_for_status(url: str, status: int, body: bytes) -> HTTPStatusError:
    snippet = body[:200].decode("utf-8", "replace").strip()
    if status in (401, 403):
        return Forbidden(url, status, snippet)
    if status == 429:
        return RateLimited(url, status, snippet)
    if status >= 500:
        return ServerError(url, status, snippet)
    return HTTPStatusError(url, status, snippet)


# ------------------------------------------------------------------ 响应对象

class Response:
    """一次（跟完重定向的）HTTP 结果。

    body 是**解压后**的正文；wire_bytes 是实际过网的字节数（响应头 + 压缩正文），
    用来核对「稳态一轮 ≤ 100 KB」这类预算。
    """

    __slots__ = ("url", "final_url", "status", "body", "etag", "last_modified",
                 "content_type", "headers", "wire_bytes", "elapsed_ms", "redirects")

    def __init__(self, url: str, final_url: str, status: int, body: bytes,
                 etag: str, last_modified: str, content_type: str,
                 headers: dict, wire_bytes: int, elapsed_ms: int, redirects: int) -> None:
        self.url = url
        self.final_url = final_url
        self.status = status
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.content_type = content_type
        self.headers = headers
        self.wire_bytes = wire_bytes
        self.elapsed_ms = elapsed_ms
        self.redirects = redirects

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def size(self) -> int:
        return len(self.body)

    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Response {self.status} {self.size}B {self.url}>"


# ------------------------------------------------------------------ 连接池

class _ConnPool:
    """按「线程 × 主机」缓存 HTTPSConnection —— 6 个线程各留一条长连接。

    http.client 的连接不是线程安全的，所以不能跨线程共享；而每个线程跑多个
    请求时复用同一条连接，能把 TLS 握手次数从 82 次压到个位数。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_thread: dict[int, dict[str, http.client.HTTPSConnection]] = {}
        self._ctx = ssl.create_default_context()

    def get(self, host: str, timeout: float, ip: str = "") -> http.client.HTTPSConnection:
        tid = threading.get_ident()
        with self._lock:
            conns = self._by_thread.setdefault(tid, {})
        conn = conns.get(host)
        if conn is None:
            if ip:
                conn = _PinnedHTTPSConnection(host, ip, timeout, self._ctx)
            else:
                conn = http.client.HTTPSConnection(host, timeout=timeout, context=self._ctx)
            conns[host] = conn
        return conn

    def drop(self, host: str) -> None:
        tid = threading.get_ident()
        with self._lock:
            conns = self._by_thread.get(tid) or {}
        conn = conns.pop(host, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关连接失败无所谓
                pass

    def close(self) -> None:
        with self._lock:
            groups = list(self._by_thread.values())
            self._by_thread.clear()
        for conns in groups:
            for conn in conns.values():
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


# ------------------------------------------------------------------ 抓取器

class Fetcher:
    """条件抓取器。线程安全；统计量在 `stats` 里（请求数 / 304 数 / 流量）。"""

    def __init__(self, workers: int = WORKERS, timeout: float = TIMEOUT,
                 retries: int = RETRIES, ua: str = BROWSER_UA) -> None:
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self.ua = ua
        self._pool = _ConnPool()
        self._lock = threading.Lock()
        self.stats = {
            "requests": 0,     # 总请求数（含重定向与重试）
            "hits_304": 0,     # 命中 304 的次数
            "downloads": 0,    # 真正拿到 200 正文的次数
            "errors": 0,
            "retries": 0,
            "body_bytes": 0,   # 过网正文（压缩后）字节
            "wire_bytes": 0,   # 过网总字节（响应头 + 正文）
        }

    # -- 统计 -----------------------------------------------------
    def _bump(self, **kw: int) -> None:
        with self._lock:
            for k, v in kw.items():
                self.stats[k] += v

    # -- 单个请求 -------------------------------------------------
    def get(self, url: str, etag: str = "", last_modified: str = "",
            accept: str = ACCEPT_HTML) -> Response:
        """条件 GET。304 返回 Response(status=304, body=b"")；失败抛 FetchError。

        网络类错误（超时/断连）自动重试 `retries` 次；4xx/5xx 不重试。
        """
        attempt = 0
        while True:
            try:
                return self._get_once(url, etag, last_modified, accept)
            except FetchError as exc:
                if not exc.retriable or attempt >= self.retries:
                    self._bump(errors=1)
                    raise
                attempt += 1
                self._bump(retries=1)
                time.sleep(0.3 * attempt)

    def _get_once(self, url: str, etag: str, last_modified: str,
                  accept: str) -> Response:
        started = time.monotonic()
        current = url
        redirects = 0
        total_wire = 0

        while True:
            parts = _validate_url(current)
            host = parts.hostname or ""
            if not host:
                raise ParseError(url, f"URL 没有主机名：{current}")
            ips = _resolve_public(host)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query

            headers = {
                "User-Agent": self.ua,
                "Accept": accept,
                "Accept-Encoding": "gzip, deflate",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

            conn = self._pool.get(host, self.timeout, ips[0])
            self._bump(requests=1)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                raw = _read_raw_limited(resp, url)
            except socket.timeout as exc:
                self._pool.drop(host)
                raise FetchTimeout(url, f"超时（>{self.timeout:g}s）：{current}") from exc
            except (http.client.HTTPException, OSError) as exc:
                # 复用的长连接可能已被服务端关掉：丢掉它，交给重试重新握手
                self._pool.drop(host)
                raise NetworkError(url, f"{type(exc).__name__}: {exc}（{current}）") from exc

            status = resp.status
            header_bytes = sum(len(k) + len(v) + 4 for k, v in resp.getheaders())
            wire = header_bytes + len(raw)
            total_wire += wire
            self._bump(wire_bytes=wire, body_bytes=len(raw))

            if (resp.getheader("Connection") or "").lower() == "close":
                self._pool.drop(host)

            if status == 304:
                self._bump(hits_304=1)
                return Response(url, current, 304, b"", etag, last_modified, "", {},
                                total_wire, _ms(started), redirects)

            if status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location") or ""
                if not location:
                    raise TooManyRedirects(url, f"HTTP {status} 没有 Location（{current}）")
                if redirects >= MAX_REDIRECTS:
                    raise TooManyRedirects(
                        url, f"重定向超过 {MAX_REDIRECTS} 跳，最后一跳 → {location[:120]}")
                redirects += 1
                current = urllib.parse.urljoin(current, location)
                continue

            if status != 200:
                raise error_for_status(url, status, raw)

            encoding = (resp.getheader("Content-Encoding") or "").lower()
            body = _decompress(raw, encoding, url)
            self._bump(downloads=1)
            return Response(
                url, current, 200, body,
                (resp.getheader("ETag") or "").strip(),
                (resp.getheader("Last-Modified") or "").strip(),
                (resp.getheader("Content-Type") or "").strip(),
                {k.lower(): v for k, v in resp.getheaders()},
                total_wire, _ms(started), redirects,
            )

    # -- 批量 -----------------------------------------------------
    def get_many(self, requests: list[dict], workers: int | None = None
                 ) -> list[Response | FetchError]:
        """并发抓一批。每项是 {"url", "etag"?, "last_modified"?, "accept"?}。

        返回与输入等长的列表，失败项是 FetchError 实例（不抛），由上层决定怎么记信号。
        """
        from concurrent.futures import ThreadPoolExecutor

        def one(req: dict) -> Response | FetchError:
            url = req["url"]
            try:
                return self.get(url, req.get("etag", ""), req.get("last_modified", ""),
                                req.get("accept", ACCEPT_HTML))
            except FetchError as exc:
                return exc
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让一个 URL 带崩整轮
                return FetchError(url, f"{type(exc).__name__}: {exc}")

        if not requests:
            return []
        n = workers or self.workers
        if n <= 1 or len(requests) == 1:
            return [one(r) for r in requests]
        with ThreadPoolExecutor(max_workers=min(n, len(requests))) as ex:
            return list(ex.map(one, requests))

    # -- 资源 -----------------------------------------------------
    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ------------------------------------------------------------------ 小工具

def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _read_raw_limited(resp: http.client.HTTPResponse, url: str) -> bytes:
    declared = resp.getheader("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_COMPRESSED_BYTES:
                raise TooLarge(url, f"Content-Length 超过 {MAX_COMPRESSED_BYTES} 字节")
        except ValueError:
            pass
    chunks = bytearray()
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_COMPRESSED_BYTES:
            raise TooLarge(url, f"响应超过 {MAX_COMPRESSED_BYTES} 字节")
    return bytes(chunks)


def _inflate(raw: bytes, wbits: int, url: str) -> bytes:
    """按指定窗口解码一段压缩流，解压后仍受 MAX_BODY_BYTES 限制。"""
    obj = zlib.decompressobj(wbits)
    out = obj.decompress(raw, MAX_BODY_BYTES + 1)
    if obj.unconsumed_tail:
        raise TooLarge(url, f"解压后正文超过 {MAX_BODY_BYTES} 字节")
    return out + obj.flush(max(1, MAX_BODY_BYTES + 1 - len(out)))


def _decompress(raw: bytes, encoding: str, url: str) -> bytes:
    if not encoding or encoding == "identity":
        if len(raw) > MAX_BODY_BYTES:
            raise TooLarge(url, f"正文超过 {MAX_BODY_BYTES} 字节")
        return raw
    encoding = encoding.lower()
    try:
        if "gzip" in encoding:
            out = _inflate(raw, 16 + zlib.MAX_WBITS, url)
        elif "deflate" in encoding:
            # deflate 有 zlib 包装与裸流两种写法：先按标准头解，失败再试裸流。
            try:
                out = _inflate(raw, zlib.MAX_WBITS, url)
            except zlib.error:
                out = _inflate(raw, -zlib.MAX_WBITS, url)
        else:
            out = raw
    except TooLarge:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ParseError(url, f"解压失败（{encoding}，{len(raw)} 字节）：{exc}") from exc
    if len(out) > MAX_BODY_BYTES:
        raise TooLarge(url, f"解压后正文超过 {MAX_BODY_BYTES} 字节")
    return out


def human_bytes(n: int) -> str:
    """12345 → '12.1 KB'（简报里用）。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"
