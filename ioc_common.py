# Helpers shared by ioc_manager.py, master_ioc.py and tools/ioc_cli.py
import os
import subprocess
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MANAGER_SCREEN = 'ioc-manager'   # screen session name for ioc_manager.py


# ── Settings ───────────────────────────────────────────────────────────────────
def log_path(settings, name):
    """Screen log file for a session; relative log_dir is relative to the project root."""
    log_dir = settings['general'].get('log_dir', 'logs')
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(PROJECT_ROOT, log_dir)
    return os.path.normpath(os.path.join(log_dir, name))

def apply_ca_env(settings):
    """Restrict Channel Access to general.epics_addr_list, unless it is empty or None."""
    addr_list = settings['general'].get('epics_addr_list')
    if addr_list is None or str(addr_list).strip() in ('', 'None'):
        return
    os.environ['EPICS_CA_ADDR_LIST'] = str(addr_list)
    os.environ['EPICS_CA_AUTO_ADDR_LIST'] = 'NO'


# ── GNU Screen sessions ────────────────────────────────────────────────────────
def screen_sessions():
    """Return {session name: 'pid.name'} for this user's live screen sessions."""
    try:
        out = subprocess.run(['screen', '-ls'], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return {}
    sessions = {}
    for line in out.splitlines():
        # Session lines look like "\t12345.name\t(Detached)"
        if not line.startswith('\t') or '(Dead' in line:
            continue
        session_id = line.split('\t')[1]
        pid, _, name = session_id.partition('.')
        if pid.isdigit() and name:
            sessions[name] = session_id
    return sessions

def _screen_command(session_id, *command):
    # Address sessions by 'pid.name' so one name can't match another that starts the same way
    subprocess.run(['screen', '-S', session_id, '-X', *command],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_session(name, command, log_file=None, timeout=5.0):
    """
    Start a detached screen session running bash in the project root, and type command into it.
    With log_file, the session is logged there and any previous log is kept as <log_file>.1.
    Returns True if the session started.
    """
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        if os.path.exists(log_file):
            os.replace(log_file, log_file + '.1')
    subprocess.run(['screen', '-dmS', name, 'bash'], cwd=PROJECT_ROOT)

    deadline = time.monotonic() + timeout
    while name not in screen_sessions():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    session_id = screen_sessions()[name]

    if log_file:   # turn logging on before the command runs, so startup errors are captured
        _screen_command(session_id, 'logfile', log_file)
        _screen_command(session_id, 'logfile', 'flush', '1')
        _screen_command(session_id, 'log', 'on')
    _screen_command(session_id, 'stuff', command + '\n')
    return True

def send_to_session(name, text):
    """Type a line of text into a session. Returns False if the session doesn't exist."""
    session_id = screen_sessions().get(name)
    if session_id is None:
        return False
    _screen_command(session_id, 'stuff', text + '\n')
    return True

def kill_session(name, timeout=5.0):
    """Kill a session and everything running in it. Returns True once it is gone."""
    session_id = screen_sessions().get(name)
    if session_id is None:
        return True
    _screen_command(session_id, 'quit')
    deadline = time.monotonic() + timeout
    while name in screen_sessions():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True
