"""受限 HTTPS 抓取器。

安全边界（这是本包最重要的一层）：
- 只允许 https；默认只允许 config.ALLOWED_HOSTS 里的官方域名，少数官方分发源
  （GitHub / npm / 桌面更新清单）通过调用方显式传入 allowed_hosts；
- 拒绝 URL 里的账号密码、非 443 端口、IP 字面量；
- DNS 解析后拒绝任何非公网 IP（私网/回环/链路本地/保留/组播），防止 SSRF；
- 把连接钉在已校验的 IP 上，同时用原域名做 SNI 和证书校验，防 DNS rebinding；
- 最多 3 跳重定向，每一跳重新校验；
- 响应体最多 4 MB（压缩前）/ 8 MB（解压后），防超大响应和解压炸弹；
- 不发送 Cookie、Authorization 等任何凭据，不跟随非 GET 方法。
"""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field

from .config import ALLOWED_HOSTS

MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 DeepSeekWatch/1.0"
)
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_ANY = "*/*"
ACCEPT_JSON = "application/json, text/plain, */*"

_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL = 300.0
# 整轮巡检的抓取截止时间（monotonic）。到点后不再发起新连接，让 run.py 有机会
# 落盘已有事件、推进基线，而不是被 systemd 的 TimeoutStartSec 直接 SIGTERM 丢掉整轮。
_ROUND_DEADLINE = 0.0


def set_round_deadline(seconds: float) -> None:
    global _ROUND_DEADLINE
    budget = float(seconds or 0)
    _ROUND_DEADLINE = time.monotonic() + budget if budget > 0 else 0.0


def round_remaining() -> float | None:
    if not _ROUND_DEADLINE:
        return None
    return _ROUND_DEADLINE - time.monotonic()


def _bounded_timeout(url: str, timeout: float) -> float:
    remaining = round_remaining()
    if remaining is None:
        return timeout
    if remaining <= 0:
        raise FetchError(url, "本轮巡检已到抓取时间预算，停止继续抓取")
    return min(timeout, max(1.0, remaining))


_USERINFO_RE = re.compile(r"(?<=//)[^/@\s]{0,300}@")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(value: object, limit: int = 800) -> str:
    """去掉 URL 里的 user:token@ 与所有 C0/DEL 控制字符。

    FetchError 可能在 ``reason`` 里携带重定向 Location、异常文本等外部内容；
    公开来源状态页会展示 last_error，因此错误消息本身必须先脱敏，不能只依赖
    redact() 对已知密钥的替换。
    """
    text = _CONTROL_RE.sub("", str(value or ""))
    text = _USERINFO_RE.sub("***@", text)
    return text[:limit]


class FetchError(Exception):
    """抓取失败的基类。message 里不包含任何密钥。"""

    kind = "error"

    def __init__(self, url: str, reason: str, status: int = 0) -> None:
        # url/reason 都可能来自外部（重定向 Location、异常文本），统一脱敏后再拼接。
        self.url = sanitize_text(url, 500)
        self.reason = sanitize_text(reason)
        self.status = status
        super().__init__(f"{self.kind}: {self.url} — {self.reason}")


class UnsafeURL(FetchError):
    kind = "unsafe_url"


class HTTPStatusError(FetchError):
    kind = "http"


class TooLarge(FetchError):
    kind = "too_large"


class ParseError(FetchError):
    kind = "parse"


class ContentError(FetchError):
    kind = "content"


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0
    redirects: int = 0

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def content_type(self) -> str:
        """去掉 charset 等参数后的 Content-Type，统一小写（兼容不同大小写键）。"""
        for key, value in self.headers.items():
            if str(key).lower() == "content-type":
                return str(value).split(";")[0].strip().lower()
        return ""


def _host_allowed(host: str, allowed_hosts: tuple[str, ...] | None = None) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    candidates = ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    for allowed in candidates:
        candidate = allowed.lower().rstrip(".")
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def validate_url(
    url: str, *, allowed_hosts: tuple[str, ...] | None = None
) -> urllib.parse.SplitResult:
    """校验 URL 协议/域名/端口/凭据；不通过直接抛 UnsafeURL。

    allowed_hosts 只用于少数「官方分发源」检查（GitHub / npm / 桌面更新清单），
    不传时仍沿用 config.ALLOWED_HOSTS，页面和资源抓取的安全边界不变。
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UnsafeURL(url, f"URL 解析失败：{exc}") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeURL(url, "只允许 https")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeURL(url, "URL 含控制字符")
    if parts.username or parts.password:
        raise UnsafeURL(url, "URL 不允许包含账号密码")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not _host_allowed(host, allowed_hosts):
        raise UnsafeURL(url, f"域名不在允许列表：{host or '(空)'}")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL(url, f"端口不合法：{exc}") from exc
    # parts.port 为 None 表示 URL 没写端口（默认 443）；0 是显式端口，不能当成
    # 未指定放过去，否则 validate_url 会声称允许 :0、实际却连接 443。
    if port is not None and port != 443:
        raise UnsafeURL(url, f"只允许 443 端口，收到 {port}")
    # 域名必须是真的域名，不能是 IP 字面量（防止 http://127.0.0.1 之类绕过）。
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise UnsafeURL(url, "不允许使用 IP 字面量")
    return parts


def _resolve_public(host: str) -> list[str]:
    """解析域名并只返回公网 IP；任何私网地址都会让整次解析失败。"""
    now = time.monotonic()
    cached = _DNS_CACHE.get(host)
    if cached and now - cached[0] < _DNS_TTL:
        return list(cached[1])
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(host, f"DNS 解析失败：{exc}") from exc
    ips: list[str] = []
    for _family, _socktype, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if not ip.is_global:
            raise UnsafeURL(host, f"域名解析到非公网地址：{raw_ip}")
        if raw_ip not in ips:
            ips.append(raw_ip)
    if not ips:
        raise FetchError(host, "DNS 没有返回可用地址")
    _DNS_CACHE[host] = (now, ips)
    return ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """把 TCP 连接钉在已校验的 IP 上，但 SNI/证书仍按原域名校验。"""

    def __init__(self, host: str, ip: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, 443, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:  # noqa: D401 - 标准库接口
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _inflate(raw: bytes, wbits: int, url: str) -> bytes:
    """按指定窗口解码一段（可能多成员的 gzip）压缩流，总输出仍受上限约束。

    截断的压缩流不能静默当成完整正文：那会让页面/资源哈希基于半截内容，
    制造假的「页面变化」并污染基线。因此必须校验 ``eof``；gzip 允许多成员，
    zlib/裸 deflate 结束后若还有数据则视为异常。
    """
    total = bytearray()
    pending = raw
    while pending:
        obj = zlib.decompressobj(wbits)
        remaining = MAX_BODY_BYTES + 1 - len(total)
        out = obj.decompress(pending, max(1, remaining))
        if obj.unconsumed_tail:
            raise TooLarge(url, f"解压后正文超过 {MAX_BODY_BYTES} 字节")
        total.extend(out)
        total.extend(obj.flush(max(1, MAX_BODY_BYTES + 1 - len(total))))
        if len(total) > MAX_BODY_BYTES:
            raise TooLarge(url, f"解压后正文超过 {MAX_BODY_BYTES} 字节")
        if not obj.eof:
            raise ParseError(url, "压缩流不完整")
        pending = obj.unused_data
        if pending and wbits < 16:
            # zlib 包装 / 裸 deflate 只允许一个流；尾部多余数据说明正文损坏。
            raise ParseError(url, "压缩流包含多余数据")
    return bytes(total)


def _decompress_limited(raw: bytes, encoding: str, url: str) -> bytes:
    if not encoding or encoding.lower() == "identity":
        if len(raw) > MAX_BODY_BYTES:
            raise TooLarge(url, f"正文超过 {MAX_BODY_BYTES} 字节")
        return raw
    encoding = encoding.lower()
    try:
        if "gzip" in encoding:
            out = _inflate(raw, 16 + zlib.MAX_WBITS, url)
        elif "deflate" in encoding:
            # deflate 有 zlib 包装与裸流两种写法，先按标准 zlib 头解，失败再试裸流。
            try:
                out = _inflate(raw, zlib.MAX_WBITS, url)
            except zlib.error:
                out = _inflate(raw, -zlib.MAX_WBITS, url)
        else:
            out = raw
    except TooLarge:
        raise
    except zlib.error as exc:
        raise ParseError(url, f"解压失败（{encoding}）：{exc}") from exc
    if len(out) > MAX_BODY_BYTES:
        raise TooLarge(url, f"解压后正文超过 {MAX_BODY_BYTES} 字节")
    return out


def _read_body(resp: http.client.HTTPResponse, url: str) -> bytes:
    declared = resp.getheader("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_COMPRESSED_BYTES:
                raise TooLarge(url, f"Content-Length 超过 {MAX_COMPRESSED_BYTES} 字节")
        except ValueError:
            pass
    chunks = bytearray()
    while True:
        # 慢速 trickle 响应同样要受整轮预算约束：只靠 socket 的每次 recv 超时
        # 可以让一个 4MB 响应拖过 240s，导致 systemd 在落盘前 SIGTERM。
        remaining = round_remaining()
        if remaining is not None and remaining <= 0:
            raise FetchError(url, "本轮巡检已到抓取时间预算，停止继续读取")
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_COMPRESSED_BYTES:
            raise TooLarge(url, f"响应超过 {MAX_COMPRESSED_BYTES} 字节")
    return _decompress_limited(bytes(chunks), resp.getheader("Content-Encoding") or "", url)


def fetch(
    url: str,
    *,
    accept: str = ACCEPT_HTML,
    referer: str = "",
    timeout: float = TIMEOUT,
    allowed_hosts: tuple[str, ...] | None = None,
) -> Response:
    """抓一个 URL；自动跟随最多 3 跳同域重定向。"""
    started = time.monotonic()
    current = url
    redirects = 0
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    while True:
        timeout = _bounded_timeout(current, timeout)
        parts = validate_url(current, allowed_hosts=allowed_hosts)
        host = (parts.hostname or "").lower()
        ips = _resolve_public(host)
        # DNS 解析本身无法中途打断；解析返回后立刻再对一次预算，避免已经超时
        # 还继续发请求。
        timeout = _bounded_timeout(current, timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Connection": "close",
        }
        if referer:
            try:
                validate_url(referer, allowed_hosts=allowed_hosts)
                headers["Referer"] = referer
            except FetchError:
                pass

        last_error: Exception | None = None
        response: http.client.HTTPResponse | None = None
        for ip in ips:
            attempt_timeout = _bounded_timeout(current, timeout)
            conn = _PinnedHTTPSConnection(host, ip, attempt_timeout, context)
            try:
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                break
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if response is None:
            raise FetchError(current, f"连接失败：{type(last_error).__name__}: {last_error}")
        # 响应头也可能慢速返回；拿到响应后、读正文前再检查一次预算。
        timeout = _bounded_timeout(current, timeout)

        status = response.status
        if status in (301, 302, 303, 307, 308):
            location = response.getheader("Location") or ""
            try:
                response.read(2048)
            except Exception:  # noqa: BLE001
                pass
            if not location:
                raise FetchError(current, f"HTTP {status} 没有 Location")
            if redirects >= MAX_REDIRECTS:
                raise FetchError(current, f"重定向超过 {MAX_REDIRECTS} 跳")
            redirects += 1
            current = urllib.parse.urljoin(current, location)
            continue

        if status != 200:
            snippet = ""
            try:
                snippet = response.read(300).decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                pass
            raise HTTPStatusError(current, f"HTTP {status} {snippet[:160]}".strip(), status=status)

        body = _read_body(response, current)
        return Response(
            url=url,
            final_url=current,
            status=status,
            body=body,
            headers={k.lower(): v for k, v in response.getheaders()},
            elapsed_ms=int((time.monotonic() - started) * 1000),
            redirects=redirects,
        )


def fetch_json(
    url: str,
    *,
    referer: str = "",
    allowed_hosts: tuple[str, ...] | None = None,
    expect: type | tuple[type, ...] = dict,
) -> dict | list:
    """抓 JSON；默认要求顶层是对象，需要数组时显式传 expect=list。"""
    response = fetch(url, accept=ACCEPT_JSON, referer=referer, allowed_hosts=allowed_hosts)
    content_type = response.content_type
    if content_type and "json" not in content_type and "text/plain" not in content_type:
        raise ParseError(url, f"Content-Type 不是 JSON：{content_type}")
    if not response.body:
        raise ParseError(url, "响应正文为空")
    try:
        data = json.loads(response.text)
    except ValueError as exc:
        raise ParseError(url, f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, expect):
        expected = "对象" if expect is dict else "数组" if expect is list else str(expect)
        raise ParseError(url, f"JSON 顶层不是{expected}")
    return data
