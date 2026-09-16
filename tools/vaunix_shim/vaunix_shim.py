#!/usr/bin/env python3
"""
Vaunix Lab Brick LMS/BLX -> TCP shim.

Runs on a small Linux host (e.g. a Raspberry Pi) that holds the USB connection
to the Lab Brick, and exposes it as a line-oriented ASCII service so the EPICS
side can talk to it with the ordinary TelnetDevice driver pattern.

    python3 vaunix_shim.py --port 1701
    python3 vaunix_shim.py --port 1701 --simulate     # no hardware needed

Wire protocol -- one request line in, exactly one response line out, always:

    *IDN?              -> VAUNIX,<model>,<serial>,<dllver>
    READ?              -> <freq_Hz> <pow_dBm> <rf_on> <int_ref> <pll_locked>
    LIMITS?            -> <fmin_Hz> <fmax_Hz> <pmin_dBm> <pmax_dBm>
    FREQ?              -> <freq_Hz>
    FREQ <Hz>          -> OK <freq_Hz>
    POW?               -> <pow_dBm>
    POW <dBm>          -> OK <pow_dBm>
    RF?                -> 0|1
    RF <0|1>           -> OK <0|1>
    REF?               -> 0|1          (1 = internal reference)
    REF <0|1>          -> OK <0|1>
    SAVE               -> OK
    anything failing   -> ERR <message>

Units on the wire are plain Hz and dBm. The 10 Hz frequency encoding and the
0.25 dB power encoding are handled here, next to the hardware, so the EPICS
driver never has to know about them.

J. Maxwell 2026
"""
import argparse
import asyncio
import ctypes
import logging
import sys

# Vaunix LVSTATUS error codes all carry the high bit (vnx_LMS_api.h).
STATUS_ERROR_BIT = 0x80000000

# PLL lock bit within the GetDeviceStatus word. Confirm against your header.
PLL_LOCK_MASK = 0x00000040

# Encoding constants from the Lab Brick USB API manual.
FREQ_UNIT_HZ = 10.0     # frequency is an unsigned int count of 10 Hz steps
POWER_UNIT_DB = 0.25    # power is an int count of 0.25 dB steps

MAX_MODELNAME = 32

# Candidate export names, most-preferred first. The Windows and Linux SDKs do
# not agree on which calls carry the "Ex" suffix, so bind whichever is present
# rather than assuming one spelling.
SYMBOLS = {
    'set_test_mode':   ['fnLMS_SetTestMode'],
    'get_dll_version': ['fnLMS_GetDLLVersion'],
    'get_num_devices': ['fnLMS_GetNumDevices'],
    'get_dev_info':    ['fnLMS_GetDevInfo'],
    'get_model_name':  ['fnLMS_GetModelNameA', 'fnLMS_GetModelName'],
    'get_serial':      ['fnLMS_GetSerialNumber'],
    'init_device':     ['fnLMS_InitDevice'],
    'close_device':    ['fnLMS_CloseDevice'],
    'get_status':      ['fnLMS_GetDeviceStatus'],
    'set_freq':        ['fnLMS_SetFrequencyEx', 'fnLMS_SetFrequency'],
    'get_freq':        ['fnLMS_GetFrequencyEx', 'fnLMS_GetFrequency'],
    'get_min_freq':    ['fnLMS_GetMinFreqEx', 'fnLMS_GetMinFreq'],
    'get_max_freq':    ['fnLMS_GetMaxFreqEx', 'fnLMS_GetMaxFreq'],
    'set_power':       ['fnLMS_SetPowerLevel'],
    'get_power':       ['fnLMS_GetAbsPowerLevel'],   # NOT GetPowerLevel, see get_power()
    'get_min_power':   ['fnLMS_GetMinPwr'],
    'get_max_power':   ['fnLMS_GetMaxPwr'],
    'set_rf_on':       ['fnLMS_SetRFOn'],
    'get_rf_on':       ['fnLMS_GetRF_On'],
    'set_int_ref':     ['fnLMS_SetUseInternalRef'],
    'get_int_ref':     ['fnLMS_GetUseInternalRef'],
    'save_settings':   ['fnLMS_SaveSettings'],
}

DEFAULT_LIBS = ['libvnx_LMS_api.so', 'vnx_LMS_api.so', './libvnx_LMS_api.so']


class VaunixError(Exception):
    """Raised for any failure talking to the Lab Brick."""


class LabBrick:
    """Owns the USB connection to one Lab Brick, addressed by serial number.

    The API hands out device IDs as opaque handles that are not stable across a
    replug, so the serial number is the only durable way to identify a specific
    unit. Everything here works in real-world units (Hz, dBm) and converts to
    the API's encodings at the boundary.
    """

    def __init__(self, library=None, serial=None, simulate=False):
        self.serial = serial
        self.simulate = simulate
        self.dev_id = None
        self.model = 'unknown'
        self.dll_version = 0
        # Populated by _read_limits() once the device is open; used to tell a
        # real reading apart from an in-band error code.
        self.fmin_hz = self.fmax_hz = None
        self.pmin_db = self.pmax_db = None

        self.lib = self._load_library(library)
        self._bind()

    # ---------------------------------------------------------------- loading

    def _load_library(self, library):
        if self.simulate and library is None:
            logging.warning("running against the built-in fake device, no hardware involved")
            return _FakeLib()

        candidates = [library] if library else DEFAULT_LIBS
        errors = []
        for name in candidates:
            try:
                return ctypes.CDLL(name)
            except OSError as e:
                errors.append(f"{name}: {e}")
        raise VaunixError("could not load the Vaunix library (" + "; ".join(errors) + ")")

    def _bind(self):
        """Resolve each call to whichever exported name this SDK actually uses."""
        self.fn = {}
        for key, candidates in SYMBOLS.items():
            for name in candidates:
                try:
                    self.fn[key] = getattr(self.lib, name)
                    break
                except AttributeError:
                    continue
            else:
                raise VaunixError(
                    f"the loaded library exports none of {candidates} -- "
                    "is this really the LMS SDK?")

        # Only the calls whose ctypes defaults are wrong need explicit
        # signatures: a default c_int return truncates the unsigned frequency
        # counts, and bools must not be passed as ints.
        for key in ('get_freq', 'get_min_freq', 'get_max_freq'):
            self.fn[key].restype = ctypes.c_uint
        self.fn['set_freq'].argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.fn['set_test_mode'].argtypes = [ctypes.c_bool]
        self.fn['set_rf_on'].argtypes = [ctypes.c_int, ctypes.c_bool]
        self.fn['set_int_ref'].argtypes = [ctypes.c_int, ctypes.c_bool]

    # ------------------------------------------------------------- open/close

    def open(self):
        """Find the requested Lab Brick and take exclusive ownership of it."""
        # Test mode makes the library simulate rather than touch hardware. The
        # manual is emphatic that it must be set explicitly either way.
        self.fn['set_test_mode'](bool(self.simulate))
        self.dll_version = int(self.fn['get_dll_version']())

        count = int(self.fn['get_num_devices']())
        if count < 1:
            raise VaunixError("no Lab Brick found on USB")

        DevIDs = ctypes.c_uint * count
        active = DevIDs()
        found = int(self.fn['get_dev_info'](active))

        catalogue = [(active[i], int(self.fn['get_serial'](active[i])))
                     for i in range(found)]
        if not catalogue:
            raise VaunixError("no Lab Brick returned by GetDevInfo")

        if self.serial is None:
            if len(catalogue) > 1:
                raise VaunixError(
                    "several Lab Bricks are attached "
                    f"({', '.join(str(s) for _, s in catalogue)}); pick one with --serial")
            self.dev_id, self.serial = catalogue[0]
        else:
            for dev_id, serial in catalogue:
                if serial == self.serial:
                    self.dev_id = dev_id
                    break
            else:
                raise VaunixError(
                    f"serial {self.serial} is not attached "
                    f"(found {', '.join(str(s) for _, s in catalogue)})")

        # InitDevice fails outright if another process already holds the
        # device, which is the most common way this goes wrong in practice.
        status = int(self.fn['init_device'](self.dev_id))
        if status & STATUS_ERROR_BIT:
            raise VaunixError(
                f"could not open serial {self.serial} (status 0x{status:08X}) -- "
                "another program may already have it open")

        self.model = self._model_name(self.dev_id)
        self._read_limits()
        logging.info("opened %s serial %s (DLL %s), %.6f-%.6f GHz, %+.2f to %+.2f dBm",
                     self.model, self.serial, self.dll_version,
                     self.fmin_hz / 1e9, self.fmax_hz / 1e9,
                     self.pmin_db, self.pmax_db)

    def close(self):
        if self.dev_id is not None:
            try:
                self.fn['close_device'](self.dev_id)
            except Exception:
                pass
            self.dev_id = None

    def reopen(self):
        """Drop the handle and find the device again, e.g. after a replug."""
        self.close()
        self.open()

    def _model_name(self, dev_id):
        buf = ctypes.create_string_buffer(MAX_MODELNAME + 1)
        if int(self.fn['get_model_name'](dev_id, buf)) <= 0:
            return 'unknown'
        return buf.value.decode('ascii', 'replace').strip()

    def _read_limits(self):
        self.fmin_hz = int(self.fn['get_min_freq'](self.dev_id)) * FREQ_UNIT_HZ
        self.fmax_hz = int(self.fn['get_max_freq'](self.dev_id)) * FREQ_UNIT_HZ
        self.pmin_db = int(self.fn['get_min_power'](self.dev_id)) * POWER_UNIT_DB
        self.pmax_db = int(self.fn['get_max_power'](self.dev_id)) * POWER_UNIT_DB
        if not 0 < self.fmin_hz < self.fmax_hz:
            raise VaunixError("device reported nonsensical frequency limits")

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check(status, what):
        """Raise if an LVSTATUS-returning set call reported failure."""
        status = int(status) & 0xFFFFFFFF
        if status & STATUS_ERROR_BIT:
            raise VaunixError(f"{what} failed (status 0x{status:08X})")

    # ------------------------------------------------------------- parameters

    def get_frequency(self):
        """Frequency in Hz."""
        raw = int(self.fn['get_freq'](self.dev_id))
        hz = raw * FREQ_UNIT_HZ
        # Get calls return error codes in-band, in the same integer as real
        # data. Range-checking against the device's own limits is a sounder
        # test than looking at the high bit, which a reading above 21.47 GHz
        # would legitimately set once scaled into 10 Hz counts.
        if not self.fmin_hz <= hz <= self.fmax_hz:
            raise VaunixError(f"frequency read back out of range (raw 0x{raw:08X})")
        return hz

    def set_frequency(self, hz):
        if not self.fmin_hz <= hz <= self.fmax_hz:
            raise VaunixError(
                f"{hz / 1e9:.6f} GHz is outside "
                f"{self.fmin_hz / 1e9:.6f}-{self.fmax_hz / 1e9:.6f} GHz")
        self._check(self.fn['set_freq'](self.dev_id, int(round(hz / FREQ_UNIT_HZ))),
                    'set frequency')
        return self.get_frequency()

    def get_power(self):
        """Absolute output power in dBm.

        Deliberately GetAbsPowerLevel, not GetPowerLevel: the latter reports
        power *relative to maximum* (i.e. attenuation) while SetPowerLevel takes
        an absolute level, so pairing them would mean the readback never agrees
        with the setpoint.
        """
        raw = ctypes.c_int(int(self.fn['get_power'](self.dev_id))).value
        dbm = raw * POWER_UNIT_DB
        if not self.pmin_db <= dbm <= self.pmax_db:
            raise VaunixError(f"power read back out of range (raw 0x{raw & 0xFFFFFFFF:08X})")
        return dbm

    def set_power(self, dbm):
        if not self.pmin_db <= dbm <= self.pmax_db:
            raise VaunixError(
                f"{dbm:+.2f} dBm is outside {self.pmin_db:+.2f} to {self.pmax_db:+.2f} dBm")
        self._check(self.fn['set_power'](self.dev_id, int(round(dbm / POWER_UNIT_DB))),
                    'set power')
        return self.get_power()

    def _get_flag(self, key, what):
        value = int(self.fn[key](self.dev_id))
        if value not in (0, 1):
            raise VaunixError(f"{what} read back as 0x{value & 0xFFFFFFFF:08X}")
        return value

    def get_rf_on(self):
        return self._get_flag('get_rf_on', 'RF state')

    def set_rf_on(self, on):
        self._check(self.fn['set_rf_on'](self.dev_id, bool(on)), 'set RF state')
        return self.get_rf_on()

    def get_internal_ref(self):
        return self._get_flag('get_int_ref', 'reference source')

    def set_internal_ref(self, internal):
        self._check(self.fn['set_int_ref'](self.dev_id, bool(internal)), 'set reference')
        return self.get_internal_ref()

    def get_pll_locked(self):
        """PLL lock from the status word, reported as 0/1.

        The status bit layout lives in vnx_LMS_api.h and has moved between
        library versions, so treat this as advisory: check PLL_LOCK_MASK against
        the header shipped with your SDK before trusting it for an interlock.
        """
        status = int(self.fn['get_status'](self.dev_id)) & 0xFFFFFFFF
        if status & STATUS_ERROR_BIT:
            raise VaunixError(f"status read failed (0x{status:08X})")
        return 1 if status & PLL_LOCK_MASK else 0

    def save_settings(self):
        self._check(self.fn['save_settings'](self.dev_id), 'save settings')

    def read_all(self):
        """Everything the IOC polls, in one pass."""
        return (self.get_frequency(), self.get_power(), self.get_rf_on(),
                self.get_internal_ref(), self.get_pll_locked())


class ShimServer:
    """Line-protocol TCP front end for one Lab Brick.

    Several clients may connect at once -- handy for poking at the device with
    netcat while the IOC is polling -- so every command is serialised behind a
    lock. Hardware calls are pushed to a worker thread because the Vaunix
    library blocks on USB.
    """

    def __init__(self, brick):
        self.brick = brick
        self.lock = asyncio.Lock()

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')
        logging.info("client connected: %s", peer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode('ascii', 'replace').strip()
                if not line:
                    continue
                async with self.lock:
                    response = await asyncio.to_thread(self.dispatch, line)
                writer.write((response + '\n').encode('ascii'))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            logging.info("client disconnected: %s", peer)
            writer.close()

    def dispatch(self, line):
        """Run one command and return exactly one response line."""
        try:
            return self._dispatch(line)
        except VaunixError as e:
            logging.warning("%s -> %s", line, e)
            return f"ERR {e}"
        except Exception as e:                      # keep the socket usable
            logging.exception("unexpected failure handling %r", line)
            return f"ERR internal error: {e}"

    def _dispatch(self, line):
        parts = line.split()
        verb = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else None

        if verb == '*IDN?':
            return (f"VAUNIX,{self.brick.model},{self.brick.serial},"
                    f"{self.brick.dll_version}")

        if verb == 'READ?':
            freq, power, rf, ref, lock = self.brick.read_all()
            return f"{freq:.0f} {power:.2f} {rf} {ref} {lock}"

        if verb == 'LIMITS?':
            return (f"{self.brick.fmin_hz:.0f} {self.brick.fmax_hz:.0f} "
                    f"{self.brick.pmin_db:.2f} {self.brick.pmax_db:.2f}")

        if verb == 'FREQ?':
            return f"{self.brick.get_frequency():.0f}"
        if verb == 'FREQ':
            return f"OK {self.brick.set_frequency(self._number(arg, 'FREQ')):.0f}"

        if verb == 'POW?':
            return f"{self.brick.get_power():.2f}"
        if verb == 'POW':
            return f"OK {self.brick.set_power(self._number(arg, 'POW')):.2f}"

        if verb == 'RF?':
            return str(self.brick.get_rf_on())
        if verb == 'RF':
            return f"OK {self.brick.set_rf_on(self._flag(arg, 'RF'))}"

        if verb == 'REF?':
            return str(self.brick.get_internal_ref())
        if verb == 'REF':
            return f"OK {self.brick.set_internal_ref(self._flag(arg, 'REF'))}"

        if verb == 'SAVE':
            self.brick.save_settings()
            return "OK"

        raise VaunixError(f"unknown command {verb}")

    @staticmethod
    def _number(arg, verb):
        if arg is None:
            raise VaunixError(f"{verb} needs a value")
        try:
            return float(arg)
        except ValueError:
            raise VaunixError(f"{verb} value {arg!r} is not a number")

    @classmethod
    def _flag(cls, arg, verb):
        value = cls._number(arg, verb)
        if value not in (0, 1):
            raise VaunixError(f"{verb} takes 0 or 1, not {arg!r}")
        return int(value)


class _FakeLib:
    """Stand-in for the Vaunix library, for --simulate without hardware.

    Mimics an LMS-203 closely enough to exercise the protocol, the encodings and
    the driver end to end. Values are held in the library's own units, exactly
    as the real one would.
    """

    def __init__(self):
        self._freq = 1_000_000_00      # 10 GHz in 10 Hz counts
        self._power = 0                # 0 dBm in 0.25 dB counts
        self._rf = 0
        self._ref = 1

    # Names that are not defined below raise AttributeError, which is exactly
    # what _bind() needs in order to fall through to the next candidate name.

    # The real ctypes function objects carry these attributes; accept writes.
    class _Fn:
        def __init__(self, call):
            self._call = call
            self.restype = None
            self.argtypes = None

        def __call__(self, *args):
            return self._call(*args)

    def _wrap(self, call):
        return _FakeLib._Fn(call)

    # -- exported surface ---------------------------------------------------
    @property
    def fnLMS_SetTestMode(self):     return self._wrap(lambda mode: None)
    @property
    def fnLMS_GetDLLVersion(self):   return self._wrap(lambda: 113)
    @property
    def fnLMS_GetNumDevices(self):   return self._wrap(lambda: 1)
    @property
    def fnLMS_GetSerialNumber(self): return self._wrap(lambda d: 12345)
    @property
    def fnLMS_InitDevice(self):      return self._wrap(lambda d: 0)
    @property
    def fnLMS_CloseDevice(self):     return self._wrap(lambda d: 0)
    @property
    def fnLMS_GetDeviceStatus(self): return self._wrap(lambda d: PLL_LOCK_MASK)
    @property
    def fnLMS_GetMinFreqEx(self):    return self._wrap(lambda d: 20_000_000)     # 200 MHz
    @property
    def fnLMS_GetMaxFreqEx(self):    return self._wrap(lambda d: 2_000_000_000)  # 20 GHz
    @property
    def fnLMS_GetMinPwr(self):       return self._wrap(lambda d: -160)           # -40 dBm
    @property
    def fnLMS_GetMaxPwr(self):       return self._wrap(lambda d: 40)             # +10 dBm
    @property
    def fnLMS_SaveSettings(self):    return self._wrap(lambda d: 0)

    @property
    def fnLMS_GetDevInfo(self):
        def call(active):
            active[0] = 1
            return 1
        return self._wrap(call)

    @property
    def fnLMS_GetModelNameA(self):
        def call(d, buf):
            name = b'LMS-203'
            buf.value = name
            return len(name)
        return self._wrap(call)

    @property
    def fnLMS_GetFrequencyEx(self):  return self._wrap(lambda d: self._freq)
    @property
    def fnLMS_GetAbsPowerLevel(self):return self._wrap(lambda d: self._power)
    @property
    def fnLMS_GetRF_On(self):        return self._wrap(lambda d: self._rf)
    @property
    def fnLMS_GetUseInternalRef(self):return self._wrap(lambda d: self._ref)

    @property
    def fnLMS_SetFrequencyEx(self):
        def call(d, value):
            self._freq = int(value)
            return 0
        return self._wrap(call)

    @property
    def fnLMS_SetPowerLevel(self):
        def call(d, value):
            self._power = int(value)
            return 0
        return self._wrap(call)

    @property
    def fnLMS_SetRFOn(self):
        def call(d, on):
            self._rf = int(bool(on))
            return 0
        return self._wrap(call)

    @property
    def fnLMS_SetUseInternalRef(self):
        def call(d, internal):
            self._ref = int(bool(internal))
            return 0
        return self._wrap(call)


async def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bind', default='0.0.0.0', help='address to listen on')
    parser.add_argument('--port', type=int, default=1701, help='TCP port (default 1701)')
    parser.add_argument('--serial', type=int,
                        help='serial number of the Lab Brick, required if more than one')
    parser.add_argument('--library', help='path to libvnx_LMS_api.so')
    parser.add_argument('--simulate', action='store_true',
                        help='run against a fake device, no hardware needed')
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format='%(asctime)s %(levelname)s %(message)s')

    try:
        brick = LabBrick(library=args.library, serial=args.serial, simulate=args.simulate)
        brick.open()
    except VaunixError as e:
        logging.error("%s", e)
        return 1

    server = await asyncio.start_server(ShimServer(brick).handle, args.bind, args.port)
    logging.info("listening on %s:%s", args.bind, args.port)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        brick.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
