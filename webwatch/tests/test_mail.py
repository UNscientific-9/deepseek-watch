"""webwatch 邮件发送的纯 CSS 观察期规则。"""
from __future__ import annotations

import unittest
from unittest import mock

from webwatch import mail


def _event(frontend: dict) -> dict:
    return {"level": 2, "payload": {"frontend": frontend}}


class CssOnlyMailTests(unittest.TestCase):
    def test_css_only_event_is_identified(self) -> None:
        self.assertTrue(mail._is_css_only(_event({
            "css_rule_changes": {"added": [".btn{color:blue}"], "removed": []},
        })))

    def test_css_with_user_copy_is_not_css_only(self) -> None:
        self.assertFalse(mail._is_css_only(_event({
            "css_rule_changes": {"added": [".btn{color:blue}"]},
            "cjk_added": ["请绑定邮箱后继续"],
        })))

    def test_send_events_skips_css_only(self) -> None:
        css_event = _event({"css_rule_changes": {"added": [".btn{color:blue}"]}})
        normal = {"level": 2, "title": "正常", "payload": {}}
        with mock.patch.object(mail, "mail_enabled", return_value=True), \
                mock.patch.object(mail, "_smtp_settings", return_value=("u", "p", "h", 465, "t")), \
                mock.patch.object(mail, "_deliver", return_value=True) as deliver:
            ok, failed = mail.send_events([css_event, normal])
        self.assertEqual((ok, failed), (1, 0))
        deliver.assert_called_once()


if __name__ == "__main__":
    unittest.main()
