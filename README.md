# Claude Code Friction Audit

> A repeatable, evidence-backed audit of where your [Claude Code](https://claude.com/claude-code) sessions burn tokens and time — run locally over your own transcripts, no data leaves your machine.

Claude Code writes a JSONL transcript for every session under `~/.claude/projects/`.
Over a few weeks that is gigabytes of ground truth about how you *actually* work:
which model ran, what got delegated to a subagent, which files were re-read a
dozen times, where permission prompts and hook bypasses happened. This tool
sweeps all of it in one streaming pass and turns it into numbers you can act on
— then diffs two runs so you can tell whether a change to your setup **actually
moved the needle** instead of guessing.

- **100% local.** Reads files already on your disk. Nothing is uploaded.
- **Zero dependencies.** Python 3.10+ standard library only.
- **Repeatable.** Snapshot → change something → snapshot again → diff.

## What it measures

| Finding | Question it answers |
|---|---|
| **Token burn / main-thread share** | How much of your token spend never left the expensive main model thread vs. was delegated to cheaper subagents? |
| **Model mix / agent-type leak** | Did a *cheap* agent type (`Explore`, `general-purpose`, …) silently run on an *expensive* model because it inherited it? |
| **Sub-agent re-delegation** | Which subagents spawned further subagents (multiplied cost)? |
| **Most re-read files** | Which big files get read many times — candidates for splitting so a re-read doesn't re-spend the whole file's context? |
| **Permission friction** | Real denials vs. you just interrupting a hung job under bypass mode. |
| **Repeated context / failing rules** | Phrases you re-type across sessions even though a `CLAUDE.md` / memory rule already covers them. |
| **Skill invocation gaps** | You asked for a skill by its trigger phrase and it didn't fire. |
| **Hook health** | A companion grep-guard hook's bypass-to-block ratio, cross-checked against the guard's own accepted-bypass log. |
| **Daily / post-fix window** | Per-day and pre/post-fix slices (with the audit's own sessions excluded) so a change made yesterday is judged on *recent* behaviour, not diluted into a six-week average. |

Findings are **ranked by time/tokens lost**, each with real evidence (session
ids, counts, example strings) and a concrete, paste-ready fix.

## Install & run

```bash
git clone https://github.com/jonathanavis96/claude-code-friction-audit
cd claude-code-friction-audit
./audit.sh
```

The first run writes `out/snapshot-YYYY-MM-DD.json` and a self-contained
`out/report-*.html` (light/dark, opens in any browser, no external assets). Run
it again later and it **auto-diffs against the previous snapshot**, colouring
each moved number by whether the move was good or bad.

```bash
./audit.sh                                              # sweep + snapshot + HTML report
python3 -m friction_audit.cli --help                    # all flags
python3 -m friction_audit.cli --compare A.json B.json   # diff two snapshots and exit
```

Flags: `--root` (transcripts root), `--out-dir`, `--previous` (explicit baseline),
`--no-report`, `--quiet`, `--version`. It never mutates your transcripts and is
safe to run unattended from cron.

## Reading the report

- **Fix verdicts** — if you track a specific change you made, it reads
  `WORKED` / `DID NOT WORK` / `BACKFIRED`, and honestly refuses with
  `TOO EARLY TO TELL` until there is enough post-fix data to judge.
- **"Did it work?"** — post-fix vs pre-fix *share* metrics over recent days,
  with the fix day (a pre/post mix) and the audit's own sessions excluded, so a
  single good day is visible instead of being averaged into oblivion.
- **"Not your problem"** — things that came back clean, called out explicitly so
  you don't waste effort optimising a non-issue.

## Make it yours

Two lists turn this from *an* audit into *your* audit:

- `friction_audit/metrics.py` → `_TRACKED_PROBES` — the phrases you want to know
  you keep re-typing, and which rule should have prevented each.
- `friction_audit/compare.py` → `FIX_CHECKS` / `FIXES_APPLIED_AT` — the specific
  changes you made and the metric each one should move.

The `hook_health` and repeated-rule metrics assume the conventions this was built
alongside — a grep-guard hook that logs `[graphify-ok]` bypasses, and a
`CLAUDE.md` + per-project `memory/` layout. Without those, those sections simply
read empty rather than erroring; everything else works out of the box.

## Transcript-format gotchas (hard-won)

If you write your own Claude Code transcript tooling, these are the traps that
silently fabricate numbers. The code documents each inline:

- **Usage double-counting.** Each assistant turn is logged once *per content
  block* with the same `message.id` and identical `usage`. Naive summing
  overcounts 2–3×. Dedupe by message id.
- **Most `type=="user"` records are automation, not you** — cron, SDK,
  task-notifications, hook resubmissions, slash-command stubs. Filter by
  `origin`/`promptSource` plus content markers, or your "how much did I actually
  type" number is fiction.
- **`permissionMode` is never on assistant records** and only on some prompts —
  forward-propagate it. Subagent transcripts don't carry it at all.
- **A denial under `bypassPermissions`** is you interrupting a hung job, not a "no".

## Testing

```bash
python3 -m pytest tests/
```

Pure stdlib, ~44 tests, no network, safe to run unattended.

## License

[MIT](LICENSE).
