"""The eight friction metrics.

Every metric returns plain dicts so the snapshot is machine-readable and
directly diffable.  Each finding carries its own evidence (session ids, real
counts) because a finding without evidence does not ship.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .events import (
    EDIT_TOOLS,
    GRAPHIFY_OK,
    Usage,
    model_family,
    prompts_for_permission,
)

# ---------------------------------------------------------------------------
# 0. Self-instrumentation: the audit measuring itself
# ---------------------------------------------------------------------------

#: A session is "self-instrumentation" when it was building or running THIS
#: tool. Its greps, its main-thread Opus work, and its [graphify-ok] bypasses
#: are the observer effect -- counting them measures the measurement, not
#: the user's real behaviour. The 84.5%-main-thread "today" figure that
#: prompted this exclusion was almost entirely one session building the audit.
_SELF_MARKERS_RX = re.compile(
    r"claude-friction-audit|friction_audit|(^|/)audit\.sh\b", re.I
)


def _session_is_self(s: dict) -> bool:
    """True if this session read from, wrote to, or ran the friction-audit tool."""
    for p in s.get("read_paths", ()) or ():
        if isinstance(p, str) and _SELF_MARKERS_RX.search(p):
            return True
    for call in s.get("tool_calls", ()) or ():
        inp = getattr(call, "input", None)
        if not isinstance(inp, dict):
            continue
        for field in ("command", "file_path", "path"):
            v = inp.get(field)
            if isinstance(v, str) and _SELF_MARKERS_RX.search(v):
                return True
    return False


def mark_self_instrumentation(sessions: dict) -> int:
    """Flag every session that was building/running this audit and return the
    count flagged. Sets ``s["self_instrumentation"]`` on each session.

    The flag propagates from a meta main session to the subagents it
    dispatched: a subagent spawned while building the audit is meta too, even
    though its own transcript never names the tool.
    """
    self_mains: set[str] = set()
    for sid, s in sessions.items():
        flag = _session_is_self(s)
        s["self_instrumentation"] = flag
        if flag and not s["is_subagent"]:
            self_mains.add(sid)
    for s in sessions.values():
        if s["is_subagent"] and s.get("parent_session_id") in self_mains:
            s["self_instrumentation"] = True
    return sum(1 for s in sessions.values() if s.get("self_instrumentation"))


# ---------------------------------------------------------------------------
# 1. Permission friction
# ---------------------------------------------------------------------------


def permission_friction(sessions: dict) -> dict:
    """Tool calls by permissionMode, and the population that actually prompted.

    Subagent transcripts NEVER carry permissionMode, so they are excluded
    entirely -- including them fabricates a mode distribution out of nulls.

    A denial recorded while in bypassPermissions is the user interrupting a
    hung job, not answering "no" to a prompt.  Those are counted separately.
    """
    by_mode: Counter = Counter()
    prompting_population = 0
    prompting_by_mode: Counter = Counter()
    real_denials: list[dict] = []
    interrupts = 0
    unknown_mode = 0
    edit_calls = 0

    for sid, s in sessions.items():
        if s["is_subagent"]:
            continue
        for call in s["tool_calls"]:
            mode = call.permission_mode or "unset"
            by_mode[mode] += 1
            if prompts_for_permission(call.name):
                prompting_population += 1
                prompting_by_mode[mode] += 1
            elif call.name in EDIT_TOOLS:
                edit_calls += 1
        for d in s["denials"]:
            mode = d.get("permission_mode")
            if d.get("interrupt"):
                # bypassPermissions: the user killing a hung job, not a "no".
                interrupts += 1
            elif mode is None:
                # Mode never established in this transcript. Absence of
                # evidence is not evidence of a real denial.
                unknown_mode += 1
            else:
                real_denials.append({"session": sid, "mode": mode})

    # Calls that COULD have prompted: a gated tool, not under bypass.
    #
    # This is an UPPER BOUND, not a prompt count. A Bash call under `default`
    # that matches an allowlist entry in settings.json never actually prompts,
    # and the transcript does not record whether a prompt was shown. Treat a
    # move in this number as a trend signal, not as "N prompts appeared".
    friction_events = sum(
        c for m, c in prompting_by_mode.items() if m not in ("bypassPermissions",)
    )
    bypass_share = (
        prompting_by_mode.get("bypassPermissions", 0) / prompting_population
        if prompting_population
        else 0.0
    )

    return {
        "tool_calls_by_mode": dict(by_mode.most_common()),
        "gated_tool_calls": prompting_population,
        "gated_by_mode": dict(prompting_by_mode.most_common()),
        "bypass_share": round(bypass_share, 4),
        "friction_events": friction_events,
        "real_denials": len(real_denials),
        "real_denial_evidence": real_denials[:20],
        "interrupts_under_bypass": interrupts,
        "denials_mode_unknown": unknown_mode,
        "edit_tool_calls": edit_calls,
    }


# ---------------------------------------------------------------------------
# 2. Token burn
# ---------------------------------------------------------------------------


def token_burn(sessions: dict, top_n: int = 10) -> dict:
    total = Usage()
    main = Usage()
    delegated = Usage()

    for s in sessions.values():
        total.add(s["usage"])
        (delegated if s["is_subagent"] else main).add(s["usage"])

    # Roll subagent cost up into the parent main session for ranking.
    rolled: dict[str, Usage] = defaultdict(Usage)
    meta: dict[str, dict] = {}
    for sid, s in sessions.items():
        key = s["parent_session_id"] if s["is_subagent"] else sid
        rolled[key].add(s["usage"])
        if not s["is_subagent"]:
            meta[key] = {"project": s["project"]}

    ranked = sorted(rolled.items(), key=lambda kv: kv[1].total, reverse=True)[:top_n]
    top = []
    for sid, usage in ranked:
        s = sessions.get(sid)
        top.append(
            {
                "session": sid,
                "project": (s or {}).get("project") or meta.get(sid, {}).get("project"),
                "tokens": usage.total,
                "usage": usage.as_dict(),
                "cause": _expensive_cause(sessions, sid),
            }
        )

    grand = total.total or 1
    return {
        "total": total.as_dict(),
        "cache_read_share": round(total.cache_read / grand, 4),
        "main_thread": main.as_dict(),
        "delegated": delegated.as_dict(),
        "main_thread_share": round(main.total / grand, 4),
        "delegated_share": round(delegated.total / grand, 4),
        "top_sessions": top,
    }


def _expensive_cause(sessions: dict, sid: str) -> str:
    """Name the specific driver of a session's cost, not a generic label."""
    s = sessions.get(sid)
    if not s:
        return "subagent-only session (parent transcript missing)"

    kids = [x for x in sessions.values() if x["parent_session_id"] == sid]
    kid_tokens = sum(k["usage"].total for k in kids)
    own = s["usage"].total
    tools = Counter(c.name for c in s["tool_calls"])
    reads = Counter(p for p in s["read_paths"])

    bits = []
    if kids:
        share = kid_tokens / (own + kid_tokens) if (own + kid_tokens) else 0
        agent_types = Counter(k["agent_type"] or "?" for k in kids)
        bits.append(
            f"{len(kids)} subagents ({share:.0%} of session tokens; "
            + ", ".join(f"{n}x{t}" for t, n in agent_types.most_common(3))
            + ")"
        )
    if reads:
        path, n = reads.most_common(1)[0]
        if n >= 5:
            bits.append(f"re-read {Path(path).name} {n}x")
    if tools:
        bits.append("top tools: " + ", ".join(f"{t}x{n}" for t, n in tools.most_common(3)))
    turns = s["turns"]
    # "turns" is distinct tool_use ids, i.e. tool calls -- not API turns.
    bits.append(f"{turns} tool calls")
    return "; ".join(bits) if bits else "no distinguishing driver"


# ---------------------------------------------------------------------------
# 3. Model mix + agent-type leak
# ---------------------------------------------------------------------------


def model_mix(sessions: dict) -> dict:
    by_family: dict[str, Usage] = defaultdict(Usage)
    by_model: dict[str, Usage] = defaultdict(Usage)
    excluded = Usage()

    for s in sessions.values():
        for model, usage in s["usage_by_model"].items():
            by_model[model].add(usage)
            fam = model_family(model)
            if fam is None:
                excluded.add(usage)
            else:
                by_family[fam].add(usage)

    fam_total = sum(u.total for u in by_family.values()) or 1
    mix = {
        fam: {"tokens": u.total, "share": round(u.total / fam_total, 4)}
        for fam, u in sorted(by_family.items(), key=lambda kv: -kv[1].total)
    }

    # Declared agent type vs the model actually used inside each subagent.
    leak: dict[str, dict] = {}
    for s in sessions.values():
        if not s["is_subagent"]:
            continue
        declared = s["agent_type"] or "<unknown>"
        entry = leak.setdefault(declared, {"tokens_by_family": Counter(), "runs": 0})
        entry["runs"] += 1
        for model, usage in s["usage_by_model"].items():
            fam = model_family(model)
            if fam:
                entry["tokens_by_family"][fam] += usage.total

    leak_out = {}
    for declared, entry in sorted(leak.items()):
        tot = sum(entry["tokens_by_family"].values()) or 1
        shares = {
            f: round(t / tot, 4) for f, t in entry["tokens_by_family"].most_common()
        }
        leak_out[declared] = {
            "runs": entry["runs"],
            "tokens": tot,
            "family_share": shares,
            "opus_share": shares.get("opus", 0.0),
        }

    return {
        "by_family": mix,
        "by_model": {m: u.total for m, u in sorted(by_model.items(), key=lambda kv: -kv[1].total)},
        "excluded_synthetic_tokens": excluded.total,
        "agent_type_leak": leak_out,
    }


# ---------------------------------------------------------------------------
# 4. Most re-read files
# ---------------------------------------------------------------------------


def most_reread_files(sessions: dict, top_n: int = 25) -> dict:
    counts: Counter = Counter()
    for s in sessions.values():
        counts.update(s["read_paths"])

    out = []
    for path, n in counts.most_common(top_n):
        p = Path(path)
        lines = None
        try:
            if p.is_file():
                with open(p, encoding="utf-8", errors="replace") as fh:
                    lines = sum(1 for _ in fh)
        except OSError:
            lines = None
        out.append({"path": path, "name": p.name, "reads": n, "lines": lines})
    return {"top": out, "distinct_files_read": len(counts), "total_reads": sum(counts.values())}


# ---------------------------------------------------------------------------
# 5. Sub-agent re-delegation
# ---------------------------------------------------------------------------


def redelegation(sessions: dict) -> dict:
    instances = 0
    files = 0
    parents: Counter = Counter()
    tokens = 0
    evidence = []

    for sid, s in sessions.items():
        if not s["is_subagent"] or not s["agent_dispatches"]:
            continue
        instances += s["agent_dispatches"]
        files += 1
        parent = s["parent_session_id"] or "?"
        parents[parent] += s["agent_dispatches"]
        tokens += s["usage"].total
        evidence.append(
            {
                "subagent": sid,
                "parent_session": parent,
                "project": s["project"],
                "agent_type": s["agent_type"],
                "dispatches": s["agent_dispatches"],
                "tokens": s["usage"].total,
            }
        )

    evidence.sort(key=lambda e: -e["dispatches"])
    return {
        "instances": instances,
        "subagent_files": files,
        "parent_sessions": len(parents),
        "tokens_in_redelegating_subagents": tokens,
        "evidence": evidence[:15],
    }


# ---------------------------------------------------------------------------
# 6. Repeated context
# ---------------------------------------------------------------------------

_STOP = set(
    """the a an and or but if then than so to of in on at for with from by is are was were be been
    being it its this that these those i you he she they we me my your our their as not no yes do
    does did doing done have has had can could should would will just now here there what which who
    when where why how all any some more most other into out up down over under again once please
    lets let need want make made get got go going use used using thing things ok okay well also very
    much many too only own same such about after before between during through above below off
    home know thank thanks read little bit time original status summary note agent finished
    still like look looks see seems think maybe sure right good great nice cool yeah yep nope
    file files code line lines run running ran add added fix fixed check checked update updated
    """.split()
)

_TRACKED_PROBES: dict[str, dict] = {
    "use_sonnet_agents": {
        "label": '"use Sonnet agents" asks',
        "pattern": r"\b(use|using|with)\b[^.\n]{0,40}\b(sonnet|worker|cheaper|scout|haiku)\b[^.\n]{0,30}\bagents?\b|\bsonnet agents?\b|\buse (a )?worker\b",
        "covered_by": "~/.claude/CLAUDE.md '## Model tiering — Opus is the director'",
        "fix_applied": "2026-07-17: added Model tiering section",
    },
    "windows_downloads": {
        "label": '"windows downloads" asks',
        "pattern": r"windows downloads|/mnt/c/Users/[^\s]*/Downloads|downloads folder",
        "covered_by": "memory/reference_asset_drop.md",
        "fix_applied": "2026-07-17: created reference_asset_drop.md + fixed MEMORY.md pointer",
    },
    "read_session_handoff": {
        "label": '"read session handoff" asks',
        "pattern": r"read (the )?(session )?handoff|session handoff|handoff (doc|packet|note)",
        "covered_by": None,
        "fix_applied": None,
    },
    "verify_ask": {
        "label": '"verify / prove it" asks',
        "pattern": r"\bverify\b|\bmake sure it works\b|\bprove it\b|\bdouble[- ]check\b",
        "covered_by": "~/.claude/CLAUDE.md verify trigger bullet",
        "fix_applied": "2026-07-17: added verify trigger bullet",
    },
    "ship_to_main_ask": {
        "label": '"ship it / get it on main" asks',
        "pattern": r"\bship it\b|\bget it on(to)? main\b|\bmerge it\b|\bput it on main\b",
        "covered_by": "~/.claude/CLAUDE.md ship-to-main trigger bullet",
        "fix_applied": "2026-07-17: added ship-to-main trigger bullet",
    },
}


def _normalise(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    words = re.findall(r"[a-z][a-z0-9_.-]{2,}", text.lower())
    return [w for w in words if w not in _STOP]


def snippet(text: str, needle: str, width: int = 200) -> str:
    """Return a window of *text* centred on *needle*.

    Evidence has to show the thing it claims to show. A blind text[:200] on a
    long message cites a phrase the reader cannot find.
    """
    flat = " ".join(text.split())
    i = flat.lower().find(needle.lower())
    if i < 0:
        return flat[:width]
    start = max(0, i - width // 3)
    end = min(len(flat), i + len(needle) + (2 * width) // 3)
    return ("..." if start else "") + flat[start:end] + ("..." if end < len(flat) else "")


def _stitch(bigrams: list[str]) -> str:
    """Rebuild a readable phrase from overlapping bigrams of one template."""
    remaining = sorted(bigrams)
    if not remaining:
        return ""
    chain = remaining[0].split()
    used = {remaining[0]}
    changed = True
    while changed:
        changed = False
        for bg in remaining:
            if bg in used:
                continue
            a, b = bg.split()
            if a == chain[-1]:
                chain.append(b)
            elif b == chain[0]:
                chain.insert(0, a)
            else:
                continue
            used.add(bg)
            changed = True
    extra = [bg for bg in remaining if bg not in used]
    phrase = " ".join(chain)
    if extra:
        phrase += " / " + " / ".join(extra[:3])
    return phrase


def _looks_templated(examples: list[dict]) -> bool:
    """True if the example messages are near-identical across sessions.

    Byte-identical text repeated across many sessions is a cron/template
    (the daily-brief mission prompt is logged with origin.kind=="human"), not
    the user patiently re-explaining something. Reporting those as "repeated
    context you should write a memory for" would be a fabricated finding.
    """
    texts = [e.get("text", "").strip() for e in examples if e.get("text")]
    if len(texts) < 2:
        return False
    # Normalise dates/ids so the daily-brief mission prompt -- identical every
    # run except for the date in its path -- is recognised as one template.
    norm = {re.sub(r"\d+", "#", t) for t in texts}
    return len(norm) == 1


def _load_rule_corpus(claude_dir: Path) -> dict[str, str]:
    """Load CLAUDE.md + memory files so we can test whether a rule exists."""
    out = {}
    candidates = [claude_dir / "CLAUDE.md"]
    # Scan every project's memory dir, not one hard-coded slug, so the rule
    # corpus is whoever's running the audit -- ``~/.claude/projects/*/memory``.
    projects = claude_dir / "projects"
    if projects.is_dir():
        for mem in sorted(projects.glob("*/memory")):
            if mem.is_dir():
                candidates.extend(sorted(mem.glob("*.md")))
    for p in candidates:
        try:
            out[str(p)] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def repeated_context(sessions: dict, claude_dir: Path, min_sessions: int = 3) -> dict:
    """Find things the user re-explains across DIFFERENT sessions.

    A phrase that recurs across many sessions AND is already covered by a rule
    is a FAILING RULE -- the rule exists and did not prevent the repetition.
    """
    rules = _load_rule_corpus(claude_dir)
    rule_blob = "\n".join(rules.values()).lower()

    # --- tracked probes: the exact numbers the 2026-07-17 fixes must move ---
    tracked = {}
    for key, spec in _TRACKED_PROBES.items():
        rx = re.compile(spec["pattern"], re.I)
        hits, hit_sessions = 0, set()
        examples = []
        for sid, s in sessions.items():
            for msg in s["human_messages"]:
                m = rx.search(msg)
                if m:
                    hits += 1
                    hit_sessions.add(sid)
                    if len(examples) < 4:
                        # Centre the snippet on the text that actually matched,
                        # not on the regex source (which never appears in the
                        # message, so snippet() would silently degrade to a
                        # head-of-message excerpt that may not show the phrase).
                        needle = " ".join(m.group(0).split())
                        examples.append({"session": sid, "text": snippet(msg, needle, 220)})
        tracked[key] = {
            "label": spec["label"],
            "messages": hits,
            "sessions": len(hit_sessions),
            "covered_by": spec["covered_by"],
            "fix_applied": spec["fix_applied"],
            "examples": examples,
        }

    # --- discovery: salient bigrams recurring across DIFFERENT sessions -----
    bigram_sessions: dict[str, set] = defaultdict(set)
    bigram_examples: dict[str, list] = defaultdict(list)
    for sid, s in sessions.items():
        seen_here = set()
        for msg in s["human_messages"]:
            words = _normalise(msg)
            for a, b in zip(words, words[1:]):
                bg = f"{a} {b}"
                if bg in seen_here:
                    continue
                seen_here.add(bg)
                bigram_sessions[bg].add(sid)
                if len(bigram_examples[bg]) < 3:
                    bigram_examples[bg].append(
                        {"session": sid, "text": snippet(msg, bg)}
                    )

    # Bigrams from one repeated template all recur in exactly the same set of
    # sessions.  Grouping by that session-set signature collapses
    # "daily-brief runs" / "runs mission.txt" / "mission.txt follow" back into
    # the single template they came from, instead of reporting five rows of
    # noise.  This is what a "repeated thing" actually is.
    total_sessions = max(len([s for s in sessions.values() if s["human_messages"]]), 1)
    clusters: dict[frozenset, list[str]] = defaultdict(list)
    for bg, sids in bigram_sessions.items():
        if len(sids) < min_sessions:
            continue
        # A phrase in most sessions is background noise, not a repeated ask.
        if len(sids) / total_sessions > 0.4:
            continue
        clusters[frozenset(sids)].append(bg)

    ranked = sorted(clusters.items(), key=lambda kv: (-len(kv[0]), -len(kv[1])))

    repeats = []
    for sids, bgs in ranked[:40]:
        phrase = _stitch(bgs)
        covered = any(bg in rule_blob for bg in bgs)
        covering_file = None
        if covered:
            for path, body in rules.items():
                low = body.lower()
                if sum(1 for bg in bgs if bg in low) >= max(1, len(bgs) // 2):
                    covering_file = path
                    break
            covered = covering_file is not None

        rep_examples = bigram_examples[bgs[0]]
        templated = _looks_templated(rep_examples)
        repeats.append(
            {
                "phrase": phrase,
                "terms": sorted(bgs)[:12],
                "sessions": len(sids),
                "covered_by_rule": covered,
                "covering_file": covering_file,
                "templated": templated,
                # Deliberately hedged. Clustering finds RECURRING TOPICS, and a
                # topic recurring is not proof a rule failed -- a client's name
                # recurs because it is a live project, not because a
                # memory entry is broken. The authoritative failing-rule signal
                # is `tracked_probes`, where the rule and its trigger are known
                # explicitly. Overclaiming here would be exactly the fabricated
                # finding this tool exists to prevent.
                "status": (
                    "templated automation (not the user re-explaining)"
                    if templated
                    else "recurring topic; a rule mentions it -- check whether it failed"
                    if covered
                    else "recurring topic; no rule covers it"
                ),
                "examples": rep_examples,
            }
        )

    candidates = [r for r in repeats if r["covered_by_rule"] and not r["templated"]]
    # A tracked probe is a real failing rule: the rule exists, its trigger is
    # known, and the user still had to type the thing anyway.
    failing = [
        {"key": k, **v}
        for k, v in tracked.items()
        if v["covered_by"] and v["sessions"] > 0
    ]
    return {
        "tracked_probes": tracked,
        "repeated_phrases": repeats,
        "failing_rules": len(failing),
        "failing_rule_detail": failing,
        "candidate_failing_rules": len(candidates),
        "uncovered_repeats": len(repeats) - len(candidates),
        "rule_files_scanned": len(rules),
    }


# ---------------------------------------------------------------------------
# 7. Skill invocation gaps
# ---------------------------------------------------------------------------

_SKILL_ASK_PATTERNS = {
    "verify": r"\bverify\b|\bmake sure it works\b|\bprove it\b|\bdouble[- ]check\b",
    "ship-to-main": r"\bship it\b|\bget it on(to)? main\b|\bmerge it\b|\bput it on main\b",
    "graphify": r"\bgraphify\b",
    "cache-warmer": r"\bcache warmer\b|\bwarm the cache\b|\bkeep the cache warm\b",
    "genealogy-research": r"\bfamily tree\b|\bgedcom\b|\bancestor\b|\bfamilysearch\b",
}


def skill_gaps(sessions: dict) -> dict:
    fired: Counter = Counter()
    fired_sessions: dict[str, set] = defaultdict(set)
    for sid, s in sessions.items():
        for sk in s["skills_fired"]:
            fired[sk] += 1
            base = sk.split(":")[-1]
            fired_sessions[base].add(sid)

    gaps = []
    for skill, pattern in _SKILL_ASK_PATTERNS.items():
        rx = re.compile(pattern, re.I)
        ask_sessions, examples = set(), []
        for sid, s in sessions.items():
            for msg in s["human_messages"]:
                m = rx.search(msg)
                if m:
                    ask_sessions.add(sid)
                    if len(examples) < 3:
                        # Centre on the matched text: the ask phrase ("ship it",
                        # "make sure it works") rarely contains the skill name.
                        needle = " ".join(m.group(0).split())
                        examples.append({"session": sid, "text": snippet(msg, needle, 200)})
                    break
        fired_in = fired_sessions.get(skill, set())
        overlap = len(ask_sessions & fired_in)
        gaps.append(
            {
                "skill": skill,
                "ask_sessions": len(ask_sessions),
                "fired_total": sum(v for k, v in fired.items() if k.split(":")[-1] == skill),
                "fired_sessions": len(fired_in),
                "fired_when_asked": overlap,
                "gap_sessions": len(ask_sessions) - overlap,
                "fire_rate": round(overlap / len(ask_sessions), 4) if ask_sessions else None,
                "examples": examples,
            }
        )
    gaps.sort(key=lambda g: -g["gap_sessions"])
    return {"gaps": gaps, "all_skills_fired": dict(fired.most_common())}


# ---------------------------------------------------------------------------
# 8b. The grep-guard's own accepted-bypass audit log
# ---------------------------------------------------------------------------

#: Written by ~/.claude/hooks/graphify-grep-guard.py. One JSON object per
#: accepted bypass: {"ts","session","category","repo","cmd"}.
GRAPHIFY_OK_AUDIT_LOG = Path("~/.claude/hooks/logs/graphify-ok-audit.log").expanduser()


def graphify_ok_audit(
    path: Path | str = GRAPHIFY_OK_AUDIT_LOG, exclude_self: bool = True
) -> dict:
    """Read the guard's accepted-bypass log.

    This is the ground truth the transcript sweep cannot give us: the guard
    itself recording every bypass it ACCEPTED, already classified by the reason
    the caller declared. The transcript can only tell us a marker was typed;
    this says the guard honoured it and why.

    Bypasses emitted while building or running THIS tool are dropped by default
    (``exclude_self``): a grep over ``friction_audit/`` marked ``[graphify-ok]``
    is the audit instrumenting itself, not a real circumvention to count.

    The log starts 2026-07-17, i.e. after the manual audit, so it explains
    nothing about the past and everything about the future. Next month's run is
    the first that can trend it.
    """
    path = Path(path).expanduser()
    entries: list[dict] = []
    bad = 0
    self_excluded = 0
    if not path.is_file():
        return {
            "present": False,
            "path": str(path),
            "note": "guard audit log not found; nothing to measure yet",
        }

    for line in _iter_lines(path):
        try:
            e = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(e, dict):
            continue
        if exclude_self and _SELF_MARKERS_RX.search(
            (e.get("cmd") or "") + " " + (e.get("repo") or "")
        ):
            self_excluded += 1
            continue
        entries.append(e)

    by_cat = Counter(e.get("category") or "unclassified" for e in entries)
    by_repo = Counter(e.get("repo") or "(none)" for e in entries)
    by_day = Counter((e.get("ts") or "")[:10] for e in entries)
    by_session = Counter(e.get("session") or "?" for e in entries)

    examples: dict[str, list] = defaultdict(list)
    for e in entries:
        cat = e.get("category") or "unclassified"
        if len(examples[cat]) < 3:
            examples[cat].append(
                {
                    "ts": e.get("ts"),
                    "session": e.get("session"),
                    "repo": e.get("repo"),
                    "cmd": (e.get("cmd") or "").strip().replace("\n", " ")[:220],
                }
            )

    ts = sorted(e.get("ts") or "" for e in entries)
    total = len(entries) or 1
    return {
        "present": True,
        "path": str(path),
        "accepted_bypasses": len(entries),
        "unparseable_lines": bad,
        "self_entries_excluded": self_excluded,
        "by_category": {
            k: {"count": v, "share": round(v / total, 4)} for k, v in by_cat.most_common()
        },
        "by_repo": dict(by_repo.most_common(10)),
        "by_day": dict(sorted(by_day.items())),
        "distinct_sessions": len(by_session),
        "first_entry": ts[0] if ts else None,
        "last_entry": ts[-1] if ts else None,
        "examples": dict(examples),
    }


def _iter_lines(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return


# ---------------------------------------------------------------------------
# 8. Hook health -- the graphify grep-guard
# ---------------------------------------------------------------------------

#: A real graphify invocation -- the CLI actually being driven, not a command
#: that merely mentions the word (``cat graphify-out/graph.json``, ``ls``,
#: ``pip install``).  Counting mentions inflates this ~3x and measures nothing.
_GRAPHIFY_CLI_RX = re.compile(
    r"(^|[;&|(\s])graphify\s+(query|explain|path|affected|build|community|update)"
)

#: The guard was tightened after the 2026-07-17 audit: a bare marker no longer
#: bypasses it, and a reason is now required. Both forms appear in the corpus.
_BYPASS_REASON_RX = re.compile(re.escape(GRAPHIFY_OK[:-1]) + r"(:\s*([a-z-]+))?\]?")

_SEARCH_RX = re.compile(r"\b(grep|rg|ripgrep|ag|ack|ugrep|find|fgrep|egrep)\b")
_RECURSIVE_RX = re.compile(r"-[a-zA-Z]*r[a-zA-Z]*\b|--recursive|\brg\b")
#: Paths that are not code: the guard has no graph for these, so firing on
#: them is a false positive.
_NON_CODE_RX = re.compile(
    r"vault|/notes|obsidian|\.claude|/tmp|/var/log|\.log\b|MEMORY\.md|/\.config"
)


def _classify_bypass(command: str, graphed_projects: set[str]) -> str:
    if not _SEARCH_RX.search(command):
        return "no-search-command-at-all"
    if _NON_CODE_RX.search(command):
        return "guard-false-positive-non-code"
    if not _RECURSIVE_RX.search(command):
        return "was-never-blocked-anyway"
    return "genuine-circumvention"


def hook_health(
    sessions: dict,
    graphed_projects: set[str] | None = None,
    exclude_self: bool = True,
) -> dict:
    graphed_projects = graphed_projects or set()
    graphify_calls = 0
    bypasses = 0
    blocks = 0
    classes: Counter = Counter()
    reasons: Counter = Counter()
    by_month_calls: Counter = Counter()
    by_month_bypass: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    self_sessions_excluded = 0

    for sid, s in sessions.items():
        # A session that was building/running this audit generates greps and
        # [graphify-ok] bypasses that are the observer effect, not real work.
        # Counting them would let the tool inflate its own bypass number.
        if exclude_self and s.get("self_instrumentation"):
            self_sessions_excluded += 1
            continue
        session_month = (s["first_ts"] or "")[:7]
        blocks += s["blocks"]
        for call in s["tool_calls"]:
            # Bucket by the call's own timestamp, not the session's start.
            month = (call.timestamp or "")[:7] or session_month
            cmd = call.input.get("command") if isinstance(call.input, dict) else None
            if call.name == "Skill" and call.input.get("skill", "").endswith("graphify"):
                graphify_calls += 1
                by_month_calls[month] += 1
                continue
            if not isinstance(cmd, str):
                continue
            if _GRAPHIFY_CLI_RX.search(cmd):
                graphify_calls += 1
                by_month_calls[month] += 1
            if GRAPHIFY_OK[:-1] in cmd:
                bypasses += 1
                by_month_bypass[month] += 1
                m = _BYPASS_REASON_RX.search(cmd)
                reasons[(m.group(2) if m and m.group(2) else "bare")] += 1
                klass = _classify_bypass(cmd, graphed_projects)
                classes[klass] += 1
                if len(examples[klass]) < 4:
                    examples[klass].append({"session": sid, "command": cmd.strip()[:220]})

    total_cls = sum(classes.values()) or 1
    return {
        "graphify_calls": graphify_calls,
        "graphify_ok_bypasses": bypasses,
        "bypass_reasons": dict(reasons.most_common()),
        "actual_blocks": blocks,
        "bypass_to_block_ratio": round(bypasses / blocks, 3) if blocks else None,
        "bypass_classification": {
            k: {"count": v, "share": round(v / total_cls, 4)}
            for k, v in classes.most_common()
        },
        "bypass_examples": {k: v for k, v in examples.items()},
        "calls_by_month": dict(sorted(by_month_calls.items())),
        "bypasses_by_month": dict(sorted(by_month_bypass.items())),
        "self_sessions_excluded": self_sessions_excluded,
    }


# ---------------------------------------------------------------------------
# 9. Daily / windowed behaviour -- "did it work" on RECENT data
# ---------------------------------------------------------------------------
#
# Every metric above is cumulative over the whole corpus. A flawless day barely
# dents a six-week average -- that is arithmetic, not evidence a fix failed.
# This slices the SAME corpus by day and by pre/post-fix window (self-
# instrumentation excluded) so the question "did the 2026-07-17 fixes change
# behaviour" reads recent reality, and so a within-snapshot before/after exists
# that does not need a second snapshot to compute.

#: Share metrics are volume-independent, so a pre/post window comparison is
#: fair even when the two windows hold very different session counts. Raw
#: counts are not, so they are reported per-window but never diffed here.
_WINDOW_TRACKED = [
    ("main_thread_share", "down", "Main-thread token share"),
    ("delegated_share", "up", "Delegated token share"),
    ("opus_share", "down", "Opus share of tokens"),
    ("sonnet_share", "up", "Sonnet share of tokens"),
]


def _behaviour_summary(subset: list[dict]) -> dict:
    """Headline behavioural numbers over an arbitrary set of sessions."""
    total = Usage()
    main = Usage()
    delegated = Usage()
    fam: Counter = Counter()
    leak: dict[str, dict] = defaultdict(lambda: {"opus": 0, "total": 0, "runs": 0})
    bypasses = 0
    blocks = 0
    subagent_runs: Counter = Counter()

    for s in subset:
        total.add(s["usage"])
        (delegated if s["is_subagent"] else main).add(s["usage"])
        for model, usage in s["usage_by_model"].items():
            f = model_family(model)
            if f:
                fam[f] += usage.total
        if s["is_subagent"]:
            declared = s["agent_type"] or "<unknown>"
            subagent_runs[declared] += 1
            entry = leak[declared]
            entry["runs"] += 1
            for model, usage in s["usage_by_model"].items():
                f = model_family(model)
                if f:
                    entry["total"] += usage.total
                    if f == "opus":
                        entry["opus"] += usage.total
        blocks += s["blocks"]
        for call in s["tool_calls"]:
            cmd = call.input.get("command") if isinstance(call.input, dict) else None
            if isinstance(cmd, str) and GRAPHIFY_OK[:-1] in cmd:
                bypasses += 1

    grand = total.total or 1
    fam_total = sum(fam.values()) or 1
    agent_opus = {
        declared: {
            "runs": e["runs"],
            "tokens": e["total"],
            "opus_share": round(e["opus"] / (e["total"] or 1), 4),
        }
        for declared, e in sorted(leak.items())
    }
    return {
        "sessions": len(subset),
        "main_sessions": sum(1 for s in subset if not s["is_subagent"]),
        "subagent_sessions": sum(1 for s in subset if s["is_subagent"]),
        "tokens": total.total,
        "main_thread_share": round(main.total / grand, 4),
        "delegated_share": round(delegated.total / grand, 4),
        "opus_share": round(fam.get("opus", 0) / fam_total, 4),
        "sonnet_share": round(fam.get("sonnet", 0) / fam_total, 4),
        "grep_guard_bypasses": bypasses,
        "grep_guard_blocks": blocks,
        "subagent_runs": dict(subagent_runs.most_common()),
        "agent_opus_share": agent_opus,
    }


def _window_delta(prefix: dict, postfix: dict) -> list[dict]:
    """Pre-fix vs post-fix on the share metrics, each judged by good direction."""
    rows = []
    for key, good, label in _WINDOW_TRACKED:
        o, n = prefix.get(key), postfix.get(key)
        if not isinstance(o, (int, float)) or not isinstance(n, (int, float)):
            continue
        d = n - o
        arrow = "flat" if d == 0 else ("up" if d > 0 else "down")
        verdict = "neutral" if arrow == "flat" else ("better" if arrow == good else "worse")
        rows.append(
            {
                "metric": key,
                "label": label,
                "good_direction": good,
                "prefix": round(o, 4),
                "postfix": round(n, 4),
                "delta": round(d, 4),
                "arrow": arrow,
                "verdict": verdict,
            }
        )
    return rows


def _local_day(ts: str | None, tz: _dt.tzinfo | None = None) -> str:
    """Calendar day of *ts* in *tz* (None = this machine's local zone).

    Transcript timestamps are UTC (``...Z``) but ``FIXES_APPLIED_AT`` and the
    report's days are local dates. Slicing ``ts[:10]`` put every session
    started between local midnight and the UTC offset on the previous day --
    e.g. a 01:30 SAST session on the fix day landed in the pre-fix window.
    A naive or unparseable timestamp falls back to its literal date.
    """
    if not ts:
        return ""
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return ts[:10]
    if parsed.tzinfo is None:
        return parsed.date().isoformat()
    return parsed.astimezone(tz).date().isoformat()


def daily_behaviour(
    sessions: dict,
    fixes_applied_at: str,
    rolling_days: int = 7,
    keep_days: int = 21,
    exclude_self: bool = True,
    tz: _dt.tzinfo | None = None,
) -> dict:
    """Per-day and pre/post-fix behavioural view, self-instrumentation removed.

    ``fixes_applied_at`` (``YYYY-MM-DD``) splits the corpus into a pre-fix and a
    post-fix window; the post-fix-vs-pre-fix share deltas are the honest
    "did it work" read that a diluted cumulative number cannot give.
    """
    kept = [
        s
        for s in sessions.values()
        if not (exclude_self and s.get("self_instrumentation"))
    ]
    excluded = len(sessions) - len(kept)

    by_day_sessions: dict[str, list] = defaultdict(list)
    undated = 0
    for s in kept:
        day = _local_day(s["first_ts"], tz)
        if day:
            by_day_sessions[day].append(s)
        else:
            undated += 1

    days_sorted = sorted(by_day_sessions)
    # The fix DAY is a mix of pre- and post-fix work (fixes land mid-day), so it
    # belongs to neither window. Counting it in "post-fix" is exactly the
    # boundary error that manufactures a false verdict on day one.
    prefix_days = [d for d in days_sorted if d < fixes_applied_at]
    postfix_days = [d for d in days_sorted if d > fixes_applied_at]
    fix_day_present = fixes_applied_at in by_day_sessions
    rolling_win_days = days_sorted[-rolling_days:]

    def _sessions_for(day_list: list[str]) -> list[dict]:
        return [s for d in day_list for s in by_day_sessions[d]]

    prefix_summary = _behaviour_summary(_sessions_for(prefix_days))
    postfix_summary = _behaviour_summary(_sessions_for(postfix_days))

    # Only the tail is emitted per-day: the report shows recent behaviour, and a
    # full six weeks of daily rows is noise nobody reads.
    by_day = {
        day: _behaviour_summary(by_day_sessions[day]) for day in days_sorted[-keep_days:]
    }

    return {
        "fixes_applied_at": fixes_applied_at,
        "self_sessions_excluded": excluded,
        "undated_sessions": undated,
        "days_covered": len(days_sorted),
        "prefix_days": len(prefix_days),
        "postfix_days": len(postfix_days),
        "fix_day_excluded_from_windows": fix_day_present,
        "windows": {
            "full": _behaviour_summary(kept),
            "prefix": prefix_summary,
            "postfix": postfix_summary,
            f"last_{rolling_days}_days": _behaviour_summary(_sessions_for(rolling_win_days)),
        },
        "postfix_vs_prefix": _window_delta(prefix_summary, postfix_summary),
        "by_day": by_day,
    }
