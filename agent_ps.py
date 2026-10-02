#!/usr/bin/env python3
"""List running coding agents (Claude Code, Codex) and their working directories.

Detection is process-based (ps plus /proc on Linux, lsof elsewhere). Claude rows are
enriched from ~/.claude/sessions/<pid>.json when present (name, status, tmux pane).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LINUX = sys.platform.startswith("linux")
HOME = Path.home()
SHELLS = frozenset({"bash", "sh", "zsh", "fish"})
INTERPRETERS = frozenset({"node", "bun"})
AGENT_KINDS = ("claude", "codex")
CLAUDE_NPM_RE = re.compile(r"@anthropic-ai/claude-code/cli\.js")
CODEX_NPM_RE = re.compile(r"@openai/codex")
ETIME_RE = re.compile(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")
CLAUDE_SESSION_FIELDS = ("name", "status", "sessionId", "entrypoint", "version")


@dataclass
class Proc:
    pid: int
    ppid: int
    user: str
    tty: str
    etime: str
    comm: str
    argv: list[str]
    exe: str = ""


@dataclass
class Agent:
    kind: str
    pid: int
    ppid: int
    user: str
    tty: str
    age_s: int | None
    cwd: str | None
    args: str
    child: bool
    tmux: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "extra"}
        d.update(self.extra)
        return d


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def read_cmdline(pid: int) -> list[str] | None:
    """NUL-separated argv from /proc, so paths with spaces survive. Linux only."""
    if not LINUX:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a] or None


def read_exe(pid: int) -> str:
    if not LINUX:
        return ""
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def list_processes() -> list[Proc]:
    out = run(["ps", "-eo", "pid=,ppid=,user=,tty=,etime=,comm=,args="])
    procs: list[Proc] = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, user, tty, etime, comm, args = parts
        procs.append(
            Proc(
                pid=int(pid),
                ppid=int(ppid),
                user=user,
                tty=tty,
                etime=etime,
                comm=comm,
                argv=read_cmdline(int(pid)) or args.split(),
                exe=read_exe(int(pid)),
            )
        )
    return procs


def proc_cwd(pid: int) -> str | None:
    if LINUX:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    for line in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


@dataclass(frozen=True)
class TmuxPane:
    target: str
    tty: str
    pid: int


def list_tmux_panes() -> list[TmuxPane]:
    """Panes across all tmux sessions; empty if tmux is absent or no server is running."""
    try:
        out = run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t#{pane_tty}\t#{pane_pid}",
            ]
        )
    except FileNotFoundError:
        return []
    panes: list[TmuxPane] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].isdigit():
            panes.append(TmuxPane(target=parts[0], tty=parts[1], pid=int(parts[2])))
    return panes


def tmux_target(p: Proc, panes: list[TmuxPane], ppid_of: dict[int, int]) -> str | None:
    """Pane holding this process, matched by tty, else by ancestry to the pane's shell."""
    tty = p.tty if p.tty.startswith("/dev/") else f"/dev/{p.tty}"
    by_tty = {pane.tty: pane.target for pane in panes}
    if tty in by_tty:
        return by_tty[tty]
    by_pid = {pane.pid: pane.target for pane in panes}
    pid, hops = p.pid, 0
    while pid in ppid_of and hops < 64:
        if pid in by_pid:
            return by_pid[pid]
        pid, hops = ppid_of[pid], hops + 1
    return None


def base(path: str) -> str:
    return os.path.basename(path.rstrip("/")) if path else ""


def classify(p: Proc) -> str | None:
    """Return "claude", "codex", or None for an unrelated process.

    argv is authoritative: the Claude binary re-execs itself as helpers such as ugrep,
    so exe and comm alone would misreport those as agents.
    """
    argv0 = base(p.argv[0]) if p.argv else ""
    script = base(p.argv[1]) if len(p.argv) > 1 else ""
    # Only the executable, plus the script when run via an interpreter, identifies the
    # program; arguments may mention an agent's package path (e.g. `rg @openai/codex`).
    program = " ".join(p.argv[:2] if argv0 in INTERPRETERS else p.argv[:1])

    # Shells spawned by agents reference agent paths in their snapshots; never count them.
    if p.comm in SHELLS or argv0 in SHELLS:
        return None
    if not p.argv:
        return next((k for k in AGENT_KINDS if k in (p.comm, base(p.exe))), None)
    if (
        argv0 == "claude"
        or CLAUDE_NPM_RE.search(program)
        or (argv0 in INTERPRETERS and script == "claude")
    ):
        return "claude"
    if (
        argv0 == "codex"
        or CODEX_NPM_RE.search(program)
        or (argv0 in INTERPRETERS and script == "codex")
    ):
        return "codex"
    return None


def agent_args(kind: str, argv: list[str]) -> str:
    """Arguments passed to the agent, minus interpreter and program path."""
    rest = list(argv)
    if rest and base(rest[0]) in INTERPRETERS:
        rest = rest[1:]
    if rest and (base(rest[0]) in AGENT_KINDS or kind in rest[0]):
        rest = rest[1:]
    return " ".join(rest)


def claude_session_info(pid: int) -> dict[str, Any]:
    path = HOME / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"warning: unreadable {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"warning: {path} is not a JSON object", file=sys.stderr)
        return {}
    if data.get("pid") not in (None, pid):
        return {}
    info = {k: data[k] for k in CLAUDE_SESSION_FIELDS if data.get(k) is not None}
    if data.get("kind") is not None:
        info["session_kind"] = data["kind"]
    if data.get("tmux") is not None:
        info["claude_tmux_id"] = data["tmux"]
    return info


def etime_seconds(etime: str) -> int | None:
    """Parse ps etime ([[dd-]hh:]mm:ss) into seconds."""
    m = ETIME_RE.match(etime.strip())
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def human_age(sec: int | None) -> str:
    if sec is None:
        return "?"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    return f"{sec // 86400}d{(sec % 86400) // 3600:02d}h"


def collect(
    procs: list[Proc],
    *,
    show_tree: bool = False,
    kinds: set[str] | None = None,
    cwd_of=proc_cwd,
    session_info=claude_session_info,
    panes: list[TmuxPane] | None = None,
) -> list[Agent]:
    """Classify processes into agents, collapsing wrapper/child chains of one kind."""
    panes = panes if panes is not None else list_tmux_panes()
    ppid_of = {p.pid: p.ppid for p in procs}
    kind_of: dict[int, str] = {}
    for p in procs:
        kind = classify(p)
        if kind and (not kinds or kind in kinds):
            kind_of[p.pid] = kind
    candidates = [p for p in procs if p.pid in kind_of]

    def same_kind_children(parent: Proc) -> list[Proc]:
        return [
            c for c in candidates if c.ppid == parent.pid and kind_of[c.pid] == kind_of[parent.pid]
        ]

    agents: list[Agent] = []
    for p in candidates:
        kind = kind_of[p.pid]
        is_child = kind_of.get(p.ppid) == kind
        if is_child and not show_tree:
            continue
        cwd = cwd_of(p.pid)
        if cwd is None and not show_tree:
            cwd = next((c for c in map(lambda k: cwd_of(k.pid), same_kind_children(p)) if c), None)
        agent = Agent(
            kind=kind,
            pid=p.pid,
            ppid=p.ppid,
            user=p.user,
            tty=p.tty,
            age_s=etime_seconds(p.etime),
            cwd=cwd,
            args=agent_args(kind, p.argv),
            child=is_child,
            tmux=tmux_target(p, panes, ppid_of),
        )
        if kind == "claude":
            agent.extra.update(session_info(p.pid))
        if not show_tree:
            kids = [c.pid for c in same_kind_children(p)]
            if kids:
                agent.extra["child_pids"] = kids
        agents.append(agent)
    agents.sort(key=lambda a: (a.kind, a.cwd or "", a.pid))
    return agents


def tilde(path: str | None) -> str:
    if path is None:
        return "?"
    home = str(HOME)
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def format_table(agents: list[Agent]) -> str:
    header = ["AGENT", "PID", "STATUS", "AGE", "TTY", "TMUX", "CWD", "INFO"]
    rows: list[list[str]] = []
    for a in agents:
        info: list[str] = []
        if a.extra.get("name"):
            info.append(str(a.extra["name"]))
        if a.args:
            info.append(a.args)
        if a.child:
            info.append(f"(child of {a.ppid})")
        status = str(a.extra.get("status", "-"))
        rows.append(
            [
                a.kind,
                str(a.pid),
                status,
                human_age(a.age_s),
                a.tty,
                a.tmux or "-",
                tilde(a.cwd),
                " ".join(info),
            ]
        )
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header) - 1)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    return "\n".join(fmt.format(*r).rstrip() for r in [header, *rows])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="List running coding agents (Claude Code, Codex) and their cwd."
    )
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument(
        "--kind", action="append", choices=AGENT_KINDS, help="filter by agent kind (repeatable)"
    )
    ap.add_argument(
        "--tree", action="store_true", help="show wrapper/child processes instead of collapsing"
    )
    ap.add_argument("--cwd-only", action="store_true", help="print unique working directories only")
    args = ap.parse_args(argv)

    agents = collect(list_processes(), show_tree=args.tree, kinds=set(args.kind or ()))

    if args.json:
        print(json.dumps([a.to_dict() for a in agents], indent=2))
        return 0
    if args.cwd_only:
        print("\n".join(dict.fromkeys(a.cwd for a in agents if a.cwd)))
        return 0
    if not agents:
        print("no running agents found", file=sys.stderr)
        return 1
    print(format_table(agents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
