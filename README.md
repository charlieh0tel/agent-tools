# agent-tools

## agent-ps

Lists running Claude Code and Codex agents with pid, status, tty, tmux pane
and working directory.

```
./agent_ps               # table
./agent_ps --json        # machine-readable
./agent_ps --cwd-only    # unique cwds
./agent_ps --kind codex  # filter
./agent_ps --tree        # show child processes
```

Plain Python 3, no dependencies. `uv run agent-ps` also works.

Dev: `uv run ruff format && uv run ruff check && uv run pytest`.
Deb: `dpkg-buildpackage -us -uc -b`; CI releases on `v*` tags.
