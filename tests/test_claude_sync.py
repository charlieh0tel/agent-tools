"""End-to-end tests: two scratch ~/.claude dirs synced through the @local test host."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
            os.environ, CLAUDE_SYNC_DIR=str(self.local), CLAUDE_SYNC_CACHE=str(base / "cache.json")
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

    def test_diverged_saves_one_conflict_copy(self):
        self.write(self.local, self.transcript(U1), "a\nX\n")
        self.write(self.remote, self.transcript(U1), "a\nY\n")
        self.sync()
        self.sync()
        copies = list((self.local / "projects" / ENC).glob(f"{U1}.jsonl.conflict-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "a\nY\n")
        self.assertEqual(self.read(self.local, self.transcript(U1)), "a\nX\n")
        self.assertEqual(self.read(self.remote, self.transcript(U1)), "a\nY\n")

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
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
