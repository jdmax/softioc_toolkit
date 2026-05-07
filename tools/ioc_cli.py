#!/usr/bin/env python3
"""
IOC Commander — interactive curses TUI for Epics IOC screen sessions.

    python tools/ioc_cli.py

Keys (main view):
    ↑ / ↓       Select IOC
    Enter       View PVs for selected IOC
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
    ↑ / ↓       Scroll
    f           Force refresh (PV view)
    q / Esc     Back to main view
"""

import asyncio
import curses
import os
import re
import subprocess
import sys
import time

import aioca
import yaml
from screenutils import Screen

# ── Paths ──────────────────────────────────────────────────────────────────────
SETTINGS_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'settings.yaml')
)
PROJECT_ROOT = os.path.dirname(SETTINGS_FILE)

REFRESH_SECS    = 2          # auto-refresh interval
LOG_TAIL        = 200        # max lines kept in log view
MANAGER_SCREEN  = 'ioc-manager'


# ── Settings / log helpers ─────────────────────────────────────────────────────
def load_settings():
    with open(SETTINGS_FILE) as f:
        return yaml.safe_load(f)

def ioc_names(settings):
    return [k for k in settings if k != 'general']

def log_path(settings, name):
    log_dir = settings['general']['log_dir']
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(PROJECT_ROOT, log_dir)
    return os.path.normpath(os.path.join(log_dir, name))

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


# ── Screen / IOC actions ───────────────────────────────────────────────────────
def ioc_running(name):
    return Screen(name).exists

def start_ioc(settings, name):
    if ioc_running(name):
        return f'{name}: already running'
    lp = log_path(settings, name)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    subprocess.run(['screen', '-dmS', name, 'bash'], check=False)
    time.sleep(0.5)
    screen = Screen(name)
    screen.send_commands(f'python {os.path.join(PROJECT_ROOT, "master_ioc.py")} -i {name}')
    screen.enable_logs(lp)
    screen.send_commands('softioc.dbl()')
    return f'{name}: started'

def stop_ioc(name):
    if not ioc_running(name):
        return f'{name}: not running'
    subprocess.run(['screen', '-XS', name, 'kill'], check=False)
    return f'{name}: stopped'

def restart_ioc(settings, name):
    stop_ioc(name)
    time.sleep(1)
    return start_ioc(settings, name)

def manager_running():
    return Screen(MANAGER_SCREEN).exists

def start_manager():
    if manager_running():
        return 'manager: already running'
    subprocess.run(['screen', '-dmS', MANAGER_SCREEN, 'bash'], check=False)
    time.sleep(0.5)
    screen = Screen(MANAGER_SCREEN)
    screen.send_commands(f'cd {PROJECT_ROOT} && python ioc_manager.py')
    return 'manager: started'

def stop_manager():
    if not manager_running():
        return 'manager: not running'
    subprocess.run(['screen', '-XS', MANAGER_SCREEN, 'kill'], check=False)
    return 'manager: stopped'

def restart_manager():
    stop_manager()
    time.sleep(1)
    return start_manager()


# ── Colour pair indices ────────────────────────────────────────────────────────
C_NORMAL   = 0
C_HEADER   = 1
C_RUNNING  = 2
C_STOPPED  = 3
C_SELECTED = 4
C_STATUS   = 5
C_TITLE    = 6
C_DIM      = 7

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
def draw_main(win, settings, names, selected, status_msg, prefix):
    h, w = win.getmaxyx()
    win.erase()

    mgr_label = 'MANAGER: running' if manager_running() else 'MANAGER: stopped'
    _, w = win.getmaxyx()
    title    = f' {prefix} IOC Monitor '
    mgr_attr = (curses.color_pair(C_RUNNING) if manager_running()
                else curses.color_pair(C_STOPPED))
    draw_title(win, title)
    safe_addstr(win, 0, w - len(mgr_label) - 2, mgr_label,
                curses.color_pair(C_TITLE) | curses.A_BOLD | mgr_attr)

    # Column widths
    col_name = max(len(n) for n in names) + 2
    col_st   = 10
    col_auto = 10
    col_log  = max(w - col_name - col_st - col_auto - 4, 10)

    # Header row
    hdr = (f'{"IOC":<{col_name}}'
           f'{"STATUS":<{col_st}}'
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

        running   = ioc_running(name)
        autostart = settings[name].get('autostart', False)

        run_label  = 'running' if running  else 'stopped'
        auto_label = 'yes'     if autostart else 'no'

        last_line = read_log_lines(log_path(settings, name), 1)[0].strip()
        if len(last_line) > col_log:
            last_line = last_line[:col_log - 3] + '...'

        line = (f'{name:<{col_name}}'
                f'{run_label:<{col_st}}'
                f'{auto_label:<{col_auto}}'
                f'{last_line:<{col_log}}')

        if i == selected:
            fill_row(win, row, curses.color_pair(C_SELECTED) | curses.A_BOLD)
            safe_addstr(win, row, 1, line[:w - 2],
                        curses.color_pair(C_SELECTED) | curses.A_BOLD)
        else:
            win.move(row, 0)
            run_attr  = (curses.color_pair(C_RUNNING) if running
                         else curses.color_pair(C_STOPPED))
            auto_attr = (curses.color_pair(C_RUNNING) if autostart
                         else curses.color_pair(C_DIM))
            safe_addstr(win, row, 1,          f'{name:<{col_name}}')
            safe_addstr(win, row, 1+col_name, f'{run_label:<{col_st}}',  run_attr)
            safe_addstr(win, row, 1+col_name+col_st,
                        f'{auto_label:<{col_auto}}', auto_attr)
            safe_addstr(win, row, 1+col_name+col_st+col_auto, last_line)

    draw_help(win, [('s','start'),('x','stop'),('l','logs'),('a','attach'),
                    ('m','mgr start'),('M','mgr stop'),('?','help'),('q','quit')])
    draw_status(win, f'  {status_msg}   (auto-refresh {REFRESH_SECS}s)')
    win.refresh()


# ── Help view ──────────────────────────────────────────────────────────────────
HELP_LINES = [
    ('Main view', [
        ('↑ / ↓',       'Select IOC'),
        ('Enter',        'View live PV values for selected IOC'),
        ('s',            'Start selected IOC'),
        ('x',            'Stop selected IOC'),
        ('r',            'Restart selected IOC'),
        ('l',            'View log for selected IOC'),
        ('a',            'Attach to screen session (Ctrl+A D to detach)'),
        ('S',            'Start ALL autostart IOCs'),
        ('X',            'Stop ALL running IOCs'),
        ('m',            'Start IOC manager'),
        ('M',            'Stop IOC manager'),
        ('R',            'Restart IOC manager'),
        ('?',            'Show this help page'),
        ('q / Esc',      'Quit'),
    ]),
    ('Log view', [
        ('↑ / ↓',        'Scroll one line'),
        ('PgUp / PgDn',  'Scroll one page'),
        ('l / q / Esc',  'Return to main view'),
    ]),
    ('PV view', [
        ('↑ / ↓',        'Scroll one line'),
        ('PgUp / PgDn',  'Scroll one page'),
        ('f',            'Force immediate refresh'),
        ('d',            'Request dbl() from IOC (re-list PVs)'),
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
    """Return a dict {pv_name: value_str} using aioca.caget."""
    if not pv_names:
        return {}

    async def _fetch():
        results = await aioca.caget(pv_names, timeout=2.0, throw=False)
        out = {}
        for name, val in zip(pv_names, results):
            if isinstance(val, aioca.CANothing):
                out[name] = '(disconnected)'
            elif hasattr(val, '__len__') and not isinstance(val, str):
                out[name] = str(list(val))
            else:
                out[name] = str(val)
        return out

    try:
        return asyncio.run(_fetch())
    except Exception as e:
        return {n: f'(error: {e})' for n in pv_names}


# ── PV view ────────────────────────────────────────────────────────────────────
def pv_view(stdscr, settings, name, prefix):
    """Full-screen view of all PVs for one IOC with live values."""
    curses.curs_set(0)
    scroll    = 0
    pv_vals   = {}
    status    = 'Fetching…'
    last_fetch = 0.0

    lp      = log_path(settings, name)
    pv_list = pv_names_from_log(lp, prefix)

    while True:
        now = time.monotonic()
        if now - last_fetch >= REFRESH_SECS:
            pv_list   = [p for p in pv_names_from_log(lp, prefix) if not p.endswith('_time')]
            pv_vals   = fetch_pv_values(pv_list)
            last_fetch = now
            ts         = time.strftime('%H:%M:%S')
            status     = f'Updated {ts}  ({len(pv_list)} PVs)' if pv_list else 'No PVs found in log'

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_title(stdscr, f' {prefix} — PVs: {name} ')

        if not pv_list:
            safe_addstr(stdscr, 2, 2, 'No PVs found. Is the IOC running?',
                        curses.color_pair(C_STOPPED))
        else:
            col_pv  = max(len(p) for p in pv_list) + 2
            col_val = max(w - col_pv - 4, 10)

            # Column header
            fill_row(stdscr, 1, curses.color_pair(C_HEADER) | curses.A_BOLD)
            safe_addstr(stdscr, 1, 1,
                        f'{"PV":<{col_pv}}{"VALUE":<{col_val}}'[:w - 2],
                        curses.color_pair(C_HEADER) | curses.A_BOLD)

            view_rows  = h - 4
            max_scroll = max(0, len(pv_list) - view_rows)
            scroll     = min(scroll, max_scroll)

            for i, pv in enumerate(pv_list[scroll:scroll + view_rows]):
                row = i + 2
                val = pv_vals.get(pv, '…')
                if len(val) > col_val:
                    val = val[:col_val - 3] + '...'
                # Dim the prefix portion, highlight the suffix
                pv_display = f'{pv:<{col_pv}}'
                val_attr = (curses.color_pair(C_STOPPED)
                            if 'disconnected' in val or 'error' in val
                            else curses.color_pair(C_RUNNING))
                safe_addstr(stdscr, row, 1, pv_display)
                safe_addstr(stdscr, row, 1 + col_pv, val, val_attr)

        draw_help(stdscr, [('↑↓','scroll'),('f','refresh'),('d','request dbl()'),('q/Esc','back')])
        draw_status(stdscr, f'  {status}')
        stdscr.refresh()

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key in (ord('q'), 27):
            return
        elif key == ord('f'):
            last_fetch = 0.0          # force immediate refresh on next loop
        elif key == ord('d'):
            if ioc_running(name):
                Screen(name).send_commands('dbl()')
                status = f'Sent dbl() to {name} — refreshing…'
                time.sleep(1)         # give the IOC a moment to write to the log
                last_fetch = 0.0
            else:
                status = f'{name} is not running'
        elif key == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            scroll = min(max(0, len(pv_list) - (h - 4)), scroll + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - (h - 4))
        elif key == curses.KEY_NPAGE:
            scroll = min(max(0, len(pv_list) - (h - 4)), scroll + (h - 4))


# ── Main TUI loop ──────────────────────────────────────────────────────────────
def tui(stdscr):
    init_colors()
    curses.curs_set(0)
    settings = load_settings()
    names    = ioc_names(settings)
    prefix   = settings['general']['prefix']
    selected = 0
    status   = 'Ready'

    while True:
        draw_main(stdscr, settings, names, selected, status, prefix)

        stdscr.timeout(REFRESH_SECS * 1000)
        key = stdscr.getch()

        if key == -1:
            # Timeout — just refresh (status already cleared on next draw)
            status = 'Ready'
            continue

        name = names[selected]

        if key in (ord('q'), 27):
            break
        elif key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(len(names) - 1, selected + 1)
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            pv_view(stdscr, settings, name, prefix)
            status = f'Returned from PVs: {name}'
        elif key == ord('s'):
            status = suspended(stdscr, lambda: start_ioc(settings, name))
        elif key == ord('x'):
            status = suspended(stdscr, lambda: stop_ioc(name))
        elif key == ord('r'):
            status = suspended(stdscr, lambda: restart_ioc(settings, name))
        elif key == ord('l'):
            log_view(stdscr, settings, name, prefix)
            status = f'Returned from log: {name}'
        elif key == ord('a'):
            if ioc_running(name):
                do_attach(stdscr, name)
                status = f'Detached from {name}'
            else:
                status = f'{name} is not running'
        elif key == ord('S'):
            def start_all():
                msgs = [start_ioc(settings, n) for n in names
                        if settings[n].get('autostart', False)]
                return ' | '.join(msgs) or 'Nothing to start'
            status = suspended(stdscr, start_all)
        elif key == ord('X'):
            def stop_all():
                msgs = [stop_ioc(n) for n in names if ioc_running(n)]
                return ' | '.join(msgs) or 'Nothing running'
            status = suspended(stdscr, stop_all)
        elif key == ord('m'):
            status = suspended(stdscr, start_manager)
        elif key == ord('M'):
            status = suspended(stdscr, stop_manager)
        elif key == ord('R'):
            status = suspended(stdscr, restart_manager)
        elif key == ord('?'):
            help_view(stdscr, prefix)


def main():
    os.chdir(PROJECT_ROOT)
    settings = load_settings()
    os.environ['EPICS_CA_ADDR_LIST']      = settings['general']['epics_addr_list']
    os.environ['EPICS_CA_AUTO_ADDR_LIST'] = 'NO'
    try:
        curses.wrapper(tui)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
