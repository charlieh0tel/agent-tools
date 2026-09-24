"""End-to-end tests: two scratch ~/.claude dirs synced through the @local test host."""
import json
import os
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
        self.env = dict(os.environ, CLAUDE_SYNC_DIR=str(self.local),
                        CLAUDE_SYNC_CACHE=str(base / "cache.json"))

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
        (side / "sessions" / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": uuid, "procStart": proc_start(pid), "status": status}))

    def sync(self, *extra: str) -> str:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "sync", "@local", "--project", PROJECT_DIR,
             "--remote-dir", str(self.remote), *extra],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

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
        for rel in (self.transcript(U1), self.transcript(U2), self.transcript(U3), f"file-history/{U2}/abc@v1"):
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
        holder = subprocess.Popen(["flock", str(self.remote / ".claude-sync.lock"), "sleep", "30"])
        try:
            for _ in range(50):  # wait until flock holds the lock
                if subprocess.run(["flock", "-n", str(self.remote / ".claude-sync.lock"), "true"]).returncode:
                    break
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "sync", "@local", "--project", PROJECT_DIR,
                 "--remote-dir", str(self.remote)], env=self.env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("another sync", r.stderr)
        finally:
            holder.kill()
            holder.wait()


if __name__ == "__main__":
    unittest.main()
