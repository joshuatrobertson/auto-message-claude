import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import autoresume

CHI = ZoneInfo("America/Chicago")
# A fixed "now": Sunday 2026-07-05 13:00 Chicago time.
NOW = datetime(2026, 7, 5, 13, 0, tzinfo=CHI)


class TestParseResetAt(unittest.TestCase):
    def test_epoch_after_pipe(self):
        # Historical shape: "Claude AI usage limit reached|1751745600"
        got = autoresume.parse_reset_at(
            "Claude AI usage limit reached|1751745600", NOW)
        self.assertEqual(got, datetime.fromtimestamp(1751745600, tz=CHI))

    def test_bare_epoch(self):
        got = autoresume.parse_reset_at("limit reached 1751745600", NOW)
        self.assertEqual(int(got.timestamp()), 1751745600)

    def test_iso_timestamp(self):
        got = autoresume.parse_reset_at(
            "your limit resets at 2026-07-05T20:00:00Z", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc))

    def test_prose_time_with_tz_future_today(self):
        got = autoresume.parse_reset_at(
            "Your limit will reset at 6pm (America/Chicago).", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 18, 0, tzinfo=CHI))

    def test_prose_time_rolls_to_tomorrow(self):
        # 3am has already passed at NOW (13:00), so it means tomorrow 3am.
        got = autoresume.parse_reset_at(
            "Your limit will reset at 3am (America/Chicago).", NOW)
        self.assertEqual(got, datetime(2026, 7, 6, 3, 0, tzinfo=CHI))

    def test_prose_time_with_minutes_no_tz_uses_now_tz(self):
        got = autoresume.parse_reset_at("resets at 2:30pm", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 14, 30, tzinfo=CHI))

    def test_weekday_prose(self):
        # NOW is Sunday; "Thursday at 9am" = 2026-07-09 09:00.
        got = autoresume.parse_reset_at(
            "Weekly limit reached. Resets Thursday at 9am.", NOW)
        self.assertEqual(got, datetime(2026, 7, 9, 9, 0, tzinfo=CHI))

    def test_weekday_same_day_rolls_a_week(self):
        # "Sunday at 9am" when it's already Sunday 13:00 → next Sunday.
        got = autoresume.parse_reset_at("resets Sunday at 9am", NOW)
        self.assertEqual(got, datetime(2026, 7, 12, 9, 0, tzinfo=CHI))

    def test_unknown_tz_name_falls_back_to_now_tz(self):
        got = autoresume.parse_reset_at(
            "reset at 6pm (Made/Up_Zone)", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 18, 0, tzinfo=CHI))

    def test_garbage_returns_none(self):
        self.assertIsNone(autoresume.parse_reset_at("rate_limit", NOW))

    def test_small_numbers_are_not_epochs(self):
        # "429" and retry counts must not parse as timestamps.
        self.assertIsNone(autoresume.parse_reset_at(
            "429 rate_limit attempt 3 of 10 retry 5000ms", NOW))

    def test_result_is_tz_aware(self):
        got = autoresume.parse_reset_at("resets at 6pm", NOW)
        self.assertIsNotNone(got.tzinfo)

    def test_malformed_iso_falls_through_to_none(self):
        # Matches the ISO regex shape but is not a real date: must not raise.
        self.assertIsNone(autoresume.parse_reset_at(
            "resets at 2026-99-99T25:99:00Z", NOW))

    def test_weekday_same_day_future_time_stays_today(self):
        # "Sunday at 2pm" when it's Sunday 13:00 → today at 14:00.
        got = autoresume.parse_reset_at("resets Sunday at 2pm", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 14, 0, tzinfo=CHI))

    def test_real_world_session_limit_string(self):
        # Captured verbatim from a live limit event, 2026-07-05. NOW is
        # 13:00 Chicago = 19:00 London, so 6:10pm London has passed and
        # rolls to the next day.
        got = autoresume.parse_reset_at(
            "You've hit your session limit · resets 6:10pm (Europe/London)",
            NOW)
        self.assertEqual(got, datetime(2026, 7, 6, 18, 10,
                                       tzinfo=ZoneInfo("Europe/London")))
