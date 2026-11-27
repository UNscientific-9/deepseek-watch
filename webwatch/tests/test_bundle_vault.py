"""版本仓库与主脚本代码差异的离线测试。"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from webwatch import bundle_mirror, bundle_vault, frontend_evidence
from webwatch.tests.support import cleanup, temp_dir


class BundleVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir("bundle-vault-")
        self.addCleanup(cleanup, self.tmp)
        self.state_dir = Path(self.tmp)
        self.root = self.state_dir / "bundle-vault"

    def _make_snapshot(self, commit: str = "abc12345") -> str:
        url = "https://fe-static.deepseek.com/chat/static/main.abc12345.js"
        body = b'let a=1;commitId:"abc12345";'
        blob, _ = bundle_mirror.write_blob(self.root, body, ".js")
        snapshot = {
            "source": "chat",
            "build_meta": {"commitId": commit},
            "main_js_url": url,
            "page": {"html_sha256": "pagehash"},
            "files": [
                {
                    "url": url,
                    "blob": blob,
                    "hash": hashlib.sha256(body).hexdigest(),
                    "status": "downloaded",
                    "kind": "js",
                    "role": "entry",
                }
            ],
        }
        directory = self.root / "snapshots" / "chat" / "20260101T000000Z-abc12345"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(snapshot), encoding="utf-8")
        return url

    def test_reads_archived_blob_and_compares_version(self) -> None:
        url = self._make_snapshot()
        vault = bundle_vault.BundleVault(self.root, "chat")
        self.assertEqual(vault.read(url), b'let a=1;commitId:"abc12345";')
        current = {
            "html_sha256": "pagehash",
            "frontend": {
                "build_meta": {"commitId": "abc12345"},
                "main_js_url": url,
            },
        }
        self.assertFalse(bundle_vault.needs_snapshot(self.root, "chat", current))
        current["frontend"]["build_meta"]["commitId"] = "def67890"
        self.assertTrue(bundle_vault.needs_snapshot(self.root, "chat", current))

    def test_same_commit_but_changed_main_or_page_needs_snapshot(self) -> None:
        url = self._make_snapshot()
        current = {
            "html_sha256": "pagehash",
            "frontend": {"build_meta": {"commitId": "abc12345"}, "main_js_url": url},
        }
        # 同 commit 但 main.js 换了，热修也必须留存。
        current["frontend"]["main_js_url"] = "https://fe-static.deepseek.com/chat/static/main.def.js"
        self.assertTrue(bundle_vault.needs_snapshot(self.root, "chat", current))
        # main.js 相同但页面 HTML 变了，也要留存。
        current["frontend"]["main_js_url"] = url
        current["html_sha256"] = "pagehash2"
        self.assertTrue(bundle_vault.needs_snapshot(self.root, "chat", current))

    def test_unknown_url_returns_empty(self) -> None:
        self._make_snapshot()
        vault = bundle_vault.BundleVault(self.root, "chat")
        self.assertEqual(vault.read("https://fe-static.deepseek.com/chat/static/missing.js"), b"")

    def test_prune_keeps_page_html_blobs(self) -> None:
        """页面 HTML/文本 blob 不在 files 列表里，但同样不能被 prune 删掉。"""
        html_blob, _ = bundle_mirror.write_blob(self.root, b"<html>page</html>", ".html")
        text_blob, _ = bundle_mirror.write_blob(self.root, b"page text", ".txt")
        directory = self.root / "snapshots" / "chat" / "20260101T000000Z-page"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "source": "chat",
                    "complete": True,
                    "page": {
                        "html_sha256": "pagehash",
                        "page_html_blob": html_blob,
                        "page_text_blob": text_blob,
                    },
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        bundle_vault.prune(self.root, "chat")
        self.assertTrue((self.root / html_blob).is_file())
        self.assertTrue((self.root / text_blob).is_file())

    def test_incomplete_snapshot_does_not_count_as_saved(self) -> None:
        """半成品快照不能凭页面哈希匹配就阻止下一轮重试。"""
        directory = self.root / "snapshots" / "chat" / "20260101T000000Z-broken"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "source": "chat",
                    "complete": False,
                    "incomplete_reason": "主脚本 main.js 未取到",
                    "page": {"html_sha256": "pagehash"},
                    "build_meta": {},
                    "main_js_url": "",
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        current = {
            "html_sha256": "pagehash",
            "frontend": {
                "build_meta": {"commitId": "abc12345"},
                "main_js_url": "https://fe-static.deepseek.com/chat/static/main.abc12345.js",
            },
        }
        vault = bundle_vault.BundleVault(self.root, "chat")
        self.assertIsNone(vault.latest_manifest())
        self.assertTrue(bundle_vault.needs_snapshot(self.root, "chat", current))

    def test_ensure_snapshot_skips_same_version_and_runs_on_change(self) -> None:
        url = self._make_snapshot()
        current = {
            "html_sha256": "pagehash",
            "frontend": {"build_meta": {"commitId": "abc12345"}, "main_js_url": url},
        }
        with mock.patch.object(
            bundle_mirror, "create_snapshot", return_value={"summary": {"ok": True}}
        ) as create:
            self.assertIsNone(
                bundle_vault.ensure_snapshot({"source": "chat"}, self.state_dir, current=current)
            )
            self.assertFalse(create.called)
            current["frontend"]["build_meta"]["commitId"] = "def67890"
            self.assertEqual(
                bundle_vault.ensure_snapshot({"source": "chat"}, self.state_dir, current=current),
                {"ok": True},
            )
            self.assertTrue(create.called)


class MainCodeSnippetTests(unittest.TestCase):
    def test_ignores_build_metadata_only_changes(self) -> None:
        from webwatch.frontend_evidence import main_code_snippets

        old = 'appVersion:"2.7.1";commitId:"aaaaaaaa";commit_datetime:"2026/11/09 17:39:53";let cat="猫";'
        new = 'appVersion:"2.8.0";commitId:"bbbbbbbb";commit_datetime:"2026/11/20 18:57:59";let cat="猫";'
        self.assertEqual(main_code_snippets(old, new), [])

    def test_keeps_real_code_change_snippet(self) -> None:
        from webwatch.frontend_evidence import main_code_snippets

        old = 'commitId:"aaaaaaaa";let a=1;let cat="猫";'
        new = 'commitId:"bbbbbbbb";let a=1;let cat="猫";let wechatLogin=true;'
        snippets = main_code_snippets(old, new)
        self.assertTrue(snippets)
        self.assertIn("wechatLogin", " ".join(snippets))


class FullAutoEvidenceTests(unittest.TestCase):
    """没有 CDN 旧文件时，证据仍应来自版本仓库并包含主脚本代码差异。"""

    def test_build_evidence_reads_archived_old_main_and_keeps_code_snippet(self) -> None:
        tmp = temp_dir("bundle-evidence-")
        self.addCleanup(cleanup, tmp)
        root = Path(tmp) / "bundle-vault"
        old_url = "https://fe-static.deepseek.com/chat/static/main.old.js"
        new_url = "https://fe-static.deepseek.com/chat/static/main.new.js"
        old_text = 'commitId:"aaaaaaaa";let a=1;let cat="猫";'
        new_text = 'commitId:"bbbbbbbb";let a=1;let cat="猫";let wechatLogin=true;'
        old_blob, _ = bundle_mirror.write_blob(root, old_text.encode(), ".js")
        new_blob, _ = bundle_mirror.write_blob(root, new_text.encode(), ".js")
        snapshot_dir = root / "snapshots" / "chat" / "20260101T000000Z"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "source": "chat",
                    "files": [
                        {
                            "url": old_url,
                            "blob": old_blob,
                            "hash": hashlib.sha256(old_text.encode()).hexdigest(),
                            "status": "downloaded",
                            "kind": "js",
                            "role": "entry",
                            "name": "main.js",
                        },
                        {
                            "url": new_url,
                            "blob": new_blob,
                            "hash": hashlib.sha256(new_text.encode()).hexdigest(),
                            "status": "downloaded",
                            "kind": "js",
                            "role": "entry",
                            "name": "main.js",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        def snap(url: str, digest: str) -> dict:
            return {
                "manifest": [
                    {
                        "key": "js:chat/static/main.js",
                        "url": url,
                        "hash": digest,
                        "kind": "js",
                        "role": "entry",
                        "name": "main.js",
                    }
                ],
                "main_js_url": url,
                "main_css_url": "",
                "build_meta": {},
            }

        old = snap(old_url, "h1")
        new = snap(new_url, "h2")
        evidence = frontend_evidence.build_evidence(
            old,
            new,
            cache_dir=None,
            source="chat",
            label="Chat 网页",
            vault=bundle_vault.BundleVault(root, "chat"),
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertFalse(evidence["baseline_missing"])
        self.assertTrue(evidence["changed_code_snippets"])
        self.assertIn("wechatLogin", " ".join(item["text"] for item in evidence["changed_code_snippets"]))


if __name__ == "__main__":
    unittest.main()
