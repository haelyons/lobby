#!/usr/bin/env python3
"""Smoke test for ssh-status, written only from README.txt (section SSH-STATUS).

ssh-status runs once as the current (normal) user with $SSH_STATUS_OUT pointing
at a temporary file. Without root it may not see every session, so the test
checks the documented header and section layout, not what the sections list.

README's example block is indented by two spaces like every block in it; the
indentation is not part of the output (compare "  (none)", which the text
quotes with exactly two spaces, and the CLAUDE-SESSIONS example).
"""

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SSH_STATUS = REPO / 'ssh-status'

TITLES = [
    'INBOUND   (remote -> this host:22)',
    'OUTBOUND  (this host -> remote:22)',
    'TMUX      (sessions across all users)',
]
HEADER = re.compile(r'SSH SESSIONS  -  (?P<host>\S+)  -  '
                    r'(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<tz>\S+)')


class SshStatusSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='ssh-status-test-')
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.out = os.path.join(cls.tmp, 'snapshot')
        env = dict(os.environ, SSH_STATUS_OUT=cls.out)
        cls.before = datetime.now().replace(microsecond=0)
        cls.problem = None
        try:
            cls.proc = subprocess.run([str(SSH_STATUS)], env=env, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            cls.proc = None
            cls.problem = f'ssh-status did not run to completion: {e}'
        cls.after = datetime.now()
        try:
            with open(cls.out, encoding='utf-8') as f:
                cls.text = f.read()
        except FileNotFoundError:
            cls.text = None

    def lines(self):
        if self.problem:
            self.fail(self.problem)
        stderr = self.proc.stderr[-500:] if self.proc else ''
        self.assertIsNotNone(self.text, f'ssh-status should write its snapshot to '
                                        f'$SSH_STATUS_OUT ({self.out}); stderr: {stderr!r}')
        return self.text.splitlines()

    def test_writes_snapshot_to_SSH_STATUS_OUT(self):
        lines = self.lines()
        self.assertTrue(any(line.strip() for line in lines),
                        'the snapshot written to $SSH_STATUS_OUT should not be empty')

    def test_header_names_host_and_time(self):
        lines = self.lines()
        self.assertTrue(lines, 'the snapshot should start with a header line')
        m = HEADER.fullmatch(lines[0])
        self.assertIsNotNone(m, f'the first line should be "SSH SESSIONS  -  <hostname>  -  '
                                f'<YYYY-MM-DD HH:MM:SS TZ>"; it is {lines[0]!r}')
        host = socket.gethostname()
        names = {host, host.split('.')[0], socket.getfqdn()}
        self.assertIn(m['host'], names, f'the header should name this host ({host!r})')
        stamp = datetime.strptime(m['time'], '%Y-%m-%d %H:%M:%S')
        self.assertTrue(self.before.timestamp() - 120 <= stamp.timestamp()
                        <= self.after.timestamp() + 120,
                        f'the header time should be the local time of the snapshot (between '
                        f'{self.before} and {self.after}); it is {m["time"]} {m["tz"]}')

    def test_blank_line_after_header(self):
        lines = self.lines()
        self.assertGreaterEqual(len(lines), 2, 'the snapshot should have more than a header')
        self.assertEqual(lines[1], '', 'a blank line should follow the header')

    def test_sections_in_documented_order(self):
        lines = self.lines()
        found = [line for line in lines if line in TITLES]
        self.assertEqual(found, TITLES,
                         'the snapshot should have the sections INBOUND, OUTBOUND and TMUX, in '
                         'that order, each once, with the documented titles')

    def sections(self):
        """Lines after the header's blank line, split at blank lines; trailing
        blank lines at the very end are ignored."""
        lines = self.lines()
        rest = lines[2:]
        while rest and rest[-1] == '':
            rest.pop()
        groups = [[]]
        for line in rest:
            if line == '':
                groups.append([])
            else:
                groups[-1].append(line)
        return groups

    def test_one_blank_line_between_sections(self):
        groups = self.sections()
        self.assertEqual([g[0] if g else '' for g in groups], TITLES,
                         'after the header and its blank line, the three sections should '
                         'follow each other separated by exactly one blank line (no blank '
                         f'lines inside a section); blank-separated groups start with '
                         f'{[g[0] if g else "" for g in groups]}')

    def test_each_section_is_none_or_indented_rows(self):
        lines = self.lines()
        starts = [i for i, line in enumerate(lines) if line in TITLES]
        self.assertEqual(len(starts), 3, 'expected the three section titles')
        ends = starts[1:] + [len(lines)]
        for start, end in zip(starts, ends):
            title = lines[start]
            # blank lines separate sections; they are checked in the test above
            body = [line for line in lines[start + 1:end] if line != '']
            with self.subTest(section=title.split()[0]):
                self.assertTrue(body, f'section {title!r} should list rows or "  (none)"')
                if '  (none)' in body:
                    self.assertEqual(body, ['  (none)'],
                                     f'an empty section shows only "  (none)"; {title!r} has '
                                     f'{body!r}')
                for line in body:
                    self.assertRegex(line, r'^  \S',
                                     f'rows of section {title!r} should be indented by two '
                                     f'spaces like "  (none)"; got {line!r}')


if __name__ == '__main__':
    unittest.main()
