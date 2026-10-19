"""sessions, tested from README.txt alone (SESSIONS) as a black box.

Each test gets a fake $HOME and $XDG_RUNTIME_DIR in a temp folder and a
fixed-offset TZ in which it is 11:40:05 when the test starts, so the README's
"11:35" and "10:02" are times of today and no test straddles midnight.
"""
import contextlib
import json
import os
import shutil
import signal
import sqlite3
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
SESSIONS  -  by last activity  -  tmux recorded since Wed 18:52

  11:35       claude       SITE-DEPLOY    idle   tmux=web   cwd=~/src/site
  10:02       claude-alt   XTAL-* (3)     busy              cwd=~/stage
  Tue 09:14   claude       LAB                              cwd=~/src/lab
  Tue 09:14   opencode     · runs (40)                      cwd=~/src/lab
  Mon 17:02   claude       · lab-replay          tmux=r1    cwd=~/src/lab
"""
RUN_PERMISSION = json.dumps([{"permission": "question", "pattern": "*", "action": "deny"}])


def pad(rows):
    """Padded output: columns as wide as their widest cell, 3 spaces apart."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["   ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


def names(out):
    """Name cells (or "(none)") of --tsv output, in order."""
    return [l.split("\t")[2] if "\t" in l else l.strip()
            for l in out.splitlines()[2:] if l.startswith("  ")]


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lobby-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.runtime = self.tmp / "home", self.tmp / "run"
        self.home.mkdir()
        self.runtime.mkdir(mode=0o700)
        self.main, self.alt = self.home / ".claude", self.home / ".claude-alt"
        self.daemon = self.runtime / "lobby-sessions"
        self.oc_db = self.home / ".local/share/opencode/opencode.db"
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
                sid=None, start=None, calls=()):
        """A transcript <config>/projects/<cwd>/<sid>.jsonl modified at mtime,
        started at start, with Bash tool calls (time, command) run in cwd."""
        sid = sid or str(uuid.uuid4())
        cwd = self.expand(cwd)
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
        lines = [dict({"type": "user", "cwd": cwd}, **({"timestamp": iso(start)} if start else {}))]
        lines += [{"type": "assistant", "cwd": cwd, "timestamp": iso(t), "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}} for t, c in calls]
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

    def oc_session(self, title="", slug="calm-river", cwd="~/proj", mtime=None, run=False,
                   worktree="/", sid=None, last=None, start=None):
        """A top-level opencode session in opencode.db; last is the data of
        its newest message, if any."""
        sid = sid or "ses_" + uuid.uuid4().hex
        self.oc_db.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.oc_db)
        db.executescript(
            "CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, worktree TEXT);"
            "CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,"
            " slug TEXT, directory TEXT, title TEXT, permission TEXT, time_updated INTEGER,"
            " time_archived INTEGER, time_created INTEGER);"
            "CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT,"
            " time_created INTEGER, data TEXT);")
        db.execute("INSERT OR IGNORE INTO project VALUES (?, ?)", (worktree, self.expand(worktree)))
        mtime = mtime or self.at("09:00", 10)
        db.execute("INSERT INTO session VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?)",
                   (sid, worktree, slug, self.expand(cwd), title, RUN_PERMISSION if run else None,
                    int(mtime * 1000), int((start or mtime) * 1000)))
        if last:
            db.execute("INSERT INTO message VALUES (?, ?, 1, ?)", ("msg_" + sid, sid, json.dumps(last)))
        db.commit()
        db.close()
        return sid

    def oc_process(self, cwd, *args, **env):
        """A live process that looks like an opencode TUI: argv[0] named
        opencode (python3 behind a symlink), args (-s <file> runs <file>,
        -m <module> runs <module>), working folder cwd."""
        cwd = Path(self.expand(cwd))
        cwd.mkdir(parents=True, exist_ok=True)
        link = self.tmp / "bin" / "opencode"
        if not link.exists():
            link.parent.mkdir()
            link.symlink_to(sys.executable)
        for a in args:
            if a.startswith(("ses_", "oc_sleep")):
                (cwd / (a + ".py" if a == "oc_sleep" else a)).write_text("import time; time.sleep(600)\n")
        p = subprocess.Popen([str(link), *args], cwd=cwd, env=self.env(**env))
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        deadline = time.monotonic() + 10
        while Path(f"/proc/{p.pid}/cmdline").read_bytes().split(b"\0")[0] != str(link).encode():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        return p

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
        self.r = subprocess.run([sys.executable, str(REPO / "sessions"), *args],
                                env=self.env(**env), cwd=self.tmp, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        return self.r.stdout

    def write_daemon_file(self):
        """Run --watch until it has written the daemon file, then kill it."""
        p = subprocess.Popen([sys.executable, str(REPO / "sessions"), "--watch"],
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
        self.assertTrue(self.daemon.exists(), "--watch should write $XDG_RUNTIME_DIR/lobby-sessions")


class SessionsTest(Case):

    def test_readme_example(self):
        self.snapshot(self.at("18:52", 1))
        site = self.session(self.main, name="SITE-DEPLOY", cwd="~/src/site", mtime=self.at("11:35"))
        self.pid_file(self.main, site, tmux="web:@3.%7", run=True)
        xtal = self.session(self.alt, name="XTAL", cwd="~/stage", mtime=self.at("10:02"))
        self.pid_file(self.alt, xtal, status="busy", run=True)
        self.session(self.alt, name="XTAL-PILOT", cwd="~/stage/p", mtime=self.at("08:00", 3))
        self.session(self.main, name="XTAL-REPLAY-B", cwd="~/stage", mtime=self.at("08:00", 4))
        t0 = self.at("09:00", 5)
        self.session(self.main, name="LAB", cwd="~/src/lab", start=t0, mtime=self.at("12:00", 4), calls=[
            (t0 + 60, "cd ~/src/lab && tmux new -d -s r1 claude && node run.js --dir ./calls")])
        replay = self.session(self.main, ai="lab-replay", cwd="~/src/lab", start=t0 + 120,
                              mtime=self.at("17:02", 3))
        self.pid_file(self.main, replay, tmux="r1:@1.%1")
        for i in range(40):
            self.oc_session(title=f"judge-{i}", cwd=f"~/src/lab/calls/{i}", run=True, start=t0 + 180,
                            worktree="~/src/lab", mtime=self.at("09:14", 2) - i * 60)
        expected = EXAMPLE.replace("since Wed", f"since {self.ddd(1)}").replace(
            "Tue 09:14", f"{self.ddd(2)} 09:14").replace("Mon 17:02", f"{self.ddd(3)} 17:02")
        self.assertEqual(self.run_cs(), expected, self.r.stderr)
        lines = expected.split("\n")
        lines[2:7] = ["  11:35\tclaude\tSITE-DEPLOY\tidle\ttmux=web\tcwd=~/src/site",
                      "  10:02\tclaude-alt\tXTAL-* (3)\tbusy\t\tcwd=~/stage",
                      f"  {self.ddd(2)} 09:14\tclaude\tLAB\t\t\tcwd=~/src/lab",
                      f"  {self.ddd(2)} 09:14\topencode\t· runs (40)\t\t\tcwd=~/src/lab",
                      f"  {self.ddd(3)} 17:02\tclaude\t· lab-replay\t\ttmux=r1\tcwd=~/src/lab"]
        self.assertEqual(self.run_cs("--tsv"), "\n".join(lines), self.r.stderr)

    def test_tsv_fits_to_LINES(self):
        for i in range(10):
            self.session(self.main if i % 2 else self.alt, ai=f"m{i}", mtime=self.at("09:00", 10 + i))
        # header and blank count; at least one row
        for lines, n in ((6, 4), (40, 10), (1, 1)):
            with self.subTest(LINES=lines):
                out = self.run_cs("--tsv", "-n", "1", LINES=lines)
                self.assertEqual(names(out), [f"m{i}" for i in range(n)], out)

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
            return {c[2]: c[4] for c in (l.split("\t") for l in out.splitlines() if "\t" in l)}

        self.assertEqual(cells(), want, self.r.stderr)
        self.assertIn(f"{a}\trec\n", memory.read_text())
        shutil.rmtree(self.main / "sessions")  # Claude deletes sessions/<pid>.json on exit
        self.assertEqual(cells(), want, "tmux.tsv should remember the tmux session")

    def test_folding(self):
        x = self.ddd(1)
        self.session(self.main, name="XTAL-A-1", cwd="~/x", mtime=self.at("10:00"))
        busy = self.session(self.alt, name="XTAL-A-2", cwd="~/y", mtime=self.at("09:00"))
        self.pid_file(self.alt, busy, tmux="xt:@1.%1", status="busy", run=True)
        self.session(self.main, name="XTAL-B", cwd="~/x", mtime=self.at("08:00"))
        fa = self.session(self.main, name="FR-A", cwd="~/f", mtime=self.at("07:00"))  # FR: too short
        fb = self.session(self.main, name="FR-B", cwd="~/f", mtime=self.at("06:00"))
        notes = self.session(self.main, ai="notes", cwd="~/n", mtime=self.at("05:00"))
        self.session(self.main, ai="notes", cwd="~/elsewhere", mtime=self.at("04:00"))
        long = "N" * 31  # cut to 31 + "…" after "-*"
        one = self.session(self.main, ai=long + "-one", cwd="~/g", mtime=self.at("08:00", 1))
        self.session(self.main, ai=long + "-two", cwd="~/g", mtime=self.at("07:00", 1))
        ja = self.oc_session(title="judge-a", cwd="~/lab/a", run=True, worktree="~/lab",
                             mtime=self.at("06:00", 1))
        self.oc_session(title="judge-b", cwd="~/lab/b", run=True, worktree="~/lab", mtime=self.at("05:00", 1))
        probe = self.oc_session(title="probe", cwd="/tmp/p", run=True, mtime=self.at("04:00", 1))
        self.oc_session(title="probe", cwd="/tmp/q", run=True, mtime=self.at("03:00", 1))
        self.oc_session(title="XTAL-C", cwd="~/x", mtime=self.at("02:00", 2))  # TUI: folds by name
        out = self.run_cs("--tsv", "-r")
        self.assertEqual([l for l in out.splitlines() if l.startswith("  ")], [
            "  10:00\tclaude-alt\tXTAL-* (4)\tbusy\ttmux=xt\tcwd=~/y",
            f"     cd ~/y && claude-alt --resume {busy}",
            "  07:00\tclaude\tFR-A\t\t\tcwd=~/f", f"     cd ~/f && claude --resume {fa}",
            "  06:00\tclaude\tFR-B\t\t\tcwd=~/f", f"     cd ~/f && claude --resume {fb}",
            "  05:00\tclaude\tnotes (2)\t\t\tcwd=~/n", f"     cd ~/n && claude --resume {notes}",
            f"  {x} 08:00\tclaude\t{long}… (2)\t\t\tcwd=~/g", f"     cd ~/g && claude --resume {one}",
            f"  {x} 06:00\topencode\truns (2)\t\t\tcwd=~/lab", f"     cd ~/lab/a && opencode -s {ja}",
            f"  {x} 04:00\topencode\truns (2)\t\t\tcwd=/tmp/p", f"     cd /tmp/p && opencode -s {probe}"],
            self.r.stderr)
        self.assertEqual(len(names(self.run_cs("--tsv", "-u"))), 14, "-u: one row per session")
        self.assertEqual(names(self.run_cs("--tsv", "xtal-a")), ["XTAL-A-* (2)"],
                         "words filter before folding")
        self.assertEqual(names(self.run_cs("--tsv", "-u", "xtal-a")), ["XTAL-A-1", "XTAL-A-2"])

    def test_word_filter(self):
        self.session(self.main, name="Deploy Site", ai="kiwi", prompt="fix the BUG",
                     mtime=self.at("09:00", 10))
        self.session(self.main, ai="Notes", prompt="deploy later", mtime=self.at("09:00", 11))
        self.session(self.main, ai="misc", cwd="~/qzfolder", mtime=self.at("09:00", 12),
                     sid="5e55a1d0-0000-4000-8000-000000000000")
        self.session(self.main, ai="pair", prompt="zebra", mtime=self.at("09:00", 14))
        self.session(self.main, ai="pair", prompt="yak", mtime=self.at("09:00", 13))
        self.oc_session(title="Lab Notes", cwd="~/lab", mtime=self.at("09:00", 15),
                        sid="ses_0c0de")
        for words, want in ((["deploy"], ["Deploy Site", "Notes"]),
                            (["DEPLOY", "bug"], ["Deploy Site"]),
                            (["Kiwi"], ["Deploy Site"]),
                            (["qzfolder"], ["misc"]),
                            (["5E55A1D0"], ["misc"]),
                            (["pair"], ["pair (2)"]),
                            (["zebra"], ["pair"]),
                            (["notes"], ["Notes", "Lab Notes"]),
                            (["ses_0C0DE"], ["Lab Notes"]),
                            (["nothing-like-this"], ["(none)"])):
            with self.subTest(words=words):
                self.assertEqual(names(self.run_cs("--tsv", *words)), want)

    def test_resume_lines_and_long_folders(self):
        deep, srv = "~/research/projects/2026/experiments/run_c", "/srv/data/archive/2026/q3/experiments/x/y"
        t1, t2, t3, t4 = (self.at("09:00", 10 + i) for i in range(4))
        m = self.session(self.main, ai="deep", cwd=deep, mtime=t1)
        o = self.session(self.main, ai="srv", cwd=srv, mtime=t2)
        a = self.session(self.alt, ai="alt one", cwd="~/notes", mtime=t3)
        c = self.oc_session(slug="calm-river", title="New session - 2026-10-19T12:03:20.677Z",
                            cwd="~/oc", mtime=t4)
        r1, r2, r3, r4 = pad([["  " + self.when(t1), "claude", "deep", "", "", "cwd=~/…/experiments/run_c"],
                              ["  " + self.when(t2), "claude", "srv", "", "", "cwd=/…/x/y"],
                              ["  " + self.when(t3), "claude-alt", "alt one", "", "", "cwd=~/notes"],
                              ["  " + self.when(t4), "opencode", "calm-river", "", "", "cwd=~/oc"]])
        self.assertEqual(self.run_cs("-r"), "\n".join([
            "SESSIONS  -  by last activity", "",
            r1, f"     cd {deep} && claude --resume {m}",
            r2, f"     cd {srv} && claude --resume {o}",
            r3, f"     cd ~/notes && claude-alt --resume {a}",
            r4, f"     cd ~/oc && opencode -s {c}", ""]), self.r.stderr)

    def test_parents(self):
        t0 = self.at("08:00")
        boss = self.session(self.main, name="BOSS", cwd="~/stage", mtime=self.at("09:00"), start=t0, calls=[
            (t0 + 60, "for s in a b; do tmux new-session -d -s loop-$s -c ~/lab 'node loop.js'; done"),
            (t0 + 70, "cd ~/lab && node launch.js --dir experiments/x"),
            (t0 + 80, "python3 - <<'EOF'\nprint('tmux new-session -s quoted')\nEOF"),
            (t0 + 90, "ls ~/lab")])
        before = self.session(self.main, name="early", start=t0 + 30, mtime=self.at("08:30"))
        self.pid_file(self.main, before, tmux="loop-a:@1.%1")  # started before loop-a was made
        after = self.session(self.main, name="worker", start=t0 + 100, mtime=self.at("08:40"))
        self.pid_file(self.main, after, tmux="loop-b:@1.%1")
        quoted = self.session(self.main, name="quoted-only", start=t0 + 100, mtime=self.at("08:35"))
        self.pid_file(self.main, quoted, tmux="quoted:@1.%1")
        scratch = f"/tmp/claude-1000/-home-me-stage/{boss}/scratchpad/w"
        self.oc_session(title="in scratchpad", cwd=scratch, run=True, start=t0 + 100, mtime=self.at("08:20"))
        self.oc_session(title="in experiment", cwd="~/lab/experiments/x/0001/cwd", run=True, worktree="~/lab",
                        start=t0 + 100, mtime=self.at("08:10"))
        self.oc_session(title="only root named", cwd="~/lab/other", run=True, worktree="~/lab",
                        start=t0 + 100, mtime=self.at("08:05"))
        rows = [l.split("\t")[2] for l in self.run_cs("--tsv", "-u").splitlines() if "\t" in l]
        self.assertEqual(rows, ["BOSS", "· worker", "· in scratchpad", "· in experiment",
                                "quoted-only", "early", "only root named"], self.r.stderr)
        self.assertEqual(names(self.run_cs("--tsv", "boss")),
                         ["BOSS", "· worker", "· in scratchpad", "· in experiment"],
                         "a word keeps the descendants of a match")
        self.assertEqual(self.run_cs("--tsv", "boss").splitlines()[2].split("\t")[0], "  09:00",
                         "a parent's time is the newest in its subtree")

    def test_children_shown(self):
        t0 = self.at("08:00")
        for n, want in ((7, 7), (10, 8), (24, 12)):
            with self.subTest(children=n):
                shutil.rmtree(self.main, ignore_errors=True)
                self.session(self.main, name="BOSS", start=t0, calls=[(t0 + 1, "tmux new -s w-$i 'claude'")],
                             mtime=self.at("07:00"))
                for i in range(n):
                    c = self.session(self.main, ai=f"child{i}", start=t0 + 10, mtime=self.at("06:00") - i * 60)
                    self.pid_file(self.main, c, tmux=f"w-{i}:@1.%1")
                out = names(self.run_cs("--tsv", "-n", "0"))
                self.assertEqual(out, ["BOSS"] + [f"· child{i}" for i in range(want)]
                                 + ([f"· +{n - want} more"] if want < n else []))

    def test_running_opencode_tui(self):
        fake_tmux = self.tmp / "bin2" / "tmux"
        fake_tmux.parent.mkdir()
        fake_tmux.write_text('#!/bin/sh\n[ "$*" = "-S /sock display-message -p -t %9 #S" ] && echo lab\n')
        fake_tmux.chmod(0o755)
        named = self.oc_session(title="named", cwd="~/a", mtime=self.at("09:00", 1), sid="ses_named",
                                last={"role": "assistant", "time": {"created": 1, "completed": 2}})
        self.oc_process("~/a", "-s", named, TMUX="/sock,1,0", TMUX_PANE="%9")
        self.oc_process("~/b", "-m", "oc_sleep")  # no -s: the newest session there since it started
        self.oc_session(title="old", cwd="~/b", mtime=self.now - 600)
        self.oc_session(title="fresh", cwd="~/b", mtime=time.time() + 5, last={"role": "user"})
        self.oc_session(title="in run", cwd="~/b", mtime=time.time() + 6, run=True, worktree="~/b")
        out = self.run_cs("--tsv", "-u", PATH=f"{fake_tmux.parent}:{os.environ['PATH']}")
        rows = {c[2]: c[3:5] for c in (l.split("\t") for l in out.splitlines() if "\t" in l)}
        self.assertEqual(rows, {"named": ["idle", "tmux=lab"], "fresh": ["busy", ""],
                                "old": ["", ""], "in run": ["", ""]}, self.r.stderr)

    def test_daemon_file_used_only_when_fresh_and_same_home(self):
        self.session(self.main, ai="before")
        self.write_daemon_file()
        self.session(self.main, ai="after", mtime=self.at("09:00", 1))
        self.assertEqual(names(self.run_cs("--tsv")), ["before"],
                         "a fresh daemon file is used instead of the folders")
        other = self.home
        self.home = self.tmp / "other"
        self.session(self.home / ".claude", ai="elsewhere")
        self.assertEqual(names(self.run_cs("--tsv")), ["elsewhere"],
                         "a daemon file written for another $HOME is ignored")
        self.home = other
        old = time.time() - 31
        os.utime(self.daemon, (old, old))
        self.assertEqual(names(self.run_cs("--tsv")), ["after", "before"],
                         "a daemon file older than 30 s is ignored")

    def test_watch_without_runtime_dir_fails(self):
        self.session(self.main, ai="alpha")
        try:
            self.run_cs("--watch", timeout=10, XDG_RUNTIME_DIR=None)
        except subprocess.TimeoutExpired:
            self.fail("--watch without $XDG_RUNTIME_DIR should exit with an error, not run")
        self.assertNotEqual(self.r.returncode, 0)
        self.assertEqual(names(self.run_cs("--tsv", XDG_RUNTIME_DIR=None)), ["alpha"])


if __name__ == "__main__":
    unittest.main()
