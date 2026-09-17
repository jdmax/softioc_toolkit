# EPICS IOC Framework in Python

A Python-based EPICS IOC (Input/Output Controller) framework for scientific instrumentation control and data acquisition using [pythonSoftIOC](https://diamondlightsource.github.io/pythonSoftIOC/master/index.html). Designed for physics experiments requiring integration of multiple devices including temperature controllers, pressure gauges, power supplies, and motor controllers.

## Features

- **Modular Device Support**: Extensible driver architecture supporting Modbus, Telnet, and custom protocols
- **YAML Configuration**: Simple configuration management via `settings.yaml`
- **Data Archiving**: Built-in data logging with deadband logic and web-based viewer
- **Status Management**: Automated state machine for experimental procedures
- **GUI Integration**: CS-Studio/Phoebus display files included
- **IOC Management**: Centralized control of multiple IOC instances, over EPICS (`ioc_manager.py`) or from a terminal (`tools/ioc_cli.py`)

## Example Devices

- **Datexel**: Modbus I/O modules (8017 ADC, 8018 Thermocouple, 8024 DAC, 8130 Relay)
- **Lakeshore**: Temperature controllers (218, 336)
- **Scientific Instruments**: SI9700 temperature controller
- **Cryomagnetics**: CS-4 and CM4G magnet power supplies, LM-500 level probe
- **Rigol**: DP832 power supply
- **Pfeiffer**: TPG 26x vacuum gauges
- **MKS**: 937B pressure controllers
- **Zaber**: Motor controllers
- **American Magnetics**: AMI 136 level monitor

## Quick Start

1. **Clone with submodules**:
   ```bash
   git clone --recurse-submodules https://github.com/jdmax/softioc_toolkit.git
   cd softioc_toolkit
   # If you cloned without --recurse-submodules, run:
   # git submodule update --init
   ```

2. **Install dependencies**:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   The IOC manager script requires GNU Screen to be installed to manage IOC instances (`sudo apt install screen`).

3. **Configure devices** in `settings.yaml`:
   ```yaml
   general:
     prefix: MYLAB

   my_device:
     module: 'devices.instruments.ls218'
     ip: '192.168.1.100'
     port: '1001'
     channels: ['Temp1', 'Temp2']
   ```

4. **Start an IOC**:
   ```bash
   python master_ioc.py -i my_device
   ```

5. **View archive data**:
   ```bash
   cd archive
   ./start_archive_viewer.sh
   ```

## Repository Structure

```
softioc_toolkit/
├── master_ioc.py         ← IOC entry point: runs one device IOC (`-i <name>`)
├── ioc_manager.py        ← manager IOC: start, stop and reset the other IOCs over EPICS
├── ioc_common.py         ← settings, Channel Access and screen helpers shared by the scripts above
├── settings.yaml         ← device configuration (one top-level key per IOC)
├── start_ioc_manager.sh  ← run ioc_manager.py in a detached screen session
├── commander.sh          ← run the Commander TUI (tools/ioc_cli.py)
├── devices/              ← git submodule: epics-device-lib (shared instrument drivers)
│   ├── base_device.py    ← BaseDevice ABC
│   ├── modbus_base.py    ← ModbusDevice + ModbusConnection
│   ├── telnet_base.py    ← TelnetDevice + TelnetConnection
│   └── instruments/      ← concrete drivers (ls218, dat8024, zaber_motor, …)
├── logic_devices/        ← deployment-specific drivers (not in the shared library)
│   ├── archiver.py       ← EPICS PV archiver
│   ├── status_ioc.py     ← experimental state machine
│   └── states.yaml       ← alarm/setpoint table for target states
├── tools/                ← operator tools and benchmarks
│   ├── ioc_cli.py        ← Commander: curses TUI for IOCs, PVs and logs
│   ├── latency_test.py   ← caput → callback round-trip latency benchmark
│   ├── stress_test.py    ← burst-write throughput benchmark
│   └── vaunix_shim/      ← USB-to-TCP shim for Vaunix Lab Bricks (runs on a Pi)
├── archive/              ← archived CSV data plus the Streamlit archive viewer
│   ├── archive_viewer.py
│   └── start_archive_viewer.sh
└── gui/                  ← CS-Studio/Phoebus screens (.bob)
```

Module paths in `settings.yaml` follow the package structure:

| Driver type | Module path |
|---|---|
| Shared instrument driver | `devices.instruments.<name>` |
| Deployment-specific driver | `logic_devices.<name>` |
| Transport base (if referenced directly) | `devices.<base>` |

## Architecture

- **Base Classes**: `BaseDevice`, `ModbusDevice`, `TelnetDevice` — defined in `devices/` (the `epics-device-lib` submodule)
- **Instrument Drivers**: `devices/instruments/` — one module per hardware model, shared across deployments via the submodule
- **Deployment Drivers**: `logic_devices/` — site-specific drivers (archiver, status IOC) that live only in this repo
- **Master IOC**: `master_ioc.py` handles IOC lifecycle; loads driver modules dynamically via `importlib` using `module:` from `settings.yaml`
- **IOC Manager**: `ioc_manager.py` is itself an IOC that starts, stops and resets every other IOC in GNU Screen sessions, and publishes a status and heartbeat for each. It is the only thing that starts or stops IOC sessions
- **Commander**: `tools/ioc_cli.py` is a curses terminal interface, run on the manager's host, that controls IOCs through the manager's PVs and shows their logs and PVs
- **Archiver**: Automatic data logging with configurable deadband and time intervals
- **Status IOC**: State machine management for complex experimental procedures

## Running IOCs

Each top-level key in `settings.yaml` (everything but `general`) defines one IOC. There are
three ways to run them, all of which end up calling `master_ioc.py -i <name>`.

### Directly, in the foreground

```bash
python master_ioc.py -i pfeiffer-26x_1
```

The IOC runs in the current terminal and drops into the `softioc` interactive shell, where
`softioc.dbl()` lists its PVs. This is the way to bring up a new device or debug a driver.

### With the IOC manager

`ioc_manager.py` is an IOC whose job is running the other IOCs. It creates a
`<PREFIX>:MAN` device with, for every IOC in `settings.yaml`:

| PV | Type | Meaning |
|---|---|---|
| `<PREFIX>:MAN:<name>_control` | mbbOut | Write `0` Stop, `1` Run, `2` Reset. Reads back `Stop` or `Run` as the IOC's actual state |
| `<PREFIX>:MAN:<name>_status` | mbbIn | `Stopped`, `Starting`, `Running`, `Stale` (MINOR), `Failed` (MAJOR) or `Stopping` |
| `<PREFIX>:MAN:<name>_msg` | stringIn | Short explanation of the status, e.g. `No update for 42 s` |
| `<PREFIX>:MAN:<name>_hb` | longIn | Seconds since that IOC last updated its `_time` PV; MINOR alarm once stale |

Plus two PVs for the manager as a whole:

| PV | Type | Meaning |
|---|---|---|
| `<PREFIX>:MAN:all` | mbbOut | `0` stops every IOC; `1` Run and `2` Reset apply to IOCs with `autostart: True` |
| `<PREFIX>:MAN:time` | aIn | Timestamp of the manager's last heartbeat pass |

Each IOC is started in its own detached GNU Screen session named after the settings key, with
the session log written to `general.log_dir` (the previous run's log is kept as `<name>.1`).
An IOC counts as `Running` once its `_time` PV answers, and `Failed` if it doesn't within
`general.start_timeout` seconds (default 30); a failed session is left open so the error can be
read. An IOC is `Stale` when its `_time` PV hasn't updated for `stale_after` seconds (set per IOC,
default `max(3 × delay, delay + 10)`). IOC sessions keep running if the manager stops, and a
restarted manager picks them up again.

Commands are applied in order per IOC and never block the manager, so writing `Run` to an IOC
that is already running does nothing, and `Run` works on a crashed IOC even if `_control`
still reads `Run`.

```bash
./start_ioc_manager.sh                  # manager itself, in a screen session named "ioc-manager"
caput MYLAB:MAN:all 1                   # start everything marked autostart
caput MYLAB:MAN:pfeiffer-26x_1_control 0   # stop one IOC
screen -r pfeiffer-26x_1                # attach to a running IOC
```

GNU Screen is required (`sudo apt install screen`).

### With the Commander TUI

```bash
./commander.sh          # == python tools/ioc_cli.py
```

Commander is a curses interface that runs on the same machine as the IOC manager; use it
over SSH to work remotely. It starts, stops and restarts IOCs by writing to the manager's
PVs, so the manager must be running for those keys to work. Commander can start, stop and
restart the manager itself. The main view lists every IOC with the manager's status,
heartbeat and last log line, and the status bar shows the manager's message for the selected
IOC. Without a manager, the status column falls back to whether a screen session exists.

| Key | Action |
|---|---|
| `↑` / `↓` | Select an IOC |
| `Enter` | View that IOC's PVs, with live values and alarm severity; `Enter` again sets a writable PV |
| `p` | View all active PVs across all running IOCs |
| `s` / `x` / `r` | Start / stop / restart the selected IOC, via the manager |
| `l` | Tail the selected IOC's log |
| `a` | Attach to its screen session (returns to Commander on detach) |
| `S` / `X` | Start all autostart IOCs / stop every IOC (asks first), via the manager |
| `m` / `M` / `R` | Start / stop / restart the IOC manager (stop and restart ask first; IOCs keep running) |
| `?` | Help page |
| `q` / `Esc` | Quit, or go back from a sub-view |

PV names for the PV views come from each IOC's log, where the manager lists them with
`softioc.dbl()` once the IOC is up.

## Tools

The `tools/` directory holds everything that is not part of an IOC itself:

- **`ioc_cli.py`** — the Commander TUI described above. Launched by `commander.sh`.
- **`latency_test.py`** — writes random values to a target PV and measures the round-trip time
  to the monitor callback. Set `TARGET_PV` and `ITERATIONS` at the top of the file.
- **`stress_test.py`** — fires a burst of back-to-back writes at a PV and reports the latency
  distribution, to find where an IOC's update loop starts falling behind.


## Creating a New Deployment

This repository is both a working IOC deployment and the starting point for new ones. A
deployment is this framework plus the configuration for one lab or experiment: the PV prefix,
the device list in `settings.yaml`, any site-specific drivers in `logic_devices/`, and the
CS-Studio/Phoebus screens in `gui/`. None of the framework code needs to be renamed — the
prefix is read from `settings.yaml` at runtime — so a new deployment is a clone with its own
configuration and its own GitHub repository.

### 1. Clone and rename

```bash
git clone --recurse-submodules https://github.com/jdmax/softioc_toolkit.git mylab_ioc
cd mylab_ioc
```

### 2. Decide what to do with the history

Start a fresh history if the new deployment should not carry this repository's commits:

```bash
rm -rf .git
git init
git submodule add https://github.com/jdmax/epics-device-lib.git devices
git add .
git commit -m "Initial commit: new deployment from softioc_toolkit"
```

Alternatively, keep the history and simply repoint the remote (see step 3). Keeping it makes it
easy to merge later framework updates; discarding it gives a clean log for the new lab.

### 3. Create the GitHub repository

```bash
gh repo create <org>/mylab_ioc --private --source=. --remote=origin --push
```

Or create the repository through the GitHub web interface and point this clone at it:

```bash
git remote set-url origin https://github.com/<org>/mylab_ioc.git   # or: git remote add origin ...
git push -u origin main
```

If you kept the original history, also keep a link to the framework so improvements can be
pulled in later:

```bash
git remote add upstream https://github.com/jdmax/softioc_toolkit.git
git fetch upstream
git merge upstream/main        # when you want framework updates
```

### 4. Configure the deployment

| What to change | Where | Notes |
|---|---|---|
| PV prefix | `settings.yaml` → `general.prefix` | Prefixes every PV; used by `master_ioc.py`, `ioc_manager.py`, and `tools/ioc_cli.py` |
| Device list | `settings.yaml` | Delete the example devices, then add one top-level key per IOC with its `module`, `ip`, `port`, `channels`, and `records` |
| Archiver | `settings.yaml` → `archiver` | Set `archive_path`, `deadband`, `time_increment` |
| Status IOC | `settings.yaml` → `status` and `logic_devices/states.yaml` | Update `full_status` PVs and the state/alarm table, or remove the `status` block entirely if the deployment has no state machine |
| Site-specific drivers | `logic_devices/` | Keep `archiver.py` and `status_ioc.py`; add or remove others as needed |
| Operator screens | `gui/` | `liquifier.bob` is specific to this deployment — replace it with your own screens; `archive.bob` is generic |
| Logging | `settings.yaml` → `general.log_dir`, `epics_addr_list` | `logs/` and `archive/data/` are gitignored and created at runtime |
| Project identity | `README.md`, `LICENSE` | Describe the new deployment and set the correct copyright holder |

### 5. Keep the shared driver library shared

The `devices/` submodule stays pointed at
[epics-device-lib](https://github.com/jdmax/epics-device-lib) — do not fork it per deployment.
New generic instrument drivers belong upstream in that library so every deployment benefits;
each deployment pins the submodule to whichever tag it has been tested against:

```bash
cd devices && git fetch --tags && git checkout <tag> && cd ..
git add devices && git commit -m "Pin epics-device-lib to <tag>"
```

Only drivers that make no sense outside this one deployment go in `logic_devices/`.

### 6. Verify

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python master_ioc.py -i <device_name>      # start one IOC in the foreground
caget <PREFIX>:<DEVICE>:<CHANNEL>          # confirm the PVs appear
./start_ioc_manager.sh                     # start all autostart IOCs under screen
./commander.sh                             # curses monitor for the running IOCs
```

## Adding a New Instrument Driver

New generic drivers belong in the `epics-device-lib` library, not this repo:

1. Add the driver to `devices/instruments/` in the [epics-device-lib](https://github.com/jdmax/epics-device-lib) repo, commit, and tag a new version.
2. Update the submodule pin here: `cd devices && git checkout <new-tag> && cd .. && git add devices && git commit`
3. Add the corresponding `module: 'devices.instruments.<name>'` entry to `settings.yaml`.

For drivers that are specific to this deployment, add them to `logic_devices/` instead.

## Configuration

Each device is configured in `settings.yaml` with:
- Network connection parameters (IP, port, timeout)
- Channel mappings and names
- EPICS record properties (limits, units, descriptions)
- Device-specific settings

## Data Archiving

The built-in archiver automatically logs PV values to CSV files when:
- Value changes exceed configurable deadband threshold
- Maximum time interval expires
- Includes web-based viewer for historical data analysis


## Author
Written in 2023-2025 by J. Maxwell (https://orcid.org/0000-0003-2710-4646).

## Citation

Further details are available in our paper:

J. Maxwell, "EPICS for small-scale laboratories with Python soft IOCs," *J. Instrum.* **21** P03030 (2026). [https://doi.org/10.1088/1748-0221/21/03/P03030](https://doi.org/10.1088/1748-0221/21/03/P03030)

## License

MIT License - see [LICENSE](LICENSE) file for details.

