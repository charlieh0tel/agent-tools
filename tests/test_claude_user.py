import os
import subprocess
from pathlib import Path

import pytest

import claude_sync
import claude_user
from claude_sync import UserConfig, account_dir, account_of, parse_user_config


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path.resolve() / "home"
    (home / "src" / "impulse" / "flux").mkdir(parents=True)
    (home / "src" / "impulse-old").mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_USER_CONFIG", str(home / ".config" / "claude-user" / "config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


def config_text(home: Path) -> str:
    return (home / ".config" / "claude-user" / "config").read_text()


def write_config(home: Path, text: str) -> None:
    path = home / ".config" / "claude-user" / "config"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_longest_directory_wins_on_whole_components(home):
    config = parse_user_config(
        "default = work\n[dirs]\n~/src = personal\n~/src/impulse = work\n", "test"
    )
    assert account_of(str(home / "src" / "impulse" / "flux"), config) == (
        "work",
        str(home / "src" / "impulse"),
    )
    assert account_of(str(home / "src" / "impulse-old"), config) == ("personal", str(home / "src"))
    assert account_of(str(home / "src"), config) == ("personal", str(home / "src"))
    assert account_of(str(home), config) == ("work", None)
    assert account_of("/tmp", config) == ("work", None)


def test_account_dirs(home):
    config = UserConfig("work", {})
    assert account_dir("work", config) == home / ".claude"
    assert account_dir(None, config) == home / ".claude"
    assert account_dir("personal", config) == home / ".claude-personal"
    assert account_dir("work", UserConfig(None, {})) == home / ".claude-work"


@pytest.mark.parametrize(
    "text",
    [
        "default = ../x\n",
        "default = a/b\n",
        "[dirs]\n~/src = \n",
        "[accounts]\n",
        "colour = blue\n",
        "[dirs]\nno equals sign\n",
        "[dirs]\n~ = personal\n",
        "[dirs]\n/ = personal\n",
    ],
)
def test_bad_configs_are_refused(home, text):
    with pytest.raises(SystemExit):
        parse_user_config(text, "test")


def test_comments_and_blank_lines_are_ignored(home):
    config = parse_user_config("# mine\n\ndefault = work  \n[dirs]\n# x\n~/src=personal\n", "t")
    assert config == UserConfig("work", {str(home / "src"): "personal"})


def test_a_missing_config_is_empty(home):
    assert claude_sync.load_user_config() == UserConfig(None, {})


def test_add_rm_and_default_keep_comments(home):
    write_config(home, "# my accounts\n[dirs]\n# personal stuff\n~/src = personal\n")
    claude_user.main(["add", "work", str(home / "src" / "impulse")])
    claude_user.main(["add", "home", "~/src"])  # replaces the entry for ~/src
    claude_user.main(["default", "work"])
    assert config_text(home) == (
        "default = work\n"
        "# my accounts\n[dirs]\n# personal stuff\n~/src = home\n~/src/impulse = work\n"
    )
    claude_user.main(["rm", "~/src"])
    assert "~/src = home" not in config_text(home)
    with pytest.raises(SystemExit):
        claude_user.main(["rm", "~/src"])


def test_add_creates_the_config(home):
    claude_user.main(["add", "personal", "~/src"])
    assert config_text(home) == "[dirs]\n~/src = personal\n"


def test_add_and_rm_several_dirs_or_the_cwd(home, monkeypatch):
    monkeypatch.chdir(home / "src" / "impulse")
    claude_user.main(["add", "work"])
    claude_user.main(["add", "personal", "~/src", "~/src/impulse-old"])
    assert config_text(home) == (
        "[dirs]\n~/src/impulse = work\n~/src = personal\n~/src/impulse-old = personal\n"
    )
    with pytest.raises(SystemExit, match="no entry for ~/src/impulse/flux"):
        claude_user.main(["rm", "~/src", "~/src/impulse/flux"])
    assert "~/src = personal" in config_text(home)  # nothing removed
    claude_user.main(["rm", "~/src", "~/src/impulse-old"])
    claude_user.main(["rm"])
    assert config_text(home) == "[dirs]\n"


def test_add_checks_every_dir_before_adding_any(home):
    with pytest.raises(SystemExit):
        claude_user.main(["add", "personal", "~/src", "~"])
    assert not (home / ".config" / "claude-user" / "config").exists()


@pytest.mark.parametrize("d", ["~", "/", "~/..", "~/src/../.."])
def test_add_refuses_home_and_above(home, d):
    with pytest.raises(SystemExit):
        claude_user.main(["add", "personal", d])


def test_show(home, capsys, monkeypatch):
    write_config(home, "default = work\n[dirs]\n~/src = personal\n")
    monkeypatch.chdir(home / "src" / "impulse")
    claude_user.main([])
    assert capsys.readouterr().out == "personal  ~/.claude-personal  (from ~/src)\n"
    claude_user.main(["show", str(home)])
    assert capsys.readouterr().out == "work  ~/.claude  (default)\n"


def test_init_copies_config_but_not_credentials(home):
    (home / ".claude" / "settings.json").write_text("{}")
    (home / ".claude" / ".credentials.json").write_text("secret")
    (home / ".claude" / "skills" / "s").mkdir(parents=True)
    (home / ".claude" / "skills" / "s" / "SKILL.md").write_text("skill")
    (home / ".claude" / "projects").mkdir()
    claude_user.main(["init", "personal"])
    dest = home / ".claude-personal"
    assert sorted(p.name for p in dest.iterdir()) == ["settings.json", "skills"]
    assert (dest / "skills" / "s" / "SKILL.md").read_text() == "skill"
    assert dest.stat().st_mode & 0o777 == 0o700
    with pytest.raises(SystemExit):
        claude_user.main(["init", "personal"])


def test_init_refuses_the_default_account(home):
    write_config(home, "default = work\n")
    with pytest.raises(SystemExit):
        claude_user.main(["init", "work"])


@pytest.fixture
def execs(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_user, "claude_exe", lambda: "/opt/claude")
    monkeypatch.setattr(os, "execve", lambda exe, argv, env: calls.append((exe, argv, env)))
    return calls


def test_run_uses_the_directory_account(home, execs, monkeypatch):
    write_config(home, "default = work\n[dirs]\n~/src = personal\n")
    (home / ".claude-personal").mkdir()
    monkeypatch.chdir(home / "src" / "impulse")
    claude_user.main(["run", "--resume", "-p", "hi"])
    exe, argv, env = execs.pop()
    assert (exe, argv) == ("/opt/claude", ["claude", "--resume", "-p", "hi"])
    assert env["CLAUDE_CONFIG_DIR"] == str(home / ".claude-personal")


def test_run_leaves_the_default_account_unset(home, execs, monkeypatch):
    write_config(home, "default = work\n[dirs]\n~/src = personal\n")
    monkeypatch.chdir(home)
    claude_user.main(["run", "-a", "work", "--", "-a"])
    _, argv, env = execs.pop()
    assert argv == ["claude", "-a"]
    assert "CLAUDE_CONFIG_DIR" not in env


def test_run_keeps_an_explicit_config_dir(home, execs, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    claude_user.main(["run"])
    assert execs.pop()[2]["CLAUDE_CONFIG_DIR"] == "/elsewhere"


def test_run_refuses_an_account_without_a_dir(home, execs):
    with pytest.raises(SystemExit, match="claude-user init personal"):
        claude_user.main(["run", "--account=personal"])
    assert not execs


def test_run_args():
    assert claude_user.run_args([]) == (None, [])
    assert claude_user.run_args(["-a", "x", "-c"]) == ("x", ["-c"])
    assert claude_user.run_args(["--account=x", "--", "-a"]) == ("x", ["-a"])
    assert claude_user.run_args(["-c", "-a", "x"]) == (None, ["-c", "-a", "x"])


def logged_in(path: Path, email: str) -> None:
    path.write_text(f'{{"oauthAccount": {{"emailAddress": "{email}"}}}}')


def session(project: Path, cwd: Path) -> None:
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(f'{{"cwd": "{cwd}"}}\n')


def test_diagnose_a_healthy_setup(home, capsys):
    write_config(home, "default = personal\n[dirs]\n~/src/impulse = work\n")
    (home / ".claude-work").mkdir()
    logged_in(home / ".claude.json", "me@home")
    logged_in(home / ".claude-work" / ".claude.json", "me@work")
    session(home / ".claude-work" / "projects" / "flux", home / "src" / "impulse" / "flux")
    claude_user.main(["diagnose"])
    assert capsys.readouterr().out == (
        "~/.claude  personal\n"
        "~/.claude-work  work\n"
        "  ~/src/impulse/flux  work\n"
        "\n"
        "ok        personal  ~/.claude: logged in as me@home\n"
        "ok        work  ~/.claude-work: logged in as me@work\n"
    )


def test_diagnose_finds_problems(home, capsys):
    write_config(home, "default = personal\n[dirs]\n~/src/impulse = work\n~/src = lab\n")
    (home / ".claude-work").mkdir()
    (home / ".claude-personal" / "skills").mkdir(parents=True)  # init before default
    (home / ".claude-old" / "projects").mkdir(parents=True)
    logged_in(home / ".claude.json", "me@work")
    logged_in(home / ".claude-work" / ".claude.json", "me@work")
    session(home / ".claude" / "projects" / "flux", home / "src" / "impulse" / "flux")
    session(home / ".claude" / "projects" / "gone", home / "src" / "gone")
    (home / ".claude" / "projects" / claude_sync.encode(str(home / "src" / "impulse-old"))).mkdir()
    (home / ".claude" / "projects" / "-nowhere").mkdir()
    with pytest.raises(SystemExit):
        claude_user.main(["diagnose"])
    assert capsys.readouterr().out.splitlines() == [
        "~/.claude  personal",
        "  ? -nowhere          ?  directory unknown",
        "  ~/src/impulse-old   lab  from its name; belongs in ~/.claude-lab",
        "  ~/src/impulse/flux  work  belongs in ~/.claude-work",
        "  ~/src/gone          lab  gone; belongs in ~/.claude-lab",
        "~/.claude-old  (no account)",
        "~/.claude-personal  (no account)",
        "~/.claude-work  work",
        "",
        "problem   lab  ~/.claude-lab: missing; create it with: claude-user init lab",
        "ok        personal  ~/.claude: logged in as me@work",
        "problem   personal  ~/.claude: holds 2 projects of lab; "
        "move them with: claude-sync split --all",
        "problem   personal  ~/.claude: holds 1 projects of work; "
        "move them with: claude-sync split --all",
        "ok        work  ~/.claude-work: logged in as me@work",
        "problem   personal, work are all logged in as me@work",
        "problem   ~/.claude-old: no account uses it; it holds sessions or logins",
        "problem   ~/.claude-personal: personal is the default account, which uses ~/.claude;"
        " it holds only settings copied by init",
    ]


def test_diagnose_an_account_that_is_not_logged_in(home, capsys):
    write_config(home, "[dirs]\n~/src = lab\n")
    (home / ".claude-lab").mkdir()
    session(home / ".claude-lab" / "projects" / "home", home)
    with pytest.raises(SystemExit):
        claude_user.main(["diagnose"])
    assert capsys.readouterr().out.splitlines() == [
        "~/.claude  (unnamed default)",
        "~/.claude-lab  lab",
        "  ~  (default)  belongs in ~/.claude",
        "",
        "problem   (unnamed default)  ~/.claude: not logged in; log in with: claude auth login",
        "problem   lab  ~/.claude-lab: not logged in; log in with: claude-user login lab",
        "problem   lab  ~/.claude-lab: holds 1 projects of the default account; "
        "move them with: claude-sync split --all --from lab",
    ]


def test_login_runs_in_an_empty_temporary_dir(home, monkeypatch):
    write_config(home, "default = work\n")
    (home / ".claude-personal").mkdir()
    monkeypatch.chdir(home / "src" / "impulse")
    monkeypatch.setattr(claude_user, "claude_exe", lambda: "/opt/claude")
    calls = []

    def run(argv, cwd, env):
        calls.append((argv, sorted(os.listdir(cwd)), env.get("CLAUDE_CONFIG_DIR"), cwd))
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as e:
        claude_user.main(["login", "personal", "--email", "me@home"])
    assert e.value.code == 3
    argv, listing, config_dir, cwd = calls.pop()
    assert argv == ["/opt/claude", "auth", "login", "--email", "me@home"]
    assert (listing, config_dir) == ([], str(home / ".claude-personal"))
    assert not os.path.exists(cwd)
    with pytest.raises(SystemExit, match="claude-user init lab"):
        claude_user.main(["login", "lab"])


def test_diagnose_finds_a_memory_only_project_from_history(home, capsys):
    gone = home / "src" / "old-name"
    (home / ".claude" / "projects" / claude_sync.encode(str(gone)) / "memory").mkdir(parents=True)
    (home / ".claude" / "history.jsonl").write_text(f'{{"project": "{gone}"}}\n')
    with pytest.raises(SystemExit):
        claude_user.main(["diagnose"])
    assert "  ~/src/old-name  (default)  from history; gone" in capsys.readouterr().out
