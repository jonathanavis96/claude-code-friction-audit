"""Tests for friction_audit.report -- the findings have to agree with the
numbers in the snapshot they render."""

from __future__ import annotations

import unittest

from friction_audit import report


def _probe(label="tracked ask", sessions=3, covered_by="rules.md"):
    return {
        "label": label,
        "messages": sessions,
        "sessions": sessions,
        "covered_by": covered_by,
        "fix_applied": "2026-07-17: added it",
        "examples": [{"session": "s1", "text": f"example of {label}"}],
    }


def _phrase(phrase, covered, templated=False):
    return {
        "phrase": phrase,
        "terms": [phrase],
        "sessions": 4,
        "covered_by_rule": covered,
        "covering_file": "memory/topic.md" if covered else None,
        "templated": templated,
        "status": "recurring topic",
        "examples": [{"session": "s1", "text": f"example of {phrase}"}],
    }


def _snap(detail=(), phrases=()):
    return {
        "repeated_context": {
            "tracked_probes": {},
            "repeated_phrases": list(phrases),
            "failing_rules": len(detail),
            "failing_rule_detail": [{"key": f"k{i}", **d} for i, d in enumerate(detail)],
            "candidate_failing_rules": 0,
            "uncovered_repeats": 0,
            "rule_files_scanned": 2,
        }
    }


class RepeatedContextFindingTests(unittest.TestCase):
    def _render(self, snap):
        return report._finding_repeated_context(snap, None, {})

    def test_failing_rules_from_the_metric_reach_the_section(self):
        f = self._render(_snap(detail=[_probe("use cheaper agents")]))
        self.assertEqual(f.verdict_class, "bad")
        self.assertIn("1 rule", f.headline)
        self.assertNotIn("No failing rules found", f.body_html)
        self.assertIn("use cheaper agents", f.body_html.split("Failing rules:")[1])
        self.assertIn("example of use cheaper agents", f.body_html.split("Failing rules:")[1])

    def test_no_failing_rules_reads_as_good(self):
        f = self._render(_snap())
        self.assertEqual(f.verdict_class, "good")
        self.assertIn("No failing rules found", f.body_html)

    def test_recurring_phrase_is_a_candidate_never_a_failing_rule(self):
        # metrics deliberately hedges these: a topic recurring is not proof a
        # rule failed, so they must not turn the finding red on their own.
        f = self._render(_snap(phrases=[_phrase("deploy site", covered=True)]))
        self.assertEqual(f.verdict_class, "good")
        self.assertIn("deploy site", f.body_html)
        self.assertIn("memory/topic.md", f.body_html)
        self.assertIn("No failing rules found", f.body_html)

    def test_uncovered_and_templated_phrases_are_not_candidates(self):
        f = self._render(_snap(phrases=[
            _phrase("backlog topic", covered=False),
            _phrase("cron prompt", covered=True, templated=True),
        ]))
        self.assertNotIn("backlog topic", f.body_html)
        self.assertNotIn("cron prompt", f.body_html)


if __name__ == "__main__":
    unittest.main()
