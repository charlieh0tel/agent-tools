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
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CACHE = Path(
    os.environ.get("CLAUDE_SYNC_CACHE", Path.home() / ".cache" / "claude-sync" / "hash-cache.json")
)
SYNC = "claude-sync"  # our own dir inside ~/.claude: lock, state/, trash/, incoming/
EXCLUDE = {"sessions-index.json"}
HISTORY = "history.jsonl"
CONFIG = ["CLAUDE.md", "settings.json", "keybindings.json", "skills"]
TRASH_DAYS = 30
MASS_DELETE = (
    20,
    0.25,
)  # refuse to delete more than this many files AND this fraction of the scope


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SYNC_DIR", Path.home() / ".claude"))


def sync_dir() -> Path:
    return claude_dir() / SYNC


def encode(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def machine_id() -> str:
    # The sync base is keyed by machine. Containers often have no machine-id, so the
    # tests (and anyone running there) can supply one.
    if override := os.environ.get("CLAUDE_SYNC_MACHINE_ID"):
        return override
    try:
        return Path("/etc/machine-id").read_text().strip()
    except FileNotFoundError:
        sys.exit("claude-sync: /etc/machine-id is missing; set CLAUDE_SYNC_MACHINE_ID")


# ---------------------------------------------------------------- file kinds


def kind_of(rel: str) -> str:
    """append: only ever grows at the end; once: written once; mutable: anything else."""
    parts = rel.split("/")
    if rel == HISTORY:
        return "history"
    if parts[0] == "file-history":
        return "once"
    if parts[0] != "projects":
        return "mutable"
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


def project_sessions(enc: str, rels) -> set[str]:
    m = re.compile(rf"projects/{re.escape(enc)}/({UUID})\.jsonl$")
    return {hit.group(1) for rel in rels if (hit := m.match(rel))}


def in_scope(rel: str, scope: dict, sessions: set[str]) -> bool:
    top = rel.split("/")[0]
    if rel == HISTORY:
        return True
    if top in CONFIG:
        return scope["config"]
    if scope["project"] is None:
        return top in ("projects", "file-history")
    return rel.startswith(f"projects/{scope['project']}/") or (
        top == "file-history" and session_of(rel) in sessions
    )


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


def write_file(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace path with text atomically. Owner-only by default, as Claude Code keeps
    transcripts: they hold whole conversations, and the umask would make them public."""
    tmp = path.with_name(path.name + ".claude-sync-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.fchmod(fd, mode)  # a leftover tmp keeps its old mode through O_CREAT
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def check_paths(rels, source: str) -> None:
    """Refuse paths that would leave ~/.claude. Only a broken or hostile peer sends them,
    so stop rather than skip: nothing else it says can be trusted either."""
    for rel in rels:
        parts = PurePosixPath(rel).parts
        if not parts or parts[0] == "/" or ".." in parts:
            sys.exit(f"claude-sync: refusing unsafe path {rel!r} from {source}")


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    write_file(CACHE, json.dumps(cache))


def scope_roots(root: Path, scope: dict) -> list[Path]:
    """Files and directories to walk for a scope."""
    if scope["project"] is None:
        roots = [root / "projects", root / "file-history"]
    else:
        proj = root / "projects" / scope["project"]
        uuids = {p.stem for p in proj.glob("*.jsonl")} | set(scope["uuids"])
        roots = [proj, *(root / "file-history" / u for u in sorted(uuids))]
    if scope["config"]:
        roots += [root / name for name in CONFIG]
    return [*roots, root / HISTORY]


def scan_failed(e: OSError):
    # An unreadable file or directory must not look like a deleted one: that would
    # trash the peer's copies. Stop instead.
    sys.exit(f"claude-sync: can't scan {e.filename}: {e.strerror}; stopping")


def walk(base: Path):
    if base.is_file():
        yield base
    elif base.is_dir():
        for dirpath, _, names in os.walk(base, onerror=scan_failed):
            for name in names:
                yield Path(dirpath) / name


def manifest(scope: dict) -> dict:
    root = claude_dir()
    cache = load_cache()
    out = {}
    for base in scope_roots(root, scope):
        for p in walk(base):
            if ".conflict-" in p.name or p.name in EXCLUDE or p.is_symlink():
                continue
            rel = str(p.relative_to(root))
            try:
                st = p.stat()
                hit = cache.get(str(p))
                if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                    digest = hit[2]
                else:
                    digest = file_hash(p)
                    cache[str(p)] = [st.st_size, st.st_mtime_ns, digest]
                entry = {"size": st.st_size, "hash": digest}
                if kind_of(rel) == "append":
                    entry["complete"] = ends_with_newline(p, st.st_size)
            except FileNotFoundError:
                continue  # removed since the walk listed it: it is gone, as a deletion
            except OSError as e:
                scan_failed(e)
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


def purge_trash() -> None:
    cutoff = time.time() - TRASH_DAYS * 86400
    for d in (sync_dir() / "trash").glob("*"):
        if d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def trash(rels: list[str]) -> None:
    """Move files into trash/<timestamp>/, pruning directories left empty."""
    root = claude_dir()
    dest = sync_dir() / "trash" / time.strftime("%Y%m%dT%H%M%S")
    for rel in rels:
        if (root / rel).exists():
            os.renames(root / rel, dest / rel)


def scan(scope: dict) -> dict:
    purge_trash()
    return {
        "manifest": manifest(scope),
        "live": live_sessions(),
        "version": claude_version(),
        "machine": machine_id(),
    }


def prefix_hashes(requests: list) -> dict:
    root = claude_dir()
    return {rel: file_hash(root / rel, n) for rel, n in requests}


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

    def _fail(self, what: str, code: int, stderr: bytes):
        err = stderr.decode(errors="replace").strip()
        if code == 255 and self.host != "@local":
            last = err.splitlines()[-1] if err else "ssh failed"
            sys.exit(f"claude-sync: can't reach {self.host}: {last}")
        sys.exit(f"claude-sync: {self.host}: {what} failed (exit {code}): {err}")

    def call(self, op: str, arg) -> dict:
        cmd = ["python3", "-", "--remote", op, json.dumps(arg)]
        if self.remote_dir:
            cmd = ["env", f"CLAUDE_SYNC_DIR={self.remote_dir}", *cmd]
        r = subprocess.run(self._argv(shlex.join(cmd)), input=self.script, capture_output=True)
        if r.returncode != 0:
            self._fail(op, r.returncode, r.stderr)
        return json.loads(r.stdout)

    def rsync_root(self) -> str:
        """The remote ~/.claude as an rsync endpoint, with a trailing slash."""
        root = (self.remote_dir or ".claude").rstrip("/") + "/"
        return root if self.host == "@local" else f"{self.host}:{root}"

    def lock(self) -> None:
        """Hold flock(1) on the remote lock file until unlock() or this process exits.

        `python3 -` reads the script to EOF before running it, so the lock can't
        be held by this script; flock + cat keeps it until our end of stdin closes.
        """
        d = (
            shlex.quote(f"{self.remote_dir}/{SYNC}")
            if self.remote_dir
            else f'"$HOME/.claude/{SYNC}"'
        )
        command = f"mkdir -p {d} && flock -n {d}/lock -c 'echo locked; exec cat >/dev/null'"
        self.lock_proc = subprocess.Popen(
            self._argv(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if self.lock_proc.stdout.readline().strip() == b"locked":
            return
        code = self.lock_proc.wait()
        if code == 1:
            sys.exit(f"claude-sync: another sync is running on {self.host}")
        self._fail("lock", code, self.lock_proc.stderr.read())

    def unlock(self) -> None:
        if self.lock_proc:
            self.lock_proc.stdin.close()
            self.lock_proc.wait()


def remote_main(op: str, arg: str) -> None:
    arg = json.loads(arg)
    if op == "scan":
        result = scan(arg)
    elif op == "prefix":
        check_paths((rel for rel, _ in arg), "the syncing machine")
        result = prefix_hashes(arg)
    elif op == "trash":
        check_paths(arg, "the syncing machine")
        result = trash(arg)
    else:
        sys.exit(f"unknown remote op {op}")
    json.dump(result, sys.stdout)


def lock_local():
    sync_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(sync_dir() / "lock", os.O_WRONLY | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("claude-sync: another sync is running on this machine")
    return fd  # the lock lasts as long as the fd stays open


# ---------------------------------------------------------------- sync base


def state_path(peer: str) -> Path:
    return sync_dir() / "state" / f"{peer}.json"


def load_base(peer: str) -> dict:
    """{rel: hash} as of the last completed sync with peer (by machine id)."""
    try:
        return json.loads(state_path(peer).read_text())["files"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}


def save_base(peer: str, me: str, files: dict, remote: Remote) -> None:
    """Write the base locally, and the same content on the peer under our machine id."""
    p = state_path(peer)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_file(p, json.dumps({"files": files}, indent=0, sort_keys=True))
    rsync(str(p), f"{remote.rsync_root()}{SYNC}/state/{me}.json", mkpath=True)


# --------------------------------------------------------------------- plan


DIVERGED = "diverged; chaining tails"


def classify(local: dict, remote_: dict, base: dict, rels, remote: Remote) -> list[list]:
    """Return [action, rel, detail] for each path, ignoring liveness.

    Actions: same, push, pull, del-local, del-remote, conflict (keep both, save the
    remote copy locally), override (local wins, remote copy saved locally),
    merge (MEMORY.md and history.jsonl: union of both; diverged transcripts: tails
    chained), forget (gone on both sides).
    """
    plan = []
    need_prefix = []  # append-only files where the remote copy is longer
    for rel in sorted(rels):
        L, R, B = local.get(rel), remote_.get(rel), base.get(rel)
        kind = kind_of(rel)
        if L is None and R is None:
            plan.append(["forget", rel, ""])
        elif R is None:
            if B is None:
                plan.append(["push", rel, "new"])
            elif L["hash"] == B:
                plan.append(["del-local", rel, "deleted on remote"])
            else:
                plan.append(["push", rel, "changed here, deleted there"])
        elif L is None:
            if B is None:
                plan.append(["pull", rel, "new"])
            elif R["hash"] == B:
                plan.append(["del-remote", rel, "deleted here"])
            else:
                plan.append(["pull", rel, "changed there, deleted here"])
        elif L["hash"] == R["hash"]:
            plan.append(["same", rel, ""])
        elif kind == "append":
            if L["size"] > R["size"]:
                ok = file_hash(claude_dir() / rel, R["size"]) == R["hash"]
                plan.append(["push", rel, "fast-forward"] if ok else ["merge", rel, DIVERGED])
            elif L["size"] < R["size"]:
                need_prefix.append((rel, L["size"]))
            else:
                plan.append(["merge", rel, DIVERGED])
        elif merger(rel):
            plan.append(["merge", rel, "union"])
        elif kind == "once":
            plan.append(["conflict", rel, "write-once file differs"])
        elif L["hash"] == B:
            plan.append(["pull", rel, "changed there"])
        elif R["hash"] == B:
            plan.append(["push", rel, "changed here"])
        else:
            plan.append(["override", rel, "changed on both sides; keeping local"])

    if need_prefix:
        remote_prefix = remote.call("prefix", need_prefix)
        for rel, _ in need_prefix:
            ok = remote_prefix[rel] == local[rel]["hash"]
            plan.append(["pull", rel, "fast-forward"] if ok else ["merge", rel, DIVERGED])
    plan.sort(key=lambda p: p[1])
    return plan


WRITES = {
    "push": "remote",
    "override": "remote",
    "del-remote": "remote",
    "pull": "local",
    "del-local": "local",
}


def apply_live(plan: list, local: dict, remote_: dict, live: dict, host: str) -> dict:
    """Turn actions that would touch a running session into skips.

    live maps side ("local" or host) -> {sessionId: info}. Returns
    {sessionId: (side, info)} for idle sessions that are being copied from.
    """
    snapshots = {}
    for item in plan:
        action, rel, _ = item
        sid = session_of(rel)
        if sid is None or action in ("same", "forget"):
            continue
        if action in ("conflict", "merge"):  # both sides end up with a different file
            for side in ("local", host):
                if sid in live[side]:
                    item[0], item[2] = "skip", f"diverged, but open on {side}"
            continue
        dst = "local" if WRITES[action] == "local" else host
        src = host if dst == "local" else "local"
        if sid in live[dst]:
            item[0], item[2] = "skip", f"open on {dst}"
        elif sid in live[src] and not action.startswith("del-"):
            info = live[src][sid]
            src_entry = (local if src == "local" else remote_)[rel]
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
    return (
        f"pid {info['pid']}, {info.get('status')}, tmux {info.get('tmux') or '-'}, "
        f"name {info.get('name') or '-'}"
    )


ARROWS = {
    "push": "->",
    "pull": "<-",
    "override": "->",
    "merge": "<->",
    "del-local": "x local",
    "del-remote": "x remote",
    "conflict": "!!",
}


def print_plan(
    plan: list, local: dict, remote_: dict, live: dict, snapshots: dict, host: str
) -> None:
    skipped = {}
    for action, rel, detail in plan:
        if action == "skip":
            skipped[(session_of(rel), detail)] = skipped.get((session_of(rel), detail), 0) + 1
    for (sid, why), n in sorted(skipped.items()):
        info = live[why.rsplit(" on ", 1)[-1]].get(sid)
        print(f"skip      {sid[:8]}  {why} ({describe(info) if info else f'{n} files'})")
    for sid, (side, info) in sorted(snapshots.items()):
        print(f"warn      {sid[:8]}  copying a snapshot; still open on {side} ({describe(info)})")

    for action, rel, detail in plan:
        if action in ("same", "skip", "forget"):
            continue
        if detail == "fast-forward":
            detail += f" +{human(abs(local[rel]['size'] - remote_[rel]['size']))}"
        elif detail == "new":
            detail += f" {human((local.get(rel) or remote_.get(rel))['size'])}"
        arrow = ARROWS[action].replace("remote", host)
        arrow = f"{arrow} {host}" if arrow in ("->", "<-", "<->") else arrow
        print(f"{action:10}  {arrow:16}  {rel}  ({detail})")

    counts = {}
    for action, *_ in plan:
        counts[action] = counts.get(action, 0) + 1
    if "skip" in counts:
        counts["skip"] = f"{len({s for s, _ in skipped})} sessions"
    order = (
        "push",
        "pull",
        "merge",
        "override",
        "del-local",
        "del-remote",
        "conflict",
        "skip",
        "same",
    )
    summary = ", ".join(f"{counts[a]} {a}" for a in order if a in counts)
    print(f"\n{summary or 'nothing to do'}")


# ------------------------------------------------------------------ execute


def rsync(src: str, dst: str, paths: list[str] | None = None, mkpath: bool = False) -> None:
    """Copy src to dst; with paths, copy only those (relative to src)."""
    if paths is not None and not paths:
        return
    # --no-links: claude-sync never syncs symlinks, and one planted by the peer would
    # otherwise be read through when merging, sending back whatever it points at.
    cmd = ["rsync", "-a", "--no-links"] + (["--mkpath"] if mkpath else [])
    if paths is not None:
        cmd += ["--files-from=-", "--from0"]
    r = subprocess.run([*cmd, src, dst], input="\0".join(paths or []).encode())
    if r.returncode != 0:
        sys.exit(f"claude-sync: rsync {src} -> {dst} failed (exit {r.returncode})")


def merge_history(ours: str, theirs: str) -> str:
    """Union of two prompt histories, sorted by timestamp, exact duplicates dropped."""
    seen, entries = set(), []
    for line in ours.split("\n") + theirs.split("\n"):  # see jsonl_lines on splitlines()
        if not line.strip() or line in seen:
            continue
        seen.add(line)
        try:
            ts = json.loads(line).get("timestamp", 0)
        except (json.JSONDecodeError, AttributeError):
            ts = 0
        entries.append((ts, line))
    entries.sort(key=lambda e: e[0])  # stable: ours before theirs on ties
    return "".join(line + "\n" for _, line in entries)


def merge_index(ours: str, theirs: str) -> str:
    """MEMORY.md: our lines in order, then any of theirs we don't have."""
    have = set(ours.split("\n"))
    extra = [line for line in theirs.split("\n") if line.strip() and line not in have]
    return (
        ours
        + ("" if not ours or ours.endswith("\n") else "\n")
        + "".join(line + "\n" for line in extra)
    )


def jsonl_lines(text: str) -> list[str]:
    """Lines of a JSONL file, each ending in a newline. Split on \\n only: splitlines()
    also breaks at U+2028 and friends, which JSON from JavaScript leaves unescaped in
    strings. The last record may lack its newline, and appending another tail must not
    run two records together."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line + "\n" for line in lines]


def split_transcripts(ours: str, theirs: str) -> tuple[list[str], set[str], list[list]]:
    """Split two transcripts where they first differ: the shared prefix's lines, the
    uuids in it, and each side's tail as [(line, record), ...]."""
    a, b = map(jsonl_lines, (ours, theirs))
    pairs = zip(a, b, strict=False)  # the tails differ in length
    n = next((i for i, (x, y) in enumerate(pairs) if x != y), min(len(a), len(b)))
    try:
        prefix_ids = {r.get("uuid") for r in map(json.loads, a[:n])} - {None}
        tails = [[(line, json.loads(line)) for line in lines] for lines in (a[n:], b[n:])]
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON lines: {e}") from None
    return a[:n], prefix_ids, tails


def tail_start(tail: list) -> tuple[str, str]:
    # ISO timestamps sort as strings; the text breaks ties the same way on both machines
    ts = next((r["timestamp"] for _, r in tail if "timestamp" in r), "")
    return ts, "".join(line for line, _ in tail)


def tail_tip(tail: list) -> str | None:
    """Where --resume would continue this tail: the leaf its last last-prompt record
    names, which need not be the last record written; else its last record."""
    ids = [r["uuid"] for _, r in tail if r.get("uuid")]
    prompts = [r for _, r in tail if r.get("type") == "last-prompt"]
    leaf = prompts[-1].get("leafUuid") if prompts else None
    return leaf if leaf in ids else next(reversed(ids), None)


def merge_transcript(ours: str, theirs: str) -> str:
    """Two transcripts continued from a common prefix, as one chain: prefix, the
    tail that started first, then the other tail hung off the first tail's tip.

    claude --resume follows one parentUuid chain back from the tip the file's last
    last-prompt record names, so tails left side by side would hide one of them.
    Records of the second tail whose parent is in the prefix are its roots and are
    re-parented; compaction boundaries (parentUuid null) are left alone, and a
    compacted tail goes first. Raises ValueError if the files are not transcripts
    that can be chained this way.
    """
    prefix, prefix_ids, tails = split_transcripts(ours, theirs)
    # --resume stops at a compaction boundary, so a compacted tail must go first: the
    # other one then hangs off its end. If both were compacted, one is always lost.
    compacted = [any(r.get("subtype") == "compact_boundary" for _, r in t) for t in tails]
    if all(compacted):
        raise ValueError("both sides were compacted")
    order = sorted((0, 1), key=lambda i: (not compacted[i], tail_start(tails[i])))
    first, second = (tails[i] for i in order)
    tip = tail_tip(first)
    if tip is None:
        raise ValueError("no message to chain onto")
    out, related = prefix + [line for line, _ in first], False
    # A sync interrupted after writing a merge locally leaves the merge on one side
    # only; merging it again must not add the other tail a second time.
    have_lines = {line for line, _ in first}
    have_ids = {r.get("uuid") for _, r in first} - {None}
    for line, r in second:
        if line in have_lines or r.get("uuid") in have_ids:
            related = True
            continue
        if r.get("uuid") and r.get("parentUuid") in prefix_ids:
            r["parentUuid"], related = tip, True
            line = json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
        out.append(line)
    if not related:
        raise ValueError("no message in the other tail starts from the shared prefix")
    check_chain(json.loads(line) for line in out)
    return "".join(out)


def check_chain(records) -> None:
    """Raise ValueError unless every uuid is unique and no parentUuid chain loops."""
    parents = {}
    for r in records:
        if uuid := r.get("uuid"):
            if uuid in parents:
                raise ValueError(f"duplicate uuid {uuid}")
            parents[uuid] = r.get("parentUuid")
    acyclic = set()
    for uuid in parents:
        path = {}  # a dict: ordered like a list, but O(1) membership on long chains
        while uuid in parents and uuid not in acyclic:
            if uuid in path:
                raise ValueError(f"parentUuid cycle through {uuid}")
            path[uuid] = None
            uuid = parents[uuid]
        acyclic.update(path)


def merger(rel: str):
    """The function that merges two versions of rel, or None if rel isn't merged."""
    if rel == HISTORY:
        return merge_history
    if rel.endswith("/memory/MEMORY.md"):
        return merge_index
    if kind_of(rel) == "append":
        return merge_transcript
    return None


def execute(plan: list, remote_: dict, remote: Remote) -> dict:
    """Carry out the plan. Returns {rel: hash} for files whose content changed locally."""
    root = claude_dir()
    by_action = {}
    for action, rel, _ in plan:
        by_action.setdefault(action, []).append(rel)
    get = lambda a: by_action.get(a, [])  # noqa: E731

    # Fetch remote versions needed locally (conflict copies, merge inputs) into a
    # staging dir on the same filesystem, so they can be moved into place.
    saves = [
        rel
        for rel in get("conflict") + get("override")
        if not (root / conflict_name(rel, remote.host, remote_[rel])).exists()
    ]
    staging = sync_dir() / "incoming"
    shutil.rmtree(staging, ignore_errors=True)
    rsync(remote.rsync_root(), f"{staging}/", saves + get("merge"))
    for rel in saves:
        os.replace(staging / rel, root / conflict_name(rel, remote.host, remote_[rel]))

    merged = {}
    for rel in get("merge"):
        # Re-read ours right before writing, so lines appended since the scan survive.
        ours = (root / rel).read_text()
        theirs = (staging / rel).read_text()
        originals = {os.uname().nodename: ours, remote.host: theirs}
        try:
            text = merger(rel)(ours, theirs)
        except ValueError as e:
            print(f"claude-sync: can't chain {rel}: {e}; saving a conflict copy", file=sys.stderr)
            originals = {remote.host: theirs}
            text = None
        if kind_of(rel) == "append":  # a rewritten transcript keeps both originals
            for host, original in originals.items():
                entry = {"hash": hashlib.sha256(original.encode()).hexdigest()}
                copy = root / conflict_name(rel, host, entry)
                if not copy.exists():
                    write_file(copy, original)
                    saves.append(rel)
        if text is not None:
            write_file(root / rel, text, stat.S_IMODE((root / rel).stat().st_mode))
            merged[rel] = hashlib.sha256(text.encode()).hexdigest()
    shutil.rmtree(staging, ignore_errors=True)

    rsync(f"{root}/", remote.rsync_root(), get("push") + get("override") + list(merged))
    rsync(remote.rsync_root(), f"{root}/", get("pull"))
    trash(get("del-local"))
    if get("del-remote"):
        remote.call("trash", get("del-remote"))

    print(
        f"done: pushed {len(get('push')) + len(get('override'))}, pulled {len(get('pull'))}, "
        f"merged {len(merged)}, deleted {len(get('del-local')) + len(get('del-remote'))}, "
        f"saved {len(saves)} conflict copies"
    )
    return merged


def new_base(plan: list, local: dict, remote_: dict, base: dict, merged: dict) -> dict:
    out = dict(base)
    for action, rel, _ in plan:
        if action in ("same", "push", "override"):
            out[rel] = local[rel]["hash"]
        elif action == "pull":
            out[rel] = remote_[rel]["hash"]
        elif action == "merge" and rel in merged:
            out[rel] = merged[rel]
        elif action in ("del-local", "del-remote", "forget"):
            out.pop(rel, None)
        # conflict / skip: keep the old base, so the next sync decides again
    return out


# ---------------------------------------------------------------------- cli


def cmd_sync(args) -> None:
    project = None if args.all else encode(os.path.abspath(args.project))
    remote = Remote(args.host, args.remote_dir)

    if not args.dry_run:
        local_lock = lock_local()
        remote.lock()

    me = machine_id()
    # The peer's machine id is only known after its scan, but its base lists
    # sessions we must scan for, so scan the remote first with every known uuid.
    bases = {p.stem: load_base(p.stem) for p in (sync_dir() / "state").glob("*.json")}
    for peer_id, b in bases.items():  # peers write these files too
        check_paths(b, f"sync state for {peer_id}")
    known = (
        set().union(*(project_sessions(project, b) for b in bases.values())) if project else set()
    )
    scope = {"project": project, "config": args.config, "uuids": sorted(known)}
    remote_scan = remote.call("scan", scope)
    local_scan = scan(scope)
    local, remote_ = local_scan["manifest"], remote_scan["manifest"]
    check_paths(remote_, args.host)
    peer = remote_scan["machine"]
    if peer == me and args.host != "@local":
        sys.exit(f"claude-sync: {args.host} is this machine")
    base = bases.get(peer, {})

    sessions = (
        project_sessions(project, local.keys() | remote_.keys() | base.keys()) if project else set()
    )
    rels = {
        rel for rel in local.keys() | remote_.keys() | base.keys() if in_scope(rel, scope, sessions)
    }
    if project and not any(r.startswith("projects/") for r in rels):
        sys.exit(f"claude-sync: no sessions for {args.project} on either side")

    lv, rv = local_scan["version"], remote_scan["version"]
    if lv != rv:
        print(f"warn      Claude Code versions differ: local {lv or '?'}, {args.host} {rv or '?'}")
    if not base:
        print(f"note      first sync with {args.host}: nothing will be deleted this time")

    live = {"local": local_scan["live"], args.host: remote_scan["live"]}
    plan = classify(local, remote_, base, rels, remote)
    snapshots = apply_live(plan, local, remote_, live, args.host)
    print_plan(plan, local, remote_, live, snapshots, args.host)

    deletes = sum(1 for p in plan if p[0].startswith("del-"))
    limit, fraction = MASS_DELETE
    if deletes > limit and deletes > fraction * len(rels) and not args.allow_deletes:
        sys.exit(
            f"\nclaude-sync: refusing to delete {deletes} of {len(rels)} files; "
            "rerun with --allow-deletes if that's intended"
        )

    if not args.dry_run:
        merged = execute(plan, remote_, remote)
        save_base(peer, me, new_base(plan, local, remote_, base, merged), remote)
        remote.unlock()
        os.close(local_lock)


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
    s.add_argument(
        "--config",
        action="store_true",
        help="also sync CLAUDE.md, settings.json, keybindings.json and skills/",
    )
    s.add_argument("--dry-run", "-n", action="store_true", help="print the plan, change nothing")
    s.add_argument("--allow-deletes", action="store_true", help="allow deleting many files at once")
    s.add_argument("--remote-dir", help=argparse.SUPPRESS)  # for testing against a scratch copy
    s.set_defaults(func=cmd_sync)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
