"""Minimal 34465A/34470A SCPI simulator for exercising the GUI without hardware.

Implements only the commands keysight_daq.instrument sends, over a raw socket:

    python tools/dmm_simulator.py                # listens on 127.0.0.1:5025
    keysight-daq --resource TCPIP::127.0.0.1::5025::SOCKET

Readings are produced in real time at the programmed sample interval. DC voltage is a
1 V offset with a 50 Hz sine, 2 kHz ripple, and noise; DC current is a 10 Hz pulse train.
Timing limits follow the manual: aperture 20 µs to 1 s, NPLC MIN = 0.001, plus a fixed
per-sample overhead.
"""

from __future__ import annotations

import argparse
import math
import re
import socketserver
import threading
import time

import numpy as np

LINE_FREQ = 60.0
OVERHEAD = 3e-6                 # per-sample time beyond the aperture
APER_MIN, APER_MAX = 20e-6, 1.0
NPLC_VALUES = (0.001, 0.002, 0.006, 0.02, 0.06, 0.2, 1, 10, 100)
OVERLOAD = 9.9e37


class SimulatedDMM:
    def __init__(self, model: str, options: str):
        self.model = model
        self.options = options
        self.memory = 2_000_000 if "MEM" in options else 50_000
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.func = "VOLT"
        self.range = {"VOLT": 1000.0, "CURR": 1.0}
        self.nplc = {"VOLT": 10.0, "CURR": 10.0}
        self.aper = {"VOLT": 0.1, "CURR": 0.1}
        self.aper_enabled = {"VOLT": False, "CURR": False}
        self.terminals = 3
        self.samp_count = 1
        self.samp_source = "IMM"
        self.samp_tim = 1.0
        self.trig_delay = 0.0
        self.trig_source = "IMM"
        self.armed = False
        self.swapped = False
        self.real = False
        self.errors: list[str] = []
        self.ques_event = 0
        self.memory_buf = np.empty(0)
        self.t0 = None
        self.produced = 0
        self.target = 0
        self.rng = np.random.default_rng()

    # --- Timing model -------------------------------------------------------------------

    def aperture(self) -> float:
        f = self.func
        return self.aper[f] if self.aper_enabled[f] else self.nplc[f] / LINE_FREQ

    def min_interval(self) -> float:
        return math.ceil((self.aperture() + OVERHEAD) * 1e6 - 1e-6) * 1e-6

    def interval(self) -> float:
        return self.samp_tim if self.samp_source == "TIM" else self.min_interval()

    def measuring(self) -> bool:
        return self.armed or (self.t0 is not None and self.produced < self.target)

    def _produce(self) -> None:
        """Generate the readings that would have completed by now."""
        if self.t0 is None:
            return
        elapsed = time.monotonic() - self.t0 - self.trig_delay
        due = min(self.target, max(0, math.floor(elapsed / self.interval()) + 1))
        if due <= self.produced:
            return
        t = np.arange(self.produced, due) * self.interval()
        self.produced = due
        new = self._signal(t)
        buf = np.concatenate((self.memory_buf, new))
        if len(buf) > self.memory:
            buf = buf[-self.memory:]
            self.ques_event |= 1 << 14
        self.memory_buf = buf

    def _signal(self, t: np.ndarray) -> np.ndarray:
        noise_scale = math.sqrt(20e-6 / self.aperture())
        if self.func == "VOLT":
            v = 1.0 + 0.5 * np.sin(2 * np.pi * 50 * t) + 0.02 * np.sin(2 * np.pi * 2000 * t)
            v += self.rng.normal(0, 2e-4 * noise_scale * max(1.0, self.range["VOLT"] / 10), len(t))
            full_scale = self.range["VOLT"] * 1.2
        else:
            v = np.where((t * 10) % 1 < 0.2, 0.08, 0.005) + self.rng.normal(0, 2e-6 * noise_scale, len(t))
            full_scale = (10.0 if self.terminals == 10 else self.range["CURR"]) * 1.2
        v[np.abs(v) > full_scale] = OVERLOAD
        if (np.abs(v) >= OVERLOAD).any():
            self.ques_event |= 1 if self.func == "VOLT" else 2
        return v

    # --- SCPI ---------------------------------------------------------------------------

    def error(self, code: int, text: str) -> None:
        self.errors.append(f'{code:+d},"{text}"')

    def handle(self, cmd: str) -> bytes | str | None:
        with self.lock:
            self._produce()
            return self._dispatch(cmd)

    def _value(self, arg: str, lo: float, hi: float, default: float) -> float:
        arg = arg.strip().upper()
        if arg.startswith("MIN"):
            return lo
        if arg.startswith("MAX"):
            return hi
        if arg.startswith("DEF"):
            return default
        return float(arg)

    def _dispatch(self, raw: str) -> bytes | str | None:
        cmd = raw.strip()
        head, _, arg = cmd.partition(" ")
        h = re.sub(r"^:?(SENS(E)?:)?", "", head.upper())

        if h == "*IDN?":
            return f"Keysight Technologies,{self.model},MY00000000,A.03.02-02.40-03.15-00.52-04-03"
        if h == "*OPT?":
            return self.options
        if h == "*RST":
            self.reset()
            return None
        if h == "*CLS":
            self.errors.clear()
            self.ques_event = 0
            return None
        if h == "*OPC?":
            return "1"
        if h in ("SYST:ERR?", "SYST:ERR:NEXT?"):
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if h in ("FUNC", "FUNC:ON"):
            name = arg.strip().strip('"').upper()
            if name.startswith("VOLT"):
                self.func = "VOLT"
            elif name.startswith("CURR"):
                self.func = "CURR"
            else:
                self.error(-224, "Illegal parameter value")
            return None
        m = re.fullmatch(r"(VOLT|CURR)(:DC)?:(RANG|TERM|IMP:AUTO|ZERO:AUTO|APER|NPLC)(\?)?", h)
        if m:
            fn, setting, query = m.group(1), m.group(3), m.group(4)
            if setting == "RANG":
                if query:
                    return f"{self.range[fn]:+.8E}"
                self.range[fn] = self._value(arg, 0.1 if fn == "VOLT" else 1e-6, 1000.0 if fn == "VOLT" else 3.0, 1.0)
            elif setting == "TERM":
                self.terminals = int(float(arg))
            elif setting == "APER":
                value = self._value(arg.split()[0] if query and arg else (arg or "DEF"), APER_MIN, APER_MAX, 0.1)
                if query:
                    return f"{value if arg else self.aper[fn]:+.8E}"
                if not APER_MIN - 1e-12 <= value <= APER_MAX:
                    self.error(-222, "Data out of range")
                else:
                    self.aper[fn] = round(value / 2e-6) * 2e-6
                    self.aper_enabled[fn] = True
            elif setting == "NPLC":
                if query:
                    return f"{self.nplc[fn]:+.8E}"
                value = self._value(arg, NPLC_VALUES[0], NPLC_VALUES[-1], 10)
                self.nplc[fn] = min(NPLC_VALUES, key=lambda v: abs(v - value))
                self.aper_enabled[fn] = False
            return None
        if h == "TRIG:SOUR":
            self.trig_source = "BUS" if arg.strip().upper().startswith("BUS") else "IMM"
            return None
        if h == "TRIG:COUN":
            return None
        if h == "*TRG":
            if self.armed:
                self.armed = False
                self.t0 = time.monotonic()
            else:
                self.error(-211, "Trigger ignored")
            return None
        if h == "TRIG:DEL":
            self.trig_delay = self._value(arg, 0, 3600, 1.0)
            return None
        if h == "SAMP:COUN":
            self.samp_count = int(self._value(arg, 1, 1e9, 1))
            return None
        if h == "SAMP:COUN?":
            return f"{self.samp_count:+d}"
        if h == "SAMP:SOUR":
            self.samp_source = "TIM" if arg.upper().startswith("TIM") else "IMM"
            return None
        if h == "SAMP:TIM?":
            if arg.strip().upper().startswith("MIN"):
                return f"{self.min_interval():+.8E}"
            return f"{self.samp_tim:+.8E}"
        if h == "SAMP:TIM":
            value = round(self._value(arg, self.min_interval(), 3600, 1.0) * 1e6) * 1e-6
            if value < self.min_interval() - 1e-12:
                self.error(-221, "Settings conflict;cannot meet requested timing;SAMP:TIM changed")
                value = self.min_interval()
            self.samp_tim = value
            return None
        if h in ("FORM", "FORM:DATA"):
            self.real = arg.upper().startswith("REAL")
            return None
        if h == "FORM:BORD":
            self.swapped = arg.upper().startswith("SWAP")
            return None
        if h in ("INIT", "INIT:IMM"):
            if self.samp_source == "TIM" and self.samp_tim < self.min_interval() - 1e-12:
                self.ques_event |= 1 << 2
            self.memory_buf = np.empty(0)
            self.produced = 0
            self.target = self.samp_count
            self.armed = self.trig_source == "BUS"
            self.t0 = None if self.armed else time.monotonic()
            return None
        if h == "ABOR":
            self.armed = False
            self.target = self.produced
            return None
        if h == "DATA:POIN?":
            return f"{len(self.memory_buf):+d}"
        if h == "DATA:REM?":
            count = int(arg.split(",")[0])
            if count > len(self.memory_buf):
                self.error(-230, "Data corrupt or stale")
                return ""
            out, self.memory_buf = self.memory_buf[:count], self.memory_buf[count:]
            return self._format(out)
        if h == "STAT:OPER:COND?":
            return f"{16 if self.measuring() else 0:+d}"
        if h in ("STAT:QUES:EVEN?", "STAT:QUES?"):
            value, self.ques_event = self.ques_event, 0
            return f"{value:+d}"
        self.error(-113, "Undefined header")
        return None

    def _format(self, values: np.ndarray) -> bytes | str:
        if not self.real:
            return ",".join(f"{v:+.8E}" for v in values)
        data = values.astype("<f8" if self.swapped else ">f8").tobytes()
        size = str(len(data))
        return f"#{len(size)}{size}".encode() + data


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        dmm: SimulatedDMM = self.server.dmm
        self.server.log(f"client connected: {self.client_address[0]}:{self.client_address[1]}")
        for line in self.rfile:
            for cmd in line.decode(errors="replace").strip().split(";"):
                if not cmd.strip():
                    continue
                reply = dmm.handle(cmd)
                if self.server.verbose:
                    self.server.log(f"<< {cmd.strip()}")
                if reply is None:
                    continue
                payload = reply if isinstance(reply, bytes) else reply.encode()
                self.wfile.write(payload + b"\n")
        self.server.log("client disconnected")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, dmm: SimulatedDMM, verbose: bool):
        super().__init__(address, Handler)
        self.dmm = dmm
        self.verbose = verbose

    def log(self, msg: str) -> None:
        print(f"[sim {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5025)
    parser.add_argument("--model", default="34465A", choices=("34465A", "34470A", "34461A"))
    parser.add_argument("--no-mem", action="store_true", help="simulate without the MEM option (50k readings)")
    parser.add_argument("--no-dig", action="store_true", help="simulate without the DIG option")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every command")
    args = parser.parse_args()

    options = f"0,0,{'0' if args.no_dig else 'DIG'},{'0' if args.no_mem else 'MEM'}"
    with Server((args.host, args.port), SimulatedDMM(args.model, options), args.verbose) as server:
        server.log(f"{args.model} ({options}) listening on {args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
