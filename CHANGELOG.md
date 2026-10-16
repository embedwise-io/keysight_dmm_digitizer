# Changelog

Notable changes to Keysight Truevolt Digitizer. Versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.1] — 2026-10-16

### Fixed

- **Clipped control panel.** With larger desktop fonts, the control panel clipped labels
  (e.g. "Integration") and squeezed its dropdowns. The panel now sizes itself to its
  contents, can't be dragged narrower than that, and remembers the width you set.
- **Forced-wide window.** The plot toolbar and statistics line no longer force a wide
  window. Toolbar controls that don't fit move to a **»** overflow menu, and the statistics
  line wraps.

### Changed

- **Instrument details.** They fit on three lines: the main firmware revision (e.g.
  A.03.03) and the memory size (e.g. 2M). Hover for the full details. Saved files still
  record the full firmware string.
- **Plot toolbar.** It now uses flat toolbar buttons, with dividers between groups of
  controls.

## [1.0.0] — 2026-10-16

This is the first release. It is a desktop GUI and Python driver for high-speed digitizing
of DC voltage and DC current with Keysight Truevolt 34465A and 34470A DMMs over LAN.

### Instrument control

- **Connection.** Connect by IP address, hostname, or VISA resource string over HiSLIP,
  VXI-11, or a raw socket, or find instruments with **Scan**. It uses the pure-Python
  pyvisa-py backend, so the Keysight IO Libraries are not required.
- **Instrument details.** The app reads the model, serial number, firmware, and options.
  Sample-rate and memory limits follow the DIG and MEM options.
- **DC voltage:** 100 mV to 1000 V ranges, with 10 MΩ or >10 GΩ input impedance.
- **DC current:** 1 µA to 3 A ranges, plus the 10 A terminal.
- **Sample rates:** the front-panel Digitize 1-2-5 steps, from 10 Hz to 50 kHz.
- **Acquisition time:** up to the reading memory. With MEM, that is 2,000,000 readings,
  or 40 s at 50 kHz.
- **Integration:**
  - **Full interval** sets the aperture to fill each sample interval, for the lowest noise.
  - **Minimum** uses NPLC 0.001, the same settings as front-panel Digitize mode.
- **Trigger modes:**
  - **Auto** repeats captures back-to-back until Stop.
  - **Manual** arms the DMM. Each **Trigger** press takes one capture, and the DMM
    re-arms until Stop.
- **Data transfer.** Readings stream to the PC in binary while the DMM is still sampling,
  and the plot updates live. If you stop part-way through a capture, the last complete
  capture stays on screen.
- **Warnings.** Overloads, sample-timing violations, memory overflow, and instrument errors
  are reported as warnings.

### Plotting

- **Zoom and view.** Zoom with the buttons, mouse wheel, or Box Zoom, and use **Fit** to
  show everything. The statistics line covers the displayed window, and the cursor
  readout gives the sample number, time, and value.
- **Styles:** Line, Line + points, or Points.
- **Overload gaps.** Overload readings appear as gaps, highlighted with red bands.
  **◀ ▶** buttons step from gap to gap, and the gaps are counted in the statistics line.
- **Large captures.** 2,000,000-point captures stay responsive.

### Saving

- **What to save.** **Save View** writes only the displayed time window; **Save All** writes
  the whole capture.
- **Formats.**
  - **CSV** has `time_s` and value columns.
  - **HDF5** is binary, lossless, and compressed: about 4–8× smaller than CSV, and readable
    by HDF5 1.14 and later.
- **Metadata.** Every file records the instrument, serial number, range, integration,
  sample interval, start time, and software version.

### Also included

- **Driver.** `keysight_daq.instrument` can be used from scripts and notebooks without the GUI.
- **Simulator.** `tools/dmm_simulator.py` simulates a DMM, for working without hardware.
- **Pinned dependencies.** `uv.lock` pins exact package versions for `uv sync`. Python 3.14
  is pinned in `.python-version`, and Python 3.12 or later is supported.

### Verified

Tested on a 34465A with the DIG and MEM options (firmware A.03.03), on Fedora 43:

- DC voltage and current, from 50 Hz to 50 kHz.
- Auto and Manual trigger modes.
- A 2,000,000-point, 40 s capture at 50 kSa/s, with no reading-memory overflow.
- With the inputs shorted on the 1 V range at 50 kSa/s, Minimum integration gave about
  23 µV rms noise, matching a front-panel Digitize capture taken the same way. All
  integration settings came out at or below the manual's figures.

### Known limitations

- **Untested setups.** The 34470A, USB connections, and Windows and macOS have not been tested.
- **Triggers.** Only immediate and software (Trigger button) triggers are available.
  External and level triggers, with pre-trigger capture, are planned. See Future
  development in the README.
- **Capture length.** A capture is limited to the DMM's reading memory.
