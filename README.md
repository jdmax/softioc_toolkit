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

1. **Install dependencies**:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   The IOC manager script requires GNU Screen to be installed to manage IOC instances (sudo apt install screen).

2. **Configure devices** in `settings.yaml`:
   ```yaml
   general:
     prefix: MYLAB
   
   my_device:
     module: 'devices.ls218'
     ip: '192.168.1.100'
     port: '1001'
     channels: ['Temp1', 'Temp2']
   ```

3. **Start an IOC**:
   ```bash
   python master_ioc.py -i my_device
   ```

4. **View archive data**:
   ```bash
   cd archive
   ./start_archive_viewer.sh
   ```

## Architecture

- **Base Classes**: `BaseDevice`, `ModbusDevice`, `TelnetDevice` provide common functionality
- **Device Drivers**: Individual modules in `devices/` folder for each instrument type
- **Master IOC**: `master_ioc.py` handles IOC lifecycle and device instantiation
- **Archiver**: Automatic data logging with configurable deadband and time intervals
- **Status IOC**: State machine management for complex experimental procedures

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


## License

MIT License - see [LICENSE](LICENSE) file for details.

