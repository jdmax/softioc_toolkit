# J. Maxwell 2023
import asyncio
import datetime
import os
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import aioca
import yaml
from softioc import softioc, builder, asyncio_dispatcher

import ioc_common

# Commands on the _control and all PVs
STOP, RUN, RESET = 0, 1, 2
# States of the _status PVs
STOPPED, STARTING, RUNNING, STALE, FAILED, STOPPING = range(6)


async def main():
    """
    IOC to manage IOCS. Sets up PVs for each IOC in settings file to allow starting and stopping.
    Uses Unix Screen to run master_ioc for each device IOC. This is the only thing that starts or stops IOC screens.
    """
    with open(os.path.join(ioc_common.PROJECT_ROOT, 'settings.yaml')) as f:  # Load settings from YAML config file
        settings = yaml.safe_load(f)
    ioc_common.apply_ca_env(settings)

    dispatcher = asyncio_dispatcher.AsyncioDispatcher()
    device_name = settings['general']['prefix'] + ':MAN'
    builder.SetDeviceName(device_name)

    i = IOCManager(device_name, settings, dispatcher.loop)
    builder.LoadDatabase()
    softioc.iocInit(dispatcher)

    async def loop():
        while True:
            await i.heartbeat()

    dispatcher(loop)  # put functions to loop in here
    softioc.interactive_ioc(globals())


class IOCManager:
    """
    Handles screens which run iocs. Makes PVs to control and monitor each ioc.
    """

    def __init__(self, device_name, settings, loop):
        """
        Make PVs for each IOC in the settings, keyed by IOC name:
            <name>_control  Stop/Run/Reset command, reads back as the last applied state
            <name>_status   Stopped, Starting, Running, Stale, Failed or Stopping
            <name>_msg      Short description of the status
            <name>_hb       Seconds since the IOC last updated its _time PV
        """
        self.device_name = device_name
        self.settings = settings
        self.loop = loop     # dispatcher event loop, for CA calls from worker threads
        self.names = [name for name in settings if name != 'general']
        self.delay = settings['general']['delay']
        self.start_timeout = settings['general'].get('start_timeout', 30)

        self.control = {}
        self.status = {}
        self.msg = {}
        self.hb = {}
        self.last_update = {}   # last _time value read from each IOC
        self.workers = {}       # one thread per IOC, so its commands run in the order they arrive
        self.pending = {}       # number of queued or running commands per IOC
        self.finished = {}      # number of completed commands per IOC
        self.lock = threading.Lock()

        for name in self.names:
            self.control[name] = builder.mbbOut(f'{name}_control',
                                                ("Stop", 'MINOR'),
                                                ("Run", 0),
                                                ("Reset", 'MINOR'),
                                                always_update=True,   # so Run works on a crashed IOC still showing Run
                                                on_update=lambda i, name=name: self.request(name, i))
            self.status[name] = builder.mbbIn(f'{name}_status',
                                              ("Stopped", 0),
                                              ("Starting", 0),
                                              ("Running", 0),
                                              ("Stale", 'MINOR'),
                                              ("Failed", 'MAJOR'),
                                              ("Stopping", 0))
            self.msg[name] = builder.stringIn(f'{name}_msg', initial_value='')
            stale = self.stale_after(name)
            self.hb[name] = builder.longIn(f'{name}_hb', EGU='s', HIGH=stale, HSV='MINOR')
            self.workers[name] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
            self.pending[name] = 0
            self.finished[name] = 0

        self.pv_all = builder.mbbOut('all',
                                     ("Stop", 'MINOR'),
                                     ("Run", 0),
                                     ("Reset", 'MINOR'),
                                     always_update=True,
                                     on_update=self.request_all)
        self.pv_time = builder.aIn('time')   # manager's own heartbeat
        self.pv_time.set(datetime.datetime.now().timestamp())

    def stale_after(self, name):
        """Seconds without an update before an IOC counts as stale; settable per IOC with stale_after"""
        delay = self.settings[name].get('delay', self.delay)
        return int(self.settings[name].get('stale_after', max(3 * delay, delay + 10)))

    # ── Commands ───────────────────────────────────────────────────────────────
    def request(self, name, command):
        """
        Control PV was written: 0=Stop, 1=Run, 2=Reset. Queue it on the IOC's worker thread,
        so the IOC loop never waits on screen.
        """
        with self.lock:
            self.pending[name] += 1
        self.workers[name].submit(self.run_command, name, command)

    def request_all(self, command):
        """Stop every IOC, or Run or Reset every IOC with autostart set to True."""
        for name in self.names:
            if command == STOP or self.settings[name].get('autostart', False):
                self.request(name, command)

    def run_command(self, name, command):
        try:
            if command == STOP:
                self.stop_ioc(name)
            elif command == RUN:
                self.start_ioc(name)
            elif command == RESET:
                if self.stop_ioc(name):
                    self.start_ioc(name)
        except Exception as e:
            print(f"{name}: command {command} failed: {e}")
            self.set_status(name, FAILED, str(e))
        finally:
            with self.lock:
                self.pending[name] -= 1
                self.finished[name] += 1

    def start_ioc(self, name):
        """
        Start screen to run ioc, then run ioc. Wait until it answers over CA, then list its PVs into the log.
        """
        if name in ioc_common.screen_sessions():
            self.control[name].set(RUN, process=False)   # already running, nothing to do
            return
        self.set_status(name, STARTING, 'Starting')
        command = shlex.join([sys.executable, 'master_ioc.py', '-i', name])
        if not ioc_common.start_session(name, command, ioc_common.log_path(self.settings, name)):
            self.control[name].set(STOP, process=False)
            self.set_status(name, FAILED, 'Could not create screen session')
            return

        self.control[name].set(RUN, process=False)
        if self.wait_for_ioc(name):
            ioc_common.send_to_session(name, 'softioc.dbl()')   # PV list goes into the log for Commander
            self.set_status(name, RUNNING, 'Running')
        elif name in ioc_common.screen_sessions():
            # Leave the session up so the error can be read in the log or by attaching
            self.set_status(name, FAILED, f'No response after {self.start_timeout} s, see log')
        else:
            self.control[name].set(STOP, process=False)
            self.set_status(name, FAILED, 'Screen session ended, see log')

    def stop_ioc(self, name):
        """
        Kill screen and ioc running within it. Returns True if it is stopped.
        """
        if name in ioc_common.screen_sessions():
            self.set_status(name, STOPPING, 'Stopping')
            if not ioc_common.kill_session(name):
                self.set_status(name, FAILED, 'Screen session did not stop')
                return False
        self.control[name].set(STOP, process=False)
        self.hb[name].set(0)
        self.last_update.pop(name, None)
        self.set_status(name, STOPPED, 'Stopped')
        return True

    def wait_for_ioc(self, name):
        """Wait for the IOC's _time PV to answer. Returns False on timeout or if its session ends."""
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if name not in ioc_common.screen_sessions():
                return False
            t = asyncio.run_coroutine_threadsafe(self.get_time(name), self.loop).result()
            if t is not None:
                self.last_update[name] = t
                return True
            time.sleep(0.5)
        return False

    def set_status(self, name, state, message):
        """Set status and message PVs, only posting changes"""
        if self.status[name].get() != state:
            self.status[name].set(state)
        message = message[:39]   # stringin holds 40 characters including the terminator
        if self.msg[name].get() != message:
            self.msg[name].set(message)

    # ── Monitoring ─────────────────────────────────────────────────────────────
    async def heartbeat(self):
        """
        Check last time written versus current time for each IOC, and bring status PVs in line with the
        screen sessions. Sessions already running when the manager starts are picked up here.
        """
        await asyncio.sleep(self.delay)
        self.pv_time.set(datetime.datetime.now().timestamp())

        with self.lock:   # skip IOCs with a command in progress, the worker owns their status
            idle = {n: self.finished[n] for n in self.names if self.pending[n] == 0}
        sessions = await asyncio.to_thread(ioc_common.screen_sessions)
        running = [n for n in idle if n in sessions]
        times = await asyncio.gather(*(self.get_time(n) for n in running))
        times = dict(zip(running, times))
        now = datetime.datetime.now().timestamp()

        with self.lock:   # drop any IOC that had a command arrive while we were looking
            idle = [n for n in idle if self.pending[n] == 0 and self.finished[n] == idle[n]]

        for name in idle:
            if name in sessions:
                self.check_running(name, times[name], now)
            else:
                self.check_stopped(name)

    def check_running(self, name, t, now):
        if self.control[name].get() != RUN:
            self.control[name].set(RUN, process=False)
        if t is not None:
            self.last_update[name] = t
        if name in self.last_update:
            age = int(now - self.last_update[name])
            self.hb[name].set(age)
        else:
            age = None

        if t is None:
            if self.status[name].get() != FAILED:   # a failed start stays failed until it answers
                self.set_status(name, STALE, 'No response over CA')
        elif age > self.stale_after(name):
            self.set_status(name, STALE, f'No update for {age} s')
        else:
            self.set_status(name, RUNNING, 'Running')

    def check_stopped(self, name):
        if self.control[name].get() != STOP:
            self.control[name].set(STOP, process=False)
        self.last_update.pop(name, None)
        state = self.status[name].get()
        if state in (RUNNING, STALE, STARTING, STOPPING):
            self.hb[name].set(0)
            self.set_status(name, STOPPED, 'Screen session ended')

    async def get_time(self, name):
        """Read the IOC's _time PV, or None if it doesn't answer"""
        try:
            return float(await aioca.caget(f"{self.device_name}:{name}_time", timeout=1))
        except aioca.CANothing:
            return None


if __name__ == "__main__":
    asyncio.run(main())
