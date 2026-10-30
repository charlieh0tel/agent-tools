# claude-sync plan

Two-way sync of Claude Code sessions between Linux machines (e.g. desktop and
laptop `nabemono`) over ssh + rsync, so a session can be continued on
whichever machine you're at. Both machines use the same home directory and
repository paths, so no path rewriting is needed.

## What gets synced

| Path under `~/.claude` | Kind | Rule |
|---|---|---|
| `projects/<enc>/<uuid>.jsonl` | append-only | prefix test |
| `projects/<enc>/<uuid>/subagents/*.jsonl` | append-only | prefix test |
| `projects/<enc>/<uuid>/tool-results/*` | write-once | copy if missing, conflict if different |
| `file-history/<uuid>/<hash>@vN` | write-once | copy if missing, conflict if different |
| `projects/<enc>/<uuid>/custom-title.json` | mutable | three-way against sync base |
| `projects/<enc>/memory/*` | mutable | three-way against sync base |
| `history.jsonl` | append by both | union, sort by `timestamp`, dedupe |
| `sessions-index.json` | derived cache | never synced |

`<enc>` is the cwd with every non-alphanumeric character replaced by `-`.

Scope is the current directory's project plus `history.jsonl` by default, or
every project with `--all`.

`--config` (opt-in) also syncs `CLAUDE.md`, `skills/`, `keybindings.json` and
`settings.json` as mutable files.

Never synced: `sessions/`, `session-env/`, `shell-snapshots/` (per-machine
process state), `plugins/`, caches, logs and telemetry.

## Facts established from real data

- `claude --resume` appends to the same `<uuid>.jsonl`; no transcript contains
  more than one `sessionId`.
- Transcripts are trees (`uuid` / `parentUuid`) and already contain branches, so
  two tails continued from a common prefix can be merged into one valid file.
- `sessions/<pid>.json` records a running session. `procStart` equals field 22
  of `/proc/<pid>/stat`, which tells a live process from a reused PID. It also
  has `status` (`idle`/`busy`) and sometimes a `tmux` pane.
- `claude --resume` loads one chain: from the tip named by the last
  `last-prompt` record's `leafUuid` (in file order, not by timestamp) back
  through `parentUuid` to the root. Other branches stay in the file but the
  model never sees them. (Tested on 2.1.296.)
- Re-parenting one tail's first message onto the other tail's last message
  makes `--resume` see both: the model recalled the messages from both
  tails, in file order, and continued from the second tail's tip.
- Claude no longer maintains `sessions-index.json`: the two left were last
  written 2026-02-03 and list none of the current sessions, and `--resume`
  works without them.

## Algorithm

```
sync(host, scope):
    lock local and remote
    L, R  = manifest + live sessions from each side   (remote: ssh host python3 - < self)
    base  = state from last sync with host
    warn if Claude versions differ
    for path in L ∪ R:
        open on the receiving side       -> skip, report
        busy on the sending side         -> skip, report
        idle on the sending side         -> snapshot up to last complete line, warn
        one side only, not in base       -> copy
        one side only, in base, other
          side unchanged since base      -> delete it there (copy to trash first)
        same hash                        -> nothing
        append-only: one is a prefix     -> fast-forward
                     otherwise           -> chain tails (keep both originals as .conflict)
        write-once, differs              -> conflict copy
        mutable: only one side != base   -> take it
                 both changed            -> local wins; remote copy saved as .conflict
        MEMORY.md, history.jsonl differ  -> union of both
    refuse if deleting > 20 files and > 25% of the scope (--allow-deletes)
    rsync push list, rsync pull list, trash deletions
    save base on both sides
```

## Decisions

- **Tool state** lives in `~/.claude/claude-sync/` on each machine: `lock`,
  `state/<peer-machine-id>.json` (the sync base: `{path: sha256}` after the last
  sync), `trash/` and `incoming/` (staging). Both machines hold the same base
  for their pair, keyed by `/etc/machine-id`, so it doesn't matter which one
  starts the sync. Keeping it inside `~/.claude` puts trash and staging on the
  same filesystem as the data, so moves are renames. The hash cache stays in
  `~/.cache/claude-sync/`.
- **Accounts** (claude-user): a sync works on one account's Claude dir on each
  side. The syncing machine picks the account and sends its name; the peer
  maps the name to a dir with its own config, since which account owns
  `~/.claude` is per machine. Tool state lives inside each account's dir, so
  each account has its own lock, base and trash.
- **Deletions** are tracked against the sync base and applied to the other side.
  The deleted file is first moved to `~/.claude/claude-sync/trash/<timestamp>/`,
  and trash older than 30 days is cleared at the start of each sync. A file that
  was changed on one side and deleted on the other is kept. The first sync with
  a host deletes nothing. More than 20 deletions that are also over 25% of the
  scope need `--allow-deletes`.
- **Mutable files changed on both sides:** the machine running the sync keeps
  its version and pushes it; the other version is saved next to it as
  `<name>.conflict-<host>-<hash8>`. `MEMORY.md` is an index of one-line
  pointers, so it is merged instead (our lines, then any of theirs we lack).
- **Running sessions:** open on the receiving side, always skipped: the
  running process doesn't see content pulled in underneath it and keeps
  extending its own copy of the conversation. (Claude opens transcripts per
  write rather than holding them open, so no writes are lost to a replaced file.)
  Busy on the sending side, skipped. Idle on the sending side, copied up to its
  last complete line, with a warning. The tool never stops or kills a session;
  it reports the pid, name and tmux pane so you can close it.
- **Divergence** is resolved by chaining the tails: shared prefix, then the
  tail whose first message is older, then the other tail with its root
  messages (those whose parent is in the shared prefix) re-parented onto the
  first tail's last message. Appending the tails side by side is not enough,
  since `--resume` would follow only the last one. Older first puts the newer
  work at the tip, and both machines build the same file. A tail's other
  roots (`/compact` boundaries, `parentUuid: null`) are left alone. Both
  originals are kept as `<name>.conflict-<host>-<hash8>` (`/resume` ignores
  them; named by content, so a persisting conflict is saved once). Files that
  can't be chained (not JSON, or no message in the other tail starts from the
  shared prefix) get a conflict copy instead, reported on stderr.
- **`history.jsonl`** is merged on every sync: union, sorted by `timestamp`,
  duplicates dropped.
- **Version mismatch** between the two Claude installs prints a warning and the
  sync continues.
- **Triggers:** manual `claude-sync sync <host>`. A SessionEnd hook (push the
  ended session, silently if the host is unreachable) and a `cs` wrapper (sync,
  then `claude --resume`) are both opt-in.

## Milestones

1. [x] Dry run: manifests, live check, prefix classification, plan output.
2. [x] Execute: rsync fast-forwards and new files; conflict copies; locking;
       idle-source snapshots; exclude `sessions-index.json`; version warning.
3. [x] Sync base state file; three-way for mutable files; deletions and trash;
       `history.jsonl` merge; `--config`.
4. [x] Chain diverged tails automatically. `resolve <uuid> --keep
       local|remote|merge` is deferred: the `.conflict` copies undo a merge by
       hand until a real case shows it's needed.
5. [x] Ship in the agent-tools package as `/usr/bin/claude-sync`.
6. [ ] Opt-in SessionEnd hook and `cs` wrapper.
7. [x] `mv OLD NEW`: rename a project directory and Claude's records of it.
       The project dirs of OLD and its subdirectories are renamed (a dir is
       checked against its sessions' first `cwd`, since the encoding is
       lossy), each record's top-level `cwd` and each `history.jsonl`
       `project` is rewritten, and so are `~/.claude.json`'s `projects` keys
       and `githubRepoPaths`. Paths inside messages and nested metadata stay
       as they were. Run on both machines before syncing, or a sync with
       `--all` sees the old files as deleted.

## Known gaps

From an adversarial review (October 2026); the rest of its findings are fixed.

- **Concurrent writers.** `history.jsonl` and `MEMORY.md` merges rewrite the
  file on both sides; lines another process appends on the remote between the
  scan and the push are lost. Likewise a session that starts after the liveness
  check can have its transcript replaced underneath it. The window is seconds.
  Fix: merge on the remote under its lock, and re-check liveness just before
  each write.
- **Diverged sync bases.** The base is now written on the peer before here, so
  a failed push leaves both on the older base; a failed local write after a
  successful push still leaves them apart. Fix: compare base generations at
  the start of a sync and treat disagreement as a conflict.
- **Symlinks inside `~/.claude`.** A symlinked project directory is followed,
  so trash and merges act on its target. Only someone already acting as the
  user can create one. Fix: refuse symlinks in every path component.
