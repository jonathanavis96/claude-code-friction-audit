"""Render a self-contained HTML friction-audit report.

STDLIB ONLY. No external requests, no external CSS/JS/fonts, no templating
engine. Every interpolated string is HTML-escaped -- transcript text is real
user prose and contains angle brackets, quotes, and stray markup.

Design contract (see the dispatch brief this was built against):

* A scannable overview table up top: one row per finding, headline number,
  delta vs. last run coloured by whether the move is GOOD or BAD (not just
  by direction).
* Each finding is a <details>/<summary> block with real evidence
  (session ids, counts, example strings) and a concrete, paste-ready fix.
* Findings are ranked by time actually lost, most expensive first.
* Findings that turned out fine get a distinct, muted "Not your problem"
  section instead of being buried or hidden.
* Fix verdicts are rendered bluntly: a fix whose number did not move is
  "DID NOT WORK", full stop.
* Never crashes on a missing key -- this runs unattended under cron.
"""

from __future__ import annotations

import html
from typing import Any

# ---------------------------------------------------------------------------
# small generic helpers
# ---------------------------------------------------------------------------


def _e(x: Any) -> str:
    """HTML-escape anything, tolerating None."""
    if x is None:
        return ""
    return html.escape(str(x))


def _fmt_num(n: Any) -> str:
    if n is None:
        return "—"
    if isinstance(n, bool):
        return str(n)
    if isinstance(n, float):
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    if isinstance(n, int):
        return f"{n:,}"
    return _e(n)


def _fmt_pct(x: Any, decimals: int = 1) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_date(ts: Any) -> str:
    if not ts or not isinstance(ts, str):
        return "?"
    return ts[:10]


def _short(sid: Any, n: int = 8) -> str:
    s = str(sid) if sid is not None else "?"
    return s[:n]


def _pre(text: Any) -> str:
    return f"<pre>{_e(text)}</pre>"


def _g(d: Any, *path: str, default: Any = None) -> Any:
    """Safe nested .get() chain -- never raises on a missing/odd-shaped key."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _resolve(obj: Any, path: str) -> Any:
    """Resolve a dotted path, tolerating list-of-dicts keyed by a few known
    identity fields (skill / phrase / path / declared-agent-type key)."""
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
                    if isinstance(x, dict)
                    and part in (x.get("skill"), x.get("phrase"), x.get("path"))
                ),
                None,
            )
            if match is None:
                return None
            cur = match
            continue
        return None
    return cur


def _local_delta(old: Any, new: Any, good: str | None) -> dict | None:
    if not isinstance(old, (int, float)) or isinstance(old, bool):
        return None
    if not isinstance(new, (int, float)) or isinstance(new, bool):
        return None
    d = new - old
    pct = (d / old) if old else None
    arrow = "flat" if d == 0 else ("up" if d > 0 else "down")
    if good is None or arrow == "flat":
        verdict = "neutral"
    else:
        verdict = "better" if arrow == good else "worse"
    return {"old": old, "new": new, "delta": d, "pct": pct, "arrow": arrow, "verdict": verdict}


_ARROW_SYM = {"up": "↑", "down": "↓", "flat": "→"}
_VERDICT_CLASS = {"better": "badge-good", "worse": "badge-bad", "neutral": "badge-neutral"}


def badge(diff_idx: dict, path: str, prev: dict | None = None, snap: dict | None = None,
          good: str | None = None) -> str:
    """A delta badge for *path*: prefers the officially-tracked compare.py row
    (whose verdict already accounts for good_direction), falls back to a
    locally-computed delta against *prev* for metrics compare.py doesn't
    track, and falls back to a plain "new" badge on a first run."""
    row = diff_idx.get(path) if diff_idx else None
    if row is not None:
        arrow = row.get("arrow", "?")
        verdict = row.get("verdict", "neutral")
        pct = row.get("pct")
    elif prev is not None and snap is not None:
        o, n = _resolve(prev, path), _resolve(snap, path)
        d = _local_delta(o, n, good)
        if d is None:
            return '<span class="badge badge-new">new</span>'
        arrow, verdict, pct = d["arrow"], d["verdict"], d["pct"]
    else:
        return '<span class="badge badge-new">new</span>'
    sym = _ARROW_SYM.get(arrow, "?")
    cls = _VERDICT_CLASS.get(verdict, "badge-neutral")
    pct_txt = f" {pct * 100:+.1f}%" if pct is not None else ""
    return f'<span class="badge {cls}">{sym}{pct_txt}</span>'


def _table(headers: list[str], rows: list[list[str]], empty_msg: str = "No data.") -> str:
    if not rows:
        return f'<p class="muted">{_e(empty_msg)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _examples_block(examples: list) -> str:
    if not examples:
        return ""
    parts = []
    for ex in examples[:5]:
        if isinstance(ex, dict):
            sid = _short(ex.get("session"))
            text = ex.get("text") or ex.get("command") or ""
            parts.append(f'<div class="example"><span class="ex-sid">{_e(sid)}</span>{_pre(text)}</div>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# finding assembly
# ---------------------------------------------------------------------------


class Finding:
    __slots__ = ("fid", "title", "headline", "badge_html", "verdict_class", "body_html")

    def __init__(self, fid: str, title: str, headline: str, badge_html: str,
                 verdict_class: str, body_html: str):
        self.fid = fid
        self.title = title
        self.headline = headline
        self.badge_html = badge_html
        self.verdict_class = verdict_class
        self.body_html = body_html


def _finding_token_burn(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    tb = _g(snap, "token_burn", default={})
    total = _g(tb, "total", default={})
    main_share = tb.get("main_thread_share")
    delegated_share = tb.get("delegated_share")
    cache_share = tb.get("cache_read_share")
    top_sessions = _g(tb, "top_sessions", default=[])

    b_main = badge(diff_idx, "token_burn.main_thread_share", prev, snap, good="down")
    b_total = badge(diff_idx, "token_burn.total.total", prev, snap, good="down")
    b_delegated = badge(diff_idx, "token_burn.delegated_share", prev, snap, good="up")
    b_cache = badge(diff_idx, "token_burn.cache_read_share", prev, snap, good="up")

    rows = []
    for s in top_sessions[:10]:
        usage = s.get("usage") or {}
        rows.append([
            f'<code>{_e(_short(s.get("session")))}</code>',
            _e(s.get("project")),
            _fmt_num(s.get("tokens")),
            (
                f'cache_read {_fmt_num(usage.get("cache_read"))} / cache_create '
                f'{_fmt_num(usage.get("cache_create"))} / fresh {_fmt_num(usage.get("fresh"))} '
                f'/ output {_fmt_num(usage.get("output"))}'
            ),
            f'<span class="cause">{_e(s.get("cause"))}</span>',
        ])
    top_table = _table(
        ["session", "project", "tokens", "usage split", "cause"], rows,
        "No top sessions recorded.",
    )

    worst = top_sessions[0] if top_sessions else None
    worst_note = ""
    if worst:
        worst_note = (
            f'<p><strong>Worst offender:</strong> session '
            f'<code>{_e(_short(worst.get("session")))}</code> in '
            f'<code>{_e(worst.get("project"))}</code> — '
            f'{_fmt_num(worst.get("tokens"))} tokens. {_e(worst.get("cause"))}</p>'
        )

    body = f"""
      <p>Total corpus tokens: <strong>{_fmt_num(total.get("total"))}</strong> {b_total}</p>
      <ul class="stat-list">
        <li>Main-thread share (never delegated): <strong>{_fmt_pct(main_share)}</strong> {b_main}</li>
        <li>Delegated (subagent) share: <strong>{_fmt_pct(delegated_share)}</strong> {b_delegated}</li>
        <li>Cache-read share: <strong>{_fmt_pct(cache_share)}</strong> {b_cache}
            <span class="muted">(cache reads are cheap re-reads, not fresh burn — a healthy
            share here is a good sign, see "Not your problem" below)</span></li>
      </ul>
      <p class="muted">Token split (corpus total): cache_read {_fmt_num(total.get("cache_read"))} /
        cache_create {_fmt_num(total.get("cache_create"))} / fresh {_fmt_num(total.get("fresh"))} /
        output {_fmt_num(total.get("output"))}</p>
      {worst_note}
      <h4>Top 10 most expensive sessions (subagent cost rolled up to parent)</h4>
      {top_table}
      <h4>Fix</h4>
      <p>This is the exact metric FIX_CHECKS #2 targets (the
        "Model tiering — Opus is the director" section added to
        <code>~/.claude/CLAUDE.md</code> on 2026-07-17). See the Fix Verdicts section
        above for whether it actually moved this number. If it is still
        "DID NOT WORK" or "BACKFIRED", the concrete next step is visible in the
        causes above: sessions with no subagent line in their "cause" column did
        all their own tool-calling instead of dispatching — that is main-thread
        share accumulating in real time, not a measurement artefact.</p>
    """
    verdict = "bad" if (main_share or 0) > 0.5 else ("good" if (main_share or 0) < 0.3 else "neutral")
    headline = f"{_fmt_pct(main_share)} of tokens never leave the main Opus thread"
    return Finding("finding-token-burn", "Token burn / main-thread share", headline, b_main, verdict, body)


def _finding_model_mix(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    mm = _g(snap, "model_mix", default={})
    by_family = _g(mm, "by_family", default={})
    opus = by_family.get("opus", {})
    sonnet = by_family.get("sonnet", {})
    leak = _g(mm, "agent_type_leak", default={})

    b_opus = badge(diff_idx, "model_mix.by_family.opus.share", prev, snap, good="down")
    b_sonnet = badge(diff_idx, "model_mix.by_family.sonnet.share", prev, snap, good="up")

    fam_rows = []
    for fam, info in by_family.items():
        fam_rows.append([_e(fam), _fmt_num(info.get("tokens")), _fmt_pct(info.get("share"))])
    fam_table = _table(["family", "tokens", "share"], fam_rows)

    leak_rows = []
    leak_fixes = []
    for declared, info in sorted(leak.items(), key=lambda kv: -(kv[1].get("opus_share") or 0)):
        opus_share = info.get("opus_share") or 0.0
        is_cheap_type = declared.lower() in ("explore", "general-purpose", "scout", "worker")
        path = f"model_mix.agent_type_leak.{declared}.opus_share"
        b = badge(diff_idx, path, prev, snap, good="down")
        flag = ' <span class="flag-bad">LEAK</span>' if (is_cheap_type and opus_share > 0) else ""
        leak_rows.append([
            f"<code>{_e(declared)}</code>{flag}",
            _fmt_num(info.get("runs")),
            _fmt_num(info.get("tokens")),
            f"{_fmt_pct(opus_share)} {b}",
        ])
        if is_cheap_type and opus_share > 0:
            leak_fixes.append(
                f"<li><code>{_e(declared)}</code> ran on Opus for "
                f"{_fmt_pct(opus_share)} of its {_fmt_num(info.get('tokens'))} tokens across "
                f"{_fmt_num(info.get('runs'))} runs — pin the model explicitly "
                f"(<code>model: \"sonnet\"</code> or <code>\"haiku\"</code> in the Agent dispatch) "
                f"instead of letting it inherit the caller's model.</li>"
            )
    leak_table = _table(["declared agent_type", "runs", "tokens", "opus_share"], leak_rows)
    fixes_html = (
        "<h4>Fix</h4><ul>" + "".join(leak_fixes) + "</ul>"
        if leak_fixes else
        '<p class="muted">No cheap-tier agent type (Explore/general-purpose/scout/worker) is '
        "leaking onto Opus — nothing to fix here.</p>"
    )

    body = f"""
      <h4>Tokens by model family</h4>
      {fam_table}
      <p>Sonnet share {b_sonnet}</p>
      <h4>Agent-type leak: declared type vs. model actually used</h4>
      {leak_table}
      {fixes_html}
    """
    headline = f"Opus share of all tokens: {_fmt_pct(opus.get('share'))} (Sonnet: {_fmt_pct(sonnet.get('share'))})"
    verdict = "bad" if any(
        info.get("opus_share", 0) > 0 and k.lower() in ("explore", "general-purpose")
        for k, info in leak.items()
    ) else "neutral"
    return Finding("finding-model-mix", "Model mix / agent-type leak", headline, b_opus, verdict, body)


def _finding_redelegation(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    rd = _g(snap, "redelegation", default={})
    instances = rd.get("instances", 0)
    b = badge(diff_idx, "redelegation.instances", prev, snap, good="down")

    rows = []
    for e in _g(rd, "evidence", default=[])[:15]:
        rows.append([
            f'<code>{_e(_short(e.get("subagent")))}</code>',
            f'<code>{_e(_short(e.get("parent_session")))}</code>',
            _e(e.get("project")),
            _e(e.get("agent_type")),
            _fmt_num(e.get("dispatches")),
            _fmt_num(e.get("tokens")),
        ])
    table = _table(
        ["subagent", "parent session", "project", "agent_type", "dispatches", "tokens"],
        rows, "No sub-agent-launches-sub-agent instances found.",
    )

    if instances:
        fix = (
            f"<h4>Fix</h4><p>{_fmt_num(instances)} instances across "
            f"{_fmt_num(rd.get('subagent_files'))} subagent transcripts "
            f"({_fmt_num(rd.get('parent_sessions'))} distinct parent sessions), burning "
            f"{_fmt_num(rd.get('tokens_in_redelegating_subagents'))} tokens inside the "
            f"redelegating subagents alone. Every dispatch prompt needs the explicit line "
            f'"Do NOT dispatch sub-agents — do all the work yourself" (already in your '
            f"personal CLAUDE.md guidance) — the evidence above shows it is still being "
            f"skipped for these agent types.</p>"
        )
        verdict = "bad"
    else:
        fix = '<p class="muted">Zero re-delegation instances — subagents are staying leaf-level.</p>'
        verdict = "good"

    body = f"""
      <p>Subagent files involved: {_fmt_num(rd.get("subagent_files"))} |
         distinct parent sessions: {_fmt_num(rd.get("parent_sessions"))} |
         tokens inside redelegating subagents: {_fmt_num(rd.get("tokens_in_redelegating_subagents"))}</p>
      {table}
      {fix}
    """
    headline = f"{_fmt_num(instances)} sub-agent instances themselves dispatched further sub-agents"
    return Finding("finding-redelegation", "Sub-agent re-delegation", headline, b, verdict, body)


def _finding_repeated_context(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    rc = _g(snap, "repeated_context", default={})
    tracked = _g(rc, "tracked_probes", default={})
    failing_rules = rc.get("failing_rules", 0)
    b = badge(diff_idx, "repeated_context.failing_rules", prev, snap, good="down")

    probe_rows = []
    for key, p in tracked.items():
        path = f"repeated_context.tracked_probes.{key}.sessions"
        pb = badge(diff_idx, path, prev, snap, good="down")
        covered = p.get("covered_by")
        fix_applied = p.get("fix_applied")
        probe_rows.append([
            _e(p.get("label")),
            _fmt_num(p.get("messages")),
            f'{_fmt_num(p.get("sessions"))} {pb}',
            _e(covered) if covered else '<span class="muted">uncovered</span>',
            _e(fix_applied) if fix_applied else '<span class="muted">none applied</span>',
        ])
    probe_table = _table(["probe", "messages", "sessions", "covered by", "fix applied"], probe_rows)

    probe_examples = "".join(
        f"<details class=\"nested\"><summary>{_e(p.get('label'))} — example asks</summary>"
        f"{_examples_block(p.get('examples', []))}</details>"
        for p in tracked.values() if p.get("examples")
    )

    failing = [p for p in _g(rc, "repeated_phrases", default=[]) if p.get("status") == "FAILING RULE"]
    fail_rows = []
    for p in failing[:20]:
        fail_rows.append([
            f'<code>{_e(p.get("phrase"))}</code>',
            _fmt_num(p.get("sessions")),
            _e(p.get("covering_file")) or "—",
        ])
    fail_table = _table(["phrase", "sessions", "covering file"], fail_rows, "No failing rules found.")

    fail_examples = "".join(
        f"<details class=\"nested\"><summary><code>{_e(p.get('phrase'))}</code> — evidence "
        f"({_fmt_num(p.get('sessions'))} sessions, covered by {_e(p.get('covering_file'))})</summary>"
        f"{_examples_block(p.get('examples', []))}</details>"
        for p in failing[:10]
    )

    if failing:
        fix = (
            f"<h4>Fix</h4><p>{len(failing)} phrases recur across multiple sessions despite an "
            f"existing rule covering them. The rule exists but is not preventing the repetition — "
            f"expand the excerpt below out to see the actual re-asks, then tighten the covering "
            f"file's trigger wording or add the literal phrasing as an explicit example.</p>"
            f"{fail_examples}"
        )
        verdict = "bad"
    else:
        fix = '<p class="muted">No covered rule is failing to prevent repetition right now.</p>'
        verdict = "good"

    body = f"""
      <h4>Tracked probes (fixes applied 2026-07-17 must move these)</h4>
      {probe_table}
      {probe_examples}
      <h4>Failing rules: covered by an existing rule, still repeated</h4>
      {fail_table}
      {fix}
      <p class="muted">{_fmt_num(rc.get("uncovered_repeats"))} additional repeated phrases have no
        covering rule at all (not shown — those are backlog, not broken rules) across
        {_fmt_num(rc.get("rule_files_scanned"))} rule files scanned.</p>
    """
    headline = f"{_fmt_num(failing_rules)} rules that exist and still got repeated"
    return Finding("finding-repeated-context", "Repeated context", headline, b, verdict, body)


def _finding_skill_gaps(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    sg = _g(snap, "skill_gaps", default={})
    gaps = _g(sg, "gaps", default=[])

    rows = []
    for g in gaps:
        skill = g.get("skill")
        b_fired = badge(diff_idx, f"skill_gaps.gaps.{skill}.fired_total", prev, snap, good="up")
        b_gap = badge(diff_idx, f"skill_gaps.gaps.{skill}.gap_sessions", prev, snap, good="down")
        flag = ' <span class="flag-bad">NEVER FIRES</span>' if (
            g.get("ask_sessions", 0) > 0 and g.get("fired_total", 0) == 0
        ) else ""
        rows.append([
            f"<code>{_e(skill)}</code>{flag}",
            _fmt_num(g.get("ask_sessions")),
            f'{_fmt_num(g.get("fired_total"))} {b_fired}',
            _fmt_num(g.get("fired_sessions")),
            f'{_fmt_num(g.get("gap_sessions"))} {b_gap}',
            _fmt_pct(g.get("fire_rate")),
        ])
    table = _table(
        ["skill", "ask_sessions", "fired_total", "fired_sessions", "gap_sessions", "fire_rate"],
        rows, "No tracked skill-ask patterns found.",
    )

    examples_html = "".join(
        f"<details class=\"nested\"><summary><code>{_e(g.get('skill'))}</code> — example asks "
        f"that did not fire it</summary>{_examples_block(g.get('examples', []))}</details>"
        for g in gaps if g.get("gap_sessions", 0) > 0 and g.get("examples")
    )

    worst = max(gaps, key=lambda g: g.get("gap_sessions", 0), default=None)
    fix = ""
    verdict = "neutral"
    if worst and worst.get("gap_sessions", 0) > 0:
        verdict = "bad"
        fix = (
            f"<h4>Fix</h4><p><code>{_e(worst.get('skill'))}</code> was asked for in "
            f"{_fmt_num(worst.get('ask_sessions'))} sessions but only actually fired in "
            f"{_fmt_num(worst.get('fired_sessions'))} of them ({_fmt_num(worst.get('gap_sessions'))} "
            f"gap sessions, fire rate {_fmt_pct(worst.get('fire_rate'))}). Expand the examples "
            f"below, then either broaden the skill's trigger phrase list to match the actual "
            f"wording used, or wire an explicit hook/trigger for it.</p>{examples_html}"
        )
    elif gaps:
        fix = '<p class="muted">Every tracked skill fires whenever its trigger phrase appears — no gap.</p>'
        verdict = "good"

    all_fired = _g(sg, "all_skills_fired", default={})
    fired_rows = [[f"<code>{_e(k)}</code>", _fmt_num(v)] for k, v in list(all_fired.items())[:15]]
    fired_table = _table(["skill (as fired)", "count"], fired_rows, "No skills fired at all.")

    headline = (
        f"'{worst.get('skill')}' asked {_fmt_num(worst.get('ask_sessions'))}x, "
        f"fired {_fmt_num(worst.get('fired_total'))}x"
    ) if worst else "No skill-gap data"
    b_headline = badge(diff_idx, f"skill_gaps.gaps.{worst.get('skill')}.gap_sessions", prev, snap, good="down") if worst else ""

    body = f"""
      {table}
      {fix}
      <h4>All skills that fired at least once (context)</h4>
      {fired_table}
    """
    return Finding("finding-skill-gaps", "Skill invocation gaps", headline, b_headline, verdict, body)


def _finding_hook_health(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    hh = _g(snap, "hook_health", default={})
    calls = hh.get("graphify_calls", 0)
    bypasses = hh.get("graphify_ok_bypasses", 0)
    blocks = hh.get("actual_blocks", 0)
    classification = _g(hh, "bypass_classification", default={})
    genuine = classification.get("genuine-circumvention", {}).get("count", 0)

    b_bypass = badge(diff_idx, "hook_health.graphify_ok_bypasses", prev, snap, good="down")
    b_blocks = badge(diff_idx, "hook_health.actual_blocks", prev, snap, good=None)

    class_rows = []
    for cls, info in classification.items():
        path = f"hook_health.bypass_classification.{cls}.count"
        cb = badge(diff_idx, path, prev, snap, good="down" if cls == "genuine-circumvention" else None)
        class_rows.append([_e(cls), _fmt_num(info.get("count")), f'{_fmt_pct(info.get("share"))} {cb}'])
    class_table = _table(["bypass class", "count", "share"], class_rows, "No [graphify-ok] bypasses recorded.")

    examples = _g(hh, "bypass_examples", default={})
    examples_html = "".join(
        f"<details class=\"nested\"><summary><code>{_e(cls)}</code> — {len(cmds)} example command(s)</summary>"
        + "".join(f'<div class="example"><span class="ex-sid">{_e(_short(c.get("session")))}</span>{_pre(c.get("command"))}</div>' for c in cmds)
        + "</details>"
        for cls, cmds in examples.items()
    )

    if genuine > 0:
        fix = (
            f"<h4>Fix</h4><p>{_fmt_num(genuine)} bypasses are genuine circumvention (a recursive "
            f"search on real code, marked <code>[graphify-ok]</code> anyway) — paste-ready action: "
            f"either add the specific commands below to the guard's non-code exclusion list if they "
            f"are legitimate, or stop marking them ok and use <code>graphify query</code> instead.</p>"
            f"{examples_html}"
        )
        verdict = "bad"
    else:
        fix = (
            '<p class="muted">Zero genuine circumventions — every <code>[graphify-ok]</code> bypass '
            "is either a non-code path the guard has no graph for, or a command that was never "
            "recursive and would not have been blocked anyway. The grep-guard is working as intended.</p>"
            + examples_html
        )
        verdict = "good"

    body = f"""
      <p>graphify calls: {_fmt_num(calls)} | bypasses: {_fmt_num(bypasses)} {b_bypass} |
         real blocks: {_fmt_num(blocks)} {b_blocks} |
         bypass:block ratio: {_fmt_num(hh.get("bypass_to_block_ratio"))}</p>
      <h4>Bypass classification</h4>
      {class_table}
      {fix}
    """
    headline = f"{_fmt_num(bypasses)} grep-guard bypasses ({_fmt_num(genuine)} genuine) vs {_fmt_num(blocks)} real blocks"
    return Finding("finding-hook-health", "Hook health (grep-guard)", headline, b_bypass, verdict, body)


def _finding_reread_files(snap: dict, prev: dict | None, diff_idx: dict) -> Finding:
    rf = _g(snap, "reread_files", default={})
    top = _g(rf, "top", default=[])

    rows = []
    fixes = []
    for f in top:
        path = f.get("path")
        reads = f.get("reads", 0)
        lines = f.get("lines")
        b = badge(diff_idx, f"reread_files.top.{path}.reads", prev, snap, good="down")
        rows.append([f'<code>{_e(path)}</code>', f"{_fmt_num(reads)} {b}", _fmt_num(lines)])
        if reads and reads >= 5 and isinstance(lines, int) and lines >= 200:
            fixes.append(
                f"<li><code>{_e(f.get('name'))}</code> ({_fmt_num(lines)} lines) was read "
                f"{_fmt_num(reads)}x — candidate for splitting into smaller modules so a re-read "
                f"doesn't re-spend the whole file's context every time.</li>"
            )
    table = _table(["path", "reads", "lines"], rows, "No re-read files recorded.")

    fix_html = (
        "<h4>Fix</h4><ul>" + "".join(fixes) + "</ul>"
        if fixes else '<p class="muted">No single file is being re-read often enough (≥5x at ≥200 lines) to justify a split.</p>'
    )

    headline = (
        f"{_e(top[0].get('name'))} read {_fmt_num(top[0].get('reads'))}x "
        f"({_fmt_num(top[0].get('lines'))} lines)"
    ) if top else "No re-read files"
    b_headline = badge(diff_idx, f"reread_files.top.{top[0].get('path')}.reads", prev, snap, good="down") if top else ""

    body = f"""
      <p>Distinct files read: {_fmt_num(rf.get("distinct_files_read"))} |
         total reads: {_fmt_num(rf.get("total_reads"))}</p>
      {table}
      {fix_html}
    """
    verdict = "bad" if fixes else "neutral"
    return Finding("finding-reread-files", "Most re-read files", headline, b_headline, verdict, body)


def _finding_permission_friction(snap: dict, prev: dict | None, diff_idx: dict) -> tuple[Finding, bool]:
    """Returns (finding, is_fine) -- is_fine drives the Not-your-problem callout."""
    pf = _g(snap, "permission_friction", default={})
    real_denials = pf.get("real_denials", 0)
    friction_events = pf.get("friction_events", 0)
    bypass_share = pf.get("bypass_share")

    b_friction = badge(diff_idx, "permission_friction.friction_events", prev, snap, good="down")
    b_denials = badge(diff_idx, "permission_friction.real_denials", prev, snap, good="down")

    mode_rows = [[f"<code>{_e(m)}</code>", _fmt_num(n)] for m, n in _g(pf, "tool_calls_by_mode", default={}).items()]
    mode_table = _table(["permission mode", "tool calls"], mode_rows)

    denial_rows = [
        [f'<code>{_e(_short(d.get("session")))}</code>', _e(d.get("mode"))]
        for d in _g(pf, "real_denial_evidence", default=[])
    ]
    denial_table = _table(["session", "mode"], denial_rows, "No real denials recorded.")

    is_fine = real_denials == 0
    if is_fine:
        fix = (
            '<p class="muted">Zero real denials in the whole corpus — permission prompts are not '
            "costing real time. Do not spend effort tightening the allowlist further; see "
            '"Not your problem" below.</p>'
        )
        verdict = "good"
    else:
        fix = (
            f"<h4>Fix</h4><p>{_fmt_num(real_denials)} real denials (you actually said no, not an"
            f"interrupt under bypass mode) — review the sessions below and either pre-allow the "
            f"specific tool pattern that keeps getting denied, or leave it denied deliberately.</p>"
            f"{denial_table}"
        )
        verdict = "bad"

    body = f"""
      <p>Gated tool calls (population that can actually prompt): {_fmt_num(pf.get("gated_tool_calls"))} |
         friction events (gated, not under bypass): {_fmt_num(friction_events)} {b_friction} |
         bypass share: {_fmt_pct(bypass_share)} |
         real denials: {_fmt_num(real_denials)} {b_denials} |
         interrupts under bypass (not real denials): {_fmt_num(pf.get("interrupts_under_bypass"))}</p>
      <h4>Tool calls by permission mode</h4>
      {mode_table}
      {fix}
    """
    headline = f"{_fmt_num(friction_events)} friction events, {_fmt_num(real_denials)} real denials, {_fmt_pct(bypass_share)} under bypass"
    return Finding("finding-permission-friction", "Permission friction", headline, b_denials, verdict, body), is_fine


# ---------------------------------------------------------------------------
# fix verdicts + not-your-problem + footer
# ---------------------------------------------------------------------------


def _render_fix_verdicts(diff: dict | None) -> str:
    if not diff:
        return (
            '<section class="fix-verdicts"><h2>Fix verdicts</h2>'
            '<p class="muted">First run — no previous snapshot to check applied fixes against yet. '
            "The next run against this baseline will show whether the 2026-07-17 fixes worked.</p></section>"
        )
    verdicts = diff.get("fix_verdicts") or []
    if not verdicts:
        return ""
    cards = []
    for v in verdicts:
        verdict = v.get("verdict", "unknown")
        cls = {
            "WORKED": "verdict-good",
            "DID NOT WORK": "verdict-bad",
            "BACKFIRED": "verdict-bad",
            "unknown": "verdict-neutral",
        }.get(verdict, "verdict-neutral")
        label = "DID NOT WORK" if verdict == "DID NOT WORK" else verdict
        cards.append(f"""
          <div class="verdict-card {cls}">
            <div class="verdict-badge">{_e(label)}</div>
            <div class="verdict-body">
              <p class="verdict-fix">{_e(v.get("fix"))}</p>
              <p class="verdict-metric"><code>{_e(v.get("metric"))}</code>:
                 {_fmt_num(v.get("old"))} → {_fmt_num(v.get("new"))}
                 {f'({v.get("pct") * 100:+.1f}%)' if v.get("pct") is not None else ""}</p>
              <p class="verdict-why">{_e(v.get("why"))}</p>
              <p class="muted">{_e(v.get("baseline_note"))}</p>
            </div>
          </div>
        """)
    return f'<section class="fix-verdicts"><h2>Fix verdicts</h2>{"".join(cards)}</section>'


def _render_daily_behaviour(snap: dict) -> str:
    """The 'did it work' surface: pre/post-fix share deltas on recent data,
    self-instrumentation excluded, plus a per-day tail. A cumulative six-week
    number cannot show a fix landing; this can."""
    db = _g(snap, "daily_behaviour", default={})
    if not db:
        return ""
    windows = _g(db, "windows", default={})
    applied = db.get("fixes_applied_at")
    postfix = windows.get("postfix") or {}
    postfix_sessions = postfix.get("sessions") or 0

    # Window comparison table.
    win_rows = []
    order = ["prefix", "postfix"] + [k for k in windows if k.startswith("last_")] + ["full"]
    seen = set()
    label_map = {"prefix": f"pre-fix (&lt; {_e(applied)})", "postfix": f"post-fix (&ge; {_e(applied)})", "full": "full corpus"}
    for key in order:
        if key in seen or key not in windows:
            continue
        seen.add(key)
        w = windows[key] or {}
        label = label_map.get(key, key.replace("_", " "))
        win_rows.append([
            label,
            _fmt_num(w.get("sessions")),
            _fmt_pct(w.get("main_thread_share")),
            _fmt_pct(w.get("delegated_share")),
            _fmt_pct(w.get("opus_share")),
            _fmt_pct(w.get("sonnet_share")),
            _fmt_num(w.get("grep_guard_bypasses")),
        ])
    win_table = _table(
        ["window", "sessions", "main-thread", "delegated", "opus", "sonnet", "guard bypasses"],
        win_rows,
    )

    # Pre/post delta rows, coloured by whether the move is good.
    delta_rows = []
    for r in _g(db, "postfix_vs_prefix", default=[]):
        cls = _VERDICT_CLASS.get(r.get("verdict"), "badge-neutral")
        sym = _ARROW_SYM.get(r.get("arrow"), "?")
        delta_rows.append([
            _e(r.get("label")),
            _fmt_pct(r.get("prefix")),
            _fmt_pct(r.get("postfix")),
            f'<span class="badge {cls}">{sym} {r.get("delta", 0) * 100:+.1f}pp</span>',
        ])
    delta_table = _table(
        ["metric", "pre-fix", "post-fix", "change"], delta_rows,
        "No pre/post windows to compare yet.",
    )

    # Recent per-day tail.
    by_day = _g(db, "by_day", default={})
    day_rows = []
    for day, w in sorted(by_day.items(), reverse=True):
        marker = " ⟵ fixes" if applied and day == applied else ""
        day_rows.append([
            f"{_e(day)}{marker}",
            _fmt_num(w.get("sessions")),
            _fmt_pct(w.get("main_thread_share")),
            _fmt_pct(w.get("opus_share")),
            _fmt_num(w.get("grep_guard_bypasses")),
        ])
    day_table = _table(
        ["day", "sessions", "main-thread", "opus", "guard bypasses"], day_rows,
        "No dated sessions.",
    )

    postfix_days = db.get("postfix_days") or 0
    fixday_note = (
        " The fix day itself is excluded from both windows — fixes land mid-day, so it is a "
        "pre/post mix that cannot be attributed cleanly."
        if db.get("fix_day_excluded_from_windows") else ""
    )
    if postfix_days < 7:
        note = (
            f'<p class="muted"><strong>Too early to tell.</strong> Only '
            f"{_fmt_num(postfix_days)} full day(s) of post-fix data "
            f"({_fmt_num(postfix_sessions)} sessions).{fixday_note} The pre/post rows below are "
            "shown for transparency but do not yet support a verdict; they sharpen with every "
            "day of real use.</p>"
        )
    else:
        worse = [r for r in _g(db, "postfix_vs_prefix", default=[]) if r.get("verdict") == "worse"]
        better = [r for r in _g(db, "postfix_vs_prefix", default=[]) if r.get("verdict") == "better"]
        conclusive = "" if postfix_days >= 14 else " (directional until ~14 post-fix days)"
        note = (
            f"<p>{len(better)} share metric(s) improved post-fix, {len(worse)} regressed, across "
            f"{_fmt_num(postfix_days)} post-fix days / {_fmt_num(postfix_sessions)} sessions"
            f"{conclusive}.{fixday_note}</p>"
        )

    return f"""
      <section class="daily-behaviour">
        <h2>Did it work? — post-fix behaviour (self-instrumentation excluded)</h2>
        <p class="muted">Cumulative corpus metrics barely move on a single good day; that is
          arithmetic, not failure. This slices the same corpus by the fix date
          ({_e(applied)}) and drops the {_fmt_num(db.get("self_sessions_excluded"))} sessions where
          this audit was measuring itself.</p>
        {note}
        <h4>Pre-fix vs post-fix (share metrics, directly comparable)</h4>
        {delta_table}
        <h4>Behaviour by window</h4>
        {win_table}
        <h4>Recent days (most recent first)</h4>
        {day_table}
      </section>
    """


def _render_not_your_problem(snap: dict, pf_is_fine: bool, hh_genuine_zero: bool) -> str:
    cards = []
    tb = _g(snap, "token_burn", default={})
    cache_share = tb.get("cache_read_share")
    if cache_share is not None and cache_share >= 0.3:
        cards.append(f"""
          <div class="ok-card">
            <h3>Cache-read share is healthy</h3>
            <p>{_fmt_pct(cache_share)} of all tokens were cache reads, not fresh burn.
               See <a href="#finding-token-burn">Token burn</a> for the full split —
               this is not where the main-thread problem lives.</p>
          </div>
        """)
    if pf_is_fine:
        pf = _g(snap, "permission_friction", default={})
        cards.append(f"""
          <div class="ok-card">
            <h3>Permission friction is not a real problem</h3>
            <p>Zero real denials across the whole corpus ({_fmt_num(pf.get("friction_events"))}
               gated events, {_fmt_pct(pf.get("bypass_share"))} of gated calls under bypass mode).
               See <a href="#finding-permission-friction">Permission friction</a> for the breakdown.
               Do not spend effort tuning the allowlist.</p>
          </div>
        """)
    if hh_genuine_zero:
        hh = _g(snap, "hook_health", default={})
        cards.append(f"""
          <div class="ok-card">
            <h3>The grep-guard is not being circumvented</h3>
            <p>Every one of the {_fmt_num(hh.get("graphify_ok_bypasses"))}
               <code>[graphify-ok]</code> bypasses is a false positive on a non-code path or a
               command that was never recursive in the first place — zero genuine
               circumventions. See <a href="#finding-hook-health">Hook health</a>.</p>
          </div>
        """)
    if not cards:
        return (
            '<section class="not-your-problem"><h2>Not your problem</h2>'
            '<p class="muted">Nothing in this run cleanly resolved to "fine" — every finding above '
            "has at least some real signal. That's not necessarily bad news, it just means there's "
            "no free pass this time.</p></section>"
        )
    return f'<section class="not-your-problem"><h2>Not your problem</h2><div class="ok-grid">{"".join(cards)}</div></section>'


def _render_footer(snap: dict, prev_path: str | None) -> str:
    corpus = _g(snap, "corpus", default={})
    hand = _g(snap, "hand_typed_corpus", default={})
    prev_note = f"<p>Deltas computed against <code>{_e(prev_path)}</code>.</p>" if prev_path else "<p>First run — no previous snapshot, all deltas shown as “new”.</p>"
    return f"""
      <footer>
        <h2>Provenance</h2>
        {prev_note}
        <p>Corpus: {_fmt_num(corpus.get("files"))} transcript files
           ({_fmt_num(corpus.get("main_sessions"))} main + {_fmt_num(corpus.get("subagent_sessions"))} subagent)
           across {_fmt_num(corpus.get("projects"))} projects,
           {_fmt_date(corpus.get("first_event"))} → {_fmt_date(corpus.get("last_event"))}.</p>
        <p>Hand-typed: {_fmt_num(hand.get("messages"))} messages across {_fmt_num(hand.get("sessions"))} sessions
           out of {_fmt_num(hand.get("raw_user_prompt_records"))} raw user-role records
           (automation share {_fmt_pct(hand.get("automation_share"))}).</p>
        <p class="muted">{_fmt_num(corpus.get("bad_lines"))} unparseable lines,
           {_fmt_num(corpus.get("failed_files"))} failed files skipped during the sweep
           (never fatal). Tool version {_e(snap.get("tool_version"))}, schema {_e(snap.get("schema"))}.</p>
      </footer>
    """


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


_STYLE = """
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #6b6b6b;
  --border: #dfe1e4;
  --card-bg: #f7f8fa;
  --code-bg: #eef0f3;
  --good: #1e7e34;
  --good-bg: #e6f4ea;
  --bad: #b3261e;
  --bad-bg: #fce8e6;
  --neutral: #5a5a5a;
  --neutral-bg: #eceef0;
  --new-bg: #e8eefc;
  --new-fg: #2a4d8f;
  --link: #1a56db;
  --accent: #4b3fd6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a;
    --fg: #e7e9ec;
    --muted: #9aa0a8;
    --border: #2c2f36;
    --card-bg: #1c1f24;
    --code-bg: #23262c;
    --good: #4caf50;
    --good-bg: #17301c;
    --bad: #ef5350;
    --bad-bg: #351b1a;
    --neutral: #b9bec6;
    --neutral-bg: #23262c;
    --new-bg: #1b2740;
    --new-fg: #8fb3ff;
    --link: #7ea6ff;
    --accent: #9d90ff;
  }
}
:root[data-theme="dark"] {
  --bg: #14161a; --fg: #e7e9ec; --muted: #9aa0a8; --border: #2c2f36;
  --card-bg: #1c1f24; --code-bg: #23262c; --good: #4caf50; --good-bg: #17301c;
  --bad: #ef5350; --bad-bg: #351b1a; --neutral: #b9bec6; --neutral-bg: #23262c;
  --new-bg: #1b2740; --new-fg: #8fb3ff; --link: #7ea6ff; --accent: #9d90ff;
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b6b6b; --border: #dfe1e4;
  --card-bg: #f7f8fa; --code-bg: #eef0f3; --good: #1e7e34; --good-bg: #e6f4ea;
  --bad: #b3261e; --bad-bg: #fce8e6; --neutral: #5a5a5a; --neutral-bg: #eceef0;
  --new-bg: #e8eefc; --new-fg: #2a4d8f; --link: #1a56db; --accent: #4b3fd6;
}
* { box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0;
  padding: 0 1.25rem 4rem;
  max-width: 1100px;
  margin-inline: auto;
  overflow-x: hidden;
}
h1 { font-size: 1.6rem; margin-top: 1.5rem; }
h2 { font-size: 1.25rem; margin-top: 2.5rem; border-bottom: 1px solid var(--border); padding-bottom: 0.4rem; }
h3 { font-size: 1.05rem; margin-bottom: 0.3rem; }
h4 { font-size: 0.95rem; color: var(--muted); margin: 1rem 0 0.4rem; }
code {
  background: var(--code-bg);
  border-radius: 3px;
  padding: 0.05rem 0.3rem;
  font-size: 0.85em;
  word-break: break-all;
}
pre {
  background: var(--code-bg);
  border-radius: 6px;
  padding: 0.6rem 0.8rem;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.82rem;
  margin: 0.3rem 0;
}
a { color: var(--link); }
.muted { color: var(--muted); font-size: 0.92em; }
.meta-bar {
  color: var(--muted);
  font-size: 0.9rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.6rem 0.9rem;
  margin-bottom: 1rem;
}
.badge {
  display: inline-block;
  border-radius: 999px;
  padding: 0.05rem 0.55rem;
  font-size: 0.8em;
  font-weight: 600;
  white-space: nowrap;
}
.badge-good { background: var(--good-bg); color: var(--good); }
.badge-bad { background: var(--bad-bg); color: var(--bad); }
.badge-neutral { background: var(--neutral-bg); color: var(--neutral); }
.badge-new { background: var(--new-bg); color: var(--new-fg); }
.flag-bad {
  background: var(--bad-bg); color: var(--bad); border-radius: 4px;
  font-size: 0.72em; font-weight: 700; padding: 0.05rem 0.35rem; margin-left: 0.3rem;
}
.table-wrap { overflow-x: auto; margin: 0.5rem 0 1rem; border: 1px solid var(--border); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
th, td { padding: 0.4rem 0.6rem; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--card-bg); }
td .cause, td code { white-space: normal; }
.overview-wrap { overflow-x: auto; }
.overview { border: 1px solid var(--border); border-radius: 8px; }
.overview th, .overview td { white-space: normal; }
.overview .rank { color: var(--muted); font-variant-numeric: tabular-nums; }
.overview .verdict-dot { display: inline-block; width: 0.6rem; height: 0.6rem; border-radius: 50%; margin-right: 0.4rem; }
.dot-good { background: var(--good); }
.dot-bad { background: var(--bad); }
.dot-neutral { background: var(--neutral); }
details.finding {
  border: 1px solid var(--border);
  border-radius: 10px;
  margin: 0.8rem 0;
  padding: 0.2rem 1rem 0.9rem;
  background: var(--card-bg);
  scroll-margin-top: 1rem;
}
details.finding[open] summary { margin-bottom: 0.5rem; }
details.finding summary {
  cursor: pointer;
  padding: 0.7rem 0;
  font-weight: 600;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 0.6rem;
}
details.finding summary::-webkit-details-marker { display: none; }
details.finding summary::before { content: "\\25B8"; color: var(--muted); transition: transform 0.15s; }
details.finding[open] summary::before { transform: rotate(90deg); }
details.finding.verdict-bad { border-left: 4px solid var(--bad); }
details.finding.verdict-good { border-left: 4px solid var(--good); }
details.finding.verdict-neutral { border-left: 4px solid var(--neutral); }
details.nested { margin: 0.4rem 0; border: 1px solid var(--border); border-radius: 6px; padding: 0.3rem 0.7rem; }
details.nested summary { cursor: pointer; font-size: 0.88rem; color: var(--muted); }
.example { margin: 0.3rem 0; padding-left: 0.6rem; border-left: 2px solid var(--border); }
.ex-sid { font-family: monospace; font-size: 0.78rem; color: var(--muted); }
.stat-list { padding-left: 1.2rem; }
.stat-list li { margin: 0.2rem 0; }
.ok-grid { display: grid; gap: 0.8rem; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.ok-card {
  background: var(--good-bg);
  border: 1px solid var(--good);
  border-radius: 8px;
  padding: 0.7rem 0.9rem;
  opacity: 0.92;
}
.ok-card h3 { color: var(--good); margin-top: 0; font-size: 0.95rem; }
.verdict-card {
  display: flex;
  gap: 0.8rem;
  border-radius: 8px;
  padding: 0.7rem 0.9rem;
  margin: 0.6rem 0;
  border: 1px solid var(--border);
}
.verdict-good { background: var(--good-bg); border-color: var(--good); }
.verdict-bad { background: var(--bad-bg); border-color: var(--bad); }
.verdict-neutral { background: var(--neutral-bg); border-color: var(--border); }
.verdict-badge {
  flex: 0 0 auto;
  font-weight: 800;
  font-size: 0.85rem;
  align-self: flex-start;
  padding: 0.2rem 0.6rem;
  border-radius: 6px;
}
.verdict-good .verdict-badge { background: var(--good); color: #fff; }
.verdict-bad .verdict-badge { background: var(--bad); color: #fff; }
.verdict-neutral .verdict-badge { background: var(--neutral); color: #fff; }
.verdict-fix { font-weight: 600; margin: 0 0 0.2rem; }
.verdict-metric, .verdict-why { margin: 0.1rem 0; font-size: 0.9rem; }
footer { margin-top: 2.5rem; color: var(--muted); font-size: 0.88rem; }
footer p { margin: 0.25rem 0; }
"""


def render(snap: dict, prev: dict | None, diff: dict | None, prev_path: str | None = None) -> str:
    """Return a complete self-contained HTML document as a string."""
    snap = snap or {}
    diff_idx = {r.get("path"): r for r in (diff or {}).get("rows", []) if isinstance(r, dict)}

    generated_at = snap.get("generated_at") or "?"
    corpus = _g(snap, "corpus", default={})
    date_range = f"{_fmt_date(corpus.get('first_event'))} → {_fmt_date(corpus.get('last_event'))}"

    pf_finding, pf_is_fine = _finding_permission_friction(snap, prev, diff_idx)
    hh_finding = _finding_hook_health(snap, prev, diff_idx)
    hh_genuine_zero = _g(snap, "hook_health", "bypass_classification", "genuine-circumvention", "count", default=0) == 0

    findings = [
        _finding_token_burn(snap, prev, diff_idx),
        _finding_model_mix(snap, prev, diff_idx),
        _finding_redelegation(snap, prev, diff_idx),
        _finding_repeated_context(snap, prev, diff_idx),
        _finding_skill_gaps(snap, prev, diff_idx),
        hh_finding,
        _finding_reread_files(snap, prev, diff_idx),
        pf_finding,
    ]

    overview_rows = []
    for i, f in enumerate(findings, 1):
        dot = {"good": "dot-good", "bad": "dot-bad", "neutral": "dot-neutral"}.get(f.verdict_class, "dot-neutral")
        overview_rows.append(f"""
          <tr>
            <td class="rank">{i}</td>
            <td><span class="verdict-dot {dot}"></span>
                <a href="#{f.fid}">{_e(f.title)}</a></td>
            <td>{_e(f.headline)}</td>
            <td>{f.badge_html}</td>
          </tr>
        """)
    overview_table = (
        '<div class="overview-wrap"><table class="overview">'
        "<thead><tr><th>#</th><th>finding</th><th>headline</th><th>vs last run</th></tr></thead>"
        f"<tbody>{''.join(overview_rows)}</tbody></table></div>"
    )

    details_html = "".join(
        f'<details class="finding verdict-{f.verdict_class}" id="{f.fid}">'
        f"<summary>{_e(f.title)} — {_e(f.headline)} {f.badge_html}</summary>"
        f"{f.body_html}</details>"
        for f in findings
    )

    fix_verdicts_html = _render_fix_verdicts(diff)
    daily_behaviour_html = _render_daily_behaviour(snap)
    not_your_problem_html = _render_not_your_problem(snap, pf_is_fine, hh_genuine_zero)
    footer_html = _render_footer(snap, prev_path)

    title = "Claude Friction Audit"
    return f"""<title>{_e(title)}</title>
<style>{_STYLE}</style>
<h1>{_e(title)}</h1>
<div class="meta-bar">
  Generated {_e(generated_at)} &middot; corpus {_e(date_range)} &middot;
  {_fmt_num(corpus.get("files"))} transcript files &middot; tool v{_e(snap.get("tool_version"))}
  {f'<br>Deltas vs previous snapshot ({_e(_g(diff, "old_generated_at", default="?"))})'
    + (f' at <code>{_e(prev_path)}</code>' if prev_path else '') if diff else '<br>First run — no previous snapshot, everything below is a baseline.'}
</div>

{fix_verdicts_html}

{daily_behaviour_html}

<h2>Findings, ranked by time actually lost</h2>
{overview_table}
{details_html}

{not_your_problem_html}

{footer_html}
"""
