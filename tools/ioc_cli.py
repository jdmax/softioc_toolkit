#!/usr/bin/env python3
"""
IOC Commander — interactive curses TUI for Epics IOC screen sessions.

    python tools/ioc_cli.py

Runs on the same machine as the IOC manager. IOCs are started, stopped and restarted
by writing to the manager's PVs; the manager is the only thing that creates or kills
IOC screen sessions. Commander only starts and stops the manager's own session.

Keys (main view):
    ↑ / ↓       Select IOC
    Enter       View PVs for selected IOC
    p           View all active PVs across all running IOCs
    s           Start selected IOC
    x           Stop  selected IOC
    r           Restart selected IOC
    l           View log for selected IOC
    a           Attach to screen session (returns on detach)
    S           Start ALL autostart IOCs
    X           Stop  ALL IOCs
    m           Start IOC manager
    M           Stop  IOC manager
    R           Restart IOC manager
    ?           Show help page
    q / Esc     Quit

Keys (log / PV view):
    ↑ / ↓       Scroll / select PV
    Enter       Set selected PV value (PV views only)
    f           Force refresh (PV view)
    q / Esc     Back to main view
"""

import asyncio
import curses
import os
import re
import shlex
import subprocess
import sys
import time

import aioca
import yaml

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))
from ioc_common import (PROJECT_ROOT, MANAGER_SCREEN, log_path, apply_ca_env,
                        screen_sessions, start_session, kill_session)

# A single event loop shared for all aioca calls.  asyncio.run() closes the
# loop after each call, which causes aioca's CA background threads to crash
# on cleanup (call_soon_threadsafe on a closed loop).
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

SETTINGS_FILE   = os.path.join(PROJECT_ROOT, 'settings.yaml')
REFRESH_SECS    = 2          # auto-refresh interval
LOG_TAIL        = 200        # max lines kept in log view

# Manager _control and all PV values
CMD_STOP, CMD_RUN, CMD_RESET = 0, 1, 2


# ── Settings / log helpers ─────────────────────────────────────────────────────
def load_settings():
    with open(SETTINGS_FILE) as f:
        return yaml.safe_load(f)

def ioc_names(settings):
    return [k for k in settings if k != 'general']

def read_log_lines(path, n):
    """Return up to n lines from the end of a log file."""
    if not os.path.exists(path):
        return ['(no log file)']
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            buf, pos = b'', f.tell()
            lines_found = 0
            while pos > 0 and lines_found < n:
                chunk = min(1024, pos)
                pos  -= chunk
                f.seek(pos)
                buf   = f.read(chunk) + buf
                lines_found = buf.count(b'\n')
        lines = buf.decode('utf-8', errors='replace').splitlines()
        return lines[-n:] if lines else ['(empty)']
    except OSError:
        return ['(unreadable)']


# ── IOC commands, through the IOC manager ──────────────────────────────────────
def manager_command(prefix, pv_suffix, command, description):
    """Write a Stop/Run/Reset command to one of the manager's PVs. Returns a status string."""
    if MANAGER_SCREEN not in screen_sessions():
        return f'Cannot {description}: IOC manager is not running (m to start it)'
    pv_name = f'{prefix}:MAN:{pv_suffix}'
    try:
        _loop.run_until_complete(aioca.caput(pv_name, command, timeout=3.0))
        return f'Requested: {description}'
    except Exception as e:
        return f'Cannot {description}: IOC manager did not answer ({e})'


# ── IOC manager session ────────────────────────────────────────────────────────
def start_manager(settings):
    if MANAGER_SCREEN in screen_sessions():
        return 'manager: already running'
    command = shlex.join([sys.executable, 'ioc_manager.py'])
    if not start_session(MANAGER_SCREEN, command, log_path(settings, MANAGER_SCREEN)):
        return 'manager: could not create screen session'
    return 'manager: started'

def stop_manager():
    if MANAGER_SCREEN not in screen_sessions():
        return 'manager: not running'
    if not kill_session(MANAGER_SCREEN):
        return 'manager: screen session did not stop'
    return 'manager: stopped (IOCs keep running)'

def restart_manager(settings):
    result = stop_manager()
    if 'did not stop' in result:
        return result
    return start_manager(settings)


# ── Colour pair indices ────────────────────────────────────────────────────────
C_NORMAL       = 0
C_HEADER       = 1
C_RUNNING      = 2
C_STOPPED      = 3
C_SELECTED     = 4
C_STATUS       = 5
C_TITLE        = 6
C_DIM          = 7
C_READONLY     = 8
C_ALARM_MINOR  = 9
C_ALARM_MAJOR  = 10
C_ALARM_INVALID = 11
C_WRITABLE     = 12

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER,   curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(C_RUNNING,  curses.COLOR_GREEN,  -1)
    curses.init_pair(C_STOPPED,  curses.COLOR_RED,    -1)
    curses.init_pair(C_SELECTED, curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(C_STATUS,   curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(C_TITLE,    curses.COLOR_BLACK,  curses.COLOR_BLUE)
    curses.init_pair(C_DIM,      curses.COLOR_YELLOW, -1)
    # Read-only PVs in plain white, writable PVs in yellow — explicit colours
    # instead of a dim/grey attribute, which renders too dark on many terminals.
    curses.init_pair(C_READONLY,      curses.COLOR_WHITE,   -1)
    curses.init_pair(C_WRITABLE,      curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_ALARM_MINOR,   curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_ALARM_MAJOR,   curses.COLOR_RED,     -1)
    curses.init_pair(C_ALARM_INVALID, curses.COLOR_MAGENTA, -1)


# EPICS alarm severity: 0=NO_ALARM, 1=MINOR, 2=MAJOR, 3=INVALID
_SEVERITY_LABELS = {1: 'MINOR', 2: 'MAJOR', 3: 'INVALID'}

def severity_label(sev):
    return _SEVERITY_LABELS.get(sev, '')

def severity_attr(sev):
    if sev == 1:
        return curses.color_pair(C_ALARM_MINOR) | curses.A_BOLD
    if sev == 2:
        return curses.color_pair(C_ALARM_MAJOR) | curses.A_BOLD
    if sev == 3:
        return curses.color_pair(C_ALARM_INVALID) | curses.A_BOLD
    return 0


# ── Drawing helpers ────────────────────────────────────────────────────────────
def safe_addstr(win, y, x, text, attr=0):
    """addstr that silently ignores out-of-bounds writes."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        win.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass

def fill_row(win, y, attr):
    h, w = win.getmaxyx()
    if 0 <= y < h:
        try:
            win.addstr(y, 0, ' ' * (w - 1), attr)
        except curses.error:
            pass

def draw_title(win, text):
    fill_row(win, 0, curses.color_pair(C_TITLE) | curses.A_BOLD)
    safe_addstr(win, 0, 2, text, curses.color_pair(C_TITLE) | curses.A_BOLD)

def draw_status(win, text):
    h, _ = win.getmaxyx()
    fill_row(win, h - 1, curses.color_pair(C_STATUS))
    safe_addstr(win, h - 1, 1, text, curses.color_pair(C_STATUS))

def draw_help(win, keys):
    """Draw a key-hint bar on the second-to-last row."""
    h, w = win.getmaxyx()
    row  = h - 2
    fill_row(win, row, curses.color_pair(C_HEADER))
    x = 1
    for key, desc in keys:
        hint = f' {key}:{desc} '
        if x + len(hint) >= w - 1:
            break
        safe_addstr(win, row, x, hint, curses.color_pair(C_HEADER))
        x += len(hint) + 1


# ── Main list view ─────────────────────────────────────────────────────────────
def fetch_main_state(settings, names, prefix):
    """
    Collect everything the main view shows. IOC status, heartbeat and message come from
    the IOC manager's PVs; without a manager, status falls back to the screen sessions.
    """
    sessions = screen_sessions()
    state = {'manager': 'stopped', 'iocs': {}}
    for name in names:
        state['iocs'][name] = {
            'status': 'running' if name in sessions else 'stopped',
            'hb':     '',
            'msg':    '',
            'log':    read_log_lines(log_path(settings, name), 1)[0].strip(),
        }
    if MANAGER_SCREEN not in sessions:
        return state

    pvs = []
    for name in names:
        pvs += [f'{prefix}:MAN:{name}_status', f'{prefix}:MAN:{name}_hb', f'{prefix}:MAN:{name}_msg']
    try:
        results = _loop.run_until_complete(
            aioca.caget(pvs, datatype=aioca.DBR_STRING, timeout=1.0, throw=False))
    except Exception:
        results = []
    if all(isinstance(r, aioca.CANothing) for r in results):
        state['manager'] = 'not responding'
        return state

    state['manager'] = 'running'
    for i, name in enumerate(names):
        ioc = state['iocs'][name]
        for key, value in zip(('status', 'hb', 'msg'), results[3 * i:3 * i + 3]):
            if not isinstance(value, aioca.CANothing):
                ioc[key] = str(value)
    return state

def status_attr(status):
    s = status.lower()
    if s == 'running':
        return curses.color_pair(C_RUNNING)
    if s == 'stopped':
        return curses.color_pair(C_STOPPED)
    if s == 'stale':
        return curses.color_pair(C_ALARM_MINOR) | curses.A_BOLD
    if s == 'failed':
        return curses.color_pair(C_ALARM_MAJOR) | curses.A_BOLD
    return curses.color_pair(C_DIM)     # starting, stopping

def draw_main(win, settings, names, selected, status_msg, prefix, state):
    h, w = win.getmaxyx()
    win.erase()

    mgr_label = f'MANAGER: {state["manager"]}'
    title    = f' {prefix} IOC Monitor '
    mgr_attr = (curses.color_pair(C_RUNNING) if state['manager'] == 'running'
                else curses.color_pair(C_STOPPED))
    draw_title(win, title)
    safe_addstr(win, 0, w - len(mgr_label) - 2, mgr_label,
                curses.color_pair(C_TITLE) | curses.A_BOLD | mgr_attr)

    # Column widths
    col_name = max(len(n) for n in names) + 2
    col_st   = 10
    col_hb   = 7
    col_auto = 6
    col_log  = max(w - col_name - col_st - col_hb - col_auto - 4, 10)

    # Header row
    hdr = (f'{"IOC":<{col_name}}'
           f'{"STATUS":<{col_st}}'
           f'{"HB":<{col_hb}}'
           f'{"AUTO":<{col_auto}}'
           f'{"LAST LOG LINE":<{col_log}}')
    fill_row(win, 1, curses.color_pair(C_HEADER) | curses.A_BOLD)
    safe_addstr(win, 1, 1, hdr[:w - 2],
                curses.color_pair(C_HEADER) | curses.A_BOLD)

    # IOC rows (leave 3 rows: title + header + help + status)
    list_rows = h - 4
    # Scroll offset so selected is always visible
    offset = max(0, selected - list_rows + 1)

    for i, name in enumerate(names):
        row = i - offset + 2   # display row
        if row < 2 or row >= h - 2:
            continue

        ioc       = state['iocs'][name]
        autostart = settings[name].get('autostart', False)

        run_label  = ioc['status']
        hb_label   = '' if run_label.lower() == 'stopped' else ioc['hb']
        auto_label = 'yes' if autostart else 'no'

        last_line = ioc['log']
        if len(last_line) > col_log:
            last_line = last_line[:col_log - 3] + '...'

        line = (f'{name:<{col_name}}'
                f'{run_label:<{col_st}}'
                f'{hb_label:<{col_hb}}'
                f'{auto_label:<{col_auto}}'
                f'{last_line:<{col_log}}')

        if i == selected:
            fill_row(win, row, curses.color_pair(C_SELECTED) | curses.A_BOLD)
            safe_addstr(win, row, 1, line[:w - 2],
                        curses.color_pair(C_SELECTED) | curses.A_BOLD)
        else:
            win.move(row, 0)
            auto_attr = (curses.color_pair(C_RUNNING) if autostart
                         else curses.color_pair(C_DIM))
            x = 1
            safe_addstr(win, row, x, f'{name:<{col_name}}')
            x += col_name
            safe_addstr(win, row, x, f'{run_label:<{col_st}}', status_attr(run_label))
            x += col_st
            safe_addstr(win, row, x, f'{hb_label:<{col_hb}}')
            x += col_hb
            safe_addstr(win, row, x, f'{auto_label:<{col_auto}}', auto_attr)
            x += col_auto
            safe_addstr(win, row, x, last_line)

    # With nothing else to report, show the manager's message for the selected IOC
    selected_msg = state['iocs'][names[selected]]['msg']
    if status_msg == 'Ready' and selected_msg:
        status_msg = f'{names[selected]}: {selected_msg}'

    draw_help(win, [('s','start'),('x','stop'),('r','restart'),('l','logs'),('a','attach'),
                    ('p','all PVs'),('m','mgr start'),('M','mgr stop'),('?','help'),('q','quit')])
    draw_status(win, f'  {status_msg}   (auto-refresh {REFRESH_SECS}s)')
    win.refresh()


# ── Help view ──────────────────────────────────────────────────────────────────
HELP_LINES = [
    ('Main view', [
        ('↑ / ↓',       'Select IOC'),
        ('Enter',        'View live PV values for selected IOC'),
        ('p',            'View all active PVs across all running IOCs'),
        ('s',            'Start selected IOC (via IOC manager)'),
        ('x',            'Stop selected IOC (via IOC manager)'),
        ('r',            'Restart selected IOC (via IOC manager)'),
        ('l',            'View log for selected IOC'),
        ('a',            'Attach to screen session (Ctrl+A D to detach)'),
        ('S',            'Start ALL autostart IOCs (via IOC manager)'),
        ('X',            'Stop ALL IOCs (via IOC manager, asks first)'),
        ('m',            'Start IOC manager'),
        ('M',            'Stop IOC manager (asks first, IOCs keep running)'),
        ('R',            'Restart IOC manager (asks first, IOCs keep running)'),
        ('?',            'Show this help page'),
        ('q / Esc',      'Quit'),
    ]),
    ('Log view', [
        ('↑ / ↓',        'Scroll one line'),
        ('PgUp / PgDn',  'Scroll one page'),
        ('l / q / Esc',  'Return to main view'),
    ]),
    ('PV view (single IOC)', [
        ('↑ / ↓',        'Move cursor / scroll'),
        ('Enter',        'Set selected PV value'),
        ('PgUp / PgDn',  'Scroll one page'),
        ('f',            'Force immediate refresh'),
        ('q / Esc',       'Return to main view'),
    ]),
    ('All PVs view', [
        ('↑ / ↓',        'Move cursor / scroll (skips IOC headers)'),
        ('Enter',        'Set selected PV value'),
        ('PgUp / PgDn',  'Scroll one page'),
        ('f',            'Force immediate refresh'),
        ('q / Esc',       'Return to main view'),
    ]),
]

def help_view(stdscr, prefix):
    """Full-screen key reference. Returns when user exits."""
    curses.curs_set(0)
    scroll = 0

    # Build flat list of display lines
    content = []
    for section, entries in HELP_LINES:
        content.append(('header', section))
        for key, desc in entries:
            content.append(('entry', key, desc))
        content.append(('blank',))

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_title(stdscr, f' {prefix} — Help ')

        view_rows  = h - 4
        max_scroll = max(0, len(content) - view_rows)
        scroll     = min(scroll, max_scroll)

        col_key = 18
        for i, item in enumerate(content[scroll:scroll + view_rows]):
            row = i + 1
            if item[0] == 'header':
                safe_addstr(stdscr, row, 2, item[1],
                            curses.color_pair(C_HEADER) | curses.A_BOLD)
            elif item[0] == 'entry':
                _, key, desc = item
                safe_addstr(stdscr, row, 4,            f'{key:<{col_key}}',
                            curses.color_pair(C_DIM))
                safe_addstr(stdscr, row, 4 + col_key,  desc[:w - col_key - 6])

        draw_help(stdscr, [('↑↓','scroll'),('q/Esc','back')])
        draw_status(stdscr, f'  Key reference  —  {len(content)} lines')
        stdscr.refresh()

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key in (ord('q'), ord('?'), 27):
            return
        elif key == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            scroll = min(max_scroll, scroll + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - view_rows)
        elif key == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + view_rows)


# ── Log view ───────────────────────────────────────────────────────────────────
def log_view(stdscr, settings, name, prefix):
    """Full-screen scrollable log viewer for one IOC. Returns when user exits."""
    curses.curs_set(0)
    scroll = 0
    status = ''

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_title(stdscr, f' {prefix} — Log: {name} ')

        lines     = read_log_lines(log_path(settings, name), LOG_TAIL)
        view_rows = h - 4          # title + help + status
        max_scroll = max(0, len(lines) - view_rows)
        scroll     = min(scroll, max_scroll)

        for i, line in enumerate(lines[scroll:scroll + view_rows]):
            safe_addstr(stdscr, i + 1, 1, line[:w - 2])

        draw_help(stdscr, [('↑↓','scroll'),('l/q/Esc','back')])
        draw_status(stdscr, f'  {name}  lines {scroll+1}–'
                             f'{min(scroll+view_rows, len(lines))}'
                             f'/{len(lines)}  {status}')
        stdscr.refresh()

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key in (ord('q'), ord('l'), 27):   # 27 = Esc
            return
        elif key == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            scroll = min(max_scroll, scroll + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - (h - 4))
        elif key == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + (h - 4))
        # timeout (key == -1): just refresh


# ── Curses suspend/resume helper ──────────────────────────────────────────────
def suspended(stdscr, fn):
    """Run fn() with curses suspended, then restore the display."""
    curses.endwin()
    result = fn()
    stdscr.clear()
    stdscr.refresh()
    curses.doupdate()
    return result


# ── Confirmation popup ─────────────────────────────────────────────────────────
def confirm_popup(stdscr, lines, confirm_label='Enter to confirm', cancel_label='Esc to cancel'):
    """
    Show a centred popup with the given lines of text.
    Returns True if the user pressed Enter, False if Esc/q.
    """
    h, w      = stdscr.getmaxyx()
    box_w     = min(max(len(l) for l in lines) + 6, w - 4)
    hint      = f'  {confirm_label}   {cancel_label}  '
    box_w     = max(box_w, len(hint) + 2)
    box_h     = len(lines) + 4          # top border + blank + lines + hint + bottom border
    by        = (h - box_h) // 2
    bx        = (w - box_w) // 2

    popup = curses.newwin(box_h, box_w, by, bx)
    popup.attron(curses.color_pair(C_SELECTED))
    popup.box()

    for i, line in enumerate(lines):
        safe_addstr(popup, i + 1, 2, line[:box_w - 4], curses.color_pair(C_SELECTED) | curses.A_BOLD)

    # Hint bar at the bottom inside the box
    safe_addstr(popup, box_h - 2, (box_w - len(hint)) // 2, hint,
                curses.color_pair(C_HEADER))

    popup.attroff(curses.color_pair(C_SELECTED))
    popup.refresh()

    curses.curs_set(0)
    popup.timeout(-1)          # block until keypress
    while True:
        key = popup.getch()
        if key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            return True
        if key in (27, ord('q')):                    # Esc or q
            return False


def input_popup(stdscr, prompt, initial=''):
    """
    Centered single-line text-input popup.
    Returns (confirmed: bool, text: str). Enter confirms, Esc cancels.
    """
    h, w  = stdscr.getmaxyx()
    box_w = min(max(len(prompt) + 6, 50), w - 6)
    box_h = 5
    by    = (h - box_h) // 2
    bx    = (w - box_w) // 2

    popup = curses.newwin(box_h, box_w, by, bx)
    popup.attron(curses.color_pair(C_SELECTED))
    popup.box()
    safe_addstr(popup, 1, 2, prompt[:box_w - 4],
                curses.color_pair(C_SELECTED) | curses.A_BOLD)
    hint = ' Enter:confirm   Esc:cancel '
    safe_addstr(popup, box_h - 2, (box_w - len(hint)) // 2, hint,
                curses.color_pair(C_HEADER))
    popup.attroff(curses.color_pair(C_SELECTED))

    curses.curs_set(1)
    text    = list(initial)
    field_x = 2
    field_w = box_w - 4

    while True:
        visible = ''.join(text)
        if len(visible) > field_w:
            visible = visible[len(visible) - field_w:]
        safe_addstr(popup, 2, field_x, ' ' * field_w)
        safe_addstr(popup, 2, field_x, visible)
        try:
            popup.move(2, field_x + len(visible))
        except curses.error:
            pass
        popup.refresh()

        key = popup.getch()
        if key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            curses.curs_set(0)
            return True, ''.join(text)
        elif key == 27:                               # Esc
            curses.curs_set(0)
            return False, ''
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if text:
                text.pop()
        elif 32 <= key <= 126:
            text.append(chr(key))


# ── Attach helper ──────────────────────────────────────────────────────────────
def do_attach(stdscr, name):
    """Show confirmation popup, then suspend curses and attach to screen session."""
    confirmed = confirm_popup(
        stdscr,
        lines=[
            f'Attaching to screen session: {name}',
            '',
            'To detach and return to the IOC monitor,',
            'press  Ctrl+A  then  D',
        ],
        confirm_label='Enter to attach',
        cancel_label='Esc to cancel',
    )
    if not confirmed:
        return
    suspended(stdscr, lambda: subprocess.run(['screen', '-r', name]))


# ── PV helpers ────────────────────────────────────────────────────────────────
def pv_names_from_log(path, prefix):
    """Extract PV names from an IOC log file (same approach as ioc_manager)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, errors='replace') as f:
            content = f.read()
        found = re.findall(rf'({re.escape(prefix)}[^\s]+)', content)
        # Deduplicate while preserving order
        seen, out = set(), []
        for pv in found:
            if pv not in seen:
                seen.add(pv)
                out.append(pv)
        return out
    except OSError:
        return []

def fetch_pv_values(pv_names):
    """Return ({pv_name: value_str}, {pv_name: severity_int}) using aioca.caget.

    Severity is fetched in the same round trip via FORMAT_TIME (0=NO_ALARM,
    1=MINOR, 2=MAJOR, 3=INVALID); disconnected/error PVs get severity None.
    """
    if not pv_names:
        return {}, {}

    async def _fetch():
        results = await aioca.caget(
            pv_names, format=aioca.FORMAT_TIME, timeout=2.0, throw=False
        )
        vals, sevs = {}, {}
        for name, val in zip(pv_names, results):
            if isinstance(val, aioca.CANothing):
                vals[name] = '(disconnected)'
                sevs[name] = None
            else:
                if hasattr(val, '__len__') and not isinstance(val, str):
                    vals[name] = str(list(val))
                else:
                    vals[name] = str(val)
                sevs[name] = int(getattr(val, 'severity', 0))
        return vals, sevs

    try:
        return _loop.run_until_complete(_fetch())
    except Exception as e:
        return ({n: f'(error: {e})' for n in pv_names},
                {n: None for n in pv_names})

# Record types where external CA writes are meaningful (output/setpoint records).
# Input records (ai, bi, longin, etc.) are treated as read-only: the IOC
# continuously overwrites their value, so a manual caput is pointless or harmful.
_WRITABLE_RTYPES = frozenset({
    'ao', 'bo', 'longout', 'mbbo', 'mbboDirect', 'stringout',
    'calcout', 'waveform', 'asyn',
})

_rtyp_cache: dict = {}   # {pv_name: str|None}  — None means unknown (treat as writable)

def fetch_missing_rtypes(pv_names):
    """Fetch and cache .RTYP for any pv_names not already in _rtyp_cache."""
    needed = [p for p in pv_names if p not in _rtyp_cache]
    if not needed:
        return

    async def _get():
        results = await aioca.caget(
            [p + '.RTYP' for p in needed], timeout=2.0, throw=False
        )
        for name, val in zip(needed, results):
            if isinstance(val, aioca.CANothing):
                _rtyp_cache[name] = None
            else:
                _rtyp_cache[name] = str(val).strip()

    try:
        _loop.run_until_complete(_get())
    except Exception:
        for name in needed:
            _rtyp_cache[name] = None

def is_pv_writable(pv_name):
    """Return True if the PV's record type suggests it accepts external writes."""
    rtyp = _rtyp_cache.get(pv_name)
    return rtyp is None or rtyp in _WRITABLE_RTYPES


def caput_pv(pv_name, value_str):
    """Write value_str to pv_name via caput. Returns a status string."""
    async def _put():
        try:
            val = int(value_str)
        except ValueError:
            try:
                val = float(value_str)
            except ValueError:
                val = value_str
        await aioca.caput(pv_name, val, timeout=3.0)

    try:
        _loop.run_until_complete(_put())
        return f'Set {pv_name} = {value_str}'
    except Exception as e:
        return f'caput {pv_name} failed: {e}'


# ── PV view ────────────────────────────────────────────────────────────────────
def pv_view(stdscr, settings, name, prefix):
    """Full-screen view of all PVs for one IOC with live values."""
    curses.curs_set(0)
    scroll     = 0
    cursor     = 0
    pv_vals    = {}
    pv_sevs    = {}
    status     = 'Fetching…'
    last_fetch = 0.0

    lp      = log_path(settings, name)
    pv_list = pv_names_from_log(lp, prefix)

    while True:
        now = time.monotonic()
        if now - last_fetch >= REFRESH_SECS:
            pv_list    = [p for p in pv_names_from_log(lp, prefix) if not p.endswith('_time')]
            pv_vals, pv_sevs = fetch_pv_values(pv_list)
            fetch_missing_rtypes(pv_list)
            last_fetch = now
            ts         = time.strftime('%H:%M:%S')
            status     = f'Updated {ts}  ({len(pv_list)} PVs)' if pv_list else 'No PVs found in log'
            cursor     = min(cursor, max(0, len(pv_list) - 1))

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_title(stdscr, f' {prefix} — PVs: {name} ')

        if not pv_list:
            safe_addstr(stdscr, 2, 2, 'No PVs found. Is the IOC running?',
                        curses.color_pair(C_STOPPED))
        else:
            col_pv    = max(len(p) for p in pv_list) + 2
            col_alarm = 9
            col_val   = max(w - col_pv - col_alarm - 4, 10)

            # Column header
            fill_row(stdscr, 1, curses.color_pair(C_HEADER) | curses.A_BOLD)
            safe_addstr(stdscr, 1, 1,
                        f'{"PV":<{col_pv}}{"VALUE":<{col_val}}{"ALARM":<{col_alarm}}'[:w - 2],
                        curses.color_pair(C_HEADER) | curses.A_BOLD)

            view_rows  = h - 4
            max_scroll = max(0, len(pv_list) - view_rows)
            scroll     = min(scroll, max_scroll)

            for i, pv in enumerate(pv_list[scroll:scroll + view_rows]):
                abs_idx   = scroll + i
                row       = i + 2
                writable  = is_pv_writable(pv)
                val       = pv_vals.get(pv, '…')
                if len(val) > col_val:
                    val = val[:col_val - 3] + '...'
                sev           = pv_sevs.get(pv)
                alarm_display = f'{severity_label(sev):<{col_alarm}}'
                pv_display    = f'{pv:<{col_pv}}'
                if abs_idx == cursor:
                    fill_row(stdscr, row, curses.color_pair(C_SELECTED) | curses.A_BOLD)
                    safe_addstr(stdscr, row, 1,
                                (pv_display + val + alarm_display)[:w - 2],
                                curses.color_pair(C_SELECTED) | curses.A_BOLD)
                elif not writable:
                    ro_attr = curses.color_pair(C_READONLY)
                    safe_addstr(stdscr, row, 1, pv_display, ro_attr)
                    safe_addstr(stdscr, row, 1 + col_pv, val, ro_attr)
                    safe_addstr(stdscr, row, 1 + col_pv + col_val, alarm_display, severity_attr(sev))
                else:
                    val_attr = (curses.color_pair(C_STOPPED)
                                if 'disconnected' in val or 'error' in val
                                else curses.color_pair(C_RUNNING))
                    alarm_attr = severity_attr(sev)
                    safe_addstr(stdscr, row, 1, pv_display, curses.color_pair(C_WRITABLE))
                    safe_addstr(stdscr, row, 1 + col_pv, val, val_attr)
                    safe_addstr(stdscr, row, 1 + col_pv + col_val, alarm_display, alarm_attr)

        draw_help(stdscr, [('↑↓','select'),('Enter','set value'),('f','refresh'),('q/Esc','back')])
        draw_status(stdscr, f'  {status}')
        stdscr.refresh()

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key in (ord('q'), 27):
            return
        elif key == ord('f'):
            last_fetch = 0.0
        elif key == curses.KEY_UP and pv_list:
            cursor = max(0, cursor - 1)
            scroll = min(scroll, cursor)
        elif key == curses.KEY_DOWN and pv_list:
            cursor = min(len(pv_list) - 1, cursor + 1)
            scroll = max(scroll, cursor - (h - 5))
        elif key == curses.KEY_PPAGE and pv_list:
            cursor = max(0, cursor - (h - 4))
            scroll = min(scroll, cursor)
        elif key == curses.KEY_NPAGE and pv_list:
            cursor = min(len(pv_list) - 1, cursor + (h - 4))
            scroll = max(scroll, cursor - (h - 5))
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')) and pv_list:
            pv = pv_list[cursor]
            if not is_pv_writable(pv):
                rtyp   = _rtyp_cache.get(pv) or 'input'
                status = f'{pv} is read-only ({rtyp} record)'
            else:
                current = pv_vals.get(pv, '')
                if 'disconnected' in current or 'error' in current:
                    current = ''
                confirmed, new_val = input_popup(stdscr, f'Set {pv}', current)
                stdscr.clear()
                if confirmed and new_val != '':
                    status = caput_pv(pv, new_val)
                    last_fetch = 0.0


# ── All-IOCs PV view ──────────────────────────────────────────────────────────
def all_pvs_view(stdscr, settings, names, prefix):
    """Full-screen view of PVs from all currently running IOCs, grouped by IOC."""
    curses.curs_set(0)
    scroll     = 0
    cursor     = 0
    status     = 'Fetching…'
    last_fetch = 0.0
    entries    = []   # list of ('header', ioc_name) | ('pv', ioc_name, pv_name)
    pv_vals    = {}
    pv_sevs    = {}

    def _move_cursor(entries, current, direction):
        """Move cursor in direction (+1/-1), skipping header rows."""
        i = current + direction
        while 0 <= i < len(entries) and entries[i][0] == 'header':
            i += direction
        return i if 0 <= i < len(entries) else current

    while True:
        now = time.monotonic()
        if now - last_fetch >= REFRESH_SECS:
            sessions = screen_sessions()
            running = [n for n in names if n in sessions]
            ioc_pv_map = {}
            all_pvs = []
            for n in running:
                lp  = log_path(settings, n)
                pvs = [p for p in pv_names_from_log(lp, prefix) if not p.endswith('_time')]
                ioc_pv_map[n] = pvs
                all_pvs.extend(pvs)

            pv_vals, pv_sevs = fetch_pv_values(all_pvs)
            fetch_missing_rtypes(all_pvs)

            entries = []
            for n in running:
                entries.append(('header', n))
                for pv in ioc_pv_map.get(n, []):
                    entries.append(('pv', n, pv))

            last_fetch = now
            ts     = time.strftime('%H:%M:%S')
            status = (f'Updated {ts}  ({len(running)} IOCs, {len(all_pvs)} PVs)'
                      if running else 'No running IOCs found')
            # Keep cursor on a valid pv row after refresh
            cursor = min(cursor, max(0, len(entries) - 1))
            if entries and entries[cursor][0] == 'header':
                cursor = _move_cursor(entries, cursor, 1)

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_title(stdscr, f' {prefix} — All Active PVs ')

        if not entries:
            safe_addstr(stdscr, 2, 2, 'No running IOCs found.',
                        curses.color_pair(C_STOPPED))
        else:
            pv_names_only = [e[2] for e in entries if e[0] == 'pv']
            col_pv    = (max(len(p) for p in pv_names_only) + 2) if pv_names_only else 30
            col_alarm = 9
            col_val   = max(w - col_pv - col_alarm - 4, 10)

            fill_row(stdscr, 1, curses.color_pair(C_HEADER) | curses.A_BOLD)
            safe_addstr(stdscr, 1, 1,
                        f'{"PV":<{col_pv}}{"VALUE":<{col_val}}{"ALARM":<{col_alarm}}'[:w - 2],
                        curses.color_pair(C_HEADER) | curses.A_BOLD)

            view_rows  = h - 4
            max_scroll = max(0, len(entries) - view_rows)
            scroll     = min(scroll, max_scroll)

            for i, entry in enumerate(entries[scroll:scroll + view_rows]):
                abs_idx = scroll + i
                row     = i + 2
                if entry[0] == 'header':
                    label = f'  {entry[1]} '
                    fill_row(stdscr, row, curses.color_pair(C_TITLE) | curses.A_BOLD)
                    safe_addstr(stdscr, row, 1, label[:w - 2],
                                curses.color_pair(C_TITLE) | curses.A_BOLD)
                else:
                    _, _, pv = entry
                    writable  = is_pv_writable(pv)
                    val = pv_vals.get(pv, '…')
                    if len(val) > col_val:
                        val = val[:col_val - 3] + '...'
                    sev           = pv_sevs.get(pv)
                    alarm_display = f'{severity_label(sev):<{col_alarm}}'
                    if abs_idx == cursor:
                        fill_row(stdscr, row, curses.color_pair(C_SELECTED) | curses.A_BOLD)
                        safe_addstr(stdscr, row, 1,
                                    (f'{pv:<{col_pv}}' + val + alarm_display)[:w - 2],
                                    curses.color_pair(C_SELECTED) | curses.A_BOLD)
                    elif not writable:
                        ro_attr = curses.color_pair(C_READONLY)
                        safe_addstr(stdscr, row, 1, f'{pv:<{col_pv}}', ro_attr)
                        safe_addstr(stdscr, row, 1 + col_pv, val, ro_attr)
                        safe_addstr(stdscr, row, 1 + col_pv + col_val, alarm_display, severity_attr(sev))
                    else:
                        val_attr = (curses.color_pair(C_STOPPED)
                                    if 'disconnected' in val or 'error' in val
                                    else curses.color_pair(C_RUNNING))
                        alarm_attr = severity_attr(sev)
                        safe_addstr(stdscr, row, 1, f'{pv:<{col_pv}}', curses.color_pair(C_WRITABLE))
                        safe_addstr(stdscr, row, 1 + col_pv, val, val_attr)
                        safe_addstr(stdscr, row, 1 + col_pv + col_val, alarm_display, alarm_attr)

        draw_help(stdscr, [('↑↓','select'),('Enter','set value'),('PgUp/Dn','page'),('f','refresh'),('q/Esc','back')])
        draw_status(stdscr, f'  {status}')
        stdscr.refresh()

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key in (ord('q'), 27):
            return
        elif key == ord('f'):
            last_fetch = 0.0
        elif key == curses.KEY_UP and entries:
            cursor = _move_cursor(entries, cursor, -1)
            scroll = min(scroll, cursor)
        elif key == curses.KEY_DOWN and entries:
            cursor = _move_cursor(entries, cursor, 1)
            scroll = max(scroll, cursor - (h - 5))
        elif key == curses.KEY_PPAGE and entries:
            cursor = max(0, cursor - (h - 4))
            if entries[cursor][0] == 'header':
                cursor = _move_cursor(entries, cursor, 1)
            scroll = min(scroll, cursor)
        elif key == curses.KEY_NPAGE and entries:
            cursor = min(len(entries) - 1, cursor + (h - 4))
            if entries[cursor][0] == 'header':
                cursor = _move_cursor(entries, cursor, -1)
            scroll = max(scroll, cursor - (h - 5))
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')) and entries:
            if 0 <= cursor < len(entries) and entries[cursor][0] == 'pv':
                _, _, pv = entries[cursor]
                if not is_pv_writable(pv):
                    rtyp   = _rtyp_cache.get(pv) or 'input'
                    status = f'{pv} is read-only ({rtyp} record)'
                else:
                    current = pv_vals.get(pv, '')
                    if 'disconnected' in current or 'error' in current:
                        current = ''
                    confirmed, new_val = input_popup(stdscr, f'Set {pv}', current)
                    stdscr.clear()
                    if confirmed and new_val != '':
                        status = caput_pv(pv, new_val)
                        last_fetch = 0.0


# ── Main TUI loop ──────────────────────────────────────────────────────────────
def tui(stdscr):
    init_colors()
    curses.curs_set(0)
    settings = load_settings()
    names    = ioc_names(settings)
    prefix   = settings['general']['prefix']
    selected = 0
    status   = 'Ready'
    state    = None
    last_fetch = 0.0

    def confirmed(lines, confirm_label):
        ok = confirm_popup(stdscr, lines, confirm_label=confirm_label)
        stdscr.clear()
        return ok

    while True:
        if time.monotonic() - last_fetch >= REFRESH_SECS:
            state = fetch_main_state(settings, names, prefix)
            last_fetch = time.monotonic()
        draw_main(stdscr, settings, names, selected, status, prefix, state)

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key == -1:
            # Timeout — just refresh (status already cleared on next draw)
            status = 'Ready'
            continue

        name = names[selected]
        refresh = True      # most keys change something worth re-reading

        if key in (ord('q'), 27):
            break
        elif key == curses.KEY_UP:
            selected = max(0, selected - 1)
            refresh = False
        elif key == curses.KEY_DOWN:
            selected = min(len(names) - 1, selected + 1)
            refresh = False
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            pv_view(stdscr, settings, name, prefix)
            status = f'Returned from PVs: {name}'
        elif key == ord('p'):
            all_pvs_view(stdscr, settings, names, prefix)
            status = 'Returned from all PVs view'
        elif key == ord('s'):
            status = manager_command(prefix, f'{name}_control', CMD_RUN, f'start {name}')
        elif key == ord('x'):
            status = manager_command(prefix, f'{name}_control', CMD_STOP, f'stop {name}')
        elif key == ord('r'):
            status = manager_command(prefix, f'{name}_control', CMD_RESET, f'restart {name}')
        elif key == ord('l'):
            log_view(stdscr, settings, name, prefix)
            status = f'Returned from log: {name}'
        elif key == ord('a'):
            if name in screen_sessions():
                do_attach(stdscr, name)
                status = f'Detached from {name}'
            else:
                status = f'{name} is not running'
        elif key == ord('S'):
            status = manager_command(prefix, 'all', CMD_RUN, 'start all autostart IOCs')
        elif key == ord('X'):
            if confirmed(['Stop ALL IOCs?', '',
                          f'All {len(names)} IOCs in settings.yaml will be stopped,',
                          'including those without autostart.'], 'Enter to stop all'):
                status = manager_command(prefix, 'all', CMD_STOP, 'stop all IOCs')
            else:
                status = 'Stop all cancelled'
        elif key == ord('m'):
            status = start_manager(settings)
        elif key == ord('M'):
            if confirmed(['Stop the IOC manager?', '',
                          'IOCs keep running, but cannot be started, stopped',
                          'or monitored until the manager is started again.'], 'Enter to stop'):
                status = stop_manager()
            else:
                status = 'Stop manager cancelled'
        elif key == ord('R'):
            if confirmed(['Restart the IOC manager?', '', 'IOCs keep running.'], 'Enter to restart'):
                status = restart_manager(settings)
            else:
                status = 'Restart manager cancelled'
        elif key == ord('?'):
            help_view(stdscr, prefix)
            refresh = False

        if refresh:
            last_fetch = 0.0


def main():
    os.chdir(PROJECT_ROOT)
    apply_ca_env(load_settings())

    # Suppress CA library disconnect/connect noise (written at the C fd level)
    # so it doesn't corrupt the curses display.
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        curses.wrapper(tui)
    except KeyboardInterrupt:
        pass
    finally:
        os.dup2(saved_stderr, 2)
        os.close(saved_stderr)


if __name__ == '__main__':
    main()
