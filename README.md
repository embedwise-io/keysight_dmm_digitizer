# Keysight Truevolt Digitizer

GUI for high-speed digitizing (up to 50 kSa/s) of DC voltage or DC current with a
Keysight 34465A or 34470A DMM over LAN. It configures the DMM, streams the readings
back while the acquisition runs, and plots the result with zoom and save.

## Setup

```sh
uv sync                      # creates .venv with the exact versions pinned in uv.lock
uv sync --extra usb          # also install PyUSB for USB connections
```

Dependency versions are pinned:

- **`uv.lock`** pins every package, including indirect ones, with hashes.
- **`.python-version`** pins Python 3.14.
- **`pyproject.toml`** lists the minimum versions this was tested with.

To move to newer releases, run `uv lock --upgrade` and then `uv sync`, retest, and
commit the updated `uv.lock`. Without uv, `python -m venv .venv && .venv/bin/pip install -e .`
also works, but it installs the newest compatible versions rather than the pinned ones.

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

   **Show gaps** shades missing (overload) samples with translucent red bands. Each band is
   at least 3 px wide, so a single missing sample in 100k is still visible, and bands
   closer than a pixel merge until you zoom in. The statistics line adds "N missing in K
   gaps" for the displayed window, and the cursor readout shows "overload" over a gap.
   The **◀ ▶** buttons next to it jump to the previous or next gap:
   - Each gap is centred with context: at least 200 samples, or 3× the gap length.
   - From the full view, ▶ starts at the first gap and ◀ at the last.
   - After you pan, they continue from where you're looking, and a zoom you've set is kept.
   - They are enabled only when the capture has gaps in that direction, and the status bar
     shows e.g. "Gap 3 of 9: 40 samples (800 µs) at 1.5 s".
5. **Save View…** writes only the displayed samples; **Save All…** writes the whole capture.
   **Format** chooses CSV or HDF5 (see below). Saving works when stopped, and in Manual
   mode while the DMM is armed.

Settings and recent addresses are remembered between sessions.

## Saved files

| | CSV (`.csv`) | HDF5 (`.h5`, `.hdf5`) |
|---|---|---|
| Contents | `time_s` and `voltage_V` or `current_A` columns, after `#` metadata lines | Dataset `values` (float64), with the metadata as its attributes |
| Values | 9 significant digits | Exact: binary, losslessly compressed (gzip + shuffle) |
| Size of a 50k-sample capture (shorted input) | 1.15 MB | 0.15 MB |
| Size of 2M samples (synthetic noisy data) | 39 MB (3.0 s to write) | 10 MB (0.15 s) |
| Opens in | Anything | h5py, MATLAB (`h5read`), HDFView, `h5dump` |

HDF5 files don't store a time column, because samples are evenly spaced. Rebuild it from
the `t0_s` and `sample_interval_s` attributes. `t0_s` is non-zero for a **Save View**
slice. The files use the modern HDF5 format, readable by HDF5 1.14 and later.

```python
import pandas as pd
df = pd.read_csv("capture.csv", comment="#")

import h5py, numpy as np
with h5py.File("capture.h5") as f:
    ds = f["values"]
    v = ds[...]
    t = ds.attrs["t0_s"] + np.arange(len(v)) * ds.attrs["sample_interval_s"]
    meta = dict(ds.attrs)   # instrument, serial, range, integration, units, started, ...
```

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
- **Minimum** uses NPLC 0.001 (about 17 µs at 60 Hz): a short snapshot per sample, but
  noisier. This matches front-panel Digitize mode. Its settings, read back from the meter,
  are identical, down to the zero trigger delay.
- At 50 kHz the interval is 20 µs and both settings are the same.

Noise measured on the 34465A (FW A.03.03), 1 V range, inputs shorted, 1 s captures, 60 Hz
line, 2026-10-15:

| Capture | Integration | Noise σ (rms) | Peak-to-peak | Manual figure* |
|---|---|---|---|---|
| Front-panel Digitize, 50 kHz | NPLC 0.001 | 22.8 µV | 152 µV | 30 µV |
| App, Minimum, 50 kHz (3 runs) | NPLC 0.001 | 18.1–22.9 µV | 148–175 µV | 30 µV |
| App, Minimum, 10 kHz | NPLC 0.001 | 17.8 µV | 125 µV | 30 µV |
| App, Full interval, 10 kHz | aperture 98 µs | 5.2 µV | 36 µV | 6 µV (0.006 PLC) |
| App, Full interval, 1 kHz | aperture 998 µs | 0.9 µV | 5.8 µV | 1.5 µV (0.06 PLC) |

\*The manual's 34465A resolution figure for that integration time, times the 1 V range.

- **Minimum vs. front panel:** Minimum gives the same noise as front-panel Digitize.
- **Full interval:** about 3.5× lower noise at 10 kHz and about 20× lower at 1 kHz.
- **Offsets and pickup:** offsets were +0.7 to +2.2 µV with autozero off, and 60 Hz
  pickup was 0.3 µV or less.

If the requested rate can't be met, the DMM's minimum interval is used and a warning is
shown. Overloaded readings (±9.9E37) are stored as NaN as each block arrives, so they appear
as gaps in the plot, including during live capture. They are saved as NaN in both CSV and
HDF5, and the warning reports how many readings were lost and in how many gaps.

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
- **HDF5 2.0 file format.** Exports currently use the format readable by HDF5 1.14+
  (`HDF5_LIBVER` in `export.py`). h5py already bundles HDF5 2.0, but files in its newest
  format can't be read by tools built on HDF5 1.x: Fedora's `h5dump` 1.14.6 failed on one,
  and HDFView and MATLAB are likely affected too. Once those tools support HDF5 2.0,
  re-evaluate. It is a one-line change: set the low bound to `"latest"`, check what the 2.0
  format adds, and confirm your tools open the files.

