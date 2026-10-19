"""claude-sessions, tested from README.txt alone (CLAUDE-SESSIONS) as a black box.

Each test gets a fake $HOME and $XDG_RUNTIME_DIR in a temp folder and a
fixed-offset TZ in which it is 11:40:05 when the test starts, so the README's
"11:35" and "10:02" are times of today and no test straddles midnight.
"""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DAY = 86400
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
NOT_RUNNING = 4194305  # above Linux's largest pid, so never alive

EXAMPLE = """\
CLAUDE SESSIONS  -  by last activity  -  tmux recorded since Wed 18:52

MAIN      (claude -> ~/.claude)
  11:35       SITE-DEPLOY      idle   tmux=web   cwd=~/src/site
  10:02       notes (3)                          cwd=~/notes
  Tue 09:14   fix flaky test                     cwd=~/src/api

ALT       (claude-alt -> ~/.claude-alt)
  (none)
"""


def pad(rows):
    """Padded output: columns as wide as their widest cell, 3 spaces apart."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["   ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


def sections(out):
    """{"MAIN": [names], "ALT": [names]} from --tsv output."""
    secs, cur = {}, None
    for line in out.splitlines():
        if line.startswith(("MAIN ", "ALT ")):
            cur = secs.setdefault(line.split()[0], [])
        elif line.startswith("  ") and cur is not None:
            cur.append(line.split("\t")[1] if "\t" in line else line.strip())
    return secs


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lobby-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.runtime = self.tmp / "home", self.tmp / "run"
        self.home.mkdir()
        self.runtime.mkdir(mode=0o700)
        self.main, self.alt = self.home / ".claude", self.home / ".claude-alt"
        self.daemon = self.runtime / "lobby-claude"
        self.now = time.time()
        off = round(11 * 3600 + 40 * 60 + 5 - self.now) % DAY
        off -= DAY if off > DAY // 2 else 0
        a = abs(off)
        self.tz = f"XYZ{'-' if off >= 0 else '+'}{a // 3600}:{a // 60 % 60:02d}:{a % 60:02d}"
        self.zone = timezone(timedelta(seconds=off))
        self.today = datetime.fromtimestamp(self.now, self.zone).replace(
            hour=0, minute=0, second=0, microsecond=0)

    # ---- time -----------------------------------------------------------
    def at(self, hhmm, days=0):
        """Local time hhmm, `days` days before today, as a timestamp."""
        h, m = map(int, hhmm.split(":"))
        return (self.today - timedelta(days=days)).replace(hour=h, minute=m).timestamp()

    def ddd(self, days):
        return WEEKDAYS[(self.today - timedelta(days=days)).weekday()]

    def when(self, ts):
        """README row cell 1, without its two spaces."""
        d = datetime.fromtimestamp(ts, self.zone)
        if d.date() == self.today.date():
            return f"{d:%H:%M}"
        if self.now - ts < 6 * DAY:
            return f"{WEEKDAYS[d.weekday()]} {d:%H:%M}"
        return f"{d.day} {MONTHS[d.month - 1]}"

    # ---- fixtures -------------------------------------------------------
    def expand(self, folder):
        return str(self.home) + folder[1:] if folder.startswith("~") else folder

    def session(self, config, name=None, ai=None, prompt=None, cwd="~/proj", mtime=None,
                sid=None):
        """A transcript <config>/projects/<cwd>/<sid>.jsonl modified at mtime."""
        sid = sid or str(uuid.uuid4())
        cwd = self.expand(cwd)
        lines = [{"type": "user", "cwd": cwd}]
        for kind, key, value in (("custom-title", "customTitle", name),
                                 ("ai-title", "aiTitle", ai),
                                 ("last-prompt", "lastPrompt", prompt)):
            if value:
                lines.append({"type": kind, key: value})
        path = config / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        mtime = mtime or self.at("09:00", 10)
        os.utime(path, (mtime, mtime))
        return sid

    def pid_file(self, config, sid, tmux=None, status="idle", run=False):
        """sessions/<pid>.json; with run=True for a live process, else a dead pid."""
        folder = config / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        pid, start = NOT_RUNNING + len(list(folder.iterdir())), "1"
        if run:
            p = subprocess.Popen(["sleep", "600"])
            self.addCleanup(p.wait)
            self.addCleanup(p.kill)
            stat = Path(f"/proc/{p.pid}/stat").read_text()
            pid, start = p.pid, stat[stat.rindex(")") + 2:].split()[19]  # field 22
        data = {"pid": pid, "sessionId": sid, "procStart": start, "status": status,
                "startedAt": int(self.now * 1000)}
        if tmux:
            data["tmux"] = tmux
        (folder / f"{pid}.json").write_text(json.dumps(data))

    def snapshot(self, ts, *panes):
        """A tmux-resurrect snapshot named after ts; panes are (session, title, folder)."""
        folder = self.home / ".local/share/tmux/resurrect"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"tmux_resurrect_{datetime.fromtimestamp(ts, self.zone):%Y%m%dT%H%M%S}.txt"
        path.write_text("".join("\t".join(["pane", s, "1", "1", ":*", "0", t, ":" + self.expand(f),
                                           "1", "claude", ":claude"]) + "\n"
                                for s, t, f in panes), encoding="utf-8")
        os.utime(path, (ts, ts))

    # ---- running --------------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("XDG_", "CLAUDE", "TMUX", "SSH_")) and k not in ("LINES", "COLUMNS")}
        env.update(HOME=str(self.home), XDG_RUNTIME_DIR=str(self.runtime), TZ=self.tz,
                   PYTHONDONTWRITEBYTECODE="1")
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)
        return env

    def run_cs(self, *args, timeout=60, **env):
        self.r = subprocess.run([sys.executable, str(REPO / "claude-sessions"), *args],
                                env=self.env(**env), cwd=self.tmp, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        return self.r.stdout

    def write_daemon_file(self):
        """Run --watch until it has written the daemon file, then kill it."""
        p = subprocess.Popen([sys.executable, str(REPO / "claude-sessions"), "--watch"],
                             env=self.env(), cwd=self.tmp, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not self.daemon.exists() and p.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        self.assertTrue(self.daemon.exists(), "--watch should write $XDG_RUNTIME_DIR/lobby-claude")


class ClaudeSessionsTest(Case):

    def test_readme_example(self):
        self.snapshot(self.at("18:52", 1))
        site = self.session(self.main, name="SITE-DEPLOY", cwd="~/src/site", mtime=self.at("11:35"))
        self.pid_file(self.main, site, tmux="web:@3.%7", run=True)
        for days, hhmm in ((0, "10:02"), (3, "08:00"), (4, "08:00")):
            self.session(self.main, ai="notes", cwd="~/notes", mtime=self.at(hhmm, days))
        self.session(self.main, ai="fix flaky test", cwd="~/src/api", mtime=self.at("09:14", 2))
        self.alt.mkdir()
        expected = EXAMPLE.replace("since Wed", f"since {self.ddd(1)}").replace(
            "Tue 09:14", f"{self.ddd(2)} 09:14")
        self.assertEqual(self.run_cs(), expected, self.r.stderr)
        lines = expected.split("\n")
        lines[3:6] = ["  11:35\tSITE-DEPLOY\tidle\ttmux=web\tcwd=~/src/site",
                      "  10:02\tnotes (3)\t\t\tcwd=~/notes",
                      f"  {self.ddd(2)} 09:14\tfix flaky test\t\t\tcwd=~/src/api"]
        self.assertEqual(self.run_cs("--tsv"), "\n".join(lines), self.r.stderr)

    def test_tsv_fits_to_LINES(self):
        for i in range(10):
            self.session(self.main, ai=f"m{i}", mtime=self.at("09:00", 10 + i))
        for i in range(8):
            self.session(self.alt, ai=f"a{i}", mtime=self.at("09:00", 10 + i))
        # 5 fixed lines; ALT gets up to 5 rows, MAIN the rest, ALT what is left;
        # every section at least one line
        for lines, n_main, n_alt in ((12, 2, 5), (22, 10, 7), (4, 1, 1)):
            with self.subTest(LINES=lines):
                out = self.run_cs("--tsv", "-n", "1", LINES=lines)
                self.assertEqual(sections(out), {"MAIN": [f"m{i}" for i in range(n_main)],
                                                 "ALT": [f"a{i}" for i in range(n_alt)]}, out)
                self.assertEqual(len(out.splitlines()), max(lines, 7), out)

    def test_tmux_cell_evidence_order(self):
        a = self.session(self.main, name="alpha", cwd="~/a")
        self.pid_file(self.main, a, tmux="rec:@1.%1")  # not running
        b = self.session(self.main, name="beta", cwd="~/b")
        memory = self.home / ".local/state/claude-sessions/tmux.tsv"
        memory.parent.mkdir(parents=True)
        memory.write_text(f"{b}\tb1\n{b}\tb2\n")
        self.session(self.main, name="gamma", cwd="~/c")
        self.session(self.main, ai="eps", cwd="~/e")
        self.session(self.main, ai="delta", cwd="~/d")
        self.snapshot(self.at("08:00", 3), ("g1", "✳ gamma", "~/c"), ("e", "✳ eps", "~/e"))
        self.snapshot(self.at("08:00", 1), ("pane", "✳ alpha", "~/a"), ("g2", "✳ gamma", "~/c"),
                      ("d", "✳ delta", "~/elsewhere"))
        want = {"alpha": "tmux=rec", "beta": "tmux=b2", "gamma": "tmux=g2", "eps": "tmux=e",
                "delta": ""}

        def cells():
            out = self.run_cs("--tsv")
            return {c[1]: c[3] for c in (l.split("\t") for l in out.splitlines() if "\t" in l)}

        self.assertEqual(cells(), want, self.r.stderr)
        self.assertIn(f"{a}\trec\n", memory.read_text())
        shutil.rmtree(self.main / "sessions")  # Claude deletes sessions/<pid>.json on exit
        self.assertEqual(cells(), want, "tmux.tsv should remember the tmux session")

    def test_grouping(self):
        old = self.session(self.main, ai="shared", cwd="~/g", mtime=self.at("08:00", 3))
        self.pid_file(self.main, old, tmux="run:@1.%1", status="busy", run=True)
        self.session(self.main, ai="shared", cwd="~/g", mtime=self.at("10:00"))
        other = self.session(self.main, ai="shared", cwd="~/h", mtime=self.at("09:00"))
        long = "N" * 31  # grouping is by full name, before the cut to 31 + "…"
        one = self.session(self.main, ai=long + "-one", cwd="~/g", mtime=self.at("08:00", 1))
        two = self.session(self.main, ai=long + "-two", cwd="~/g", mtime=self.at("07:00", 1))
        y = self.ddd(1)
        out = self.run_cs("--tsv", "-r")
        self.assertEqual([l for l in out.splitlines() if l.startswith("  ")], [
            "  10:00\tshared (2)\tbusy\ttmux=run\tcwd=~/g",
            f"     cd ~/g && claude --resume {old}",
            "  09:00\tshared\t\t\tcwd=~/h",
            f"     cd ~/h && claude --resume {other}",
            f"  {y} 08:00\t{long}…\t\t\tcwd=~/g",
            f"     cd ~/g && claude --resume {one}",
            f"  {y} 07:00\t{long}…\t\t\tcwd=~/g",
            f"     cd ~/g && claude --resume {two}"], self.r.stderr)

    def test_word_filter(self):
        self.session(self.main, name="Deploy Site", ai="kiwi", prompt="fix the BUG",
                     mtime=self.at("09:00", 10))
        self.session(self.main, ai="Notes", prompt="deploy later", mtime=self.at("09:00", 11))
        self.session(self.main, ai="misc", cwd="~/qzfolder", mtime=self.at("09:00", 12),
                     sid="5e55a1d0-0000-4000-8000-000000000000")
        self.session(self.main, ai="pair", prompt="zebra", mtime=self.at("09:00", 14))
        self.session(self.main, ai="pair", prompt="yak", mtime=self.at("09:00", 13))
        for words, names in ((["deploy"], ["Deploy Site", "Notes"]),
                             (["DEPLOY", "bug"], ["Deploy Site"]),
                             (["Kiwi"], ["Deploy Site"]),
                             (["qzfolder"], ["misc"]),
                             (["5E55A1D0"], ["misc"]),
                             (["yak"], ["pair (2)"]),
                             (["zebra"], ["(none)"])):  # a group matches on the session shown
            with self.subTest(words=words):
                self.assertEqual(sections(self.run_cs("--tsv", *words))["MAIN"], names)

    def test_resume_lines_and_long_folders(self):
        deep, srv = "~/research/projects/2026/experiments/run_c", "/srv/data/archive/2026/q3/experiments/x/y"
        t1, t2, t3 = self.at("09:00", 10), self.at("09:00", 11), self.at("09:00", 12)
        m = self.session(self.main, ai="deep", cwd=deep, mtime=t1)
        o = self.session(self.main, ai="srv", cwd=srv, mtime=t2)
        a = self.session(self.alt, ai="alt one", cwd="~/notes", mtime=t3)
        r1, r2, r3 = pad([["  " + self.when(t1), "deep", "", "", "cwd=~/…/experiments/run_c"],
                          ["  " + self.when(t2), "srv", "", "", "cwd=/…/x/y"],
                          ["  " + self.when(t3), "alt one", "", "", "cwd=~/notes"]])
        self.assertEqual(self.run_cs("-r"), "\n".join([
            "CLAUDE SESSIONS  -  by last activity", "",
            "MAIN      (claude -> ~/.claude)",
            r1, f"     cd {deep} && claude --resume {m}",
            r2, f"     cd {srv} && claude --resume {o}", "",
            "ALT       (claude-alt -> ~/.claude-alt)",
            r3, f"     cd ~/notes && claude-alt --resume {a}", ""]), self.r.stderr)

    def test_daemon_file_used_only_when_fresh_and_same_home(self):
        self.session(self.main, ai="before")
        self.write_daemon_file()
        self.session(self.main, ai="after", mtime=self.at("09:00", 1))
        self.assertEqual(sections(self.run_cs("--tsv"))["MAIN"], ["before"],
                         "a fresh daemon file is used instead of the folders")
        other = self.home
        self.home = self.tmp / "other"
        self.session(self.home / ".claude", ai="elsewhere")
        self.assertEqual(sections(self.run_cs("--tsv"))["MAIN"], ["elsewhere"],
                         "a daemon file written for another $HOME is ignored")
        self.home = other
        old = time.time() - 31
        os.utime(self.daemon, (old, old))
        self.assertEqual(sections(self.run_cs("--tsv"))["MAIN"], ["after", "before"],
                         "a daemon file older than 30 s is ignored")

    def test_watch_without_runtime_dir_fails(self):
        self.session(self.main, ai="alpha")
        try:
            self.run_cs("--watch", timeout=10, XDG_RUNTIME_DIR=None)
        except subprocess.TimeoutExpired:
            self.fail("--watch without $XDG_RUNTIME_DIR should exit with an error, not run")
        self.assertNotEqual(self.r.returncode, 0)
        self.assertEqual(sections(self.run_cs("--tsv", XDG_RUNTIME_DIR=None))["MAIN"], ["alpha"])


if __name__ == "__main__":
    unittest.main()
