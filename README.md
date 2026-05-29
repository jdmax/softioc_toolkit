# EPICS IOC Framework in Python

A Python-based EPICS IOC (Input/Output Controller) framework for scientific instrumentation control and data acquisition using [pythonSoftIOC](https://diamondlightsource.github.io/pythonSoftIOC/master/index.html). Designed for physics experiments requiring integration of multiple devices including temperature controllers, pressure gauges, power supplies, and motor controllers.

## Features

- **Modular Device Support**: Extensible driver architecture supporting Modbus, Telnet, and custom protocols
- **YAML Configuration**: Simple configuration management via `settings.yaml`
- **Data Archiving**: Built-in data logging with deadband logic and web-based viewer
- **Status Management**: Automated state machine for experimental procedures
- **GUI Integration**: CS-Studio/Phoebus display files included
- **IOC Management**: Centralized control of multiple IOC instances

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
├── devices/              ← git submodule: epics-device-lib (shared instrument drivers)
│   ├── base_device.py    ← BaseDevice ABC
│   ├── modbus_base.py    ← ModbusDevice + ModbusConnection
│   ├── telnet_base.py    ← TelnetDevice + TelnetConnection
│   └── instruments/      ← concrete drivers (ls218, dat8024, zaber_motor, …)
├── logic_devices/        ← deployment-specific drivers (not in the shared library)
│   ├── archiver.py       ← EPICS PV archiver
│   ├── status_ioc.py     ← experimental state machine
│   └── states.yaml       ← alarm/setpoint table for target states
├── master_ioc.py         ← IOC entry point
└── settings.yaml         ← device configuration
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
- **Archiver**: Automatic data logging with configurable deadband and time intervals
- **Status IOC**: State machine management for complex experimental procedures

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

