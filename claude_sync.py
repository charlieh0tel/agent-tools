#!/usr/bin/env python3
"""Two-way sync of Claude Code sessions between machines over ssh + rsync.

The same file runs on both ends: locally as the CLI, and on the remote host as
`ssh host python3 - --remote ...` with this script fed over stdin, so nothing
has to be installed on the other machine.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CACHE = Path.home() / ".cache" / "claude-sync" / "hash-cache.json"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SYNC_DIR", Path.home() / ".claude"))


def encode(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ---------------------------------------------------------------- file kinds

def kind_of(rel: str) -> str:
    """append: only ever grows at the end; once: written once; mutable: anything else."""
    parts = rel.split("/")
    if parts[0] == "file-history":
        return "once"
    if len(parts) >= 4 and parts[2] != "memory":
        if parts[3] == "tool-results":
            return "once"
        if parts[3] == "subagents" and rel.endswith(".jsonl"):
            return "append"
    if len(parts) == 3 and rel.endswith(".jsonl"):
        return "append"
    return "mutable"


def session_of(rel: str) -> str | None:
    m = re.match(rf"(?:projects/[^/]+|file-history)/({UUID})", rel)
    return m.group(1) if m else None


# ------------------------------------------------------------------ scanning

def file_hash(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as f:
        while remaining is None or remaining > 0:
            chunk = f.read(1 << 20 if remaining is None else min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.hexdigest()


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE)


def scope_roots(root: Path, project: str | None) -> list[Path]:
    """Directories to walk: one project (plus its sessions' file-history), or everything."""
    if project is None:
        return [root / "projects", root / "file-history"]
    proj = root / "projects" / project
    uuids = [p.stem for p in proj.glob("*.jsonl")]
    return [proj] + [root / "file-history" / u for u in uuids]


def manifest(project: str | None) -> dict:
    root = claude_dir()
    cache = load_cache()
    out = {}
    for base in scope_roots(root, project):
        if not base.is_dir():
            continue
        for dirpath, _, names in os.walk(base):
            for name in names:
                p = Path(dirpath) / name
                if ".conflict-" in name:
                    continue
                st = p.stat()
                rel = str(p.relative_to(root))
                key = str(p)
                hit = cache.get(key)
                if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                    digest = hit[2]
                else:
                    digest = file_hash(p)
                    cache[key] = [st.st_size, st.st_mtime_ns, digest]
                out[rel] = {"size": st.st_size, "mtime": st.st_mtime_ns, "hash": digest}
    save_cache(cache)
    return out


def proc_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # comm (field 2) can contain spaces and parens, so split after the last ')'
    return stat.rsplit(")", 1)[1].split()[19]


def live_sessions() -> dict:
    live = {}
    for f in (claude_dir() / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "pid" in s and proc_start(s["pid"]) == s.get("procStart"):
            live[s["sessionId"]] = {k: s.get(k) for k in ("pid", "status", "tmux", "name", "cwd")}
    return live


def prefix_hashes(requests: list) -> dict:
    root = claude_dir()
    return {rel: file_hash(root / rel, n) for rel, n in requests}


def scan(project: str | None) -> dict:
    return {"manifest": manifest(project), "live": live_sessions()}


# ------------------------------------------------------------------- remote

class Remote:
    def __init__(self, host: str, remote_dir: str | None):
        self.host = host
        self.remote_dir = remote_dir
        self.script = Path(__file__).read_bytes()

    def call(self, op: str, arg) -> dict:
        cmd = ["python3", "-", "--remote", op, json.dumps(arg)]
        if self.remote_dir:
            cmd = ["env", f"CLAUDE_SYNC_DIR={self.remote_dir}"] + cmd
        if self.host == "@local":  # test hook: run the "remote" side as a local subprocess
            argv = ["sh", "-c", shlex.join(cmd)]
        else:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, shlex.join(cmd)]
        r = subprocess.run(argv, input=self.script, capture_output=True)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip()
            sys.exit(f"claude-sync: {self.host}: {op} failed (exit {r.returncode}): {err}")
        return json.loads(r.stdout)


def remote_main(op: str, arg: str) -> None:
    arg = json.loads(arg)
    if op == "scan":
        result = scan(arg)
    elif op == "prefix":
        result = prefix_hashes(arg)
    else:
        sys.exit(f"unknown remote op {op}")
    json.dump(result, sys.stdout)


# --------------------------------------------------------------------- plan

def classify(local: dict, remote_: dict, live: set, remote: Remote) -> list[tuple]:
    """Return (action, rel, detail) for every path on either side."""
    plan = []
    need_prefix = []  # append-only files where the remote copy is longer
    for rel in sorted(local.keys() | remote_.keys()):
        L, R = local.get(rel), remote_.get(rel)
        sid = session_of(rel)
        if sid in live:
            plan.append(("live", rel, sid))
        elif R is None:
            plan.append(("push", rel, "new"))
        elif L is None:
            plan.append(("pull", rel, "new"))
        elif L["hash"] == R["hash"]:
            plan.append(("same", rel, ""))
        elif kind_of(rel) == "append":
            if L["size"] > R["size"]:
                ok = file_hash(claude_dir() / rel, R["size"]) == R["hash"]
                plan.append(("push", rel, "fast-forward") if ok else ("conflict", rel, "diverged"))
            elif L["size"] < R["size"]:
                need_prefix.append((rel, L["size"]))
            else:
                plan.append(("conflict", rel, "diverged"))
        elif kind_of(rel) == "once":
            plan.append(("conflict", rel, "write-once file differs"))
        else:
            plan.append(("conflict", rel, "mutable, both differ (no sync base yet)"))

    if need_prefix:
        remote_prefix = remote.call("prefix", need_prefix)
        for rel, _ in need_prefix:
            ok = remote_prefix[rel] == local[rel]["hash"]
            plan.append(("pull", rel, "fast-forward") if ok else ("conflict", rel, "diverged"))
    return plan


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def print_plan(plan: list, local: dict, remote_: dict, live_info: dict, host: str) -> None:
    counts = {}
    for action, *_ in plan:
        counts[action] = counts.get(action, 0) + 1
    if "live" in counts:
        counts["live"] = f"{len({p[2] for p in plan if p[0] == 'live'})} sessions"

    for sid in sorted({p[2] for p in plan if p[0] == "live"}):
        side, info = live_info[sid]
        print(f"skip  {sid[:8]}  running on {side}: pid {info['pid']}, {info['status']}, "
              f"tmux {info.get('tmux') or '-'}, name {info.get('name') or '-'}")

    for action, rel, detail in plan:
        if action in ("same", "live"):
            continue
        if action in ("push", "pull") and detail == "fast-forward":
            L, R = local[rel], remote_[rel]
            detail += f" +{human(abs(L['size'] - R['size']))}"
        elif action in ("push", "pull"):
            detail += f" {human((local.get(rel) or remote_.get(rel))['size'])}"
        arrow = {"push": f"-> {host}", "pull": f"<- {host}", "conflict": "!!"}[action]
        print(f"{action:8}  {arrow:14}  {rel}  ({detail})")

    summary = ", ".join(f"{counts[a]} {a}" for a in ("push", "pull", "conflict", "live", "same") if a in counts)
    print(f"\n{summary or 'nothing to do'}")


# ---------------------------------------------------------------------- cli

def cmd_sync(args) -> None:
    project = None if args.all else encode(os.path.abspath(args.project))
    remote = Remote(args.host, args.remote_dir)

    local_scan = scan(project)
    remote_scan = remote.call("scan", project)
    if project and not local_scan["manifest"] and not remote_scan["manifest"]:
        sys.exit(f"claude-sync: no sessions for {args.project} on either side")

    live_info = {sid: ("local", i) for sid, i in local_scan["live"].items()}
    live_info.update({sid: (args.host, i) for sid, i in remote_scan["live"].items()})

    plan = classify(local_scan["manifest"], remote_scan["manifest"], live_info.keys(), remote)
    print_plan(plan, local_scan["manifest"], remote_scan["manifest"], live_info, args.host)

    if not args.dry_run:
        sys.exit("\nclaude-sync: only --dry-run is implemented so far")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--remote":
        remote_main(sys.argv[2], sys.argv[3])
        return

    ap = argparse.ArgumentParser(prog="claude-sync", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="two-way sync with a host")
    s.add_argument("host")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--project", default=".", help="project directory (default: cwd)")
    g.add_argument("--all", action="store_true", help="every project")
    s.add_argument("--dry-run", "-n", action="store_true", help="print the plan, change nothing")
    s.add_argument("--remote-dir", help=argparse.SUPPRESS)  # for testing against a scratch copy
    s.set_defaults(func=cmd_sync)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
