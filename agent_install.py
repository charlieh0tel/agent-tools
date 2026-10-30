#!/usr/bin/env python3
"""Print the command that installs a coding agent's native build, to pipe to sh.

The vendors' own installers are used as is, run with settings that keep them out
of shell startup files. Codex's appends a PATH block to ~/.bashrc or ~/.zshrc
unless its bin dir is on PATH and no other codex is found, so it gets a throwaway
HOME for that edit and its real install dirs explicitly. Claude's and agy's only
suggest the PATH line, which having ~/.local/bin on PATH silences.
"""

import argparse
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Installer:
    url: str
    shell: str
    versions: re.Pattern | None  # the versions it accepts; None: only the latest
    version_args: tuple[str, ...] = ()  # put before the version


INSTALLERS = {
    "claude": Installer(
        "https://claude.ai/install.sh",
        "bash",
        re.compile(r"stable|latest|[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?"),
    ),
    "codex": Installer(
        "https://chatgpt.com/codex/install.sh",
        "sh",
        re.compile(
            r"latest|(rust-)?v?[0-9]+\.[0-9]+\.[0-9]+(-alpha(\.[0-9]+){0,2}|-beta(\.[0-9]+)?)?"
        ),
        ("--release",),
    ),
    "agy": Installer("https://antigravity.google/cli/install.sh", "bash", None),
}


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def install_env(agent: str) -> list[str]:
    """Assignments for env(1). $PATH and $(mktemp -d) are left for sh to expand."""
    env = [f'PATH={shlex.quote(str(bin_dir()))}:"$PATH"']
    if agent == "codex":
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        env += [
            'HOME="$(mktemp -d)"',
            f"CODEX_INSTALL_DIR={shlex.quote(str(bin_dir()))}",
            f"CODEX_HOME={shlex.quote(codex_home)}",
            "CODEX_NON_INTERACTIVE=1",
        ]
    return env


def install_command(agent: str, version: str | None) -> str:
    installer = INSTALLERS[agent]
    args = []
    if version is not None:
        if installer.versions is None:
            sys.exit(f"agent-install: {agent}'s installer only installs the latest version")
        if not installer.versions.fullmatch(version):
            sys.exit(f"agent-install: {version!r} is not a {agent} version")
        args = ["-s", "--", *installer.version_args, version]
    run = ["env", *install_env(agent), shlex.join([installer.shell, *args])]
    return f"curl -fsSL {installer.url} | {' '.join(run)}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agent-install", description=__doc__.splitlines()[0])
    ap.add_argument("agent", choices=sorted(INSTALLERS))
    ap.add_argument("version", nargs="?", help="version to install (default: the latest)")
    args = ap.parse_args(argv)
    print(install_command(args.agent, args.version))
    if args.agent == "agy" and (bin_dir() / "agy").exists():
        print(
            f"agent-install: {bin_dir() / 'agy'} exists, so its installer will do nothing; "
            "agy updates itself",
            file=sys.stderr,
        )
    if sys.stdout.isatty():
        print(f"agent-install: to run it: agent-install {args.agent} | sh", file=sys.stderr)


if __name__ == "__main__":
    main()
