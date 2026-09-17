# Fix checklist

Findings from a code review on 2026-09-16, against commit `5f854fa` on the `supervisord` branch.
Line numbers refer to that commit and will drift as fixes land, so search for the quoted code
if a line number no longer matches.

## How to use this file

- Work top to bottom. The P0 items break things with the `settings.yaml` that is committed now.
- Take one item, or a small group of items in the same file, per session and per commit. Tick
  the box in the same commit as the fix.
- Items were found by reading the code. Nothing was run. Items marked *(traced)* follow the
  logic through the library source (`softioc`, `aioca`, `screenutils`) but were not reproduced.
- The project runs on Linux (`screen`, `curses`). There is no test suite. At a minimum, run
  `python -m py_compile <file>` after each edit. Anything involving screen sessions, curses or
  Channel Access has to be checked on the Linux IOC host.
- The `devices/` submodule (epics-device-lib) was not reviewed beyond `base_device.py`,
  `telnet_base.py` and `modbus_base.py`. Don't edit it from this repo.

## Done: the IOC manager owns IOC screen sessions (2026-09-17)

`ioc_manager.py` was rewritten, and `tools/ioc_cli.py` (Commander) now starts, stops and restarts
IOCs by writing to the manager's PVs. Commander still runs on the manager's host, reads logs,
attaches to sessions, and starts and stops the manager's own session. The screen, settings and
Channel Access helpers moved into a new `ioc_common.py`, and `screenutils` is no longer used.
The README describes the new manager PVs (`_status`, `_msg`, `longIn` `_hb`, `time`) and
behaviour.

This fixed items 1, 2, 7, 9, 12, 13, 15, 16, 17 and 33, and parts of 14 and 19. Line numbers
quoted below for `ioc_manager.py` and `tools/ioc_cli.py` no longer match.

It was tested end to end in WSL (Ubuntu, screen 4.09, softioc 4.6.1) with fake IOCs:
- start, and Run while already running
- reset, which restarts once and doesn't loop
- a failed import, and an IOC that stops updating
- a session dying outside the manager, and a manager restart that picks up running IOCs
- stop all, including non-autostart IOCs
- Commander with the manager down

Nothing was tested against real hardware or on the production host.

---

## P0: broken with the committed settings

- [x] **1. `epics_addr_list: 'None'` disables Channel Access in the manager and Commander.**
  `ioc_manager.py:24-26` and `tools/ioc_cli.py:1029-1030` set `EPICS_CA_ADDR_LIST` to the
  literal string `None` and set `EPICS_CA_AUTO_ADDR_LIST=NO`, so no PV can be found.
  `master_ioc.py:19` already skips `'None'`.
  Fix: add one helper used by all three scripts. It sets the two variables only when the
  value is non-empty and not `None`/`'None'`. This also covers an unquoted `None` in the YAML,
  which currently raises `TypeError`.
  Check: with `'None'`, both variables stay unset. With a real address list, both are set.

- [x] **2. `MAN:all` starts nothing because `ioc_load` has no `autostart`.**
  `ioc_manager.py:115` uses `self.settings[name]['autostart']`. `ioc_load` comes first in the
  file, so the `KeyError` ends the loop before any IOC starts.
  Fix: `.get('autostart', False)`. Also change `'general' in name` to `name == 'general'` at
  lines 63 and 114, because the substring test would also skip an IOC named `general_purpose`.

- [ ] **3. An empty `records:` key crashes the IOC and stops archiver discovery.**
  In `rigol_dp832`, `records:` loads as `None`. At `master_ioc.py:55`,
  `.get('records', {})` returns `None`, and `name in records` at line 67 raises `TypeError`.
  At `logic_devices/archiver.py:230-231`, iterating `None` jumps to the outer `except`, so no
  IOC after it is archived.
  Fix: `records = ioc_settings.get('records') or {}` in both places. In the archiver, catch
  errors per IOC instead of around the whole loop.

- [ ] **4. The status IOC can't find `states.yaml`.**
  `logic_devices/status_ioc.py:20` opens `states.yaml`, and lines 62 and 76 use `last.yaml`,
  all relative to the working directory. The manager and Commander run from the repo root,
  but both files are in `logic_devices/`.
  Fix: build the paths from `Path(__file__).parent`.

- [ ] **5. The status IOC never restores its last state.**
  `status_ioc.py:73` defines `async def connect()`, but `master_ioc.py:59` calls
  `self.device.connect()` without awaiting it, so the coroutine never runs.
  Fix: make `connect()` a normal function, since it doesn't await anything. Don't change
  `master_ioc.py`, because every `BaseDevice.connect()` is synchronous.
  Watch out: `.set()` on the `status`/`species` PVs happens before `iocInit`, so it doesn't
  fire `stat_update`. Decide whether restoring should also re-apply the alarm limits, and if
  so, trigger it after `iocInit`.

- [ ] **6. The archiver never stores its file handles.**
  At `logic_devices/archiver.py:147`, `_get_csv_writer(pv)` is called while the dict for
  `monitored_pvs[pv]` is still being built, so the check at line 319 fails and `file_handle`
  is never saved. As a result, writes are never flushed, the daily file never rolls over, and
  handles leak on every disable/enable.
  Fix: create the entry first, then open the writer, or have `_get_csv_writer` return
  `(writer, handle)` and store both.
  Check: after midnight a new dated CSV appears, and rows show up in the file right away.

## P1: serious runtime bugs and safety

- [x] **7. Manager Reset can restart an IOC in a loop or leave it stopped.** *(traced)*
  `ioc_manager.py:93-149`. `.set()` on an output record queues `on_update` when the value
  changes. During Reset, `stop_ioc` calls `set(0)`, which queues a Stop, and then the IOC is
  started. The queued Stop either kills the new screen, or the start thread's `set(1)` at
  line 212 hits "Run while running, so reset" at line 102 and the cycle repeats. "all Run" on
  an IOC that Commander started also ends up here. `time.sleep(1)` at line 148 blocks the
  IOC's event loop.
  Fix: pass `process=False` whenever the manager sets its own control PVs to reflect state.
  Make Run on a running IOC do nothing. Move the stop/sleep/start sequence into the worker
  thread.

- [ ] **8. Commander lets you write to input records.**
  At `tools/ioc_cli.py:645-647`, a failed `.RTYP` lookup is cached as `None`, and
  `is_pv_writable` treats `None` as writable. If a PV view opens before its IOC is up, those
  PVs stay writable for the rest of the session.
  Fix: don't cache failures, so they are retried. Treat an unknown type as read-only until it
  resolves.

- [x] **9. Commander has no confirmation for destructive keys.**
  `X` (stop all), `M` (stop manager) and `R` (restart manager) act on a single keypress
  (`tools/ioc_cli.py:1011-1021`). Consider confirming `x` and `r` too.
  Fix: reuse `confirm_popup`.

- [ ] **10. The status IOC reports "Full" when it fails.**
  At `status_ioc.py:152-159`, both `except` branches set `production` to 4 (Full) with a
  MAJOR alarm. They should use 0 (Not Ready) with an INVALID alarm.
  Related: `logic_devices/states.yaml` has no `thresholds` for `4He` or `3He`, so choosing
  either species raises `KeyError` on every loop. Either add thresholds or skip the
  production logic when a species has none.

- [ ] **11. The status IOC hardcodes another deployment's PV names.**
  `status_ioc.py:94-96, 99-102, 111, 131, 141` use `TGT:BTARG:*`, including puts to
  `TGT:BTARG:status`. The committed prefix is `MYLAB`, and `status.full_status` in
  `settings.yaml` also lists `TGT:BTARG:*` PVs.
  Fix: build names from `self.device_name`. Set this IOC's own status with
  `self.pvs['status'].set(n)`, not a caput to itself. Move the flag PV names into settings,
  and skip the flag logic when they aren't configured.

- [x] **12. The manager's start thread is fragile.**
  `ioc_manager.py:195-220`.
  - `enable_logs` runs after the IOC command, so early import errors are missing from the
    log. Commander has the same order at `tools/ioc_cli.py:107-108`.
  - The manager never creates `log_dir`, so `os.path.getsize` raises `FileNotFoundError` and
    the thread dies.
  - Logs append, so an old log passes the `> 10` bytes check at once and the IOC is marked
    running before it starts.

  Fix: create `log_dir`, and delete or rotate the log before starting. Turn logging on before
  sending the command. Wait for the PV list output from `softioc.dbl()`, not for file size.
  Do this together with item 13.

- [x] **13. Logs keep every run's output.**
  Commander reads PV names from the whole log (`tools/ioc_cli.py:575-591`) every 2 s, so PVs
  that were renamed or removed stay listed as disconnected, and each refresh gets slower as
  the log grows.
  Fix: clear or rotate the log whenever an IOC starts, in both the manager and Commander, for
  example by renaming it to `<name>.1`. Optionally parse only the output after the last
  `dbl()`.

- [ ] **14. The archiver misses IOCs and doesn't start by itself.**
  `logic_devices/archiver.py`.
  - ~~Line 194: an IOC counts as running only if its manager `_control` PV is 1, so IOCs that
    Commander started are never archived.~~ Fixed by the manager rewrite: every IOC now starts
    through the manager, and `_control` reads back the real state. Checking
    `<prefix>:MAN:<name>_status` == Running would be more precise.
  - Line 87: `Archive_Enable` starts False, so archiving is off after every restart. Add an
    `enable_on_start` setting and start archiving once `iocInit` has run.
  - Lines 97-100: `do_reads` replaces an Error (2) status with Running or Stopped.
  - Lines 62 and 65: the fallback prefix is hardcoded to `TGT:MEOP`.

- [x] **15. Heartbeat PVs are the wrong record type.**
  At `ioc_manager.py:70`, `_hb` is an `mbbOut`, which holds a state index from 0 to 15 and has
  no HIGH/HIHI fields. That's likely why the alarm lines at 71-72 are commented out.
  Fix: use `builder.longIn` with `EGU='s'` and HIGH/HIHI limits. Check that no `.bob` screen
  in `gui/` expects an mbbo, and update the README table.

- [x] **16. Some IOC names break the manager.**
  At `ioc_manager.py:123, 134, 145`, `pv_name.replace('_control', '')` removes every match,
  so `flow_controller_control` becomes `flowler`. Use `removesuffix('_control')`, which needs
  Python 3.9 or later.

- [x] **17. Heartbeats only cover IOCs this manager instance started.**
  `ioc_manager.py:170` loops over `self.screens`, which only `StartThread` fills. IOCs started
  by Commander or before a manager restart get no heartbeat.
  Fix: loop over every IOC whose screen exists. Also iterate over `list(...)`, because another
  thread changes the dict.

## P2: compatibility and portability

- [ ] **18. `requirements.txt` is missing packages.**
  Add `streamlit`, `pandas` and `plotly` (archive viewer) and `pyepics` (the `tools/` benchmarks).
  Optional extras files are fine if you want to keep the IOC install small.

- [ ] **19. Scripts depend on the working directory and on `python` in PATH.**
  - `master_ioc.py:88` loads `./settings.yaml`, and `ioc_manager.py:21` opens `settings.yaml`
    relative to the working directory.
  - ~~`ioc_manager.py:197`, `tools/ioc_cli.py:107` and `tools/ioc_cli.py:132` start IOCs with
    `python`.~~ Fixed: both now use `sys.executable` and load settings by absolute path.
    `start_ioc_manager.sh` still runs plain `python` and writes no log, unlike Commander's `m`.
  - `master_ioc.py:100` and `master_ioc.py:104` call `exit()`, which should be `sys.exit(1)`.

  Fix: resolve the repo root from `__file__`, pass `sys.executable` to screen, and `chdir` to
  the repo root before starting.
  `master_ioc.py:92-93`: `ioc_list.remove('general')` fails if there is no `general` key.

- [ ] **20. The shell launchers depend on where they're run from.**
  `commander.sh`, `start_ioc_manager.sh` and `archive/start_archive_viewer.sh` have no shebang
  (except the viewer), don't `cd` to their own directory, and hardcode `venv/`, although this
  checkout uses `.venv/`.
  Fix: add a shebang, `cd "$(dirname "$0")"`, and activate whichever of `venv` or `.venv`
  exists. Mark the scripts executable in git.

- [ ] **21. `.gitignore` problems.**
  - `archive/` ignores the whole directory, so new viewer files won't be tracked. Change it to
    `archive/data/`.
  - `last.yaml` is ignored, but `logic_devices/last.yaml` is already committed. Run
    `git rm --cached logic_devices/last.yaml`.
  - Add `venv/` and `.venv/`.

- [ ] **22. Commander crashes on some terminals and some settings files.**
  - `curses.use_default_colors()` and `curses.curs_set()` raise on terminals that don't
    support them. Wrap them in `try/except curses.error`.
  - An empty IOC list crashes at `tools/ioc_cli.py:257` (`max()` on an empty sequence) and at
    `tools/ioc_cli.py:976` (`names[selected]`).
  - Popups crash when the terminal is smaller than the box. Clamp `box_w` and `box_h`.

- [ ] **23. The archive viewer uses deprecated Streamlit arguments.**
  `archive/archive_viewer.py` uses `use_container_width=`, which current Streamlit deprecates in
  favor of `width='stretch'`. Pin a Streamlit version in item 18 so you know which to use.

## P3: ease of use

- [ ] **24. Commander freezes while fetching PVs.**
  `fetch_pv_values` (`tools/ioc_cli.py:593-623`) calls `caget` with a 2 s timeout on the UI
  thread every 2 s, so disconnected PVs lock the UI.
  Fix: keep `camonitor` subscriptions on a background event-loop thread that writes values
  into a shared dict for the UI to read. This is the biggest change in this list.

- [ ] **25. Esc takes about 1 s in Commander.** Set `os.environ.setdefault('ESCDELAY', '25')`
  in `main()` before `curses.wrapper`.

- [ ] **26. The log view doesn't open on the newest lines.** It starts at the top of the last
  200 lines (`tools/ioc_cli.py:416`). Start at the bottom and follow new output until the user
  scrolls up.

- [ ] **27. Enum PVs show numbers in Commander.** Fetch with `datatype=aioca.DBR_STRING` (keep
  `format=FORMAT_TIME` for severity), so enums show `Run` and not `1`, and floats follow PREC.

- [ ] **28. Attach fails if the session is attached somewhere else.** `tools/ioc_cli.py:571`
  uses `screen -r`. Use `screen -x`, which shares the session, or `screen -d -r`, which takes it
  over.

- [ ] **29. Setting mistakes are silently ignored.** In `master_ioc.py`, print a warning at
  startup for `records:` keys that don't match any PV (typos currently do nothing) and for
  missing `delay`. In the manager and Commander, warn about IOCs missing `module`.

- [ ] **30. The archive viewer clears its plot on every change.**
  - The results depend on `st.button` (`archive/archive_viewer.py:273, 276`), so changing any
    widget reruns the page and the plot disappears. Keep the loaded state in
    `st.session_state`.
  - The default path `data` (line 210) depends on the working directory. Resolve it from
    `__file__`.
  - `get_archived_pvs` (line 61) rebuilds PV names wrongly when the prefix contains `:`, so
    `TGT:BTARG_Cell` is shown for `TGT:BTARG:Cell`. Plotting still works.
  - `--server.address localhost` in the start script keeps other machines out. Make it
    configurable.

- [ ] **31. The benchmarks hardcode their target.** `tools/latency_test.py` and
  `tools/stress_test.py` hardcode `TGT:MEOP:Voltage1_VC`. Take the PV and the iteration or burst
  count from `argparse`. In `stress_test.py`, `statistics.stdev` fails with fewer than 2
  samples.

- [ ] **32. The Vaunix shim doesn't recover from a USB replug.**
  `LabBrick.reopen()` (`tools/vaunix_shim/vaunix_shim.py:212`) is never called. After a replug
  every command returns `ERR` until the service restarts. Call `reopen()` once on a
  `VaunixError` from a hardware call, then retry.
  The comment at line 440 says 10 GHz, but `1_000_000_00` 10 Hz counts is 1 GHz.

- [x] **33. (Optional refactor) Replace `screenutils`.** It is unmaintained and builds shell
  strings for `os.system`/`getoutput`. The code only needs "does a session exist" (one
  `screen -ls` per refresh), "start a detached session with logging"
  (`screen -dmS name -L -Logfile path ...`) and "kill a session". A small shared module used
  by both `ioc_manager.py` and `tools/ioc_cli.py` would also remove their duplicated
  start/stop code. Do this after items 7, 12 and 13, or together with them.
