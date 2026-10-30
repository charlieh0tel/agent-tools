#!/usr/bin/env python3
"""Run Claude Code as the account a directory belongs to.

Each account has its own Claude config dir (CLAUDE_CONFIG_DIR): ~/.claude for the
default account, ~/.claude-NAME for the others. Which directories belong to which
account is set in ~/.config/claude-user/config.
"""

import argparse
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from claude_sync import (
    CONFIG,
    account_dir,
    account_of,
    check_account,
    check_account_dir,
    claude_exe,
    load_user_config,
    resolve_dir,
    user_config_path,
    write_file,
)

ENV = "CLAUDE_CONFIG_DIR"


def tilde(path) -> str:
    home, path = str(Path.home()), str(path)
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


# -------------------------------------------------------------- config edits


def entry_key(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith(("#", "[")) or "=" not in s:
        return None
    return s.rpartition("=")[0].strip()


def set_entry(
    lines: list[str], section: str | None, matches: Callable[[str], bool], new: str | None
) -> list[str]:
    """lines with the entry of section whose key matches replaced by new, or removed
    if new is None; a new entry goes at the end of its section. Comments are kept."""
    out, current, last, header = [], None, None, None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1].strip()
            if current == section:
                header = len(out)
        elif current == section and (key := entry_key(line)) is not None:
            if matches(key):
                if new is not None:
                    out.append(new)
                    new = None
                continue
            last = len(out)
        out.append(line)
    if new is None:
        return out
    if last is not None:
        out.insert(last + 1, new)
    elif header is not None:
        out.insert(header + 1, new)
    elif section is None:
        out.insert(0, new)
    else:
        out += ([""] if out and out[-1].strip() else []) + [f"[{section}]", new]
    return out


def edit_config(section: str | None, matches: Callable[[str], bool], new: str | None) -> None:
    load_user_config()  # refuse to edit a config that doesn't parse
    path = user_config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(
        path, "".join(f"{line}\n" for line in set_entry(lines, section, matches, new)), 0o644
    )


def config_dir_arg(d: str) -> str:
    return check_account_dir(resolve_dir(d), "claude-user")


# ---------------------------------------------------------------- commands


def cmd_show(args) -> None:
    config = load_user_config()
    account, by = account_of(args.dir, config)
    why = f"from {tilde(by)}" if by else "default"
    print(f"{account or '-'}  {tilde(account_dir(account, config))}  ({why})")
    if ENV in os.environ:
        print(f"note: {ENV}={os.environ[ENV]} is set; run uses it")


def cmd_list(args) -> None:
    config = load_user_config()
    print(f"default  {config.default or '-'}  ~/.claude")
    for d, account in sorted(config.dirs.items()):
        print(f"{tilde(d)}  {account}  {tilde(account_dir(account, config))}")


def cmd_add(args) -> None:
    account = check_account(args.account, "claude-user")
    for d in [config_dir_arg(d) for d in args.dirs]:  # all checked before any is added
        edit_config("dirs", lambda key, d=d: resolve_dir(key) == d, f"{tilde(d)} = {account}")


def cmd_rm(args) -> None:
    dirs = [config_dir_arg(d) for d in args.dirs]
    config = load_user_config()
    if missing := [tilde(d) for d in dirs if d not in config.dirs]:
        sys.exit(f"claude-user: no entry for {', '.join(missing)}")
    for d in dirs:
        edit_config("dirs", lambda key, d=d: resolve_dir(key) == d, None)


def cmd_default(args) -> None:
    if args.account is None:
        print(load_user_config().default or "-")
        return
    account = check_account(args.account, "claude-user")
    edit_config(None, lambda key: key == "default", f"default = {account}")


def cmd_init(args) -> None:
    """Create an account's config dir with the CLAUDE.md, settings and skills of ~/.claude.
    Credentials, sessions and plugins stay behind: the new account logs in afresh."""
    config = load_user_config()
    account = check_account(args.account, "claude-user")
    dest, src = account_dir(account, config), account_dir(None, config)
    if dest == src:
        sys.exit(f"claude-user: {account} is the default account; it uses {tilde(src)}")
    if dest.exists():
        sys.exit(f"claude-user: {tilde(dest)} already exists")
    dest.mkdir(mode=0o700)
    for name in CONFIG:
        if (src / name).is_dir():
            shutil.copytree(src / name, dest / name, symlinks=True)
        elif (src / name).is_file():
            shutil.copy2(src / name, dest / name)
        else:
            continue
        print(f"copy      {tilde(src / name)} -> {tilde(dest / name)}")
    print(f"created   {tilde(dest)}; log in with: claude-user run -a {account}, then /login")


def run_args(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split run's own -a/--account option from the arguments for claude. argparse
    can't: it rejects the options it doesn't know, which here are claude's."""
    account = None
    if argv[:1] in (["-a"], ["--account"]) and len(argv) >= 2:
        account, argv = argv[1], argv[2:]
    elif argv and argv[0].startswith("--account="):
        account, argv = argv[0].partition("=")[2], argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    return account, argv


def account_env(account: str | None) -> dict[str, str]:
    """The environment to run claude in as account; None is the account of the cwd,
    unless CLAUDE_CONFIG_DIR is set, which is then kept."""
    env = dict(os.environ)
    if not account and ENV in env:
        return env
    config = load_user_config()
    account = check_account(account, "claude-user") if account else None
    account = account or account_of(os.getcwd(), config)[0]
    d = account_dir(account, config)
    if d == account_dir(None, config):
        # Set to ~/.claude, Claude would look for .claude.json in it, not in ~.
        env.pop(ENV, None)
    elif not d.is_dir():
        sys.exit(f"claude-user: {tilde(d)} doesn't exist; run claude-user init {account}")
    else:
        env[ENV] = str(d)
    return env


def find_claude() -> str:
    exe = claude_exe()
    if os.path.realpath(exe) == os.path.realpath(sys.argv[0]):
        sys.exit(f"claude-user: {exe} is claude-user itself; can't find claude")
    return exe


def cmd_run(args) -> None:
    env, exe = account_env(args.account), find_claude()
    try:
        os.execve(exe, ["claude", *args.args], env)
    except OSError as e:
        sys.exit(f"claude-user: can't run {exe}: {e.strerror}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="claude-user", description=__doc__.splitlines()[0])
    ap.set_defaults(func=cmd_show, dir=".")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("show", help="the account a directory belongs to (the default command)")
    p.add_argument("dir", nargs="?", default=".")
    p.set_defaults(func=cmd_show)
    # Listed for --help only: main() hands run's arguments to run_args.
    p = sub.add_parser("run", help="run claude as the account of the current directory")
    p.add_argument("-a", "--account", help="use this account instead")
    p.add_argument("args", nargs="*", help="arguments for claude")
    p = sub.add_parser("list", help="the default account and the directories of each account")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("add", help="make directories and everything in them belong to an account")
    p.add_argument("account")
    p.add_argument("dirs", nargs="*", default=["."], metavar="dir", help="default: the cwd")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("rm", help="remove directories' entries")
    p.add_argument("dirs", nargs="*", default=["."], metavar="dir", help="default: the cwd")
    p.set_defaults(func=cmd_rm)
    p = sub.add_parser("default", help="show or set the account that owns ~/.claude here")
    p.add_argument("account", nargs="?")
    p.set_defaults(func=cmd_default)
    p = sub.add_parser("init", help="create an account's config dir")
    p.add_argument("account")
    p.set_defaults(func=cmd_init)
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["run"]:
        account, rest = run_args(argv[1:])
        cmd_run(argparse.Namespace(account=account, args=rest))
        return
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
