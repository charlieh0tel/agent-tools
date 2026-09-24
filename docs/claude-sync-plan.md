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
                     otherwise           -> merge tails (keep both originals as .conflict)
        write-once, differs              -> conflict copy
        mutable: only one side != base   -> take it
                 both changed            -> conflict copy
    merge history.jsonl
    rsync push list, rsync pull list
    save base
```

## Decisions

- **Deletions** are tracked against the sync base and applied to the other side.
  The deleted file is first moved to `~/.local/state/claude-sync/trash/`, and
  trash older than 30 days is cleared at the start of each sync. A file that
  was changed on one side and deleted on the other is kept.
- **Running sessions:** open on the receiving side, always skipped (rsync
  replaces the file and the running process keeps writing to the old one).
  Busy on the sending side, skipped. Idle on the sending side, copied up to its
  last complete line, with a warning. The tool never stops or kills a session;
  it reports the pid, name and tmux pane so you can close it.
- **Divergence** is resolved by merging the tails automatically, keeping both
  originals as `<name>.conflict-<host>-<hash8>` (`/resume` ignores them; named by content, so a persisting conflict is saved once), provided
  the milestone 4 test shows `--resume` handles merged transcripts. Until then,
  write a conflict copy and wait for `resolve <uuid> --keep local|remote|merge`.
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
3. [ ] Sync base state file; three-way for mutable files; deletions and trash;
       `history.jsonl` merge; `--config`.
4. [ ] Tail merge and `resolve`; test which branch `--resume` continues, then
       make auto-merge the default if it passes.
5. [ ] Install to `~/.local/bin` on both machines; opt-in SessionEnd hook and
       `cs` wrapper.

## Still to verify

- After a tail merge, which branch does `claude --resume` continue? (milestone 4)
- Does Claude rebuild `sessions-index.json` on its own?
