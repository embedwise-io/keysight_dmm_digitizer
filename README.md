# Keysight Truevolt Digitizer

GUI for high-speed digitizing (up to 50 kSa/s) of DC voltage or DC current with a
Keysight 34465A or 34470A DMM over LAN. It configures the DMM, streams the readings
back while the acquisition runs, and plots the result with zoom and save.

## Setup

```sh
uv venv .venv && uv pip install --python .venv/bin/python -e .
# or: python -m venv .venv && .venv/bin/pip install -e .
```

The default VISA backend is `pyvisa-py` (pure Python), so the Keysight IO Libraries are
not required. If they are installed, PyVISA uses them automatically.

## Run

```sh
.venv/bin/keysight-daq                      # or: .venv/bin/python -m keysight_daq
.venv/bin/keysight-daq --resource 10.101.0.215
```

1. Enter the DMM's IP address (or a full VISA resource string), or press **Scan**. Choose
   the protocol (HiSLIP is the default), then press **Connect**.
2. Choose the function, range, sample rate, acquisition time (s), and trigger mode. Sample
   rates follow the front-panel Digitize 1-2-5 steps, from 10 Hz to 50 kHz.
3. Press **Run** (F5):
   - **Auto (continuous):** captures repeat back-to-back until you press **Stop**.
   - **Manual:** the DMM is armed and waits. Each **Trigger** press (F6) starts one capture,
     and the DMM re-arms afterwards until you press **Stop**.

   Data plots live as it arrives. If you stop part-way through a capture, the last complete
   capture stays on screen. A partial capture is shown only if no complete capture exists.
4. Zoom with the toolbar buttons, the mouse wheel, or **Box Zoom**. A zoom set during a run
   is kept for later captures. The statistics line covers the displayed window.
   **Style** selects Line, Line + points, or Points:
   - **Line + points** adds markers once samples are at least 3 px apart on screen, so
     zoom in to see them.
   - **Points** always shows markers. Zoomed out, each marker is the min or max of a group
     of samples.
5. **Save View…** writes only the displayed samples; **Save All…** writes the whole capture.
   Saving works when stopped, and in Manual mode while the DMM is armed. CSV files carry a
   `#` metadata header (`pandas.read_csv(path, comment="#")`). Use `.npz` for large captures.

Settings and recent addresses are remembered between sessions.

## How it works

The front-panel Digitize mode can't be used over SCPI. `keysight_daq/instrument.py`
builds the same configuration from individual commands:

```
*RST; *CLS
FUNC "VOLT:DC"                   (or CURR:DC, with CURR:DC:TERM 3|10)
VOLT:DC:RANG <range>             fixed range (autorange breaks timed sampling)
VOLT:DC:ZERO:AUTO OFF
TRIG:SOUR IMM | BUS; TRIG:COUN 1; TRIG:DEL 0    Auto | Manual
SAMP:SOUR TIM
VOLT:DC:APER <t>  or  NPLC MIN   integration time (see below)
SAMP:TIM <interval>              1 µs resolution; read back with SAMP:TIM?
SAMP:COUN <n>                    capped by memory: 2,000,000 with MEM, else 50,000
FORM:DATA REAL,64; FORM:BORD SWAP

per capture:
INIT                                (Manual: wait for the Trigger button, then *TRG)
loop: DATA:POIN? -> DATA:REM? <k>   binary blocks, read while the DMM is still sampling
STAT:QUES:EVEN?; SYST:ERR?          overload, timing violation, memory overflow
```

The configuration is sent once per **Run**. Each capture is then just `INIT` (plus `*TRG`
in Manual) followed by the transfer, so Auto mode restarts in about 0.2 s.

**Integration**

Each reading is the input averaged over an integration window (the aperture), and that
window can be shorter than the sample interval.

- **Full interval** (default) sets the aperture to the interval minus one 2 µs step. The
  step avoids the `-221` settings conflict the DMM raises when they are equal. This gives
  the lowest noise, and signal content between samples is averaged in rather than missed
  or aliased.
- **Minimum** uses NPLC 0.001 (about 17 µs at 60 Hz), the same as the front-panel Digitize
  mode: a short snapshot per sample, but noisier.
- At 50 kHz the interval is 20 µs and both settings are the same.

Measured on the 34465A (10 V range, 4.2 V DC input):

| Rate | Full interval | Minimum |
|---|---|---|
| 10 kHz (100 µs) | aperture 98 µs: **~55–60 µV σ** | NPLC 0.001: ~180–215 µV σ |

If the requested rate can't be met, the DMM's minimum interval is used and a warning is
shown. Overloaded readings (±9.9E37) are stored as NaN and appear as gaps in the plot.

`instrument.py` has no GUI dependency, so you can use it from scripts or notebooks:

```python
from keysight_daq.instrument import DigitizerConfig, Function, TruevoltDMM

with TruevoltDMM("TCPIP::10.101.0.215::hislip0::INSTR") as dmm:
    acq = dmm.acquire(DigitizerConfig(Function.DCV, range=10, sample_rate=50e3, duration=2.0))
t, v = acq.time, acq.values
```

## Simulator

`tools/dmm_simulator.py` emulates the SCPI subset above over a raw socket, so you can
work on the GUI without hardware:

```sh
.venv/bin/python tools/dmm_simulator.py        # add --no-mem / --no-dig / -v
.venv/bin/keysight-daq --resource TCPIP::127.0.0.1::5025::SOCKET
```

## Notes

- Front panel: sending any SCPI command puts the DMM in Remote. Pressing **[Local]**
  afterwards clears reading memory. Don't connect while a front-panel digitize or data
  log that you still need is held in memory.
- USB: run `pip install -e .[usb]` (PyUSB), then enter the `USB0::…::INSTR` resource
  string. Scan also lists USB instruments once PyUSB is installed.

## Future development

### Shared trigger: 34465A + 34470A capturing together

Run both DMMs at once, for example voltage on one and current on the other, to get
time-aligned V, I, and P = V·I.

- **Trigger wiring.** Arm both DMMs with `TRIG:SOUR EXT` and drive their rear-panel
  **Ext Trig** BNCs from one TTL edge: a function generator, a GPIO, or a button. Polarity
  is set with `TRIG:SLOP POS|NEG`. Alternatively, trigger the master with `*TRG` and wire its
  **VM Comp** output (a 3.3 V pulse after each reading) to the slave's Ext Trig. That is
  simpler, but the slave starts about one sample late.
- **Software.** The driver already supports several `TruevoltDMM` sessions. The GUI would
  need a second instrument panel, an External trigger source, an "arm all, then trigger"
  sequence, two curves (or V/I/P plots), and a combined save with a shared time axis.
- **Caveats.**
  - Each DMM samples from its own timebase, so the clocks drift apart slowly over long
    captures. Check the timebase spec, and re-align or resample in post if needed.
  - Both meters need the same sample rate.
  - Confirm the 34470A's options with `*OPT?`: without MEM its memory is 50k readings,
    which is 1 s at 50 kHz.

### Other ideas

- **Level trigger and pretrigger.** Supported by the DIG option: `TRIG:SOUR INT`,
  `TRIG:LEV`, `TRIG:SLOP`, and `SAMP:COUN:PRET` to capture data from before the trigger.
- **Captures longer than reading memory.** Streaming already keeps up at 50 kSa/s over LAN
  (a 2M-point, 40 s capture had no overflow), so continuous logging to disk is feasible.
- **USB connection.** See the Notes section above.

