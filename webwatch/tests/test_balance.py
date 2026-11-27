from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from webwatch.balance import (
    BJ,
    alert_wanted,
    daily_spend,
    load_history,
    mark_alerted,
    parse_balance,
    probe_balance,
    record_balance,
    record_balance_error,
)
from webwatch import run as run_module
from webwatch.tests.support import cleanup, temp_dir


class BalanceTests(unittest.TestCase):
    def test_parse_balance_prefers_cny(self) -> None:
        body = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "1.00"},
                {"currency": "CNY", "total_balance": "5.99"},
            ],
        }
        self.assertEqual(parse_balance(body), ("CNY", 5.99))

    def test_daily_spend_ignores_topups_and_splits_negative_deltas(self) -> None:
        points = [
            {"at": "2026-11-16T00:00:00Z", "amount": 10.0},
            {"at": "2026-11-16T00:10:00Z", "amount": 9.995},
            {"at": "2026-11-16T00:20:00Z", "amount": 9.99},
            {"at": "2026-11-16T00:30:00Z", "amount": 10.0},
            {"at": "2026-11-16T00:40:00Z", "amount": 9.98},
        ]
        self.assertEqual(daily_spend(points), {"2026-11-16": 0.03})

    def test_daily_spend_does_not_subtract_across_currencies(self) -> None:
        points = [
            {"at": "2026-11-16T00:00:00Z", "amount": 10.0, "currency": "CNY"},
            {"at": "2026-11-16T00:10:00Z", "amount": 1.0, "currency": "USD"},
        ]
        self.assertEqual(daily_spend(points), {})

    def test_balance_error_is_redacted(self) -> None:
        state: dict = {}
        record_balance_error(state, datetime.now(BJ), RuntimeError("bad key sk-abcdef1234567890"))
        self.assertNotIn("sk-abcdef1234567890", state["balance"]["last_error"])
        self.assertIn("***", state["balance"]["last_error"])

    def test_record_balance_tracks_since_watch_and_alerts_once(self) -> None:
        state_dir = temp_dir("balance-")
        self.addCleanup(cleanup, state_dir)
        state: dict = {}
        start = datetime(2026, 11, 16, 12, 0, tzinfo=BJ)

        first = record_balance(state, state_dir, start, "CNY", 10.0)
        self.assertEqual(first["today_spend_since_watch"], 0.0)
        self.assertFalse(alert_wanted(first, state))

        second = record_balance(state, state_dir, start + timedelta(minutes=10), "CNY", 9.99)
        self.assertEqual(second["today_spend_since_watch"], 0.01)
        self.assertFalse(alert_wanted(second, state))

        third = record_balance(state, state_dir, start + timedelta(minutes=20), "CNY", 9.98)
        self.assertEqual(third["today_spend_since_watch"], 0.02)
        self.assertTrue(alert_wanted(third, state))
        mark_alerted(state, third)
        self.assertFalse(alert_wanted(third, state))

        history = load_history(state_dir)
        self.assertEqual(len(history["points"]), 3)

    def test_probe_balance_uses_api_request(self) -> None:
        payload = {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "5.99"}],
        }
        with mock.patch("webwatch.balance._api_request", return_value=payload) as request:
            self.assertEqual(probe_balance("test-key"), ("CNY", 5.99))
            request.assert_called_once_with("GET", "/user/balance", "test-key")

    def test_only_balance_round_is_not_a_global_failure(self) -> None:
        state_dir = temp_dir("balance-")
        self.addCleanup(cleanup, state_dir)
        events_dir = state_dir / "events"
        future = "2099-01-01T00:00:00+08:00"
        (state_dir / "state.json").write_text(
            '{"schedule": {"pages": "%s", "catalog": "%s", "fingerprint": "%s"}}'
            % (future, future, future),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fake-key"}), mock.patch.object(
            run_module, "probe_balance", return_value=("CNY", 5.0)
        ), mock.patch.object(
            run_module, "send_text"
        ) as mail_mock, mock.patch.object(run_module, "send_events", return_value=(0, 0)):
            rc = run_module.run(
                state_dir=state_dir,
                events_dir=events_dir,
                dry_run=False,
                use_api=True,
                force=False,
            )
        self.assertEqual(rc, 0)
        self.assertTrue((state_dir / "balance-history.json").is_file())
        saved = (state_dir / "state.json").read_text(encoding="utf-8")
        self.assertIn('"balance"', saved)
        mail_mock.assert_not_called()


class BalanceZoneTests(unittest.TestCase):
    def test_beijing_zone_is_utc_plus_8(self) -> None:
        self.assertEqual(BJ.utcoffset(datetime(2026, 11, 16, 12, 0)), timedelta(hours=8))


if __name__ == "__main__":
    unittest.main()
