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

    def test_words_that_only_look_like_a_recursive_flag(self):
        for cmd in (
            "grep foo some-report.py",
            "grep --color foo app.py",
            "grep --line-number foo app.py",
            "grep -n foo app.py && rm -rf build",
            "sort -r names.py | grep foo",
            "grep 'a -r b' app.py",
            "grep foo /opt/rg/app.py",
            "grep -er foo app.py",
            "grep -e -r app.py",
            "grep -A3 -m1 foo app.py",
            "grep -- -r app.py",
            "grep foo app.py  # [graphify-ok: literal] don't need the graph",
            "find src -iregex '.*py'",
            "find src -name '*.py' -print0",
            "xargs -r grep foo < list",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    metrics._classify_bypass(cmd, set()), "was-never-blocked-anyway"
                )

    def test_real_tree_searches(self):
        for cmd in (
            "grep -R foo src",
            "grep -rn foo src",
            "grep -nr foo src",
            "grep -nA3 -ri foo src",
            "grep foo src -r",
            "grep --recursive foo src",
            "grep --dereference-recursive foo src",
            "grep --directories=recurse foo src",
            "grep -d recurse foo src",
            "grep --directories recurse foo src",
            "grep -drecurse foo src",
            "egrep -r foo src",
            "/usr/bin/grep -r foo src",
            "cd src && grep -r foo .",
            "cat list | xargs grep -r foo",
            "X=$(grep -r foo src)",
            'echo "$(grep -r foo src)"',
            "echo `grep -r foo src`",
            "bash -c 'grep -r foo src'",
            "grep -r foo src  # it's a literal",
            "git grep foo",
            "git -C repo grep foo",
            "rg foo",
            "ripgrep foo",
            "ag foo",
            "ack foo",
            "find src -exec grep foo {} +",
            "find src -name '*.py' | xargs grep foo",
            "grep foo $(find src -name '*.py')",
            "git ls-files | xargs grep foo",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    metrics._classify_bypass(cmd, set()), "genuine-circumvention"
                )

    def test_a_tool_name_inside_another_word_is_not_a_search(self):
        for cmd in ("cat grep-notes.md", "python3 find_it.py", "ls /opt/rg/"):
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    metrics._classify_bypass(cmd, set()), "no-search-command-at-all"
                )

    def test_a_command_with_unbalanced_quotes_is_still_classified(self):
        self.assertEqual(
            metrics._classify_bypass("grep -r it's src", set()), "genuine-circumvention"
        )
        self.assertEqual(
            metrics._classify_bypass("grep it's some-report.py", set()),
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


def _padding(sid: str) -> str:
    """Filler unique to one session, so only the phrase under test recurs."""
    return " ".join(f"pad{sid}{i}" for i in range(80))


class RepeatedPhraseTests(unittest.TestCase):
    def _phrases(self, messages_by_session):
        sessions = {
            sid: _main_session(messages) for sid, messages in messages_by_session.items()
        }
        # The filler sessions keep the phrase under the 40% background cutoff.
        for i in range(12):
            sessions[f"filler{i}"] = _main_session([f"unrelated{i} topic{i}"])
        with tempfile.TemporaryDirectory() as d:
            return metrics.repeated_context(sessions, Path(d))

    def test_trailing_punctuation_does_not_split_a_phrase(self):
        rc = self._phrases({
            "a": ["please restart nginx"],
            "b": ["then restart nginx."],
            "c": ["can you restart nginx, then stop"],
            "d": ["restart nginx-"],
        })
        terms = [t for r in rc["repeated_phrases"] for t in r["terms"]]
        self.assertIn("restart nginx", terms)
        self.assertNotIn("restart nginx.", terms)
        self.assertNotIn("restart nginx-", terms)
        row = next(r for r in rc["repeated_phrases"] if "restart nginx" in r["terms"])
        self.assertEqual(row["sessions"], 4)

    def test_a_stop_word_with_punctuation_is_still_a_stop_word(self):
        self.assertEqual(metrics._normalise("restart the. nginx"), ["restart", "nginx"])

    def test_inner_punctuation_is_kept(self):
        self.assertEqual(
            metrics._normalise("open mission.txt and foo-bar_baz."),
            ["open", "mission.txt", "foo-bar_baz"],
        )

    def test_evidence_shows_the_phrase_when_stop_words_split_it(self):
        rc = self._phrases({
            sid: [f"{_padding(sid)} restart {middle} nginx {_padding(sid)}"]
            for sid, middle in (("a", "the"), ("b", "all the"), ("c", "the"))
        })
        row = next(r for r in rc["repeated_phrases"] if "restart nginx" in r["terms"])
        self.assertTrue(row["examples"])
        for ex in row["examples"]:
            self.assertIn("restart", ex["text"])
            self.assertIn("nginx", ex["text"])

    def test_evidence_keeps_the_original_case_and_layout(self):
        rc = self._phrases({
            sid: [f"{_padding(sid)} Restart\n\nNGINX {_padding(sid)}"] for sid in ("a", "b", "c")
        })
        row = next(r for r in rc["repeated_phrases"] if "restart nginx" in r["terms"])
        for ex in row["examples"]:
            self.assertIn("Restart NGINX", ex["text"])

    def test_words_separated_by_a_long_code_block_still_give_evidence(self):
        code = "```\n" + "x = 1\n" * 200 + "```"
        rc = self._phrases({sid: [f"restart {code} nginx"] for sid in ("a", "b", "c")})
        row = next(r for r in rc["repeated_phrases"] if "restart nginx" in r["terms"])
        for ex in row["examples"]:
            self.assertIn("restart", ex["text"])
            self.assertLess(len(ex["text"]), 400)

    def test_uncovered_count_leaves_out_templated_and_covered_phrases(self):
        sessions = {
            "a": ["restart nginx today"],
            "b": ["restart nginx again", "morning digest 1"],
            "c": ["restart nginx later", "morning digest 2"],
            "d": ["morning digest 3"],
            "e": ["rotate logs now"],
            "f": ["rotate logs soon"],
            "g": ["rotate logs later"],
            "h": ["rotate logs again"],
        }
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "CLAUDE.md").write_text("Rule: rotate logs weekly.", encoding="utf-8")
            sessions = {sid: _main_session(m) for sid, m in sessions.items()}
            for i in range(12):
                sessions[f"filler{i}"] = _main_session([f"unrelated{i} topic{i}"])
            rc = metrics.repeated_context(sessions, Path(d))
        status = {r["terms"][0]: r for r in rc["repeated_phrases"]}
        self.assertTrue(status["morning digest"]["templated"])
        self.assertTrue(status["rotate logs"]["covered_by_rule"])
        self.assertFalse(status["restart nginx"]["covered_by_rule"])
        self.assertEqual(rc["uncovered_repeats"], 1)


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
