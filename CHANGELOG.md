# Changelog

Notable changes to Keysight Truevolt Digitizer. Versions follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-11-02

### Added

- **Markers.** X1 and X2 mark times, and Y1 and Y2 mark levels. Each is a labelled line you
  drag to move: magenta for X, green for Y. Place them with the **Markers** button, or at a
  point from the plot's right-click menu. The Measurements panel shows:
  - the time at X1 and X2, the reading there ("overload" in a gap), ΔX = X2 − X1 with the
    difference in reading, and 1/ΔX;
  - the levels at Y1 and Y2, ΔY = Y2 − Y1 and 1/ΔY.

  Switching between DC voltage and DC current clears the Y markers, since they are levels
  in volts or amps.
- **Selection box.** With **Select** on, dragging draws a translucent box instead of
  panning. The Measurements panel shows:
  - the points in the box's time range, and any overloads there;
  - the points in the box (also between its levels);
  - the time in the box, with its share of the time range (e.g. the time above a
    threshold);
  - the min, mean, max and σ of the points in the box.

  Drag a corner to resize the box, or, with Select on, the inside to move it. **Esc**
  clears it.
- **Measurements panel.** It shows beside the plot while there are markers or a selection.
  **Pop Out** moves it into a window of its own, which can be moved on Wayland, unlike a
  floating dock. Closing that window clears the markers and the selection. **Dock** puts it
  back. Whether it is docked or popped out is remembered.
- **Tests.** A pytest suite (`uv sync --extra test && .venv/bin/pytest`) covers:
  - the analysis helpers;
  - trigger modes, gap navigation and saving, against the simulator;
  - the markers and selection box, driven with the mouse.

### Fixed

- **Plot resizing as the mouse moved.** The cursor readout under the plot changed width
  with the value under the mouse. That changed where the statistics beside it wrapped, so
  the plot grew and shrank with the number of lines. It was most noticeable in narrow
  windows, or with the Measurements panel open. The readout now keeps the width of its
  longest text, and the statistics keep the height their longest text needs at the current
  width.

### Changed

- **Style wording.** The Style option's per-sample dots are now called dots rather than
  markers, to avoid confusion with the new markers.

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
