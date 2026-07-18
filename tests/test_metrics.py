"""Tests for friction_audit.metrics -- currently the grep-guard bypass
classifier, which is the metric most prone to silently mislabeling a
legitimate non-code search as "genuine circumvention".
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from friction_audit import metrics
from friction_audit.events import ToolCall, Usage


def _tc(name="Bash", command=None, file_path=None, timestamp="2026-07-18T10:00:00"):
    inp: dict = {}
    if command is not None:
        inp["command"] = command
    if file_path is not None:
        inp["file_path"] = file_path
    return ToolCall(
        name=name, input=inp, permission_mode=None, tool_use_id="t", model=None,
        timestamp=timestamp,
    )


def _sess(
    *,
    is_subagent=False,
    agent_type=None,
    parent_session_id=None,
    first_ts="2026-07-18T10:00:00",
    tokens_by_model=None,
    tool_calls=(),
    read_paths=(),
    blocks=0,
):
    """Build a session record shaped like snapshot._session_record output."""
    by_model = {}
    total = Usage()
    for model, tok in (tokens_by_model or {}).items():
        u = Usage(fresh=tok)
        by_model[model] = u
        total.add(u)
    return {
        "is_subagent": is_subagent,
        "agent_type": agent_type,
        "parent_session_id": parent_session_id,
        "first_ts": first_ts,
        "last_ts": first_ts,
        "usage": total,
        "usage_by_model": by_model,
        "tool_calls": list(tool_calls),
        "read_paths": list(read_paths),
        "blocks": blocks,
    }


class ClassifyBypassTests(unittest.TestCase):
    def test_no_search_command_at_all(self):
        self.assertEqual(
            metrics._classify_bypass("ls -la /tmp", set()),
            "no-search-command-at-all",
        )

    def test_recursive_grep_over_code_repo_is_genuine_circumvention(self):
        self.assertEqual(
            metrics._classify_bypass(
                "grep -r 'TODO' /home/user/code/myrepo", set()
            ),
            "genuine-circumvention",
        )

    def test_grep_over_obsidian_vault_is_guard_false_positive(self):
        # _NON_CODE_RX is case-sensitive, so use a lowercase vault/notes path
        # (matches the real corpus's actual note paths).
        self.assertEqual(
            metrics._classify_bypass(
                "grep -r 'todo' ~/obsidian-vault/notes", set()
            ),
            "guard-false-positive-non-code",
        )

    def test_non_recursive_targeted_grep_was_never_blocked_anyway(self):
        self.assertEqual(
            metrics._classify_bypass("grep 'foo' file.py", set()),
            "was-never-blocked-anyway",
        )


def _main_session(human_messages, skills_fired=()):
    return {
        "is_subagent": False,
        "human_messages": list(human_messages),
        "skills_fired": list(skills_fired),
    }


class EvidenceSnippetTests(unittest.TestCase):
    """Evidence excerpts must contain the phrase they claim to show.

    Passing the regex SOURCE (or the skill name) as the snippet needle never
    matches the message text, so snippet() silently degrades to a
    head-of-message excerpt that can omit the phrase entirely.
    """

    def test_tracked_probe_example_contains_the_matched_phrase(self):
        padding = "unrelated preamble words " * 30  # push the match past 220 chars
        msg = padding + "please use sonnet agents for this refactor"
        sessions = {"s1": _main_session([msg])}
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = metrics.repeated_context(sessions, Path(td), min_sessions=99)
        probe = out["tracked_probes"]["use_sonnet_agents"]
        self.assertEqual(probe["messages"], 1)
        self.assertIn("sonnet agents", probe["examples"][0]["text"].lower())

    def test_skill_gap_example_contains_the_ask_not_the_skill_name(self):
        padding = "context context context " * 30
        msg = padding + "ok now ship it please"
        sessions = {"s1": _main_session([msg])}
        out = metrics.skill_gaps(sessions)
        gap = next(g for g in out["gaps"] if g["skill"] == "ship-to-main")
        self.assertEqual(gap["ask_sessions"], 1)
        self.assertIn("ship it", gap["examples"][0]["text"].lower())


class SelfInstrumentationTests(unittest.TestCase):
    def test_flags_session_that_ran_the_audit(self):
        sessions = {
            "meta": _sess(tool_calls=[_tc(command="cd claude-friction-audit && ./audit.sh")]),
            "real": _sess(tool_calls=[_tc(command="grep -r foo /home/user/code/some-project")]),
        }
        n = metrics.mark_self_instrumentation(sessions)
        self.assertEqual(n, 1)
        self.assertTrue(sessions["meta"]["self_instrumentation"])
        self.assertFalse(sessions["real"]["self_instrumentation"])

    def test_flags_by_read_path(self):
        sessions = {
            "meta": _sess(read_paths=["/home/user/code/claude-friction-audit/metrics.py"]),
        }
        metrics.mark_self_instrumentation(sessions)
        self.assertTrue(sessions["meta"]["self_instrumentation"])

    def test_propagates_to_subagents_of_a_meta_session(self):
        sessions = {
            "parent": _sess(tool_calls=[_tc(command="python3 -m friction_audit.cli")]),
            "kid": _sess(is_subagent=True, parent_session_id="parent", agent_type="Explore"),
            "other_kid": _sess(is_subagent=True, parent_session_id="unrelated"),
        }
        metrics.mark_self_instrumentation(sessions)
        self.assertTrue(sessions["kid"]["self_instrumentation"])
        self.assertFalse(sessions["other_kid"]["self_instrumentation"])


class HookHealthSelfExclusionTests(unittest.TestCase):
    def test_self_session_bypasses_are_not_counted(self):
        meta = _sess(tool_calls=[_tc(command="grep -r x . # [graphify-ok] audit test")])
        meta["self_instrumentation"] = True
        real = _sess(tool_calls=[_tc(command="grep -r y . # [graphify-ok] real")])
        real["self_instrumentation"] = False
        out = metrics.hook_health({"meta": meta, "real": real})
        self.assertEqual(out["graphify_ok_bypasses"], 1)
        self.assertEqual(out["self_sessions_excluded"], 1)


class GraphifyOkAuditSelfExclusionTests(unittest.TestCase):
    def test_drops_entries_from_the_audit_tool_itself(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "audit.log"
            log.write_text(
                json.dumps({"ts": "2026-07-18T10:00:00", "session": "a", "cmd": "grep -rn foo src"})
                + "\n"
                + json.dumps({"ts": "2026-07-18T10:01:00", "session": "b", "cmd": "grep -rn bar friction_audit/"})
                + "\n",
                encoding="utf-8",
            )
            out = metrics.graphify_ok_audit(log)
        self.assertEqual(out["accepted_bypasses"], 1)
        self.assertEqual(out["self_entries_excluded"], 1)


class DailyBehaviourTests(unittest.TestCase):
    def _corpus(self):
        # Pre-fix: heavy main-thread Opus. Post-fix: work delegated to Sonnet.
        sessions = {
            "pre_main": _sess(
                first_ts="2026-07-15T09:00:00",
                tokens_by_model={"claude-opus-4-8": 1000},
            ),
            "post_main": _sess(
                first_ts="2026-07-18T09:00:00",
                tokens_by_model={"claude-opus-4-8": 100},
            ),
            "post_kid": _sess(
                is_subagent=True,
                parent_session_id="post_main",
                agent_type="Explore",
                first_ts="2026-07-18T09:05:00",
                tokens_by_model={"claude-sonnet-5": 900},
            ),
            "meta": _sess(
                first_ts="2026-07-18T12:00:00",
                tokens_by_model={"claude-opus-4-8": 5000},
                tool_calls=[_tc(command="./audit.sh")],
            ),
        }
        metrics.mark_self_instrumentation(sessions)
        return sessions

    def test_excludes_self_and_splits_pre_post(self):
        db = metrics.daily_behaviour(self._corpus(), fixes_applied_at="2026-07-17")
        self.assertEqual(db["self_sessions_excluded"], 1)
        self.assertEqual(db["prefix_days"], 1)
        self.assertEqual(db["postfix_days"], 1)
        # Pre-fix is 100% main-thread; post-fix delegates 900/1000 to a subagent.
        self.assertEqual(db["windows"]["prefix"]["main_thread_share"], 1.0)
        self.assertEqual(db["windows"]["postfix"]["delegated_share"], 0.9)

    def test_postfix_vs_prefix_marks_improvement(self):
        db = metrics.daily_behaviour(self._corpus(), fixes_applied_at="2026-07-17")
        rows = {r["metric"]: r for r in db["postfix_vs_prefix"]}
        self.assertEqual(rows["main_thread_share"]["verdict"], "better")  # went down
        self.assertEqual(rows["opus_share"]["verdict"], "better")  # opus share fell

    def test_meta_session_tokens_never_reach_the_windows(self):
        db = metrics.daily_behaviour(self._corpus(), fixes_applied_at="2026-07-17")
        # The 5000-token meta session would dominate opus_share if counted.
        self.assertNotIn("2026-07-18T12", str(db["windows"]["postfix"]))
        self.assertEqual(db["windows"]["full"]["tokens"], 2000)


if __name__ == "__main__":
    unittest.main()
