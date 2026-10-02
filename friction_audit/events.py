"""Streaming transcript parsing, usage dedup, and the automation filter.

Everything here is written against verified corpus behaviour.  The three
things that silently fabricate numbers if you get them wrong:

1.  USAGE DEDUP.  Each assistant API turn is written to the transcript once
    per content block (thinking / text / tool_use) with ``message.id`` and the
    ``usage`` object repeated verbatim.  Naive summing overcounts 2-3x.  We
    dedup on ``message.id``; verified zero collisions across files, so a global
    seen-set is safe and also survives resumed/forked sessions.

2.  THE AUTOMATION FILTER.  ~65% of ``type=="user"`` records are not the human.
    See :func:`classify_user_record`.

3.  PERMISSION MODE PROPAGATION.  ``permissionMode`` lives on only ~69% of
    prompt records and on dedicated ``type=="permission-mode"`` records.  It is
    NEVER on assistant records, so a tool call's mode has to be carried forward
    along the session timeline.  Subagent transcripts carry it nowhere at all.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# The literal denial marker. Anything else is guesswork.
DENIAL_MARKER = "The user doesn't want to proceed with this tool use"

# The graphify grep-guard hard block, delivered as an errored tool_result.
BLOCK_MARKER = "BLOCKED: recursive"

# Escape hatch comment that bypasses the grep-guard.
GRAPHIFY_OK = "[graphify-ok]"

#: Tool names that never prompt for permission (built-in read-only auto-allow).
#: Counting these in "permission friction" inflates the denominator with calls
#: that could not possibly have prompted.
AUTO_ALLOWED_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "NotebookRead",
        "TodoWrite",
        "Task",
        "Agent",
        "Skill",
        "WebFetch",
        "WebSearch",
        "BashOutput",
        "KillShell",
        "ExitPlanMode",
        "EnterPlanMode",
    }
)

def prompts_for_permission(tool_name: str) -> bool:
    """True if *tool_name* is in the population that can actually prompt.

    Scope is Bash + every MCP tool, matching the 2026-07-17 baseline so the
    deltas stay comparable.  Write/Edit also gate, but they were outside the
    baseline population; counting them here would silently inflate the
    denominator from 16,807 to ~23,000 and make the bypass share look better
    than the baseline's without anything actually changing.  They are reported
    separately as ``edit_tool_calls``.
    """
    if not tool_name:
        return False
    if tool_name.startswith("mcp__"):
        return True
    return tool_name == "Bash"


EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def model_family(model: str | None) -> str | None:
    """Map a raw model id onto a tier name.

    Returns None for ``<synthetic>`` and unknown values so they can be excluded
    from the model mix rather than silently bucketed as one of the tiers.
    """
    if not model or model.startswith("<"):
        return None
    m = model.lower()
    for fam in ("opus", "sonnet", "fable", "haiku"):
        if f"-{fam}-" in m or m.startswith(f"claude-{fam}"):
            return fam
    return None


@dataclass
class Usage:
    """Deduplicated token usage for one assistant API turn."""

    cache_read: int = 0
    cache_create: int = 0
    fresh: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.cache_read + self.cache_create + self.fresh + self.output

    def add(self, other: "Usage") -> None:
        self.cache_read += other.cache_read
        self.cache_create += other.cache_create
        self.fresh += other.fresh
        self.output += other.output

    def as_dict(self) -> dict:
        return {
            "cache_read": self.cache_read,
            "cache_create": self.cache_create,
            "fresh": self.fresh,
            "output": self.output,
            "total": self.total,
        }


def usage_from_message(message: dict) -> Usage | None:
    u = message.get("usage")
    if not isinstance(u, dict):
        return None
    return Usage(
        cache_read=_tokens(u.get("cache_read_input_tokens")),
        cache_create=_tokens(u.get("cache_creation_input_tokens")),
        fresh=_tokens(u.get("input_tokens")),
        output=_tokens(u.get("output_tokens")),
    )


def _tokens(value) -> int:
    """A token count, or 0 for anything that is not a number.

    int("n/a") or int({...}) used to raise, and one bad usage field then
    discarded the whole transcript at the sweep level.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # json.loads accepts NaN/Infinity, and int() of those raises.
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


# --------------------------------------------------------------------------
# The automation filter
# --------------------------------------------------------------------------

#: Content prefixes that mark a synthetic/system-injected "user" message.
_SYNTHETIC_PREFIXES = (
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<task-notification>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
)

#: Content markers for automation that is logged with origin.kind=="human".
#:
#: The cache-guard hook REWRITES the prompt in place before Claude sees it, so
#: its rejection notice inherits the human origin of whatever it interrupted --
#: including task-notification returns it wrapped ("Original prompt:
#: <task-notification>").  Trusting origin.kind alone lets these through and
#: they then dominate the repeated-context clustering with phrases like
#: "cache-guard-bypass included once", which the user never typed.
_AUTOMATION_CONTENT = (
    "UserPromptSubmit operation blocked by hook",
    "Cache guard blocked this prompt",
    "/.claude/cache-guard/handoffs/",
    "[cache-guard-bypass]",
    # A task-notification return, wrapped by the cache-guard or otherwise, is
    # the harness talking to itself -- never something the user typed.
    "<task-notification>",
    "<task-id>",
)


def message_text(message: dict) -> str:
    """Flatten a message's content to plain text."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", None):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def classify_user_record(event: dict) -> str:
    """Classify a ``type=="user"`` record.

    Returns one of:

    ``tool_result``  -- a tool result being fed back, not a message at all.
    ``human``        -- genuinely hand-typed by the user.
    ``automation``   -- cron ``claude -p``, sdk-py, task-notification,
                        cache-guard resubmission, slash-command stubs, meta.

    Verified field semantics:

    * ``origin.kind == "human"``           -> hand-typed (current CC versions).
    * ``origin`` absent + ``promptSource`` in {typed, queued} -> hand-typed on
      OLDER CC versions that predate the ``origin`` field.  The 2026-07-17
      one-off audit missed these ~157 messages.
    * ``origin`` absent + ``promptSource`` absent -> slash-command stubs
      (``/clear``, ``/login``) and command stdout.  Automation.
    * ``entrypoint`` in {sdk-py, sdk-cli} -> automation regardless.
    * ``isMeta`` -> automation.
    """
    if "toolUseResult" in event:
        return "tool_result"
    if event.get("isMeta"):
        return "automation"

    entrypoint = event.get("entrypoint")
    if entrypoint not in ("cli", None):
        return "automation"  # sdk-py / sdk-cli

    origin = event.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    prompt_source = event.get("promptSource")

    text = message_text(event.get("message") or {}).lstrip()
    if text.startswith(_SYNTHETIC_PREFIXES):
        return "automation"
    if any(marker in text for marker in _AUTOMATION_CONTENT):
        return "automation"

    if kind == "human":
        return "human"
    if kind is not None:
        return "automation"  # task-notification etc.

    # No origin field: older transcript versions.
    if prompt_source in ("typed", "queued"):
        return "human"
    return "automation"


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


#: Only these input keys are retained. A full sweep holds ~100k tool calls in
#: memory; keeping whole input dicts (Bash commands, Write file bodies, agent
#: prompts) costs GBs and OOMs WSL. Everything the metrics need is here.
_KEPT_INPUT_KEYS = ("command", "file_path", "skill", "subagent_type", "pattern")
_MAX_INPUT_VALUE = 600


def slim_input(raw: dict) -> dict:
    out = {}
    for k in _KEPT_INPUT_KEYS:
        v = raw.get(k)
        if isinstance(v, str):
            out[k] = v[:_MAX_INPUT_VALUE]
    return out


@dataclass(slots=True)
class ToolCall:
    name: str
    input: dict
    permission_mode: str | None
    tool_use_id: str | None
    model: str | None
    #: The call's OWN timestamp. Bucketing by session start instead silently
    #: mis-attributes every call in a long session that crosses a month
    #: boundary -- which is most of the expensive ones.
    timestamp: str | None = None


@dataclass
class ParsedTranscript:
    """Everything one pass over a transcript yields."""

    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    total_usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    human_messages: list[str] = field(default_factory=list)
    user_class_counts: dict[str, int] = field(default_factory=dict)
    denials: list[dict] = field(default_factory=list)
    blocks: int = 0
    read_paths: list[str] = field(default_factory=list)
    skills_fired: list[str] = field(default_factory=list)
    slash_commands: list[str] = field(default_factory=list)
    agent_dispatches: int = 0
    bad_lines: int = 0
    timestamps: list[str] = field(default_factory=list)


def iter_events(path: Path) -> Iterator[tuple[dict | None, bool]]:
    """Yield (event, ok). A malformed line yields (None, False), never raises."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    yield None, False
                    continue
                if not isinstance(event, dict):
                    yield None, False
                    continue
                yield event, True
    except OSError:
        return


def parse_transcript(path: Path, seen_message_ids: set[str]) -> ParsedTranscript:
    """Single streaming pass over one transcript.

    *seen_message_ids* is shared across the whole run and is what makes the
    usage dedup correct -- pass the same set to every call.
    """
    out = ParsedTranscript()
    current_mode: str | None = None

    for event, ok in iter_events(path):
        if not ok or event is None:
            out.bad_lines += 1
            continue
        # A truncated or foreign record can carry a non-object "message".
        # Every handler below calls .get() on it, and one AttributeError
        # discards the whole transcript (and its tokens) at the sweep level.
        if "message" in event and not isinstance(event["message"], (dict, type(None))):
            out.bad_lines += 1
            continue

        etype = event.get("type")

        # Forward-propagate permission mode along the session timeline.
        if etype == "permission-mode":
            current_mode = event.get("permissionMode") or current_mode
            continue
        if event.get("permissionMode"):
            current_mode = event["permissionMode"]

        ts = event.get("timestamp")
        if isinstance(ts, str):
            out.timestamps.append(ts)

        if etype == "user":
            _handle_user(event, out, current_mode)
            continue

        if etype != "assistant":
            continue

        message = event.get("message") or {}
        model = message.get("model")
        mid = message.get("id")

        # --- usage dedup: count each API turn exactly once -----------------
        if mid and mid not in seen_message_ids:
            seen_message_ids.add(mid)
            usage = usage_from_message(message)
            if usage:
                out.total_usage.add(usage)
                bucket = out.usage_by_model.setdefault(model or "<unknown>", Usage())
                bucket.add(usage)

        # --- tool calls: every block counts, no dedup ----------------------
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            raw_input = block.get("input")
            binput: dict = raw_input if isinstance(raw_input, dict) else {}
            out.tool_calls.append(
                ToolCall(
                    name=name,
                    input=slim_input(binput),
                    permission_mode=current_mode,
                    tool_use_id=block.get("id"),
                    model=model,
                    timestamp=ts if isinstance(ts, str) else None,
                )
            )
            if name == "Read":
                fp = binput.get("file_path")
                if isinstance(fp, str):
                    out.read_paths.append(fp)
            elif name == "Skill":
                sk = binput.get("skill")
                if isinstance(sk, str):
                    out.skills_fired.append(sk)
            elif name == "Agent":
                out.agent_dispatches += 1

    return out


def _handle_user(event: dict, out: ParsedTranscript, current_mode: str | None) -> None:
    kind = classify_user_record(event)
    out.user_class_counts[kind] = out.user_class_counts.get(kind, 0) + 1

    message = event.get("message") or {}
    text = message_text(message)

    if kind == "human":
        out.human_messages.append(text)
        return

    stripped = text.lstrip()
    if stripped.startswith("<command-name>"):
        end = stripped.find("</command-name>")
        if end > 0:
            out.slash_commands.append(stripped[len("<command-name>") : end].strip())

    # Denials and hook blocks arrive as errored tool_result blocks.
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if not isinstance(body, str):
            body = json.dumps(body) if body is not None else ""
        if DENIAL_MARKER in body:
            # Denial records never carry permissionMode themselves; the
            # propagated session mode is the only way to tell a real "no" from
            # the user interrupting a hung job under bypassPermissions.
            out.denials.append(
                {
                    "tool_use_id": block.get("tool_use_id"),
                    "permission_mode": current_mode,
                    "interrupt": current_mode == "bypassPermissions",
                }
            )
        if BLOCK_MARKER in body:
            out.blocks += 1
