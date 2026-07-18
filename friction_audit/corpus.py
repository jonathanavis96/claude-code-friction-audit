"""Corpus discovery.

Layout of ~/.claude/projects/:

    <project-slug>/<session-uuid>.jsonl                       main session
    <project-slug>/<session-uuid>/subagents/agent-<id>.jsonl  subagent
    <project-slug>/<session-uuid>/subagents/agent-<id>.meta.json

The meta sidecar is named ``agent-<id>.meta.json`` -- there is no bare
``meta.json`` anywhere in the corpus.  It carries::

    {"agentType": "Explore", "description": "...",
     "toolUseId": "toolu_...", "spawnDepth": 1}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path("~/.claude/projects").expanduser()


@dataclass
class Transcript:
    """One .jsonl file plus what we know about it from its path."""

    path: Path
    project: str
    session_id: str
    is_subagent: bool
    #: For subagents only: parent main-session uuid (the grandparent dir name).
    parent_session_id: str | None = None
    #: For subagents only: parsed agent-<id>.meta.json contents.
    meta: dict = field(default_factory=dict)

    @property
    def agent_type(self) -> str | None:
        """Declared agent type from the meta sidecar (``Explore``, ``worker``...)."""
        return self.meta.get("agentType")

    @property
    def spawn_depth(self) -> int:
        return int(self.meta.get("spawnDepth") or 0)


def _read_meta(jsonl_path: Path) -> dict:
    meta_path = jsonl_path.with_suffix(".meta.json")
    try:
        with open(meta_path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A missing or corrupt sidecar must never kill the run.
        return {}


def discover(root: Path | str = DEFAULT_ROOT) -> list[Transcript]:
    """Find every transcript under *root*. Never raises on odd directories."""
    root = Path(root).expanduser()
    out: list[Transcript] = []
    if not root.is_dir():
        return out

    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        project = project_dir.name

        for jsonl in sorted(project_dir.glob("*.jsonl")):
            out.append(
                Transcript(
                    path=jsonl,
                    project=project,
                    session_id=jsonl.stem,
                    is_subagent=False,
                )
            )

        for sub in sorted(project_dir.glob("*/subagents/*.jsonl")):
            parent = sub.parent.parent.name
            out.append(
                Transcript(
                    path=sub,
                    project=project,
                    session_id=sub.stem,
                    is_subagent=True,
                    parent_session_id=parent,
                    meta=_read_meta(sub),
                )
            )

    return out


def corpus_summary(transcripts: list[Transcript], root: Path | str = DEFAULT_ROOT) -> dict:
    projects = {t.project for t in transcripts}
    root = Path(root).expanduser()
    try:
        project_dirs = sum(1 for p in root.iterdir() if p.is_dir())
    except OSError:
        project_dirs = len(projects)
    return {
        "files": len(transcripts),
        "main_sessions": sum(1 for t in transcripts if not t.is_subagent),
        "subagent_sessions": sum(1 for t in transcripts if t.is_subagent),
        # Projects that actually contain transcripts. `project_dirs` counts
        # every directory under the root, including empty ones -- that is the
        # number the 2026-07-17 baseline quoted as "52 project dirs".
        "projects": len(projects),
        "project_dirs": project_dirs,
    }
