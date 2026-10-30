import os
import subprocess
from pathlib import Path

import pytest

import agent_install
from agent_install import install_command


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "h o"  # a space, so quoting matters
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def test_claude(home):
    bin_dir = f"'{home}/.local/bin'"
    assert install_command("claude", None) == (
        f'curl -fsSL https://claude.ai/install.sh | env PATH={bin_dir}:"$PATH" bash'
    )
    assert install_command("claude", "2.1.310").endswith(" bash -s -- 2.1.310")
    assert install_command("claude", "stable").endswith(" bash -s -- stable")


def test_codex_gets_a_throwaway_home_and_its_real_dirs(home):
    cmd = install_command("codex", "0.47.0")
    assert cmd.startswith("curl -fsSL https://chatgpt.com/codex/install.sh | env ")
    assert 'HOME="$(mktemp -d)"' in cmd
    assert f"CODEX_INSTALL_DIR='{home}/.local/bin'" in cmd
    assert f"CODEX_HOME='{home}/.codex'" in cmd
    assert "CODEX_NON_INTERACTIVE=1" in cmd
    assert cmd.endswith(" sh -s -- --release 0.47.0")


def test_codex_keeps_codex_home(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/srv/codex")
    assert "CODEX_HOME=/srv/codex" in install_command("codex", None)


def test_agy_installs_only_the_latest():
    assert install_command("agy", None).endswith(" bash")
    with pytest.raises(SystemExit):
        install_command("agy", "1.4.10")


@pytest.mark.parametrize(
    ("agent", "version"),
    [("claude", "1.2"), ("claude", "1.2.3; rm -rf ~"), ("codex", "stable"), ("codex", "1.2.3-rc1")],
)
def test_bad_versions_are_refused(agent, version):
    with pytest.raises(SystemExit):
        install_command(agent, version)


@pytest.mark.parametrize("agent", sorted(agent_install.INSTALLERS))
def test_commands_parse_and_expand_in_sh(agent, home, tmp_path):
    """Run the command with curl, env and the shell replaced by a stub that prints
    its arguments: what sh would pass the installer, without running it."""
    stub = tmp_path / "stub"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    stub.chmod(0o755)
    cmd = (
        install_command(agent, None)
        .replace("curl ", f"{stub} ", 1)
        .replace("| env ", f"| {stub} ", 1)
    )
    env = dict(os.environ, TMPDIR=str(tmp_path))  # where $(mktemp -d) makes the codex HOME
    out = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True, check=True)
    args = out.stdout.splitlines()
    path = next(a for a in args if a.startswith("PATH="))
    assert path.startswith(f"PATH={home}/.local/bin:/")
    if agent == "codex":
        fake_home = next(a for a in args if a.startswith("HOME="))[5:]
        assert Path(fake_home).parent == tmp_path


def test_main_warns_that_an_installed_agy_is_left_alone(home, capsys):
    (home / ".local" / "bin" / "agy").write_text("")
    agent_install.main(["agy"])
    captured = capsys.readouterr()
    assert captured.out.startswith("curl ")
    assert "updates itself" in captured.err
