"""Digitizing driver for Keysight Truevolt 34465A / 34470A DMMs.

The front-panel "Digitize" mode is not available over SCPI, so this module builds the
equivalent configuration by hand (fixed range, autozero off, timed sampling) and then
streams readings out of reading memory with DATA:REMove? while the acquisition runs.

configure() sets the instrument up once; capture() then takes one acquisition and can be
repeated (continuous captures, or re-arming for the next manual trigger). The driver has
no GUI dependencies, so it can also be used from scripts or notebooks:

    from keysight_daq.instrument import DigitizerConfig, Function, TruevoltDMM

    with TruevoltDMM("TCPIP::192.168.1.50::hislip0::INSTR") as dmm:
        acq = dmm.acquire(DigitizerConfig(Function.DCV, range=10, sample_rate=50e3, duration=1.0))
    t, v = acq.time, acq.values
"""

from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pyvisa

SUPPORTED_MODELS = ("34465A", "34470A")

# Reading memory depth with and without the MEM option.
MEMORY_STANDARD = 50_000
MEMORY_MEM_OPTION = 2_000_000

# Fastest timed sampling with the DIG option (0.001 PLC); without it the limit is 5 kHz.
MAX_RATE_DIG = 50_000.0
MAX_RATE_STD = 5_000.0

# Overloaded readings are returned as +/-9.9E37.
OVERLOAD_THRESHOLD = 9.0e37

# Questionable Data register bits.
QUES_VOLT_OVERLOAD = 1 << 0
QUES_CURR_OVERLOAD = 1 << 1
QUES_TIMING_VIOLATION = 1 << 2
QUES_MEMORY_OVERFLOW = 1 << 14
# Standard Operation register bits.
OPER_MEASURING = 1 << 4

APERTURE_STEP = 2e-6          # aperture resolution
APERTURE_MAX = 1.0
TRANSFER_CHUNK = 200_000      # readings per DATA:REMove? (1.6 MB as REAL,64)
POLL_INTERVAL = 0.1           # s between reading-memory polls while acquiring
STALL_TIMEOUT = 5.0           # s without new readings before checking whether the DMM stopped


class InstrumentError(RuntimeError):
    """The instrument rejected a command or returned something unexpected."""


class Function(enum.Enum):
    DCV = ("VOLT", "V", "DC Voltage")
    DCI = ("CURR", "A", "DC Current")

    def __init__(self, scpi: str, unit: str, label: str):
        self.scpi = scpi
        self.unit = unit
        self.label = label


class Integration(enum.Enum):
    AUTO = "Full interval (lowest noise)"
    MINIMUM = "Minimum (front-panel Digitize)"


class TriggerSource(enum.Enum):
    IMMEDIATE = ("IMM", "Immediate")    # sampling starts as soon as the DMM is initiated
    BUS = ("BUS", "Software (*TRG)")    # DMM is armed and waits for *TRG

    def __init__(self, scpi: str, label: str):
        self.scpi = scpi
        self.label = label


# (range, label) per function. 10 A is selected with the 10 A terminal, not RANGe.
RANGES: dict[Function, list[tuple[float, str]]] = {
    Function.DCV: [(0.1, "100 mV"), (1.0, "1 V"), (10.0, "10 V"), (100.0, "100 V"), (1000.0, "1000 V")],
    Function.DCI: [
        (1e-6, "1 µA"), (10e-6, "10 µA"), (100e-6, "100 µA"), (1e-3, "1 mA"), (10e-3, "10 mA"),
        (0.1, "100 mA"), (1.0, "1 A"), (3.0, "3 A"), (10.0, "10 A (10 A terminal)"),
    ],
}


def range_label(function: Function, value: float) -> str:
    return next((label for v, label in RANGES[function] if math.isclose(v, value)), f"{value:g} {function.unit}")


def resource_for(target: str, protocol: str) -> str:
    """Turn an IP address / hostname into a VISA resource string; full resource strings pass through."""
    target = target.strip()
    if "::" in target:
        return target
    return {
        "HiSLIP": f"TCPIP::{target}::hislip0::INSTR",
        "VXI-11": f"TCPIP::{target}::inst0::INSTR",
        "Socket": f"TCPIP::{target}::5025::SOCKET",
    }[protocol]


@dataclass(frozen=True)
class DeviceInfo:
    resource: str
    manufacturer: str
    model: str
    serial: str
    firmware: str
    options: frozenset[str]

    @classmethod
    def parse(cls, resource: str, idn: str, opt: str) -> DeviceInfo:
        parts = [p.strip() for p in idn.split(",")] + [""] * 4
        # *OPT? returns e.g. "0,0,DIG,MEM,0", with 0 in place of each missing option.
        options = frozenset(o.strip() for o in opt.split(",") if o.strip() not in ("", "0"))
        return cls(resource, parts[0], parts[1], parts[2], parts[3], options)

    @property
    def has_dig(self) -> bool:
        return "DIG" in self.options

    @property
    def memory_depth(self) -> int:
        return MEMORY_MEM_OPTION if "MEM" in self.options else MEMORY_STANDARD

    @property
    def max_sample_rate(self) -> float:
        return MAX_RATE_DIG if self.has_dig else MAX_RATE_STD

    def summary(self) -> str:
        opts = ", ".join(sorted(self.options)) or "none"
        return (f"{self.model} · S/N {self.serial}\n"
                f"Firmware {self.firmware}\n"
                f"Options {opts} · {self.memory_depth:,}-reading memory")


@dataclass(frozen=True)
class DigitizerConfig:
    function: Function
    range: float
    sample_rate: float                      # requested, Hz
    duration: float                         # requested, s
    integration: Integration = Integration.AUTO
    high_impedance: bool = False            # DCV only: >10 GΩ on the 100 mV to 10 V ranges
    trigger_source: TriggerSource = TriggerSource.IMMEDIATE

    @property
    def interval(self) -> float:
        """Requested sample interval, rounded to the 1 µs sample-timer resolution."""
        return max(1, round(1e6 / self.sample_rate)) * 1e-6


@dataclass(frozen=True)
class Timing:
    """What the instrument actually accepted for a DigitizerConfig."""
    interval: float
    num_samples: int
    integration: str
    warnings: tuple[str, ...]


@dataclass
class Acquisition:
    config: DigitizerConfig
    device: DeviceInfo
    timing: Timing
    values: np.ndarray                      # V or A; overloads are NaN
    started: datetime                       # when sampling was triggered
    warnings: list[str] = field(default_factory=list)

    @property
    def interval(self) -> float:
        return self.timing.interval

    @property
    def sample_rate(self) -> float:
        return 1.0 / self.timing.interval

    @property
    def unit(self) -> str:
        return self.config.function.unit

    @property
    def complete(self) -> bool:
        return len(self.values) == self.timing.num_samples

    @property
    def time(self) -> np.ndarray:
        return np.arange(len(self.values)) * self.timing.interval

    def metadata(self) -> dict[str, str]:
        cfg = self.config
        meta = {
            "instrument": f"{self.device.manufacturer} {self.device.model}",
            "serial": self.device.serial,
            "firmware": self.device.firmware,
            "resource": self.device.resource,
            "started": self.started.isoformat(timespec="milliseconds"),
            "trigger": cfg.trigger_source.label,
            "function": cfg.function.label,
            "range": range_label(cfg.function, cfg.range),
            "sample_interval_s": f"{self.timing.interval:.9g}",
            "sample_rate_hz": f"{self.sample_rate:.9g}",
            "integration": self.timing.integration,
            "samples": str(len(self.values)),
        }
        if cfg.function is Function.DCV:
            meta["input_impedance"] = ">10 GOhm (100 mV-10 V)" if cfg.high_impedance else "10 MOhm"
        return meta


class CaptureHooks:
    """Observe and control a capture. Methods are called on the thread running capture()."""

    def wait_for_trigger(self) -> bool:
        """The DMM is armed (software trigger). Return True to trigger now, False to abort."""
        return True

    def started(self, timing: Timing) -> None:
        """Sampling has started."""

    def progress(self, values: np.ndarray, timing: Timing) -> None:
        """Called after each transfer with all values received so far."""

    def should_stop(self) -> bool:
        """Return True to abort; the readings already taken are kept."""
        return False


class TruevoltDMM:
    """One VISA session to a 34465A/34470A."""

    def __init__(self, resource: str, rm: pyvisa.ResourceManager | None = None, timeout_ms: int = 10_000):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._rm = rm or pyvisa.ResourceManager()
        self._inst = None
        self.info: DeviceInfo | None = None
        self._config: DigitizerConfig | None = None
        self._timing: Timing | None = None

    def __enter__(self) -> TruevoltDMM:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Session -------------------------------------------------------------------------

    def open(self) -> DeviceInfo:
        inst = self._rm.open_resource(self.resource, open_timeout=self.timeout_ms)
        inst.timeout = self.timeout_ms
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        self._inst = inst
        try:
            idn = self.query("*IDN?")
            model = idn.split(",")[1].strip() if idn.count(",") >= 1 else idn
            if model not in SUPPORTED_MODELS:
                raise InstrumentError(f"Unsupported instrument '{idn}'. Expected a 34465A or 34470A.")
            self.info = DeviceInfo.parse(self.resource, idn, self.query("*OPT?"))
            self.write("*CLS")
        except Exception:
            self.close()
            raise
        return self.info

    def close(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            finally:
                self._inst = None
                self.info = None
                self._config = self._timing = None

    @property
    def is_open(self) -> bool:
        return self._inst is not None

    def write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd))

    def query_int(self, cmd: str) -> int:
        return int(float(self.query(cmd)))

    def errors(self) -> list[str]:
        """Drain the instrument error queue."""
        errs = []
        for _ in range(32):
            err = self.query("SYST:ERR?")
            if err.startswith(("+0", "0")):
                break
            errs.append(err)
        return errs

    def abort(self) -> None:
        """Best-effort return to idle after an interrupted transfer."""
        for action in (self._inst.clear, lambda: self.write("ABOR")):
            try:
                action()
            except Exception:
                pass

    # --- Digitizing ----------------------------------------------------------------------

    def configure(self, cfg: DigitizerConfig) -> Timing:
        """Reset and set up timed digitizing. Settings the instrument can't meet are adjusted and reported."""
        f = cfg.function.scpi
        warnings: list[str] = []

        self.write("*RST")
        self.write("*CLS")
        self.write(f'FUNC "{f}:DC"')
        if cfg.function is Function.DCI:
            if cfg.range >= 10:
                self.write("CURR:DC:TERM 10")
            else:
                self.write("CURR:DC:TERM 3")
                self.write(f"CURR:DC:RANG {cfg.range:g}")
        else:
            self.write(f"VOLT:DC:RANG {cfg.range:g}")
            self.write(f"VOLT:DC:IMP:AUTO {'ON' if cfg.high_impedance else 'OFF'}")
        self.write(f"{f}:DC:ZERO:AUTO OFF")
        self.write(f"TRIG:SOUR {cfg.trigger_source.scpi}")
        self.write("TRIG:COUN 1")
        self.write("TRIG:DEL 0")
        # Timed sampling needs a sample count > 1 before the timer limits apply; the real count is set below.
        self.write("SAMP:COUN 2")
        self.write("SAMP:SOUR TIM")

        # The shortest integration time sets the fastest possible sample interval.
        integration = self._set_min_nplc(f)
        fastest = math.ceil(self.query_float("SAMP:TIM? MIN") * 1e6 - 1e-3) * 1e-6
        interval = cfg.interval
        if interval < fastest:
            interval = fastest
            warnings.append(f"Sample rate reduced to {1e-3 / interval:.4g} kHz, the fastest this "
                            f"instrument allows ({interval * 1e6:.0f} µs interval).")
        if cfg.integration is Integration.AUTO:
            integration = self._fit_aperture(f, interval) or integration
        self.write(f"SAMP:TIM {interval:.6f}")
        interval = self.query_float("SAMP:TIM?")

        memory = self.info.memory_depth
        num_samples = max(2, round(cfg.duration / interval))
        if num_samples > memory:
            num_samples = memory
            warnings.append(f"Sample count limited to the {memory:,}-reading memory "
                            f"({memory * interval:.4g} s at this rate).")
        self.write(f"SAMP:COUN {num_samples}")
        self.write("FORM:DATA REAL,64")
        self.write("FORM:BORD SWAP")  # little-endian, so no byte swapping on the host

        errs = self.errors()
        # -221 "Settings conflict" means the instrument adjusted a value; anything else is a real failure.
        rejected = [e for e in errs if not e.startswith("-221")]
        if rejected:
            raise InstrumentError("Instrument rejected the configuration:\n" + "\n".join(rejected))
        warnings += [f"Instrument adjusted a setting: {e}" for e in errs if e.startswith("-221")]
        self._config = cfg
        self._timing = Timing(interval, num_samples, integration, tuple(warnings))
        return self._timing

    def _fit_aperture(self, f: str, interval: float) -> str | None:
        """Integrate over all but one aperture step of each sample interval.

        A longer aperture lowers noise and acts as a boxcar anti-alias filter. An aperture equal
        to the interval can make the DMM stretch the interval (error -221), hence the margin.
        Returns None (leaving the minimum NPLC in place) if no aperture fits.
        """
        aperture_min = self.query_float(f"{f}:DC:APER? MIN")
        steps = math.floor((min(interval, APERTURE_MAX + APERTURE_STEP) - APERTURE_STEP) / APERTURE_STEP + 1e-6)
        aperture = max(aperture_min, steps * APERTURE_STEP)
        for _ in range(20):
            if interval <= aperture_min + 1e-9 or aperture < aperture_min - 1e-9:
                break
            self.write(f"{f}:DC:APER {aperture:.6f}")
            self.write(f"SAMP:TIM {interval:.6f}")
            errs = self.errors()
            rejected = [e for e in errs if not e.startswith("-221")]
            if rejected:
                raise InstrumentError("Instrument rejected the integration time:\n" + "\n".join(rejected))
            if not errs:
                return f"aperture {aperture * 1e6:,.0f} µs"
            # Back off by one step, or 0.1 % for long apertures where timer rounding is coarser.
            aperture -= max(1, round(aperture * 1e-3 / APERTURE_STEP)) * APERTURE_STEP
        self._set_min_nplc(f)
        return None

    def _set_min_nplc(self, f: str) -> str:
        self.write(f"{f}:DC:NPLC MIN")
        return f"NPLC {self.query_float(f'{f}:DC:NPLC?'):g} (minimum)"

    def acquire(self, cfg: DigitizerConfig, hooks: CaptureHooks | None = None) -> Acquisition | None:
        """configure() then capture(): one acquisition."""
        self.configure(cfg)
        return self.capture(hooks)

    def capture(self, hooks: CaptureHooks | None = None) -> Acquisition | None:
        """Take one acquisition with the current configuration and stream it back.

        With a software trigger the DMM is armed and hooks.wait_for_trigger() decides when to fire.
        Returns None if aborted before the trigger; a stop after the trigger returns the readings taken.
        """
        if self._timing is None:
            raise InstrumentError("configure() must be called before capture().")
        hooks = hooks or CaptureHooks()
        cfg, timing = self._config, self._timing
        total = timing.num_samples
        values = np.empty(total)
        got = 0
        stopped = False
        warnings = list(timing.warnings)

        self.write("INIT")
        try:
            if cfg.trigger_source is TriggerSource.BUS:
                if not hooks.wait_for_trigger():
                    self.write("ABOR")
                    return None
                self.write("*TRG")
            started = datetime.now().astimezone()
            hooks.started(timing)
            last_data = time.monotonic()
            while got < total:
                if not stopped and hooks.should_stop():
                    self.write("ABOR")
                    stopped = True
                    # Keep the readings already in memory.
                    total = got + self.query_int("DATA:POIN?")
                    continue
                available = self.query_int("DATA:POIN?")
                if available:
                    count = min(available, total - got, TRANSFER_CHUNK)
                    block = self._inst.query_binary_values(
                        f"DATA:REM? {count}", datatype="d", is_big_endian=False, container=np.array)
                    if len(block) != count:
                        raise InstrumentError(f"Expected {count} readings, received {len(block)}.")
                    values[got:got + count] = block
                    got += count
                    last_data = time.monotonic()
                    hooks.progress(values[:got], timing)
                    if available > count:
                        continue
                elif time.monotonic() - last_data > STALL_TIMEOUT + timing.interval:
                    if not self.query_int("STAT:OPER:COND?") & OPER_MEASURING:
                        warnings.append(f"Instrument stopped after {got:,} of {total:,} samples.")
                        break
                    last_data = time.monotonic()
                time.sleep(POLL_INTERVAL)
        except BaseException:
            self.abort()
            raise

        values = values[:got]
        if stopped:
            warnings.append(f"Stopped by user after {got:,} of {timing.num_samples:,} samples.")
        warnings += self._check_status(values)
        return Acquisition(cfg, self.info, timing, values, started, warnings)

    def _check_status(self, values: np.ndarray) -> list[str]:
        warnings = []
        overload = np.abs(values) >= OVERLOAD_THRESHOLD
        if overload.any():
            values[overload] = np.nan
            warnings.append(f"{overload.sum():,} overload readings (input exceeded range) are shown as gaps.")
        ques = self.query_int("STAT:QUES:EVEN?")
        if ques & QUES_TIMING_VIOLATION:
            warnings.append("Instrument reported a sample timing violation; sample timing may not be valid.")
        if ques & QUES_MEMORY_OVERFLOW:
            warnings.append("Reading memory overflowed; the oldest readings were lost.")
        warnings += [f"Instrument error: {e}" for e in self.errors()]
        return warnings
