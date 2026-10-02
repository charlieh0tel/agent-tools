from __future__ import annotations

import agent_ps
from agent_ps import (
    Proc,
    TmuxPane,
    agent_args,
    classify,
    collect,
    etime_seconds,
    format_table,
    human_age,
    one_line,
    tmux_target,
    truncate,
)

CODEX_BIN = (
    "/home/u/.nvm/versions/node/v22/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
)


def proc(
    pid: int, ppid: int, comm: str, argv: list[str], exe: str = "", etime: str = "01:00"
) -> Proc:
    return Proc(
        pid=pid, ppid=ppid, user="u", tty="pts/1", etime=etime, comm=comm, argv=argv, exe=exe
    )


def claude(pid: int, ppid: int = 1, *extra: str) -> Proc:
    return proc(
        pid, ppid, "claude", ["claude", *extra], exe="/home/u/.local/share/claude/versions/2.1.287"
    )


def test_classify_claude_variants():
    assert classify(claude(10)) == "claude"
    assert (
        classify(
            proc(11, 1, "node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
        )
        == "claude"
    )
    assert (
        classify(proc(12, 1, "node", ["node", "/home/u/.local/bin/claude", "--resume"])) == "claude"
    )


def test_classify_codex_variants():
    assert (
        classify(proc(20, 1, "node", ["node", "/home/u/.nvm/versions/node/v22/bin/codex"]))
        == "codex"
    )
    assert classify(proc(21, 20, "codex", [CODEX_BIN], exe=CODEX_BIN)) == "codex"
    assert classify(proc(22, 21, "codex-code-mode-", [CODEX_BIN + "-code-mode-host"])) == "codex"


def test_classify_ignores_claude_self_exec_helpers():
    claude_exe = "/home/u/.local/share/claude/versions/2.1.287"
    assert classify(proc(33, 10, "claude", ["ugrep", "-G", "-E", "^error"], exe=claude_exe)) is None
    assert classify(proc(34, 10, "ugrep", ["ugrep", "x"], exe=claude_exe)) is None


def test_classify_falls_back_to_exe_without_argv():
    assert (
        classify(proc(35, 1, "claude", [], exe="/home/u/.local/share/claude/versions/2.1.287"))
        == "claude"
    )
    assert classify(proc(36, 1, "codex", [], exe=CODEX_BIN)) == "codex"
    assert (
        classify(proc(37, 1, "ugrep", [], exe="/home/u/.local/share/claude/versions/2.1.287"))
        is None
    )


def test_classify_ignores_agent_paths_in_arguments():
    assert classify(proc(40, 1, "rg", ["rg", "@openai/codex", "/work"])) is None
    assert (
        classify(proc(41, 1, "vim", ["vim", "node_modules/@anthropic-ai/claude-code/cli.js"]))
        is None
    )
    codex_mentioning_claude = [CODEX_BIN, "exec", "cat @anthropic-ai/claude-code/cli.js"]
    assert classify(proc(42, 1, "codex", codex_mentioning_claude, exe=CODEX_BIN)) == "codex"


def test_claude_session_info_rejects_non_object_json(monkeypatch, tmp_path, capsys):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(agent_ps, "HOME", tmp_path)
    (sessions / "1.json").write_text("null")
    (sessions / "2.json").write_text("[]")
    (sessions / "3.json").write_text("{not json")
    (sessions / "4.json").write_text(
        '{"pid": 4, "name": "ok", "kind": "interactive", "tmux": "s:@0.%0"}'
    )
    assert agent_ps.claude_session_info(1) == {}
    assert agent_ps.claude_session_info(2) == {}
    assert agent_ps.claude_session_info(3) == {}
    assert agent_ps.claude_session_info(5) == {}
    assert agent_ps.claude_session_info(4) == {
        "name": "ok",
        "session_kind": "interactive",
        "claude_tmux_id": "s:@0.%0",
    }
    assert capsys.readouterr().err.count("warning:") == 3


def test_classify_ignores_shells_and_unrelated():
    snapshot = "source /home/u/.claude/shell-snapshots/snap.sh && eval 'ls'"
    assert classify(proc(30, 10, "bash", ["/bin/bash", "-c", snapshot])) is None
    assert classify(proc(31, 1, "node", ["node", "/srv/app/server.js"])) is None
    assert classify(proc(32, 1, "vim", ["vim", "codex-notes.md"])) is None


def test_agent_args_strips_interpreter_and_program():
    assert agent_args("claude", ["claude", "--resume"]) == "--resume"
    assert agent_args("codex", ["node", "/x/bin/codex", "exec", "hi"]) == "exec hi"
    assert agent_args("claude", ["claude"]) == ""


def test_etime_seconds():
    assert etime_seconds("00:44") == 44
    assert etime_seconds("01:30:46") == 5446
    assert etime_seconds("3-18:55:07") == 3 * 86400 + 18 * 3600 + 55 * 60 + 7
    assert etime_seconds("garbage") is None


def test_human_age():
    assert human_age(None) == "?"
    assert human_age(44) == "44s"
    assert human_age(5446) == "1h30m"
    assert human_age(3 * 86400 + 7200) == "3d02h"


def sample_procs() -> list[Proc]:
    return [
        claude(100, 1, "--resume"),
        proc(200, 2, "node", ["node", "/home/u/.nvm/versions/node/v22/bin/codex"]),
        proc(201, 200, "codex", [CODEX_BIN], exe=CODEX_BIN),
        proc(202, 201, "codex-code-mode-", [CODEX_BIN + "-code-mode-host"]),
        proc(300, 100, "bash", ["/bin/bash", "-c", "ls"]),
    ]


def fake_cwd(pid: int) -> str | None:
    return {100: "/work/a", 200: None, 201: "/work/b", 202: "/work/b"}.get(pid)


def fake_session(pid: int) -> dict:
    return (
        {"name": "a-session", "status": "busy", "session_kind": "interactive"} if pid == 100 else {}
    )


PANES = [TmuxPane(target="flux:0.0", tty="/dev/pts/1", pid=1)]


def test_tmux_target_by_tty_and_by_ancestry():
    ppid_of = {100: 1, 200: 2, 201: 200, 500: 7, 7: 1}
    assert tmux_target(proc(100, 1, "claude", ["claude"]), PANES, ppid_of) == "flux:0.0"
    shell_pane = [TmuxPane(target="work:1.2", tty="/dev/pts/9", pid=7)]
    detached = Proc(pid=500, ppid=7, user="u", tty="?", etime="00:01", comm="codex", argv=["codex"])
    assert tmux_target(detached, shell_pane, ppid_of) == "work:1.2"
    assert tmux_target(detached, [], ppid_of) is None


def test_collect_collapses_codex_chain_and_fills_cwd_from_child():
    agents = collect(sample_procs(), cwd_of=fake_cwd, session_info=fake_session, panes=PANES)
    assert agents[0].tmux == "flux:0.0"
    assert [(a.kind, a.pid) for a in agents] == [("claude", 100), ("codex", 200)]
    codex = agents[1]
    assert codex.cwd == "/work/b"
    assert codex.extra["child_pids"] == [201]
    assert agents[0].extra["name"] == "a-session"
    assert agents[0].kind == "claude"


def test_collect_tree_shows_children():
    agents = collect(
        sample_procs(), show_tree=True, cwd_of=fake_cwd, session_info=fake_session, panes=[]
    )
    assert [(a.pid, a.child) for a in agents if a.kind == "codex"] == [
        (200, False),
        (201, True),
        (202, True),
    ]


def test_collect_kind_filter():
    agents = collect(
        sample_procs(), kinds={"codex"}, cwd_of=fake_cwd, session_info=fake_session, panes=[]
    )
    assert {a.kind for a in agents} == {"codex"}


def test_format_table(monkeypatch):
    monkeypatch.setattr(agent_ps, "HOME", type("P", (), {"__str__": lambda self: "/work"})())
    agents = collect(sample_procs(), cwd_of=fake_cwd, session_info=fake_session, panes=PANES)
    out = format_table(agents).splitlines()
    assert out[0].split() == ["AGENT", "PID", "STATUS", "AGE", "TTY", "TMUX", "CWD", "INFO"]
    claude_row = [
        "claude",
        "100",
        "busy",
        "1m",
        "pts/1",
        "flux:0.0",
        "~/a",
        "a-session",
        "--resume",
    ]
    assert out[1].split() == claude_row
    assert out[2].split() == ["codex", "200", "-", "1m", "pts/1", "flux:0.0", "~/b"]


def test_one_line_collapses_whitespace():
    assert one_line("a\nb\n\n  c\td ") == "a b c d"


def test_truncate():
    assert truncate("abcdef", 0) == "abcdef"
    assert truncate("abcdef", 6) == "abcdef"
    assert truncate("abcdefgh", 6) == "abc..."
    assert truncate("abcdef", 2) == "ab"


def test_format_table_keeps_multiline_prompt_on_one_row(monkeypatch):
    monkeypatch.setattr(agent_ps, "HOME", type("P", (), {"__str__": lambda self: "/work"})())
    prompt = "\n".join(f"line {i} of a long prompt" for i in range(30))
    procs = [claude(100, 1, prompt, "--permission-mode", "plan")]
    agents = collect(procs, cwd_of=fake_cwd, session_info=lambda pid: {}, panes=PANES)
    out = format_table(agents).splitlines()
    assert len(out) == 2
    assert out[1].endswith("line 29 of a long prompt --permission-mode plan")


def test_format_table_truncates_info_to_width(monkeypatch):
    monkeypatch.setattr(agent_ps, "HOME", type("P", (), {"__str__": lambda self: "/work"})())
    procs = [claude(100, 1, "x" * 200)]
    agents = collect(procs, cwd_of=fake_cwd, session_info=lambda pid: {}, panes=PANES)
    out = format_table(agents, width=80).splitlines()
    assert all(len(line) <= 80 for line in out)
    assert out[1].endswith("...")
    assert format_table(agents).splitlines()[1].endswith("x" * 200)
