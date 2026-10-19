"""ssh-status, tested from README.txt alone (SSH-STATUS).

It reads the machine's real sessions, so the test checks the format of
whatever it finds, and how the file is written.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECTIONS = (("INBOUND   (remote -> this host:22)", r"  \S+ +user=\S+( +cwd=.+)?"),
            ("OUTBOUND  (this host -> remote:22)", r"  \S+ +user=\S+"),
            ("TMUX      (sessions across all users)", r"  \S+ +\S.* +\d+ window\(s\), (attached|detached)"))


class SshStatusTest(unittest.TestCase):

    def test_snapshot_replaced_in_one_step_with_mode_0644(self):
        tmp = Path(tempfile.mkdtemp(prefix="ssh-status-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        out = tmp / "out" / "lobby-ssh"
        out.parent.mkdir()
        out.write_text("old\n")
        out.chmod(0o600)
        inode = out.stat().st_ino
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "SSH_"))}
        env.update(HOME=str(tmp), XDG_RUNTIME_DIR=str(tmp), SSH_STATUS_OUT=str(out), TZ="XYZ-05:30")
        t0 = time.time()
        r = subprocess.run([str(REPO / "ssh-status")], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60, umask=0o077)
        t1 = time.time()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(out.stat().st_ino, inode, "the file is replaced, not rewritten")
        self.assertEqual(oct(out.stat().st_mode & 0o777), oct(0o644))
        self.assertEqual(os.listdir(out.parent), ["lobby-ssh"], "no temporary file left behind")

        text = out.read_text()
        head, *groups = text.rstrip("\n").split("\n\n")
        m = re.fullmatch(rf"SSH SESSIONS  -  {re.escape(os.uname().nodename)}  -  (.+) XYZ", head)
        self.assertTrue(m, f"header line: {head!r}")
        stamp = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp()
        self.assertTrue(t0 - 1 <= stamp <= t1, f"header time {m[1]} is local time now")
        self.assertEqual([g.split("\n")[0] for g in groups], [t for t, _ in SECTIONS], text)
        for (title, row), group in zip(SECTIONS, groups):
            rows = group.split("\n")[1:]
            if rows != ["  (none)"]:
                for line in rows:
                    self.assertRegex(line, f"^{row}$", f"a row of {title.split()[0]}")


if __name__ == "__main__":
    unittest.main()
