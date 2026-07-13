"""Tests for spend_api: credential parsing/saving and refresh_spend outcomes."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner
import spend_api


class TestParseCredentialsInput(unittest.TestCase):
    def test_raw_key(self):
        r = spend_api.parse_credentials_input("sk-ant-sid02-ABC123")
        self.assertEqual(r["session_key"], "sk-ant-sid02-ABC123")
        self.assertIsNone(r["org_id"])

    def test_cookie_string(self):
        r = spend_api.parse_credentials_input("foo=1; sessionKey=sk-ant-sid02-XYZ; bar=2")
        self.assertEqual(r["session_key"], "sk-ant-sid02-XYZ")

    def test_full_curl_blob_extracts_key_and_org(self):
        blob = (
            "curl 'https://claude.ai/api/organizations/"
            "abcdef01-2345-6789-abcd-ef0123456789/usage/spend' "
            "-b 'sessionKey=sk-ant-sid02-QQQ; cf_clearance=zzz'"
        )
        r = spend_api.parse_credentials_input(blob)
        self.assertEqual(r["session_key"], "sk-ant-sid02-QQQ")
        self.assertEqual(r["org_id"], "abcdef01-2345-6789-abcd-ef0123456789")

    def test_no_key(self):
        self.assertIsNone(spend_api.parse_credentials_input("nothing here")["session_key"])


class TestSaveCredentials(unittest.TestCase):
    def test_round_trip_and_org_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            with mock.patch.object(spend_api, "CREDENTIALS_PATH", path):
                spend_api.save_credentials("sk-ant-sid02-AAA", org_id="org-1")
                data = json.loads(path.read_text())
                self.assertEqual(data["cookie"], "sessionKey=sk-ant-sid02-AAA")
                self.assertEqual(data["org_id"], "org-1")
                # A later save without an org keeps the stored one.
                spend_api.save_credentials("sk-ant-sid02-BBB")
                data = json.loads(path.read_text())
                self.assertEqual(data["cookie"], "sessionKey=sk-ant-sid02-BBB")
                self.assertEqual(data["org_id"], "org-1")


FAKE_SERIES = [{
    "bucket": "2026-07-01", "group_key": "claude_opus_4_8", "cost_minor_units": 500,
    "tokens": {"input": 1, "output": 2, "cache_read": 3, "cache_write_5m": 4, "cache_write_1h": 5},
    "request_count": 6,
}]


class TestRefreshSpend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Path(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _status(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='api_spend_last_status'").fetchone()
        conn.close()
        return row["value"] if row else None

    def test_no_credentials(self):
        with mock.patch.object(spend_api, "has_credentials", return_value=False):
            res = spend_api.refresh_spend(db_path=self.db)
        self.assertEqual(res["status"], "no_credentials")

    def test_ok_stores_rows_and_status(self):
        with mock.patch.object(spend_api, "has_credentials", return_value=True), \
             mock.patch.object(spend_api, "fetch_spend", return_value={"series": FAKE_SERIES}):
            res = spend_api.refresh_spend(db_path=self.db, start="2026-07-01", end="2026-07-01")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["total"], 5.0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM api_spend").fetchone()[0], 1)
        conn.close()
        self.assertEqual(self._status(), "ok")

    def test_auth_failed_recorded(self):
        with mock.patch.object(spend_api, "has_credentials", return_value=True), \
             mock.patch.object(spend_api, "fetch_spend", side_effect=spend_api.AuthError("expired")):
            res = spend_api.refresh_spend(db_path=self.db)
        self.assertEqual(res["status"], "auth_failed")
        self.assertEqual(self._status(), "auth_failed")

    def test_network_error_recorded(self):
        with mock.patch.object(spend_api, "has_credentials", return_value=True), \
             mock.patch.object(spend_api, "fetch_spend", side_effect=spend_api.SpendApiError("boom")):
            res = spend_api.refresh_spend(db_path=self.db)
        self.assertEqual(res["status"], "network_error")
        self.assertEqual(self._status(), "network_error")


if __name__ == "__main__":
    unittest.main()
