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

## Install (Debian/Ubuntu)

Packaged in [charlieh0tel/apt-repo](https://github.com/charlieh0tel/apt-repo):

```
curl -fsSL https://charlieh0tel.github.io/apt-repo/public.key | sudo gpg --dearmor -o /usr/share/keyrings/charlieh0tel.gpg
echo "deb [signed-by=/usr/share/keyrings/charlieh0tel.gpg] https://charlieh0tel.github.io/apt-repo bookworm main" | sudo tee /etc/apt/sources.list.d/charlieh0tel.list
sudo apt-get update && sudo apt-get install agent-tools
```

Dev: `uv run ruff format && uv run ruff check && uv run pytest`.
Deb: `dpkg-buildpackage -us -uc -b`; CI releases on `v*` tags.
