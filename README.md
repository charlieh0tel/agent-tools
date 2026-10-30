# agent-tools

## agent-install

Prints the command that installs a coding agent's native build with its
vendor's installer, for you to read and pipe to `sh`. The installer is run so
it leaves `~/.bashrc` and friends alone: Codex's would otherwise append a PATH
block, so it gets a throwaway `HOME` for that edit.

```
agent-install codex            # print the command
agent-install codex | sh       # run it
agent-install claude 2.1.310   # a given version (claude, codex)
agent-install agy              # Antigravity CLI, Google's successor to Gemini CLI
```

All three install into `~/.local/bin`, which must be on your PATH.

## agent-ps

Lists running Claude Code and Codex agents with pid, status, tty, tmux pane
and working directory.

```
./agent_ps               # table
./agent_ps --json        # machine-readable
./agent_ps --cwd-only    # unique cwds
./agent_ps --kind codex  # filter
./agent_ps --tree        # show child processes
./agent_ps --wide        # do not truncate INFO to the terminal width
```

Plain Python 3, no dependencies.

```
$ agent-ps
AGENT   PID      STATUS  AGE    TTY    TMUX      CWD              INFO
claude  3218277  busy    16m    pts/0  -         ~/src/apt-repo   apt-repo-72
claude  2842282  busy    3h46m  pts/4  flux:0.0  ~/src/flux       flux --resume
claude  2843101  idle    3h45m  pts/9  api:0.0   ~/src/flux-api   flux-api --resume
```

## claude-sync

Two-way sync of Claude Code sessions with another machine over ssh and rsync,
so a session can be continued with `claude --resume` on either one. Both
machines need the same home directory and project paths; the other one needs
only `python3`, `rsync` and `flock`, since the script sends itself over ssh.

```
claude-sync sync laptop -n       # dry run: print the plan
claude-sync sync laptop          # this project's sessions + prompt history
claude-sync sync laptop --all    # every project
```

`claude-sync mv OLD NEW` renames a project directory and updates Claude's
records of it (project dirs, transcript `cwd`s, `history.jsonl`,
`~/.claude.json`), so its sessions still resume from the new path. Run it on
both machines before the next sync.

A session continued on both machines is chained into one conversation, with
both originals kept as `.conflict-*` copies. Sessions open on the receiving
side are skipped. See `claude-sync(1)` and
[docs/claude-sync-plan.md](docs/claude-sync-plan.md) for the rules.

## claude-user

Runs Claude Code as the account a directory belongs to, so work and personal
logins, sessions and history stay apart. Each account has its own config dir
(`CLAUDE_CONFIG_DIR`): `~/.claude` for this machine's default account,
`~/.claude-NAME` for the others.

```
claude-user default work              # ~/.claude is the work account here
claude-user init personal             # create ~/.claude-personal with its settings
claude-user login personal            # log it in, from an empty temp dir
claude-user add personal ~/src        # ~/src and everything in it is personal...
claude-user add work ~/src/impulse    # ...except this: the longest match wins
claude-user                           # which account applies here, and why
claude-user run --resume              # claude as that account
alias claude='claude-user run'
claude-user diagnose                  # missing logins, stray dirs, misplaced projects
```

The settings live in `~/.config/claude-user/config`. See `claude-user(1)`.
`claude-sync` syncs and moves the sessions of the project's account, and the
other machine finds that account's directory from its own config.
`claude-sync split --all` sorts a `~/.claude` that held every account: each
project's sessions, prompts and settings move to the account it belongs to.

## Install (Debian/Ubuntu)

Packaged in [charlieh0tel/apt-repo](https://github.com/charlieh0tel/apt-repo):

```
curl -fsSL https://charlieh0tel.github.io/apt-repo/public.key | sudo gpg --dearmor -o /usr/share/keyrings/charlieh0tel.gpg
echo "deb [signed-by=/usr/share/keyrings/charlieh0tel.gpg] https://charlieh0tel.github.io/apt-repo bookworm main" | sudo tee /etc/apt/sources.list.d/charlieh0tel.list
sudo apt-get update && sudo apt-get install agent-tools
```

Dev: `uv run ruff format && uv run ruff check && uv run pytest`.
Deb: `dpkg-buildpackage -us -uc -b`; CI releases on `v*` tags (see [RELEASING.md](RELEASING.md)).
