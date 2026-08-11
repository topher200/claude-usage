"""Tests for the UTC day convention and the hourly chart's Eastern hour axis.

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


class TestHourlyChartHours(unittest.TestCase):
    """Hour-of-day buckets are Eastern — the one documented exception to the UTC
    calendar, because the throttling window they highlight is fixed in Eastern and
    moves in UTC. Day buckets and date ranges stay UTC."""

    def test_viewer_dependent_tz_handling_removed(self):
        """The hour axis is a fixed zone, not the viewer's, and not a toggle."""
        for token in ('data-tz="local"', 'data-tz="utc"', "setHourlyTZ", "hourlyTZ",
                      "tzDisplayName", "utcHourToDisplay", "localOffsetHours",
                      "getTimezoneOffset"):
            with self.subTest(token=token):
                self.assertNotIn(token, HTML_TEMPLATE)

    def test_hour_zone_is_fixed_eastern(self):
        self.assertIn("const HOUR_TZ = 'America/New_York';", HTML_TEMPLATE)
        self.assertIn("const HOUR_TZ_LABEL = 'ET';", HTML_TEMPLATE)

    def test_hours_resolved_through_intl_not_a_fixed_offset(self):
        """A fixed offset would be wrong for about five months a year; Intl
        resolves each instant, so DST and its transition days are handled."""
        self.assertIn("timeZone: HOUR_TZ", HTML_TEMPLATE)
        self.assertIn("function displayHourFor(day, hour)", HTML_TEMPLATE)
        self.assertIn("formatToParts", HTML_TEMPLATE)

    def test_hours_are_labelled_et(self):
        self.assertIn("HOUR_TZ_LABEL + ' hours'", HTML_TEMPLATE)
        self.assertIn("formatHourLabel(h.hour) + ' ' + HOUR_TZ_LABEL", HTML_TEMPLATE)
        self.assertIn("'Hour of day (' + HOUR_TZ_LABEL + ')'", HTML_TEMPLATE)

    def test_peak_hours_keyed_off_the_display_hour(self):
        self.assertIn("function isPeakHour(displayHour)", HTML_TEMPLATE)
        self.assertIn("return PEAK_HOURS_ET.has(displayHour);", HTML_TEMPLATE)

    def test_hour_bucketing_does_not_leak_into_the_range_filter(self):
        """Only the hour axis is Eastern. getRangeBounds stays on UTC."""
        start = HTML_TEMPLATE.index("function getRangeBounds(range)")
        end = HTML_TEMPLATE.index("function readURLRange()")
        body = HTML_TEMPLATE[start:end]
        self.assertNotIn("HOUR_TZ", body)
        self.assertNotIn("America/New_York", body)


class TestPeakWindowMatchesPacificDefinition(unittest.TestCase):
    """`PEAK_HOURS_ET` is a derived value: Anthropic defines the throttling window
    as Mon-Fri 05:00-11:00 Pacific. These assert the constant still equals that
    window converted to Eastern, in both daylight-saving regimes, rather than
    restating the numbers — and that the same window in UTC is not constant, which
    is the reason the hour axis is not UTC."""

    PACIFIC_START, PACIFIC_END = 5, 11  # 05:00 through 10:59 PT

    def setUp(self):
        try:
            from zoneinfo import ZoneInfo
        except ImportError:  # pragma: no cover - Python < 3.9
            self.skipTest("zoneinfo requires Python 3.9+")
        try:
            self.et = ZoneInfo("America/New_York")
            self.pt = ZoneInfo("America/Los_Angeles")
        except Exception:  # pragma: no cover - system without a tz database
            self.skipTest("system tz database unavailable")

    def _pacific_window_in(self, zone, day):
        return {
            datetime.fromisoformat(f"{day}T{h:02d}:00:00")
            .replace(tzinfo=self.pt).astimezone(zone).hour
            for h in range(self.PACIFIC_START, self.PACIFIC_END)
        }

    def _declared_peak_hours(self):
        marker = "const PEAK_HOURS_ET = new Set(["
        start = HTML_TEMPLATE.index(marker) + len(marker)
        return {int(x) for x in HTML_TEMPLATE[start:HTML_TEMPLATE.index("]", start)].split(",")}

    def test_declared_hours_match_the_pacific_window_year_round(self):
        declared = self._declared_peak_hours()
        for day in ("2026-01-15", "2026-04-15", "2026-07-15", "2026-11-15"):
            with self.subTest(day=day):
                self.assertEqual(declared, self._pacific_window_in(self.et, day))

    def test_the_same_window_is_not_constant_in_utc(self):
        self.assertNotEqual(self._pacific_window_in(timezone.utc, "2026-01-15"),
                            self._pacific_window_in(timezone.utc, "2026-07-15"))


if __name__ == "__main__":
    unittest.main()
