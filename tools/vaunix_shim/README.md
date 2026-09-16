# Vaunix Lab Brick TCP shim

The Vaunix LMS/BLX Lab Brick is a USB instrument driven by a C library
(`fnLMS_*`). It has no network interface, so it cannot go behind a
serial-to-Ethernet adapter the way the other RS-232 instruments in this toolkit
do. Instead a small Linux host — a Raspberry Pi is plenty — holds the USB
connection and re-exports the device as a line protocol on a TCP socket.

```
  Lab Brick ──USB── Raspberry Pi ──Ethernet── IOC host ──CA── control room
                    vaunix_shim.py            devices.instruments.vaunix_lms
```

Everything above the Pi looks exactly like any other Telnet instrument here, so
[`vaunix_lms.py`](../../devices/instruments/vaunix_lms.py) is an ordinary
`TelnetDevice` driver.

## Why the units are converted on the Pi

The API encodes frequency as a count of 10 Hz steps and power as a count of
0.25 dB steps, and its getters return error codes *in band*, in the same integer
as real data. All of that is handled in the shim, so the wire protocol carries
plain Hz and dBm and the EPICS driver never has to know the encodings. It also
means you can debug the instrument with `nc` and read what you get.

## Install on the Pi

1. Install the Vaunix Linux SDK and put `libvnx_LMS_api.so` somewhere the
   loader will find it (`/usr/local/lib`, then `sudo ldconfig`).

2. Let non-root users talk to the device. Get the real vendor/product IDs first:

   ```bash
   lsusb        # find the Vaunix entry, e.g. "ID 041f:1234 Vaunix ..."
   ```

   then fill them into `99-vaunix.rules`, install it and replug the Brick:

   ```bash
   sudo cp 99-vaunix.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```

3. Check it comes up:

   ```bash
   python3 vaunix_shim.py --port 1701 --log-level DEBUG
   ```

   The startup line reports the model, serial, and the frequency and power
   limits the device claims. If those limits look wrong, stop — everything
   downstream range-checks against them.

4. Install the service:

   ```bash
   sudo cp vaunix-shim.service /etc/systemd/system/
   sudo systemctl enable --now vaunix-shim
   ```

## Try it without hardware

`--simulate` runs against a built-in fake LMS-203, which is enough to bring up
the IOC, exercise the encodings and click through the screens:

```bash
python3 vaunix_shim.py --port 1701 --simulate
```

## Poking at it by hand

```
$ nc 192.168.1.50 1701
*IDN?
VAUNIX,LMS-203,12345,113
LIMITS?
200000000 20000000000 -40.00 10.00
READ?
9400000000 -7.50 1 1 1
FREQ 9400000000
OK 9400000000
```

Every command returns exactly one line, including failures (`ERR <reason>`), so
the socket cannot fall out of step with the conversation.

## Things that will bite you

- **Only one process may hold the Lab Brick.** `fnLMS_InitDevice` fails outright
  if something else has it open. If Bridge12's own software also wants the
  device, that conflict has to be settled before any of this works.
- **Device IDs are not stable across a replug.** The shim addresses the device
  by serial number for that reason. With more than one Brick attached, `--serial`
  is required.
- **`PLL_LOCK_MASK` is a guess.** The status bit layout lives in
  `vnx_LMS_api.h` and has moved between library versions. Check it against the
  header shipped with your SDK before trusting `_Lock` for anything that
  matters.
- **The manual contradicts itself on frequency.** Page 4 gives 5.5 GHz as
  `550000000`; page 8 gives 6 GHz as `6000000`. Page 4 is right — the formula is
  `Hz / 10`, and the page 8 example is off by 100x.
- **Set and get are asymmetric on power.** `SetPowerLevel` takes absolute dBm,
  but `GetPowerLevel` returns power *relative to maximum*. The shim reads back
  with `GetAbsPowerLevel` so the readback actually matches the setpoint.
