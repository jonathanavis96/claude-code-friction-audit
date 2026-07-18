"""Tests for friction_audit.events: usage dedup, the automation filter,
permission-mode propagation, denial/interrupt classification, malformed-line
handling, and corpus discovery.

All fixtures are small hand-written .jsonl files under tests/fixtures/ --
never the real corpus at ~/.claude/projects.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from friction_audit import corpus, events

FIXTURES = Path(__file__).parent / "fixtures"


class UsageDedupTests(unittest.TestCase):
    """(A) The single most important behaviour in the tool: dedup on
    message.id, not on line count."""

    def test_three_blocks_same_message_id_counted_once(self):
        seen: set[str] = set()
        parsed = events.parse_transcript(FIXTURES / "dedup_a.jsonl", seen)

        # msg_1 usage (10/100/1000/5) counted exactly once, plus msg_2's
        # (20/200/2000/15) -- NOT 3x msg_1's contribution.
        self.assertEqual(parsed.total_usage.fresh, 10 + 20)
        self.assertEqual(parsed.total_usage.cache_create, 100 + 200)
        self.assertEqual(parsed.total_usage.cache_read, 1000 + 2000)
        self.assertEqual(parsed.total_usage.output, 5 + 15)
        self.assertEqual(parsed.total_usage.total, 30 + 300 + 3000 + 20)

    def test_tool_calls_not_deduped_by_message_id(self):
        """Dedup applies to usage only -- every tool_use block still counts,
        even the three that repeat msg_1's id."""
        seen: set[str] = set()
        parsed = events.parse_transcript(FIXTURES / "dedup_a.jsonl", seen)
        self.assertEqual(len(parsed.tool_calls), 4)

    def test_dedup_survives_across_files_via_shared_seen_set(self):
        """The real corpus is dozens of files; seen_message_ids must be
        threaded through every parse_transcript call for dedup to hold."""
        seen: set[str] = set()
        events.parse_transcript(FIXTURES / "dedup_a.jsonl", seen)
        second = events.parse_transcript(FIXTURES / "dedup_b.jsonl", seen)

        # msg_1 reappears in dedup_b with a bogus usage payload (999s) --
        # it must contribute exactly zero because it was already seen.
        # Only msg_3's usage (1/2/3/4) should show up.
        self.assertEqual(second.total_usage.fresh, 1)
        self.assertEqual(second.total_usage.cache_create, 2)
        self.assertEqual(second.total_usage.cache_read, 3)
        self.assertEqual(second.total_usage.output, 4)
        self.assertEqual(second.total_usage.total, 10)

        # But msg_1's tool_use block in dedup_b still counts -- tool calls
        # are never deduped, only usage is.
        self.assertEqual(len(second.tool_calls), 1)


class ClassifyUserRecordTests(unittest.TestCase):
    """(B) The automation filter. Each case from the verified corpus
    semantics, asserted individually."""

    def test_tool_use_result_key(self):
        event = {"toolUseResult": {"ok": True}, "message": {"content": "x"}}
        self.assertEqual(events.classify_user_record(event), "tool_result")

    def test_human_typed_with_origin(self):
        event = {
            "entrypoint": "cli",
            "origin": {"kind": "human"},
            "promptSource": "typed",
        }
        self.assertEqual(events.classify_user_record(event), "human")

    def test_human_queued_with_origin(self):
        event = {
            "entrypoint": "cli",
            "origin": {"kind": "human"},
            "promptSource": "queued",
        }
        self.assertEqual(events.classify_user_record(event), "human")

    def test_task_notification_is_automation(self):
        event = {
            "entrypoint": "cli",
            "origin": {"kind": "task-notification"},
            "promptSource": "system",
        }
        self.assertEqual(events.classify_user_record(event), "automation")

    def test_sdk_entrypoint_is_automation(self):
        event = {"entrypoint": "sdk-py", "promptSource": "sdk"}
        self.assertEqual(events.classify_user_record(event), "automation")

    def test_is_meta_is_automation(self):
        event = {"entrypoint": "cli", "isMeta": True}
        self.assertEqual(events.classify_user_record(event), "automation")

    def test_no_origin_typed_is_human(self):
        """Older Claude Code versions predate the `origin` field -- these
        are genuinely hand-typed and must NOT be dropped."""
        event = {"promptSource": "typed"}
        self.assertEqual(events.classify_user_record(event), "human")

    def test_no_origin_queued_is_human(self):
        event = {"promptSource": "queued"}
        self.assertEqual(events.classify_user_record(event), "human")

    def test_no_origin_no_prompt_source_slash_command_is_automation(self):
        event = {
            "message": {"content": "<command-name>/clear</command-name>"},
        }
        self.assertEqual(events.classify_user_record(event), "automation")

    def test_local_command_stdout_is_automation(self):
        event = {
            "message": {"content": "<local-command-stdout>ok</local-command-stdout>"},
        }
        self.assertEqual(events.classify_user_record(event), "automation")

    def test_mixed_fixture_filters_not_just_passes_everything(self):
        """Regression: the filter must actually filter. If it returns every
        raw user record as human, it is broken (real corpus: ~1706 human of
        ~4244 raw, i.e. automation is the majority)."""
        seen: set[str] = set()
        parsed = events.parse_transcript(FIXTURES / "mixed_realistic.jsonl", seen)

        self.assertEqual(parsed.user_class_counts.get("human"), 4)
        self.assertEqual(parsed.user_class_counts.get("automation"), 5)
        self.assertEqual(parsed.user_class_counts.get("tool_result"), 1)

        raw = parsed.user_class_counts.get("human", 0) + parsed.user_class_counts.get(
            "automation", 0
        )
        human = parsed.user_class_counts.get("human", 0)
        self.assertEqual(raw, 9)
        self.assertLess(human, raw)  # it filters something out
        self.assertGreater(human, 0)  # but not everything
        self.assertTrue(0.2 < (human / raw) < 0.6)  # plausible band, not near 1.0


class ModelFamilyTests(unittest.TestCase):
    def test_families(self):
        cases = {
            "claude-opus-4-8": "opus",
            "claude-sonnet-5": "sonnet",
            "claude-sonnet-4-6": "sonnet",
            "claude-fable-5": "fable",
            "claude-haiku-4-5-20251001": "haiku",
            "<synthetic>": None,
            None: None,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(events.model_family(model), expected)


class PromptsForPermissionTests(unittest.TestCase):
    def test_cases(self):
        cases = {
            "Bash": True,
            "mcp__foo__bar": True,
            "Read": False,
            "Glob": False,
            "": False,
        }
        for tool, expected in cases.items():
            with self.subTest(tool=tool):
                self.assertEqual(events.prompts_for_permission(tool), expected)


class PermissionModePropagationTests(unittest.TestCase):
    def test_forward_propagation_and_override(self):
        seen: set[str] = set()
        parsed = events.parse_transcript(FIXTURES / "permission_mode.jsonl", seen)

        self.assertEqual(len(parsed.tool_calls), 2)
        # A dedicated permission-mode record precedes the first tool call --
        # it is NEVER present on the assistant record itself.
        self.assertEqual(parsed.tool_calls[0].permission_mode, "bypassPermissions")
        # A permissionMode field on a later user prompt record overrides it
        # for subsequent tool calls.
        self.assertEqual(parsed.tool_calls[1].permission_mode, "default")


class DenialInterruptTests(unittest.TestCase):
    def test_denial_under_bypass_is_interrupt_under_default_is_real(self):
        seen: set[str] = set()
        parsed = events.parse_transcript(FIXTURES / "denial.jsonl", seen)

        self.assertEqual(len(parsed.denials), 2)
        first, second = parsed.denials
        self.assertEqual(first["permission_mode"], "bypassPermissions")
        self.assertTrue(first["interrupt"])
        self.assertEqual(second["permission_mode"], "default")
        self.assertFalse(second["interrupt"])


class MalformedLineTests(unittest.TestCase):
    def test_bad_lines_counted_valid_records_survive(self):
        seen: set[str] = set()
        parsed = events.parse_transcript(FIXTURES / "malformed.jsonl", seen)

        self.assertEqual(parsed.bad_lines, 1)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.human_messages, ["genuinely typed prompt"])

    def test_iter_events_does_not_raise(self):
        # Exercising iter_events directly on the same fixture must never
        # raise, blank line or otherwise.
        results = list(events.iter_events(FIXTURES / "malformed.jsonl"))
        oks = [ok for _, ok in results]
        self.assertEqual(oks.count(False), 1)
        self.assertEqual(oks.count(True), 2)


class CorpusDiscoverTests(unittest.TestCase):
    def test_nonexistent_root_returns_empty_list(self):
        self.assertEqual(corpus.discover(Path("/nonexistent/path/xyz-friction")), [])

    def test_discover_main_and_subagent_with_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "-home-user-code-someproject"
            proj.mkdir()

            main_uuid = "11111111-1111-1111-1111-111111111111"
            (proj / f"{main_uuid}.jsonl").write_text('{"type": "user"}\n')

            sub_dir = proj / main_uuid / "subagents"
            sub_dir.mkdir(parents=True)
            (sub_dir / "agent-x.jsonl").write_text('{"type": "assistant"}\n')
            (sub_dir / "agent-x.meta.json").write_text(
                json.dumps({"agentType": "Explore", "spawnDepth": 1})
            )

            # A second subagent whose meta sidecar is missing entirely, and
            # a third whose sidecar is corrupt JSON -- neither may raise.
            (sub_dir / "agent-y.jsonl").write_text('{"type": "assistant"}\n')
            (sub_dir / "agent-z.jsonl").write_text('{"type": "assistant"}\n')
            (sub_dir / "agent-z.meta.json").write_text("{not valid json")

            transcripts = corpus.discover(root)

        by_id = {t.session_id: t for t in transcripts}
        self.assertEqual(len(transcripts), 4)

        main = by_id[main_uuid]
        self.assertFalse(main.is_subagent)
        self.assertIsNone(main.parent_session_id)
        self.assertEqual(main.project, "-home-user-code-someproject")

        sub_x = by_id["agent-x"]
        self.assertTrue(sub_x.is_subagent)
        self.assertEqual(sub_x.parent_session_id, main_uuid)
        self.assertEqual(sub_x.agent_type, "Explore")
        self.assertEqual(sub_x.spawn_depth, 1)

        sub_y = by_id["agent-y"]
        self.assertTrue(sub_y.is_subagent)
        self.assertIsNone(sub_y.agent_type)  # missing sidecar -> None, no raise

        sub_z = by_id["agent-z"]
        self.assertTrue(sub_z.is_subagent)
        self.assertIsNone(sub_z.agent_type)  # corrupt sidecar -> None, no raise


if __name__ == "__main__":
    unittest.main()
