"""lobby, tested from README.txt alone (LOBBY; its input is the SSH-STATUS snapshot).

Runs have no terminal, explicit $COLUMNS and $LINES, $SSH_STATUS_OUT pointing at
a snapshot written here, and the fake $HOME and time zone of test_claude_sessions.
"""
import os
import re
import subprocess
import sys
import time
import unittest

from tests import test_claude_sessions as cs

EXAMPLE = """\
lobby   box   Thu 16 Oct 11:40

  SSH  REMOTE        USER  CWD
  in   10.0.0.7      me    ~/src/site
  out  10.0.0.9 (2)  me    -

  TMUX  SESSION  WINDOWS  ATTACHED
  me    web      2        yes
  me    notes    1        no

  CLAUDE     NAME         STATE  TMUX  CWD
  11:35      SITE-DEPLOY  idle   web   ~/src/site
  Tue 09:14  notes (3)    -      -     ~/notes

  CLAUDE-ALT
  (none)

  ssh 2s ago   claude 4s ago   tmux since Wed 18:52
"""
TITLES = ("INBOUND   (remote -> this host:22)", "OUTBOUND  (this host -> remote:22)",
          "TMUX      (sessions across all users)")


class LobbyTest(cs.Case):

    def setUp(self):
        super().setUp()
        self.ssh = self.tmp / "lobby-ssh"

    def write_ssh(self, inbound=(), outbound=(), tmux=(), age=7200):
        """A snapshot as ssh-status writes it, modified `age` seconds ago."""
        text = "SSH SESSIONS  -  box  -  2026-10-16 11:40:00 XYZ\n"
        for title, rows in zip(TITLES, (inbound, outbound, tmux)):
            text += f"\n{title}\n" + "".join(f"  {r}\n" for r in rows or ["(none)"])
        self.ssh.write_text(text)
        t = time.time() - age
        os.utime(self.ssh, (t, t))

    def run_lobby(self, *args, columns=200, lines=200):
        self.r = subprocess.run([sys.executable, str(cs.REPO / "lobby"), *args],
                                env=self.env(SSH_STATUS_OUT=self.ssh, COLUMNS=columns, LINES=lines),
                                cwd=self.tmp, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=60, start_new_session=True)
        return self.r.stdout.splitlines()

    def test_readme_example(self):
        self.write_ssh(inbound=[f"10.0.0.7   user=me   cwd={self.home}/src/site"],
                       outbound=["10.0.0.9   user=me"] * 2,
                       tmux=["me   web   2 window(s), attached", "me   notes   1 window(s), detached"])
        self.snapshot(self.at("18:52", 1))
        site = self.session(self.main, name="SITE-DEPLOY", cwd="~/src/site", mtime=self.at("11:35"))
        self.pid_file(self.main, site, tmux="web:@3.%7", run=True)
        for days, hhmm in ((2, "09:14"), (3, "08:00"), (4, "08:00")):
            self.session(self.main, ai="notes", cwd="~/notes", mtime=self.at(hhmm, days))
        self.alt.mkdir()
        self.write_daemon_file()
        t = time.time()
        os.utime(self.ssh, (t - 2, t - 2))
        os.utime(self.daemon, (t - 4, t - 4))
        out = self.run_lobby()
        late = range(int(time.time() - t) + 1)  # ages are rounded down
        d = self.today
        expected = EXAMPLE.replace("Tue 09:14", f"{self.ddd(2)} 09:14").replace(
            "since Wed", f"since {self.ddd(1)}").splitlines()
        expected[0] = f"lobby   {os.uname().nodename}   {cs.WEEKDAYS[d.weekday()]} {d.day} {cs.MONTHS[d.month - 1]} 11:40"
        self.assertEqual(out[:-1], expected[:-1], self.r.stderr)
        self.assertIn(out[-1], [f"  ssh {2 + i}s ago   claude {4 + j}s ago   tmux since {self.ddd(1)} 18:52"
                                for i in late for j in late])

    def test_snapshot_rows(self):
        docs = f"{self.home}/My Docs/site"
        self.write_ssh(inbound=[f"10.0.0.7   user=me   cwd={docs}", "10.0.0.8 user=?",
                                f"10.0.0.7      user=me      cwd={docs}", "10.0.0.7   user=root   cwd=/root"],
                       outbound=["10.0.0.9          user=me"],
                       tmux=["me   my notes   3 window(s), attached", "uid1001 web 1 window(s), detached"])
        self.assertEqual(self.run_lobby()[2:-2], [
            "  SSH  REMOTE        USER  CWD",
            "  in   10.0.0.7 (2)  me    ~/My Docs/site",
            "  in   10.0.0.8      -     -",
            "  in   10.0.0.7      root  /root",
            "  out  10.0.0.9      me    -",
            "",
            "  TMUX     SESSION   WINDOWS  ATTACHED",
            "  me       my notes  3        yes",
            "  uid1001  web       1        no"], self.r.stderr)

    def test_long_lines_cut_to_COLUMNS(self):
        self.write_ssh(inbound=[f"10.0.0.7   user=me   cwd={self.home}/a/rather/long/folder/name"])
        self.snapshot(self.at("18:52", 1))
        self.session(self.main, name="a session with a long name", cwd="~/src/site")
        wide, narrow = self.run_lobby(columns=300), self.run_lobby(columns=24)
        self.assertTrue(len(wide[0]) > 24 and len(wide[-1]) > 24, wide)
        self.assertEqual(narrow, [l if len(l) <= 24 else l[:23] + "…" for l in wide], self.r.stderr)

    def test_fit_to_LINES_without_snapshot(self):
        for i in range(10):
            self.session(self.main, ai=f"m{i}", mtime=self.at("09:00", 10 + i))
        self.session(self.main, ai="the oldest session, never shown", mtime=self.at("09:00", 30))
        for i in range(8):
            self.session(self.alt, ai=f"a{i}", mtime=self.at("09:00", 10 + i))
        # 7 fixed lines: first line, blanks, header row, CLAUDE-ALT, footer
        for lines, n_main, n_alt in ((14, 2, 5), (5, 1, 1)):
            with self.subTest(LINES=lines):
                out = self.run_lobby(lines=lines)
                self.assertEqual(len(out), max(lines, 9), out)
                self.assertEqual(out[2], "  CLAUDE  NAME  STATE  TMUX  CWD", out)
                main, alt = out[3:3 + n_main], out[5 + n_main:-2]
                self.assertEqual(out[3 + n_main:5 + n_main], ["", "  CLAUDE-ALT"], out)
                self.assertEqual([re.split(" {2,}", l)[2] for l in main + alt],
                                 [f"m{i}" for i in range(n_main)] + [f"a{i}" for i in range(n_alt)])
                self.assertEqual(out[-1], f"  no {self.ssh}   claude fresh")

    def test_words_pick_tables(self):
        self.write_ssh(inbound=["10.0.0.7   user=me"], tmux=["me   web   1 window(s), attached"])
        self.snapshot(self.at("18:52", 1))
        self.session(self.main, ai="alpha")
        claude = f"claude fresh   tmux since {self.ddd(1)} 18:52"
        for args, tables, footer in (((), ["SSH", "TMUX", "CLAUDE"], "ssh 2h ago   " + claude),
                                     (("claude", "ssh"), ["SSH", "CLAUDE"], "ssh 2h ago   " + claude),
                                     (("tmux",), ["TMUX"], "ssh 2h ago"),
                                     (("claude", "claude"), ["CLAUDE"], claude)):
            with self.subTest(args=args):
                out = self.run_lobby(*args)
                self.assertEqual([l.split()[0] for l in out
                                  if l.startswith(("  SSH  ", "  TMUX  ", "  CLAUDE  "))], tables, out)
                self.assertEqual(out[-1], "  " + footer)
        for arg in ("--usage", "--help", "-h"):
            with self.subTest(arg=arg):
                text = "\n".join(self.run_lobby(arg))
                self.assertEqual(self.r.returncode, 0, self.r.stderr)
                self.assertTrue(all(w in text for w in ("ssh", "tmux", "claude")), text)
                self.assertNotIn("  SSH  REMOTE", text)
                self.assertLess(text.count("\n"), 20, "a short usage")
        for arg in ("--bogus", "sshh"):
            with self.subTest(arg=arg):
                self.run_lobby(arg)
                self.assertNotEqual(self.r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
