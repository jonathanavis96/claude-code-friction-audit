"""Tests for friction_audit.corpus -- what discovery misses, every metric misses."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from friction_audit import corpus, snapshot

TURN = {
    "type": "assistant",
    "timestamp": "2026-07-01T10:00:00Z",
    "message": {
        "id": "msg_1",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
}


def _write(path: Path, record: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record or {"type": "system"}) + "\n", encoding="utf-8")


class WorkflowSubagentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "p1"
        self.session = self.project / "sess-1"
        _write(self.project / "sess-1.jsonl")
        _write(self.session / "subagents" / "agent-plain.jsonl")
        self.workflow = self.session / "subagents" / "workflows" / "wf_abc"

    def _by_id(self):
        return {t.session_id: t for t in corpus.discover(self.root)}

    def test_finds_workflow_subagent_transcripts(self):
        _write(self.workflow / "agent-wf.jsonl")
        found = self._by_id()
        self.assertIn("agent-wf", found)
        self.assertTrue(found["agent-wf"].is_subagent)

    def test_workflow_subagent_keeps_its_real_parent_session(self):
        _write(self.workflow / "agent-wf.jsonl")
        self.assertEqual(self._by_id()["agent-wf"].parent_session_id, "sess-1")

    def test_workflow_subagent_reads_its_meta_sidecar(self):
        _write(self.workflow / "agent-wf.jsonl")
        (self.workflow / "agent-wf.meta.json").write_text(
            json.dumps({"agentType": "workflow-subagent", "spawnDepth": 1}), encoding="utf-8"
        )
        t = self._by_id()["agent-wf"]
        self.assertEqual(t.agent_type, "workflow-subagent")
        self.assertEqual(t.spawn_depth, 1)

    def test_workflow_journal_is_not_a_transcript(self):
        _write(self.workflow / "agent-wf.jsonl")
        _write(self.workflow / "journal.jsonl")
        self.assertNotIn("journal", self._by_id())

    def test_workflow_tokens_count_as_delegated(self):
        _write(self.workflow / "agent-wf.jsonl", TURN)
        snap = snapshot.build(self.root, progress=False)
        self.assertEqual(snap["corpus"]["subagent_sessions"], 2)
        self.assertEqual(snap["token_burn"]["delegated"]["total"], 15)


class BadMetaSidecarTests(unittest.TestCase):
    """A corrupt sidecar must never kill the run, even when it parses as JSON."""

    def _sweep_with_meta(self, meta: dict):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "p1" / "sess-1" / "subagents"
            _write(sub / "agent-x.jsonl")
            (sub / "agent-x.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            return snapshot.build(d, progress=False), corpus.discover(d)[0]

    def test_non_numeric_spawn_depth_is_zero(self):
        _, t = self._sweep_with_meta({"spawnDepth": "deep"})
        self.assertEqual(t.spawn_depth, 0)

    def test_non_string_agent_type_is_unknown(self):
        snap, t = self._sweep_with_meta({"agentType": ["worker"]})
        self.assertIsNone(t.agent_type)
        self.assertIn("<unknown>", snap["model_mix"]["agent_type_leak"])


if __name__ == "__main__":
    unittest.main()
