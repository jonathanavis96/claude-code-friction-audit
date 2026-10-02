"""Hardening tests: malformed records must not discard a whole transcript,
and the CLI must not lose or crash on its own snapshot files.

Fixtures are written to temp dirs -- never the real corpus.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from friction_audit import cli, events

GOOD_TURN = {
    "type": "assistant",
    "message": {
        "id": "msg_good",
        "model": "claude-opus-4",
        "usage": {"input_tokens": 7, "output_tokens": 3},
        "content": [],
    },
}


def _write_jsonl(path: Path, records: list) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class MalformedMessageTests(unittest.TestCase):
    def _parse(self, records: list) -> events.ParsedTranscript:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            _write_jsonl(p, records)
            return events.parse_transcript(p, set())

    def test_non_dict_message_does_not_discard_transcript(self):
        parsed = self._parse(
            [
                {"type": "assistant", "message": "truncated"},
                {"type": "user", "message": ["not", "a", "dict"]},
                GOOD_TURN,
            ]
        )
        self.assertEqual(parsed.total_usage.total, 10)
        self.assertEqual(parsed.bad_lines, 2)

    def test_non_numeric_usage_does_not_discard_transcript(self):
        bad = {
            "type": "assistant",
            "message": {
                "id": "msg_bad",
                "model": "claude-opus-4",
                "usage": {"input_tokens": "n/a", "output_tokens": {"x": 1}, "cache_read_input_tokens": 5},
            },
        }
        parsed = self._parse([bad, GOOD_TURN])
        # The garbage fields count as 0; the numeric one still counts.
        self.assertEqual(parsed.total_usage.total, 10 + 5)

    def test_infinite_usage_does_not_discard_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            line = '{"type":"assistant","message":{"id":"m_inf","usage":{"input_tokens":Infinity,"output_tokens":NaN}}}'
            p.write_text(line + "\n" + json.dumps(GOOD_TURN) + "\n", encoding="utf-8")
            parsed = events.parse_transcript(p, set())
        self.assertEqual(parsed.total_usage.total, 10)


class DailyBucketTimezoneTests(unittest.TestCase):
    def test_utc_timestamps_bucket_by_local_day(self):
        import datetime as dt

        from friction_audit import metrics
        from tests.test_metrics import _sess

        def sess(ts):
            return _sess(first_ts=ts, tokens_by_model={"claude-opus-4": 10})

        sast = dt.timezone(dt.timedelta(hours=2))
        sessions = {
            # 23:30 UTC on the 16th is 01:30 SAST on the 17th: the fix day.
            "late": sess("2026-07-16T23:30:00.000Z"),
            # 22:30 UTC on the 17th is 00:30 SAST on the 18th: post-fix.
            "after": sess("2026-07-17T22:30:00.000Z"),
        }
        db = metrics.daily_behaviour(sessions, fixes_applied_at="2026-07-17", tz=sast)
        self.assertEqual(db["prefix_days"], 0)
        self.assertEqual(db["postfix_days"], 1)
        self.assertTrue(db["fix_day_excluded_from_windows"])


class CliSnapshotFileTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> int:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return cli.main(argv)

    def test_compare_with_corrupt_snapshot_returns_error_code(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "old.json"
            new = Path(d) / "new.json"
            old.write_text('{"corpus": {', encoding="utf-8")
            new.write_text("{}", encoding="utf-8")
            self.assertEqual(self._run(["--out-dir", d, "--compare", str(old), str(new)]), 2)

    def test_corrupt_previous_snapshot_does_not_crash_audit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "projects" / "p1"
            root.mkdir(parents=True)
            _write_jsonl(root / "sess.jsonl", [GOOD_TURN])
            out = Path(d) / "out"
            out.mkdir()
            # A half-written snapshot from an interrupted earlier run.
            (out / "snapshot-2000-01-01.json").write_text('{"corpus": {"fi', encoding="utf-8")
            rc = self._run(["--root", str(Path(d) / "projects"), "--out-dir", str(out), "--no-report", "--quiet"])
            self.assertEqual(rc, 0)
            snaps = sorted(p.name for p in out.glob("snapshot-*.json"))
            self.assertEqual(len(snaps), 2)

    def test_report_with_corrupt_previous_snapshot_is_a_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "projects" / "p1"
            root.mkdir(parents=True)
            _write_jsonl(root / "sess.jsonl", [GOOD_TURN])
            out = Path(d) / "out"
            out.mkdir()
            (out / "snapshot-2000-01-01.json").write_text('{"corpus": {"fi', encoding="utf-8")
            rc = self._run(["--root", str(Path(d) / "projects"), "--out-dir", str(out), "--quiet"])
            self.assertEqual(rc, 0)
            reports = sorted(out.glob("report-*.html"))
            self.assertEqual(len(reports), 1, reports)
            html = reports[0].read_text(encoding="utf-8")
            self.assertIn("no previous snapshot, everything below is a baseline", html)
            self.assertNotIn("Deltas computed against", html)

    def test_snapshot_write_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "projects" / "p1"
            root.mkdir(parents=True)
            _write_jsonl(root / "sess.jsonl", [GOOD_TURN])
            out = Path(d) / "out"
            rc = self._run(["--root", str(Path(d) / "projects"), "--out-dir", str(out), "--no-report", "--quiet"])
            self.assertEqual(rc, 0)
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(len(names), 1, names)
            json.loads((out / names[0]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
