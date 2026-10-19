lobby — who is around on an X11 box: lock screen, desktop and terminal
======================================================================

Part of lore: small tools grown on one Linux box, made portable.

WHAT THIS IS
------------
lobby draws plain-text panels onto the xscreensaver lock screen and onto the
desktop, and prints the same information as tables in a terminal. A panel is
whatever a command prints, refreshed every few seconds.

  ssh-status        daemon: SSH sessions in and out, and tmux sessions,
                    written to /run/lobby-ssh
  claude-sessions   every Claude Code session, newest first; with --watch,
                    a daemon that keeps $XDG_RUNTIME_DIR/lobby-claude current
  lockin            the renderer: lock screen, or desktop with --desk
  lobby             everything at once, as tables for a terminal

The two daemons are the information layer. Panels and the terminal read what
they last wrote, so the screens are optional.

The look of the panels is fixed on purpose: one serif font, one colour, text
only.

REQUIREMENTS
------------
- python3 3.11+.
- lockin only: X11 and xscreensaver (a GNOME/Wayland lock screen cannot run
  this), PyGObject with GTK 3, pycairo:
    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 xscreensaver \
                     x11-xserver-utils
- Font "Crimson Text" (SIL OFL). Without it another serif is used.
- ssh-status runs as root: only root sees other users' sshd processes and
  tmux servers.
- Tests: Xvfb (sudo apt install xvfb).

INSTALL
-------
  install -m 0755 lockin lobby claude-sessions ~/.local/bin/
  install -D -m 0644 claude-sessions.service ~/.config/systemd/user/claude-sessions.service
  systemctl --user enable --now claude-sessions
  sudo install -m 0755 ssh-status /usr/local/bin/ssh-status
  sudo install -m 0644 ssh-status.service /etc/systemd/system/
  sudo systemctl enable --now ssh-status

Never symlink ssh-status into a folder you can write: it runs as root.

Lock screen, in ~/.xscreensaver (full paths; it runs through /bin/sh):

  mode:       one
  programs:   /home/me/.local/bin/lockin                                 \
                --left 'cat /run/lobby-ssh'                              \
                --right '/home/me/.local/bin/claude-sessions --tsv'      \
                2>>/home/me/.local/share/lockin.log                      \n

Desktop, ~/.config/autostart/lockin.desktop:

  [Desktop Entry]
  Type=Application
  Name=lockin
  Exec=/home/me/.local/bin/lockin --desk --left "cat /run/lobby-ssh" --right "/home/me/.local/bin/claude-sessions --tsv"

LOCKIN
------
  lockin [--desk] [--left CMD] [--right CMD]

Each CMD runs through /bin/sh -c. Its standard output is the panel's text.

Lock screen (default). xscreensaver starts one lockin per monitor and passes
the window id in $XSCREENSAVER_WINDOW (decimal, or hex with 0x). lockin
paints the whole window #0a0a0a, then:
  - draws the left panel 90 px from the left edge and 70 px from the top;
  - draws the right panel only if the window is at least as wide as it is
    tall, also 70 px from the top. Its text block ends 90 px from the right
    edge, but starts no closer than 90 px to the right end of the left
    panel's widest line. Lines in a block are left-aligned. The right
    command is never run for a window that is taller than wide.

Desktop (--desk). One window per given panel, on X's primary monitor (else
the first monitor at least as wide as tall, else the first), in the order
`xrandr --listmonitors` gives. Windows stay below all others, show on every
workspace, never take focus and are not in the taskbar. Both are as tall as
the monitor and painted #0a0a0a.
  - left: at the monitor's left edge, 1280 px wide, text 44 px from the
    left and 40 px from the top.
  - right: against the monitor's right edge, as wide as its widest line plus
    2 x 44 px, but never wider than the monitor minus 1280 px. Text 44 px
    from its left and 40 px from the top.

Panels, everywhere:
  - Text is Crimson Text 36, colour #dcdcdc.
  - A line containing a TAB is a row of cells. Cells line up in columns
    across all rows of the panel: each column starts 32 px after the end of
    the widest cell in the column before it. Lines without a TAB are drawn
    as they are.
  - A line that would reach into the right margin (as wide as the left
    one) is cut short with "…". Only whole lines that fit above the bottom
    margin are drawn: at most $LINES.
  - Each run gets $LINES: how many lines fit, which is (panel height minus
    twice the top margin) divided by the line height, rounded down.
  - The left command starts every 2 s, the right every 10 s, counted from
    the start of the previous run. A new run never starts while the
    previous one is still going.
  - If a run exits non-zero, the panel keeps the last good text. Before any
    good run it shows: (CMD failed: exit N)

LOBBY
-----
  lobby [ssh] [tmux] [claude] [--fresh]
  lobby --usage | --help | -h

Prints SSH sessions, tmux sessions and Claude sessions as tables sized to
the terminal. SSH and tmux come from /run/lobby-ssh as ssh-status wrote it
($SSH_STATUS_OUT overrides the path). Claude sessions come the way
claude-sessions gets them: from its daemon file when that is current, else
read now (see CLAUDE-SESSIONS). --fresh always reads them now. lobby must be
in the same folder as claude-sessions: it loads it.

The words ssh, tmux and claude each pick one table; none means all three.
Picked tables keep their usual order whatever the order of the words, and
the footer keeps only the parts about them: "ssh <age>" (or "no <path>")
goes with ssh and tmux, "claude <age>" (or "claude fresh") and "tmux since"
with claude. A repeated word counts once. --usage, --help and -h print a
short usage on standard output, a few lines naming each word, what it shows
and where it comes from, and exit 0 without printing tables. Any other
argument is an error (exit status not 0).

Example (invented sessions):

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

Lines, in order:
  - "lobby", this machine's host name (as uname -n prints it) and the local
    time as "Ddd D Mon HH:MM" (D without a leading zero), joined by three
    spaces, then a blank line.
  - The SSH table and the tmux table. Both are left out if /run/lobby-ssh
    cannot be read.
  - The Claude table. Left out if neither config folder exists.
  - The footer.
Each table is followed by one blank line.

A table is a header row of column names in capitals, then one row per item.
Every line of a table starts with two spaces. Cells are separated by two
spaces, every column is as wide as its widest cell (header included), and
trailing spaces are removed. An empty cell is shown as "-". The SSH or tmux
table with no rows is the single line "  <first column name>  (none)".

  SSH     Columns SSH REMOTE USER CWD. One row per connection in
          /run/lobby-ssh: inbound ("in") first, then outbound ("out"), each
          in the file's order. USER is "-" where the file says "?". CWD is
          the folder with ~ for $HOME. Rows equal in every cell are one row,
          at the place of the first, with " (N)" added to REMOTE.
  TMUX    Columns TMUX SESSION WINDOWS ATTACHED. One row per tmux session,
          in the file's order: user, session, window count, yes or no.
  CLAUDE  Columns <first section's command in capitals> NAME STATE TMUX
          CWD. Sections as in claude-sessions, MAIN first, in one table so
          the columns line up across sections. The first section's rows
          follow the header row; every later section starts with a blank
          line and a line with its command in capitals ("  CLAUDE-ALT").
          Cells are claude-sessions' row cells without their prefixes and
          leading spaces: time, name, state, tmux session, folder. A section
          with no sessions shows "  (none)". Column widths are taken over
          the rows shown, after fitting; section lines and "(none)" lines
          do not count.

Size: W columns and H lines are the terminal's size; $COLUMNS and $LINES
win when set; with no terminal, 80 and 24. Every line longer than W, the
first line and the footer included, is cut to W - 1 characters and "…". Claude rows are fitted so that the output has at
most H lines, with claude-sessions' rule for fitting to $LINES (ALT up to 5
rows, MAIN as many as fit in the rest, ALT then takes whatever is left), and
every section shows at least one line, even if the output gets longer.

Footer: two spaces and these parts, joined by three spaces:
  - "ssh <age>": the age of /run/lobby-ssh (its modification time), or
    "no <path>" with the path read if it cannot be read;
  - "claude <age>": the age of the daemon file, if it was used; else
    "claude fresh";
  - "tmux since <time>" if any tmux-resurrect snapshot exists, with the
    time claude-sessions' header shows.
An age is "<N>s ago" under a minute, "<N>m ago" under an hour, else
"<N>h ago", N rounded down.

CLAUDE-SESSIONS
---------------
  claude-sessions [-n N] [-r] [--tsv] [WORD...]
  claude-sessions --watch

Lists Claude Code sessions from two config folders, each its own section:

  MAIN   ~/.claude        sessions started with `claude`
  ALT    ~/.claude-alt    sessions started with `claude-alt`, i.e.
                          CLAUDE_CONFIG_DIR=~/.claude-alt claude

A config folder that does not exist gets no section.

  -n N      at most N rows per section; default 25, 0 means all
  -r        under each row, the command that resumes that session:
              "     cd <folder> && <claude or claude-alt> --resume <id>"
            The folder is written in full, with ~ for $HOME.
  --tsv     cells joined by one TAB instead of padding, for lockin. When
            $LINES is set, the output is fitted to that many lines and -n
            is ignored. Resume lines (-r) are not counted.
  WORD...   keep only rows whose name, auto title, last prompt, folder or
            session id contain every WORD, ignoring case. A grouped row is
            matched on the session it shows.
  --watch   run until killed, writing the daemon file at start and then
            every 10 s (below).
            claude-sessions.service runs this.

The daemon file, $XDG_RUNTIME_DIR/lobby-claude, holds every session, already
grouped, for the other modes and lobby to read; its format is internal.
--watch replaces it in one step (writes a temporary file in the same folder,
then renames it). Any other run uses it instead of reading the config
folders if it is younger than 30 s (modification time) and was written for
the same $HOME. The output is then what reading the folders gave when the
file was written. Without $XDG_RUNTIME_DIR there is no daemon file: --watch
exits with an error, other runs read the folders.

Example (invented sessions):

  CLAUDE SESSIONS  -  by last activity  -  tmux recorded since Wed 18:52

  MAIN      (claude -> ~/.claude)
    11:35       SITE-DEPLOY      idle   tmux=web   cwd=~/src/site
    10:02       notes (3)                          cwd=~/notes
    Tue 09:14   fix flaky test                     cwd=~/src/api

  ALT       (claude-alt -> ~/.claude-alt)
    (none)

Lines, in order:
  - Header: "CLAUDE SESSIONS  -  by last activity". If any tmux-resurrect
    snapshot exists it continues "  -  tmux recorded since " and the time
    in the oldest snapshot's file name, in the time format below.
  - A blank line, then the sections, MAIN first, one blank line between.
  - Section title: the label padded with spaces to 9 characters, a space,
    then "(<command> -> <config folder>)".
  - One row per session, newest first, or "  (none)" if there are none.

Row cells, in order:
  1. two spaces and the time of last activity: the transcript file's
     modification time, in local time.
       same day as now          HH:MM          11:35
       less than 6 x 24 h ago   Ddd HH:MM      Tue 09:14
       older                    D Mon          3 Oct
  2. name: the /rename name, else Claude's auto title, else the first 8
     characters of the session id. Over 32 characters: the first 31 and
     "…". Sessions in the same section with the same (full) name and
     folder are one row; " (N)" is added after it when N > 1. That row
     shows the running session if one is running, else the newest; its
     time is the newest's.
  3. state of a running session: its status, e.g. busy, idle, waiting, or
     "live" if it has none. Empty if not running.
  4. "tmux=<tmux session>" if the session ran inside tmux, else empty.
  5. "cwd=<folder>", with ~ for $HOME. If that is longer than 40
     characters it keeps its first part and its last two, joined by "/…/":
     ~/…/experiments/run_c (outside $HOME the first part is empty: /…/x/y)

Padded output: each column is as wide as its widest cell in the whole
output, columns are separated by three spaces, and trailing spaces are
removed. --tsv: cells joined by one TAB, empty cells kept.

Fitting to $LINES (--tsv): header, blank line, section titles and the blank
lines between sections count. ALT is given up to 5 rows, MAIN as many rows
as fit in the rest, and ALT then takes whatever is left. Every section shows
at least one line, even if that makes the output longer than $LINES.

Where the data comes from
  <config>/projects/<anything>/<session id>.jsonl
      Claude's transcript, one JSON object per line. Files in deeper
      folders (subagents) are ignored. A transcript with no /rename name, no
      auto title and no last prompt is not listed. Used lines:
        {"cwd": "/path", ...}                        the folder (first one)
        {"type": "custom-title", "customTitle": "…"}   /rename name
        {"type": "ai-title", "aiTitle": "…"}           auto title
        {"type": "last-prompt", "lastPrompt": "…"}     last prompt
      Later title and prompt lines replace earlier ones. If no line has a
      cwd, the folder is the <anything> folder name with "-" read as "/".
  <config>/sessions/<pid>.json
      Written by Claude while it runs: {"pid": 123, "sessionId": "…",
      "procStart": "<field 22 of /proc/<pid>/stat>", "status": "idle",
      "startedAt": <ms>, "tmux": "<session>:@<window>.%<pane>"}. "tmux" is
      only present inside tmux. The session counts as running if that pid
      is alive and its start time still equals procStart.
  ~/.local/share/tmux/resurrect/tmux_resurrect_YYYYMMDDTHHMMSS.txt
      tmux-resurrect snapshots, TAB-separated. On a line whose 1st field is
      "pane": 2nd = tmux session, 7th = pane title, 8th = ":" + folder,
      10th = pane command, 11th = ":" + full command. It is a Claude pane if
      the 10th or 11th field contains "claude". Claude titles its pane with
      a status symbol, a space, and the session name.
  ~/.local/state/claude-sessions/tmux.tsv
      claude-sessions' own memory, because Claude deletes sessions/<pid>.json
      when it exits. Each run that reads the config folders appends
      "<session id>\t<tmux session>\n" for ids seen in sessions/*.json that
      are new or newly inside tmux (empty tmux session = seen outside tmux).

The tmux cell, best evidence first:
  1. the tmux session recorded for this id in sessions/*.json (running or
     not) or tmux.tsv; the latest one recorded wins;
  2. else the tmux session of a resurrect Claude pane whose title, without
     the symbol, equals this session's /rename name or auto title, and
     whose folder equals this session's folder; the newest snapshot wins;
  3. else empty. Before the first snapshot there is no record, which is
     why the header says since when.

SSH-STATUS
----------
  ssh-status

Writes a snapshot to /run/lobby-ssh ($SSH_STATUS_OUT overrides the path),
replacing the file in one step, mode 0644:

  SSH SESSIONS  -  <hostname>  -  <YYYY-MM-DD HH:MM:SS TZ>

  INBOUND   (remote -> this host:22)
    <ip>   user=<user>   cwd=<folder of the session's shell>

  OUTBOUND  (this host -> remote:22)
    <ip>   user=<user>

  TMUX      (sessions across all users)
    <user>   <session>   <N> window(s), attached|detached

with "  (none)" for an empty section and a blank line between sections.
Cells are separated by one or more spaces; only the folder and the tmux
session name can contain spaces. The <user> of a connection is "?" when
unknown; a tmux <user> is "uid<N>" when the uid has no name. "cwd=…" is
left out when no shell was found. ssh-status.service reruns it every 3 s.

PRIVACY
-------
The lock screen shows IP addresses, user names, folders and session names
to anyone in front of the screen. Choose panels with that in mind.

TESTS
-----
  python3 -m unittest discover -s tests -t .

Tests use made-up data in temporary folders and their own Xvfb displays.
lockin tests are skipped when Xvfb is not on PATH.

LICENSE
-------
MIT, see LICENSE.
