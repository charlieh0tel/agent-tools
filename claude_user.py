#!/usr/bin/env python3
"""Run Claude Code as the account a directory belongs to.

Each account has its own Claude config dir (CLAUDE_CONFIG_DIR): ~/.claude for the
default account, ~/.claude-NAME for the others. Which directories belong to which
account is set in ~/.config/claude-user/config.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from claude_sync import (
    ACCOUNT,
    CONFIG,
    account_dir,
    account_of,
    check_account,
    check_account_dir,
    claude_exe,
    claude_json,
    load_user_config,
    project_home,
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
    print(f"created   {tilde(dest)}; log in with: claude-user login {account}")


def login(d: Path) -> str | None:
    """The account Claude in config dir d is logged in as, or None if it isn't."""
    try:
        account = json.loads(claude_json(d).read_text()).get("oauthAccount") or {}
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"claude-user: can't read {tilde(claude_json(d))}: {e}")
    email, org = account.get("emailAddress"), account.get("organizationName")
    return f"{email} ({org})" if email and org else email


def stray_dirs(config) -> list[Path]:
    """~/.claude-NAME dirs that are no account's config dir under this config."""
    used = {account_dir(a, config) for a in {config.default, *config.dirs.values()}}
    return sorted(
        d
        for d in Path.home().glob(".claude-*")
        if d.is_dir() and ACCOUNT.fullmatch(d.name[len(".claude-") :]) and d not in used
    )


def held_projects(d: Path) -> list[tuple[Path, str | None, bool]]:
    """Config dir d's project dirs, each with the directory it is for and whether that
    was found by its name alone; the directory is None if nothing tells."""
    return [
        (proj, *project_home(proj))
        for proj in sorted((d / "projects").glob("*"))
        if proj.is_dir() and not proj.is_symlink()
    ]


def claude_dirs(config) -> list[Path]:
    """The config dirs here: the accounts' that exist and the stray ones."""
    accounts = {account_dir(a, config) for a in {None, config.default, *config.dirs.values()}}
    return sorted({d for d in accounts if d.is_dir()} | set(stray_dirs(config)))


def inventory(config, held: dict[Path, list]) -> list[str]:
    """For each config dir, the directories of its projects and the account each
    belongs to, marking those that belong in another config dir."""
    owners = {account_dir(a, config): a for a in config.dirs.values()}
    owners[account_dir(None, config)] = config.default or "(unnamed default)"
    out = []
    for d, projects in held.items():
        out.append(f"{tilde(d)}  {owners.get(d, '(no account)')}")
        rows = []
        for proj, cwd, by_name in projects:
            if cwd is None:
                rows.append((f"? {proj.name}", "?", "no session says whose it is"))
                continue
            to = account_of(cwd, config)[0]
            notes = ["from its name"] if by_name else []
            notes += [] if os.path.isdir(cwd) else ["gone"]
            if (should := account_dir(to, config)) != d:
                notes.append(f"belongs in {tilde(should)}")
            rows.append((tilde(cwd), to or "(default)", "; ".join(notes)))
        width = max((len(where) for where, _, _ in rows), default=0)
        out += [f"  {where:<{width}}  {to}  {note}".rstrip() for where, to, note in rows]
    return out


def diagnose(config, held: dict[Path, list]) -> list[tuple[bool, str]]:
    """(ok, message) for each account's config dir, login and projects, and for each
    ~/.claude-NAME dir no account uses."""
    out, logins = [], {}
    for account in sorted({config.default, *config.dirs.values()}, key=lambda a: a or ""):
        d = account_dir(account, config)
        name = f"{account or '(unnamed default)'}  {tilde(d)}"
        if not d.is_dir():
            out.append((False, f"{name}: missing; create it with: claude-user init {account}"))
            continue
        if who := login(d):
            out.append((True, f"{name}: logged in as {who}"))
            logins.setdefault(who, []).append(account or "default")
        else:
            how = f"claude-user login {account}" if account else "claude auth login"
            out.append((False, f"{name}: not logged in; log in with: {how}"))
        counts = {}
        for _, cwd, _ in held.get(d, []):
            if cwd is not None and account_dir(to := account_of(cwd, config)[0], config) != d:
                counts[to] = counts.get(to, 0) + 1
        split = "claude-sync split --all" + (
            f" --from {account}" if account != config.default else ""
        )
        for to, n in sorted(counts.items(), key=lambda item: item[0] or ""):
            whose = to or "the default account"
            out.append((False, f"{name}: holds {n} projects of {whose}; move them with: {split}"))
    for who, accounts in logins.items():
        if len(accounts) > 1:
            out.append((False, f"{', '.join(accounts)} are all logged in as {who}"))
    for d in stray_dirs(config):
        name = d.name[len(".claude-") :]
        why = (
            f"{name} is the default account, which uses ~/.claude"
            if name == config.default
            else "no account uses it"
        )
        extra = {p.name for p in d.iterdir()} - set(CONFIG)
        what = "holds only settings copied by init" if not extra else "holds sessions or logins"
        out.append((False, f"{tilde(d)}: {why}; it {what}"))
    if ENV in os.environ:
        out.append((True, f"{ENV}={os.environ[ENV]} is set; run uses it without -a"))
    return out


def cmd_diagnose(args) -> None:
    config = load_user_config()
    held = {d: held_projects(d) for d in claude_dirs(config)}
    for line in inventory(config, held):
        print(line)
    findings = diagnose(config, held)
    if findings:
        print()
    for ok, message in findings:
        print(f"{'ok' if ok else 'problem':<9} {message}")
    if not all(ok for ok, _ in findings):
        sys.exit(1)


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


def cmd_login(args) -> None:
    """Log account in from an empty temporary directory, so no project of the account
    gets a record of it."""
    env, exe = account_env(check_account(args.account, "claude-user")), find_claude()
    with tempfile.TemporaryDirectory(prefix="claude-user-") as d:
        try:
            r = subprocess.run([exe, "auth", "login", *args.args], cwd=d, env=env)
        except OSError as e:
            sys.exit(f"claude-user: can't run {exe}: {e.strerror}")
    sys.exit(r.returncode)


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
    p = sub.add_parser("login", help="log an account in, from an empty temporary directory")
    p.add_argument("account")
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments for claude auth login")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("diagnose", help="check the accounts' config dirs, logins and projects")
    p.set_defaults(func=cmd_diagnose)
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["run"]:
        account, rest = run_args(argv[1:])
        cmd_run(argparse.Namespace(account=account, args=rest))
        return
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
