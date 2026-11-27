"""frontend_bundle_mirror 的离线测试：不联网，全部用 fake fetch 注入。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from tools import frontend_bundle_mirror as mirror
from tools.tests.support import cleanup, temp_dir

PAGE_URL = "https://platform.deepseek.com/"
MAIN_V1 = "https://fe-static.deepseek.com/platform/static/main.aaaa1111.js"
MAIN_CSS_V1 = "https://fe-static.deepseek.com/platform/static/main.aaaa1111.css"
MAIN_V2 = "https://fe-static.deepseek.com/platform/static/main.bbbb2222.js"
MAIN_CSS_V2 = "https://fe-static.deepseek.com/platform/static/main.bbbb2222.css"

MAIN_JS_V1 = (
    'f.k=e=>"static/"+e+".cccc3333.css",'
    'f.u=e=>"static/"+(({100:"chunkA",200:"chunkB",300:"chunkD"})[e]||e)+"."+'
    '({100:"aaaa1111",200:"bbbb2222",300:"dddd4444"})[e]+".js";'
    'appVersion:"2.7.1";commitId:"abc12345";let a="/api/v0/old";let b="旧文案";'
)
MAIN_JS_V2 = (
    'f.k=e=>"static/"+e+".cccc3333.css",'
    'f.u=e=>"static/"+(({100:"chunkA",200:"chunkB",300:"chunkC"})[e]||e)+"."+'
    '({100:"eeee5555",200:"ffff6666",300:"9999aaaa"})[e]+".js";'
    'appVersion:"2.8.0";commitId:"def67890";let a="/api/v0/new";let b="新文案";'
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, final_url: str = "") -> None:
        self.body = body
        self.status = status
        self.final_url = final_url


class FakeFetch:
    """按 URL 返回固定正文；未登记的 URL 记到 unexpected，测试结束时检查。"""

    def __init__(self, mapping: dict[str, bytes], missing: set[str] | None = None) -> None:
        self.mapping = dict(mapping)
        self.missing = set(missing or set())
        self.calls: list[str] = []
        self.unexpected: list[str] = []

    def __call__(self, url: str, accept: str | None = None) -> FakeResponse:
        self.calls.append(url)
        if url in self.missing:
            raise mirror.FetchError(url, "HTTP 404", status=404)
        if url not in self.mapping:
            self.unexpected.append(url)
            raise mirror.FetchError(url, "测试未登记该 URL", status=404)
        return FakeResponse(self.mapping[url], final_url=url)


def _html(version: str = "v1") -> str:
    if version == "v1":
        main_url, main_css = MAIN_V1, MAIN_CSS_V1
        heading = "欢迎"
        commit = "abc12345"
    else:
        main_url, main_css = MAIN_V2, MAIN_CSS_V2
        heading = "欢迎新版"
        commit = "def67890"
    return f"""<!doctype html>
<html><head><title>Platform Console</title>
<meta name="commit-id" content="{commit}">
<link rel="stylesheet" href="{main_css}">
<link rel="modulepreload" href="https://fe-static.deepseek.com/platform/static/preload.2222bbbb.js">
<link rel="preload" as="script" href="https://fe-static.deepseek.com/platform/static/preload2.3333cccc.js">
<link rel="icon" href="/favicon.1111dddd.ico">
<link rel="manifest" href="/site.webmanifest">
<script src="{main_url}"></script>
<script src="{main_url}"></script>
<style>.hero{{background:url('/img/bg.4444eeee.png')}}</style>
</head><body><h1>{heading}</h1><img src="/img/logo.5555ffff.png"></body></html>"""


def fixture(version: str = "v1") -> dict[str, bytes]:
    base = "https://fe-static.deepseek.com/platform/static/"
    common = {
        PAGE_URL: _html(version).encode("utf-8"),
        "https://fe-static.deepseek.com/platform/static/preload.2222bbbb.js": b"preload-body",
        "https://fe-static.deepseek.com/platform/static/preload2.3333cccc.js": b"preload-body",
        "https://platform.deepseek.com/favicon.1111dddd.ico": b"ICO",
        "https://platform.deepseek.com/site.webmanifest": '{"name":"平台"}'.encode("utf-8"),
        "https://platform.deepseek.com/img/logo.5555ffff.png": b"PNG",
        "https://platform.deepseek.com/img/bg.4444eeee.png": b"BG",
        "https://fe-static.deepseek.com/fonts/font.6666aaaa.woff2": b"FONT",
    }
    if version == "v1":
        mapping = {
            **common,
            MAIN_V1: MAIN_JS_V1.encode(),
            MAIN_CSS_V1: b".x{color:red}@font-face{font-family:F;src:url('/fonts/font.6666aaaa.woff2')}",
            base + "chunkA.aaaa1111.js": 'let a=1;let b="旧文案";'.encode("utf-8"),
            base + "chunkB.bbbb2222.js": b"let c=2;",
            base + "chunkD.dddd4444.js": b"let d=4;",
            base + "chunkA.cccc3333.css": b".chunkA{color:red}",
            base + "chunkD.cccc3333.css": b".chunkD{color:green}",
        }
        return mapping
    mapping = {
        **common,
        MAIN_V2: MAIN_JS_V2.encode(),
        MAIN_CSS_V2: b".x{color:blue}@font-face{font-family:F;src:url('/fonts/font.6666aaaa.woff2')}",
        base + "chunkA.eeee5555.js": 'let a=1;let b="旧文案";'.encode("utf-8"),
        base + "chunkB.ffff6666.js": 'let c=2;let d="新增B";i18n:{usageLimitReached:"使用额度已达上限"}'.encode("utf-8"),
        base + "chunkC.9999aaaa.js": b"let c=3;",
        base + "chunkA.cccc3333.css": b".chunkA{color:red}",
        base + "chunkC.cccc3333.css": b".chunkC{color:blue}",
    }
    return mapping


MISSING = {"https://fe-static.deepseek.com/platform/static/chunkB.cccc3333.css"}

TARGET = {"source": "platform", "label": "Platform 控制台", "url": PAGE_URL}


def rename_fixture(version: str) -> dict[str, bytes]:
    """构造「旧 chunk 删除 + 新 chunk 新增」场景，含完全相同与部分相同两种分片。"""
    base = "https://fe-static.deepseek.com/platform/static/"
    if version == "old":
        main_url = base + "main.1111aaaa.js"
        main_text = (
            'f.k=e=>"static/"+e+".cccc3333.css",'
            'f.u=e=>"static/"+(({100:"chunkOld",200:"chunkSameOld",300:"chunkGone"})[e]||e)+"."+'
            '({100:"aaaa1111",200:"bbbb2222",300:"dddd4444"})[e]+".js";appVersion:"1.0";'
        )
        html = f"<html><head><title>Rename</title><script src=\"{main_url}\"></script></head><body>same</body></html>"
        return {
            PAGE_URL: html.encode("utf-8"),
            main_url: main_text.encode(),
            base + "chunkOld.aaaa1111.js": (
                'let common="共同文案";let oldOnly="仅旧版";'
                'let commonApi="/api/v0/common";let oldApi="/api/v0/old";'
                'i18n:{commonKey:"共同",oldKey:"旧"};'
            ).encode(),
            base + "chunkSameOld.bbbb2222.js": 'let same="完全相同";'.encode(),
            base + "chunkGone.dddd4444.js": 'let gone="已删除";'.encode(),
            base + "chunkOld.cccc3333.css": b".common{color:red}.old{color:green}",
            base + "chunkSameOld.cccc3333.css": b".same{color:gray}",
            base + "chunkGone.cccc3333.css": b".gone{color:black}",
        }
    main_url = base + "main.2222bbbb.js"
    main_text = (
        'f.k=e=>"static/"+e+".cccc3333.css",'
        'f.u=e=>"static/"+(({100:"chunkNew",200:"chunkSameNew"})[e]||e)+"."+'
        '({100:"eeee5555",200:"ffff6666"})[e]+".js";appVersion:"2.0";'
    )
    html = f"<html><head><title>Rename</title><script src=\"{main_url}\"></script></head><body>same</body></html>"
    return {
        PAGE_URL: html.encode("utf-8"),
        main_url: main_text.encode(),
        base + "chunkNew.eeee5555.js": (
            'let common="共同文案";let newOnly="仅新版";'
            'let commonApi="/api/v0/common";let newApi="/api/v0/new";'
            'i18n:{commonKey:"共同",newKey:"新"};'
        ).encode(),
        base + "chunkSameNew.ffff6666.js": 'let same="完全相同";'.encode(),
        base + "chunkNew.cccc3333.css": b".common{color:red}.new{color:blue}",
        base + "chunkSameNew.cccc3333.css": b".same{color:gray}",
    }


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir("mirror-")
        self.addCleanup(cleanup, self.tmp)


class RspackParseTests(unittest.TestCase):
    def test_parses_chat_two_table_and_platform_constant(self) -> None:
        chat = (
            'h.u=e=>"static/"+(({100:"named-chunk"})[e]||e)+"."+'
            '({100:"aaaa1111",200:"bbbb2222"})[e]+".js";'
            'h.k=e=>"static/"+(({5:"style"})[e]||e)+"."+({5:"cccc3333"})[e]+".css";'
        )
        maps = mirror.parse_rspack_maps(chat)
        self.assertEqual(maps["js"]["hashes"], {"100": "aaaa1111", "200": "bbbb2222"})
        self.assertEqual(maps["js"]["names"], {"100": "named-chunk"})
        self.assertEqual(maps["css"]["hashes"], {"5": "cccc3333"})

        platform = mirror.parse_rspack_maps(MAIN_JS_V1)
        self.assertEqual(platform["css"]["constant"], "cccc3333.css")
        self.assertEqual(platform["js"]["hashes"]["100"], "aaaa1111")

    def test_exponent_chunk_keys_are_normalized(self) -> None:
        text = 'f.u=e=>"static/"+e+"."+({1e3:"aaaa1111",200:"bbbb2222"})[e]+".js";'
        maps = mirror.parse_rspack_maps(text)
        self.assertEqual(maps["js"]["hashes"]["1000"], "aaaa1111")
        self.assertNotIn("3", maps["js"]["hashes"])

    def test_css_constant_builds_url_per_js_chunk(self) -> None:
        items = mirror.build_chunk_items(MAIN_V1, mirror.parse_rspack_maps(MAIN_JS_V1))
        css = {item["key"]: item for item in items if item["kind"] == "css"}
        self.assertEqual(
            css["chunk:css:chunkA"]["url"],
            "https://fe-static.deepseek.com/platform/static/chunkA.cccc3333.css",
        )
        self.assertEqual(css["chunk:css:chunkD"]["chunk_id"], "300")


class PageAssetTests(TempDirTestCase):
    def test_parses_script_style_preload_and_static_assets(self) -> None:
        parsed = mirror.parse_page_assets(_html("v1"), PAGE_URL)
        by_url = {asset["url"]: asset for asset in parsed["assets"]}
        self.assertEqual(by_url[MAIN_V1]["role"], "entry")
        self.assertEqual(by_url[MAIN_V1]["kind"], "js")
        self.assertEqual(by_url[MAIN_CSS_V1]["kind"], "css")
        self.assertEqual(
            by_url["https://fe-static.deepseek.com/platform/static/preload.2222bbbb.js"]["role"],
            "entry",
        )
        self.assertEqual(by_url["https://platform.deepseek.com/site.webmanifest"]["kind"], "manifest")
        self.assertEqual(by_url["https://platform.deepseek.com/img/logo.5555ffff.png"]["role"], "asset")
        self.assertIn("https://platform.deepseek.com/img/bg.4444eeee.png", by_url)
        self.assertEqual(sum(1 for asset in parsed["assets"] if asset["url"] == MAIN_V1), 1)


class SnapshotTests(TempDirTestCase):
    def test_module_level_fetch_is_the_default_fetch_path(self) -> None:
        """CLI 不传 fetch_func 时走模块级 fetch；测试通过 monkeypatch 避免真联网。"""
        fake = FakeFetch(fixture("v1"), missing=MISSING)
        with mock.patch.object(mirror, "fetch", fake):
            result = mirror.create_snapshot(
                TARGET,
                self.tmp / "bundles",
                snapshot_id="20260101T000000Z-default1",
            )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(fake.unexpected, [])
        self.assertIn(MAIN_V1, fake.calls)

    def test_manifest_generation_blob_dedupe_and_page_files(self) -> None:
        root = self.tmp / "bundles"
        fake = FakeFetch(fixture("v1"), missing=MISSING)
        result = mirror.create_snapshot(TARGET, root, fetch_func=fake, snapshot_id="20260101T000000Z-abc12345")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(fake.unexpected, [])
        manifest = result["manifest"]
        files = {item["key"]: item for item in manifest["files"]}

        self.assertIn("js:platform/static/main.js", files)
        self.assertIn("chunk:js:chunkA", files)
        self.assertIn("chunk:css:chunkA", files)
        self.assertEqual(files["chunk:css:chunkB"]["status"], "missing")
        self.assertEqual(files["js:platform/static/main.js"]["size"], len(MAIN_JS_V1.encode()))
        self.assertEqual(len(files["js:platform/static/main.js"]["hash"]), 64)
        self.assertTrue(files["js:platform/static/main.js"]["blob"].startswith("blobs/"))

        # 两个 URL 内容相同 -> 共用同一个 blob，只落一份。
        preload1 = next(item for item in manifest["files"] if "preload.2222bbbb" in item["url"])
        preload2 = next(item for item in manifest["files"] if "preload2.3333cccc" in item["url"])
        self.assertEqual(preload1["blob"], preload2["blob"])
        self.assertEqual({preload1["status"], preload2["status"]}, {"downloaded", "deduped"})
        self.assertEqual((root / preload1["blob"]).stat().st_size, len(b"preload-body"))

        # 同一 main.js 在 HTML 里出现两次，清单只保留一条。
        self.assertEqual(sum(1 for item in manifest["files"] if item["url"] == MAIN_V1), 1)

        snap_dir = result["snapshot_dir"]
        self.assertEqual((snap_dir / "page.html").read_bytes(), fixture("v1")[PAGE_URL])
        self.assertIn("欢迎", (snap_dir / "page.txt").read_text(encoding="utf-8"))
        self.assertEqual(manifest["page"]["html_sha256"], mirror.sha256_bytes(fixture("v1")[PAGE_URL]))

        summary = result["summary"]
        self.assertEqual(summary["totals"]["missing"], 1)
        self.assertEqual(summary["totals"]["required_missing"], 0)
        self.assertEqual(summary["totals"]["css_probe_404"], 1)
        self.assertEqual(summary["totals"]["files"], len(manifest["files"]))
        self.assertGreater(summary["totals"]["downloaded"], 0)
        self.assertTrue(summary["complete"])
        self.assertTrue(manifest["complete"])

    def test_second_snapshot_reuses_local_blobs_and_missing_stays_missing(self) -> None:
        root = self.tmp / "bundles"
        first_fake = FakeFetch(fixture("v1"), missing=MISSING)
        first = mirror.create_snapshot(TARGET, root, fetch_func=first_fake, snapshot_id="20260101T000000Z-abc12345")
        self.assertEqual(first["exit_code"], 0)
        blob_count_before = len(list((root / "blobs").glob("*/*")))

        second_fake = FakeFetch(fixture("v1"), missing=MISSING)
        second = mirror.create_snapshot(TARGET, root, fetch_func=second_fake, snapshot_id="20260101T010000Z-abc12345")
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(second_fake.unexpected, [])
        # 不可变 URL（带内容哈希）不应再次下载；非哈希 URL（site.webmanifest）仍需重新抓。
        self.assertNotIn(MAIN_V1, second_fake.calls)
        self.assertNotIn("https://fe-static.deepseek.com/platform/static/chunkA.aaaa1111.js", second_fake.calls)
        self.assertIn("https://platform.deepseek.com/site.webmanifest", second_fake.calls)
        files = {item["key"]: item for item in second["manifest"]["files"]}
        self.assertEqual(files["js:platform/static/main.js"]["status"], "cached")
        self.assertEqual(files["chunk:css:chunkB"]["status"], "missing")
        self.assertEqual(len(list((root / "blobs").glob("*/*"))), blob_count_before)

        diff = mirror.diff_snapshots(root, "platform")
        self.assertEqual(diff["totals"]["missing"], 1)
        self.assertEqual(diff["missing_breakdown"]["required"], 0)
        self.assertEqual(diff["missing_breakdown"]["css_probe_404"], 1)
        missing_keys = [item["key"] for item in diff["files"] if item["status"] == "missing"]
        self.assertIn("chunk:css:chunkB", missing_keys)
        self.assertFalse(diff["page_text_changed"])

    def test_main_js_failure_is_critical_but_snapshot_is_saved(self) -> None:
        root = self.tmp / "bundles"
        fake = FakeFetch(fixture("v1"), missing={MAIN_V1})
        result = mirror.create_snapshot(TARGET, root, fetch_func=fake, snapshot_id="20260101T000000Z-failed00")
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(result["summary"]["critical_main_failed"])
        self.assertFalse(result["summary"]["complete"])
        self.assertTrue(result["summary"]["incomplete_reason"])
        self.assertTrue((result["snapshot_dir"] / "manifest.json").is_file())
        files = {item["key"]: item for item in result["manifest"]["files"]}
        self.assertEqual(files["js:platform/static/main.js"]["status"], "missing")
        self.assertGreaterEqual(result["summary"]["totals"]["required_missing"], 1)


class DiffTests(TempDirTestCase):
    def _make_pair(self) -> tuple[Path, dict, dict]:
        root = self.tmp / "bundles"
        fake1 = FakeFetch(fixture("v1"), missing=MISSING)
        fake2 = FakeFetch(fixture("v2"), missing=MISSING)
        first = mirror.create_snapshot(TARGET, root, fetch_func=fake1, snapshot_id="20260101T000000Z-abc12345")
        second = mirror.create_snapshot(TARGET, root, fetch_func=fake2, snapshot_id="20260102T000000Z-def67890")
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(fake1.unexpected, [])
        self.assertEqual(fake2.unexpected, [])
        return root, first["manifest"], second["manifest"]

    def test_added_removed_content_changed_url_changed(self) -> None:
        root, _old_manifest, _new_manifest = self._make_pair()
        diff = mirror.diff_snapshots(root, "platform")
        totals = diff["totals"]
        self.assertEqual(totals["added"], 2)  # chunkC JS + chunkC CSS
        self.assertEqual(totals["removed"], 2)  # chunkD JS + chunkD CSS
        self.assertEqual(totals["content_changed"], 3)  # main.js / main.css / chunkB.js
        self.assertEqual(totals["url_changed"], 1)  # chunkA.js 内容不变，URL 变了
        self.assertEqual(totals["missing"], 1)  # chunkB.css 两次都 404
        self.assertTrue(diff["page_text_changed"])

        self.assertIn("新文案", diff["cjk_added"])
        self.assertIn("新增B", diff["cjk_added"])
        self.assertIn("/api/v0/new", diff["api_added"])
        self.assertIn("usageLimitReached", diff["i18n_added"])
        self.assertIn(".x{color:blue}", diff["css_rules_added"])
        self.assertIn(".x{color:red}", diff["css_rules_removed"])
        snippet_text = " ".join(item["text"] for item in diff["code_snippets"])
        self.assertIn("新增B", snippet_text)

        statuses = {(item["key"], item["status"]) for item in diff["files"]}
        self.assertIn(("chunk:js:chunkC", "added"), statuses)
        self.assertIn(("chunk:js:chunkD", "removed"), statuses)
        self.assertIn(("chunk:js:chunkA", "url_changed"), statuses)
        self.assertIn(("chunk:js:chunkB", "content_changed"), statuses)

        explicit = mirror.diff_snapshots(
            root,
            "platform",
            from_id="20260101T000000Z-abc12345",
            to_id="20260102T000000Z-def67890",
        )
        self.assertEqual(explicit["totals"], totals)

    def test_old_blob_missing_marks_baseline_missing_and_does_not_guess(self) -> None:
        root, old_manifest, _new_manifest = self._make_pair()
        old_item = next(item for item in old_manifest["files"] if item["key"] == "chunk:js:chunkB")
        (root / old_item["blob"]).unlink()

        diff = mirror.diff_snapshots(root, "platform")
        self.assertTrue(diff["baseline_missing"])
        self.assertGreaterEqual(diff["baseline_missing_files"], 1)
        record = next(item for item in diff["files"] if item["key"] == "chunk:js:chunkB")
        self.assertTrue(record["baseline_missing"])
        self.assertIn("旧 blob 缺失", record["note"])
        # 不能把新分片里的新增文案当成「新增」。
        self.assertNotIn("新增B", diff["cjk_added"])
        self.assertNotIn("usageLimitReached", diff["i18n_added"])


class RenameDiffTests(TempDirTestCase):
    def test_global_set_diff_and_renamed_same_content(self) -> None:
        root = self.tmp / "bundles"
        old = mirror.create_snapshot(
            TARGET, root, fetch_func=FakeFetch(rename_fixture("old")), snapshot_id="20260201T000000Z-old00001"
        )
        new = mirror.create_snapshot(
            TARGET, root, fetch_func=FakeFetch(rename_fixture("new")), snapshot_id="20260202T000000Z-new00002"
        )
        self.assertEqual(old["exit_code"], 0)
        self.assertEqual(new["exit_code"], 0)

        diff = mirror.diff_snapshots(root, "platform")
        totals = diff["totals"]
        self.assertEqual(diff["renamed_same_content"], 2)  # chunkSame 的 JS + CSS
        self.assertIn("仅新版", diff["cjk_added"])
        self.assertIn("仅旧版", diff["cjk_removed"])
        self.assertIn("已删除", diff["cjk_removed"])
        self.assertNotIn("共同文案", diff["cjk_added"])
        self.assertNotIn("共同文案", diff["cjk_removed"])
        self.assertNotIn("完全相同", diff["cjk_added"])
        self.assertNotIn("完全相同", diff["cjk_removed"])
        self.assertIn("/api/v0/new", diff["api_added"])
        self.assertIn("/api/v0/old", diff["api_removed"])
        self.assertNotIn("/api/v0/common", diff["api_added"])
        self.assertIn("newKey", diff["i18n_added"])
        self.assertIn("oldKey", diff["i18n_removed"])
        self.assertNotIn("commonKey", diff["i18n_added"])
        self.assertIn(".new{color:blue}", diff["css_rules_added"])
        self.assertIn(".old{color:green}", diff["css_rules_removed"])
        self.assertNotIn(".common{color:red}", diff["css_rules_added"])
        renamed_records = [item for item in diff["files"] if item["status"] == "renamed_same_content"]
        self.assertTrue(renamed_records)


def _write_snapshot(root: Path, source: str, snapshot_id: str, files: list[tuple[str, str, str, str]]) -> None:
    """写一个最小快照（manifest + blobs），供全局集合差分场景使用。"""
    snap_dir = root / "snapshots" / source / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for key, name, kind, text in files:
        body = text.encode("utf-8")
        blob, _ = mirror.write_blob(root, body, kind if kind in ("js", "css") else "js")
        items.append(
            {
                "key": key,
                "name": name,
                "kind": kind,
                "role": "chunk",
                "status": "downloaded",
                "hash": mirror.sha256_bytes(body),
                "size": len(body),
                "blob": blob,
                "url": f"https://fe-static.deepseek.com/platform/static/{name}",
            }
        )
    manifest = {
        "snapshot_id": snapshot_id,
        "source": source,
        "files": items,
        "page": {"text_sha256": "same", "title": "T"},
        "build_meta": {},
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )


class GlobalSetDiffScenarioTests(TempDirTestCase):
    """P1-07 四个探针场景：unchanged / content_changed 也必须参与全局集合差分。"""

    def _diff(self, old_files: list[tuple[str, str, str, str]], new_files) -> dict:
        root = self.tmp / "bundles"
        _write_snapshot(root, "platform", "20260301T000000Z-old", old_files)
        _write_snapshot(root, "platform", "20260302T000000Z-new", new_files)
        return mirror.diff_snapshots(root, "platform")

    def test_global_set_diff_covers_unchanged_and_content_changed(self) -> None:
        unchanged = ("chunk:js:same", "same.js", "js", 'let common="公共文案";let api="/api/v0/common";')
        cases = [
            (
                "unchanged 已有公共文案，新增分片重复不算新增",
                [unchanged],
                [
                    unchanged,
                    ("chunk:js:new", "new.js", "js", 'let common="公共文案";let api="/api/v0/common";let fresh="新增文案";'),
                ],
                {"cjk_added_absent": ["公共文案"], "api_added_absent": ["/api/v0/common"], "cjk_added_present": ["新增文案"]},
            ),
            (
                "content_changed 搬到新增分片：同一文案不能同时 added+removed",
                [("chunk:js:100", "100.js", "js", 'let moved="搬家文案";')],
                [
                    ("chunk:js:100", "100.js", "js", 'let other="换掉";'),
                    ("chunk:js:101", "101.js", "js", 'let moved="搬家文案";'),
                ],
                {"cjk_added_absent": ["搬家文案"], "cjk_removed_absent": ["搬家文案"]},
            ),
            (
                "content_changed 新增了 unchanged 已有的文案：不算新增",
                [
                    unchanged,
                    ("chunk:js:100", "100.js", "js", "let x=1;"),
                ],
                [
                    unchanged,
                    ("chunk:js:100", "100.js", "js", 'let common="公共文案";let newOnly="内容变化新增";'),
                ],
                {"cjk_added_absent": ["公共文案"], "cjk_added_present": ["内容变化新增"]},
            ),
            (
                "删除分片的文案仍存在于 unchanged：不算删除",
                [
                    unchanged,
                    ("chunk:js:102", "102.js", "js", 'let common="公共文案";let gone="已删除";'),
                ],
                [unchanged],
                {"cjk_removed_absent": ["公共文案"], "cjk_removed_present": ["已删除"]},
            ),
        ]
        for title, old_files, new_files, expected in cases:
            with self.subTest(title=title):
                diff = self._diff(old_files, new_files)
                for key in expected.get("cjk_added_absent", []):
                    self.assertNotIn(key, diff["cjk_added"])
                for key in expected.get("api_added_absent", []):
                    self.assertNotIn(key, diff["api_added"])
                for key in expected.get("cjk_added_present", []):
                    self.assertIn(key, diff["cjk_added"])
                for key in expected.get("cjk_removed_absent", []):
                    self.assertNotIn(key, diff["cjk_removed"])
                for key in expected.get("cjk_removed_present", []):
                    self.assertIn(key, diff["cjk_removed"])


if __name__ == "__main__":
    unittest.main()
