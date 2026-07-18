"""Tests for friction_audit.compare: the dotted-path getter (including its
list-of-dicts branch for skill_gaps.gaps) and fix_verdicts, which decides
whether a fix that shipped actually moved its metric.
"""

from __future__ import annotations

import unittest

from friction_audit import compare


class GetPathTests(unittest.TestCase):
    def setUp(self):
        self.snap = {
            "skill_gaps": {
                "gaps": [
                    {"skill": "verify", "fired_total": 5, "ask_sessions": 70},
                    {"skill": "ship-to-main", "fired_total": 0, "ask_sessions": 3},
                ]
            },
            "token_burn": {"total": {"total": 1000}},
        }

    def test_plain_dict_lookup(self):
        self.assertEqual(compare.get_path(self.snap, "token_burn.total.total"), 1000)

    def test_list_of_dicts_keyed_by_skill(self):
        self.assertEqual(
            compare.get_path(self.snap, "skill_gaps.gaps.verify.fired_total"), 5
        )
        self.assertEqual(
            compare.get_path(self.snap, "skill_gaps.gaps.ship-to-main.ask_sessions"),
            3,
        )

    def test_missing_path_returns_none(self):
        self.assertIsNone(compare.get_path(self.snap, "does.not.exist"))
        self.assertIsNone(
            compare.get_path(self.snap, "skill_gaps.gaps.no-such-skill.fired_total")
        )
        self.assertIsNone(compare.get_path(self.snap, "token_burn.total.missing"))


class FixVerdictsTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            "repeated_context": {
                "tracked_probes": {
                    "use_sonnet_agents": {"sessions": 57},
                    "windows_downloads": {"sessions": 19},
                }
            },
            "token_burn": {"main_thread_share": 0.722},
            "skill_gaps": {"gaps": [{"skill": "verify", "fired_total": 0}]},
        }

    def _verdicts_by_metric(self, new):
        # fix_verdicts refuses to judge a fix until the corpus extends far
        # enough past FIXES_APPLIED_AT to contain post-fix sessions. These
        # cases are about the verdict logic itself, so give them a window that
        # is comfortably past that gate. The gate has its own test below.
        new.setdefault("corpus", {"last_event": "2026-09-01T00:00:00Z"})
        return {row["metric"]: row for row in compare.fix_verdicts(self.old, new)}

    def test_too_early_when_corpus_barely_passes_the_fix_date(self):
        """A still number is not evidence of failure with no post-fix data."""
        new = {
            "repeated_context": {
                "tracked_probes": {
                    "use_sonnet_agents": {"sessions": 57},
                    "windows_downloads": {"sessions": 19},
                }
            },
            "token_burn": {"main_thread_share": 0.722},
            "skill_gaps": {"gaps": [{"skill": "verify", "fired_total": 0}]},
            "corpus": {"last_event": compare.FIXES_APPLIED_AT + "T12:00:00Z"},
        }
        rows = {row["metric"]: row for row in compare.fix_verdicts(self.old, new)}
        for row in rows.values():
            self.assertEqual(row["verdict"], "TOO EARLY TO TELL")
            self.assertEqual(row["postfix_days"], 0)
            self.assertNotIn("DID NOT WORK", row["verdict"])

    def test_metric_that_did_not_move_is_did_not_work(self):
        new = {
            "repeated_context": {
                "tracked_probes": {
                    "use_sonnet_agents": {"sessions": 57},
                    "windows_downloads": {"sessions": 19},  # unchanged
                }
            },
            "token_burn": {"main_thread_share": 0.722},
            "skill_gaps": {"gaps": [{"skill": "verify", "fired_total": 0}]},
        }
        by_metric = self._verdicts_by_metric(new)
        row = by_metric["repeated_context.tracked_probes.windows_downloads.sessions"]
        self.assertEqual(row["verdict"], "DID NOT WORK")

    def test_metric_moving_wanted_direction_is_worked(self):
        new = {
            "repeated_context": {
                "tracked_probes": {
                    "use_sonnet_agents": {"sessions": 40},  # down, as wanted
                    "windows_downloads": {"sessions": 19},
                }
            },
            "token_burn": {"main_thread_share": 0.722},
            "skill_gaps": {"gaps": [{"skill": "verify", "fired_total": 12}]},  # up
        }
        by_metric = self._verdicts_by_metric(new)
        self.assertEqual(
            by_metric["repeated_context.tracked_probes.use_sonnet_agents.sessions"][
                "verdict"
            ],
            "WORKED",
        )
        self.assertEqual(
            by_metric["skill_gaps.gaps.verify.fired_total"]["verdict"], "WORKED"
        )

    def test_metric_moving_wrong_direction_is_backfired(self):
        new = {
            "repeated_context": {
                "tracked_probes": {
                    "use_sonnet_agents": {"sessions": 57},
                    "windows_downloads": {"sessions": 19},
                }
            },
            "token_burn": {"main_thread_share": 0.80},  # wanted down, went up
            "skill_gaps": {"gaps": [{"skill": "verify", "fired_total": 0}]},
        }
        by_metric = self._verdicts_by_metric(new)
        self.assertEqual(
            by_metric["token_burn.main_thread_share"]["verdict"], "BACKFIRED"
        )


if __name__ == "__main__":
    unittest.main()
