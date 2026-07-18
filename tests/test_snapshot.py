"""Tests for friction_audit.snapshot -- the sweep must never silently drop a
transcript's tokens.

The real corpus contains subagent transcripts whose file stem
(``agent-<id>.jsonl``) repeats under two DIFFERENT parent sessions (an agent
continued across sessions).  Keying the sessions dict on the bare stem
overwrote the first copy; combined with the global message-id dedup, the
overwritten record was the one holding the tokens, so they vanished from
every downstream metric.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from friction_audit import snapshot


def _assistant_line(mid: str, tokens: int, ts: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": mid,
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "hi"}],
            },
        }
    )


class DuplicateSubagentStemTests(unittest.TestCase):
    def _build_corpus(self, root: Path) -> None:
        proj = root / "-home-user-code"
        for parent, mid, tokens in (
            ("aaaa-1111", "msg_first", 1000),
            ("bbbb-2222", "msg_second", 500),
        ):
            sub = proj / parent / "subagents"
            sub.mkdir(parents=True)
            (proj / f"{parent}.jsonl").write_text(
                _assistant_line(f"msg_main_{parent}", 10, "2026-07-01T10:00:00Z")
                + "\n",
                encoding="utf-8",
            )
            # Same stem under two different parent sessions.
            (sub / "agent-dup.jsonl").write_text(
                _assistant_line(mid, tokens, "2026-07-01T11:00:00Z") + "\n",
                encoding="utf-8",
            )
            (sub / "agent-dup.meta.json").write_text(
                json.dumps({"agentType": "worker", "spawnDepth": 1}),
                encoding="utf-8",
            )

    def test_duplicate_stems_keep_both_records_and_all_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._build_corpus(root)
            swept = snapshot.sweep(root, progress=False)
            sessions = swept["sessions"]

            subs = {k: s for k, s in sessions.items() if s["is_subagent"]}
            self.assertEqual(len(subs), 2, "one duplicate copy was dropped")

            total_sub_tokens = sum(s["usage"].total for s in subs.values())
            self.assertEqual(
                total_sub_tokens,
                1500,
                "tokens from the overwritten duplicate vanished",
            )
            # Each copy stays attributed to its own parent session.
            parents = sorted(s["parent_session_id"] for s in subs.values())
            self.assertEqual(parents, ["aaaa-1111", "bbbb-2222"])


if __name__ == "__main__":
    unittest.main()
