#!/usr/bin/env python3
"""Two-way sync of Claude Code sessions between machines over ssh + rsync.

The same file runs on both ends: locally as the CLI, and on the remote host as
`ssh host python3 - --remote ...` with this script fed over stdin, so nothing
has to be installed on the other machine.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CACHE = Path(os.environ.get("CLAUDE_SYNC_CACHE", Path.home() / ".cache" / "claude-sync" / "hash-cache.json"))
LOCK_NAME = ".claude-sync.lock"
INCOMING = ".claude-sync-incoming"
EXCLUDE = {"sessions-index.json"}


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


def ends_with_newline(path: Path, size: int) -> bool:
    if size == 0:
        return True
    with open(path, "rb") as f:
        f.seek(size - 1)
        return f.read(1) == b"\n"


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
                if ".conflict-" in name or name in EXCLUDE:
                    continue
                p = Path(dirpath) / name
                st = p.stat()
                rel = str(p.relative_to(root))
                key = str(p)
                hit = cache.get(key)
                if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                    digest = hit[2]
                else:
                    digest = file_hash(p)
                    cache[key] = [st.st_size, st.st_mtime_ns, digest]
                entry = {"size": st.st_size, "mtime": st.st_mtime_ns, "hash": digest}
                if kind_of(rel) == "append":
                    entry["complete"] = ends_with_newline(p, st.st_size)
                out[rel] = entry
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


def claude_version() -> str | None:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout else None


def prefix_hashes(requests: list) -> dict:
    root = claude_dir()
    return {rel: file_hash(root / rel, n) for rel, n in requests}


def scan(project: str | None) -> dict:
    return {"manifest": manifest(project), "live": live_sessions(), "version": claude_version()}


# ------------------------------------------------------------------- remote

class Remote:
    def __init__(self, host: str, remote_dir: str | None):
        if host == "@local" and not remote_dir:
            sys.exit("claude-sync: @local needs --remote-dir")
        self.host = host
        self.remote_dir = remote_dir
        self.script = Path(__file__).read_bytes()
        self.lock_proc = None

    def _argv(self, command: str) -> list[str]:
        if self.host == "@local":  # test hook: run the "remote" side as a local subprocess
            return ["sh", "-c", command]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, command]

    def call(self, op: str, arg) -> dict:
        cmd = ["python3", "-", "--remote", op, json.dumps(arg)]
        if self.remote_dir:
            cmd = ["env", f"CLAUDE_SYNC_DIR={self.remote_dir}"] + cmd
        r = subprocess.run(self._argv(shlex.join(cmd)), input=self.script, capture_output=True)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip()
            sys.exit(f"claude-sync: {self.host}: {op} failed (exit {r.returncode}): {err}")
        return json.loads(r.stdout)

    def rsync_root(self) -> str:
        """The remote ~/.claude as an rsync endpoint, with a trailing slash."""
        root = (self.remote_dir or ".claude").rstrip("/") + "/"
        return root if self.host == "@local" else f"{self.host}:{root}"

    def lock(self) -> None:
        """Hold flock(1) on the remote lock file until this process exits.

        `python3 -` reads the script to EOF before running it, so the lock can't
        be held by this script; flock + cat keeps it until our end of stdin closes.
        """
        path = shlex.quote(f"{self.remote_dir}/{LOCK_NAME}") if self.remote_dir \
            else f'"$HOME/.claude/{LOCK_NAME}"'
        command = f"flock -n {path} -c 'echo locked; exec cat >/dev/null'"
        self.lock_proc = subprocess.Popen(self._argv(command), stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if self.lock_proc.stdout.readline().strip() != b"locked":
            err = self.lock_proc.stderr.read().decode(errors="replace").strip()
            sys.exit(f"claude-sync: {self.host}: another sync is running there {err}".rstrip())

    def unlock(self) -> None:
        if self.lock_proc:
            self.lock_proc.stdin.close()
            self.lock_proc.wait()


def remote_main(op: str, arg: str) -> None:
    arg = json.loads(arg)
    if op == "scan":
        result = scan(arg)
    elif op == "prefix":
        result = prefix_hashes(arg)
    else:
        sys.exit(f"unknown remote op {op}")
    json.dump(result, sys.stdout)


def lock_local():
    f = open(claude_dir() / LOCK_NAME, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("claude-sync: another sync is running on this machine")
    return f  # the lock lasts as long as the file stays open


# --------------------------------------------------------------------- plan

def classify(local: dict, remote_: dict, remote: Remote) -> list[list]:
    """Return [action, rel, detail] for every path on either side, ignoring liveness."""
    plan = []
    need_prefix = []  # append-only files where the remote copy is longer
    for rel in sorted(local.keys() | remote_.keys()):
        L, R = local.get(rel), remote_.get(rel)
        if R is None:
            plan.append(["push", rel, "new"])
        elif L is None:
            plan.append(["pull", rel, "new"])
        elif L["hash"] == R["hash"]:
            plan.append(["same", rel, ""])
        elif kind_of(rel) == "append":
            if L["size"] > R["size"]:
                ok = file_hash(claude_dir() / rel, R["size"]) == R["hash"]
                plan.append(["push", rel, "fast-forward"] if ok else ["conflict", rel, "diverged"])
            elif L["size"] < R["size"]:
                need_prefix.append((rel, L["size"]))
            else:
                plan.append(["conflict", rel, "diverged"])
        elif kind_of(rel) == "once":
            plan.append(["conflict", rel, "write-once file differs"])
        else:
            plan.append(["conflict", rel, "both changed (no sync base yet)"])

    if need_prefix:
        remote_prefix = remote.call("prefix", need_prefix)
        for rel, _ in need_prefix:
            ok = remote_prefix[rel] == local[rel]["hash"]
            plan.append(["pull", rel, "fast-forward"] if ok else ["conflict", rel, "diverged"])
    plan.sort(key=lambda p: p[1])
    return plan


def apply_live(plan: list, local: dict, remote_: dict, live: dict, host: str) -> dict:
    """Turn actions that would touch a running session into skips.

    live maps side ("local" or host) -> {sessionId: info}. Returns
    {sessionId: (side, info)} for idle sessions that are being copied from.
    """
    snapshots = {}
    for item in plan:
        action, rel, _ = item
        sid = session_of(rel)
        if action == "same" or sid is None:
            continue
        if action == "conflict":
            for side in ("local", host):
                if sid in live[side]:
                    item[0], item[2] = "skip", f"diverged, but open on {side}"
            continue
        src, dst = ("local", host) if action == "push" else (host, "local")
        src_entry = (local if action == "push" else remote_)[rel]
        if sid in live[dst]:
            item[0], item[2] = "skip", f"open on {dst}"
        elif sid in live[src]:
            info = live[src][sid]
            if info.get("status") != "idle":
                item[0], item[2] = "skip", f"busy on {src}"
            elif not src_entry.get("complete", True):
                item[0], item[2] = "skip", f"partial last line on {src}"
            else:
                snapshots[sid] = (src, info)
    return snapshots


def conflict_name(rel: str, host: str, entry: dict) -> str:
    # Keyed by content, so a conflict that persists is saved once, not on every sync.
    return f"{rel}.conflict-{host}-{entry['hash'][:8]}"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def describe(info: dict) -> str:
    return (f"pid {info['pid']}, {info.get('status')}, tmux {info.get('tmux') or '-'}, "
            f"name {info.get('name') or '-'}")


def print_plan(plan: list, local: dict, remote_: dict, live: dict, snapshots: dict, host: str) -> None:
    skipped = {}
    for action, rel, detail in plan:
        if action == "skip":
            skipped.setdefault((session_of(rel), detail), 0)
            skipped[(session_of(rel), detail)] += 1
    for (sid, why), n in sorted(skipped.items()):
        side = why.rsplit(" on ", 1)[-1]
        info = live[side].get(sid)
        print(f"skip      {sid[:8]}  {why} ({describe(info) if info else f'{n} files'})")
    for sid, (side, info) in sorted(snapshots.items()):
        print(f"warn      {sid[:8]}  copying a snapshot; still open on {side} ({describe(info)})")

    for action, rel, detail in plan:
        if action in ("same", "skip"):
            continue
        if detail == "fast-forward":
            detail += f" +{human(abs(local[rel]['size'] - remote_[rel]['size']))}"
        elif detail == "new":
            detail += f" {human((local.get(rel) or remote_.get(rel))['size'])}"
        arrow = {"push": f"-> {host}", "pull": f"<- {host}", "conflict": "!!"}[action]
        print(f"{action:8}  {arrow:14}  {rel}  ({detail})")

    counts = {}
    for action, *_ in plan:
        counts[action] = counts.get(action, 0) + 1
    if "skip" in counts:
        counts["skip"] = f"{len({s for s, _ in skipped})} sessions"
    summary = ", ".join(f"{counts[a]} {a}" for a in ("push", "pull", "conflict", "skip", "same") if a in counts)
    print(f"\n{summary or 'nothing to do'}")


# ------------------------------------------------------------------ execute

def rsync(src: str, dst: str, paths: list[str]) -> None:
    if not paths:
        return
    r = subprocess.run(["rsync", "-a", "--files-from=-", "--from0", src, dst],
                       input="\0".join(paths).encode())
    if r.returncode != 0:
        sys.exit(f"claude-sync: rsync {src} -> {dst} failed (exit {r.returncode})")


def execute(plan: list, remote_: dict, remote: Remote) -> None:
    root = claude_dir()
    local_root = f"{root}/"
    push = [rel for action, rel, _ in plan if action == "push"]
    pull = [rel for action, rel, _ in plan if action == "pull"]
    conflicts = [rel for action, rel, _ in plan if action == "conflict"
                 and not (root / conflict_name(rel, remote.host, remote_[rel])).exists()]

    rsync(local_root, remote.rsync_root(), push)
    rsync(remote.rsync_root(), local_root, pull)

    # Conflict copies: fetch the remote version into a staging dir on the same
    # filesystem, then move each next to the local file under its conflict name.
    if conflicts:
        staging = root / INCOMING
        shutil.rmtree(staging, ignore_errors=True)
        rsync(remote.rsync_root(), f"{staging}/", conflicts)
        for rel in conflicts:
            os.replace(staging / rel, root / conflict_name(rel, remote.host, remote_[rel]))
        shutil.rmtree(staging)

    print(f"done: pushed {len(push)}, pulled {len(pull)}, saved {len(conflicts)} conflict copies")


# ---------------------------------------------------------------------- cli

def cmd_sync(args) -> None:
    project = None if args.all else encode(os.path.abspath(args.project))
    remote = Remote(args.host, args.remote_dir)

    if not args.dry_run:
        local_lock = lock_local()
        remote.lock()

    local_scan = scan(project)
    remote_scan = remote.call("scan", project)
    local, remote_ = local_scan["manifest"], remote_scan["manifest"]
    if project and not local and not remote_:
        sys.exit(f"claude-sync: no sessions for {args.project} on either side")

    lv, rv = local_scan["version"], remote_scan["version"]
    if lv != rv:
        print(f"warn      Claude Code versions differ: local {lv or '?'}, {args.host} {rv or '?'}")

    live = {"local": local_scan["live"], args.host: remote_scan["live"]}
    plan = classify(local, remote_, remote)
    snapshots = apply_live(plan, local, remote_, live, args.host)
    print_plan(plan, local, remote_, live, snapshots, args.host)

    if not args.dry_run:
        execute(plan, remote_, remote)
        remote.unlock()
        local_lock.close()


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
