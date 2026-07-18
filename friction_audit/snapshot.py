"""Build a snapshot: sweep the corpus once, run every metric, emit JSON."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

from . import __version__, metrics
from .compare import FIXES_APPLIED_AT
from .corpus import DEFAULT_ROOT, Transcript, corpus_summary, discover
from .events import parse_transcript


def _session_record(t: Transcript, parsed) -> dict:
    ts = sorted(parsed.timestamps)
    # Enforce the invariant in one place: a subagent transcript cannot contain
    # a hand-typed message, so no downstream metric can accidentally treat a
    # dispatch prompt as something the user wrote.
    human_messages = [] if t.is_subagent else parsed.human_messages
    return {
        "project": t.project,
        "is_subagent": t.is_subagent,
        "parent_session_id": t.parent_session_id,
        "agent_type": t.agent_type,
        "spawn_depth": t.spawn_depth,
        "usage": parsed.total_usage,
        "usage_by_model": parsed.usage_by_model,
        "tool_calls": parsed.tool_calls,
        "human_messages": human_messages,
        "user_class_counts": parsed.user_class_counts,
        "denials": parsed.denials,
        "blocks": parsed.blocks,
        "read_paths": parsed.read_paths,
        "skills_fired": parsed.skills_fired,
        "slash_commands": parsed.slash_commands,
        "agent_dispatches": parsed.agent_dispatches,
        "turns": len({c.tool_use_id for c in parsed.tool_calls}),
        "bad_lines": parsed.bad_lines,
        "first_ts": ts[0] if ts else None,
        "last_ts": ts[-1] if ts else None,
    }


def sweep(root: Path | str = DEFAULT_ROOT, progress: bool = True) -> dict:
    """One streaming pass over every transcript. Returns the in-memory model."""
    transcripts = discover(root)
    sessions: dict[str, dict] = {}
    seen_message_ids: set[str] = set()
    bad_lines = 0
    failed_files = 0

    for i, t in enumerate(transcripts, 1):
        if progress and i % 200 == 0:
            print(f"  ...{i}/{len(transcripts)} transcripts", file=sys.stderr)
        try:
            parsed = parse_transcript(t.path, seen_message_ids)
        except Exception as exc:  # a single bad file must never kill the run
            failed_files += 1
            print(f"  ! failed {t.path}: {exc}", file=sys.stderr)
            continue
        bad_lines += parsed.bad_lines
        # Subagent transcript stems are NOT globally unique: the same
        # agent-<id>.jsonl appears under two different parent sessions when an
        # agent is continued across sessions. Keying on the bare stem silently
        # overwrites the first copy -- and because the usage dedup is global,
        # the overwritten record is the one holding the tokens, so they vanish
        # from every metric. Disambiguate instead of dropping.
        key = t.session_id
        if key in sessions:
            n = 2
            while f"{key}#{n}" in sessions:
                n += 1
            key = f"{key}#{n}"
        sessions[key] = _session_record(t, parsed)

    return {
        "sessions": sessions,
        "transcripts": transcripts,
        "bad_lines": bad_lines,
        "failed_files": failed_files,
    }


def build(root: Path | str = DEFAULT_ROOT, progress: bool = True) -> dict:
    swept = sweep(root, progress=progress)
    sessions = swept["sessions"]

    # Flag sessions that were building/running this audit BEFORE any metric
    # runs, so both the grep-guard count and the daily view can exclude the
    # observer effect from a single place.
    self_sessions = metrics.mark_self_instrumentation(sessions)

    all_ts = [s["first_ts"] for s in sessions.values() if s["first_ts"]]
    all_ts += [s["last_ts"] for s in sessions.values() if s["last_ts"]]

    # Hand-typed messages only exist in MAIN sessions. A subagent transcript's
    # "user" records are the dispatch prompt and tool results -- never
    # the user -- so counting them inflates the automation denominator.
    main = [s for s in sessions.values() if not s["is_subagent"]]
    human_msgs = sum(len(s["human_messages"]) for s in main)
    human_sessions = sum(1 for s in main if s["human_messages"])
    raw_user = sum(
        sum(s["user_class_counts"].get(k, 0) for k in ("human", "automation"))
        for s in main
    )

    graphed = {t.project for t in swept["transcripts"]}

    snap = {
        "schema": 1,
        "tool_version": __version__,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "corpus": {
            **corpus_summary(swept["transcripts"], root),
            "first_event": min(all_ts) if all_ts else None,
            "last_event": max(all_ts) if all_ts else None,
            "bad_lines": swept["bad_lines"],
            "failed_files": swept["failed_files"],
            "self_instrumentation_sessions": self_sessions,
        },
        "hand_typed_corpus": {
            "messages": human_msgs,
            "sessions": human_sessions,
            "raw_user_prompt_records": raw_user,
            "automation_share": round(1 - human_msgs / raw_user, 4) if raw_user else None,
        },
        "permission_friction": metrics.permission_friction(sessions),
        "token_burn": metrics.token_burn(sessions),
        "model_mix": metrics.model_mix(sessions),
        "reread_files": metrics.most_reread_files(sessions),
        "redelegation": metrics.redelegation(sessions),
        "repeated_context": metrics.repeated_context(
            sessions, Path("~/.claude").expanduser()
        ),
        "skill_gaps": metrics.skill_gaps(sessions),
        "hook_health": metrics.hook_health(sessions, graphed),
        "graphify_ok_audit_log": metrics.graphify_ok_audit(),
        "daily_behaviour": metrics.daily_behaviour(sessions, FIXES_APPLIED_AT),
    }
    return snap


def default_filename(snap: dict) -> str:
    day = (snap.get("generated_at") or "")[:10] or _dt.date.today().isoformat()
    return f"snapshot-{day}.json"
