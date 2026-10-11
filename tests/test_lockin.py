#!/usr/bin/env python3
"""Black-box tests for lockin, written only from README.txt (section LOCKIN).

Every test class starts its own Xvfb server(s); Xvfb picks a free display
number itself (-displayfd), so the real display is never used. The classes
are skipped when Xvfb is not on PATH.

Lock-screen mode: the tests create a window of a chosen size on the Xvfb
display (magenta, so unpainted pixels stand out) and pass its id in
$XSCREENSAVER_WINDOW. Desktop mode: the tests look at the windows lockin
creates (geometry, EWMH/ICCCM hints) and at their pixels.

All X access (creating windows, reading pixels with Gdk, listing windows)
happens in a helper process (this file run with --x-agent) whose DISPLAY is
the Xvfb display, so the test runner itself never opens a display.

Panel commands are small shell commands. Text is made of capital "H"s: a
line of them is a solid band of ink from cap height down to the baseline,
which makes lines, cells and margins easy to find without knowing the font.
"""

import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parent.parent
LOCKIN = REPO / 'lockin'
XVFB = shutil.which('Xvfb')
XRANDR = shutil.which('xrandr')

BG = (0x0A, 0x0A, 0x0A)            # README: painted #0a0a0a
FG = (0xDC, 0xDC, 0xDC)            # README: colour #dcdcdc
MAGENTA = 0xFF00FF                 # our own windows and the root before lockin paints
MAGENTA_RGB = b'\xff\x00\xff'
INK_T = 8                          # a channel differing from the background by more than this is ink
STARTUP = 15.0                     # seconds lockin gets to start and draw

needs_xvfb = unittest.skipUnless(XVFB, 'Xvfb is not on PATH')


# --------------------------------------------------------------------------
# processes: Xvfb, the X helper ("agent"), lockin
# --------------------------------------------------------------------------

def _stop(proc, group=True, timeout=3.0):
    """Terminate a process (and its process group) and reap it."""
    if proc is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if group:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if proc.poll() is None:
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=timeout)
            if group and sig == signal.SIGTERM:
                # children of the group (panel commands) may still be alive
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            return
        except subprocess.TimeoutExpired:
            continue


def base_env(display, tmp):
    """Environment for lockin and the helper: only the Xvfb display, no session."""
    drop = {'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'LINES', 'COLUMNS',
            'XSCREENSAVER_WINDOW', 'SESSION_MANAGER', 'DBUS_SESSION_BUS_ADDRESS',
            'GDK_BACKEND', 'DESKTOP_STARTUP_ID', 'XDG_SESSION_TYPE'}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(
        DISPLAY=display,
        GDK_BACKEND='x11',
        NO_AT_BRIDGE='1',
        GIO_USE_VFS='local',
        GSETTINGS_BACKEND='memory',
        # a bus address that does not exist: nothing talks to the user's session bus
        DBUS_SESSION_BUS_ADDRESS='unix:path=' + os.path.join(tmp, 'no-session-bus'),
    )
    return env


class Xvfb:
    """An Xvfb server on a display number it chooses itself."""

    def __init__(self, width, height):
        self.proc = None
        rfd, wfd = os.pipe()
        try:
            self.proc = subprocess.Popen(
                [XVFB, '-displayfd', str(wfd), '-screen', '0', f'{width}x{height}x24',
                 '-nolisten', 'tcp', '-noreset'],
                pass_fds=(wfd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        finally:
            os.close(wfd)
        try:
            buf = b''
            deadline = time.monotonic() + 20
            while not buf.endswith(b'\n'):
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([rfd], [], [], left)[0]:
                    raise RuntimeError('Xvfb did not report its display number')
                chunk = os.read(rfd, 64)
                if not chunk:
                    raise RuntimeError('Xvfb exited before reporting a display number')
                buf += chunk
        except BaseException:
            self.stop()
            raise
        finally:
            os.close(rfd)
        self.display = ':' + buf.decode().strip()

    def stop(self):
        _stop(self.proc)
        self.proc = None


class Agent:
    """This file run with --x-agent on one display; JSON lines over a pipe."""

    def __init__(self, env, log_path):
        self.log_path = log_path
        self.log = open(log_path, 'wb')
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE), '--x-agent'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log, env=env, start_new_session=True)
        self.call('hello')

    def call(self, op, timeout=30.0, **kw):
        self.proc.stdin.write((json.dumps(dict(kw, op=op)) + '\n').encode())
        self.proc.stdin.flush()
        if not select.select([self.proc.stdout.fileno()], [], [], timeout)[0]:
            raise RuntimeError(f'X helper did not answer {op!r}')
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f'X helper died during {op!r}: {tail(self.log_path)}')
        reply = json.loads(line)
        if 'error' in reply:
            raise RuntimeError(f'X helper failed {op!r}: {reply["error"]}')
        return reply

    def close(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.call('quit', timeout=5)
        except Exception:
            pass
        _stop(self.proc)
        self.proc = None
        self.log.close()


def tail(path, n=800):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return ''
    return data[-n:].decode('utf-8', 'replace').strip()


class Lockin:
    def __init__(self, args, env, log_path):
        self.log_path = log_path
        self.proc = None
        self.error = None
        if not LOCKIN.exists():
            self.error = f'lockin not found at {LOCKIN}'
            return
        cmd = [str(LOCKIN)] if os.access(LOCKIN, os.X_OK) else [sys.executable, str(LOCKIN)]
        with open(log_path, 'wb') as log:
            try:
                self.proc = subprocess.Popen(
                    cmd + list(args), env=env, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                self.error = f'could not start lockin: {e}'

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        _stop(self.proc)


# --------------------------------------------------------------------------
# pixels
# --------------------------------------------------------------------------

_TABLES = {}


def _tables(bg):
    t = _TABLES.get(bg)
    if t is None:
        t = tuple(bytes(1 if abs(v - c) > INK_T else 0 for v in range(256)) for c in bg)
        _TABLES[bg] = t
    return t


class Image:
    """RGB pixels, 3 bytes each, rows top to bottom."""

    def __init__(self, w, h, data):
        if len(data) != w * h * 3:
            raise ValueError('bad capture size')
        self.w, self.h, self.data = w, h, bytes(data)
        self.ox = self.oy = 0       # where pixel (0, 0) is inside the window it shows
        self._ink = {}

    def pixel(self, x, y):
        i = (y * self.w + x) * 3
        return tuple(self.data[i:i + 3])

    def row(self, y, x0=0, x1=None):
        x1 = self.w if x1 is None else x1
        base = y * self.w * 3
        return self.data[base + x0 * 3: base + x1 * 3]

    @property
    def corner(self):
        """Background colour as lockin painted it (no panel draws in a corner)."""
        return self.pixel(2, 2)

    def has_magenta(self):
        i = self.data.find(MAGENTA_RGB)
        while i != -1:
            if i % 3 == 0:
                return True
            i = self.data.find(MAGENTA_RGB, i + 1)
        return False

    def count(self, rgb):
        """Number of pixels exactly rgb."""
        n = 0
        needle = bytes(rgb)
        i = self.data.find(needle)
        while i != -1:
            if i % 3 == 0:
                n += 1
                i = self.data.find(needle, i + 3)
            else:
                i = self.data.find(needle, i + 1)
        return n

    def off_colour(self, rgb, x0, y0, x1, y1):
        """(count, example) of pixels in [x0,x1) x [y0,y1) that are not exactly rgb."""
        want = bytes(rgb) * (x1 - x0)
        bad, example = 0, None
        for y in range(y0, y1):
            r = self.row(y, x0, x1)
            if r != want:
                for i in range(x1 - x0):
                    if r[i * 3:i * 3 + 3] != want[:3]:
                        bad += 1
                        if example is None:
                            example = (x0 + i, y, '#%02x%02x%02x' % tuple(r[i * 3:i * 3 + 3]))
        return bad, example

    def ink(self, bg=None):
        """Ink against bg (default: the corner colour)."""
        bg = tuple(bg or self.corner)
        if bg in self._ink:
            return self._ink[bg]
        tr, tg, tb = _tables(bg)
        bgrow = bytes(bg) * self.w
        rows = {}
        for y in range(self.h):
            r = self.row(y)
            if r == bgrow:
                continue
            m = (int.from_bytes(r[0::3].translate(tr), 'big')
                 | int.from_bytes(r[1::3].translate(tg), 'big')
                 | int.from_bytes(r[2::3].translate(tb), 'big'))
            if m:
                rows[y] = m
        self._ink[bg] = Ink(rows, self.w)
        return self._ink[bg]


class Ink:
    """Ink pixels: {row: int whose big-endian bytes are 1 where there is ink}."""

    def __init__(self, rows, w):
        self.rows, self.w = rows, w

    def __bool__(self):
        return bool(self.rows)

    def cols(self, x0=0, x1=None):
        x1 = self.w if x1 is None else x1
        keep = ((1 << (8 * (x1 - x0))) - 1) << (8 * (self.w - x1))
        return Ink({y: m & keep for y, m in self.rows.items() if m & keep}, self.w)

    def lines(self, y0, y1):
        return Ink({y: m for y, m in self.rows.items() if y0 <= y <= y1}, self.w)

    def minus(self, other):
        rows = {}
        for y, m in self.rows.items():
            m &= ~other.rows.get(y, 0)
            if m:
                rows[y] = m
        return Ink(rows, self.w)

    def _union(self):
        u = 0
        for m in self.rows.values():
            u |= m
        return u.to_bytes(self.w, 'big')

    def bbox(self):
        """(left, top, right, bottom), inclusive; None without ink."""
        if not self.rows:
            return None
        u = self._union()
        ys = sorted(self.rows)
        return u.find(b'\x01'), ys[0], u.rfind(b'\x01'), ys[-1]

    def bands(self):
        """Runs of consecutive rows with ink: one per drawn line of H's."""
        out = []
        for y in sorted(self.rows):
            if out and y == out[-1][1] + 1:
                out[-1][1] = y
            else:
                out.append([y, y])
        return [tuple(b) for b in out]

    def clusters(self, y0, y1, gap=16):
        """Column runs of ink in rows y0..y1, joining runs closer than gap px."""
        runs = [[m.start(), m.end() - 1]
                for m in re.finditer(rb'\x01+', self.lines(y0, y1)._union())]
        out = []
        for r in runs:
            if out and r[0] - out[-1][1] - 1 < gap:
                out[-1][1] = r[1]
            else:
                out.append(r)
        return [tuple(c) for c in out]


def line_starts(ink):
    """x where each drawn line's ink starts, top to bottom."""
    return [ink.lines(y0, y1).bbox()[0] for y0, y1 in ink.bands()]


def line_pitch(bands):
    """Distance between consecutive lines, from the first to the last band."""
    return (bands[-1][0] - bands[0][0]) / (len(bands) - 1)


def lines_that_fit(height, margin, pitch):
    """README: (panel height - 2 x top margin) / line height, rounded down.
    The measured pitch is good to about +-0.3 px, so give the range."""
    room = height - 2 * margin
    return math.floor(room / (pitch + 0.3)), math.floor(room / (pitch - 0.3))


def read_lines(path):
    try:
        with open(path) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def wait_until(pred, deadline, step=0.2):
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def settle(capture, want_ink, deadline, alive=lambda: True):
    """Capture until two captures 0.4 s apart agree (and show ink if wanted)."""
    prev = None
    while True:
        img = capture()
        good = not img.has_magenta() and (bool(img.ink()) if want_ink else True)
        if good and prev is not None and prev.data == img.data:
            return img
        if time.monotonic() >= deadline or not alive():
            return img
        prev = img if good else None
        time.sleep(0.4)


# --------------------------------------------------------------------------
# lock-screen scenarios: several xscreensaver-like windows on one Xvfb
# --------------------------------------------------------------------------

class Scn:
    """One lockin run against one window. '@D@' in a command is its own folder."""

    def __init__(self, name, w, h, left=None, right=None, hex_id=False):
        self.name, self.w, self.h = name, w, h
        self.left, self.right, self.hex_id = left, right, hex_id
        self.x = self.y = 0
        self.dir = None
        self.lockin = None
        self.img = None
        self.log = ''
        self.files = {}

    def cmd(self, template):
        return template.replace('@D@', shlex.quote(self.dir))

    def path(self, name):
        return os.path.join(self.dir, name)

    def lines(self, name):
        return read_lines(self.path(name))

    def alive(self):
        return self.lockin is not None and self.lockin.alive()

    def keep(self, *names):
        """Copy files the tests need before the batch folder is removed."""
        for n in names:
            self.files[n] = self.lines(n)

    def diag(self):
        start = self.lockin.error if self.lockin and self.lockin.error else ''
        return f'[{self.name}] {start} lockin output: {self.log[-600:]!r}'


class LockBatch:
    GAP = 40
    MAX_W = 3400

    def __init__(self, scenarios):
        self.scns = {s.name: s for s in scenarios}
        x = y = row_h = width = height = 0
        for s in scenarios:
            if x and x + s.w > self.MAX_W:
                x, y, row_h = 0, y + row_h + self.GAP, 0
            s.x, s.y = x, y
            x += s.w + self.GAP
            row_h = max(row_h, s.h)
            width, height = max(width, s.x + s.w), max(height, s.y + s.h)
        self.size = (width, height)
        self.tmp = tempfile.mkdtemp(prefix='lockin-test-')
        self.xvfb = self.agent = None
        self.closed = False

    def __getitem__(self, name):
        return self.scns[name]

    def start(self):
        self.xvfb = Xvfb(*self.size)
        env = base_env(self.xvfb.display, self.tmp)
        self.agent = Agent(env, os.path.join(self.tmp, 'agent.log'))
        self.agent.call('root_background', pixel=MAGENTA)
        for s in self.scns.values():
            s.dir = os.path.join(self.tmp, s.name)
            os.mkdir(s.dir)
            s.xid = self.agent.call('window', x=s.x, y=s.y, w=s.w, h=s.h, pixel=MAGENTA)['xid']
            args = []
            if s.left is not None:
                args += ['--left', s.cmd(s.left)]
            if s.right is not None:
                args += ['--right', s.cmd(s.right)]
            e = dict(env, XSCREENSAVER_WINDOW=hex(s.xid) if s.hex_id else str(s.xid))
            s.lockin = Lockin(args, e, os.path.join(self.tmp, s.name + '.log'))
        self.t0 = time.monotonic()

    def capture(self, s):
        path = os.path.join(self.tmp, 'capture.rgb')
        self.agent.call('capture', x=s.x, y=s.y, w=s.w, h=s.h, path=path)
        with open(path, 'rb') as f:
            return Image(s.w, s.h, f.read())

    def settle(self, s, want_ink, at_least=2.0):
        deadline = max(self.t0 + STARTUP, time.monotonic() + at_least)
        s.img = settle(lambda: self.capture(s), want_ink, deadline, s.alive)
        return s.img

    def deadline(self, extra=0.0):
        return self.t0 + STARTUP + extra

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            for s in self.scns.values():
                if s.lockin:
                    s.lockin.stop()
                    s.log = tail(s.lockin.log_path, 2000)
        finally:
            try:
                if self.agent:
                    self.agent.close()
            finally:
                if self.xvfb:
                    self.xvfb.stop()
                shutil.rmtree(self.tmp, ignore_errors=True)


class LockTestCase(unittest.TestCase):
    batch = None

    def scn(self, name):
        s = self.batch[name]
        if s.lockin is None or s.lockin.error:
            self.fail(f'lockin did not start: {s.diag()}')
        return s

    def assert_ends_with_ellipsis(self, ink, what):
        """The first line of ink ends in low marks (the dots of "…") after its last
        full-height letter, not in a letter clipped at the margin."""
        y0, y1 = ink.bands()[0]
        height = y1 - y0 + 1
        last_full_letter = ink.lines(y0, y0 + int(0.4 * height)).bbox()[2]
        after = ink.cols(last_full_letter + 1).lines(y0, y1)
        self.assertTrue(after, f'{what} should end with "…" after its last letter; nothing is '
                               f'drawn after the last full-height letter (looks clipped)')
        a_left, a_top, a_right, _ = after.bbox()
        self.assertGreaterEqual(a_top, y1 - int(0.35 * height),
                                f'the marks after the last letter of {what} should sit low on the '
                                f'line like the dots of "…"; they reach up to y={a_top} on a line '
                                f'spanning y={y0}..{y1}')
        self.assertGreaterEqual(a_right - a_left, 8,
                                f'the "…" after the last letter of {what} should be several px '
                                f'wide; the marks span only x={a_left}..{a_right}')

    def drawn_ink(self, name, what):
        """The scenario's ink; fails if lockin left the window unpainted or empty."""
        s = self.scn(name)
        self.assertFalse(s.img.has_magenta(),
                         f'lockin should paint the whole window it is given; parts of it were '
                         f'never painted. {s.diag()}')
        ink = s.img.ink()
        self.assertTrue(ink, f'{what} should be drawn, but the window shows no text. {s.diag()}')
        return ink


# --------------------------------------------------------------------------
# LOCK SCREEN: window id, background, margins, colour
# --------------------------------------------------------------------------

LEFT_TEXT = 'printf "HHHH\\nHH\\nHHHHHH\\nH\\n"; echo run >> @D@/runs'


@needs_xvfb
class LockScreenLayoutTests(LockTestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = b = LockBatch([
            Scn('decimal', 1600, 900, left=LEFT_TEXT),
            Scn('hex', 1600, 900, left=LEFT_TEXT, hex_id=True),
            Scn('right', 1600, 900, left='printf ""',
                right='printf "HHHHHH\\nHH\\n"; echo run >> @D@/runs'),
            Scn('empty', 1600, 900, left='echo run >> @D@/runs', right='printf ""'),
            Scn('stderr', 1600, 900, left='echo HHHHHHHH >&2; echo run >> @D@/runs'),
        ])
        cls.addClassCleanup(b.close)
        b.start()
        for s in b.scns.values():
            wait_until(lambda: len(s.lines('runs')) >= 1 or not s.alive(), b.deadline())
        for name in ('decimal', 'hex', 'right'):
            b.settle(b[name], want_ink=True)
        time.sleep(0.5)
        for name in ('empty', 'stderr'):
            b.settle(b[name], want_ink=False)
        b.close()

    def test_decimal_window_id_is_painted_and_drawn_into(self):
        self.drawn_ink('decimal', 'the left panel (window id given in decimal)')

    def test_hex_window_id_is_painted_and_drawn_into(self):
        self.drawn_ink('hex', 'the left panel (window id given as hex with 0x)')

    def test_window_with_empty_panels_is_entirely_0a0a0a(self):
        s = self.scn('empty')
        bad, example = s.img.off_colour(BG, 0, 0, s.w, s.h)
        self.assertEqual(bad, 0, f'with panels that print nothing, the whole window should be '
                                 f'#0a0a0a; {bad} pixels are not, e.g. {example}. {s.diag()}')

    def test_background_around_text_is_0a0a0a(self):
        s = self.scn('decimal')
        # text-free areas: above the top margin, left of the left margin, and the
        # right and bottom halves (four short lines of H's end far before them)
        for x0, y0, x1, y1 in ((0, 0, s.w, 60), (0, 0, 80, s.h),
                               (0, s.h // 2, s.w, s.h), (s.w // 2, 0, s.w, s.h)):
            bad, example = s.img.off_colour(BG, x0, y0, x1, y1)
            self.assertEqual(bad, 0, f'the window background should be #0a0a0a; in the text-free '
                                     f'area ({x0},{y0})-({x1},{y1}) {bad} pixels are not, '
                                     f'e.g. {example}. {s.diag()}')

    def test_left_panel_text_starts_90px_from_left_edge(self):
        left = self.drawn_ink('decimal', 'the left panel').bbox()[0]
        self.assertTrue(89 <= left <= 96,
                        f'the left panel should be drawn 90 px from the left edge; its text '
                        f'starts at x={left} (expected 90 plus at most a few px of glyph '
                        f'side bearing)')

    def test_left_panel_text_starts_70px_from_top(self):
        ink = self.drawn_ink('decimal', 'the left panel')
        bands = ink.bands()
        self.assertEqual(len(bands), 4, f'expected the 4 printed lines as 4 bands of ink, got '
                                        f'{bands}')
        pitch = line_pitch(bands)
        top = bands[0][0]
        self.assertTrue(69 <= top <= 70 + 0.45 * pitch,
                        f'the left panel should be drawn 70 px from the top; the first line\'s '
                        f'capital letters start at y={top} (line height {pitch:.1f} px, so '
                        f'expected between 70 and {70 + 0.45 * pitch:.0f})')

    def test_left_panel_lines_are_left_aligned(self):
        starts = line_starts(self.drawn_ink('decimal', 'the left panel'))
        self.assertLessEqual(max(starts) - min(starts), 1,
                             f'lines of different length in the left panel should all start at '
                             f'the same x (left-aligned); they start at {starts}')

    def test_text_colour_is_dcdcdc(self):
        s = self.scn('decimal')
        self.drawn_ink('decimal', 'the left panel')
        n = s.img.count(FG)
        self.assertGreaterEqual(n, 50, f'panel text should be colour #dcdcdc; only {n} pixels '
                                       f'of that exact colour were found inside the letters')

    def test_right_panel_text_block_ends_90px_from_right_edge(self):
        s = self.scn('right')
        right = self.drawn_ink('right', 'the right panel').bbox()[2]
        edge = s.w - 90
        self.assertTrue(edge - 8 <= right <= edge,
                        f'the right panel\'s text block should end 90 px from the right edge '
                        f'(x={edge}); its text ends at x={right}')

    def test_right_panel_text_starts_70px_from_top(self):
        bands = self.drawn_ink('right', 'the right panel').bands()
        self.assertEqual(len(bands), 2, f'expected the 2 printed lines as 2 bands, got {bands}')
        pitch = line_pitch(bands)
        top = bands[0][0]
        self.assertTrue(69 <= top <= 70 + 0.45 * pitch,
                        f'the right panel should be drawn 70 px from the top; its first line\'s '
                        f'capitals start at y={top} (line height {pitch:.1f} px, so expected '
                        f'between 70 and {70 + 0.45 * pitch:.0f})')

    def test_right_panel_lines_are_left_aligned(self):
        starts = line_starts(self.drawn_ink('right', 'the right panel'))
        self.assertEqual(len(starts), 2, f'expected 2 drawn lines, got starts {starts}')
        self.assertLessEqual(abs(starts[0] - starts[1]), 1,
                             f'the right block ends 90 px from the right edge but its lines are '
                             f'left-aligned: the short second line should start where the long '
                             f'first one does; they start at {starts}')

    def test_stderr_is_not_panel_text(self):
        s = self.scn('stderr')
        self.assertFalse(s.img.has_magenta(), f'lockin should paint the window. {s.diag()}')
        self.assertFalse(s.img.ink(), f'only the command\'s standard output is panel text; '
                                      f'text written to stderr was drawn at '
                                      f'{s.img.ink().bbox()}')


# --------------------------------------------------------------------------
# LOCK SCREEN: right panel against the left one, over-long lines, TAB
# columns, $LINES and the bottom margin
# --------------------------------------------------------------------------

SQUEEZE_LEFT = 'printf "HH\\nHHHHHHHHHHHH\\nHHH\\n"; echo run >> @D@/runs'
TAB_ROWS = 'H\\tHH\\tH\\nHHHHHH\\tH\\tH\\n\\tHHHH\\tH\\n'


@needs_xvfb
class LockScreenPanelTextTests(LockTestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = b = LockBatch([
            # same left panel twice; only one has a right panel, so the
            # difference between the two captures is the right panel alone
            Scn('squeeze', 1600, 600, left=SQUEEZE_LEFT,
                right='printf "%s\\n" ' + 'H' * 60 + '; echo run >> @D@/rruns'),
            Scn('squeeze_ref', 1600, 600, left=SQUEEZE_LEFT,
                right='printf ""; echo run >> @D@/rruns'),
            Scn('tabs', 1600, 900, left='printf "' + TAB_ROWS + '"; echo run >> @D@/runs'),
            Scn('tabs_plain', 1600, 900,
                left='printf "' + 'H' * 20 + '\\n' + TAB_ROWS + '"; echo run >> @D@/runs'),
            Scn('lines', 1000, 800,
                left='echo "$LINES" >> @D@/lines; i=0; '
                     'while [ $i -lt 60 ]; do echo H; i=$((i+1)); done',
                right='echo "$LINES" >> @D@/rlines'),
            # no right panel in a portrait window: the left panel's right
            # margin is 90 px, as wide as its left one
            Scn('portrait_cut', 700, 1000,
                left='printf "%s\\n" ' + 'H' * 40 + '; echo run >> @D@/runs'),
        ])
        cls.addClassCleanup(b.close)
        b.start()
        for s in b.scns.values():
            wait_until(lambda: s.lines('runs') or s.lines('lines') or not s.alive(), b.deadline())
        for name in ('squeeze', 'squeeze_ref'):
            wait_until(lambda: b[name].lines('rruns') or not b[name].alive(), b.deadline())
        wait_until(lambda: b['lines'].lines('rlines') or not b['lines'].alive(), b.deadline())
        for s in b.scns.values():
            b.settle(s, want_ink=True)
        b['lines'].keep('lines', 'rlines')
        b.close()

    # -- right panel vs. left panel --------------------------------------

    def right_panel(self):
        """Ink of the right panel (squeeze minus squeeze_ref) and the left panel."""
        left = self.drawn_ink('squeeze_ref', 'the left panel')
        both = self.drawn_ink('squeeze', 'the panels')
        right = both.minus(left)
        self.assertTrue(right, 'the right panel (an over-long line) should be drawn on a '
                               'landscape window')
        return left, right

    def test_right_block_starts_90px_after_left_panels_widest_line(self):
        left, right = self.right_panel()
        left_end = left.bbox()[2]
        right_start = right.bbox()[0]
        gap = right_start - left_end - 1
        self.assertGreaterEqual(gap, 89,
                                f'the right panel should start no closer than 90 px to the end '
                                f'of the left panel\'s widest line (x={left_end}); it starts at '
                                f'x={right_start}, {gap} px after it')

    def test_over_long_right_line_stays_inside_90px_right_margin(self):
        s = self.scn('squeeze')
        _, right = self.right_panel()
        end = right.bbox()[2]
        self.assertLessEqual(end, s.w - 90,
                             f'a line too wide for the room left should be cut short so the '
                             f'right block still ends 90 px from the right edge (x={s.w - 90}); '
                             f'its text reaches x={end}')

    def test_over_long_line_ends_with_ellipsis_not_a_clipped_letter(self):
        _, right = self.right_panel()
        self.assert_ends_with_ellipsis(right, 'the cut right-panel line')

    def test_over_long_left_line_is_cut_at_90px_right_margin(self):
        s = self.scn('portrait_cut')
        end = self.drawn_ink('portrait_cut', 'the left panel').bbox()[2]
        self.assertLessEqual(end, s.w - 90,
                             f'a left-panel line that would reach into the right margin (90 px, '
                             f'as wide as the left one) should be cut short; in a {s.w} px wide '
                             f'window its text reaches x={end}, past x={s.w - 90}')

    def test_cut_left_line_ends_with_ellipsis(self):
        ink = self.drawn_ink('portrait_cut', 'the left panel')
        self.assert_ends_with_ellipsis(ink, 'the cut left-panel line')

    # -- TAB columns -----------------------------------------------------

    def tab_cells(self, name, first_row_band):
        """Per printed row: ink clusters (cells). Rows: 'H|HH|H', 'HHHHHH|H|H', '|HHHH|H'."""
        bands = self.drawn_ink(name, 'the TAB rows').bands()
        rows = bands[first_row_band:first_row_band + 3]
        self.assertEqual(len(rows), 3, f'expected 3 drawn rows of cells, got bands {bands}')
        ink = self.batch[name].img.ink()
        cells = [ink.clusters(y0, y1) for y0, y1 in rows]
        self.assertEqual([len(c) for c in cells], [3, 3, 2],
                         f'expected cells "H|HH|H", "HHHHHH|H|H", "|HHHH|H" to be drawn as '
                         f'3, 3 and 2 separate groups, got {cells}')
        return cells

    def test_tab_cells_line_up_in_columns(self):
        r0, r1, r2 = self.tab_cells('tabs', 0)
        col1 = [r0[1][0], r1[1][0], r2[0][0]]
        col2 = [r0[2][0], r1[2][0], r2[1][0]]
        self.assertLessEqual(max(col1) - min(col1), 1,
                             f'cells of the second column should start at the same x in every '
                             f'row (also after an empty first cell); they start at {col1}')
        self.assertLessEqual(max(col2) - min(col2), 1,
                             f'cells of the third column should start at the same x in every '
                             f'row; they start at {col2}')

    def test_tab_column_starts_32px_after_widest_cell_before_it(self):
        r0, r1, r2 = self.tab_cells('tabs', 0)
        widest0_end = max(r0[0][1], r1[0][1])          # "HHHHHH"
        widest1_end = max(r0[1][1], r1[1][1], r2[0][1])  # "HHHH"
        gap1 = r1[1][0] - widest0_end - 1
        gap2 = r1[2][0] - widest1_end - 1
        # ink gaps are 32 px plus the glyphs' side bearings (a few px)
        self.assertTrue(31 <= gap1 <= 40,
                        f'the second column should start 32 px after the end of the widest cell '
                        f'of the first column; the ink gap is {gap1} px')
        self.assertTrue(31 <= gap2 <= 40,
                        f'the third column should start 32 px after the end of the widest cell '
                        f'of the second column; the ink gap is {gap2} px')

    def test_line_without_tab_does_not_widen_columns(self):
        plain = self.tab_cells('tabs_plain', 1)
        alone = self.tab_cells('tabs', 0)
        header_end = self.batch['tabs_plain'].img.ink().clusters(
            *self.batch['tabs_plain'].img.ink().bands()[0])[-1][1]
        self.assertGreater(header_end, plain[0][2][1],
                           'precondition: the line without TAB should be wider than the rows')
        self.assertLessEqual(abs(plain[1][1][0] - alone[1][1][0]), 1,
                             f'a wide line without TAB is drawn as it is and should not move the '
                             f'TAB columns; the second column starts at x={plain[1][1][0]} with '
                             f'it and at x={alone[1][1][0]} without it')

    # -- $LINES and the bottom margin -----------------------------------

    def test_LINES_is_how_many_lines_fit_between_margins(self):
        s = self.scn('lines')
        bands = self.drawn_ink('lines', 'the left panel').bands()
        self.assertGreaterEqual(len(bands), 3, f'expected many drawn lines, got {bands}')
        values = s.files['lines']
        self.assertTrue(values, 'the left command should have run')
        lo, hi = lines_that_fit(s.h, 70, line_pitch(bands))
        for v in values:
            self.assertTrue(v.isdigit() and lo <= int(v) <= hi,
                            f'$LINES should be (window height {s.h} - 2 x 70) / line height '
                            f'{line_pitch(bands):.1f}, rounded down = {lo}'
                            f'{"" if lo == hi else f" or {hi}"}; the command got {v!r}')

    def test_only_whole_lines_above_bottom_margin_are_drawn(self):
        s = self.scn('lines')
        bands = self.drawn_ink('lines', 'the left panel').bands()
        self.assertGreaterEqual(len(bands), 3, f'expected many drawn lines, got {bands}')
        lo, hi = lines_that_fit(s.h, 70, line_pitch(bands))
        self.assertTrue(lo <= len(bands) <= hi,
                        f'of 60 printed lines only the whole lines that fit above the 70 px '
                        f'bottom margin should be drawn: {lo} in a {s.h} px window; '
                        f'{len(bands)} were drawn')
        self.assertLessEqual(bands[-1][1], s.h - 70,
                             f'no drawn line may reach below the bottom margin (y={s.h - 70}); '
                             f'the last one reaches y={bands[-1][1]}')

    def test_at_most_LINES_lines_are_drawn(self):
        s = self.scn('lines')
        bands = self.drawn_ink('lines', 'the left panel').bands()
        values = [int(v) for v in s.files['lines'] if v.isdigit()]
        self.assertTrue(values, f'the command should have received a number in $LINES; got '
                                f'{s.files["lines"]}')
        self.assertLessEqual(len(bands), min(values),
                             f'at most $LINES lines should be drawn: $LINES was {min(values)}, '
                             f'{len(bands)} lines were drawn')

    def test_right_command_gets_same_LINES_on_lock_screen(self):
        s = self.scn('lines')
        left, right = s.files['lines'], s.files['rlines']
        self.assertTrue(left and right, f'both commands should have run. {s.diag()}')
        self.assertEqual(right[0], left[0],
                         'both lock-screen panels start 70 px from the top of the same window, '
                         'so the right command should get the same $LINES as the left')


# --------------------------------------------------------------------------
# LOCK SCREEN: when the right panel exists, failures, /bin/sh
# --------------------------------------------------------------------------

@needs_xvfb
class LockScreenCommandTests(LockTestCase):
    FAIL_TEXT = '(exit 3 failed: exit 3)'

    @classmethod
    def setUpClass(cls):
        cls.batch = b = LockBatch([
            Scn('square', 900, 900, left='printf "HH\\n"; echo run >> @D@/runs',
                right='printf "HH\\n"; echo run >> @D@/rruns'),
            Scn('portrait', 700, 1000, left='printf "HH\\n"; echo run >> @D@/runs',
                right='echo run >> @D@/rruns; printf "HH\\n"'),
            Scn('failing', 1200, 600, left='exit 3'),
            Scn('failing_ref', 1200, 600, left="printf '%s\\n' " + shlex.quote(cls.FAIL_TEXT)),
            Scn('shell', 600, 600, left='printf "%s\\n" "$0" > @D@/zero; printf "H\\n"'),
        ])
        cls.addClassCleanup(b.close)
        b.start()
        sq, pt = b['square'], b['portrait']
        wait_until(lambda: (sq.lines('runs') and sq.lines('rruns')) or not sq.alive(), b.deadline())
        wait_until(lambda: len(pt.lines('runs')) >= 2 or not pt.alive(), b.deadline(4))
        for s in b.scns.values():
            b.settle(s, want_ink=True)
        pt.keep('runs', 'rruns')
        sq.keep('rruns')
        b['shell'].keep('zero')
        b.close()

    def test_square_window_gets_right_panel(self):
        s = self.scn('square')
        self.assertTrue(s.files['rruns'], 'a window exactly as wide as tall should run the '
                                          'right command')
        ink = self.drawn_ink('square', 'the panels')
        box = ink.cols(s.w // 2).bbox()
        self.assertIsNotNone(box, 'a window exactly as wide as tall should show the right panel')
        self.assertTrue(s.w - 98 <= box[2] <= s.w - 90,
                        f'the right panel should end 90 px from the right edge (x={s.w - 90}); '
                        f'it ends at x={box[2]}')

    def test_portrait_window_never_runs_right_command(self):
        s = self.scn('portrait')
        self.assertGreaterEqual(len(s.files['runs']), 2,
                                f'the left command should have run at least twice. {s.diag()}')
        self.assertEqual(s.files['rruns'], [],
                         'the right command must never run for a window taller than wide')

    def test_portrait_window_draws_no_right_panel(self):
        s = self.scn('portrait')
        ink = self.drawn_ink('portrait', 'the left panel')
        right = ink.cols(s.w // 2).bbox()
        self.assertIsNone(right, f'a window taller than wide should show no right panel; text '
                                 f'was drawn in its right half at {right}')

    def test_failed_command_before_any_good_run_shows_failure_text(self):
        failing = self.scn('failing')
        ref = self.scn('failing_ref')
        self.drawn_ink('failing_ref', 'the reference text')
        self.drawn_ink('failing', f'the failure text {self.FAIL_TEXT!r}')
        self.assertTrue(failing.img.data == ref.img.data,
                        f'a command that exits 3 before any good run should show exactly '
                        f'{self.FAIL_TEXT!r}: its window should look the same as one whose '
                        f'command prints that text; they differ (failing ink '
                        f'{failing.img.ink().bbox()}, reference ink {ref.img.ink().bbox()})')

    def test_commands_run_through_bin_sh(self):
        s = self.scn('shell')
        self.assertTrue(s.files['zero'], f'the left command should have run. {s.diag()}')
        self.assertIn(s.files['zero'][0], ('/bin/sh', 'sh'),
                      f'each CMD should run through /bin/sh -c, so $0 should be /bin/sh; it was '
                      f'{s.files["zero"][0]!r}')


# --------------------------------------------------------------------------
# refresh timing, overlapping runs, keeping the last good text
# --------------------------------------------------------------------------

OVERLAP_CMD = (
    'p=$(cat @D@/pid 2>/dev/null); '
    'if [ -n "$p" ] && [ -d "/proc/$p" ] && '
    '! grep -q "^State:[[:space:]]*Z" "/proc/$p/status" 2>/dev/null; '
    'then echo overlap >> @D@/log; fi; '
    'echo $$ > @D@/pid; echo start >> @D@/log; sleep 3; echo end >> @D@/log; printf "H\\n"'
)
KEEP_CMD = (
    'n=$(cat @D@/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > @D@/n; '
    'if [ "$n" -eq 1 ]; then printf "HHH\\nH\\n"; '
    'else printf "WRONG\\nWRONG WRONG WRONG\\n"; exit 1; fi'
)


@needs_xvfb
class PanelRefreshTests(LockTestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = b = LockBatch([
            # runs take 1 s and 2 s: counted start to start the gaps stay 2 s
            # and 10 s; counted from the end they would be 3 s and 12 s
            Scn('freq', 800, 600, left='date +%s.%N >> @D@/ltimes; sleep 1; printf "H\\n"',
                right='date +%s.%N >> @D@/rtimes; sleep 2; printf "H\\n"'),
            Scn('slow', 800, 600, left=OVERLAP_CMD),
            Scn('keep', 800, 600, left=KEEP_CMD),
            Scn('keep_ref', 800, 600, left='printf "HHH\\nH\\n"'),
        ])
        cls.addClassCleanup(b.close)
        b.start()
        fr, sl, kp = b['freq'], b['slow'], b['keep']

        def n_runs():
            v = kp.lines('n')
            return int(v[0]) if v and v[0].isdigit() else 0

        wait_until(lambda: n_runs() >= 3 or not kp.alive(), b.deadline(8))
        time.sleep(1.0)
        b.settle(kp, want_ink=True)
        b.settle(b['keep_ref'], want_ink=True)
        kp.runs_seen = n_runs()
        wait_until(lambda: sl.lines('log').count('start') >= 3 or not sl.alive(), b.deadline(10))
        wait_until(lambda: len(fr.lines('rtimes')) >= 2 or not fr.alive(), b.deadline(14))
        fr.keep('ltimes', 'rtimes')
        sl.keep('log')
        b.close()

    @staticmethod
    def intervals(lines):
        t = [float(x) for x in lines if re.fullmatch(r'\d+(\.\d+)?', x)]
        return [b - a for a, b in zip(t, t[1:])]

    def test_left_command_starts_every_2_seconds_start_to_start(self):
        s = self.scn('freq')
        gaps = self.intervals(s.files['ltimes'])
        self.assertGreaterEqual(len(gaps), 3,
                                f'the left command should start every 2 s; it started '
                                f'{len(s.files["ltimes"])} times in about 11 s. {s.diag()}')
        median = sorted(gaps)[len(gaps) // 2]
        self.assertTrue(1.7 <= median <= 2.4,
                        f'a left command taking 1 s should still start every 2 s, counted from '
                        f'the start of the previous run (3 s would mean counted from its end); '
                        f'typical gap {median:.2f} s (all gaps {[round(g, 2) for g in gaps]})')

    def test_right_command_starts_every_10_seconds_start_to_start(self):
        s = self.scn('freq')
        gaps = self.intervals(s.files['rtimes'])
        self.assertTrue(gaps, f'the right command should start at once and again 10 s later; '
                              f'runs recorded: {len(s.files["rtimes"])}. {s.diag()}')
        self.assertTrue(9.0 <= gaps[0] <= 11.0,
                        f'a right command taking 2 s should still start every 10 s, counted from '
                        f'the start of the previous run (12 s would mean counted from its end); '
                        f'the first gap was {gaps[0]:.2f} s')

    def test_new_run_never_starts_while_previous_still_going(self):
        s = self.scn('slow')
        log = s.files['log']
        self.assertGreaterEqual(log.count('start'), 2,
                                f'a slow (3 s) left command should still be rerun once it has '
                                f'finished; runs started: {log.count("start")}. {s.diag()}')
        self.assertNotIn('overlap', log,
                         f'a new run of a 3 s command started while the previous run was still '
                         f'going (run log: {log})')

    def test_failed_runs_keep_last_good_text(self):
        keep, ref = self.scn('keep'), self.scn('keep_ref')
        self.assertGreaterEqual(keep.runs_seen, 3,
                                f'the left command should have run at least 3 times (1 good, '
                                f'then failures); it ran {keep.runs_seen}. {keep.diag()}')
        self.drawn_ink('keep_ref', 'the reference text')
        self.drawn_ink('keep', 'the last good text')
        self.assertTrue(keep.img.data == ref.img.data,
                        'after a good run and then failing runs (which print other text and exit '
                        '1), the panel should still show the last good text; it does not match '
                        f'a panel showing that text (ink {keep.img.ink().bbox()} vs '
                        f'{ref.img.ink().bbox()})')


# --------------------------------------------------------------------------
# DESKTOP (--desk)
# --------------------------------------------------------------------------

def parse_listmonitors(text):
    """xrandr --listmonitors -> [(primary, name, w, h, x, y)]"""
    out = []
    for line in text.splitlines():
        m = re.match(r'\s*\d+:\s+([*+]*)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)', line)
        if m:
            out.append(('*' in m.group(1), m.group(2), int(m.group(3)), int(m.group(4)),
                        int(m.group(5)), int(m.group(6))))
    return out


class DeskRun:
    """lockin --desk on its own Xvfb."""

    def __init__(self, name, left=None, right=None, screen=(2560, 1440), monitors=None,
                 n_windows=None):
        self.name, self.left, self.right = name, left, right
        self.screen, self.monitors = screen, monitors
        self.n_windows = n_windows or (left is not None) + (right is not None)
        self.xvfb = self.agent = self.lockin = None
        self.windows = []
        self.images = []
        self.files = {}
        self.setup_problem = None
        self.log = ''
        self.dir = None

    def cmd(self, template):
        return template.replace('@D@', shlex.quote(self.dir))

    def start(self, tmp):
        self.dir = os.path.join(tmp, self.name)
        os.mkdir(self.dir)
        self.xvfb = Xvfb(*self.screen)
        env = base_env(self.xvfb.display, self.dir)
        if self.monitors:
            for name, geometry, outputs in self.monitors:
                subprocess.run([XRANDR, '--setmonitor', name, geometry, outputs], env=env,
                               capture_output=True, timeout=15)
            self.xrandr = parse_listmonitors(subprocess.run(
                [XRANDR, '--listmonitors'], env=env, capture_output=True, text=True,
                timeout=15).stdout)
        self.agent = Agent(env, os.path.join(self.dir, 'agent.log'))
        self.agent.call('root_background', pixel=MAGENTA)
        self.gdk_monitors = self.agent.call('monitors')['monitors']
        args = ['--desk']
        if self.left is not None:
            args += ['--left', self.cmd(self.left)]
        if self.right is not None:
            args += ['--right', self.cmd(self.right)]
        self.lockin = Lockin(args, env, os.path.join(self.dir, 'lockin.log'))

    def collect(self, deadline):
        """Wait for the windows to settle, then capture each of them."""
        prev = None
        while True:
            wins = self.agent.call('toplevels')['windows']
            geo = sorted((w['x'], w['y'], w['w'], w['h']) for w in wins)
            if len(wins) >= self.n_windows and geo == prev:
                break
            if time.monotonic() >= deadline or not self.lockin or not self.lockin.alive():
                break
            prev = geo
            time.sleep(0.5)
        self.windows = sorted(wins, key=lambda w: (w['x'], w['y']))
        self.images = []
        sw, sh = self.screen
        for w in self.windows:
            # only the part of the window that is on the screen can be seen
            x0, y0 = max(0, w['x']), max(0, w['y'])
            x1, y1 = min(sw, w['x'] + w['w']), min(sh, w['y'] + w['h'])
            if x1 <= x0 or y1 <= y0:
                self.images.append(None)
                continue
            path = os.path.join(self.dir, 'capture.rgb')

            def cap(x0=x0, y0=y0, x1=x1, y1=y1, w=w, path=path):
                self.agent.call('capture', x=x0, y=y0, w=x1 - x0, h=y1 - y0, path=path)
                with open(path, 'rb') as f:
                    img = Image(x1 - x0, y1 - y0, f.read())
                img.ox, img.oy = x0 - w['x'], y0 - w['y']
                return img

            self.images.append(settle(cap, True, max(deadline, time.monotonic() + 2),
                                      self.lockin.alive))
        self.windows = self.agent.call('toplevels')['windows']
        self.windows.sort(key=lambda w: (w['x'], w['y']))

    def keep(self, *names):
        for n in names:
            self.files[n] = read_lines(os.path.join(self.dir, n))

    def diag(self):
        err = self.lockin.error if self.lockin and self.lockin.error else ''
        return f'[{self.name}] {err} lockin output: {self.log[-600:]!r}'

    def stop(self):
        try:
            if self.lockin:
                self.lockin.stop()
                self.log = tail(self.lockin.log_path, 2000)
        finally:
            try:
                if self.agent:
                    self.agent.close()
            finally:
                if self.xvfb:
                    self.xvfb.stop()
                self.xvfb = self.agent = None


class DeskBatch:
    def __init__(self, runs):
        self.runs = {r.name: r for r in runs}
        self.tmp = tempfile.mkdtemp(prefix='lockin-desk-test-')
        self.closed = False

    def __getitem__(self, name):
        return self.runs[name]

    def start(self):
        for r in self.runs.values():
            r.start(self.tmp)
        self.t0 = time.monotonic()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            for r in self.runs.values():
                r.stop()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class DeskTestCase(unittest.TestCase):
    batch = None

    def run_of(self, name, n=None):
        r = self.batch[name]
        if r.lockin is None or r.lockin.error:
            self.fail(f'lockin did not start: {r.diag()}')
        n = r.n_windows if n is None else n
        self.assertEqual(len(r.windows), n,
                         f'lockin --desk with {n} panel(s) should show {n} window(s); found '
                         f'{len(r.windows)}: {r.windows}. {r.diag()}')
        return r


WIDE = 'H' * 150
MON_HALF = 720


@needs_xvfb
class DesktopTests(DeskTestCase):
    MON_W, MON_H = 2560, 1440

    @classmethod
    def setUpClass(cls):
        cls.batch = b = DeskBatch([
            DeskRun('both',
                    left='i=0; while [ $i -lt 8 ]; do echo HH; i=$((i+1)); done; '
                         'echo "$LINES" >> @D@/left_lines',
                    right='printf "HHHHHHHH\\nHHH\\n"; echo "$LINES" >> @D@/right_lines'),
            DeskRun('left_only', left='printf "HH\\n"'),
            DeskRun('right_only', right='printf "HH\\n"'),
            DeskRun('wide_right', left='printf "HH\\n"', right='printf "%s\\n" ' + WIDE),
            # an over-long first line, then far more lines than fit
            DeskRun('long_left',
                    left='printf "%s\\n" ' + 'H' * 60 + '; i=0; '
                         'while [ $i -lt 60 ]; do echo H; i=$((i+1)); done; '
                         'echo "$LINES" >> @D@/lines'),
        ])
        cls.addClassCleanup(b.close)
        b.start()
        for r in b.runs.values():
            r.collect(b.t0 + STARTUP)
        both = b['both']
        wait_until(lambda: (read_lines(os.path.join(both.dir, 'left_lines'))
                            and read_lines(os.path.join(both.dir, 'right_lines')))
                   or not both.lockin or not both.lockin.alive(), b.t0 + STARTUP)
        both.keep('left_lines', 'right_lines')
        b['long_left'].keep('lines')
        b.close()

    def left_right(self, name='both'):
        r = self.run_of(name)
        return r, r.windows[0], r.windows[1], r.images[0], r.images[1]

    def test_one_window_per_given_panel(self):
        for name, n in (('both', 2), ('left_only', 1), ('right_only', 1)):
            with self.subTest(panels=name):
                self.run_of(name, n)

    def test_left_window_at_monitor_left_edge_1280_wide_full_height(self):
        for name in ('both', 'left_only'):
            with self.subTest(run=name):
                w = self.run_of(name).windows[0]
                self.assertEqual((w['x'], w['y'], w['w'], w['h']), (0, 0, 1280, self.MON_H),
                                 'the left window should sit at the monitor\'s left edge, '
                                 '1280 px wide and as tall as the monitor (x, y, w, h)')

    def test_right_window_against_monitor_right_edge_full_height(self):
        for name, idx in (('both', 1), ('right_only', 0)):
            with self.subTest(run=name):
                w = self.run_of(name).windows[idx]
                self.assertEqual((w['x'] + w['w'], w['y'], w['h']), (self.MON_W, 0, self.MON_H),
                                 'the right window should touch the monitor\'s right edge and be '
                                 'as tall as the monitor (right edge, y, height)')

    def test_right_window_is_widest_line_plus_2x44_wide(self):
        r, _, rw, _, rimg = self.left_right()
        box = self.text_box(rimg, 'right window')
        ink_w = box[2] - box[0] + 1
        extra = rw['w'] - 2 * 44 - ink_w
        self.assertTrue(-1 <= extra <= 10,
                        f'the right window should be as wide as its widest line plus 2 x 44 px; '
                        f'it is {rw["w"]} px wide for a widest line about {ink_w} px wide')

    def test_right_window_never_wider_than_monitor_minus_1280(self):
        r, _, rw, _, _ = self.left_right('wide_right')
        self.assertEqual(rw['w'], self.MON_W - 1280,
                         f'with a line far too wide, the right window should be capped at the '
                         f'monitor width minus 1280 px ({self.MON_W - 1280}); it is {rw["w"]} px')

    def visible(self, img, what):
        self.assertIsNotNone(img, f'the {what} should be on the screen')
        return img

    def text_box(self, img, what):
        """Bounding box of the text, in window coordinates."""
        box = self.visible(img, what).ink().bbox()
        self.assertIsNotNone(box, f'the {what} should show its text')
        return box[0] + img.ox, box[1] + img.oy, box[2] + img.ox, box[3] + img.oy

    def text_lines(self, img, what):
        """Top and bottom row of each drawn line, in window coordinates."""
        self.text_box(img, what)
        return [(a + img.oy, b + img.oy) for a, b in img.ink().bands()]

    def test_left_window_text_44px_from_left(self):
        _, _, _, limg, _ = self.left_right()
        left = self.text_box(limg, 'left window')[0]
        self.assertTrue(43 <= left <= 50,
                        f'text in the left window should start 44 px from its left edge; it '
                        f'starts at x={left}')

    def test_left_window_text_40px_from_top(self):
        _, _, _, limg, _ = self.left_right()
        bands = self.text_lines(limg, 'left window')
        pitch = line_pitch(bands)
        top = bands[0][0]
        self.assertTrue(39 <= top <= 40 + 0.45 * pitch,
                        f'text in the left window should start 40 px from the top; the first '
                        f'line\'s capitals start at y={top} (line height {pitch:.1f})')

    def test_right_window_text_44px_from_its_left(self):
        _, _, _, _, rimg = self.left_right()
        left = self.text_box(rimg, 'right window')[0]
        self.assertTrue(43 <= left <= 50,
                        f'text in the right window should start 44 px from its left edge; it '
                        f'starts at x={left}')

    def test_right_window_text_40px_from_top(self):
        _, _, _, limg, rimg = self.left_right()
        pitch = line_pitch(self.text_lines(limg, 'left window'))
        top = self.text_box(rimg, 'right window')[1]
        self.assertTrue(39 <= top <= 40 + 0.45 * pitch,
                        f'text in the right window should start 40 px from the top; its first '
                        f'line\'s capitals start at y={top}')

    def test_right_window_lines_are_left_aligned(self):
        _, _, _, _, rimg = self.left_right()
        self.text_box(rimg, 'right window')
        starts = line_starts(rimg.ink())
        self.assertEqual(len(starts), 2, f'expected 2 drawn lines, got starts {starts}')
        self.assertLessEqual(abs(starts[0] - starts[1]), 1,
                             f'lines in the right window should be left-aligned; the short '
                             f'second line starts at x={starts[1]}, the first at x={starts[0]}')

    def test_left_window_cuts_long_line_at_44px_right_margin(self):
        r = self.run_of('long_left')
        w, img = r.windows[0], r.images[0]
        end = self.text_box(img, 'left window')[2]
        self.assertLessEqual(end, w['w'] - 44,
                             f'a line that would reach into the right margin (44 px, as wide as '
                             f'the left one) should be cut short; in the {w["w"]} px left window '
                             f'its text reaches x={end}, past x={w["w"] - 44}')

    def test_capped_right_window_cuts_long_line_at_44px_right_margin(self):
        r, _, rw, _, rimg = self.left_right('wide_right')
        self.visible(rimg, 'right window')
        self.assertTrue(rimg.ox == 0 and rimg.w == rw['w'],
                        f'the right window ({rw["w"]} px at x={rw["x"]}) should lie on the '
                        f'monitor, so its right margin can be seen')
        end = self.text_box(rimg, 'right window')[2]
        self.assertLessEqual(end, rw['w'] - 44,
                             f'a line too wide for the capped right window should be cut short '
                             f'before its 44 px right margin; in the {rw["w"]} px window its text '
                             f'reaches x={end}, past x={rw["w"] - 44}')

    def test_only_whole_lines_above_bottom_margin_are_drawn_desktop(self):
        r = self.run_of('long_left')
        bands = self.text_lines(r.images[0], 'left window')
        self.assertGreaterEqual(len(bands), 3, f'expected many drawn lines, got {bands}')
        lo, hi = lines_that_fit(self.MON_H, 40, line_pitch(bands))
        self.assertTrue(lo <= len(bands) <= hi,
                        f'of 61 printed lines only the whole lines that fit above the 40 px '
                        f'bottom margin should be drawn: {lo} in a {self.MON_H} px window; '
                        f'{len(bands)} were drawn')
        self.assertLessEqual(bands[-1][1], self.MON_H - 40,
                             f'no drawn line may reach below the bottom margin '
                             f'(y={self.MON_H - 40}); the last one reaches y={bands[-1][1]}')

    def test_windows_painted_0a0a0a(self):
        for name in ('both', 'wide_right'):
            r, lw, rw, limg, rimg = self.left_right(name)
            for side, img in (('left', limg), ('right', rimg)):
                with self.subTest(run=name, window=side):
                    self.visible(img, side + ' window')
                    # rows above the top margin, and the bottom half (in window rows)
                    for x0, y0, x1, y1 in ((0, 0, img.w, 36 - img.oy),
                                           (0, MON_HALF - img.oy, img.w, img.h)):
                        bad, example = img.off_colour(BG, x0, y0, x1, y1)
                        self.assertEqual(bad, 0,
                                         f'the {side} window should be painted #0a0a0a; in the '
                                         f'text-free area ({x0},{y0})-({x1},{y1}) {bad} pixels '
                                         f'are not, e.g. {example}')

    def test_LINES_same_for_both_panels(self):
        r = self.run_of('both')
        left, right = r.files['left_lines'], r.files['right_lines']
        self.assertTrue(left and right, f'both commands should have run. {r.diag()}')
        self.assertEqual(left[-1], right[-1],
                         'both windows are as tall as the monitor with the same margins, so both '
                         'commands should get the same $LINES')

    def test_LINES_is_how_many_lines_fit_desktop(self):
        r, _, _, limg, _ = self.left_right()
        bands = self.text_lines(limg, 'left window')
        self.assertEqual(len(bands), 8, f'expected the 8 printed lines, got {bands}')
        lo, hi = lines_that_fit(self.MON_H, 40, line_pitch(bands))
        values = r.files['left_lines']
        self.assertTrue(values, 'the left command should have run')
        for v in values:
            self.assertTrue(v.isdigit() and lo <= int(v) <= hi,
                            f'$LINES should be (monitor height {self.MON_H} - 2 x 40) / line '
                            f'height {line_pitch(bands):.1f}, rounded down = {lo}'
                            f'{"" if lo == hi else f" or {hi}"}; the command got {v!r}')

    # -- window manager hints: what a WM needs to keep the promises ------

    def each_window(self):
        r = self.run_of('both')
        for side, w in zip(('left', 'right'), r.windows):
            yield side, w

    def test_windows_stay_below_all_others(self):
        for side, w in self.each_window():
            with self.subTest(window=side):
                self.assertTrue(
                    '_NET_WM_STATE_BELOW' in w['state']
                    or '_NET_WM_WINDOW_TYPE_DESKTOP' in w['types'],
                    f'the {side} window should ask to stay below all others '
                    f'(_NET_WM_STATE_BELOW or window type DESKTOP); state {w["state"]}, '
                    f'types {w["types"]}')

    def test_windows_show_on_every_workspace(self):
        for side, w in self.each_window():
            with self.subTest(window=side):
                self.assertTrue(
                    w['override_redirect'] or w['desktop'] == 0xFFFFFFFF
                    or '_NET_WM_STATE_STICKY' in w['state']
                    or '_NET_WM_WINDOW_TYPE_DESKTOP' in w['types'],
                    f'the {side} window should be on every workspace (_NET_WM_DESKTOP '
                    f'0xFFFFFFFF, _NET_WM_STATE_STICKY or type DESKTOP); desktop '
                    f'{w["desktop"]}, state {w["state"]}, types {w["types"]}')

    def test_windows_never_take_focus(self):
        for side, w in self.each_window():
            with self.subTest(window=side):
                self.assertTrue(
                    w['override_redirect'] or w['input'] is False,
                    f'the {side} window should never take focus (WM_HINTS input = False); '
                    f'its input hint is {w["input"]}')

    def test_windows_not_in_taskbar(self):
        for side, w in self.each_window():
            with self.subTest(window=side):
                self.assertTrue(
                    w['override_redirect'] or '_NET_WM_STATE_SKIP_TASKBAR' in w['state']
                    or '_NET_WM_WINDOW_TYPE_DESKTOP' in w['types'],
                    f'the {side} window should stay out of the taskbar '
                    f'(_NET_WM_STATE_SKIP_TASKBAR or type DESKTOP); state {w["state"]}, '
                    f'types {w["types"]}')


@needs_xvfb
@unittest.skipUnless(XRANDR, 'xrandr is needed to give Xvfb several monitors')
class DesktopMonitorChoiceTests(DeskTestCase):
    """Monitors are RandR monitors defined with xrandr --setmonitor. README:
    X's primary monitor, else the first at least as wide as tall, else the
    first, in the order `xrandr --listmonitors` gives.

    Without a primary, X reports none (no '*' monitor, XRRGetOutputPrimary is
    None), although GDK still calls one monitor "primary" as its own fallback;
    these tests follow X, as README says."""

    PORTRAIT_A = ('A', '1600/400x2000/500+0+0', 'none')
    LANDSCAPE_B = ('B', '2560/600x1440/340+1600+0', 'screen')
    PORTRAIT_B = ('B', '1400/350x1800/450+1600+0', 'screen')
    CASES = {
        # name: (screen, monitors, expected monitors (primary, name, w, h, x, y), chosen)
        'primary_portrait': ((4160, 2000), [('*A',) + PORTRAIT_A[1:], LANDSCAPE_B],
                             [(True, 'A', 1600, 2000, 0, 0), (False, 'B', 2560, 1440, 1600, 0)],
                             0),
        'no_primary_first_portrait': ((4160, 2000), [PORTRAIT_A, LANDSCAPE_B],
                                      [(False, 'A', 1600, 2000, 0, 0),
                                       (False, 'B', 2560, 1440, 1600, 0)], 1),
        'no_primary_all_portrait': ((3000, 2000), [PORTRAIT_A, PORTRAIT_B],
                                    [(False, 'A', 1600, 2000, 0, 0),
                                     (False, 'B', 1400, 1800, 1600, 0)], 0),
        # listed right monitor first: list order, not position, decides
        'no_primary_list_order': ((5120, 1440),
                                  [('R', '2560/600x1440/340+2560+0', 'none'),
                                   ('L', '2560/600x1440/340+0+0', 'screen')],
                                  [(False, 'R', 2560, 1440, 2560, 0),
                                   (False, 'L', 2560, 1440, 0, 0)], 0),
    }

    @classmethod
    def setUpClass(cls):
        cls.batch = b = DeskBatch([
            DeskRun(name, left='printf "HH\\n"', screen=screen, monitors=mons)
            for name, (screen, mons, _, _) in cls.CASES.items()])
        cls.addClassCleanup(b.close)
        b.start()
        for r in b.runs.values():
            r.collect(b.t0 + STARTUP)
        b.close()

    def check_case(self, name):
        _, _, expected, chosen = self.CASES[name]
        r = self.batch[name]
        if r.xrandr != expected:
            self.skipTest(f'this Xvfb could not present the monitor layout: xrandr '
                          f'--listmonitors gave {r.xrandr}, wanted {expected}')
        r = self.run_of(name)
        mon = expected[chosen]
        w = r.windows[0]
        self.assertEqual((w['x'], w['y'], w['h']), (mon[4], mon[5], mon[3]),
                         f'the left window should be on monitor {mon[1]} at its left edge and as '
                         f'tall as it (x, y, height); xrandr --listmonitors order (primary, '
                         f'name, w, h, x, y): {expected}')

    def test_primary_monitor_is_used_even_if_portrait(self):
        self.check_case('primary_portrait')

    def test_without_primary_first_landscape_monitor_is_used(self):
        self.check_case('no_primary_first_portrait')

    def test_without_primary_or_landscape_first_monitor_is_used(self):
        self.check_case('no_primary_all_portrait')

    def test_first_landscape_monitor_follows_xrandr_list_order(self):
        self.check_case('no_primary_list_order')


# --------------------------------------------------------------------------
# the X helper process
# --------------------------------------------------------------------------

def _x_agent():
    """Runs on one Xvfb display; answers JSON requests on stdin."""
    import ctypes
    import ctypes.util
    import traceback
    from ctypes import (POINTER, byref, c_char_p, c_int, c_long, c_uint, c_ulong,
                        c_ushort, c_void_p)

    def reply(obj):
        sys.stdout.write(json.dumps(obj) + '\n')
        sys.stdout.flush()

    class XWindowAttributes(ctypes.Structure):
        _fields_ = [('x', c_int), ('y', c_int), ('width', c_int), ('height', c_int),
                    ('border_width', c_int), ('depth', c_int), ('visual', c_void_p),
                    ('root', c_ulong), ('class_', c_int), ('bit_gravity', c_int),
                    ('win_gravity', c_int), ('backing_store', c_int),
                    ('backing_planes', c_ulong), ('backing_pixel', c_ulong),
                    ('save_under', c_int), ('colormap', c_ulong), ('map_installed', c_int),
                    ('map_state', c_int), ('all_event_masks', c_long),
                    ('your_event_mask', c_long), ('do_not_propagate_mask', c_long),
                    ('override_redirect', c_int), ('screen', c_void_p)]

    class XClientMessageEvent(ctypes.Structure):
        _fields_ = [('type', c_int), ('serial', c_ulong), ('send_event', c_int),
                    ('display', c_void_p), ('window', c_ulong), ('message_type', c_ulong),
                    ('format', c_int), ('l', c_long * 5)]

    class XEvent(ctypes.Union):
        _fields_ = [('type', c_int), ('xclient', XClientMessageEvent), ('pad', c_long * 24)]

    ERRFUNC = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
    state = {}
    try:
        import gi
        gi.require_version('Gdk', '3.0')
        from gi.repository import Gdk
        gdisplay = Gdk.Display.get_default()
        if gdisplay is None:
            raise RuntimeError(f'Gdk could not open {os.environ.get("DISPLAY")!r}')
        X = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
        X.XOpenDisplay.restype = c_void_p
        X.XOpenDisplay.argtypes = [c_char_p]
        X.XDefaultRootWindow.restype = c_ulong
        X.XDefaultRootWindow.argtypes = [c_void_p]
        X.XSelectInput.argtypes = [c_void_p, c_ulong, c_long]
        X.XInternAtom.restype = c_ulong
        X.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        X.XGetAtomName.restype = c_void_p
        X.XGetAtomName.argtypes = [c_void_p, c_ulong]
        X.XFree.argtypes = [c_void_p]
        X.XQueryTree.argtypes = [c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong),
                                 POINTER(POINTER(c_ulong)), POINTER(c_uint)]
        X.XGetWindowAttributes.argtypes = [c_void_p, c_ulong, POINTER(XWindowAttributes)]
        X.XGetWindowProperty.argtypes = [c_void_p, c_ulong, c_ulong, c_long, c_long, c_int,
                                         c_ulong, POINTER(c_ulong), POINTER(c_int),
                                         POINTER(c_ulong), POINTER(c_ulong), POINTER(c_void_p)]
        X.XPending.argtypes = [c_void_p]
        X.XNextEvent.argtypes = [c_void_p, POINTER(XEvent)]
        X.XCreateSimpleWindow.restype = c_ulong
        X.XCreateSimpleWindow.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint,
                                          c_uint, c_ulong, c_ulong]
        X.XMapWindow.argtypes = [c_void_p, c_ulong]
        X.XSync.argtypes = [c_void_p, c_int]
        X.XSetWindowBackground.argtypes = [c_void_p, c_ulong, c_ulong]
        X.XClearWindow.argtypes = [c_void_p, c_ulong]
        X.XSetErrorHandler.restype = c_void_p
        X.XSetErrorHandler.argtypes = [ERRFUNC]
        xd = X.XOpenDisplay(os.environ['DISPLAY'].encode())
        if not xd:
            raise RuntimeError('Xlib could not open the display')
        state['errfunc'] = ERRFUNC(lambda d, e: 0)   # BadWindow etc. must not kill us
        X.XSetErrorHandler(state['errfunc'])
        root = X.XDefaultRootWindow(xd)
        X.XSelectInput(xd, root, 1 << 19)            # SubstructureNotifyMask: see client messages
        X.XSync(xd, 0)
        init_error = None
    except Exception as e:  # reported on every request
        init_error = f'{type(e).__name__}: {e}'

    own = set()
    atoms, names = {}, {}
    messages = {}   # window -> [(kind, ...)] client messages a window manager would act on

    def atom(name):
        if name not in atoms:
            atoms[name] = X.XInternAtom(xd, name.encode(), 0)
        return atoms[name]

    def atom_name(a):
        if not a:
            return None
        if a not in names:
            p = X.XGetAtomName(xd, a)
            if not p:
                return None
            names[a] = ctypes.string_at(p).decode('latin-1')
            X.XFree(p)
        return names[a]

    def prop(win, name):
        at, af, n, after, data = c_ulong(), c_int(), c_ulong(), c_ulong(), c_void_p()
        st = X.XGetWindowProperty(xd, win, atom(name), 0, 4096, 0, 0, byref(at), byref(af),
                                  byref(n), byref(after), byref(data))
        if st != 0 or not at.value:
            if data.value:
                X.XFree(data)
            return None
        try:
            if af.value == 32:
                arr = ctypes.cast(data, POINTER(c_ulong))
                return [arr[i] & 0xFFFFFFFF for i in range(n.value)]
            if af.value == 16:
                arr = ctypes.cast(data, POINTER(c_ushort))
                return [arr[i] for i in range(n.value)]
            return ctypes.string_at(data, n.value).decode('utf-8', 'replace')
        finally:
            X.XFree(data)

    def drain():
        ev = XEvent()
        while X.XPending(xd) > 0:
            X.XNextEvent(xd, byref(ev))
            if ev.type != 33 or ev.xclient.format != 32:    # ClientMessage
                continue
            c = ev.xclient
            kind = atom_name(c.message_type)
            if kind == '_NET_WM_STATE':
                messages.setdefault(c.window, []).append(
                    ('state', c.l[0], [atom_name(c.l[1] & 0xFFFFFFFF),
                                       atom_name(c.l[2] & 0xFFFFFFFF)]))
            elif kind == '_NET_WM_DESKTOP':
                messages.setdefault(c.window, []).append(('desktop', c.l[0] & 0xFFFFFFFF))

    def op_root_background(req):
        X.XSetWindowBackground(xd, root, req['pixel'])
        X.XClearWindow(xd, root)
        X.XSync(xd, 0)
        return {'ok': True}

    def op_window(req):
        w = X.XCreateSimpleWindow(xd, root, req['x'], req['y'], req['w'], req['h'], 0, 0,
                                  req['pixel'])
        X.XMapWindow(xd, w)
        X.XSync(xd, 0)
        own.add(w)
        return {'xid': w}

    def op_capture(req):
        x, y, w, h = req['x'], req['y'], req['w'], req['h']
        rootwin = gdisplay.get_default_screen().get_root_window()
        pb = Gdk.pixbuf_get_from_window(rootwin, x, y, w, h)
        if pb is None:
            raise RuntimeError('Gdk could not read the pixels')
        nch, rs, px = pb.get_n_channels(), pb.get_rowstride(), pb.get_pixels()
        if nch == 3 and rs == w * 3:
            raw = px
        else:
            out = bytearray(w * h * 3)
            for i in range(h):
                row = px[i * rs:i * rs + w * nch]
                for c in range(3):
                    out[i * w * 3 + c:(i + 1) * w * 3:3] = row[c::nch]
            raw = bytes(out)
        with open(req['path'], 'wb') as f:
            f.write(raw)
        return {'ok': True}

    def op_monitors(req):
        out = []
        for i in range(gdisplay.get_n_monitors()):
            m = gdisplay.get_monitor(i)
            g = m.get_geometry()
            out.append([g.x, g.y, g.width, g.height, bool(m.is_primary())])
        return {'monitors': out}

    def op_toplevels(req):
        drain()
        r, p, ch, n = c_ulong(), c_ulong(), POINTER(c_ulong)(), c_uint()
        if not X.XQueryTree(xd, root, byref(r), byref(p), byref(ch), byref(n)):
            return {'windows': []}
        kids = [ch[i] for i in range(n.value)]
        if n.value:
            X.XFree(ctypes.cast(ch, c_void_p))
        out = []
        for w in kids:
            if w in own:
                continue
            a = XWindowAttributes()
            if not X.XGetWindowAttributes(xd, w, byref(a)):
                continue
            if a.map_state != 2 or a.class_ == 2:          # IsViewable, not InputOnly
                continue
            st = prop(w, '_NET_WM_STATE')
            states = {atom_name(v) for v in st} if isinstance(st, list) else set()
            ty = prop(w, '_NET_WM_WINDOW_TYPE')
            types = [atom_name(v) for v in ty] if isinstance(ty, list) else []
            dk = prop(w, '_NET_WM_DESKTOP')
            desktop = dk[0] if isinstance(dk, list) and dk else None
            hints = prop(w, 'WM_HINTS')
            input_hint = None
            if isinstance(hints, list) and len(hints) >= 2 and hints[0] & 1:
                input_hint = bool(hints[1])
            for msg in messages.get(w, []):      # what a window manager would apply
                if msg[0] == 'desktop':
                    desktop = msg[1]
                    continue
                for s in msg[2]:
                    if not s:
                        continue
                    if msg[1] == 0 or (msg[1] == 2 and s in states):
                        states.discard(s)
                    else:
                        states.add(s)
            out.append({'id': w, 'x': a.x, 'y': a.y, 'w': a.width, 'h': a.height,
                        'override_redirect': bool(a.override_redirect),
                        'state': sorted(states), 'types': types, 'desktop': desktop,
                        'input': input_hint, 'name': prop(w, 'WM_NAME')})
        return {'windows': out}

    ops = {'hello': lambda req: {'ok': True}, 'root_background': op_root_background,
           'window': op_window, 'capture': op_capture, 'monitors': op_monitors,
           'toplevels': op_toplevels}
    for line in sys.stdin:
        req = json.loads(line)
        if req['op'] == 'quit':
            reply({'ok': True})
            break
        if init_error:
            reply({'error': init_error})
            continue
        try:
            reply(ops[req['op']](req))
        except Exception:
            reply({'error': traceback.format_exc()})
    return 0


if __name__ == '__main__':
    if sys.argv[1:2] == ['--x-agent']:
        sys.exit(_x_agent())
    unittest.main()
