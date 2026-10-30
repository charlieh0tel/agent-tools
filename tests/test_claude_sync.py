"""Tests for claude-sync, mostly end to end: two scratch ~/.claude dirs synced through
the @local test host."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_sync

SCRIPT = Path(__file__).resolve().parent.parent / "claude_sync.py"
PROJECT_DIR = "/home/test/src/demo"
ENC = "-home-test-src-demo"
U1, U2, U3 = (f"{d * 8}-{d * 4}-{d * 4}-{d * 4}-{d * 12}" for d in "123")


def proc_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.local, self.remote = base / "local", base / "remote"
        for side in (self.local, self.remote):
            (side / "projects" / ENC).mkdir(parents=True)
            (side / "sessions").mkdir()
        self.env = dict(
            os.environ,
            CLAUDE_SYNC_DIR=str(self.local),
            CLAUDE_SYNC_CACHE=str(base / "cache.json"),
            CLAUDE_SYNC_MACHINE_ID="test-machine",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, side: Path, rel: str, data: str) -> Path:
        p = side / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
        return p

    def transcript(self, uuid: str) -> str:
        return f"projects/{ENC}/{uuid}.jsonl"

    def mark_live(self, side: Path, uuid: str, status: str) -> None:
        pid = os.getpid()
        (side / "sessions" / f"{pid}.json").write_text(
            json.dumps(
                {"pid": pid, "sessionId": uuid, "procStart": proc_start(pid), "status": status}
            )
        )

    def sync(self, *extra: str, ok: bool = True) -> str:
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "sync",
                "@local",
                "--project",
                PROJECT_DIR,
                "--remote-dir",
                str(self.remote),
                *extra,
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
        return r.stdout + r.stderr

    def read(self, side: Path, rel: str) -> str:
        return (side / rel).read_text()

    def test_fast_forward_and_new_files_both_ways(self):
        self.write(self.local, self.transcript(U1), "a\nb\n")
        self.write(self.remote, self.transcript(U1), "a\n")
        self.write(self.local, self.transcript(U2), "a\n")
        self.write(self.remote, self.transcript(U2), "a\nb\nc\n")
        self.write(self.local, self.transcript(U3), "local only\n")
        self.write(self.remote, f"file-history/{U2}/abc@v1", "backup\n")
        self.sync()
        for rel in (
            self.transcript(U1),
            self.transcript(U2),
            self.transcript(U3),
            f"file-history/{U2}/abc@v1",
        ):
            self.assertEqual(self.read(self.local, rel), self.read(self.remote, rel), rel)
        self.assertEqual(self.read(self.local, self.transcript(U2)), "a\nb\nc\n")
        self.assertNotRegex(self.sync("-n"), r"(?m)^(push|pull|conflict) ")

    def test_dry_run_changes_nothing(self):
        self.write(self.local, self.transcript(U1), "a\n")
        out = self.sync("-n")
        self.assertIn("push", out)
        self.assertFalse((self.remote / self.transcript(U1)).exists())

    def test_unchainable_divergence_saves_one_conflict_copy(self):
        self.write(self.local, self.transcript(U1), "a\nX\n")
        self.write(self.remote, self.transcript(U1), "a\nY\n")
        self.assertIn("can't chain", self.sync())
        self.sync()
        copies = list((self.local / "projects" / ENC).glob(f"{U1}.jsonl.conflict-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "a\nY\n")
        self.assertEqual(self.read(self.local, self.transcript(U1)), "a\nX\n")
        self.assertEqual(self.read(self.remote, self.transcript(U1)), "a\nY\n")

    def test_diverged_transcripts_are_chained(self):
        def rec(uuid, parent, minute, **extra):
            ts = f"2026-10-11T10:{minute:02}:00.000Z"
            return json.dumps({"uuid": uuid, "parentUuid": parent, "timestamp": ts, **extra})

        base = [rec("b1", None, 0), rec("b2", "b1", 1)]
        # the remote tail started first, so it goes first and the local tail hangs off it;
        # the local tail's compaction boundary is a root of its own and must stay one
        theirs = [rec("r1", "b2", 2), rec("r2", "r1", 5)]
        ours = [rec("l1", "b2", 3), rec("l2", "l1", 4), rec("l3", None, 6, logicalParentUuid="l2")]
        self.write(self.local, self.transcript(U1), "\n".join(base + ours) + "\n")
        self.write(self.remote, self.transcript(U1), "\n".join(base + theirs) + "\n")
        (self.local / self.transcript(U1)).chmod(0o600)
        self.assertIn("chaining tails", self.sync())

        merged = self.read(self.local, self.transcript(U1))
        self.assertEqual(merged, self.read(self.remote, self.transcript(U1)))
        records = [json.loads(line) for line in merged.splitlines()]
        self.assertEqual([r["uuid"] for r in records], ["b1", "b2", "r1", "r2", "l1", "l2", "l3"])
        parents = {r["uuid"]: r["parentUuid"] for r in records}
        self.assertEqual(parents["l1"], "r2")
        self.assertIsNone(parents["l3"])
        chain, u = [], "l2"
        while u:
            chain.append(u)
            u = parents[u]
        self.assertEqual(chain, ["l2", "l1", "r2", "r1", "b2", "b1"])

        copies = sorted((self.local / "projects" / ENC).glob(f"{U1}.jsonl.conflict-*"))
        self.assertEqual(len(copies), 2)
        # transcripts are owner-only; the merge and the copies must not widen that
        for path in [*copies, self.local / self.transcript(U1), self.remote / self.transcript(U1)]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        self.assertEqual(
            {c.read_text() for c in copies},
            {"\n".join(base + ours) + "\n", "\n".join(base + theirs) + "\n"},
        )
        self.assertNotRegex(self.sync("-n"), r"(?m)^merge ")

    def test_unsafe_path_in_sync_state_is_refused(self):
        self.write(
            self.local,
            "claude-sync/state/test-machine.json",
            json.dumps({"files": {f"projects/{ENC}/../../../escape": "0"}}),
        )
        self.write(self.local, self.transcript(U1), "a\n")
        self.assertIn("refusing unsafe path", self.sync(ok=False))
        self.assertFalse((self.remote / self.transcript(U1)).exists())

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable directories")
    def test_unreadable_directory_stops_the_sync(self):
        rel = f"projects/{ENC}/memory/notes.md"
        self.write(self.local, rel, "keep\n")
        self.sync()
        memory = self.local / "projects" / ENC / "memory"
        memory.chmod(0)
        try:
            self.assertIn("can't scan", self.sync(ok=False))
        finally:
            memory.chmod(0o755)
        self.assertEqual(self.read(self.remote, rel), "keep\n")

    def test_diverged_and_open_is_skipped(self):
        self.write(self.local, self.transcript(U1), "a\nX\n")
        self.write(self.remote, self.transcript(U1), "a\nY\n")
        self.mark_live(self.local, U1, "idle")
        self.assertIn("diverged, but open on local", self.sync())
        self.assertEqual(self.read(self.local, self.transcript(U1)), "a\nX\n")
        self.assertEqual(self.read(self.remote, self.transcript(U1)), "a\nY\n")

    def test_unreadable_session_file_stops_the_sync(self):
        self.write(self.local, self.transcript(U1), "a\n")
        self.write(self.local, "sessions/123.json", '{"pid": 12')
        self.assertIn("can't tell which sessions are running", self.sync(ok=False))
        self.assertFalse((self.remote / self.transcript(U1)).exists())

    def test_open_on_receiving_side_is_skipped(self):
        self.write(self.local, self.transcript(U1), "a\nb\n")
        self.write(self.remote, self.transcript(U1), "a\n")
        self.mark_live(self.remote, U1, "idle")
        out = self.sync()
        self.assertIn("open on @local", out)
        self.assertEqual(self.read(self.remote, self.transcript(U1)), "a\n")

    def test_busy_on_sending_side_is_skipped(self):
        self.write(self.local, self.transcript(U1), "a\nb\n")
        self.write(self.remote, self.transcript(U1), "a\n")
        self.mark_live(self.local, U1, "busy")
        out = self.sync()
        self.assertIn("busy on local", out)
        self.assertEqual(self.read(self.remote, self.transcript(U1)), "a\n")

    def test_idle_on_sending_side_is_snapshotted(self):
        self.write(self.local, self.transcript(U1), "a\nb\n")
        self.mark_live(self.local, U1, "idle")
        out = self.sync()
        self.assertIn("snapshot", out)
        self.assertEqual(self.read(self.remote, self.transcript(U1)), "a\nb\n")

    def test_idle_with_partial_last_line_is_skipped(self):
        self.write(self.local, self.transcript(U1), "a\nhalf-writ")
        self.mark_live(self.local, U1, "idle")
        out = self.sync()
        self.assertIn("partial last line", out)
        self.assertFalse((self.remote / self.transcript(U1)).exists())

    def test_sessions_index_is_excluded(self):
        self.write(self.local, f"projects/{ENC}/sessions-index.json", "{}")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        self.assertFalse((self.remote / "projects" / ENC / "sessions-index.json").exists())

    def test_memory_is_synced(self):
        self.write(self.local, f"projects/{ENC}/memory/MEMORY.md", "- note\n")
        self.sync()
        self.assertEqual(self.read(self.remote, f"projects/{ENC}/memory/MEMORY.md"), "- note\n")

    def test_concurrent_sync_is_refused(self):
        self.write(self.local, self.transcript(U1), "a\n")
        lock = self.remote / "claude-sync" / "lock"
        lock.parent.mkdir()
        holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
        try:
            for _ in range(50):  # wait until flock holds the lock
                if subprocess.run(["flock", "-n", str(lock), "true"]).returncode:
                    break
            r = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "sync",
                    "@local",
                    "--project",
                    PROJECT_DIR,
                    "--remote-dir",
                    str(self.remote),
                ],
                env=self.env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("another sync", r.stderr)
        finally:
            holder.kill()
            holder.wait()

    # ------------------------------------------------ milestone 3: sync base

    def trashed(self, side: Path) -> list[str]:
        return sorted(
            str(p.relative_to(t))
            for t in (side / "claude-sync" / "trash").glob("*")
            for p in t.rglob("*")
            if p.is_file()
        )

    def test_state_is_written_on_both_sides(self):
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        for side in (self.local, self.remote):
            states = list((side / "claude-sync" / "state").glob("*.json"))
            self.assertEqual(len(states), 1, side)
            self.assertIn(self.transcript(U1), json.loads(states[0].read_text())["files"])

    @unittest.skipIf(os.geteuid() == 0, "root writes read-only directories")
    def test_failed_state_push_leaves_both_bases_unchanged(self):
        state = self.remote / "claude-sync" / "state"
        state.mkdir(parents=True)
        state.chmod(0o500)
        self.write(self.local, self.transcript(U1), "a\n")
        try:
            self.sync(ok=False)
        finally:
            state.chmod(0o700)
        self.assertEqual(list((self.local / "claude-sync" / "state").glob("*.json")), [])

    def test_first_sync_never_deletes(self):
        self.write(self.remote, self.transcript(U1), "a\n")
        out = self.sync()
        self.assertIn("first sync", out)
        self.assertEqual(self.read(self.local, self.transcript(U1)), "a\n")

    def test_deletion_propagates_to_trash(self):
        self.write(self.local, self.transcript(U1), "a\n")
        self.write(self.local, self.transcript(U2), "keep\n")
        self.sync()
        (self.local / self.transcript(U1)).unlink()
        self.sync()
        self.assertFalse((self.remote / self.transcript(U1)).exists())
        self.assertEqual(self.trashed(self.remote), [self.transcript(U1)])
        self.assertIn("same", self.sync("-n"))

    def test_deletion_from_remote_side(self):
        self.write(self.local, self.transcript(U1), "a\n")
        self.write(self.local, self.transcript(U2), "keep\n")
        self.sync()
        (self.remote / self.transcript(U1)).unlink()
        self.sync()
        self.assertFalse((self.local / self.transcript(U1)).exists())
        self.assertEqual(self.trashed(self.local), [self.transcript(U1)])

    def test_changed_beats_deleted(self):
        self.write(self.local, self.transcript(U1), "a\n")
        self.write(self.local, self.transcript(U2), "keep\n")
        self.sync()
        (self.local / self.transcript(U1)).unlink()
        self.write(self.remote, self.transcript(U1), "a\nmore\n")
        self.sync()
        self.assertEqual(self.read(self.local, self.transcript(U1)), "a\nmore\n")

    def test_mass_delete_is_refused(self):
        for i in range(30):
            self.write(self.local, f"projects/{ENC}/memory/m{i}.md", "x\n")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        shutil.rmtree(self.local / "projects" / ENC / "memory")
        out = self.sync(ok=False)
        self.assertIn("refusing to delete 30", out)
        self.assertEqual(len(list((self.remote / "projects" / ENC / "memory").glob("*"))), 30)
        self.sync("--allow-deletes")
        self.assertFalse((self.remote / "projects" / ENC / "memory").exists())

    def test_mutable_three_way(self):
        rel = f"projects/{ENC}/memory/fact.md"
        self.write(self.local, rel, "v1\n")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        self.write(self.remote, rel, "v2 from remote\n")
        self.sync()
        self.assertEqual(self.read(self.local, rel), "v2 from remote\n")
        self.write(self.local, rel, "v3 local\n")
        self.sync()
        self.assertEqual(self.read(self.remote, rel), "v3 local\n")

    def test_mutable_changed_on_both_keeps_local_and_saves_remote(self):
        rel = f"projects/{ENC}/memory/fact.md"
        self.write(self.local, rel, "v1\n")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        self.write(self.local, rel, "local edit\n")
        self.write(self.remote, rel, "remote edit\n")
        self.sync()
        self.assertEqual(self.read(self.local, rel), "local edit\n")
        self.assertEqual(self.read(self.remote, rel), "local edit\n")
        copies = list((self.local / rel).parent.glob("fact.md.conflict-*"))
        self.assertEqual([c.read_text() for c in copies], ["remote edit\n"])

    def test_memory_index_is_unioned(self):
        rel = f"projects/{ENC}/memory/MEMORY.md"
        self.write(self.local, rel, "- [A](a.md)\n- [B](b.md)\n")
        self.write(self.remote, rel, "- [A](a.md)\n- [C](c.md)\n")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        want = "- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n"
        self.assertEqual(self.read(self.local, rel), want)
        self.assertEqual(self.read(self.remote, rel), want)

    def test_history_is_merged_by_timestamp(self):
        def line(ts, text):
            return json.dumps({"display": text, "timestamp": ts, "project": PROJECT_DIR})

        self.write(self.local, "history.jsonl", "\n".join([line(1, "a"), line(3, "c")]) + "\n")
        self.write(self.remote, "history.jsonl", "\n".join([line(1, "a"), line(2, "b")]) + "\n")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        want = "\n".join([line(1, "a"), line(2, "b"), line(3, "c")]) + "\n"
        self.assertEqual(self.read(self.local, "history.jsonl"), want)
        self.assertEqual(self.read(self.remote, "history.jsonl"), want)

    def test_config_only_with_flag(self):
        self.write(self.local, "CLAUDE.md", "- be terse\n")
        self.write(self.local, "skills/x/SKILL.md", "skill\n")
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync()
        self.assertFalse((self.remote / "CLAUDE.md").exists())
        self.sync("--config")
        self.assertEqual(self.read(self.remote, "CLAUDE.md"), "- be terse\n")
        self.assertEqual(self.read(self.remote, "skills/x/SKILL.md"), "skill\n")

    def test_old_trash_is_purged(self):
        old = self.local / "claude-sync" / "trash" / "20200101T000000"
        self.write(old, "x", "gone")
        os.utime(old, (0, 0))
        self.write(self.local, self.transcript(U1), "a\n")
        self.sync("-n")
        self.assertTrue(old.exists(), "a dry run must not delete recovery copies")
        self.sync()
        self.assertFalse(old.exists())


def rec(uuid, parent, minute, **extra):
    ts = f"2026-10-11T10:{minute:02}:00.000Z"
    record = {"uuid": uuid, "parentUuid": parent, "timestamp": ts, **extra}
    # unescaped non-ASCII, as Claude Code writes it
    return json.dumps(record, ensure_ascii=False) + "\n"


def chain(text):
    lines = text.rstrip("\n").split("\n")  # not splitlines(): records may hold U+2028
    return [(r.get("uuid"), r.get("parentUuid")) for r in map(json.loads, lines)]


class UnitTest(unittest.TestCase):
    def test_merging_again_after_an_interrupted_sync_changes_nothing(self):
        base = rec("p", None, 0)
        ours, theirs = base + rec("a", "p", 1), base + rec("b", "p", 2)
        merged = claude_sync.merge_transcript(ours, theirs)
        self.assertEqual(chain(merged), [("p", None), ("a", "p"), ("b", "a")])
        # the push failed, so the peer still has its own tail, perhaps grown since
        self.assertEqual(claude_sync.merge_transcript(merged, theirs), merged)
        grown = claude_sync.merge_transcript(merged, theirs + rec("c", "b", 3))
        self.assertEqual(chain(grown), [("p", None), ("a", "p"), ("b", "a"), ("c", "b")])

    def test_second_tail_hangs_off_the_resumed_leaf(self):
        base = rec("p", None, 0)
        # x was written after a, but last-prompt says --resume continues from a
        ours = (
            base
            + rec("a", "p", 1)
            + rec("x", "p", 1)
            + json.dumps({"type": "last-prompt", "leafUuid": "a"})
            + "\n"
        )
        merged = claude_sync.merge_transcript(ours, base + rec("b", "p", 2))
        self.assertIn(("b", "a"), chain(merged))

    def test_last_record_without_newline_stays_on_its_own_line(self):
        base = rec("p", None, 0)
        ours = base + rec("a", "p", 1).rstrip("\n")
        merged = claude_sync.merge_transcript(ours, base + rec("b", "p", 2))
        self.assertEqual(chain(merged), [("p", None), ("a", "p"), ("b", "a")])

    def test_line_separator_inside_a_record_is_not_a_line_break(self):
        base = rec("p", None, 0)
        ours = base + rec("a", "p", 1, text="one\u2028two")
        merged = claude_sync.merge_transcript(ours, base + rec("b", "p", 2))
        self.assertEqual(chain(merged), [("p", None), ("a", "p"), ("b", "a")])

    def test_history_merge_keeps_line_separators_inside_prompts(self):
        line = json.dumps({"display": "one\u2028two", "timestamp": 2}, ensure_ascii=False)
        merged = claude_sync.merge_history(line + "\n", json.dumps({"timestamp": 1}) + "\n")
        self.assertEqual(merged.rstrip("\n").split("\n")[1], line)

    def test_compacted_tail_goes_first(self):
        base = rec("p", None, 0)
        ours = base + rec("a", "p", 1)  # started first, but not compacted
        boundary = rec("c", None, 3, subtype="compact_boundary", logicalParentUuid="b")
        theirs = base + rec("b", "p", 2) + boundary + rec("d", "c", 4)
        merged = chain(claude_sync.merge_transcript(ours, theirs))
        self.assertEqual(merged[-1], ("a", "d"))  # resuming from a reaches d, then c

    def test_both_tails_compacted_is_refused(self):
        base = rec("p", None, 0)
        ours = base + rec("a", "p", 1) + rec("c", None, 2, subtype="compact_boundary")
        theirs = base + rec("b", "p", 3) + rec("e", None, 4, subtype="compact_boundary")
        with self.assertRaisesRegex(ValueError, "compacted"):
            claude_sync.merge_transcript(ours, theirs)

    def test_check_chain_refuses_duplicates_and_cycles(self):
        claude_sync.check_chain(
            [{"uuid": "a", "parentUuid": None}, {"uuid": "b", "parentUuid": "a"}]
        )
        for records in (
            [{"uuid": "a", "parentUuid": None}, {"uuid": "a", "parentUuid": None}],
            [{"uuid": "a", "parentUuid": "b"}, {"uuid": "b", "parentUuid": "a"}],
            [{"uuid": "a", "parentUuid": "a"}],
        ):
            with self.assertRaises(ValueError, msg=records):
                claude_sync.check_chain(records)

    def test_check_paths_refuses_escapes(self):
        claude_sync.check_paths([f"projects/{ENC}/{U1}.jsonl", "history.jsonl"], "test")
        for rel in ("../x", "/etc/passwd", f"projects/{ENC}/../../x", ""):
            with self.assertRaises(SystemExit, msg=rel):
                claude_sync.check_paths([rel], "test")

    def test_machine_ids_that_could_be_paths_are_refused(self):
        self.assertEqual(claude_sync.check_machine_id("e8d5227c9b4f42", "test"), "e8d5227c9b4f42")
        for mid in ("/home/ch/.claude/settings", "../x", "a/b", "", None):
            with self.assertRaises(SystemExit, msg=mid):
                claude_sync.check_machine_id(mid, "test")

    def test_trashing_the_same_path_twice_keeps_both_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, CLAUDE_SYNC_DIR=tmp):
                for text in ("first\n", "second\n"):  # within the same second
                    (Path(tmp) / "f").write_text(text)
                    claude_sync.trash(["f"])
            kept = sorted(p.read_text() for p in Path(tmp).glob("claude-sync/trash/*/f"))
            self.assertEqual(kept, ["first\n", "second\n"])

    def test_rsync_copies_a_file_whose_size_and_mtime_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "src", Path(tmp) / "dst"
            for side, text in ((src, "new\n"), (dst, "old\n")):
                side.mkdir()
                (side / "f").write_text(text)
                os.utime(side / "f", (1_000_000_000, 1_000_000_000))
            claude_sync.rsync(f"{src}/", f"{dst}/", ["f"])
            self.assertEqual((dst / "f").read_text(), "new\n")

    def test_rsync_does_not_copy_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "src", Path(tmp) / "dst"
            src.mkdir()
            (Path(tmp) / "secret").write_text("secret\n")
            (src / "link").symlink_to(Path(tmp) / "secret")
            claude_sync.rsync(f"{src}/", f"{dst}/", ["link"])
            self.assertFalse((dst / "link").exists() or (dst / "link").is_symlink())


class MvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.claude = base / ".claude"
        (self.claude / "sessions").mkdir(parents=True)
        self.old, self.new = base / "src" / "foo", base / "src" / "bar"
        (self.old / "sub").mkdir(parents=True)
        self.env = dict(os.environ, CLAUDE_SYNC_DIR=str(self.claude))

    def tearDown(self):
        self.tmp.cleanup()

    def project(self, path: Path) -> Path:
        return self.claude / "projects" / claude_sync.encode(str(path))

    def session(self, cwd: Path, uuid: str = U1, name: str | None = None) -> Path:
        records = [
            {"type": "user", "cwd": str(cwd), "message": {"content": f"cd {cwd}"}},
            {"type": "summary", "summary": "no cwd"},
            {"type": "assistant", "cwd": f"{cwd}/sub", "message": {"content": "ok"}},
        ]
        p = self.project(cwd) / (name or f"{uuid}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
        return p

    def mv(self, *extra: str, ok: bool = True) -> str:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "mv", *extra, str(self.old), str(self.new)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
        return r.stdout + r.stderr

    def cwds(self, p: Path) -> list:
        return [json.loads(line).get("cwd") for line in p.read_text().splitlines()]

    def test_moves_the_directory_and_claude_records(self):
        (self.old / "file").write_text("kept\n")
        self.session(self.old)
        self.session(self.old / "sub", U2)
        sub = self.project(self.old) / U1 / "subagents" / "agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        shutil.copy(self.session(self.old, name="tmp.jsonl"), sub)
        (self.project(self.old) / "tmp.jsonl").unlink()
        (self.project(self.old) / "memory").mkdir()
        (self.project(self.old) / "memory" / "MEMORY.md").write_text("- note\n")
        other = self.session(self.old.parent / "foobar", U3)  # a sibling, not inside old
        history = [
            {"display": "a", "project": str(self.old), "timestamp": 1},
            {"display": "b", "project": str(self.old.parent / "foobar"), "timestamp": 2},
            {"display": "c", "project": str(self.old / "sub"), "timestamp": 3},
        ]
        (self.claude / "history.jsonl").write_text(
            "".join(json.dumps(h, separators=(",", ":")) + "\n" for h in history)
        )
        config = {
            "projects": {"/x": {}, str(self.old): {"trust": True}, "/y": {}},
            "githubRepoPaths": {"me/foo": [str(self.old)], "me/y": ["/y"]},
        }
        (self.claude / ".claude.json").write_text(json.dumps(config, indent=2))
        other_text = other.read_text()

        out = self.mv()
        self.assertIn("rewrite   3 transcripts", out)
        self.assertFalse(self.old.exists())
        self.assertEqual((self.new / "file").read_text(), "kept\n")
        self.assertFalse(self.project(self.old).exists())
        moved = self.project(self.new)
        new, sub_new = str(self.new), f"{self.new}/sub"
        self.assertEqual(self.cwds(moved / f"{U1}.jsonl"), [new, None, sub_new])
        self.assertEqual(
            self.cwds(moved / U1 / "subagents" / "agent-1.jsonl"), [new, None, sub_new]
        )
        sub_project = self.project(self.new / "sub") / f"{U2}.jsonl"
        self.assertEqual(self.cwds(sub_project)[0], sub_new)
        self.assertEqual((moved / "memory" / "MEMORY.md").read_text(), "- note\n")
        self.assertIn(f"cd {self.old}", (moved / f"{U1}.jsonl").read_text())  # history as is
        self.assertEqual(other.read_text(), other_text)
        projects = [
            json.loads(line)["project"]
            for line in (self.claude / "history.jsonl").read_text().splitlines()
        ]
        self.assertEqual(projects, [new, str(self.old.parent / "foobar"), sub_new])
        config = json.loads((self.claude / ".claude.json").read_text())
        self.assertEqual(list(config["projects"]), ["/x", new, "/y"])
        self.assertEqual(config["projects"][new], {"trust": True})
        self.assertEqual(config["githubRepoPaths"], {"me/foo": [new], "me/y": ["/y"]})

    def test_dry_run_changes_nothing(self):
        p = self.session(self.old)
        text = p.read_text()
        out = self.mv("-n")
        self.assertIn(f"move      {self.old} -> {self.new}", out)
        self.assertIn("rewrite   1 transcripts", out)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertEqual(p.read_text(), text)

    def test_already_moved_directory_updates_records_only(self):
        self.session(self.old)
        self.old.rename(self.new)
        self.assertIn("updating Claude's records only", self.mv())
        self.assertEqual(self.cwds(self.project(self.new) / f"{U1}.jsonl")[0], str(self.new))

    def test_same_encoding_from_another_directory_is_left_alone(self):
        # src/foo-x and src/foo/x encode alike; only the one inside old moves
        p = self.session(self.old.parent / "foo-x")
        self.mv()
        self.assertTrue(p.exists())
        self.assertEqual(self.cwds(p)[0], str(self.old.parent / "foo-x"))

    def test_refuses_while_a_session_runs_in_old_or_new(self):
        pid = os.getpid()
        record = {"pid": pid, "sessionId": U1, "procStart": proc_start(pid)}
        for cwd in (self.old / "sub", self.new):
            (self.claude / "sessions" / f"{pid}.json").write_text(
                json.dumps({**record, "cwd": str(cwd)})
            )
            self.assertIn("close the sessions", self.mv(ok=False), cwd)
            self.assertTrue(self.old.is_dir())

    def test_refuses_an_existing_target_or_a_move_into_itself(self):
        self.new.mkdir()
        self.assertIn("already exists", self.mv(ok=False))
        self.new.rmdir()
        self.session(self.old)
        self.project(self.new).mkdir(parents=True)
        self.assertIn("already exists", self.mv(ok=False))
        self.assertTrue(self.old.is_dir())
        self.new = self.old / "inner"
        self.assertIn("into itself", self.mv(ok=False))

    def test_move_field_keeps_unchanged_lines_and_a_missing_last_newline(self):
        text = '{"cwd": "/a/b", "x": 1}\nnot json\n{"cwd":"/a"}'
        out = claude_sync.move_field(text, "cwd", "/a", "/c", Path("t"))
        self.assertEqual(out, '{"cwd":"/c/b","x":1}\nnot json\n{"cwd":"/c"}')
        self.assertIs(claude_sync.move_field(text, "cwd", "/z", "/c", Path("t")), text)

    def test_move_config_keeps_settings_already_at_the_new_path(self):
        text = json.dumps({"projects": {"/a": {"old": 1}, "/c": {"new": 1}}}, indent=2)
        out = json.loads(claude_sync.move_config(text, "/a", "/c"))
        self.assertEqual(out["projects"], {"/a": {"old": 1}, "/c": {"new": 1}})


class AccountFixture(unittest.TestCase):
    """Two homes with claude-user accounts; the account that owns ~/.claude differs
    between them. The project belongs to account personal here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.here, self.there = base / "here", base / "there"
        self.project_dir = self.here / "src" / "demo"
        self.project_dir.mkdir(parents=True)
        self.enc = claude_sync.encode(str(self.project_dir))
        self.config(self.here, "default = work\n[dirs]\n~/src = personal\n")
        self.config(self.there, "default = personal\n")
        for d in (self.here / ".claude", self.here / ".claude-personal"):
            (d / "sessions").mkdir(parents=True)
        (self.there / ".claude" / "sessions").mkdir(parents=True)
        (self.there / ".claude-work").mkdir()
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDE_SYNC_DIR", "CLAUDE_USER_CONFIG", "XDG_CONFIG_HOME")
        }
        self.env.update(
            HOME=str(self.here),
            CLAUDE_SYNC_REMOTE_HOME=str(self.there),
            CLAUDE_SYNC_CACHE=str(base / "cache.json"),
            CLAUDE_SYNC_MACHINE_ID="test-machine",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, home: Path, text: str) -> None:
        path = home / ".config" / "claude-user" / "config"
        path.parent.mkdir(parents=True)
        path.write_text(text)

    def run_sync(self, *args: str, ok: bool = True) -> str:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=self.env,
            cwd=self.project_dir,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode == 0, ok, r.stderr + r.stdout)
        return r.stdout + r.stderr


class AccountTest(AccountFixture):
    """Sync and mv with claude-user accounts."""

    def test_sync_maps_the_account_on_each_side(self):
        rel = f"projects/{self.enc}/{U1}.jsonl"
        (self.here / ".claude-personal" / rel).parent.mkdir(parents=True)
        (self.here / ".claude-personal" / rel).write_text("a\n")
        out = self.run_sync("sync", "@local")
        self.assertIn(f"account   personal: {self.here}/.claude-personal here", out)
        self.assertEqual((self.there / ".claude" / rel).read_text(), "a\n")
        self.assertFalse((self.here / ".claude" / "projects").exists())

    def test_sync_account_option_and_default_account(self):
        rel = f"projects/{self.enc}/{U1}.jsonl"
        (self.here / ".claude" / rel).parent.mkdir(parents=True)
        (self.here / ".claude" / rel).write_text("w\n")
        self.run_sync("sync", "@local", "--account", "work")
        self.assertEqual((self.there / ".claude-work" / rel).read_text(), "w\n")

    def test_sync_refuses_a_missing_account_dir(self):
        shutil.rmtree(self.there / ".claude-work")
        out = self.run_sync("sync", "@local", "--account", "work", ok=False)
        self.assertIn(f"has no {self.there}/.claude-work", out)
        out = self.run_sync("sync", "@local", "--account", "other", ok=False)
        self.assertIn(f"has no {self.here}/.claude-other here", out)

    def test_mv_updates_the_account_records(self):
        new = self.here / "src" / "renamed"
        proj = self.here / ".claude-personal" / "projects" / self.enc
        proj.mkdir(parents=True)
        (proj / f"{U1}.jsonl").write_text(json.dumps({"cwd": str(self.project_dir)}) + "\n")
        config = {"projects": {str(self.project_dir): {}}}
        (self.here / ".claude-personal" / ".claude.json").write_text(json.dumps(config))
        (self.here / ".claude.json").write_text(json.dumps(config))
        self.run_sync("mv", str(self.project_dir), str(new))
        moved = self.here / ".claude-personal" / "projects" / claude_sync.encode(str(new))
        self.assertEqual(json.loads((moved / f"{U1}.jsonl").read_text())["cwd"], str(new))
        account_config = json.loads((self.here / ".claude-personal" / ".claude.json").read_text())
        self.assertEqual(list(account_config["projects"]), [str(new)])
        self.assertEqual(json.loads((self.here / ".claude.json").read_text()), config)

    def test_mv_warns_when_the_new_path_has_another_account(self):
        (self.here / "elsewhere").mkdir()
        out = self.run_sync("mv", str(self.project_dir), str(self.here / "elsewhere" / "demo"))
        self.assertIn("belongs to account work", out)

    def test_claude_json_follows_the_claude_dir(self):
        with mock.patch.dict(os.environ, HOME=str(self.here)):
            os.environ.pop("CLAUDE_SYNC_DIR", None)
            with mock.patch.object(claude_sync, "ACCOUNT_DIR", None):
                self.assertEqual(claude_sync.claude_json(), self.here / ".claude.json")
            with mock.patch.object(claude_sync, "ACCOUNT_DIR", self.here / ".claude-x"):
                self.assertEqual(claude_sync.claude_json(), self.here / ".claude-x/.claude.json")


class SplitTest(AccountFixture):
    """claude-sync split, from a mixed ~/.claude (account work) into ~/.claude-personal."""

    def setUp(self):
        super().setUp()
        self.work, self.personal = self.here / ".claude", self.here / ".claude-personal"
        self.other_dir = self.here / "work" / "thing"  # not under ~/src: stays in work
        self.other_dir.mkdir(parents=True)
        self.proj = self.session(self.project_dir, U1)
        self.session(self.other_dir, U2)
        (self.proj / U1 / "subagents").mkdir(parents=True)
        (self.proj / U1 / "subagents" / "agent-1.jsonl").write_text("{}\n")
        (self.proj / "memory").mkdir()
        (self.proj / "memory" / "MEMORY.md").write_text("- note\n")
        for rel in (
            f"file-history/{U1}/abc@v1",
            f"todos/{U1}-agent-{U1}.json",
            f"file-history/{U2}/x",
        ):
            (self.work / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.work / rel).write_text("x\n")
        (self.work / "session-env" / U1).mkdir(parents=True)
        history = [
            {"display": "a", "project": str(self.project_dir), "timestamp": 1},
            {"display": "b", "project": str(self.other_dir), "timestamp": 2},
            {"display": "c", "project": str(self.project_dir), "timestamp": 3},
        ]
        (self.work / "history.jsonl").write_text("".join(json.dumps(h) + "\n" for h in history))
        (self.personal / "history.jsonl").write_text(
            json.dumps({"display": "p", "project": "/p", "timestamp": 2}) + "\n"
        )
        self.write_json(
            self.here / ".claude.json",
            {
                "projects": {str(self.project_dir): {"trust": 1}, str(self.other_dir): {}},
                "githubRepoPaths": {"me/demo": [str(self.project_dir), "/x"]},
            },
        )
        self.write_json(self.personal / ".claude.json", {"numStartups": 1})

    def write_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2))

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def session(self, cwd: Path, uuid: str) -> Path:
        proj = self.work / "projects" / claude_sync.encode(str(cwd))
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{uuid}.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")
        return proj

    def displays(self, root: Path) -> list:
        return [json.loads(line)["display"] for line in (root / "history.jsonl").open()]

    def assert_split(self) -> None:
        moved = self.personal / "projects" / self.enc
        self.assertFalse(self.proj.exists())
        self.assertEqual((moved / "memory" / "MEMORY.md").read_text(), "- note\n")
        self.assertTrue((moved / U1 / "subagents" / "agent-1.jsonl").exists())
        for rel in (
            f"file-history/{U1}/abc@v1",
            f"todos/{U1}-agent-{U1}.json",
            f"session-env/{U1}",
        ):
            self.assertTrue((self.personal / rel).exists(), rel)
            self.assertFalse((self.work / rel).exists(), rel)
        self.assertTrue((self.work / f"file-history/{U2}/x").exists())
        self.assertEqual(self.displays(self.work), ["b"])
        self.assertEqual(self.displays(self.personal), ["a", "p", "c"])
        work_config = self.read_json(self.here / ".claude.json")
        self.assertEqual(list(work_config["projects"]), [str(self.other_dir)])
        self.assertEqual(work_config["githubRepoPaths"], {"me/demo": ["/x"]})
        personal_config = self.read_json(self.personal / ".claude.json")
        self.assertEqual(personal_config["projects"], {str(self.project_dir): {"trust": 1}})
        self.assertEqual(personal_config["githubRepoPaths"], {"me/demo": [str(self.project_dir)]})
        self.assertEqual(personal_config["numStartups"], 1)

    def test_split_a_project(self):
        out = self.run_sync("split", str(self.project_dir))
        self.assertIn(f"move      {self.project_dir} -> personal", out)
        self.assert_split()
        self.assertIn("nothing to split", self.run_sync("split", str(self.project_dir)))

    def test_split_all_moves_each_project_to_its_account(self):
        (self.work / "projects" / "-no-sessions").mkdir()
        out = self.run_sync("split", "--all")
        self.assertIn("skipping", out)
        self.assert_split()
        self.assertTrue((self.work / "projects" / claude_sync.encode(str(self.other_dir))).is_dir())

    def test_split_all_moves_a_memory_only_project_by_its_name(self):
        notes = self.project_dir.parent / "notes"  # its transcripts cleaned up
        notes.mkdir()
        memory = self.work / "projects" / claude_sync.encode(str(notes)) / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text("- kept\n")
        self.assertIn(f"move      {notes} -> personal", self.run_sync("split", "--all"))
        moved = self.personal / "projects" / claude_sync.encode(str(notes)) / "memory"
        self.assertEqual((moved / "MEMORY.md").read_text(), "- kept\n")
        self.assertFalse(memory.exists())

    def test_dry_run_changes_nothing(self):
        before = {p: p.read_bytes() for p in self.here.rglob("*") if p.is_file()}
        self.assertIn("move", self.run_sync("split", "--all", "-n"))
        after = {p: p.read_bytes() for p in self.here.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_merges_into_a_project_already_in_the_target(self):
        target = self.personal / "projects" / self.enc
        target.mkdir(parents=True)
        (target / f"{U3}.jsonl").write_text("new\n")
        (target / "memory").mkdir()
        (target / "memory" / "MEMORY.md").write_text("- note\n")  # the same: fine
        self.run_sync("split", str(self.project_dir))
        self.assertTrue((target / f"{U3}.jsonl").exists())
        self.assertTrue((target / f"{U1}.jsonl").exists())

    def test_a_differing_file_in_the_target_stops_before_moving_anything(self):
        clash = self.personal / "projects" / self.enc / "memory" / "MEMORY.md"
        clash.parent.mkdir(parents=True)
        clash.write_text("- other\n")
        self.assertIn("differs", self.run_sync("split", str(self.project_dir), ok=False))
        self.assertTrue((self.proj / f"{U1}.jsonl").exists())
        self.assertTrue((self.work / f"file-history/{U1}/abc@v1").exists())

    def test_refuses_while_a_session_runs_there(self):
        pid = os.getpid()
        record = {"pid": pid, "sessionId": U3, "procStart": proc_start(pid)}
        record["cwd"] = str(self.project_dir / "sub")
        (self.personal / "sessions" / f"{pid}.json").write_text(json.dumps(record))
        self.assertIn("close the sessions", self.run_sync("split", "--all", ok=False))
        self.assertTrue(self.proj.exists())

    def test_to_and_from(self):
        self.assertIn(
            "already in", self.run_sync("split", str(self.project_dir), "--to", "work", ok=False)
        )
        self.run_sync("split", str(self.project_dir))
        self.run_sync("split", str(self.project_dir), "--from", "personal", "--to", "work")
        self.assertTrue((self.proj / f"{U1}.jsonl").exists())
        self.assertEqual(self.displays(self.work), ["a", "b", "c"])

    def test_syncs_carry_a_split_made_on_one_machine_only(self):
        self.run_sync("sync", "@local", "--all", "--account", "work")
        there_work, there_personal = self.there / ".claude-work", self.there / ".claude"
        self.assertTrue((there_work / "projects" / self.enc / f"{U1}.jsonl").exists())
        self.run_sync("split", "--all")
        self.run_sync("sync", "@local", "--all", "--account", "work")
        self.run_sync("sync", "@local", "--all", "--account", "personal")
        self.assertFalse((there_work / "projects" / self.enc).exists())
        self.assertTrue((there_personal / "projects" / self.enc / f"{U1}.jsonl").exists())
        self.assertTrue((there_personal / f"file-history/{U1}/abc@v1").exists())
        self.assertEqual(self.displays(self.work), ["a", "b", "c"])  # back from there

    def test_settings_stay_when_the_target_has_no_claude_json(self):
        (self.personal / ".claude.json").unlink()
        self.assertIn("project settings left", self.run_sync("split", str(self.project_dir)))
        self.assertIn(str(self.project_dir), self.read_json(self.here / ".claude.json")["projects"])


if __name__ == "__main__":
    unittest.main()
