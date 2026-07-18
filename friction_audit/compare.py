"""Diff two snapshots.

The whole point of the tool: a run should show DELTAS, not re-derive
everything from scratch.  A fix that does not move its number did not work,
and :func:`fix_verdicts` says so plainly.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

#: (dotted path, human label, direction that counts as improvement)
TRACKED: list[tuple[str, str, str]] = [
    ("token_burn.total.total", "Total tokens", "down"),
    ("token_burn.main_thread_share", "Main-thread token share", "down"),
    ("token_burn.delegated_share", "Delegated token share", "up"),
    ("token_burn.cache_read_share", "Cache-read share", "up"),
    ("model_mix.by_family.opus.share", "Opus share of tokens", "down"),
    ("model_mix.by_family.sonnet.share", "Sonnet share of tokens", "up"),
    ("model_mix.agent_type_leak.Explore.opus_share", "Explore running on Opus", "down"),
    (
        "model_mix.agent_type_leak.general-purpose.opus_share",
        "general-purpose running on Opus",
        "down",
    ),
    ("permission_friction.friction_events", "Permission friction events", "down"),
    ("permission_friction.real_denials", "Real denials", "down"),
    ("redelegation.instances", "Sub-agent re-delegation instances", "down"),
    ("hook_health.graphify_ok_bypasses", "grep-guard bypasses", "down"),
    ("hook_health.actual_blocks", "grep-guard blocks", "flat"),
    # Post-fix window only, self-instrumentation excluded: the behavioural
    # numbers that actually answer "did it work", undiluted by six weeks of
    # pre-fix history.
    ("daily_behaviour.windows.postfix.main_thread_share", "Post-fix main-thread share", "down"),
    ("daily_behaviour.windows.postfix.opus_share", "Post-fix Opus share of tokens", "down"),
    ("daily_behaviour.windows.postfix.delegated_share", "Post-fix delegated share", "up"),
    ("repeated_context.failing_rules", "Failing rules", "down"),
    (
        "repeated_context.tracked_probes.use_sonnet_agents.sessions",
        '"use Sonnet agents" asks (sessions)',
        "down",
    ),
    (
        "repeated_context.tracked_probes.windows_downloads.sessions",
        '"windows downloads" asks (sessions)',
        "down",
    ),
    ("hand_typed_corpus.messages", "Hand-typed messages", "flat"),
]

#: When the 2026-07-17 fixes were applied. A snapshot whose corpus barely
#: extends past this date CANNOT judge them: the window is almost entirely
#: pre-fix data, so every number will sit still and every fix will look like it
#: failed. Rendering "DID NOT WORK" there would be exactly the kind of
#: confidently-wrong finding this tool exists to prevent.
FIXES_APPLIED_AT = "2026-07-17"

#: Days of post-fix corpus required before a verdict means anything.
MIN_POSTFIX_DAYS = 14

#: Fixes applied on 2026-07-17 and the metric each one must move.
FIX_CHECKS = [
    {
        "fix": "Added '## Model tiering — Opus is the director' to ~/.claude/CLAUDE.md",
        "metric": "repeated_context.tracked_probes.use_sonnet_agents.sessions",
        "want": "down",
        "baseline_note": "57 of 450 sessions at baseline; June 24 -> July 42, diverging",
    },
    {
        "fix": "Added '## Model tiering' section (delegation pressure)",
        "metric": "token_burn.main_thread_share",
        "want": "down",
        "baseline_note": "72.2% of tokens never left the main Opus thread",
    },
    {
        "fix": "Added verify trigger bullet to ~/.claude/CLAUDE.md",
        "metric": "skill_gaps.gaps.verify.fired_total",
        "want": "up",
        "baseline_note": "70 sessions asked, 0 invocations",
    },
    {
        "fix": "Created memory/reference_asset_drop.md + fixed the misleading MEMORY.md DROP line",
        "metric": "repeated_context.tracked_probes.windows_downloads.sessions",
        "want": "down",
        "baseline_note": "24 messages / 19 sessions",
    },
]


def _postfix_days(snap: dict) -> int:
    """Days of corpus after the fixes landed. Negative/zero means none."""
    last = ((snap.get("corpus") or {}).get("last_event") or "")[:10]
    if not last:
        return 0
    try:
        end = _dt.date.fromisoformat(last)
        applied = _dt.date.fromisoformat(FIXES_APPLIED_AT)
    except ValueError:
        return 0
    return (end - applied).days


def get_path(obj: Any, path: str) -> Any:
    """Resolve a dotted path, tolerating list-of-dicts keyed by 'skill'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            return None
        if isinstance(cur, list):
            match = next(
                (
                    x
                    for x in cur
                    if isinstance(x, dict) and part in (x.get("skill"), x.get("phrase"))
                ),
                None,
            )
            if match is None:
                return None
            cur = match
            continue
        return None
    return cur


def _delta(old: Any, new: Any) -> dict:
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return {"old": old, "new": new, "delta": None, "pct": None, "arrow": "new"}
    d = new - old
    pct = (d / old) if old else None
    arrow = "flat" if d == 0 else ("up" if d > 0 else "down")
    return {
        "old": old,
        "new": new,
        "delta": round(d, 6) if isinstance(d, float) else d,
        "pct": round(pct, 4) if pct is not None else None,
        "arrow": arrow,
    }


def compare(old: dict, new: dict) -> dict:
    rows = []
    for path, label, good in TRACKED:
        o, n = get_path(old, path), get_path(new, path)
        if o is None and n is None:
            continue
        d = _delta(o, n)
        if d["arrow"] == "new" or good == "flat" or d["arrow"] == "flat":
            verdict = "neutral"
        else:
            verdict = "better" if d["arrow"] == good else "worse"
        rows.append({"path": path, "label": label, "good_direction": good, "verdict": verdict, **d})

    return {
        "old_generated_at": old.get("generated_at"),
        "new_generated_at": new.get("generated_at"),
        "rows": rows,
        "fix_verdicts": fix_verdicts(old, new),
    }


def fix_verdicts(old: dict, new: dict) -> list[dict]:
    """A fix that doesn't move its number is a fix that didn't work.

    ...but only once there is post-fix data to move. Until then the honest
    answer is "too early", not a false accusation.
    """
    days = _postfix_days(new)
    too_early = days < MIN_POSTFIX_DAYS

    out = []
    for check in FIX_CHECKS:
        o, n = get_path(old, check["metric"]), get_path(new, check["metric"])
        d = _delta(o, n)
        if d["delta"] is None:
            verdict, why = "unknown", "metric not present in both snapshots"
        elif too_early:
            verdict = "TOO EARLY TO TELL"
            why = (
                f"corpus ends {days} day(s) after the fixes landed on "
                f"{FIXES_APPLIED_AT}; it is almost entirely pre-fix data. "
                f"Needs ~{MIN_POSTFIX_DAYS} days of post-fix sessions before a "
                f"still number means anything."
            )
        elif d["delta"] == 0:
            verdict, why = "DID NOT WORK", "the number did not move at all"
        elif d["arrow"] == check["want"]:
            verdict, why = "WORKED", f"moved {check['want']} as intended"
        else:
            verdict, why = "BACKFIRED", f"moved the wrong way (wanted {check['want']})"
        out.append(
            {**check, **d, "verdict": verdict, "why": why, "postfix_days": days}
        )
    return out
