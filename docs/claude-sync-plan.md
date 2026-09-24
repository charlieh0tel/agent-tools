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

`<enc>` is the cwd with every non-alphanumeric character replaced by `-`.

Scope is the current directory's project by default, or everything with `--all`.

Not synced: `sessions/`, `session-env/`, `shell-snapshots/` (per-machine
process state), caches, logs, telemetry, and config (`settings.json`,
`CLAUDE.md`, `skills/`, `plugins/`).

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
    for path in L ∪ R:
        session live on either side      -> skip, report
        one side only                    -> copy
        same hash                        -> nothing
        append-only: one is a prefix     -> fast-forward
                     otherwise           -> conflict copy
        write-once, differs              -> conflict copy
        mutable: only one side != base   -> take it
                 both changed            -> conflict copy
    rsync push list, rsync pull list
    save base
```

Conflict copies are `<name>.conflict-<host>-<ts>`; `/resume` ignores them.
`resolve <uuid> --keep local|remote|merge` settles a diverged session.

Running sessions: if the receiving side has the session open, it is always
blocked, because rsync replaces the file and the running process keeps writing
to the old one. If the sending side has it open, it is skipped by default.
`--stop` ends idle sessions (`/exit` via tmux, else SIGTERM) and never touches
busy ones.

## Milestones

1. [x] Dry run: manifests, live check, prefix classification, plan output.
2. [ ] Execute: rsync fast-forwards and new files; write conflict copies; locking.
3. [ ] Sync base state file; three-way for mutable files; `history.jsonl` merge.
4. [ ] `resolve`, including an experimental tail merge; test which branch `--resume` continues.
5. [ ] `--stop`; SessionEnd hook; `cs` wrapper; install to `~/.local/bin`.

## Open questions

- Merge: after a tail merge, which branch does `claude --resume` continue?
- `--stop`: does Claude exit cleanly on SIGTERM? (laptop sessions have no tmux pane)
- `sessions-index.json`: exclude it, or does Claude need it synced?
- Deletions: Claude Code deletes old transcripts after `cleanupPeriodDays`; without
  deletion tracking, sync brings them back from the other side.
- Idle sessions on the sending side: skip (current), or allow a snapshot copy?
- Config sync (`--config`): wanted at all?
- `history.jsonl`: in the default per-project scope, or only with `--all`?
- Claude versions differ between machines (local 2.1.282, laptop not checked)
  -- transcript format compatibility.
