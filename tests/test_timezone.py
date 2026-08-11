"""Tests for the UTC day convention.

Turns are bucketed by the UTC portion of their timestamp (`substr(timestamp, 1, 10)`),
and claude.ai bills on a day boundary at or within a couple of hours of UTC, so every
date the CLI and dashboard compare against that data has to be a UTC date. A local
date silently selects the wrong day for part of every day, which is invisible when
the tests run in UTC — CI does. These tests force non-UTC zones so it isn't.
"""

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cli
from dashboard import HTML_TEMPLATE
from scanner import get_db, init_db, insert_turns

# UTC-12 and UTC+14 are the extremes of the real tz range. For any instant at
# least one of them sits on a different calendar date than UTC, which makes
# "a local date is not a UTC date" assertable without mocking the clock.
TZ_WEST = "Etc/GMT+12"
TZ_EAST = "Pacific/Kiritimati"

requires_tzset = unittest.skipUnless(
    hasattr(time, "tzset"), "time.tzset is Unix-only; TZ cannot be forced on Windows"
)


class _ForcedTZ:
    """Run a block with TZ set to `name`, restoring the original afterwards."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._orig = os.environ.get("TZ")
        os.environ["TZ"] = self.name
        time.tzset()

    def __exit__(self, *exc):
        if self._orig is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig
        time.tzset()


def _turn(message_id, ts):
    return {
        "session_id": "sess-tz", "timestamp": ts, "model": "claude-opus-4-8",
        "input_tokens": 1000, "output_tokens": 500,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": "/home/user/proj",
        "message_id": message_id, "is_subagent": 0, "agent_id": None,
    }


@requires_tzset
class TestUtcToday(unittest.TestCase):
    def test_matches_utc_calendar_in_both_extreme_zones(self):
        for tz in (TZ_WEST, TZ_EAST):
            with self.subTest(tz=tz), _ForcedTZ(tz):
                self.assertEqual(cli.utc_today(), datetime.now(timezone.utc).date())

    def test_differs_from_the_local_calendar_in_at_least_one_zone(self):
        """Guards against a `date.today()` regression actually being observable."""
        from datetime import date

        differs = []
        for tz in (TZ_WEST, TZ_EAST):
            with _ForcedTZ(tz):
                differs.append(date.today() != cli.utc_today())
        self.assertTrue(any(differs),
                        "expected UTC-12 or UTC+14 to disagree with the UTC date")


@requires_tzset
class TestCmdTodaySelectsUtcDay(unittest.TestCase):
    """`today` must report the UTC day's turns, not the local day's."""

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        utc_now = datetime.now(timezone.utc)
        self.utc_day = utc_now.date().isoformat()
        # Noon UTC today, and noon UTC on the neighbouring days. Whatever the
        # local zone, only the middle one belongs to the UTC "today".
        conn = get_db(self.db_path)
        init_db(conn)
        insert_turns(conn, [
            _turn("m-prev", (utc_now - timedelta(days=1)).date().isoformat() + "T12:00:00Z"),
            _turn("m-today", self.utc_day + "T12:00:00Z"),
            _turn("m-next", (utc_now + timedelta(days=1)).date().isoformat() + "T12:00:00Z"),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def test_reports_the_utc_day_regardless_of_local_zone(self):
        for tz in (TZ_WEST, TZ_EAST):
            with self.subTest(tz=tz), _ForcedTZ(tz):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cli.cmd_today()
                out = buf.getvalue()
                self.assertIn(self.utc_day, out)
                # Exactly the one turn on the UTC day, not its neighbours.
                self.assertIn("turns=1", out)
                self.assertNotIn("No usage recorded today.", out)


class TestDashboardDateHelpersAreUtc(unittest.TestCase):
    """The client formats and walks dates in UTC."""

    def test_utc_iso_date_helper_present(self):
        self.assertIn("function utcISODate(d)", HTML_TEMPLATE)
        self.assertIn("d.getUTCFullYear()", HTML_TEMPLATE)

    def test_local_iso_date_helper_absent(self):
        """`localISODate` put the client on a different calendar than the data."""
        self.assertNotIn("localISODate", HTML_TEMPLATE)

    def test_range_bounds_uses_utc_primitives(self):
        self.assertIn("today.getUTCDay()", HTML_TEMPLATE)
        self.assertIn("mon.setUTCDate(", HTML_TEMPLATE)
        self.assertIn("Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1)", HTML_TEMPLATE)
        self.assertIn("d.setUTCDate(d.getUTCDate() - days)", HTML_TEMPLATE)

    def test_range_bounds_has_no_local_date_primitives(self):
        """The real regression catcher: no local getters inside getRangeBounds."""
        start = HTML_TEMPLATE.index("function getRangeBounds(range)")
        end = HTML_TEMPLATE.index("function readURLRange()")
        body = HTML_TEMPLATE[start:end]
        for local_call in ("getDay()", "setDate(", "getFullYear()", "getMonth()", "getDate()"):
            with self.subTest(call=local_call):
                self.assertNotIn(local_call, body)

    def test_range_selector_is_labelled_utc(self):
        """A UTC calendar disagrees with the viewer's near midnight; say so."""
        self.assertIn('class="tz-note"', HTML_TEMPLATE)


class TestHourlyChartIsUtcOnly(unittest.TestCase):
    """The hourly distribution has one timezone, matching every other chart."""

    def test_tz_toggle_removed(self):
        for token in ('data-tz="local"', 'data-tz="utc"', "setHourlyTZ", "hourlyTZ",
                      "tzDisplayName", "utcHourToDisplay", "localOffsetHours"):
            with self.subTest(token=token):
                self.assertNotIn(token, HTML_TEMPLATE)

    def test_hours_are_labelled_utc(self):
        self.assertIn("' averaged · UTC'", HTML_TEMPLATE)
        self.assertIn("formatHourLabel(h.hour) + ' UTC'", HTML_TEMPLATE)

    def test_peak_hours_keyed_directly_off_utc_hour(self):
        self.assertIn("function isPeakHour(utcHour)", HTML_TEMPLATE)
        self.assertIn("return PEAK_HOURS_UTC.has(utcHour);", HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
