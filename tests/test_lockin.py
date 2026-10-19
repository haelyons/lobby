"""lockin, tested from README.txt alone (LOCKIN), each run on its own Xvfb.

Panels print lines of capital H: a drawn line is a solid band of ink, so lines,
margins and widths can be measured without knowing the font. Lock-screen runs
get an xscreensaver-like magenta window; Xvfb's root is black (-br), so
anything lockin painted (#0a0a0a) stands out.
"""
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
XVFB, XRANDR = shutil.which("Xvfb"), shutil.which("xrandr")
BG, MAGENTA = b"\x0a\x0a\x0a", b"\xff\x00\xff"

WINDOW = r"""import ctypes, sys
X = ctypes.CDLL("libX11.so.6")
P, U, I = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
X.XOpenDisplay.restype = P
X.XDefaultRootWindow.argtypes = [P]
X.XDefaultRootWindow.restype = X.XCreateSimpleWindow.restype = U
X.XCreateSimpleWindow.argtypes = [P, U, I, I, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, U, U]
X.XMapWindow.argtypes = [P, U]
X.XSetCloseDownMode.argtypes = [P, I]
X.XCloseDisplay.argtypes = [P]
d = X.XOpenDisplay(None)
w = X.XCreateSimpleWindow(d, X.XDefaultRootWindow(d), 0, 0, int(sys.argv[1]), int(sys.argv[2]), 0, 0, 0xFF00FF)
X.XMapWindow(d, w)
X.XSetCloseDownMode(d, 1)  # RetainPermanent: the window outlives this process
X.XCloseDisplay(d)
print(w)
"""
CAPTURE = r"""import sys, gi
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk
r = Gdk.get_default_root_window()
w, h = r.get_width(), r.get_height()
p = Gdk.pixbuf_get_from_window(r, 0, 0, w, h)
s, px = p.get_rowstride(), p.get_pixels()
sys.stdout.buffer.write(b"".join(px[y * s:y * s + w * 3] for y in range(h)))
"""


def bbox(ink):
    return min(a for a, _ in ink.values()), min(ink), max(b for _, b in ink.values()), max(ink)


def bands(ink):
    """[top, bottom] of each run of rows with ink: one per drawn line of H's."""
    out = []
    for y in sorted(ink):
        if out and y == out[-1][1] + 1:
            out[-1][1] = y
        else:
            out.append([y, y])
    return out


def pitch(b):
    return (b[-1][0] - b[0][0]) / (len(b) - 1)


class Screen:
    """An Xvfb display running one lockin; @D in a command is a folder of its own."""

    def __init__(self, tmp, name, w, h, left=None, right=None, desk=False, hex_id=False,
                 monitors=()):
        self.w, self.h, self.dir, self.shot = w, h, Path(tmp, name), None
        self.dir.mkdir()
        rfd, wfd = os.pipe()
        self.xvfb = subprocess.Popen([XVFB, "-displayfd", str(wfd), "-screen", "0", f"{w}x{h}x24",
                                      "-br", "-nolisten", "tcp", "-noreset"], pass_fds=(wfd,),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        os.close(wfd)
        with os.fdopen(rfd) as f:
            display = f.readline().strip()
        self.lockin = None
        if not display:
            raise RuntimeError("Xvfb did not start")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("XDG_", "WAYLAND", "XAUTH", "XSCREENSAVER"))
                    and k not in ("LINES", "COLUMNS")}
        self.env.update(DISPLAY=":" + display, HOME=str(self.dir), XDG_RUNTIME_DIR=str(self.dir),
                        GDK_BACKEND="x11", NO_AT_BRIDGE="1", PYTHONDONTWRITEBYTECODE="1",
                        DBUS_SESSION_BUS_ADDRESS=f"unix:path={self.dir}/no-bus")
        for m in monitors:
            subprocess.run([XRANDR, "--setmonitor", *m], env=self.env, capture_output=True, timeout=15)
        self.monitors = monitors and subprocess.run([XRANDR, "--listmonitors"], env=self.env,
                                                    capture_output=True, text=True, timeout=15).stdout
        env, args = dict(self.env), ["--desk"] if desk else []
        if not desk:
            xid = int(subprocess.run([sys.executable, "-c", WINDOW, str(w), str(h)], env=env,
                                     capture_output=True, text=True, timeout=30, check=True).stdout)
            env["XSCREENSAVER_WINDOW"] = hex(xid) if hex_id else str(xid)
        for flag, cmd in (("--left", left), ("--right", right)):
            if cmd is not None:
                args += [flag, cmd.replace("@D", shlex.quote(str(self.dir)))]
        with open(self.dir / "lockin.log", "w") as log:
            self.lockin = subprocess.Popen([sys.executable, str(REPO / "lockin"), *args], env=env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True)

    def lines(self, name):
        p = self.dir / name
        return p.read_text().split() if p.exists() else []

    def ink(self, x0=0, x1=None, y0=0, y1=None):
        """{y: (first x, last x)} of pixels in the box that are not #0a0a0a."""
        x1, y1, ink = x1 or self.w, y1 or self.h, {}
        for y in range(y0, y1):
            seg = self.shot[3 * (y * self.w + x0):3 * (y * self.w + x1)]
            if seg.strip(b"\x0a"):
                ink[y] = (x0 + (len(seg) - len(seg.lstrip(b"\x0a"))) // 3,
                          x0 + (len(seg.rstrip(b"\x0a")) - 1) // 3)
        return ink

    def painted(self, y):
        """[x0, x1) runs of #0a0a0a pixels on row y: the windows lockin shows there."""
        row, runs = self.shot[3 * y * self.w:3 * (y + 1) * self.w], []
        for x in range(self.w):
            if row[3 * x:3 * x + 3] == BG:
                if runs and runs[-1][1] == x:
                    runs[-1][1] = x + 1
                else:
                    runs.append([x, x + 1])
        return runs

    def column(self, x):
        return sum(self.shot[3 * (y * self.w + x):3 * (y * self.w + x) + 3] == BG for y in range(self.h))

    def log(self):
        return (self.dir / "lockin.log").read_text()[-600:]

    def stop(self):
        for p in (self.lockin, self.xvfb):
            if p:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()


def settle(screens, deadline):
    """Capture every screen until two captures in a row are the same."""
    todo = list(screens)
    while todo:
        procs = [(s, subprocess.Popen([sys.executable, "-c", CAPTURE], env=s.env,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
                 for s in todo]
        for s, p in procs:
            shot = p.communicate(timeout=60)[0]
            if shot == s.shot or time.monotonic() > deadline:
                todo.remove(s)
            s.shot = shot
        time.sleep(0.5)


H4 = 'printf "HHHH\\nHH\\nHHHHHH\\n"'
KEEP = ('date +%s.%N >> @D/t; sleep 1; if [ "$(wc -l < @D/t)" -eq 1 ]; '
        'then printf "HHH\\nH\\n"; else echo WRONG; exit 1; fi')


@unittest.skipUnless(XVFB, "Xvfb is not on PATH")
class LockinTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="lockin-test-")
        cls.addClassCleanup(shutil.rmtree, tmp, True)
        cls.s = {}

        def screen(name, *args, **kw):
            cls.s[name] = Screen(tmp, name, *args, **kw)
            cls.addClassCleanup(cls.s[name].stop)

        screen("land", 1600, 900, left=H4, right='printf "HHHHHH\\nHH\\n"', hex_id=True)
        screen("squeeze", 1600, 600, left='printf "HH\\nHHHHHHHHHHHH\\nHHH\\n"',
               right='printf "%s\\n" ' + "H" * 60)
        screen("portrait", 700, 1000, left='echo run >> @D/runs; printf "HH\\n"',
               right='echo run >> @D/rruns; printf "HH\\n"')
        screen("lines", 1000, 800, right='echo "$LINES" >> @D/rlines',
               left='echo "$LINES" >> @D/lines; i=0; while [ $i -lt 60 ]; do echo H; i=$((i+1)); done')
        screen("fail", 1200, 600, left="exit 3")
        screen("fail_ref", 1200, 600, left="echo '(exit 3 failed: exit 3)'")
        screen("keep", 1200, 600, left=KEEP)
        screen("keep_ref", 1200, 600, left='printf "HHH\\nH\\n"')
        screen("desk", 2560, 1440, desk=True, left='printf "HH\\nHH\\n"',
               right='printf "HHHHHHHH\\nHHH\\n"')
        screen("desk_wide", 2560, 1440, desk=True, right='printf "%s\\n" ' + "H" * 150)
        if XRANDR:
            portrait, landscape = ("600/150x900/225+0+0", "none"), ("1200/300x700/175+600+0", "screen")
            screen("primary", 1800, 900, desk=True, left='printf "HH\\n"',
                   monitors=[("*A",) + portrait, ("B",) + landscape])
            screen("no_primary", 1800, 900, desk=True, left='printf "HH\\n"',
                   monitors=[("A",) + portrait, ("B",) + landscape])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (len(cls.s["keep"].lines("t")) < 4
                                               or not cls.s["lines"].lines("rlines")):
            time.sleep(0.2)
        settle(cls.s.values(), deadline + 10)
        for s in cls.s.values():
            s.stop()

    def test_lock_screen_layout(self):
        s = self.s["land"]
        self.assertNotIn(MAGENTA, s.shot, "lockin paints the whole window: " + s.log())
        self.assertIn(b"\xdc\xdc\xdc", s.shot, "text colour #dcdcdc")
        left, right = s.ink(0, s.w // 2), s.ink(s.w // 2)
        self.assertTrue(left and right, "both panels drawn: " + s.log())
        l0, ltop, _, _ = bbox(left)
        _, rtop, r1, _ = bbox(right)
        lb, rb = bands(left), bands(right)
        self.assertTrue(89 <= l0 <= 96, f"left panel 90 px from the left edge, not x={l0}")
        self.assertTrue(70 <= ltop <= 70 + pitch(lb) / 2, f"left panel 70 px from the top, not y={ltop}")
        self.assertEqual(rtop, ltop, "right panel also 70 px from the top")
        self.assertTrue(s.w - 98 <= r1 <= s.w - 90, f"right block ends 90 px from the right edge, not x={r1}")
        starts = [min(right[y][0] for y in range(a, b + 1)) for a, b in rb]
        self.assertLessEqual(max(starts) - min(starts), 1, f"right lines left-aligned: {starts}")

        s = self.s["squeeze"]  # a right line too long for the room left
        b = bands(s.ink())
        left_end = bbox(s.ink(y0=b[1][0], y1=b[1][1] + 1))[2]  # the left panel's widest line
        r0, _, r1, _ = bbox(s.ink(left_end + 1, y0=b[0][0], y1=b[0][1] + 1))
        self.assertGreaterEqual(r0 - left_end - 1, 89, "right block starts 90 px after the left panel")
        self.assertLessEqual(r1, s.w - 90, "the long line is cut short of the right margin")

    def test_portrait_window_never_runs_right_command(self):
        s = self.s["portrait"]
        self.assertGreaterEqual(len(s.lines("runs")), 2, s.log())
        self.assertEqual(s.lines("rruns"), [])
        self.assertTrue(s.ink(0, s.w // 2), "left panel drawn: " + s.log())
        self.assertEqual(s.ink(s.w // 2), {}, "no right panel")

    def test_LINES_is_how_many_lines_fit(self):
        s = self.s["lines"]
        b = bands(s.ink())
        room = s.h - 2 * 70
        fits = range(math.floor(room / (pitch(b) + 0.3)), math.floor(room / (pitch(b) - 0.3)) + 1)
        got = [int(v) for v in s.lines("lines")]
        self.assertTrue(got and set(got) <= set(fits), f"$LINES {got}, expected one of {list(fits)}")
        self.assertEqual(len(b), got[0], "of 60 lines, $LINES are drawn")
        self.assertLessEqual(b[-1][1], s.h - 70, "nothing below the bottom margin")
        self.assertEqual(s.lines("rlines")[:1], s.lines("lines")[:1], "the right command gets it too")

    def test_failed_runs(self):
        for name in ("fail", "keep"):
            with self.subTest(name):
                s, ref = self.s[name], self.s[name + "_ref"]
                self.assertTrue(ref.ink(), ref.log())
                self.assertTrue(s.shot == ref.shot, f"{name} should look like {name}_ref: " + s.log())
        starts = [float(t) for t in self.s["keep"].lines("t")]
        gaps = sorted(b - a for a, b in zip(starts, starts[1:]))
        self.assertTrue(gaps and 1.7 <= gaps[len(gaps) // 2] <= 2.4,
                        f"a 1 s left command starts every 2 s, start to start: gaps {gaps}")

    def test_desk_windows(self):
        s = self.s["desk"]
        (lx0, lx1), (rx0, rx1) = s.painted(2)
        self.assertEqual((lx0, lx1, s.column(lx0), rx1, s.column(rx1 - 1)), (0, 1280, s.h, s.w, s.h),
                         "left window at the left edge 1280 px wide, right one against the right"
                         " edge, both full height: " + s.log())
        left = s.ink(0, 1280)
        l0, ltop, _, _ = bbox(left)
        r0, rtop, r1, _ = bbox(s.ink(rx0, rx1))
        self.assertTrue(43 <= l0 <= 50 and 43 <= r0 - rx0 <= 50, f"text 44 px from the left: {l0}, {r0 - rx0}")
        self.assertTrue(40 <= ltop <= 40 + pitch(bands(left)) / 2 and rtop == ltop,
                        f"text 40 px from the top: {ltop}, {rtop}")
        self.assertTrue(-1 <= (rx1 - rx0) - 88 - (r1 - r0 + 1) <= 10,
                        f"right window {rx1 - rx0} px for a {r1 - r0 + 1} px line plus 2 x 44")
        s = self.s["desk_wide"]
        self.assertEqual(s.painted(2), [[1280, 2560]],
                         "only the right window, capped at monitor width - 1280: " + s.log())

    @unittest.skipUnless(XRANDR, "xrandr is needed for several monitors")
    def test_desk_monitor_choice(self):
        for name, x, h in (("primary", 0, 900), ("no_primary", 600, 700)):
            with self.subTest(name):
                s = self.s[name]
                if "A 600/150x900/225+0+0" not in s.monitors:
                    self.skipTest(f"Xvfb did not take the monitors: {s.monitors}")
                x0 = s.painted(2)[0][0] if s.painted(2) else None
                self.assertEqual((x0, s.column(x)), (x, h), s.monitors + s.log())


if __name__ == "__main__":
    unittest.main()
