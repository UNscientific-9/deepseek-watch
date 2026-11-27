from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from webwatch import frontend_evidence as fe


def _item(key: str, url: str, digest: str, role: str, kind: str, name: str) -> dict:
    return {"key": key, "url": url, "hash": digest, "role": role, "kind": kind, "name": name}


def _snapshot(main_url: str, css_url: str, chunk_url: str, main_digest: str, css_digest: str, chunk_digest: str, app_version: str) -> dict:
    return {
        "engine": "rspack",
        "manifest": [
            _item("js:chat/static/main.js", main_url, main_digest, "entry", "js", "main.js"),
            _item("css:chat/static/main.css", css_url, css_digest, "entry", "css", "main.css"),
            _item("chunk:js:11165", chunk_url, chunk_digest, "chunk", "js", "11165"),
        ],
        "build_meta": {"appVersion": app_version},
    }


class RspackMapTests(unittest.TestCase):
    def test_parses_chat_two_table_map(self) -> None:
        text = (
            'h.u=e=>"static/"+(({100:"named-chunk"})[e]||e)+"."+'
            '({100:"aaaa1111",200:"bbbb2222"})[e]+".js";'
            'h.k=e=>"static/"+(({5:"style"})[e]||e)+"."+({5:"cccc3333"})[e]+".css";'
        )
        maps = fe.parse_rspack_maps(text)
        self.assertEqual(maps["js"]["hashes"], {"100": "aaaa1111", "200": "bbbb2222"})
        self.assertEqual(maps["js"]["names"], {"100": "named-chunk"})
        self.assertEqual(maps["css"]["hashes"], {"5": "cccc3333"})

    def test_parses_platform_single_table_map(self) -> None:
        text = 'f.u=e=>"static/"+e+"."+({100:"aaaa1111"})[e]+".js";'
        maps = fe.parse_rspack_maps(text)
        self.assertEqual(maps["js"]["hashes"], {"100": "aaaa1111"})
        self.assertEqual(maps["js"]["names"], {})

    def test_parses_platform_constant_css_hash(self) -> None:
        text = 'f.k=e=>"static/"+e+".b116c9f4a0.css";'
        maps = fe.parse_rspack_maps(text)
        self.assertEqual(maps["css"]["constant"], "b116c9f4a0.css")


class RemovedCopyTests(unittest.TestCase):
    def test_removed_i18n_reference_is_visible_and_has_diff(self) -> None:
        old = _snapshot(
            "https://fe-static.deepseek.com/platform/static/main.old.js",
            "https://fe-static.deepseek.com/platform/static/main.old.css",
            "https://fe-static.deepseek.com/platform/static/2653.old.js",
            "oldmain",
            "oldcss",
            "oldchunk",
            "1.0.1",
        )
        new = _snapshot(
            "https://fe-static.deepseek.com/platform/static/main.new.js",
            "https://fe-static.deepseek.com/platform/static/main.new.css",
            "https://fe-static.deepseek.com/platform/static/2653.new.js",
            "newmain",
            "newcss",
            "newchunk",
            "1.0.1",
        )
        definitions = (
            'let a={deleteAccountWarnNoServiceTitle:"服务将无法访问",'
            'deleteAccountWarnLostDataTitle:"历史对话将被删除",'
            'deleteAccountNoMoneyBackTitle:"充值余额无法恢复",'
            'deleteAccountNoMoneyBackDesc:"如您的开放平台账户中尚有未消费的充值余额，注销即视为放弃金额。",'
            'deleteAccountWarnFrequencyTitle:"请勿频繁重复注销"};'
        )
        old_chunk = (
            definitions
            + "let n=[[a.deleteAccountWarnNoServiceTitle],"
            "[a.deleteAccountWarnLostDataTitle],"
            "[a.deleteAccountNoMoneyBackTitle,a.deleteAccountNoMoneyBackDesc],"
            "[a.deleteAccountWarnFrequencyTitle]];"
        )
        new_chunk = (
            definitions
            + "let n=[[a.deleteAccountWarnNoServiceTitle],"
            "[a.deleteAccountWarnLostDataTitle],"
            "[a.deleteAccountWarnFrequencyTitle]];"
        )
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1.0.1";',
            new["manifest"][0]["url"]: b'appVersion:"1.0.1";',
            old["manifest"][1]["url"]: b".x{color:red}",
            new["manifest"][1]["url"]: b".x{color:red}",
            old["manifest"][2]["url"]: old_chunk.encode(),
            new["manifest"][2]["url"]: new_chunk.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old,
                new,
                cache_dir=None,
                source="platform",
                label="Platform 控制台",
                old_commit="old",
                new_commit="new",
            )

        assert evidence is not None
        refs = {item["key"]: item for item in evidence["removed_i18n_refs"]}
        self.assertIn("deleteAccountNoMoneyBackTitle", refs)
        self.assertIn("充值余额无法恢复", refs["deleteAccountNoMoneyBackTitle"]["values"])
        self.assertIn("deleteAccountNoMoneyBackDesc", refs)
        removed = " ".join(item["text"] for item in evidence["removed_code_snippets"])
        self.assertIn("deleteAccountNoMoneyBackTitle", removed)
        groups = fe.build_changes_detail(evidence, 2)
        removed_group = next(group for group in groups if group["page"] == "删除用户文案")
        self.assertIn("充值余额无法恢复", " ".join(removed_group["bullets"]))
        self.assertIn("- 充值余额无法恢复", removed_group["diff"])
        # 主包只有 appVersion 变化，不应制造假的代码 diff。
        self.assertFalse(any("main.js" in str(item.get("file") or "") for item in evidence["file_diffs"]))

    def test_chunk_rename_still_reports_removed_i18n_reference(self) -> None:
        old_main = "https://fe-static.deepseek.com/platform/static/main.old.js"
        new_main = "https://fe-static.deepseek.com/platform/static/main.new.js"
        old_chunk = "https://fe-static.deepseek.com/platform/static/2653.old.js"
        new_chunk = "https://fe-static.deepseek.com/platform/static/2654.new.js"
        old_css = "https://fe-static.deepseek.com/platform/static/main.old.css"
        new_css = "https://fe-static.deepseek.com/platform/static/main.new.css"
        definitions = (
            'let a={deleteAccountNoMoneyBackTitle:"充值余额无法恢复",'
            'deleteAccountNoMoneyBackDesc:"注销即视为放弃金额。"};'
        )
        old = {
            "engine": "rspack",
            "manifest": [
                _item("js:entry:platform/static/main.js", old_main, "oldmain", "entry", "js", "main.js"),
                _item("css:entry:platform/static/main.css", old_css, "oldcss", "entry", "css", "main.css"),
                _item("chunk:js:2653", old_chunk, "oldchunk", "chunk", "js", "2653"),
            ],
            "build_meta": {"appVersion": "1.0.1"},
        }
        new = {
            "engine": "rspack",
            "manifest": [
                _item("js:entry:platform/static/main.js", new_main, "newmain", "entry", "js", "main.js"),
                _item("css:entry:platform/static/main.css", new_css, "newcss", "entry", "css", "main.css"),
                _item("chunk:js:2654", new_chunk, "newchunk", "chunk", "js", "2654"),
            ],
            "build_meta": {"appVersion": "1.0.1"},
        }
        mapping = {
            old_main: b'appVersion:"1.0.1";',
            new_main: b'appVersion:"1.0.1";',
            old_css: b".x{color:red}",
            new_css: b".x{color:red}",
            old_chunk: (definitions + "let n=[[a.deleteAccountNoMoneyBackTitle,a.deleteAccountNoMoneyBackDesc]];").encode(),
            new_chunk: (definitions + "let n=[];").encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old, new, cache_dir=None, source="platform", label="Platform 控制台",
                old_commit="old", new_commit="new",
            )

        assert evidence is not None
        refs = {item["key"] for item in evidence["removed_i18n_refs"]}
        self.assertIn("deleteAccountNoMoneyBackTitle", refs)
        self.assertTrue(any(item.get("file") == "分片整体" for item in evidence["file_diffs"]))

    def test_runtime_hash_map_is_not_treated_as_code_change(self) -> None:
        old_main = 'x=1;u={100:"aaaaaaaa",200:"bbbbbbbb",300:"cccccccc"};'
        new_main = 'x=1;u={100:"11111111",200:"22222222",300:"33333333"};'
        pieces = fe.code_diff_pieces(old_main, new_main)
        self.assertEqual(pieces["added"], [])
        self.assertEqual(pieces["removed"], [])


class EvidenceTests(unittest.TestCase):
    def test_builds_user_visible_evidence_without_network(self) -> None:
        old = _snapshot(
            "https://fe-static.deepseek.com/chat/static/main.old.js",
            "https://fe-static.deepseek.com/chat/static/main.old.css",
            "https://fe-static.deepseek.com/chat/static/11165.old.js",
            "oldmain",
            "oldcss",
            "oldchunk",
            "2.7.1",
        )
        new = _snapshot(
            "https://fe-static.deepseek.com/chat/static/main.new.js",
            "https://fe-static.deepseek.com/chat/static/main.new.css",
            "https://fe-static.deepseek.com/chat/static/11165.new.js",
            "newmain",
            "newcss",
            "newchunk",
            "2.8.0",
        )
        old_main = 'appVersion:"2.7.1";let a="/api/v0/old";let b="旧文案";'
        new_main = 'appVersion:"2.8.0";let a="/api/v0/chat/status";let b="使用额度已达上限，请额度恢复后再试";'
        old_css = ".x{color:red}"
        new_css = ".x{color:red;background:var(--quota-tint-light);mask:var(--quota-image)}"
        old_i18n = "{" + ",".join(f'k{i}:"旧{i}"' for i in range(10)) + "}"
        new_i18n = "{" + ",".join(f'k{i}:"新{i}"' for i in range(10)) + ',usageLimitReached:"使用额度已达上限"}'
        mapping = {
            old["manifest"][0]["url"]: old_main.encode(),
            new["manifest"][0]["url"]: new_main.encode(),
            old["manifest"][1]["url"]: old_css.encode(),
            new["manifest"][1]["url"]: new_css.encode(),
            old["manifest"][2]["url"]: old_i18n.encode(),
            new["manifest"][2]["url"]: new_i18n.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old, new, cache_dir=None, source="chat", label="Chat 网页", old_commit="old", new_commit="new"
            )

        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["api_added"], ["/api/v0/chat/status"])
        self.assertIn("使用额度已达上限，请额度恢复后再试", evidence["cjk_added"])
        self.assertIn("--quota-tint-light", evidence["css_vars_added"])
        self.assertIn("--quota-image", evidence["css_vars_added"])
        self.assertEqual(evidence["i18n"]["added"], {"usageLimitReached": 1})
        groups = fe.build_changes_detail(evidence, 3)
        pages = [group["page"] for group in groups]
        self.assertIn("新增接口", pages)
        self.assertIn("新增用户文案", pages)
        self.assertIn("语言包", pages)

    def test_changes_detail_states_i18n_scope_when_nothing_was_checked(self) -> None:
        evidence = {
            "totals": {"manifest": 1, "added": 1, "removed": 0, "changed": 0},
            "i18n": {"checked": 0, "changed_chunks": 0, "added": {}, "removed": {}},
        }
        groups = fe.build_changes_detail(evidence, 2)
        notes = " ".join(bullet for group in groups for bullet in group["bullets"])
        self.assertIn("i18n 未核对", notes)

    def test_no_previous_is_baseline_not_change(self) -> None:
        new = _snapshot("https://fe-static.deepseek.com/chat/static/main.new.js", "", "", "m", "", "", "2.8.0")
        self.assertIsNone(fe.build_evidence(None, new, cache_dir=None, source="chat", label="Chat"))

    def test_expired_old_asset_does_not_guess_added_content(self) -> None:
        old = _snapshot(
            "https://fe-static.deepseek.com/chat/static/main.old.js",
            "https://fe-static.deepseek.com/chat/static/main.old.css",
            "https://fe-static.deepseek.com/chat/static/11165.old.js",
            "oldmain",
            "oldcss",
            "oldchunk",
            "2.7.1",
        )
        new = _snapshot(
            "https://fe-static.deepseek.com/chat/static/main.new.js",
            "https://fe-static.deepseek.com/chat/static/main.new.css",
            "https://fe-static.deepseek.com/chat/static/11165.new.js",
            "newmain",
            "newcss",
            "newchunk",
            "2.8.0",
        )
        # 旧版 CDN 文件已过期：新包里的接口 / 文案 / 样式变量不能被当成「新增」。
        mapping = {
            new["manifest"][0]["url"]: b'appVersion:"2.8.0";let a="/api/v0/chat/status";let b="x";',
            new["manifest"][1]["url"]: b".x{background:var(--quota-image)}",
            new["manifest"][2]["url"]: b"{" + b",".join(b'k%d:"new"' % i for i in range(10)) + b"}",
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            if url in mapping:
                return mapping[url]
            raise RuntimeError("expired")

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old, new, cache_dir=None, source="chat", label="Chat 网页", old_commit="old", new_commit="new"
            )

        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertTrue(evidence["baseline_missing"])
        self.assertEqual(evidence["api_added"], [])
        self.assertEqual(evidence["cjk_added"], [])
        self.assertEqual(evidence["css_vars_added"], [])
        self.assertEqual(evidence["i18n"]["added"], {})
        groups = fe.build_changes_detail(evidence, 2)
        notes = " ".join(bullet for group in groups for bullet in group["bullets"])
        self.assertIn("旧版正文已无法下载", notes)


class ReplacedChunkTests(unittest.TestCase):
    def test_changed_chunk_code_and_css_rules_are_extracted(self) -> None:
        """同编号分片内容变化时，代码新增和 CSS 规则也要进证据。"""
        old = {
            "engine": "rspack",
            "manifest": [
                _item(
                    "js:platform/static/main.js",
                    "https://fe-static.deepseek.com/platform/static/main.old.js",
                    "oldmain",
                    "entry",
                    "js",
                    "main.js",
                ),
                _item(
                    "chunk:js:378",
                    "https://fe-static.deepseek.com/platform/static/378.oldchunk.js",
                    "oldchunk",
                    "chunk",
                    "js",
                    "378",
                ),
            ],
            "main_js_url": "https://fe-static.deepseek.com/platform/static/main.old.js",
            "css_constant": "oldcss.css",
            "build_meta": {},
        }
        new = {
            "engine": "rspack",
            "manifest": [
                _item(
                    "js:platform/static/main.js",
                    "https://fe-static.deepseek.com/platform/static/main.new.js",
                    "newmain",
                    "entry",
                    "js",
                    "main.js",
                ),
                _item(
                    "chunk:js:378",
                    "https://fe-static.deepseek.com/platform/static/378.newchunk.js",
                    "newchunk",
                    "chunk",
                    "js",
                    "378",
                ),
            ],
            "main_js_url": "https://fe-static.deepseek.com/platform/static/main.new.js",
            "css_constant": "newcss.css",
            "build_meta": {},
        }
        old_chunk = 'let a=1;let b="旧文案";'
        new_chunk = 'let a=1;let b="旧文案";!o&&(0,e7.jsx)("div",{children:d.bindEmailModalDesc});oE:"e99ffd90";'
        old_css = ".old{color:red}"
        new_css = ".old{color:red}.ec486a1b.e99ffd90 .ds-modal-content__header-wrapper{margin-bottom:12px}"
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            old["manifest"][1]["url"]: old_chunk.encode(),
            new["manifest"][1]["url"]: new_chunk.encode(),
            "https://fe-static.deepseek.com/platform/static/378.oldcss.css": old_css.encode(),
            "https://fe-static.deepseek.com/platform/static/378.newcss.css": new_css.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old,
                new,
                cache_dir=None,
                source="platform",
                label="Platform 控制台",
                old_commit="old",
                new_commit="new",
            )

        assert evidence is not None
        snippets = " ".join(item["text"] for item in evidence["changed_code_snippets"])
        self.assertIn("bindEmailModalDesc", snippets)
        self.assertIn("e99ffd90", snippets)
        self.assertEqual(evidence["css_rule_changes"]["checked"], 1)
        self.assertIn(
            ".ec486a1b.e99ffd90 .ds-modal-content__header-wrapper{margin-bottom:12px}",
            evidence["css_rule_changes"]["added"],
        )
        pages = [group["page"] for group in fe.build_changes_detail(evidence, 2)]
        self.assertIn("样式调整", pages)

    def test_replaced_chunks_are_diffed_for_new_copy(self) -> None:
        """chunk 编号整体重排时，新增分片里的新文案也要进证据。"""
        old = {
            "engine": "rspack",
            "manifest": [
                _item(
                    "js:platform/static/main.js",
                    "https://fe-static.deepseek.com/platform/static/main.old.js",
                    "oldmain",
                    "entry",
                    "js",
                    "main.js",
                ),
                _item(
                    "chunk:js:6654",
                    "https://fe-static.deepseek.com/platform/static/6654.oldchunk.js",
                    "oldchunk",
                    "chunk",
                    "js",
                    "6654",
                ),
            ],
            "build_meta": {},
        }
        new = {
            "engine": "rspack",
            "manifest": [
                _item(
                    "js:platform/static/main.js",
                    "https://fe-static.deepseek.com/platform/static/main.new.js",
                    "newmain",
                    "entry",
                    "js",
                    "main.js",
                ),
                _item(
                    "chunk:js:378",
                    "https://fe-static.deepseek.com/platform/static/378.newchunk.js",
                    "newchunk",
                    "chunk",
                    "js",
                    "378",
                ),
            ],
            "build_meta": {},
        }
        old_chunk = (
            '{"captcha:"人机验证失败，请重试",'
            + ",".join(f'k{i}:"旧{i}"' for i in range(10))
            + "}"
        )
        new_chunk = (
            '{"captcha:"人机验证失败，请重试",'
            'captchaChallengePrompt:"请完成真人验证后继续",'
            + ",".join(f'k{i}:"旧{i}"' for i in range(10))
            + "}"
        )
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            old["manifest"][1]["url"]: old_chunk.encode(),
            new["manifest"][1]["url"]: new_chunk.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old,
                new,
                cache_dir=None,
                source="platform",
                label="Platform 控制台",
                old_commit="old",
                new_commit="new",
            )

        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["chunk_changes"]["paired"], 1)
        self.assertTrue(evidence["chunk_changes"]["checked"])
        self.assertIn("请完成真人验证后继续", evidence["cjk_added"])
        self.assertEqual(evidence["i18n"]["added"].get("captchaChallengePrompt"), 1)
        text = " ".join(
            bullet
            for group in fe.build_changes_detail(evidence, 3)
            for bullet in group["bullets"]
        )
        self.assertIn("请完成真人验证后继续", text)

    def test_added_only_chunks_are_marked_partial_not_falsely_diffed(self) -> None:
        """只有新增分片、没有删除分片时不能拿它单边当差分。"""
        old = {
            "engine": "rspack",
            "manifest": [
                _item(
                    "js:platform/static/main.js",
                    "https://fe-static.deepseek.com/platform/static/main.old.js",
                    "oldmain",
                    "entry",
                    "js",
                    "main.js",
                ),
            ],
            "build_meta": {},
        }
        new = {
            "engine": "rspack",
            "manifest": [
                _item(
                    "js:platform/static/main.js",
                    "https://fe-static.deepseek.com/platform/static/main.new.js",
                    "newmain",
                    "entry",
                    "js",
                    "main.js",
                ),
                _item(
                    "chunk:js:378",
                    "https://fe-static.deepseek.com/platform/static/378.newchunk.js",
                    "newchunk",
                    "chunk",
                    "js",
                    "378",
                ),
            ],
            "build_meta": {},
        }
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            new["manifest"][1]["url"]: '{"captchaChallengePrompt:"请完成真人验证后继续"}'.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old,
                new,
                cache_dir=None,
                source="platform",
                label="Platform 控制台",
                old_commit="old",
                new_commit="new",
            )

        assert evidence is not None
        chunk_changes = evidence["chunk_changes"]
        self.assertEqual(chunk_changes["added"], 1)
        self.assertEqual(chunk_changes["removed"], 0)
        self.assertTrue(chunk_changes["partial_pairing"])
        self.assertFalse(chunk_changes["checked"])
        self.assertEqual(chunk_changes["paired"], 0)
        # 单边新增分片的内容不能当成「真正新增」的证据。
        self.assertNotIn("请完成真人验证后继续", evidence["cjk_added"])
        resource = " ".join(
            bullet
            for group in fe.build_changes_detail(evidence, 2)
            for bullet in group["bullets"]
        )
        self.assertIn("未完整配对", resource)


class NextEvidenceTests(unittest.TestCase):
    def test_next_chunk_content_changes_are_pairable(self) -> None:
        """harness 用 Next.js：分片文件名带构建哈希，但 key 是稳定的逻辑名。"""
        old = {
            "engine": "html",
            "manifest": [
                _item(
                    "js:_next/static/chunks/main-app.js",
                    "https://www.deepseek.com/_next/static/chunks/main-app-1111111111111111.js",
                    "1111111111111111",
                    "entry",
                    "js",
                    "main-app.js",
                )
            ],
            "build_meta": {},
        }
        new = {
            "engine": "html",
            "manifest": [
                _item(
                    "js:_next/static/chunks/main-app.js",
                    "https://www.deepseek.com/_next/static/chunks/main-app-2222222222222222.js",
                    "2222222222222222",
                    "entry",
                    "js",
                    "main-app.js",
                )
            ],
            "build_meta": {},
        }
        added, removed, changed, unchanged = fe._changed_entries(old, new)
        self.assertEqual((added, removed, unchanged), ([], [], []))
        self.assertEqual([item["key"] for item in changed], ["js:_next/static/chunks/main-app.js"])

    def test_next_style_hashed_name_becomes_stable_key(self) -> None:
        entries = fe._manifest_entries(
            {
                "scripts": [
                    "https://www.deepseek.com/_next/static/chunks/app/layout-0e3103b0b947b1aa.js",
                    "https://www.deepseek.com/_next/static/chunks/2576-6994e819e7d6be1b.js",
                ],
                "styles": ["https://www.deepseek.com/_next/static/css/12ac947760ee9d08.css"],
            }
        )
        keys = {entry["key"] for entry in entries}
        self.assertIn("js:_next/static/chunks/app/layout.js", keys)
        self.assertIn("js:_next/static/chunks/2576.js", keys)
        self.assertIn("css:_next/static/css/12ac947760ee9d08.css", keys)


class ChatCssMapTests(unittest.TestCase):
    def test_parses_object_style_css_map_and_builds_urls(self) -> None:
        """Chat 对象式 `.k={...}` 也要能拼出 CSS 分片 URL（P2-12）。"""
        text = (
            'h.u=e=>"static/"+(({100:"named-chunk"})[e]||e)+"."+'
            '({100:"aaaa1111"})[e]+".js";'
            'h.k={100:"cccc3333"};'
        )
        maps = fe.parse_rspack_maps(text)
        self.assertEqual(maps["css"]["hashes"], {"100": "cccc3333"})
        self.assertTrue(maps["css"].get("object"))
        css_maps = {
            **maps["css"],
            "names": maps["js"]["names"],
            "prefix": maps["js"]["prefix"],
        }
        items = fe._chunk_urls(
            "https://fe-static.deepseek.com/chat/static/main.aaaa1111.js", css_maps, "css"
        )
        self.assertEqual(
            items[0]["url"],
            "https://fe-static.deepseek.com/chat/static/named-chunk.cccc3333.css",
        )

    def test_index_hashed_entry_and_multiple_mains_are_merged(self) -> None:
        """入口改名 index.<hash>.js / 多 main 时不能只展开第一个。"""
        page = {
            "scripts": [
                "https://fe-static.deepseek.com/chat/static/index.abc12345.js",
                "https://fe-static.deepseek.com/chat/static/main.def67890.js",
            ]
        }
        texts = {
            page["scripts"][0]: 'i.u=e=>"static/"+e+"."+({10:"aaaa1111"})[e]+".js";appVersion:"1";',
            page["scripts"][1]: 'm.u=e=>"static/"+e+"."+({20:"bbbb2222"})[e]+".js";appVersion:"2";',
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return texts[url].encode()

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            main_url, main_text, maps, info = fe._load_main_js(page, None)
        self.assertEqual(info["main_urls"], page["scripts"])
        self.assertEqual(set(maps["js"]["hashes"]), {"10", "20"})
        self.assertIn("appVersion", main_text)
        self.assertEqual(main_url, page["scripts"][0])


def _front_manifest(main_url: str, main_hash: str, css_entry: dict | None = None, chunks: list[dict] | None = None) -> dict:
    manifest = [_item("js:chat/static/main.js", main_url, main_hash, "entry", "js", "main.js")]
    if css_entry:
        manifest.append(css_entry)
    manifest.extend(chunks or [])
    return {
        "engine": "rspack",
        "manifest": manifest,
        "main_js_url": main_url,
        "build_meta": {},
        "health": "ok",
    }


class CssRuleEvidenceTests(unittest.TestCase):
    def _build(self, old: dict, new: dict, mapping: dict[str, bytes]) -> dict:
        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old, new, cache_dir=None, source="platform", label="Platform", old_commit="a", new_commit="b"
            )
        assert evidence is not None
        return evidence

    def test_pure_entry_css_rule_change_is_visible_signal(self) -> None:
        """纯 main.css 变化、无任何 chunk 变化，也要有 CSS 规则证据并升到 2 级。"""
        main_url = "https://fe-static.deepseek.com/platform/static/main.same.js"
        old = _front_manifest(
            main_url,
            "same",
            _item("css:platform/static/main.css", "https://fe-static.deepseek.com/platform/static/main.old.css", "oldcss", "entry", "css", "main.css"),
        )
        new = _front_manifest(
            main_url,
            "same",
            _item("css:platform/static/main.css", "https://fe-static.deepseek.com/platform/static/main.new.css", "newcss", "entry", "css", "main.css"),
        )
        mapping = {
            main_url: b'appVersion:"1";',
            old["manifest"][1]["url"]: b".btn{color:red}",
            new["manifest"][1]["url"]: b".btn{color:blue}",
        }
        evidence = self._build(old, new, mapping)
        self.assertGreaterEqual(evidence["css_rule_changes"]["checked"], 1)
        self.assertIn(".btn{color:blue}", evidence["css_rule_changes"]["added"])
        from webwatch import quality
        from webwatch.events import build_event

        self.assertTrue(quality.has_visible_signal(evidence))
        event = build_event(
            event_id="web:platform:css",
            slug="web-platform-css",
            source="platform",
            kind="page_change",
            level=2,
            title="Platform 样式更新",
            summary="样式变化",
            changed_at="2026-11-18T00:00:00Z",
            page_url="https://platform.deepseek.com/",
            payload={"frontend": evidence},
        )
        events, _ = quality.gate_events([event])
        self.assertEqual(events[0]["level"], 2)

    def test_chat_two_table_css_chunk_rule_change(self) -> None:
        """Chat 两表 CSS：chunk hash 变化要逐对比对规则，而不是等 css_constant。"""
        old_chunk = _item(
            "chunk:css:5",
            "https://fe-static.deepseek.com/chat/static/style.oldhash.css",
            "oldhash",
            "chunk",
            "css",
            "style",
        )
        new_chunk = _item(
            "chunk:css:5",
            "https://fe-static.deepseek.com/chat/static/style.newhash.css",
            "newhash",
            "chunk",
            "css",
            "style",
        )
        old = _front_manifest("https://fe-static.deepseek.com/chat/static/main.old.js", "oldmain", chunks=[old_chunk])
        new = _front_manifest("https://fe-static.deepseek.com/chat/static/main.new.js", "newmain", chunks=[new_chunk])
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            old_chunk["url"]: b".badge{color:red}",
            new_chunk["url"]: b".badge{color:blue}",
        }
        evidence = self._build(old, new, mapping)
        self.assertEqual(evidence["css_rule_changes"]["checked"], 1)
        self.assertIn(".badge{color:blue}", evidence["css_rule_changes"]["added"])

    def test_platform_css_constant_change_diffs_rules(self) -> None:
        """Platform 的 .k 常量变化时按 JS chunk 名拼旧/新 CSS URL 比对。"""
        chunk = _item(
            "chunk:js:100",
            "https://fe-static.deepseek.com/platform/static/chunkA.aaaa1111.js",
            "aaaa1111",
            "chunk",
            "js",
            "chunkA",
        )
        old = _front_manifest("https://fe-static.deepseek.com/platform/static/main.old.js", "oldmain", chunks=[chunk])
        new = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.new.js",
            "newmain",
            chunks=[{**chunk, "url": "https://fe-static.deepseek.com/platform/static/chunkA.bbbb2222.js", "hash": "bbbb2222"}],
        )
        old["css_constant"] = "oldcss"
        new["css_constant"] = "newcss"
        old["css_prefix"] = "static/"
        new["css_prefix"] = "static/"
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            chunk["url"]: b"let a=1;",
            new["manifest"][1]["url"]: b"let a=2;",
            "https://fe-static.deepseek.com/platform/static/chunkA.oldcss.css": b".login{width:300px}",
            "https://fe-static.deepseek.com/platform/static/chunkA.newcss.css": b".login{width:360px}",
        }
        evidence = self._build(old, new, mapping)
        self.assertEqual(evidence["css_rule_changes"]["checked"], 1)
        self.assertIn(".login{width:360px}", evidence["css_rule_changes"]["added"])

    def test_css_change_without_checked_rules_is_incomplete(self) -> None:
        """CSS 有变化但一条规则都没核对到时，coverage_complete 必须为 False。"""
        old = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.old.js",
            "oldmain",
            _item("css:platform/static/main.css", "https://fe-static.deepseek.com/platform/static/main.old.css", "oldcss", "entry", "css", "main.css"),
        )
        new = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.new.js",
            "newmain",
            _item("css:platform/static/main.css", "https://fe-static.deepseek.com/platform/static/main.new.css", "newcss", "entry", "css", "main.css"),
        )
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            if url in mapping:
                return mapping[url]
            raise RuntimeError("css unavailable")

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(old, new, cache_dir=None, source="platform", label="Platform")
        assert evidence is not None
        from webwatch import quality

        self.assertGreater(evidence["css_rule_changes"]["changed"], 0)
        self.assertEqual(evidence["css_rule_changes"]["checked"], 0)
        self.assertFalse(quality.coverage_complete(evidence))


class ChangedChunkCopyTests(unittest.TestCase):
    def _snapshots(self) -> tuple[dict, dict]:
        old = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.old.js",
            "oldmain",
            chunks=[_item("chunk:js:378", "https://fe-static.deepseek.com/platform/static/378.old.js", "oldc", "chunk", "js", "378")],
        )
        new = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.new.js",
            "newmain",
            chunks=[_item("chunk:js:378", "https://fe-static.deepseek.com/platform/static/378.new.js", "newc", "chunk", "js", "378")],
        )
        return old, new

    def test_changed_chunk_new_copy_enters_cjk_added(self) -> None:
        """P2-05：同编号 chunk 内新增中文要进「新增用户文案」。"""
        old, new = self._snapshots()
        old_chunk = 'let a=1;let b="旧文案";'
        new_chunk = 'let a=1;let b="旧文案";let c="请绑定邮箱后继续";'
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            old["manifest"][1]["url"]: old_chunk.encode(),
            new["manifest"][1]["url"]: new_chunk.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(old, new, cache_dir=None, source="platform", label="Platform")
        assert evidence is not None
        self.assertIn("请绑定邮箱后继续", evidence["cjk_added"])
        groups = fe.build_changes_detail(evidence, 2)
        self.assertEqual(groups[0]["page"], "新增用户文案")
        self.assertIn("请绑定邮箱后继续", " ".join(groups[0]["bullets"]))

    def test_added_only_chunk_content_is_shown_as_unpaired(self) -> None:
        """P2-06：只有新增分片时，内容作为「未配对新增内容」展示，而不是完全不可见。"""
        old = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.old.js", "oldmain"
        )
        new = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.new.js",
            "newmain",
            chunks=[_item("chunk:js:379", "https://fe-static.deepseek.com/platform/static/379.new.js", "newc", "chunk", "js", "379")],
        )
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            new["manifest"][1]["url"]: '{"hint:"请先完成验证"}'.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(old, new, cache_dir=None, source="platform", label="Platform")
        assert evidence is not None
        unpaired = evidence["chunk_changes"]["unpaired_added"]
        self.assertIn("请先完成验证", unpaired["cjk"])
        # 未配对内容不进入「确认新增」的 cjk_added。
        self.assertNotIn("请先完成验证", evidence["cjk_added"])
        groups = fe.build_changes_detail(evidence, 2)
        self.assertIn("未配对新增内容", [group["page"] for group in groups])


class ChangesDetailOrderTests(unittest.TestCase):
    def test_human_copy_and_style_come_before_technical_groups(self) -> None:
        """P1-11：人话在前，技术核对在后并打 is_asset 标记。"""
        evidence = {
            "old_commit": "abc1234",
            "new_commit": "def5678",
            "old_build": {"appVersion": "1.0"},
            "new_build": {"appVersion": "2.0"},
            "cjk_added": ["请绑定邮箱后继续"],
            "css_rule_changes": {"checked": 1, "changed": 1, "added": [".btn{color:blue}"], "removed": []},
            "i18n": {"checked": 1, "changed_chunks": 1, "added": {"k": 1}, "removed": {}},
            "chunk_changes": {"checked": True, "paired": 1},
            "totals": {"manifest": 10, "added": 1, "removed": 0, "changed": 1},
            "main_js": {"old_size": 10, "new_size": 11},
        }
        groups = fe.build_changes_detail(evidence, 2)
        pages = [group["page"] for group in groups]
        self.assertEqual(pages[0], "新增用户文案")
        self.assertLess(pages.index("样式调整"), pages.index("版本与构建"))
        self.assertLess(pages.index("新增用户文案"), pages.index("语言包"))
        by_page = {group["page"]: group for group in groups}
        self.assertTrue(by_page["版本与构建"]["is_asset"])
        self.assertTrue(by_page["语言包"]["is_asset"])
        self.assertTrue(by_page["资源核对"]["is_asset"])
        self.assertFalse(by_page["样式调整"]["is_asset"])


class RealSampleRegressionTests(unittest.TestCase):
    """用公开样本重建的最小 fixture，锁定「有变化却说不清」的回归。"""

    def _build(self, old: dict, new: dict, mapping: dict[str, bytes]) -> dict:
        def fake_fetch(url, cache_dir=None, use_cache=True):
            if url not in mapping:
                raise RuntimeError(f"missing {url}")
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(
                old, new, cache_dir=None, source="chat", label="Chat 网页",
                old_commit="old", new_commit="new",
            )
        assert evidence is not None
        return evidence

    def test_main_js_three_byte_build_meta_is_technical_only(self) -> None:
        """11-20 样本：main.js +3 字节只是构建元数据，没有可读文案。"""
        old_url = "https://fe-static.deepseek.com/chat/static/main.old.js"
        new_url = "https://fe-static.deepseek.com/chat/static/main.new.js"
        old_main = (
            'appVersion:"2.8.0";clientVersion:"1";commitId:"6f3ecd94";'
            'commit_datetime:"2026/11/09 17:39:53";let a="旧文案";'
        )
        new_main = (
            'appVersion:"2.8.10";clientVersion:"1.2";commitId:"74b5f95e";'
            'commit_datetime:"2026/11/20 18:57:59";let a="旧文案";'
        )
        old = _front_manifest(old_url, "oldhash")
        new = _front_manifest(new_url, "newhash")
        evidence = self._build(old, new, {old_url: old_main.encode(), new_url: new_main.encode()})

        self.assertEqual(evidence["totals"]["changed"], 1)
        self.assertEqual(
            evidence["main_js"]["new_size"] - evidence["main_js"]["old_size"], 3
        )
        self.assertEqual(evidence["cjk_added"], [])
        self.assertEqual(evidence["changed_code_snippets"], [])
        self.assertEqual(evidence["file_diffs"], [])
        lines = fe.technical_change_lines(evidence)
        self.assertTrue(any("main.js" in line and "+3" in line for line in lines))

        from webwatch import quality

        event = {
            "source": "chat",
            "kind": "page_change",
            "level": 2,
            "title": "未捕获到用户可见变化（可能为内部调整）",
            "payload": {"frontend": evidence},
        }
        self.assertEqual(quality.assess_event(event)["status"], "internal")
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("internal"), 1)
        self.assertTrue(event["title"].startswith("仅有技术变化"))
        self.assertIn("main.js", event["title"])
        self.assertIn("+3", event["title"])

    def test_truncated_but_readable_copy_is_ok(self) -> None:
        """11-25 样本形态：预算截断后仍有可读文案和可对照 diff，应发布为 ok。"""
        main_url = "https://fe-static.deepseek.com/platform/static/main.same.js"
        old = _front_manifest(
            main_url,
            "same",
            chunks=[
                _item("chunk:js:1", "https://fe-static.deepseek.com/platform/static/1.old.js", "old1", "chunk", "js", "1"),
                _item("chunk:js:2", "https://fe-static.deepseek.com/platform/static/2.old.js", "old2", "chunk", "js", "2"),
            ],
        )
        new = _front_manifest(
            main_url,
            "same",
            chunks=[
                _item("chunk:js:1", "https://fe-static.deepseek.com/platform/static/1.new.js", "new1", "chunk", "js", "1"),
                _item("chunk:js:2", "https://fe-static.deepseek.com/platform/static/2.new.js", "new2", "chunk", "js", "2"),
            ],
        )
        mapping = {
            main_url: b'appVersion:"1";',
            old["manifest"][1]["url"]: 'let a=1;let b="旧文案";'.encode(),
            new["manifest"][1]["url"]: 'let a=1;let b="旧文案";let c="新增用户文案";'.encode(),
            old["manifest"][2]["url"]: 'let a=2;let b="另一条旧文案";'.encode(),
            new["manifest"][2]["url"]: 'let a=2;let b="另一条旧文案";let d="第二条新增文案";'.encode(),
        }
        with mock.patch.object(fe, "MAX_CHANGED_FILES", 1):
            evidence = self._build(old, new, mapping)

        self.assertTrue(evidence["truncated"])
        self.assertEqual(evidence["totals"]["changed"], 2)
        self.assertEqual(evidence["chunk_changes"]["changed_checked"], 1)
        self.assertTrue(evidence["cjk_added"])
        self.assertTrue(evidence["file_diffs"])

        from webwatch import quality

        event = {
            "source": "platform",
            "kind": "page_change",
            "level": 2,
            "payload": {
                "frontend": evidence,
                "analysis": {
                    "headline": "新增用户文案",
                    "tldr": "界面新增用户文案",
                    "visible_changes": ["界面新增用户文案"],
                    "impact": [],
                    "action": "无需操作",
                },
            },
        }
        self.assertEqual(quality.assess_event(event)["status"], "ok")
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("ok"), 1)
        self.assertEqual(event["payload"]["quality"], "ok")

    def test_changed_files_cap_counts_pairs_not_downloads(self) -> None:
        """MAX_CHANGED_FILES 按「核对的文件对」计数，不能把新旧两次下载算成两个文件。"""
        main_url = "https://fe-static.deepseek.com/platform/static/main.same.js"
        chunks_old = [
            _item(f"chunk:js:{i}", f"https://fe-static.deepseek.com/platform/static/{i}.old.js", f"old{i}", "chunk", "js", str(i))
            for i in range(1, 4)
        ]
        chunks_new = [
            _item(f"chunk:js:{i}", f"https://fe-static.deepseek.com/platform/static/{i}.new.js", f"new{i}", "chunk", "js", str(i))
            for i in range(1, 4)
        ]
        old = _front_manifest(main_url, "same", chunks=chunks_old)
        new = _front_manifest(main_url, "same", chunks=chunks_new)
        mapping = {main_url: b'appVersion:"1";'}
        for i in range(1, 4):
            mapping[f"https://fe-static.deepseek.com/platform/static/{i}.old.js"] = f'let a={i};let b="旧文案{i}";'.encode()
            mapping[f"https://fe-static.deepseek.com/platform/static/{i}.new.js"] = f'let a={i};let b="旧文案{i}";let c="新增文案{i}";'.encode()
        with mock.patch.object(fe, "MAX_CHANGED_FILES", 4):
            evidence = self._build(old, new, mapping)

        self.assertEqual(evidence["chunk_changes"]["changed"], 3)
        self.assertEqual(evidence["chunk_changes"]["changed_checked"], 3)
        self.assertFalse(evidence["truncated"])


class EvidenceFieldOrderTests(unittest.TestCase):
    def test_control_fields_serialize_before_large_lists(self) -> None:
        """P2-08：控制字段排在 dict 前部，提示词从头截断也不会丢。"""
        old = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.old.js",
            "oldmain",
            chunks=[_item("chunk:js:1", "https://fe-static.deepseek.com/platform/static/1.old.js", "oldc", "chunk", "js", "1")],
        )
        new = _front_manifest(
            "https://fe-static.deepseek.com/platform/static/main.new.js",
            "newmain",
            chunks=[_item("chunk:js:1", "https://fe-static.deepseek.com/platform/static/1.new.js", "newc", "chunk", "js", "1")],
        )
        mapping = {
            old["manifest"][0]["url"]: b'appVersion:"1";',
            new["manifest"][0]["url"]: b'appVersion:"2";',
            old["manifest"][1]["url"]: b"let a=1;",
            new["manifest"][1]["url"]: 'let a=1;let b="新文案";'.encode(),
        }

        def fake_fetch(url, cache_dir=None, use_cache=True):
            return mapping[url]

        with mock.patch.object(fe, "_fetch_bytes", side_effect=fake_fetch):
            evidence = fe.build_evidence(old, new, cache_dir=None, source="platform", label="Platform")
        assert evidence is not None
        keys = list(evidence)
        self.assertEqual(keys[:2], ["truncated", "baseline_missing"])
        self.assertIn("totals", keys[:8])
        self.assertIn("chunk_changes", keys[:8])
        self.assertIn("css_rule_changes", keys[:8])
        head = json.dumps(evidence, ensure_ascii=False)[:3000]
        for field in ("truncated", "baseline_missing", "totals", "chunk_changes", "css_rule_changes"):
            self.assertIn(field, head)


class SnapshotHealthTests(unittest.TestCase):
    def test_main_js_failure_marks_degraded_and_produces_no_evidence(self) -> None:
        page = {
            "scripts": ["https://fe-static.deepseek.com/chat/static/main.abc12345.js"],
            "styles": [],
            "meta": {},
        }
        previous = {
            "commit_id": "c1",
            "frontend": {
                "engine": "rspack",
                "manifest": [
                    _item("js:chat/static/main.js", page["scripts"][0], "h1", "entry", "js", "main.js"),
                    _item("chunk:js:1", "https://fe-static.deepseek.com/chat/static/1.a1b2c3d4.js", "d1", "chunk", "js", "1"),
                    _item("chunk:js:2", "https://fe-static.deepseek.com/chat/static/2.a1b2c3d4.js", "d2", "chunk", "js", "2"),
                ],
                "main_js_url": page["scripts"][0],
                "main_css_url": "",
                "build_meta": {},
                "health": "ok",
            },
        }
        with mock.patch.object(fe, "_fetch_bytes", side_effect=RuntimeError("timeout")):
            snapshot, evidence = fe.build_frontend_snapshot(
                {"source": "chat", "label": "Chat"}, page, previous, None
            )
        self.assertEqual(snapshot["health"], "degraded")
        self.assertIsNone(evidence)

    def test_index_hashed_main_snapshot_is_healthy(self) -> None:
        main_url = "https://fe-static.deepseek.com/chat/static/index.abc12345.js"
        page = {"scripts": [main_url], "styles": [], "meta": {}}
        main_js = 'i.u=e=>"static/"+e+"."+({10:"aaaa1111"})[e]+".js";appVersion:"1";'
        with mock.patch.object(fe, "_fetch_bytes", return_value=main_js.encode()):
            snapshot, evidence = fe.build_frontend_snapshot(
                {"source": "chat", "label": "Chat"}, page, None, None
            )
        self.assertEqual(snapshot["health"], "ok")
        self.assertEqual(snapshot["engine"], "rspack")
        self.assertIsNone(evidence)


class I18nReferenceScopeTests(unittest.TestCase):
    """只统计语言包对象上的引用：业务对象同名属性变化不再误报。"""

    def test_business_object_property_change_is_not_reported_as_removed_copy(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        # 语言包引用 a.title 在两版里都保留，只删掉业务对象 state 的赋值。
        old = definitions + 'render(a.title);state.title="abc";state.title="def";'
        new = definitions + 'render(a.title);state.title="abc";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_real_language_pack_reference_removal_is_still_reported(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        old = definitions + 'render(a.title);render(a.title);'
        new = definitions + 'render(a.title);'
        refs = fe._removed_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["title"])
        self.assertEqual(refs[0]["values"], ["确认删除"])

    def test_bracket_notation_reference_is_counted(self) -> None:
        definitions = 'let a={warn:"请稍后再试"};'
        old = definitions + 'render(a["warn"]);render(a.warn);'
        new = definitions + 'render(a.warn);'
        refs = fe._removed_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["warn"])

    def test_business_assignments_to_short_common_key_are_ignored(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        old = definitions + 'render(a.title);cfg.title="x";cfg.title="y";'
        new = definitions + 'render(a.title);cfg.title="x";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_reference_like_text_inside_string_is_ignored(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        old = definitions + 'render(a.title);const note="a.title";'
        new = definitions + 'render(a.title);const note="";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_receiver_suffix_is_not_counted_for_pack(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        old = definitions + 'render(a.title);data.title="x";data.title="y";'
        new = definitions + 'render(a.title);data.title="x";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_property_named_like_pack_variable_is_not_counted(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        old = definitions + 'render(a.title);foo.a.title="x";foo.a.title="y";'
        new = definitions + 'render(a.title);foo.a.title="x";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_business_bracket_access_is_not_counted_for_pack(self) -> None:
        definitions = 'let a={warn:"请稍后再试"};'
        old = definitions + 'render(a.warn);data["warn"]="x";data["warn"]="y";'
        new = definitions + 'render(a.warn);data["warn"]="x";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_unbound_export_default_pack_stays_detectable(self) -> None:
        old = 'const a={title:"确认删除"};export default {warn:"请稍后再试"};' \
              'render(a.title);render(this.warn);render(this.warn);'
        new = 'const a={title:"确认删除"};export default {warn:"请稍后再试"};' \
              'render(a.title);render(this.warn);'
        refs = fe._removed_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["warn"])

    def test_brace_inside_value_does_not_break_binding(self) -> None:
        old = 'const b={url:"/api/{id}",warn:"请稍后再试"};render(b.warn);render(b.warn);'
        new = 'const b={url:"/api/{id}",warn:"请稍后再试"};render(b.warn);'
        refs = fe._removed_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["warn"])

    def test_literal_to_object_freeze_does_not_fake_removal(self) -> None:
        old = 'const a={warn:"请稍后再试"};render(a.warn);const t={ok:"完成"};'
        new = 'const b=Object.freeze({warn:"请稍后再试"});render(b.warn);const t={ok:"完成"};'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_optional_chaining_reference_removal_is_detected(self) -> None:
        old = 'const a={title:"确认删除"};render(a?.title);render(a?.title);'
        new = 'const a={title:"确认删除"};render(a?.title);'
        refs = fe._removed_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["title"])

    def test_nested_namespace_reference_removal_is_detected(self) -> None:
        old = 'let a={ns:{warn:"请稍后再试"}};render(a.ns.warn);render(a.ns.warn);'
        new = 'let a={ns:{warn:"请稍后再试"}};render(a.ns.warn);'
        refs = fe._removed_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["warn"])

    def test_business_object_with_same_key_does_not_override_pack(self) -> None:
        definitions = 'const a={title:"确认删除",warn:"请稍后再试",ok:"完成"};const cfg={title:"确认删除"};'
        old = definitions + 'render(a.title);cfg.title="x";cfg.title="y";'
        new = definitions + 'render(a.title);cfg.title="x";'
        self.assertEqual(fe._removed_i18n_refs(old, new), [])

    def test_real_added_reference_is_reported(self) -> None:
        definitions = 'let a={warn:"请稍后再试"};'
        old = definitions + 'render(a.warn);'
        new = definitions + 'render(a.warn);render(a.warn);'
        refs = fe._added_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["warn"])
        self.assertEqual((refs[0]["old_refs"], refs[0]["new_refs"]), (1, 2))

    def test_business_addition_is_not_reported_as_new_pack_reference(self) -> None:
        definitions = 'let a={title:"确认删除"};'
        old = definitions + 'render(a.title);cfg.title="x";'
        new = definitions + 'render(a.title);cfg.title="x";cfg.title="y";'
        self.assertEqual(fe._added_i18n_refs(old, new), [])

    def test_bracket_notation_addition_is_counted(self) -> None:
        definitions = 'let a={warn:"请稍后再试"};'
        old = definitions + 'render(a.warn);'
        new = definitions + 'render(a.warn);render(a["warn"]);'
        refs = fe._added_i18n_refs(old, new)
        self.assertEqual([item["key"] for item in refs], ["warn"])
        self.assertEqual((refs[0]["old_refs"], refs[0]["new_refs"]), (1, 2))

    def test_large_language_pack_reference_scan_stays_fast(self) -> None:
        """500 个 key、约 1.1MB 的代码片应远低于按 key 数乘全文扫描的旧实现。"""
        keys = [f"messageKey{i:03d}" for i in range(500)]
        pack = "const a={" + ",".join(
            f'{key}:"提示文案{i}测试"' for i, key in enumerate(keys)
        ) + "};"
        refs = "".join(f"render(a.{key});" for key in keys)
        text = pack + refs * 100
        started = time.perf_counter()
        refs_out = fe._removed_i18n_refs(text, text)
        elapsed = time.perf_counter() - started
        self.assertEqual(refs_out, [])
        self.assertLess(elapsed, 10.0)


class RemovedApiDetailTests(unittest.TestCase):
    def test_api_removed_shows_as_technical_group(self) -> None:
        groups = fe.build_changes_detail({"api_removed": ["/api/v0/old"]}, 2)
        group = next(group for group in groups if group["page"] == "删除接口")
        self.assertIn("/api/v0/old", " ".join(group["bullets"]))
        self.assertTrue(group["is_asset"])


if __name__ == "__main__":
    unittest.main()
