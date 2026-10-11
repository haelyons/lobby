"""Black-box tests for claude-sessions, written from README.txt only.

Every expected value is derived from the documented rules (CLAUDE-SESSIONS,
"Where the data comes from" and "The tmux cell"), never from program output.
Fixtures are made-up config folders under a temporary $HOME.

The program runs with a fixed-offset local time zone (UTC+05:30, no DST) so
that "local time" is well defined and differs from UTC.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROGRAM = REPO / "claude-sessions"

TZ_ENV = "XYZ-05:30"  # POSIX TZ: local time is UTC+05:30
LOCAL = timezone(timedelta(hours=5, minutes=30))

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

HEADER = "CLAUDE SESSIONS  -  by last activity"
SINCE = "  -  tmux recorded since "
MAIN_TITLE = "MAIN      (claude -> ~/.claude)"
ALT_TITLE = "ALT       (claude-alt -> ~/.claude-alt)"
NONE_LINE = "  (none)"
DAY = 86400
_DEFAULT = object()


def fmt_time(ts, now):
    """The documented time format (without the row's two leading spaces):
    same day as now -> HH:MM; less than 6 x 24 h ago -> Ddd HH:MM;
    older -> D Mon. Times within 10 minutes of the 6 x 24 h boundary are
    refused, so the gap between building fixtures and running the program
    cannot flip the bucket.
    """
    d = datetime.fromtimestamp(ts, LOCAL)
    n = datetime.fromtimestamp(now, LOCAL)
    hhmm = f"{d.hour:02d}:{d.minute:02d}"
    if d.date() == n.date():
        return hhmm
    age = now - ts
    if abs(age - 6 * DAY) < 600:
        raise ValueError("test bug: timestamp too close to 6 x 24 h")
    if 0 <= age < 6 * DAY:
        return f"{WEEKDAYS[d.weekday()]} {hhmm}"
    return f"{d.day} {MONTHS[d.month - 1]}"


def pad_rows(rows):
    """The documented padded layout: each column as wide as its widest cell,
    columns separated by three spaces, trailing spaces removed."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["   ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
            for r in rows]


def proc_start(pid):
    """Field 22 of /proc/<pid>/stat (the process start time)."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rindex(")") + 2:].split()  # fields[0] is field 3
    return fields[22 - 3]


def pane(session, title, folder, command="claude", full=":claude"):
    """A tmux-resurrect "pane" line: 2nd session, 7th title, 8th ":"+folder,
    10th pane command, 11th ":"+full command."""
    return "\t".join(["pane", session, "1", "1", ":*", "0", title,
                      ":" + folder, "1", command, full])


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


class ClaudeSessionsCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cs_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.main = self.home / ".claude"
        self.alt = self.home / ".claude-alt"
        self.tmux_tsv = self.home / ".local/state/claude-sessions/tmux.tsv"
        self.resurrect = self.home / ".local/share/tmux/resurrect"
        self.now = time.time()
        self.result = None

    # ---- time helpers -------------------------------------------------
    def days_ago(self, days):
        return self.now - days * DAY

    def today_ts(self, frac):
        """A moment earlier today (local time), whatever the time of day."""
        n = datetime.fromtimestamp(self.now, LOCAL)
        midnight = n.replace(hour=0, minute=0, second=0,
                             microsecond=0).timestamp()
        return midnight + frac * (self.now - midnight)

    def time_cell(self, ts):
        return "  " + fmt_time(ts, self.now)

    # ---- fixture builders ---------------------------------------------
    def add_session(self, config, *, sid=None, custom=None, ai=None,
                    prompt=None, cwd=_DEFAULT, mtime=None, project=None,
                    lines=None):
        """Write <config>/projects/<project>/<sid>.jsonl and set its mtime."""
        config.mkdir(parents=True, exist_ok=True)
        sid = sid or str(uuid.uuid4())
        if cwd is _DEFAULT:
            cwd = str(self.home / "proj")
        if project is None:
            project = (cwd or "/misc").replace("/", "-")
        if lines is None:
            lines = []
            if cwd:
                lines.append({"type": "user", "cwd": cwd, "sessionId": sid,
                              "message": {"role": "user",
                                          "content": "hello"}})
            lines.append({"type": "assistant", "sessionId": sid,
                          "message": {"role": "assistant",
                                      "content": "hi"}})
            for t in _as_list(custom):
                lines.append({"type": "custom-title", "customTitle": t,
                              "sessionId": sid})
            for t in _as_list(ai):
                lines.append({"type": "ai-title", "aiTitle": t,
                              "sessionId": sid})
            for t in _as_list(prompt):
                lines.append({"type": "last-prompt", "lastPrompt": t,
                              "sessionId": sid})
        path = config / "projects" / project / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(x) + "\n" for x in lines),
                        encoding="utf-8")
        if mtime is None:
            mtime = self.today_ts(0.5)
        os.utime(path, (mtime, mtime))
        return sid

    def add_many(self, config, count, prefix):
        """count sessions with distinct names, newest first by index."""
        return [self.add_session(config, ai=f"{prefix} {i:02d}",
                                 cwd=str(self.home / prefix),
                                 mtime=self.days_ago(10 + i))
                for i in range(count)]

    def start_process(self):
        p = subprocess.Popen(["sleep", "600"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, p)
        return p, proc_start(p.pid)

    @staticmethod
    def _stop(p):
        if p.poll() is None:
            p.kill()
        p.wait()

    def write_session_file(self, config, pid, sid, start, status="idle",
                           tmux=None):
        data = {"pid": pid, "sessionId": sid, "procStart": start}
        if status is not None:
            data["status"] = status
        data["startedAt"] = int(self.now * 1000)
        if tmux is not None:
            data["tmux"] = tmux
        d = config / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")

    def add_running(self, config, sid, status="idle", tmux=None):
        """Start a real child process and record it as running session sid."""
        p, start = self.start_process()
        self.write_session_file(config, p.pid, sid, start, status, tmux)
        return p

    def add_snapshot(self, ts, lines, mtime=None):
        """A tmux-resurrect snapshot named after local time ts; its mtime is
        ts too unless given."""
        ts = int(ts)
        self.resurrect.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(ts, LOCAL).strftime("%Y%m%dT%H%M%S")
        path = self.resurrect / f"tmux_resurrect_{stamp}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        mtime = ts if mtime is None else mtime
        os.utime(path, (mtime, mtime))
        return ts

    def seed_tmux_tsv(self, text):
        self.tmux_tsv.parent.mkdir(parents=True, exist_ok=True)
        self.tmux_tsv.write_text(text, encoding="utf-8")

    # ---- running and parsing ------------------------------------------
    def run_cs(self, *args, lines=None):
        drop_prefixes = ("XDG_", "CLAUDE", "TMUX", "LC_")
        drop = {"LINES", "COLUMNS", "TZ", "LANG", "LANGUAGE"}
        env = {k: v for k, v in os.environ.items()
               if k not in drop and not k.startswith(drop_prefixes)}
        env.update(HOME=str(self.home), TZ=TZ_ENV, LC_ALL="C.UTF-8")
        if lines is not None:
            env["LINES"] = str(lines)
        self.result = subprocess.run(
            [sys.executable, str(PROGRAM), *args], env=env, cwd=str(self.tmp),
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        return self.result.stdout

    def ctx(self):
        r = self.result
        if r is None:
            return ""
        return (f"\n--- exit {r.returncode}; stdout ---\n{r.stdout}"
                f"--- stderr ---\n{r.stderr}")

    @staticmethod
    def out_lines(out):
        out = out.rstrip("\n")
        return out.split("\n") if out else []

    def sections(self, out):
        """{label: [lines under that section title]} for MAIN and ALT."""
        secs, cur = {}, None
        for line in self.out_lines(out)[1:]:
            words = line.split()
            if line and not line.startswith(" ") and words[0] in ("MAIN",
                                                                   "ALT"):
                cur = secs.setdefault(words[0], [])
            elif line == "":
                cur = None
            elif cur is not None:
                cur.append(line)
        return secs

    def tsv_rows(self, out, label):
        return [l.split("\t") for l in self.sections(out).get(label, [])
                if "\t" in l]

    def tsv_names(self, out, label):
        return [cells[1] for cells in self.tsv_rows(out, label)
                if len(cells) > 1]

    def tsv_row(self, out, label, name):
        for cells in self.tsv_rows(out, label):
            if len(cells) > 1 and cells[1] == name:
                return cells
        self.fail(f"expected a {label} row named {name!r}" + self.ctx())

    def cell(self, cells, index):
        self.assertEqual(len(cells), 5,
                         "a --tsv row should have exactly 5 TAB-joined cells"
                         + self.ctx())
        return cells[index]


# ======================================================================
class TestLayout(ClaudeSessionsCase):

    def test_empty_config_folders_give_header_titles_and_none(self):
        self.main.mkdir()
        self.alt.mkdir()
        out = self.run_cs()
        self.assertEqual(
            self.out_lines(out),
            [HEADER, "", MAIN_TITLE, NONE_LINE, "", ALT_TITLE, NONE_LINE],
            "with two empty config folders the output should be the header, "
            "a blank line, MAIN title, (none), a blank line, ALT title, "
            "(none)" + self.ctx())

    def test_missing_alt_folder_gets_no_section(self):
        self.add_session(self.main, ai="only main")
        out = self.run_cs()
        self.assertIn(MAIN_TITLE, self.out_lines(out),
                      "MAIN section should be shown when ~/.claude exists"
                      + self.ctx())
        self.assertNotIn("ALT", self.sections(out),
                         "no ALT section should be shown when ~/.claude-alt "
                         "does not exist" + self.ctx())

    def test_missing_main_folder_gets_no_section(self):
        self.add_session(self.alt, ai="only alt")
        out = self.run_cs()
        self.assertIn(ALT_TITLE, self.out_lines(out),
                      "ALT section should be shown when ~/.claude-alt exists"
                      + self.ctx())
        self.assertNotIn("MAIN", self.sections(out),
                         "no MAIN section should be shown when ~/.claude "
                         "does not exist" + self.ctx())

    def test_padded_output_matches_documented_layout(self):
        snap = self.add_snapshot(self.days_ago(4), [
            pane("web", "shell", "/tmp", command="bash", full=":bash"),
            "state\tweb\t"])
        t_site = self.today_ts(0.9)
        site = self.add_session(self.main, custom="SITE-DEPLOY",
                                ai="deploy the site",
                                cwd=str(self.home / "src/site"),
                                mtime=t_site)
        self.add_running(self.main, site, status="idle", tmux="web:@3.%7")
        t_notes = self.today_ts(0.8)
        notes = str(self.home / "notes")
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(2))
        self.add_session(self.main, ai="notes", cwd=notes, mtime=t_notes)
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(1))
        t_fix = self.days_ago(3)
        self.add_session(self.main, ai="fix flaky test",
                         cwd=str(self.home / "src/api"), mtime=t_fix)
        self.alt.mkdir()

        rows = pad_rows([
            [self.time_cell(t_site), "SITE-DEPLOY", "idle", "tmux=web",
             "cwd=~/src/site"],
            [self.time_cell(t_notes), "notes (3)", "", "", "cwd=~/notes"],
            [self.time_cell(t_fix), "fix flaky test", "", "",
             "cwd=~/src/api"],
        ])
        expected = ([HEADER + SINCE + fmt_time(snap, self.now), "",
                     MAIN_TITLE] + rows + ["", ALT_TITLE, NONE_LINE])
        out = self.run_cs()
        self.assertEqual(self.out_lines(out), expected,
                         "padded output should follow the documented layout "
                         "(as in the README example)" + self.ctx())

    def test_columns_align_across_sections(self):
        self.add_session(self.main, ai="alpha", cwd=str(self.home / "x"),
                         mtime=self.today_ts(0.5))
        self.add_session(self.alt, ai="a much longer session name",
                         cwd=str(self.home / "y"), mtime=self.days_ago(3))
        out = self.run_cs()
        secs = self.sections(out)
        main_row = secs.get("MAIN", [""])[0]
        alt_row = secs.get("ALT", [""])[0]
        self.assertIn("alpha", main_row, "MAIN row expected" + self.ctx())
        self.assertIn("a much longer", alt_row, "ALT row expected" + self.ctx())
        self.assertEqual(main_row.index("alpha"),
                         alt_row.index("a much longer"),
                         "name column should start at the same position in "
                         "both sections (widths are over the whole output)"
                         + self.ctx())
        self.assertEqual(main_row.index("cwd="), alt_row.index("cwd="),
                         "cwd column should start at the same position in "
                         "both sections (widths are over the whole output)"
                         + self.ctx())

    def test_padded_lines_have_no_trailing_spaces(self):
        self.add_session(self.main, ai="short", cwd=str(self.home / "a"),
                         mtime=self.today_ts(0.5))
        self.add_session(self.main, ai="other",
                         cwd=str(self.home / "a/much/longer/folder"),
                         mtime=self.days_ago(2))
        out = self.run_cs()
        self.assertEqual(len(self.sections(out).get("MAIN", [])), 2,
                         "expected the two MAIN rows to check" + self.ctx())
        for line in self.out_lines(out):
            self.assertFalse(line.endswith(" "),
                             f"trailing spaces should be removed: {line!r}"
                             + self.ctx())

    def test_tsv_joins_cells_with_one_tab_and_keeps_empty_cells(self):
        t = self.today_ts(0.5)
        self.add_session(self.main, ai="plain session",
                         cwd=str(self.home / "proj"), mtime=t)
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.sections(out).get("MAIN"),
            [self.time_cell(t) + "\tplain session\t\t\tcwd=~/proj"],
            "--tsv row should be the 5 cells joined by single TABs, with the "
            "empty state and tmux cells kept" + self.ctx())


# ======================================================================
class TestHeader(ClaudeSessionsCase):

    def test_header_without_snapshots(self):
        self.main.mkdir()
        out = self.run_cs()
        self.assertEqual(self.out_lines(out)[:2], [HEADER, ""],
                         "without tmux-resurrect snapshots the header is "
                         "exactly the plain title, followed by a blank line"
                         + self.ctx())

    def test_header_names_time_of_oldest_snapshot(self):
        self.main.mkdir()
        self.add_snapshot(self.days_ago(1), ["state\tweb\t"])
        oldest = self.add_snapshot(self.days_ago(3), ["state\tweb\t"])
        out = self.run_cs()
        lines = self.out_lines(out)
        self.assertEqual(lines[:1],
                         [HEADER + SINCE + fmt_time(oldest, self.now)],
                         "header should continue with the time of the oldest "
                         "snapshot in the documented time format" + self.ctx())

    def test_header_time_comes_from_file_name_not_mtime(self):
        self.main.mkdir()
        named = self.add_snapshot(self.days_ago(3), ["state\tweb\t"],
                                  mtime=self.today_ts(0.5))
        out = self.run_cs()
        self.assertEqual(self.out_lines(out)[:1],
                         [HEADER + SINCE + fmt_time(named, self.now)],
                         "the header time should be the time in the "
                         "snapshot's file name, not its modification time"
                         + self.ctx())

    def test_header_picks_oldest_snapshot_by_file_name(self):
        self.main.mkdir()
        oldest = self.add_snapshot(self.days_ago(3), ["state\tweb\t"],
                                   mtime=self.today_ts(0.5))
        self.add_snapshot(self.days_ago(1), ["state\tweb\t"],
                          mtime=self.days_ago(20))
        out = self.run_cs()
        self.assertEqual(self.out_lines(out)[:1],
                         [HEADER + SINCE + fmt_time(oldest, self.now)],
                         "the oldest snapshot is the one with the oldest "
                         "file-name time, whatever the modification times"
                         + self.ctx())


# ======================================================================
class TestTimeCell(ClaudeSessionsCase):

    def test_same_day_shows_hours_and_minutes(self):
        t = self.today_ts(0.5)
        self.add_session(self.main, ai="today", mtime=t)
        out = self.run_cs("--tsv")
        d = datetime.fromtimestamp(t, LOCAL)
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "today"), 0),
                         f"  {d.hour:02d}:{d.minute:02d}",
                         "a transcript modified today should show two "
                         "spaces and HH:MM in local time" + self.ctx())

    def test_under_six_days_shows_weekday_and_time(self):
        t = self.days_ago(3)
        self.add_session(self.main, ai="recent", mtime=t)
        out = self.run_cs("--tsv")
        d = datetime.fromtimestamp(t, LOCAL)
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "recent"), 0),
                         f"  {WEEKDAYS[d.weekday()]} "
                         f"{d.hour:02d}:{d.minute:02d}",
                         "a transcript modified 3 days ago should show "
                         "'Ddd HH:MM'" + self.ctx())

    def test_just_under_6x24h_shows_weekday_and_time(self):
        # 5 days 23 h ago: usually 6 calendar days back, still < 6 x 24 h.
        t = self.days_ago(6) + 3600
        self.add_session(self.main, ai="edge", mtime=t)
        out = self.run_cs("--tsv")
        d = datetime.fromtimestamp(t, LOCAL)
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "edge"), 0),
                         f"  {WEEKDAYS[d.weekday()]} "
                         f"{d.hour:02d}:{d.minute:02d}",
                         "a transcript modified 5 days 23 hours ago (less "
                         "than 6 x 24 h) should show 'Ddd HH:MM'" + self.ctx())

    def test_just_over_6x24h_shows_day_and_month(self):
        t = self.days_ago(6) - 3600
        self.add_session(self.main, ai="edge", mtime=t)
        out = self.run_cs("--tsv")
        d = datetime.fromtimestamp(t, LOCAL)
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "edge"), 0),
                         f"  {d.day} {MONTHS[d.month - 1]}",
                         "a transcript modified 6 days 1 hour ago (not less "
                         "than 6 x 24 h) should show 'D Mon'" + self.ctx())

    def test_older_shows_day_without_leading_zero_and_month(self):
        t = datetime(2025, 3, 7, 12, 0, tzinfo=LOCAL).timestamp()
        self.add_session(self.main, ai="old", mtime=t)
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "old"), 0),
                         "  7 Mar",
                         "an old transcript should show 'D Mon' with no "
                         "leading zero" + self.ctx())

    def test_time_is_local_not_utc(self):
        # 01:00 on 7 Mar at UTC+05:30 is still 6 Mar in UTC.
        t = datetime(2025, 3, 7, 1, 0, tzinfo=LOCAL).timestamp()
        self.add_session(self.main, ai="early", mtime=t)
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "early"), 0),
                         "  7 Mar",
                         "the modification time should be shown in local "
                         "time ($TZ), not UTC" + self.ctx())

    def test_rows_are_newest_first(self):
        self.add_session(self.main, ai="mid", mtime=self.days_ago(2))
        self.add_session(self.main, ai="old", mtime=self.days_ago(10))
        self.add_session(self.main, ai="new", mtime=self.today_ts(0.5))
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["new", "mid", "old"],
                         "rows should be ordered by transcript modification "
                         "time, newest first" + self.ctx())


# ======================================================================
class TestNameCell(ClaudeSessionsCase):

    def test_rename_name_preferred_over_auto_title(self):
        self.add_session(self.main, custom="my-name", ai="Auto title here")
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["my-name"],
                         "the /rename name should be shown in preference to "
                         "the auto title" + self.ctx())

    def test_auto_title_used_without_rename_name(self):
        self.add_session(self.main, ai="Auto title here", prompt="do it")
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["Auto title here"],
                         "without a /rename name the auto title should be "
                         "shown" + self.ctx())

    def test_session_id_prefix_used_without_any_title(self):
        sid = "a1b2c3d4-0000-4000-8000-000000000001"
        self.add_session(self.main, sid=sid, prompt="only a prompt")
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["a1b2c3d4"],
                         "a session with only a last prompt should be listed, "
                         "named by the first 8 characters of its id"
                         + self.ctx())

    def test_later_rename_line_replaces_earlier(self):
        self.add_session(self.main, custom=["first name", "second name"])
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["second name"],
                         "a later custom-title line should replace an earlier "
                         "one" + self.ctx())

    def test_later_auto_title_line_replaces_earlier(self):
        self.add_session(self.main, ai=["first title", "second title"])
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["second title"],
                         "a later ai-title line should replace an earlier one"
                         + self.ctx())

    def test_name_over_32_characters_is_cut_to_31_plus_ellipsis(self):
        name = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"  # 40 characters
        self.add_session(self.main, ai=name)
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), [name[:31] + "…"],
                         "a name over 32 characters should become its first "
                         "31 characters and '…'" + self.ctx())

    def test_name_of_exactly_32_characters_is_kept(self):
        name = "abcdefghijklmnopqrstuvwxyz012345"
        self.assertEqual(len(name), 32)
        self.add_session(self.main, ai=name)
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), [name],
                         "a name of exactly 32 characters should be shown in "
                         "full" + self.ctx())


# ======================================================================
class TestTranscripts(ClaudeSessionsCase):

    def test_transcript_without_titles_or_prompt_is_not_listed(self):
        self.add_session(self.main)  # only user/assistant lines with a cwd
        out = self.run_cs("--tsv")
        self.assertEqual(self.sections(out).get("MAIN"), [NONE_LINE],
                         "a transcript with no rename name, auto title or "
                         "last prompt should not be listed" + self.ctx())

    def test_transcripts_in_deeper_folders_are_ignored(self):
        sid = self.add_session(self.main, ai="parent session")
        project = (str(self.home / "proj")).replace("/", "-")
        sub = self.main / "projects" / project / sid / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1234.jsonl").write_text(json.dumps(
            {"type": "custom-title", "customTitle": "SUBAGENT",
             "cwd": "/srv/x"}) + "\n", encoding="utf-8")
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["parent session"],
                         "only the top-level transcript should be listed; "
                         "files in deeper (subagent) folders are ignored"
                         + self.ctx())

    def test_first_cwd_line_gives_the_folder(self):
        sid = str(uuid.uuid4())
        first = str(self.home / "first")
        second = str(self.home / "second")
        self.add_session(self.main, sid=sid, project="-whatever", lines=[
            {"type": "ai-title", "aiTitle": "two folders", "sessionId": sid},
            {"type": "user", "cwd": first, "sessionId": sid},
            {"type": "user", "cwd": second, "sessionId": sid},
        ])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "two folders"), 4),
            "cwd=~/first",
            "the folder should come from the first line that has a cwd"
            + self.ctx())

    def test_folder_from_project_folder_name_without_cwd(self):
        self.add_session(self.main, ai="no cwd", cwd=None,
                         project="-srv-data-proj")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "no cwd"), 4),
                         "cwd=/srv/data/proj",
                         "without any cwd line the folder should be the "
                         "project folder name with '-' read as '/'"
                         + self.ctx())

    def test_alt_sessions_are_listed_under_alt_only(self):
        self.main.mkdir()
        self.add_session(self.alt, ai="alt work")
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "ALT"), ["alt work"],
                         "a session in ~/.claude-alt should be listed in the "
                         "ALT section" + self.ctx())
        self.assertEqual(self.sections(out).get("MAIN"), [NONE_LINE],
                         "a session in ~/.claude-alt should not appear in "
                         "MAIN" + self.ctx())


# ======================================================================
class TestFolderCell(ClaudeSessionsCase):

    def test_home_is_written_as_tilde(self):
        self.add_session(self.main, ai="site", cwd=str(self.home / "src/site"))
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "site"), 4),
                         "cwd=~/src/site",
                         "a folder under $HOME should be written with ~"
                         + self.ctx())

    def test_long_folder_keeps_first_part_and_last_two(self):
        folder = self.home / "projects/research/archive/2026/experiments/run_c"
        self.add_session(self.main, ai="long", cwd=str(folder))
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "long"), 4),
                         "cwd=~/…/experiments/run_c",
                         "a folder longer than 40 characters should keep its "
                         "first part and last two, joined by '/…/'"
                         + self.ctx())

    def test_folder_of_exactly_40_characters_is_kept(self):
        folder = "/srv/data/" + "a" * 14 + "/" + "b" * 15
        self.assertEqual(len(folder), 40)
        self.add_session(self.main, ai="forty", cwd=folder)
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "forty"), 4),
                         "cwd=" + folder,
                         "a folder of exactly 40 characters should not be "
                         "shortened" + self.ctx())

    def test_tilde_form_of_40_characters_is_kept(self):
        rel = "a" * 12 + "/" + "b" * 12 + "/" + "c" * 12
        self.assertEqual(len("~/" + rel), 40)
        self.assertGreater(len(str(self.home / rel)), 40)
        self.add_session(self.main, ai="forty", cwd=str(self.home / rel))
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "forty"), 4),
                         "cwd=~/" + rel,
                         "shortening applies after ~ substitution: a folder "
                         "whose ~ form is 40 characters is kept even though "
                         "its full path is longer" + self.ctx())

    def test_tilde_form_of_41_characters_is_shortened(self):
        rel = "a" * 12 + "/" + "b" * 12 + "/" + "c" * 13
        self.assertEqual(len("~/" + rel), 41)
        self.add_session(self.main, ai="forty-one", cwd=str(self.home / rel))
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "forty-one"), 4),
            "cwd=~/…/" + "b" * 12 + "/" + "c" * 13,
            "a folder whose ~ form is 41 characters should become ~, '/…/' "
            "and its last two parts" + self.ctx())


# ======================================================================
class TestGrouping(ClaudeSessionsCase):

    def test_same_name_and_folder_become_one_row_with_count(self):
        notes = str(self.home / "notes")
        for days in (1, 2, 3):
            self.add_session(self.main, ai="notes", cwd=notes,
                             mtime=self.days_ago(days))
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["notes (3)"],
                         "three sessions with the same name and folder should "
                         "be one row named 'notes (3)'" + self.ctx())

    def test_same_name_in_different_folders_stays_separate(self):
        self.add_session(self.main, ai="notes", cwd=str(self.home / "a"),
                         mtime=self.days_ago(1))
        self.add_session(self.main, ai="notes", cwd=str(self.home / "b"),
                         mtime=self.days_ago(2))
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["notes", "notes"],
                         "sessions with the same name in different folders "
                         "should be separate rows without a count"
                         + self.ctx())

    def test_same_name_and_folder_in_different_sections_stay_separate(self):
        folder = str(self.home / "a")
        self.add_session(self.main, ai="notes", cwd=folder)
        self.add_session(self.alt, ai="notes", cwd=folder)
        out = self.run_cs("--tsv")
        self.assertEqual(
            (self.tsv_names(out, "MAIN"), self.tsv_names(out, "ALT")),
            (["notes"], ["notes"]),
            "grouping happens only within a section: each section should "
            "show its own 'notes' row without a count" + self.ctx())

    def test_grouping_uses_full_name_not_cut_name(self):
        base = "Investigate the flaky login tes"
        self.assertEqual(len(base), 31)
        folder = str(self.home / "app")
        self.add_session(self.main, ai=base + "t on CI runners", cwd=folder,
                         mtime=self.days_ago(1))
        self.add_session(self.main, ai=base + "t on local machines",
                         cwd=folder, mtime=self.days_ago(2))
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"),
                         [base + "…", base + "…"],
                         "two long names that differ only after character 31 "
                         "are different full names: two rows, no count"
                         + self.ctx())

    def test_count_comes_after_ellipsis(self):
        name = "Investigate the flaky login test on CI runners"
        folder = str(self.home / "app")
        self.add_session(self.main, ai=name, cwd=folder,
                         mtime=self.days_ago(1))
        self.add_session(self.main, ai=name, cwd=folder,
                         mtime=self.days_ago(2))
        out = self.run_cs("--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), [name[:31] + "… (2)"],
                         "a grouped long name should be cut to 31 characters "
                         "and '…', then get ' (2)'" + self.ctx())

    def test_group_time_is_the_newest_members_time(self):
        notes = str(self.home / "notes")
        newest = self.days_ago(1)
        self.add_session(self.main, ai="notes", cwd=notes, mtime=newest)
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(3))
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(2))
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "notes (3)"), 0),
                         self.time_cell(newest),
                         "a grouped row should show the newest member's time"
                         + self.ctx())

    def test_group_shows_state_of_running_member(self):
        notes = str(self.home / "notes")
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(1))
        older = self.add_session(self.main, ai="notes", cwd=notes,
                                 mtime=self.days_ago(2))
        self.add_running(self.main, older, status="busy")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "notes (2)"), 2),
                         "busy",
                         "a grouped row should show the running member, here "
                         "its status 'busy'" + self.ctx())

    def test_group_resume_command_uses_running_member(self):
        notes = str(self.home / "notes")
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(1))
        older = self.add_session(self.main, ai="notes", cwd=notes,
                                 mtime=self.days_ago(2))
        self.add_running(self.main, older, status="idle")
        out = self.run_cs("-r")
        self.assertEqual(
            self.sections(out).get("MAIN", [None, None])[1:],
            [f"     cd ~/notes && claude --resume {older}"],
            "the resume line of a grouped row should resume the running "
            "member" + self.ctx())

    def test_group_resume_command_uses_newest_member_when_none_running(self):
        notes = str(self.home / "notes")
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(2))
        newest = self.add_session(self.main, ai="notes", cwd=notes,
                                  mtime=self.days_ago(1))
        self.add_session(self.main, ai="notes", cwd=notes,
                         mtime=self.days_ago(3))
        out = self.run_cs("-r")
        self.assertEqual(
            self.sections(out).get("MAIN", [None, None])[1:],
            [f"     cd ~/notes && claude --resume {newest}"],
            "with no running member, a grouped row should resume the newest "
            "member" + self.ctx())


# ======================================================================
class TestStateCell(ClaudeSessionsCase):

    def test_running_session_shows_its_status(self):
        sid = self.add_session(self.main, ai="worker")
        self.add_running(self.main, sid, status="busy")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 2),
                         "busy",
                         "a running session (pid alive, start time matches) "
                         "should show its status" + self.ctx())

    def test_running_session_without_status_shows_live(self):
        sid = self.add_session(self.main, ai="worker")
        self.add_running(self.main, sid, status=None)
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 2),
                         "live",
                         "a running session with no status should show 'live'"
                         + self.ctx())

    def test_session_whose_pid_has_exited_is_not_running(self):
        sid = self.add_session(self.main, ai="worker")
        p, start = self.start_process()
        p.kill()
        p.wait()
        self.write_session_file(self.main, p.pid, sid, start, status="busy")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 2),
                         "",
                         "a session whose pid is no longer alive should have "
                         "an empty state cell" + self.ctx())

    def test_reused_pid_with_other_start_time_is_not_running(self):
        sid = self.add_session(self.main, ai="worker")
        p, start = self.start_process()
        self.write_session_file(self.main, p.pid, sid, str(int(start) + 1),
                                status="busy")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 2),
                         "",
                         "a live pid whose start time differs from procStart "
                         "should not count as running" + self.ctx())


# ======================================================================
class TestTmuxCell(ClaudeSessionsCase):

    def test_tmux_session_from_sessions_json(self):
        sid = self.add_session(self.main, ai="worker")
        self.add_running(self.main, sid, status="idle", tmux="web:@3.%7")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 3),
                         "tmux=web",
                         "the tmux session (part before ':') from "
                         "sessions/<pid>.json should be shown as tmux=<name>"
                         + self.ctx())

    def test_tmux_session_from_tmux_tsv(self):
        sid = self.add_session(self.main, ai="worker")
        self.seed_tmux_tsv(f"{sid}\tlab\n")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 3),
                         "tmux=lab",
                         "the tmux session recorded in tmux.tsv should be "
                         "shown when sessions/<pid>.json is gone"
                         + self.ctx())

    def test_tmux_tsv_entry_inside_tmux_after_one_outside(self):
        sid = self.add_session(self.main, ai="worker")
        self.seed_tmux_tsv(f"{sid}\t\n{sid}\tlab\n")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 3),
                         "tmux=lab",
                         "an id first seen outside tmux and later inside "
                         "tmux 'lab' should show tmux=lab" + self.ctx())

    def test_tmux_session_from_resurrect_pane_title_and_folder(self):
        folder = str(self.home / "src/site")
        self.add_session(self.main, custom="SITE-DEPLOY", cwd=folder)
        self.add_snapshot(self.days_ago(1), [
            "window\tweb\t1\t:main\t1\t:*\tc0de,200x50,0,0,1\t:",
            pane("web", "✳ SITE-DEPLOY", folder),
            "state\tweb\t"])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3),
            "tmux=web",
            "a resurrect Claude pane titled with the session name (after the "
            "symbol) in the same folder should give its tmux session"
            + self.ctx())

    def test_resurrect_pane_matches_auto_title_of_renamed_session(self):
        folder = str(self.home / "src/parser")
        self.add_session(self.main, custom="alpha", ai="Refactor parser",
                         cwd=folder)
        self.add_snapshot(self.days_ago(1), [
            pane("ops", "✳ Refactor parser", folder)])
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "alpha"), 3),
                         "tmux=ops",
                         "a resurrect pane titled with the session's auto "
                         "title should match even when it has a rename name"
                         + self.ctx())

    def test_resurrect_claude_pane_recognised_by_full_command(self):
        folder = str(self.home / "src/site")
        self.add_session(self.main, custom="SITE-DEPLOY", cwd=folder)
        self.add_snapshot(self.days_ago(1), [
            pane("web", "✳ SITE-DEPLOY", folder, command="node",
                 full=":node /opt/bin/claude --resume x")])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3),
            "tmux=web",
            "a pane whose 11th field (full command) contains 'claude' is a "
            "Claude pane" + self.ctx())

    def test_resurrect_pane_in_other_folder_is_ignored(self):
        self.add_session(self.main, custom="SITE-DEPLOY",
                         cwd=str(self.home / "src/site"))
        self.add_snapshot(self.days_ago(1), [
            pane("web", "✳ SITE-DEPLOY", str(self.home / "elsewhere"))])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3), "",
            "a resurrect pane whose folder differs from the session's should "
            "not give a tmux session" + self.ctx())

    def test_resurrect_non_claude_pane_is_ignored(self):
        folder = str(self.home / "src/site")
        self.add_session(self.main, custom="SITE-DEPLOY", cwd=folder)
        self.add_snapshot(self.days_ago(1), [
            pane("web", "✳ SITE-DEPLOY", folder, command="bash",
                 full=":bash")])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3), "",
            "a pane with no 'claude' in its 10th or 11th field is not a "
            "Claude pane and should not give a tmux session" + self.ctx())

    def test_resurrect_pane_in_older_snapshot_is_used(self):
        folder = str(self.home / "src/site")
        self.add_session(self.main, custom="SITE-DEPLOY", cwd=folder)
        self.add_snapshot(self.days_ago(3), [
            pane("web", "⠂ SITE-DEPLOY", folder)])
        self.add_snapshot(self.days_ago(1), [
            pane("misc", "shell", "/tmp", command="bash", full=":bash")])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3),
            "tmux=web",
            "a matching Claude pane in an older snapshot should still count "
            "(the record runs from the oldest snapshot)" + self.ctx())

    def test_recorded_tmux_session_beats_resurrect_pane(self):
        folder = str(self.home / "src/site")
        sid = self.add_session(self.main, custom="SITE-DEPLOY", cwd=folder)
        self.seed_tmux_tsv(f"{sid}\tlab\n")
        self.add_snapshot(self.days_ago(1), [
            pane("web", "✳ SITE-DEPLOY", folder)])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3),
            "tmux=lab",
            "the tmux session recorded for the id should win over a "
            "resurrect pane match" + self.ctx())

    def test_tmux_session_from_sessions_json_of_stopped_session(self):
        sid = self.add_session(self.main, ai="worker")
        p, start = self.start_process()
        p.kill()
        p.wait()
        self.write_session_file(self.main, p.pid, sid, start, status="idle",
                                tmux="web:@3.%7")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 3),
                         "tmux=web",
                         "sessions/<pid>.json gives the tmux session even "
                         "when that session is not running" + self.ctx())

    def test_latest_tmux_tsv_record_wins(self):
        sid = self.add_session(self.main, ai="worker")
        self.seed_tmux_tsv(f"{sid}\tweb\n{sid}\tlab\n")
        out = self.run_cs("--tsv")
        self.assertEqual(self.cell(self.tsv_row(out, "MAIN", "worker"), 3),
                         "tmux=lab",
                         "with two tmux.tsv records for the id, the latest "
                         "one ('lab') should win" + self.ctx())

    def test_newest_snapshot_wins_between_resurrect_matches(self):
        folder = str(self.home / "src/site")
        self.add_session(self.main, custom="SITE-DEPLOY", cwd=folder)
        self.add_snapshot(self.days_ago(1), [
            pane("ops", "✳ SITE-DEPLOY", folder)])
        self.add_snapshot(self.days_ago(3), [
            pane("web", "✳ SITE-DEPLOY", folder)])
        out = self.run_cs("--tsv")
        self.assertEqual(
            self.cell(self.tsv_row(out, "MAIN", "SITE-DEPLOY"), 3),
            "tmux=ops",
            "when panes in several snapshots match, the newest snapshot's "
            "tmux session ('ops') should win" + self.ctx())


# ======================================================================
class TestTmuxMemory(ClaudeSessionsCase):

    def read_tsv(self):
        try:
            return self.tmux_tsv.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def test_new_id_inside_tmux_is_recorded(self):
        sid = self.add_session(self.main, ai="worker")
        self.add_running(self.main, sid, tmux="web:@1.%2")
        self.run_cs()
        self.assertEqual(self.read_tsv(), f"{sid}\tweb\n",
                         "a run should append '<id>\\t<tmux session>\\n' to "
                         "~/.local/state/claude-sessions/tmux.tsv for a new "
                         "id seen inside tmux" + self.ctx())

    def test_new_id_outside_tmux_is_recorded_with_empty_tmux(self):
        sid = self.add_session(self.main, ai="worker")
        self.add_running(self.main, sid)
        self.run_cs()
        self.assertEqual(self.read_tsv(), f"{sid}\t\n",
                         "a run should append '<id>\\t\\n' for a new id seen "
                         "outside tmux" + self.ctx())

    def test_known_id_is_not_recorded_again(self):
        sid = self.add_session(self.main, ai="worker")
        self.add_running(self.main, sid, tmux="web:@1.%2")
        self.run_cs()
        self.run_cs()
        self.assertEqual(self.read_tsv(), f"{sid}\tweb\n",
                         "a second run should not append the same id again"
                         + self.ctx())

    def test_id_from_sessions_json_of_stopped_session_is_recorded(self):
        sid = self.add_session(self.main, ai="worker")
        p, start = self.start_process()
        p.kill()
        p.wait()
        self.write_session_file(self.main, p.pid, sid, start, status="idle",
                                tmux="web:@1.%2")
        self.run_cs()
        self.assertEqual(self.read_tsv(), f"{sid}\tweb\n",
                         "an id seen in sessions/*.json should be recorded "
                         "even if that session is not running" + self.ctx())

    def test_id_newly_inside_tmux_is_recorded_again(self):
        sid = self.add_session(self.main, ai="worker")
        self.seed_tmux_tsv(f"{sid}\t\n")
        self.add_running(self.main, sid, tmux="web:@1.%2")
        self.run_cs()
        self.assertEqual(self.read_tsv(), f"{sid}\t\n{sid}\tweb\n",
                         "an id recorded outside tmux and now inside tmux "
                         "should be appended with its tmux session"
                         + self.ctx())


# ======================================================================
class TestOptions(ClaudeSessionsCase):

    def test_default_is_at_most_25_rows_per_section(self):
        self.add_many(self.main, 27, "task")
        out = self.run_cs("--tsv")
        self.assertEqual(len(self.tsv_rows(out, "MAIN")), 25,
                         "by default at most 25 rows should be shown per "
                         "section" + self.ctx())

    def test_n_limits_rows_to_the_newest(self):
        self.add_many(self.main, 5, "task")
        out = self.run_cs("-n", "2", "--tsv")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["task 00", "task 01"],
                         "-n 2 should show only the two newest rows"
                         + self.ctx())

    def test_n_applies_to_each_section(self):
        self.add_many(self.main, 3, "main")
        self.add_many(self.alt, 3, "alt")
        out = self.run_cs("-n", "2", "--tsv")
        self.assertEqual(
            (len(self.tsv_rows(out, "MAIN")), len(self.tsv_rows(out, "ALT"))),
            (2, 2),
            "-n 2 should allow 2 rows in each section" + self.ctx())

    def test_n_zero_shows_all_rows(self):
        self.add_many(self.main, 27, "task")
        out = self.run_cs("-n", "0", "--tsv")
        self.assertEqual(len(self.tsv_rows(out, "MAIN")), 27,
                         "-n 0 should show all rows" + self.ctx())

    def test_r_adds_resume_command_under_each_row(self):
        a = self.add_session(self.main, ai="first", cwd=str(self.home / "a"),
                             mtime=self.days_ago(1))
        b = self.add_session(self.main, ai="second", cwd=str(self.home / "b"),
                             mtime=self.days_ago(2))
        out = self.run_cs("-r")
        sec = self.sections(out).get("MAIN", [])
        self.assertEqual(
            [sec[1:2], sec[3:4]],
            [[f"     cd ~/a && claude --resume {a}"],
             [f"     cd ~/b && claude --resume {b}"]],
            "-r should put '     cd <folder> && claude --resume <id>' under "
            "each row" + self.ctx())

    def test_r_uses_claude_alt_in_alt_section(self):
        sid = self.add_session(self.alt, ai="alt work",
                               cwd=str(self.home / "w"))
        out = self.run_cs("-r")
        self.assertEqual(self.sections(out).get("ALT", [])[1:],
                         [f"     cd ~/w && claude-alt --resume {sid}"],
                         "the ALT resume command should use claude-alt"
                         + self.ctx())

    def test_r_writes_folder_in_full(self):
        folder = "projects/research/archive/2026/experiments/run_c"
        sid = self.add_session(self.main, ai="long",
                               cwd=str(self.home / folder))
        out = self.run_cs("-r")
        self.assertEqual(self.sections(out).get("MAIN", [])[1:],
                         [f"     cd ~/{folder} && claude --resume {sid}"],
                         "the resume command should give the folder in full "
                         "(not shortened), with ~ for $HOME" + self.ctx())


# ======================================================================
class TestWordFilter(ClaudeSessionsCase):

    def test_word_matches_name_ignoring_case(self):
        self.add_session(self.main, custom="SITE-DEPLOY",
                         cwd=str(self.home / "s"))
        self.add_session(self.main, ai="notes", cwd=str(self.home / "n"))
        out = self.run_cs("--tsv", "deploy")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["SITE-DEPLOY"],
                         "a WORD should match the name ignoring case and drop "
                         "other rows" + self.ctx())

    def test_word_matches_auto_title_of_renamed_session(self):
        self.add_session(self.main, custom="alpha", ai="Refactor parser",
                         cwd=str(self.home / "a"))
        self.add_session(self.main, ai="bravo", cwd=str(self.home / "b"))
        out = self.run_cs("--tsv", "PARSER")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["alpha"],
                         "a WORD should match the auto title even when the "
                         "row shows the rename name" + self.ctx())

    def test_word_matches_last_prompt(self):
        self.add_session(self.main, ai="alpha", prompt="fix the websocket",
                         cwd=str(self.home / "a"))
        self.add_session(self.main, ai="bravo", prompt="something else",
                         cwd=str(self.home / "b"))
        out = self.run_cs("--tsv", "websocket")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["alpha"],
                         "a WORD should match the last prompt" + self.ctx())

    def test_earlier_last_prompt_is_replaced(self):
        self.add_session(self.main, ai="session x",
                         prompt=["albatross", "pelican"])
        out = self.run_cs("--tsv", "albatross")
        self.assertEqual(self.sections(out).get("MAIN"), [NONE_LINE],
                         "a last prompt replaced by a later one should no "
                         "longer match" + self.ctx())

    def test_word_matches_folder(self):
        self.add_session(self.main, ai="alpha",
                         cwd=str(self.home / "src/kangaroo"))
        self.add_session(self.main, ai="bravo",
                         cwd=str(self.home / "src/wombat"))
        out = self.run_cs("--tsv", "kangaroo")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["alpha"],
                         "a WORD should match the folder" + self.ctx())

    def test_word_matches_session_id(self):
        self.add_session(self.main, ai="alpha",
                         sid="11111111-2222-4333-8444-deadbeef5555",
                         cwd=str(self.home / "a"))
        self.add_session(self.main, ai="bravo",
                         sid="66666666-7777-4888-9999-000000000000",
                         cwd=str(self.home / "b"))
        out = self.run_cs("--tsv", "DEADBEEF")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["alpha"],
                         "a WORD should match the session id, ignoring case"
                         + self.ctx())

    OLDER = "aaaaaaaa-0000-4000-8000-0000c0ffee01"
    NEWER = "bbbbbbbb-0000-4000-8000-0000feed0002"

    def make_group(self, run_older=False):
        """Two 'notes' sessions in one folder; NEWER is the newest."""
        notes = str(self.home / "notes")
        self.add_session(self.main, ai="notes", sid=self.OLDER, cwd=notes,
                         mtime=self.days_ago(2))
        self.add_session(self.main, ai="notes", sid=self.NEWER, cwd=notes,
                         mtime=self.days_ago(1))
        if run_older:
            self.add_running(self.main, self.OLDER, status="idle")

    def test_grouped_row_matched_on_shown_newest_session(self):
        self.make_group()
        out = self.run_cs("--tsv", "feed")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["notes (2)"],
                         "a WORD in the id of the session a grouped row "
                         "shows (the newest) should keep the row" + self.ctx())

    def test_grouped_row_not_matched_on_hidden_member(self):
        self.make_group()
        out = self.run_cs("--tsv", "c0ffee")
        self.assertEqual(self.sections(out).get("MAIN"), [NONE_LINE],
                         "a WORD found only in a member the grouped row does "
                         "not show should not keep the row" + self.ctx())

    def test_grouped_row_matched_on_shown_running_session(self):
        self.make_group(run_older=True)
        out = self.run_cs("--tsv", "c0ffee")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["notes (2)"],
                         "a grouped row shows its running member, so a WORD "
                         "in that member's id should keep the row"
                         + self.ctx())

    def test_grouped_row_not_matched_on_newest_when_running_is_shown(self):
        self.make_group(run_older=True)
        out = self.run_cs("--tsv", "feed")
        self.assertEqual(self.sections(out).get("MAIN"), [NONE_LINE],
                         "when a grouped row shows its running member, a WORD "
                         "found only in the newest member should not keep the "
                         "row" + self.ctx())

    def test_every_word_must_match(self):
        self.add_session(self.main, custom="SITE-DEPLOY")
        out = self.run_cs("--tsv", "deploy", "zebra")
        self.assertEqual(self.sections(out).get("MAIN"), [NONE_LINE],
                         "a row should be kept only if every WORD matches; "
                         "with no rows left the section shows (none)"
                         + self.ctx())

    def test_words_may_match_different_fields(self):
        self.add_session(self.main, custom="SITE-DEPLOY",
                         cwd=str(self.home / "src/kangaroo"))
        self.add_session(self.main, custom="deploy docs",
                         cwd=str(self.home / "src/wombat"))
        out = self.run_cs("--tsv", "deploy", "kangaroo")
        self.assertEqual(self.tsv_names(out, "MAIN"), ["SITE-DEPLOY"],
                         "one WORD may match the name and another the folder"
                         + self.ctx())


# ======================================================================
class TestFitToLines(ClaudeSessionsCase):

    def test_output_has_exactly_LINES_lines_when_sessions_overflow(self):
        self.add_many(self.main, 30, "main")
        self.add_many(self.alt, 30, "alt")
        out = self.run_cs("--tsv", lines=20)
        self.assertEqual(len(self.out_lines(out)), 20,
                         "with --tsv and LINES=20 and many sessions the "
                         "output should be exactly 20 lines" + self.ctx())

    def test_main_gets_the_rest_after_a_small_alt(self):
        self.add_many(self.main, 30, "main")
        self.add_many(self.alt, 3, "alt")
        out = self.run_cs("--tsv", lines=20)
        # 20 - header - blank - MAIN title - blank - ALT title - 3 ALT rows
        self.assertEqual(
            (len(self.tsv_rows(out, "MAIN")), len(self.tsv_rows(out, "ALT"))),
            (12, 3),
            "LINES=20 with 3 ALT sessions should give ALT its 3 rows and "
            "MAIN the remaining 12" + self.ctx())

    def test_alt_takes_what_main_leaves(self):
        self.add_many(self.main, 3, "main")
        self.add_many(self.alt, 30, "alt")
        out = self.run_cs("--tsv", lines=20)
        self.assertEqual(
            (len(self.tsv_rows(out, "MAIN")), len(self.tsv_rows(out, "ALT"))),
            (3, 12),
            "LINES=20 with only 3 MAIN sessions should let ALT take the "
            "12 lines left" + self.ctx())

    def test_none_line_of_empty_alt_counts(self):
        self.add_many(self.main, 30, "main")
        self.alt.mkdir()
        out = self.run_cs("--tsv", lines=20)
        self.assertEqual(
            (len(self.out_lines(out)), len(self.tsv_rows(out, "MAIN")),
             self.sections(out).get("ALT")),
            (20, 14, [NONE_LINE]),
            "LINES=20 with an empty ALT should give 20 lines: MAIN 14 rows "
            "and ALT its (none) line" + self.ctx())

    def test_main_alone_fills_lines(self):
        self.add_many(self.main, 30, "main")
        out = self.run_cs("--tsv", lines=10)
        self.assertEqual(
            (len(self.out_lines(out)), len(self.tsv_rows(out, "MAIN"))),
            (10, 7),
            "LINES=10 with only MAIN should give header, blank, title and "
            "7 rows" + self.ctx())

    def test_n_is_ignored_when_fitting(self):
        self.add_many(self.main, 30, "main")
        self.add_many(self.alt, 2, "alt")
        out = self.run_cs("-n", "1", "--tsv", lines=20)
        self.assertEqual(len(self.tsv_rows(out, "MAIN")), 13,
                         "with --tsv and LINES set, -n should be ignored "
                         "(MAIN gets 20 - 5 - 2 = 13 rows)" + self.ctx())

    def test_alt_is_given_at_most_5_rows(self):
        self.add_many(self.main, 30, "main")
        self.add_many(self.alt, 30, "alt")
        out = self.run_cs("--tsv", lines=20)
        # 20 - 5 fixed lines = 15 rows: ALT up to 5, MAIN the other 10.
        self.assertEqual(
            (len(self.tsv_rows(out, "MAIN")), len(self.tsv_rows(out, "ALT"))),
            (10, 5),
            "LINES=20 with many sessions in both should give ALT 5 rows and "
            "MAIN the other 10" + self.ctx())

    def test_every_section_shows_one_line_even_beyond_LINES(self):
        self.add_many(self.main, 10, "main")
        self.add_many(self.alt, 10, "alt")
        out = self.run_cs("--tsv", lines=4)
        self.assertEqual(
            (len(self.tsv_rows(out, "MAIN")), len(self.tsv_rows(out, "ALT")),
             len(self.out_lines(out))),
            (1, 1, 7),
            "with LINES=4 (less than the 5 fixed lines) each section should "
            "still show exactly one row, making 7 lines in all" + self.ctx())

    def test_resume_lines_are_not_counted(self):
        self.add_many(self.main, 30, "main")
        self.add_many(self.alt, 3, "alt")
        out = self.run_cs("-r", "--tsv", lines=20)
        resume = [l for l in self.out_lines(out) if l.startswith("     cd ")]
        self.assertEqual(
            (len(self.tsv_rows(out, "MAIN")), len(self.tsv_rows(out, "ALT")),
             len(resume)),
            (12, 3, 15),
            "with -r, resume lines are not counted in LINES: MAIN 12 rows, "
            "ALT 3 rows, plus one resume line for each of the 15 rows"
            + self.ctx())

    def test_lines_is_ignored_without_tsv(self):
        self.add_many(self.main, 30, "main")
        out = self.run_cs(lines=10)
        self.assertEqual(len(self.sections(out).get("MAIN", [])), 25,
                         "without --tsv, LINES should not fit the output; "
                         "the default 25 rows apply" + self.ctx())


if __name__ == "__main__":
    unittest.main()
