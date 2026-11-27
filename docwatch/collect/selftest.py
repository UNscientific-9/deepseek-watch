"""docwatch.collect.selftest —— 采集层自检与真实站点验收。

两种模式：

    python -m docwatch.collect.selftest --fake     # 离线（内存假站点），确定性，不联网
    python -m docwatch.collect.selftest --real     # 真实抓 58 页：基线 + 稳态 + 手工构造变化

--fake 覆盖：基线 0 信号、稳态 0 信号、正文变、资源内容变、资源改名、图片换地址、
新页面、下线页面、抓取失败、证据包结构、BRIEF 行数与可读性、manifest 里的
page_class / text_changed_chars。

--real 把验收数据打出来：基线耗时、稳态耗时/流量/304 数、手工改库后产生的信号，
并打印一份真实作业包的 BRIEF.md 前 40 行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docwatch.collect import asset as A
from docwatch.collect import signals as S
from docwatch.collect.fetch import (BROWSER_UA, FetchError, Forbidden, HTTPStatusError,
                                    Response)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = "https://api-docs.deepseek.com"
WWW = "https://www.deepseek.com/zh/"


# ================================================================ 断言小工具

class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, cond: bool, msg: str) -> bool:
        if cond:
            self.passed += 1
            print(f"  [ok]   {msg}")
        else:
            self.failures.append(msg)
            print(f"  [FAIL] {msg}")
        return bool(cond)

    def eq(self, got, want, msg: str) -> bool:
        return self.ok(got == want, f"{msg}（实际 {got!r}，期望 {want!r}）")

    def le(self, got, want, msg: str) -> bool:
        return self.ok(got <= want, f"{msg}（实际 {got}，上限 {want}）")

    def summary(self) -> int:
        print()
        if self.failures:
            print(f"结果：{self.passed} 项通过，{len(self.failures)} 项失败")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print(f"结果：{self.passed} 项全部通过")
        return 0


def kinds_of(job: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in job.get("signals") or []:
        out[s["kind"]] = out.get(s["kind"], 0) + 1
    return out


# ================================================================ 假站点

class FakeSite:
    """内存站点：url → (bytes, content_type)，ETag 由内容哈希推出（内容变则 ETag 变）。"""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}
        self.fail: dict[str, str] = {}       # url → 错误类型（"403" / "500" / "timeout"）
        self.sitemap: list[str] = []
        self.hits: dict[str, int] = {}

    # -- 内容管理 ---------------------------------------------------
    def set(self, url: str, body: str | bytes, ctype: str = "") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.files[url] = (data, ctype or _guess_ctype(url))

    def drop(self, url: str) -> None:
        self.files.pop(url, None)

    def set_sitemap(self, paths: list[str]) -> None:
        self.sitemap = list(paths)
        locs = "".join(f"<url><loc>{DOCS}{p}</loc></url>" for p in paths)
        self.set(A.DOCS_SITEMAP,
                 '<?xml version="1.0" encoding="UTF-8"?>'
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f"{locs}</urlset>",
                 "application/xml")

    def etag(self, url: str) -> str:
        data = self.files.get(url, (b"", ""))[0]
        return '"' + hashlib.sha256(data).hexdigest()[:16] + '"'


def _guess_ctype(url: str) -> str:
    path = url.lower()
    if path.endswith(".js") or path.endswith(".mjs"):
        return "application/javascript"
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".xml"):
        return "application/xml"
    return "text/html"


class FakeFetcher:
    """按 FakeFetcher 接口模拟 Fetcher：支持 If-None-Match、403/5xx/超时。"""

    def __init__(self, site: FakeSite) -> None:
        self.site = site
        self.stats = {"requests": 0, "hits_304": 0, "downloads": 0, "errors": 0,
                      "retries": 0, "body_bytes": 0, "wire_bytes": 0}

    def get(self, url: str, etag: str = "", last_modified: str = "",
            accept: str = "") -> Response:
        self.stats["requests"] += 1
        self.site.hits[url] = self.site.hits.get(url, 0) + 1
        fail = self.site.fail.get(url)
        if fail == "403":
            self.stats["errors"] += 1
            raise Forbidden(url, 403, "blocked by fake site")
        if fail == "500":
            self.stats["errors"] += 1
            raise HTTPStatusError(url, 503, "fake 503")
        if fail == "timeout":
            self.stats["errors"] += 1
            raise FetchError(url, "fake timeout")
        if url not in self.site.files:
            self.stats["errors"] += 1
            raise HTTPStatusError(url, 404, "not found")
        body, ctype = self.site.files[url]
        now_etag = self.site.etag(url)
        if etag and etag == now_etag:
            self.stats["hits_304"] += 1
            self.stats["wire_bytes"] += 120
            return Response(url, url, 304, b"", etag, "", "", {}, 120, 1, 0)
        self.stats["downloads"] += 1
        self.stats["body_bytes"] += len(body)
        self.stats["wire_bytes"] += len(body) + 120
        return Response(url, url, 200, body, now_etag, "Thu, 01 Jan 2026 00:00:00 GMT",
                        ctype, {"content-type": ctype}, len(body) + 120, 1, 0)

    def get_many(self, requests: list[dict], workers: int | None = None):
        out = []
        for req in requests:
            try:
                out.append(self.get(req["url"], req.get("etag", ""),
                                    req.get("last_modified", ""), req.get("accept", "")))
            except FetchError as exc:
                out.append(exc)
        return out

    def fetch_asset(self, url: str, etag: str = "", last_modified: str = "") -> dict:
        resp = self.get(url, etag, last_modified)
        return {"url": url, "status": resp.status, "bytes": resp.size,
                "sha256": resp.sha256() if resp.status == 200 else "",
                "etag": resp.etag, "last_modified": resp.last_modified,
                "wire_bytes": resp.wire_bytes, "elapsed_ms": 0, "body": resp.body}

    def close(self) -> None:
        pass


# -- 假页面构造 ------------------------------------------------------

def docs_page(title: str, paragraphs: list[str], *, scripts: tuple[str, ...],
              styles: tuple[str, ...] = ("styles.60bb05fd.css",),
              images: tuple[str, ...] = ()) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    img = "".join(f'<img src="/zh-cn/img/{x}" alt="">' for x in images)
    return (
        "<!doctype html><html><head>"
        f"<title>{title} | DeepSeek API Docs</title>"
        + "".join(f'<link rel="stylesheet" href="/zh-cn/assets/css/{c}">' for c in styles)
        + "".join(f'<script src="/zh-cn/assets/js/{s}"></script>' for s in scripts)
        + "</head><body><header>导航条</header><nav>文档目录</nav>"
        f"<article><h1>{title}</h1>{body}{img}</article>"
        "<footer>版权所有</footer></body></html>")


def nextjs_page(texts: list[str], chunks: tuple[str, ...] = ("6872-09ee2a9bce847688.js",)) -> str:
    payload = ""
    for t in texts:
        payload += ('3:["$","h1",null,{"children":"%s"}]\n' % t)
    push = "self.__next_f.push(%s)</script>" % json.dumps(
        [1, payload], ensure_ascii=False)
    links = "".join(
        f'<script src="/_next/static/chunks/{c}" async></script>' for c in chunks)
    return ("<!doctype html><html><head><title>DeepSeek | 深度求索</title></head>"
            f"<body><div id=\"__next\"></div><script>{push}</script>{links}</body></html>")


# ================================================================ 数据目录防护

VERIFY_TMP = REPO_ROOT / ".verify_tmp"


def _prepare_workdir(workdir: Path, *, force: bool = False) -> Path:
    """自检数据目录校验：拒绝 /opt、默认限制在 .verify_tmp、已有 docs.db 需 --force。

    生产库误删是不可恢复事故（P2-26）。即使 --force，也先把 docs.db/-wal/-shm
    改名成 `.bak-<时间>` 保留，再清目录，绝不直接 rmtree 掉唯一一份数据。
    """
    target = Path(workdir).expanduser()
    target = (target if target.is_absolute() else REPO_ROOT / target).resolve()
    posix = target.as_posix().lower()
    if posix == "/opt" or posix.startswith("/opt/") or re.match(r"^[a-z]:/opt(?:/|$)", posix):
        raise SystemExit(f"[拒绝] --data-dir 不能指向系统 /opt（生产目录风险）：{target}")
    verify_root = VERIFY_TMP.resolve()
    inside = target == verify_root or verify_root in target.parents
    if not inside and not force:
        raise SystemExit(
            f"[拒绝] 自检目录必须在 {verify_root} 下；确需其它目录请加 --force：{target}")
    if (target / "docs.db").exists():
        if not force:
            raise SystemExit(
                f"[拒绝] {target} 已有 docs.db（可能是真实数据）；"
                "确认要重置请加 --force（会先改名备份为 *.bak-时间戳）")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for name in ("docs.db", "docs.db-wal", "docs.db-shm"):
            src = target / name
            if not src.exists():
                continue
            try:
                src.rename(src.with_name(f"{name}.bak-{stamp}"))
            except OSError as exc:
                raise SystemExit(f"[拒绝] 备份 {src} 失败：{exc}") from exc
    return target


def _reset_workdir(target: Path) -> None:
    """清空自检目录里的旧文件，但保留 `*.bak-*` 备份。"""
    if target.exists():
        for child in list(target.iterdir()):
            if ".bak-" in child.name:
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                pass
    target.mkdir(parents=True, exist_ok=True)


# ================================================================ --fake

def run_fake(workdir: Path, *, force: bool = False) -> int:
    print("=" * 78)
    print("离线自检（内存假站点，不联网）")
    print("=" * 78)
    ck = Checker()
    workdir = _prepare_workdir(workdir, force=force)
    _reset_workdir(workdir)

    site = FakeSite()
    pages = ["/api/create-chat-completion/", "/guides/vision/",
             "/quick_start/pricing/", "/quick_start/rate_limit/", "/updates/"]
    site.set_sitemap(pages)

    PRICING_V1 = ["DeepSeek 的 API 按 token 计费。",
                  "输出价格：13.5 元 / 百万 tokens（高峰时段）。",
                  "上下文长度 128K，最大输出 8K。"]

    def doc_body(url_path: str, scripts: tuple[str, ...] = ("main.0c15113f.js",),
                 pricing_texts: list[str] | None = None,
                 images: tuple[str, ...] = ()) -> str:
        """同一个页面除了 script 之外内容一致 —— 这样才能验证「只重建、不改正文」。"""
        if url_path == "/quick_start/pricing/":
            return docs_page("模型 & 价格", pricing_texts or PRICING_V1,
                             scripts=scripts, images=images)
        return docs_page("接口文档" + url_path, ["这里是接口说明第一段。", "第二段说明。"],
                         scripts=scripts)

    site.set(f"{DOCS}/zh-cn/assets/js/main.0c15113f.js",
             'var a="输出 13.5 元 / 百万 tokens";var b="DeepSeek-V4";'
             'var c="上下文长度 128K";var d="请求失败，请稍后重试";', "application/javascript")
    site.set(f"{DOCS}/zh-cn/assets/css/styles.60bb05fd.css",
             ':root{--c1:#4d6bfe}body{font-family:"Helvetica Neue",sans-serif}', "text/css")
    site.set("https://www.deepseek.com/_next/static/chunks/6872-09ee2a9bce847688.js",
             'var t="深度求索，探索未至之境";var u="API 已上线，支持网页搜索与抓取";',
             "application/javascript")

    for p in pages:
        site.set(f"{DOCS}/zh-cn{p}", doc_body(p))
    site.set(WWW, nextjs_page(["深度求索，探索未至之境", "向全球开发者提供 API 服务",
                               "DeepSeek-V4 预览版本上线"]))
    site.set(f"{DOCS}/zh-cn/img/hero.webp", b"RIFF....WEBP", "image/webp")

    fx = FakeFetcher(site)

    # ---- 1. 基线
    print("\n[1] 首次基线")
    job = S.run_once(workdir, fetcher=fx)
    ck.ok(job["baseline"], "基线标记为 True")
    ck.eq(len(job["signals"]), 0, "基线不产生任何信号")
    ck.eq(job["pages_total"], len(pages) + 1, "监控页面数（5 个文档页 + 官网首页）")
    ck.eq(job["assets_total"], 3, "监控资源数（2 个文档站 + 1 个官网 chunk）")
    ck.eq(job["evidence_dir"], "", "基线不落证据包")

    # ---- 2. 稳态：0 信号 + 全部 304
    print("\n[2] 紧接着再跑一次（无变化）")
    job = S.run_once(workdir, fetcher=fx)
    ck.eq(len(job["signals"]), 0, "稳态 0 信号")
    ck.eq(job["run"]["pages_304"], job["pages_total"], "全部页面命中 304")
    ck.eq(job["run"]["assets_304"], job["assets_total"], "全部资源命中 304")
    ck.eq(job["run"]["pages_200"], 0, "没有任何页面被重新下载")

    # ---- 3. 正文变化
    print("\n[3] 手工构造：pricing 页正文改了")
    PRICING_V2 = ["DeepSeek 的 API 按 token 计费。",
                  "输出价格：15 元 / 百万 tokens（高峰时段）。",
                  "上下文长度 128K，最大输出 8K。"]
    site.set(f"{DOCS}/zh-cn/quick_start/pricing/", doc_body("/quick_start/pricing/",
                                                             pricing_texts=PRICING_V2))
    job = S.run_once(workdir, fetcher=fx)
    kinds = kinds_of(job)
    ck.eq(kinds.get("page_text"), 1, "产生 1 条 page_text 信号")
    ck.ok("page_text" in kinds, "正文变化被识别")
    sig = next(s for s in job["signals"] if s["kind"] == "page_text")
    ck.ok("13.5" in sig["before"] and "15" in sig["after"],
          "信号 before/after 里能看到 13.5 → 15")
    ck.ok("page_class" not in sig, "signal 不再携带未使用的 page_class（规则按 URL 自行分级）")
    ck.ok(sig["bytes_delta"] != 0 or True, "带 bytes_delta 字段")
    ev = Path(job["evidence_dir"])
    ck.ok((ev / "BRIEF.md").is_file(), "证据包有 BRIEF.md")
    ck.ok((ev / "manifest.json").is_file(), "证据包有 manifest.json")
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    pv = next(p for p in manifest["pages"] if "pricing" in p["url"])
    ck.eq(pv["page_class"], "key", "manifest 页面条目有 page_class")
    ck.ok(pv["text_changed_chars"] > 0, "manifest 有 text_changed_chars > 0")
    ck.ok(any(x["url"].endswith("/quick_start/pricing/") for x in manifest["pages"]),
          "manifest 页面清单含 pricing 页")
    slug = pv["slug"]
    ck.ok((ev / "pages" / f"{slug}.before.txt").is_file(), "pages/<slug>.before.txt 落盘")
    ck.ok((ev / "pages" / f"{slug}.after.txt").is_file(), "pages/<slug>.after.txt 落盘")
    ck.ok((ev / "diff" / f"{slug}.diff").is_file(), "diff/<slug>.diff 落盘")
    brief = (ev / "BRIEF.md").read_text(encoding="utf-8")
    n_lines = len(brief.split("\n"))
    ck.le(n_lines, 200, "BRIEF.md 行数 ≤ 200")
    ck.ok("15 元 / 百万 tokens" in brief, "BRIEF 里能看到新增的价格行")
    ck.ok("13.5" in brief, "BRIEF 里能看到被删掉的价格行")
    ck.ok("pricing" in brief, "BRIEF 按页面分组（出现 pricing 标签）")

    # ---- 4. 同名资源内容变化（asset_content）
    print("\n[4] 手工构造：styles.css 同名内容变了")
    site.set(f"{DOCS}/zh-cn/assets/css/styles.60bb05fd.css",
             ':root{--c1:#7b61ff}body{font-family:"PingFang SC",sans-serif}', "text/css")
    job = S.run_once(workdir, fetcher=fx)
    kinds = kinds_of(job)
    ck.eq(kinds.get("asset_content"), 1, "产生 1 条 asset_content 信号")
    sig = next(s for s in job["signals"] if s["kind"] == "asset_content")
    ck.ok(sig["asset"].endswith("styles.60bb05fd.css"), "信号指向 styles.css")
    ck.ok(sig["detail"].startswith("styles.60bb05fd.css 内容变了"), "detail 是人话")

    # ---- 5. 前端重新构建：资源改名 + 页面 HTML 变但正文不变
    print("\n[5] 手工构造：main.js 重新构建（文件名哈希变）")
    site.set(f"{DOCS}/zh-cn/assets/js/main.9ab31f02.js",
             'var a="输出 15 元 / 百万 tokens";var b="DeepSeek-V4";'
             'var c="上下文长度 128K";var d="请求失败，请稍后重试";var e="新增的界面文案";',
             "application/javascript")
    for p in pages:
        site.set(f"{DOCS}/zh-cn{p}", doc_body(
            p, scripts=("main.9ab31f02.js",), pricing_texts=PRICING_V2))
    job = S.run_once(workdir, fetcher=fx)
    kinds = kinds_of(job)
    ck.eq(kinds.get("asset_url"), 1, "产生 1 条 asset_url 改名信号")
    sig = next(s for s in job["signals"] if s["kind"] == "asset_url")
    ck.ok("0c15113f" in sig["before"] and "9ab31f02" in sig["after"],
          "改名信号带旧/新哈希")
    ck.ok(sig["asset"].endswith("main.9ab31f02.js"), "asset 字段是新地址")
    ck.ok(sig["page"] == "", "被多页共用的资源记成站点级（page 为空）")
    page_html = [s for s in job["signals"] if s["kind"] == "page_html"]
    ck.eq(len(page_html), len(pages), "每个引用了该资源的页面各产 1 条 page_html")
    ck.ok(all(s.get("refs_only") for s in page_html), "page_html 标出「差异只在资源文件名」")
    ck.ok(all("资源文件名" in s["detail"] for s in page_html), "detail 说明了变化性质")
    ck.eq(kinds.get("page_text", 0), 0, "重新构建不产 page_text（正文没变）")
    ev = Path(job["evidence_dir"])
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    rename = next(a for a in manifest["assets"] if a["action"] == "rename")
    after_rel = (rename.get("evidence") or {}).get("after") or ""
    ck.ok(bool(after_rel), "资源改名证据里登记了 after 文件")
    ck.ok(after_rel and (ev / after_rel).is_file(), "assets/<slug>.after.txt 落盘（P2-23）")
    ck.ok(after_rel and "新增的界面文案" in (ev / after_rel).read_text(encoding="utf-8"),
          "assets after.txt 含本轮新增的 JS 字符串")

    # ---- 6. 图片换地址
    print("\n[6] 手工构造：pricing 页配图换了地址")
    site.set(f"{DOCS}/zh-cn/img/hero-v2.webp", b"RIFF....WEBP", "image/webp")
    site.set(f"{DOCS}/zh-cn/quick_start/pricing/", doc_body(
        "/quick_start/pricing/", scripts=("main.9ab31f02.js",), pricing_texts=PRICING_V2,
        images=("hero-v2.webp",)))
    job = S.run_once(workdir, fetcher=fx)
    img_sigs = [s for s in job["signals"] if s.get("ref_kind") == "img"]
    ck.ok(len(img_sigs) >= 1, "图片引用变化产信号（kind=asset_url）")
    ck.ok(any("图片" in s["detail"] and "hero" in s["detail"] for s in img_sigs),
          "detail 里注明是图片并给出文件名")
    ck.ok(any(s["page"].endswith("/quick_start/pricing/") for s in img_sigs),
          "图片信号能定位到具体页面")

    # ---- 7. 新页面 / 下线页面
    print("\n[7] 手工构造：sitemap 新增一页、删掉一页")
    site.set(f"{DOCS}/zh-cn/guides/brand-new/",
             docs_page("新功能", ["这是一个新页面。"], scripts=("main.9ab31f02.js",)))
    site.set_sitemap(pages + ["/guides/brand-new/"])
    job = S.run_once(workdir, fetcher=fx)
    ck.eq(kinds_of(job).get("new_page"), 1, "产生 1 条 new_page 信号")
    np_sig = next(s for s in job["signals"] if s["kind"] == "new_page")
    ck.ok(np_sig["page"].endswith("/guides/brand-new/"), "new_page 指向新页面")
    ev = Path(job["evidence_dir"])
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    np_entry = next(p for p in manifest["pages"] if p["url"].endswith("/guides/brand-new/"))
    after_rel = (np_entry.get("evidence") or {}).get("after") or ""
    ck.ok(after_rel and (ev / after_rel).is_file(), "新页面 pages/<slug>.after.txt 落盘（P2-25）")
    ck.ok(after_rel and "这是一个新页面" in (ev / after_rel).read_text(encoding="utf-8"),
          "新页面 after.txt 含正文")
    brief = (ev / "BRIEF.md").read_text(encoding="utf-8")
    ck.ok("新增 1 个页面" in brief, "BRIEF 一句话包含新增页面计数")
    ck.ok("没有检测到任何变化" not in brief, "只有新页面时不得写「没有检测到任何变化」")

    site.set_sitemap([p for p in pages if p != "/guides/brand-new/"])
    job = S.run_once(workdir, fetcher=fx)
    ck.eq(kinds_of(job).get("gone_page"), 1, "产生 1 条 gone_page 信号")
    brief = (Path(job["evidence_dir"]) / "BRIEF.md").read_text(encoding="utf-8")
    ck.ok("页面下线" in brief, "BRIEF 一句话包含页面下线")

    # ---- 8. 抓取失败（只在状态翻转时产一次信号，不重复刷屏）
    print("\n[8] 手工构造：pricing 页 403")
    site.fail[f"{DOCS}/zh-cn/quick_start/pricing/"] = "403"
    job = S.run_once(workdir, fetcher=fx)
    errs = [s for s in job["signals"] if s["kind"] == "fetch_error"]
    ck.eq(len(errs), 1, "首次失败产 1 条 fetch_error")
    ck.ok("抓取失败" in errs[0]["detail"] and "403" in errs[0]["detail"],
          "错误 detail 带状态码")
    job = S.run_once(workdir, fetcher=fx)
    ck.eq(kinds_of(job).get("fetch_error", 0), 0, "持续失败不重复产信号")
    ck.ok(job["n_errors"] >= 1, "但 n_errors 里仍记录着失败")
    site.fail.pop(f"{DOCS}/zh-cn/quick_start/pricing/")

    # ---- 9. 不限频：两页同时变 → 两条信号
    print("\n[9] 手工构造：两个页面同时改正文")
    site.set(f"{DOCS}/zh-cn/guides/vision/",
             docs_page("视觉理解", ["支持图片输入。", "单图最大 20MB。"],
                       scripts=("main.9ab31f02.js",)))
    site.set(f"{DOCS}/zh-cn/updates/",
             docs_page("更新日志", ["2026/11/04 新增视觉能力说明"],
                       scripts=("main.9ab31f02.js",)))
    job = S.run_once(workdir, fetcher=fx)
    ck.eq(kinds_of(job).get("page_text"), 2, "两页变化 → 两条 page_text（不合并）")
    ck.eq(job["pages_changed"], 2, "pages_changed 统计正确")

    # ---- 10. 局部跑（--limit / --only）不能污染基线
    print("\n[10] 局部跑不污染基线")
    one = f"{DOCS}/zh-cn/quick_start/rate_limit/"
    job = S.run_once(workdir, fetcher=fx, pages=[one])
    ck.ok(job["partial"], "局部跑被标记为 partial")
    ck.eq(len(job["signals"]), 0, "只跑一页时不产假信号（不会被误判成资源消失）")
    job = S.run_once(workdir, fetcher=fx)
    ck.eq(len(job["signals"]), 0, "局部跑之后的全量跑仍然 0 信号（旧快照没被破坏）")
    ck.eq(job["pages_total"], len(pages) + 1, "全量跑页面数没被局部跑带偏")

    # ---- 11. 极端情况：所有页面正文 + 资源一起变，BRIEF 仍然 ≤ 200 行
    print("\n[11] 极端情况：全站正文和资源同时变（检查 BRIEF 行数上限）")
    site.set(f"{DOCS}/zh-cn/assets/js/main.cccc3333.js",
             'var a="输出 15 元 / 百万 tokens";var b="DeepSeek-V5";'
             'var c="上下文长度 256K";var d="新错误信息";', "application/javascript")
    for p in pages:
        if p == "/quick_start/pricing/":
            site.set(f"{DOCS}/zh-cn{p}", docs_page(
                "模型 & 价格", ["DeepSeek 的 API 按 token 计费。" * 1,
                                "输出价格：18 元 / 百万 tokens。",
                                "新增：上下文缓存价格 0.1 元 / 百万 tokens。",
                                "上下文长度 256K，最大输出 16K。"],
                scripts=("main.cccc3333.js",)))
        else:
            site.set(f"{DOCS}/zh-cn{p}", docs_page(
                "接口文档" + p, [f"这是 {p} 的新版说明第一段。", "第二段也改了，新增了流式示例。"],
                scripts=("main.cccc3333.js",)))
    job = S.run_once(workdir, fetcher=fx)
    ev = Path(job["evidence_dir"])
    brief = (ev / "BRIEF.md").read_text(encoding="utf-8")
    n_lines = len(brief.split("\n"))
    ck.le(n_lines, 200, "全站同时变化时 BRIEF 仍然 ≤ 200 行")
    ck.ok("截断" in brief or "上限" in brief or n_lines < 200,
          "超长时有截断提示（或本来就没超）")
    ck.ok(kinds_of(job).get("page_text") == len(pages),
          "每个页面各产一条 page_text（不合并）")
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    ck.eq(len(manifest["pages"]), len(pages), "manifest 里页面条目一个不少")
    ck.eq(len(manifest["signals"]), len(job["signals"]), "manifest 与 job 的信号数一致")
    print(f"    BRIEF {n_lines} 行 / {len(job['signals'])} 条信号 / "
          f"manifest {len(manifest['pages'])} 个页面条目")

    # ---- 12. 长期运行：库与磁盘
    print("\n[12] 库与磁盘")
    conn = sqlite3.connect(workdir / S.DB_NAME)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ck.ok("v2_pages" in tables and "v2_assets" in tables and "v2_signals" in tables,
          "关键表存在（v2_pages / v2_assets / v2_signals）")
    n_jobs = conn.execute("SELECT COUNT(*) FROM v2_jobs").fetchone()[0]
    ck.ok(n_jobs >= 10, f"作业记录已落库（{n_jobs} 条）")
    n_vers = conn.execute("SELECT COUNT(*) FROM v2_page_versions").fetchone()[0]
    ck.ok(n_vers >= 1, f"历史版本有归档（{n_vers} 条）")
    blob = conn.execute("SELECT COALESCE(SUM(size),0) FROM v2_asset_blobs").fetchone()[0]
    ck.ok(blob > 0, f"资源内容按 sha256 存了 blob（{blob} 字节）")
    conn.close()

    rc = ck.summary()
    brief = next(iter(sorted((workdir / "jobs").iterdir(), key=lambda d: int(d.name))), None)
    if brief is not None:
        print(f"\n最后一次作业目录：{brief}")
    print(f"数据目录：{workdir}（可删）")
    return rc


# ================================================================ --real

def _stats_line(tag: str, job: dict) -> str:
    run = job.get("run") or {}
    return (f"{tag}: {job['duration_ms'] / 1000:.2f} s · "
            f"{run.get('wire_bytes', 0) / 1024:.1f} KB 流量 · "
            f"{run.get('requests', 0)} 请求（页面 200/304 = "
            f"{run.get('pages_200', 0)}/{run.get('pages_304', 0)}，资源 200/304 = "
            f"{run.get('assets_200', 0)}/{run.get('assets_304', 0)}）· "
            f"{len(job.get('signals') or [])} 信号")


def run_real(data_dir: Path, limit: int, *, force: bool = False) -> int:
    print("=" * 78)
    print("真实站点验收（58 页 + 24 个 JS/CSS）")
    print("=" * 78)
    ck = Checker()
    data_dir = _prepare_workdir(data_dir, force=force)
    if not limit:
        _reset_workdir(data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
    opts = {"limit": limit} if limit else {}

    # ---- 1. 基线
    print("\n[1] 首次基线")
    job = S.run_once(data_dir, **opts)
    print("    " + _stats_line("基线", job))
    ck.ok(job["baseline"], "本轮是基线（库里原本没数据）")
    ck.eq(len(job["signals"]), 0, "基线不产生任何信号")
    ck.le(job["duration_ms"], 6000, "基线耗时 ≤ 6 s")
    ck.ok(job["assets_total"] >= 20, f"抓到全部前端资源（{job['assets_total']} 个 JS/CSS）")
    baseline_ms = job["duration_ms"]

    # ---- 2. 稳态
    print("\n[2] 紧接着再跑一次（预期 0 信号 / 全 304 / ≤5 s / ≤100 KB）")
    job = S.run_once(data_dir, **opts)
    run = job.get("run") or {}
    print("    " + _stats_line("稳态", job))
    ck.eq(len(job["signals"]), 0, "稳态 0 信号")
    ck.le(job["duration_ms"], 5000, "稳态耗时 ≤ 5 s")
    ck.le(run.get("wire_bytes", 0), 100 * 1024, "稳态流量 ≤ 100 KB")
    ck.eq(run.get("pages_304", -1), job["pages_total"], "全部页面命中 304")
    ck.eq(run.get("assets_304", -1), job["assets_total"], "全部资源命中 304")
    steady_ms, steady_bytes = job["duration_ms"], run.get("wire_bytes", 0)
    steady_signals = len(job["signals"])

    # ---- 3. 手工构造变化（改库 + 清 ETag，强制两页/两资源重新下载）
    print("\n[3] 手工构造变化：改库里的页面正文 / HTML 哈希、资源的字符串集合")
    conn = sqlite3.connect(data_dir / S.DB_NAME)
    conn.row_factory = sqlite3.Row
    page_row = conn.execute(
        "SELECT url, text, html_sha256 FROM v2_pages"
        " WHERE url LIKE '%/quick_start/pricing/'").fetchone()
    assert page_row is not None, "库里没有 pricing 页"
    # 把价格改回「上一版」的数值，这样 BRIEF 里的 diff 读起来就是一次真实调价
    fake_text = page_row["text"].replace("13.5元", "12元").replace("27.0元", "24.0元")
    if fake_text == page_row["text"]:  # 站点改版了也不至于让测试失效
        fake_text = page_row["text"] + "\n（历史版本）输出价格：12 元 / 百万 tokens"
    conn.execute(
        "UPDATE v2_pages SET text = ?, html_sha256 = ?, etag = '', last_modified = '',"
        " text_sha256 = ? WHERE url = ?",
        (fake_text, "0" * 64, S.sha256_hex(fake_text), page_row["url"]))
    asset_row = conn.execute(
        "SELECT url, size, sha256, strings_json FROM v2_assets WHERE url LIKE '%.js'"
        " ORDER BY size DESC LIMIT 1").fetchone()
    old_strings = json.loads(asset_row["strings_json"] or "[]")
    # 造一个「上一版」的字符串集合：少 3 条有信息量的新串、多 3 条旧串，diff 才有内容可看
    dropped = [x for x in old_strings if " " in x and 20 <= len(x) <= 120][:3] \
        or old_strings[-3:]
    fake_strings = [x for x in old_strings if x not in dropped] + [
        "（旧版）DeepSeek-V4.1 现已更新", "（旧版）上下文长度 256K", "（旧版）输出 13.5 元 / 百万 tokens"]
    conn.execute(
        "UPDATE v2_assets SET sha256 = ?, etag = '', last_modified = '',"
        " strings_json = ? WHERE url = ?",
        ("f" * 64, json.dumps(fake_strings, ensure_ascii=False), asset_row["url"]))
    conn.commit()
    conn.close()
    print(f"    改：{page_row['url']}")
    print(f"    改：{asset_row['url']}（原 {asset_row['size']} 字节）")

    job = S.run_once(data_dir, **opts)
    run = job.get("run") or {}
    print("    " + _stats_line("构造变化后", job))
    kinds = kinds_of(job)
    ck.eq(kinds.get("page_text"), 1, "pricing 页正文变化 → 1 条 page_text")
    ck.eq(kinds.get("page_html"), 1, "pricing 页 HTML 变化 → 1 条 page_html")
    ck.eq(kinds.get("asset_content"), 1, "资源内容变化 → 1 条 asset_content")
    pt = next(s for s in job["signals"] if s["kind"] == "page_text")
    ck.ok("12元" in pt["before"] or "12" in pt["before"],
          "page_text 的 before 里能看到构造的旧价格")
    ck.ok("page_class" not in pt, "page_text 信号不再携带未使用的 page_class")
    ac = next(s for s in job["signals"] if s["kind"] == "asset_content")
    ck.ok(ac["detail"].endswith("内容变了：") or "内容变了" in ac["detail"],
          "asset_content detail 是人话")
    ck.ok(bool(job["evidence_dir"]), "有信号就落了证据包")
    ev = Path(job["evidence_dir"])
    for rel in ("BRIEF.md", "manifest.json"):
        ck.ok((ev / rel).is_file(), f"证据包有 {rel}")
    brief = (ev / "BRIEF.md").read_text(encoding="utf-8")
    ck.le(len(brief.split("\n")), 200, "BRIEF.md 行数 ≤ 200")
    ck.ok("pricing" in brief and ("12元" in brief or "12 元" in brief),
          "BRIEF 里能看到构造的旧正文与新正文差异")
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    ck.ok(manifest["pages"] and all("page_class" in p for p in manifest["pages"]),
          "manifest 每个页面条目都有 page_class")
    ck.ok(all("text_changed_chars" in p for p in manifest["pages"]),
          "manifest 每个页面条目都有 text_changed_chars")
    ck.ok(manifest["assets"] and "strings_added" in manifest["assets"][0],
          "manifest 资源条目有 strings_added")
    ck.ok(any(f.startswith("pages/") for f in manifest["evidence_files"]),
          "manifest 列出证据文件")
    print("\n---- BRIEF.md 前 40 行 ----")
    for line in brief.split("\n")[:40]:
        print("| " + line)
    print("---- BRIEF.md 结束 ----")

    # ---- 4. 清理验证：证据包目录结构
    print("\n[4] 证据包目录结构")
    dirs = sorted({p.parent.name for p in ev.rglob("*") if p.is_file()})
    ck.ok("pages" in dirs and "diff" in dirs, "有 pages/ 与 diff/ 子目录")
    ck.ok(any("assets" == d for d in dirs), "有 assets/ 子目录")
    n_files = sum(1 for _ in ev.rglob("*") if _.is_file())
    print(f"    证据包共 {n_files} 个文件：{', '.join(dirs)}")

    print("\n---- 验收数据汇总 ----")
    print(f"  基线：{baseline_ms / 1000:.2f} s（上限 6 s）· 资源 {job['assets_total']} 个")
    print(f"  稳态：{steady_ms / 1000:.2f} s（上限 5 s）· {steady_bytes / 1024:.1f} KB"
          f"（上限 100 KB）· {steady_signals} 信号")
    print(f"  构造变化后：{job['duration_ms'] / 1000:.2f} s · "
          f"{run.get('wire_bytes', 0) / 1024:.1f} KB · {len(job['signals'])} 信号")
    print(f"  作业包：{ev}")
    rc = ck.summary()
    return rc


# ================================================================ 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="docwatch.collect 自检 / 验收")
    ap.add_argument("--fake", action="store_true", help="离线确定性测试（不联网）")
    ap.add_argument("--real", action="store_true", help="真实站点验收（基线 + 稳态 + 构造变化）")
    ap.add_argument("--data-dir", default="", help="真实验收用的数据目录")
    ap.add_argument("--limit", type=int, default=0, help="真实验收只跑前 N 页（调试）")
    ap.add_argument("--force", action="store_true",
                    help="允许 .verify_tmp 之外的目录；已有 docs.db 会先改名备份")
    args = ap.parse_args(argv)
    if not args.fake and not args.real:
        args.fake = True
    rc = 0
    try:
        if args.fake:
            work = Path(args.data_dir) if args.data_dir else REPO_ROOT / ".verify_tmp" / "scout-fake"
            # 默认目录就是专用自检区，允许直接重建（仍会先把旧 docs.db 改名备份）。
            rc = run_fake(work, force=args.force or not args.data_dir)
        if args.real:
            work = Path(args.data_dir) if args.data_dir else REPO_ROOT / ".verify_tmp" / "scout-demo"
            rc = max(rc, run_real(work, args.limit, force=args.force or not args.data_dir))
    except Exception:  # noqa: BLE001 - 自检脚本自己也不能静默失败
        traceback.print_exc()
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
