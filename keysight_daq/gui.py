"""PyQt6 front end for digitizing with a Keysight 34465A / 34470A over LAN."""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyvisa
from PyQt6.QtCore import QObject, QSettings, QThread, QTimer, Qt, pyqtSignal as Signal, pyqtSlot as Slot
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget,
    QWidgetAction,
)

from . import export
from .instrument import (
    MEMORY_MEM_OPTION, MIN_RATE, MAX_RATE_DIG, RANGES, Acquisition, CaptureHooks, DeviceInfo, DigitizerConfig,
    Function, Integration, Timing, TriggerSource, TruevoltDMM, range_label, resource_for,
)

PROTOCOLS = ("HiSLIP", "VXI-11", "Socket")
TRIGGER_MODES = (("Auto (continuous)", TriggerSource.IMMEDIATE), ("Manual", TriggerSource.BUS))
LIVE_PLOT_PERIOD = 0.25        # s between plot refreshes while acquiring
HISTORY_LENGTH = 10
DEFAULT_RANGE = {Function.DCV: 10.0, Function.DCI: 0.1}
PLOT_STYLES = ("Line", "Line + points", "Points")
TRACE_COLOR = "#f0c000"
MARKER_SIZE = 5
MARKER_MIN_SPACING = 3.0       # px between samples before "Line + points" draws markers


class TraceItem(pg.PlotDataItem):
    """Evenly sampled trace whose auto-range bounds cover the full data set.

    With clip-to-view, PlotDataItem measures only the visible slice, so Fit, the "A" button
    and "View All" can't zoom back out. Even sample spacing makes full-data bounds cheap.
    """

    def dataBounds(self, ax, frac=1.0, orthoRange=None):
        x, y = self.xData, self.yData
        if x is None or len(x) == 0:
            return None, None
        if ax == 0:
            return float(x[0]), float(x[-1])
        if orthoRange is not None and len(x) > 1 and x[1] > x[0]:
            dx = x[1] - x[0]
            start = max(0, math.ceil((orthoRange[0] - x[0]) / dx))
            stop = min(len(y), math.floor((orthoRange[1] - x[0]) / dx) + 1)
            y = y[start:stop]
        finite = y[np.isfinite(y)]
        if finite.size == 0:
            return None, None
        return float(finite.min()), float(finite.max())


class _WorkerHooks(CaptureHooks):
    """Relays driver callbacks (worker thread) to Qt signals, and GUI requests back to the driver."""

    def __init__(self, worker: InstrumentWorker):
        self.worker = worker
        self._last_progress = 0.0

    def wait_for_trigger(self) -> bool:
        w = self.worker
        w._trigger.clear()
        w.armed.emit()
        while not w._stop.is_set():
            if w._trigger.wait(0.05):
                return True
        return False

    def started(self, timing: Timing) -> None:
        self._last_progress = 0.0
        self.worker.capture_started.emit(timing)

    def progress(self, values: np.ndarray, timing: Timing) -> None:
        now = time.monotonic()
        if now - self._last_progress >= LIVE_PLOT_PERIOD:
            self._last_progress = now
            self.worker.progress.emit(values, timing)

    def should_stop(self) -> bool:
        return self.worker._stop.is_set()


class InstrumentWorker(QObject):
    """Owns the VISA session. Every slot runs on the worker thread so the UI never blocks on I/O."""

    scanned = Signal(list)
    connected = Signal(object)          # DeviceInfo
    disconnected = Signal()
    armed = Signal()                    # waiting for a manual trigger
    capture_started = Signal(object)    # Timing
    progress = Signal(object, object)   # values so far, Timing
    acquired = Signal(object)           # Acquisition
    run_finished = Signal()
    failed = Signal(str, str)           # title, message

    def __init__(self):
        super().__init__()
        self._rm: pyvisa.ResourceManager | None = None
        self._dmm: TruevoltDMM | None = None
        self._stop = threading.Event()
        self._trigger = threading.Event()

    # Thread-safe requests from the GUI thread while run() is busy.
    def request_stop(self) -> None:
        self._stop.set()

    def request_trigger(self) -> None:
        self._trigger.set()

    def _resource_manager(self) -> pyvisa.ResourceManager:
        if self._rm is None:
            self._rm = pyvisa.ResourceManager()
        return self._rm

    @Slot()
    def scan(self) -> None:
        try:
            found = sorted(set(self._resource_manager().list_resources("?*::INSTR")))
        except Exception as e:
            self.failed.emit("Scan failed", str(e))
            found = []
        self.scanned.emit(found)

    @Slot(str)
    def open_session(self, resource: str) -> None:
        self.shutdown()
        try:
            dmm = TruevoltDMM(resource, self._resource_manager())
            info = dmm.open()
        except Exception as e:
            self.failed.emit("Connection failed", f"{resource}\n\n{e}")
            self.disconnected.emit()
            return
        self._dmm = dmm
        self.connected.emit(info)

    @Slot()
    def close_session(self) -> None:
        self.shutdown()
        self.disconnected.emit()

    def shutdown(self) -> None:
        if self._dmm is not None:
            try:
                self._dmm.close()
            except Exception:
                pass
            self._dmm = None

    @Slot(object)
    def run(self, cfg: DigitizerConfig) -> None:
        """Configure once, then capture repeatedly (re-arming for each manual trigger) until stopped."""
        self._stop.clear()
        hooks = _WorkerHooks(self)
        try:
            self._dmm.configure(cfg)
            while not self._stop.is_set():
                acq = self._dmm.capture(hooks)
                if acq is None:
                    break
                self.acquired.emit(acq)
        except Exception as e:
            self.failed.emit("Acquisition failed", str(e) or type(e).__name__)
        self.run_finished.emit()


class MainWindow(QMainWindow):
    request_scan = Signal()
    request_open = Signal(str)
    request_close = Signal()
    request_run = Signal(object)

    def __init__(self, resource: str | None = None, protocol: str | None = None):
        super().__init__()
        self.setWindowTitle("Keysight Truevolt Digitizer")
        self.settings = QSettings("EmbedWise", "KeysightDAQ")
        self.info: DeviceInfo | None = None
        self.acq: Acquisition | None = None      # capture on display
        self._busy = False                       # scanning, connecting, or disconnecting
        self._running = False                    # between Run and the end of the run
        self._stopping = False
        self._armed = False                      # waiting for the Trigger button
        self._capturing = False                  # readings are being taken
        self._run_captures = 0                   # complete captures in this run
        self._new_run = False                    # next capture is the first of a run
        self._live_time: np.ndarray | None = None
        self._display_interval: float | None = None  # sample interval of the trace on screen
        self._markers_shown: bool | None = None
        self._range_choice = {f: self.settings.value(f"range/{f.name}", DEFAULT_RANGE[f], float) for f in Function}

        self._build_ui()
        self._start_worker()
        self._restore_settings(resource, protocol)
        self._update_summary()
        self._update_controls()

    # --- Construction --------------------------------------------------------------------

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_plot())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 1000])
        self.setCentralWidget(splitter)
        self.resize(1360, 780)

        shortcuts = [
            (QKeySequence("F5"), self._on_run),
            (QKeySequence("F6"), self._on_trigger),
            (QKeySequence(QKeySequence.StandardKey.ZoomIn), lambda: self._zoom(0.5)),
            (QKeySequence(QKeySequence.StandardKey.ZoomOut), lambda: self._zoom(2.0)),
            (QKeySequence("Ctrl+0"), self._fit),
            (QKeySequence(QKeySequence.StandardKey.Save), lambda: self._save(view_only=True)),
        ]
        for key, slot in shortcuts:
            QShortcut(key, self).activated.connect(slot)

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(300)
        layout = QVBoxLayout(panel)

        # Instrument
        box = QGroupBox("Instrument")
        form = QFormLayout(box)
        self.address = QComboBox()
        self.address.setEditable(True)
        self.address.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.address.lineEdit().setPlaceholderText("IP address, hostname, or VISA resource")
        self.address.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.address.editTextChanged.connect(self._update_controls)
        form.addRow("Address", self.address)
        self.protocol = QComboBox()
        self.protocol.addItems(PROTOCOLS)
        self.protocol.setToolTip("Used when the address is an IP or hostname")
        form.addRow("Protocol", self.protocol)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setToolTip("Discover LAN instruments (VXI-11 broadcast and mDNS)")
        self.scan_btn.clicked.connect(self._on_scan)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        row = QHBoxLayout()
        row.addWidget(self.scan_btn)
        row.addWidget(self.connect_btn, 1)
        form.addRow(row)
        self.device_label = QLabel("Not connected")
        self.device_label.setWordWrap(True)
        self.device_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.device_label)
        layout.addWidget(box)

        # Measurement
        box = QGroupBox("Measurement")
        form = QFormLayout(box)
        self.function = QComboBox()
        for f in Function:
            self.function.addItem(f.label, f)
        self.function.currentIndexChanged.connect(self._on_function_changed)
        form.addRow("Function", self.function)
        self.range = QComboBox()
        self.range.currentIndexChanged.connect(self._on_range_changed)
        form.addRow("Range", self.range)
        self.impedance = QComboBox()
        self.impedance.addItem("10 MΩ", False)
        self.impedance.addItem("> 10 GΩ (100 mV – 10 V)", True)
        self.impedance_label = QLabel("Input Z")
        form.addRow(self.impedance_label, self.impedance)
        self.integration = QComboBox()
        for mode in Integration:
            self.integration.addItem(mode.value, mode)
        self.integration.setToolTip(
            "Each reading is the input averaged over an integration window.\n"
            "Full interval: the window spans almost the whole sample interval. Lowest noise, and signal\n"
            "content between samples is averaged in rather than missed or aliased.\n"
            "Minimum: the shortest window (~17-20 µs), as the front-panel Digitize mode uses. A snapshot\n"
            "per sample; noisier. At 50 kHz both settings are the same.")
        form.addRow("Integration", self.integration)
        layout.addWidget(box)

        # Acquisition
        box = QGroupBox("Acquisition")
        form = QFormLayout(box)
        self.rate = QDoubleSpinBox()
        self.rate.setDecimals(3)
        self.rate.setRange(MIN_RATE / 1e3, MAX_RATE_DIG / 1e3)
        self.rate.setSuffix(" kHz")
        self.rate.valueChanged.connect(self._update_summary)
        form.addRow("Sample rate", self.rate)
        self.duration = QDoubleSpinBox()
        self.duration.setDecimals(3)
        self.duration.setMinimum(0.001)
        self.duration.setSuffix(" s")
        self.duration.valueChanged.connect(self._update_summary)
        form.addRow("Acquisition time", self.duration)
        self.trigger_mode = QComboBox()
        for label, source in TRIGGER_MODES:
            self.trigger_mode.addItem(label, source)
        self.trigger_mode.setToolTip(
            "Auto: Run captures back-to-back until Stop.\n"
            "Manual: Run arms the DMM; each Trigger press starts one capture, then it re-arms until Stop.")
        self.trigger_mode.currentIndexChanged.connect(self._update_controls)
        form.addRow("Trigger mode", self.trigger_mode)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        form.addRow(self.summary)
        layout.addWidget(box)

        buttons = QHBoxLayout()
        self.run_btn = self._big_button("Run", "Configure the DMM and start acquiring (F5)", self._on_run)
        self.trigger_btn = self._big_button("Trigger", "Start a capture now: Manual mode, while armed (F6)",
                                            self._on_trigger)
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.trigger_btn)
        layout.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        layout.addStretch(1)
        return panel

    @staticmethod
    def _big_button(text: str, tip: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumHeight(44)
        font = btn.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        btn.setFont(font)
        btn.setToolTip(tip)
        btn.clicked.connect(slot)
        return btn

    def _build_plot(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        bar = QHBoxLayout()
        buttons = [
            ("Zoom In", "Zoom in on time, Y fits the visible data (Ctrl++)", lambda: self._zoom(0.5)),
            ("Zoom Out", "Zoom out on time, Y fits the visible data (Ctrl+-)", lambda: self._zoom(2.0)),
            ("Fit", "Show all data (Ctrl+0)", self._fit),
        ]
        for text, tip, slot in buttons:
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        self.box_zoom_btn = QPushButton("Box Zoom")
        self.box_zoom_btn.setCheckable(True)
        self.box_zoom_btn.setToolTip("Drag a rectangle to zoom. When off, drag pans; wheel zooms; right-drag scales.")
        self.box_zoom_btn.toggled.connect(self._on_box_zoom)
        bar.addWidget(self.box_zoom_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Style"))
        self.plot_style = QComboBox()
        self.plot_style.addItems(PLOT_STYLES)
        self.plot_style.setToolTip(
            "Line + points: markers appear once you zoom in far enough to tell samples apart.\n"
            "Points: markers only. Zoomed out, each marker is the min or max of a group of samples.")
        self.plot_style.currentIndexChanged.connect(self._apply_plot_style)
        bar.addWidget(self.plot_style)
        bar.addStretch(1)
        self.save_view_btn = QPushButton("Save View…")
        self.save_view_btn.setToolTip("Save the samples in the displayed time window (Ctrl+S)")
        self.save_view_btn.clicked.connect(lambda: self._save(view_only=True))
        bar.addWidget(self.save_view_btn)
        self.save_all_btn = QPushButton("Save All…")
        self.save_all_btn.setToolTip("Save the complete capture")
        self.save_all_btn.clicked.connect(lambda: self._save(view_only=False))
        bar.addWidget(self.save_all_btn)
        layout.addLayout(bar)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("QLabel { background: #fff3cd; color: #664d03; padding: 4px; }")
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        pg.setConfigOptions(antialias=False)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setDownsampling(auto=True, mode="peak")
        self.plot.setClipToView(True)
        self.plot.getViewBox().setAutoVisible(y=True)
        self._trace_pen = pg.mkPen(TRACE_COLOR, width=1)
        self.curve = TraceItem(pen=self._trace_pen)
        self.plot.addItem(self.curve)
        self.curve.setSymbolSize(MARKER_SIZE)
        self.curve.setSymbolBrush(TRACE_COLOR)
        self.curve.setSymbolPen(None)
        vb = self.plot.getViewBox()
        vb.sigXRangeChanged.connect(lambda *_: self._stats_timer.start())
        vb.sigXRangeChanged.connect(self._update_markers)
        vb.sigResized.connect(self._update_markers)
        # pyqtgraph's context-menu "Points" option only affects curves that already have a symbol,
        # so it does nothing here; the Style box replaces it.
        plot_item = self.plot.getPlotItem()
        for action in plot_item.ctrlMenu.actions():
            submenu = action.menu()
            if submenu and any(isinstance(a, QWidgetAction) and a.defaultWidget() is plot_item.ctrl.pointsGroup
                               for a in submenu.actions()):
                action.setVisible(False)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        layout.addWidget(self.plot, 1)

        info = QHBoxLayout()
        self.stats_label = QLabel()
        self.stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cursor_label = QLabel()
        self.cursor_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        info.addWidget(self.stats_label, 1)
        info.addWidget(self.cursor_label)
        layout.addLayout(info)

        # Statistics over the visible window are recomputed shortly after the view stops changing.
        self._stats_timer = QTimer(self, singleShot=True, interval=100)
        self._stats_timer.timeout.connect(self._update_stats)
        return panel

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = InstrumentWorker()
        self.worker.moveToThread(self.thread)
        self.request_scan.connect(self.worker.scan)
        self.request_open.connect(self.worker.open_session)
        self.request_close.connect(self.worker.close_session)
        self.request_run.connect(self.worker.run)
        self.worker.scanned.connect(self._on_scanned)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.armed.connect(self._on_armed)
        self.worker.capture_started.connect(self._on_capture_started)
        self.worker.progress.connect(self._on_progress)
        self.worker.acquired.connect(self._on_acquired)
        self.worker.run_finished.connect(self._on_run_finished)
        self.worker.failed.connect(self._on_failed)
        self.thread.start()

    # --- Settings ------------------------------------------------------------------------

    def _restore_settings(self, resource: str | None, protocol: str | None) -> None:
        s = self.settings
        history = s.value("history", [], list)
        self.address.addItems(history)
        self.address.setEditText(resource or (history[0] if history else ""))
        self.protocol.setCurrentText(protocol or s.value("protocol", PROTOCOLS[0], str))
        function = Function.__members__.get(s.value("function", "DCV", str), Function.DCV)
        self.function.setCurrentIndex(self.function.findData(function))
        self._on_function_changed()
        self.impedance.setCurrentIndex(s.value("impedance", 0, int))
        self.integration.setCurrentIndex(s.value("integration", 0, int))
        self.plot_style.setCurrentIndex(s.value("plot_style", 0, int))
        self.trigger_mode.setCurrentIndex(s.value("trigger_mode", 0, int))
        self.rate.setValue(s.value("rate_khz", 50.0, float))
        self.duration.setValue(s.value("duration_s", 1.0, float))
        if geometry := s.value("geometry"):
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("protocol", self.protocol.currentText())
        s.setValue("function", self.current_function().name)
        for f, value in self._range_choice.items():
            s.setValue(f"range/{f.name}", value)
        s.setValue("impedance", self.impedance.currentIndex())
        s.setValue("integration", self.integration.currentIndex())
        s.setValue("plot_style", self.plot_style.currentIndex())
        s.setValue("trigger_mode", self.trigger_mode.currentIndex())
        s.setValue("rate_khz", self.rate.value())
        s.setValue("duration_s", self.duration.value())
        s.setValue("geometry", self.saveGeometry())

    def _remember_address(self, text: str) -> None:
        history = [text] + [h for h in self.settings.value("history", [], list) if h != text]
        self.settings.setValue("history", history[:HISTORY_LENGTH])

    # --- State ---------------------------------------------------------------------------

    def current_function(self) -> Function:
        return self.function.currentData()

    def current_config(self) -> DigitizerConfig:
        return DigitizerConfig(
            function=self.current_function(),
            range=self.range.currentData(),
            sample_rate=self.rate.value() * 1e3,
            duration=self.duration.value(),
            integration=self.integration.currentData(),
            high_impedance=self.current_function() is Function.DCV and self.impedance.currentData(),
            trigger_source=self.trigger_mode.currentData(),
        )

    def _update_controls(self) -> None:
        connected = self.info is not None
        idle = not self._busy and not self._running
        has_address = bool(self.address.currentText().strip())
        self.address.setEnabled(idle and not connected)
        self.scan_btn.setEnabled(idle and not connected)
        self.protocol.setEnabled(idle and not connected and "::" not in self.address.currentText())
        self.connect_btn.setEnabled(idle and (connected or has_address))
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        for w in (self.function, self.range, self.integration, self.rate, self.duration, self.trigger_mode):
            w.setEnabled(not self._running)
        is_dcv = self.current_function() is Function.DCV
        self.impedance.setEnabled(not self._running and is_dcv)
        self.impedance_label.setEnabled(is_dcv)
        self.run_btn.setText("Stop" if self._running else "Run")
        self.run_btn.setEnabled(connected and not self._busy and not self._stopping)
        self.trigger_btn.setEnabled(self._armed)
        has_data = self.acq is not None and len(self.acq.values) > 0 and not self._capturing
        self.save_view_btn.setEnabled(has_data)
        self.save_all_btn.setEnabled(has_data)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_controls()

    def _update_summary(self) -> None:
        cfg = self.current_config()
        memory = self.info.memory_depth if self.info else MEMORY_MEM_OPTION
        max_duration = memory * cfg.interval
        self.duration.setMaximum(max_duration)  # clamps the value when the rate goes up
        samples = max(2, round(self.duration.value() / cfg.interval))
        rate = 1.0 / cfg.interval
        self.summary.setText(
            f"Interval {cfg.interval * 1e6:,.0f} µs ({pg.siFormat(rate, precision=5, suffix='Sa/s')})\n"
            f"{samples:,} samples · max {max_duration:,.4g} s"
            + ("" if self.info else " (2M memory assumed)"))

    # --- Instrument panel ----------------------------------------------------------------

    def _on_scan(self) -> None:
        self._set_busy(True)
        self.statusBar().showMessage("Scanning for instruments…")
        self.request_scan.emit()

    @Slot(list)
    def _on_scanned(self, found: list) -> None:
        self._set_busy(False)
        current = self.address.currentText()
        existing = {self.address.itemText(i) for i in range(self.address.count())}
        self.address.addItems([r for r in found if r not in existing])
        self.address.setEditText(current or (found[0] if found else ""))
        if found:
            self.statusBar().showMessage(f"Found {len(found)} instrument(s); pick one from the Address list.", 8000)
            self.address.showPopup()
        else:
            self.statusBar().showMessage("No instruments found. Enter the DMM's IP address "
                                         "(front panel: [Utility] > I/O Config > LAN Settings).", 10000)

    def _on_connect(self) -> None:
        if self.info is not None:
            self._set_busy(True)
            self.request_close.emit()
            return
        text = self.address.currentText().strip()
        resource = resource_for(text, self.protocol.currentText())
        self._set_busy(True)
        self.statusBar().showMessage(f"Connecting to {resource}…")
        self._remember_address(text)
        self.request_open.emit(resource)

    @Slot(object)
    def _on_connected(self, info: DeviceInfo) -> None:
        self.info = info
        self.device_label.setText(info.summary())
        self.rate.setMaximum(info.max_sample_rate / 1e3)
        msg = f"Connected to {info.model} at {info.resource}"
        if not info.has_dig:
            msg += " — DIG option not found, sample rate limited to 5 kHz (a firmware update adds DIG)"
        self.statusBar().showMessage(msg, 10000)
        self._set_busy(False)
        self._update_summary()

    @Slot()
    def _on_disconnected(self) -> None:
        if self.info is not None:
            self.statusBar().showMessage("Disconnected", 5000)
        self.info = None
        self.device_label.setText("Not connected")
        self.rate.setMaximum(MAX_RATE_DIG / 1e3)
        self._set_busy(False)
        self._update_summary()

    # --- Measurement panel ---------------------------------------------------------------

    def _on_function_changed(self) -> None:
        f = self.current_function()
        wanted = self._range_choice[f]
        self.range.blockSignals(True)
        self.range.clear()
        for value, label in RANGES[f]:
            self.range.addItem(label, value)
        index = next((i for i, (v, _) in enumerate(RANGES[f]) if math.isclose(v, wanted)), 0)
        self.range.setCurrentIndex(index)
        self.range.blockSignals(False)
        self._update_controls()

    def _on_range_changed(self) -> None:
        if self.range.currentData() is not None:
            self._range_choice[self.current_function()] = self.range.currentData()

    # --- Acquisition ---------------------------------------------------------------------

    def _on_run(self) -> None:
        if self._running:
            if not self._stopping:
                self._stopping = True
                self.worker.request_stop()
                self.statusBar().showMessage("Stopping…")
            self._armed = False
            self._update_controls()
            return
        if self.info is None or self._busy:
            return
        self._running = True
        self._run_captures = 0
        self._new_run = True
        self.warning_label.hide()
        self._set_progress(busy=True)
        self.statusBar().showMessage("Configuring…")
        self._update_controls()
        self.request_run.emit(self.current_config())

    def _on_trigger(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.worker.request_trigger()
        self.statusBar().showMessage("Triggered")
        self._update_controls()

    @Slot()
    def _on_armed(self) -> None:
        if self._stopping:
            return
        self._armed = True
        self._set_progress(text="Armed — press Trigger (F6)")
        self.statusBar().showMessage(f"Armed, waiting for trigger (captures so far: {self._run_captures})")
        self._update_controls()

    @Slot(object)
    def _on_capture_started(self, timing: Timing) -> None:
        self._armed = False
        self._capturing = True
        self._live_time = np.arange(timing.num_samples) * timing.interval
        self._display_interval = timing.interval
        self._update_markers()
        self._set_progress(maximum=timing.num_samples)
        if self._new_run:
            # Fit the first capture of a run; later captures keep whatever view the user chose.
            self._new_run = False
            cfg = self.current_config()
            self.plot.setLabel("left", cfg.function.label.split()[-1], units=cfg.function.unit)
            self.plot.setTitle(None)
            vb = self.plot.getViewBox()
            end = (timing.num_samples - 1) * timing.interval
            vb.setLimits(xMin=0, xMax=end, minXRange=5 * timing.interval)
            vb.setXRange(0, end, padding=0)
            vb.enableAutoRange(y=True)
        self.statusBar().showMessage(f"Capture {self._run_captures + 1}: acquiring…")
        self._update_controls()

    @Slot(object, object)
    def _on_progress(self, values: np.ndarray, timing: Timing) -> None:
        if self._live_time is None:
            return
        n = len(values)
        self.curve.setData(self._live_time[:n], values)
        self.progress.setValue(n)
        self.statusBar().showMessage(f"Capture {self._run_captures + 1}: {n:,} / {timing.num_samples:,} samples")

    @Slot(object)
    def _on_acquired(self, acq: Acquisition) -> None:
        self._capturing = False
        self._live_time = None
        if not acq.complete and self._stopping and self._run_captures and self.acq is not None:
            # Stopped part-way through a capture: keep the last complete one on display.
            self._show(self.acq)
            self.statusBar().showMessage(f"Stopped; showing capture {self._run_captures} "
                                         "(the interrupted capture was discarded)", 10000)
            return
        if acq.complete:
            self._run_captures += 1
        self._show(acq)
        n = len(acq.values)
        number = self._run_captures if acq.complete else self._run_captures + 1
        self.statusBar().showMessage(f"Capture {number}: {n:,} samples in {n * acq.interval:.4g} s"
                                     f"{'' if acq.complete else ' (partial)'}", 10000)

    def _show(self, acq: Acquisition) -> None:
        self.acq = acq
        self._display_interval = acq.interval
        self._update_markers()
        n = len(acq.values)
        self.curve.setData(acq.time, acq.values)
        if n > 1:
            self.plot.getViewBox().setLimits(xMin=0, xMax=(n - 1) * acq.interval, minXRange=5 * acq.interval)
        self.plot.setTitle(
            f"{acq.device.model} · {acq.config.function.label} {range_label(acq.config.function, acq.config.range)}"
            f" · {pg.siFormat(acq.sample_rate, precision=5, suffix='Sa/s')} · {acq.timing.integration}"
            f" · {acq.started:%Y-%m-%d %H:%M:%S}", size="9pt")
        self._set_progress(maximum=max(1, acq.timing.num_samples), value=n)
        if acq.warnings:
            self.warning_label.setText("⚠ " + "\n⚠ ".join(acq.warnings))
        self.warning_label.setVisible(bool(acq.warnings))
        self._update_controls()
        self._update_stats()

    @Slot()
    def _on_run_finished(self) -> None:
        stopped = self._stopping
        self._running = self._stopping = self._armed = self._capturing = False
        if self.acq is not None:
            self._set_progress(maximum=max(1, self.acq.timing.num_samples), value=len(self.acq.values))
        else:
            self._set_progress()
        if stopped and self.statusBar().currentMessage() == "Stopping…":
            self.statusBar().showMessage(f"Stopped after {self._run_captures} capture(s)", 10000)
        self._update_controls()
        self._update_stats()

    @Slot(str, str)
    def _on_failed(self, title: str, message: str) -> None:
        if self._running:
            message += "\n\nIf the instrument stays unresponsive, disconnect and reconnect."
        self.statusBar().showMessage(title, 10000)
        QMessageBox.critical(self, title, message)

    def _set_progress(self, maximum: int = 1, value: int = 0, busy: bool = False, text: str = "%p%") -> None:
        self.progress.setRange(0, 0 if busy else maximum)
        self.progress.setValue(value)
        self.progress.setFormat(text)

    # --- Plot ----------------------------------------------------------------------------

    def _zoom(self, factor: float) -> None:
        vb = self.plot.getViewBox()
        (x0, x1), _ = vb.viewRange()
        center, half = (x0 + x1) / 2, (x1 - x0) * factor / 2
        vb.setXRange(center - half, center + half, padding=0)
        vb.enableAutoRange(y=True)

    def _fit(self) -> None:
        self.plot.getViewBox().autoRange(padding=0.02)
        self.plot.getViewBox().enableAutoRange(y=True)

    def _apply_plot_style(self) -> None:
        self.curve.setPen(None if self.plot_style.currentText() == "Points" else self._trace_pen)
        self._update_markers()

    def _update_markers(self, *_) -> None:
        style = self.plot_style.currentText()
        show = style == "Points" or (style == "Line + points" and self._samples_resolved())
        if show != self._markers_shown:
            self._markers_shown = show
            self.curve.setSymbol("o" if show else None)

    def _samples_resolved(self) -> bool:
        """True when adjacent samples are at least MARKER_MIN_SPACING pixels apart on screen."""
        if not self._display_interval:
            return False
        vb = self.plot.getViewBox()
        (x0, x1), _ = vb.viewRange()
        samples = (x1 - x0) / self._display_interval
        return samples > 0 and vb.width() / samples >= MARKER_MIN_SPACING

    def _on_box_zoom(self, enabled: bool) -> None:
        vb = self.plot.getViewBox()
        vb.setMouseMode(pg.ViewBox.RectMode if enabled else pg.ViewBox.PanMode)

    def _visible_range(self) -> tuple[int, int]:
        """Sample index range [start, stop) inside the displayed time window."""
        acq = self.acq
        x0, x1 = self.plot.getViewBox().viewRange()[0]
        start = max(0, math.ceil(x0 / acq.interval - 1e-6))
        stop = min(len(acq.values), math.floor(x1 / acq.interval + 1e-6) + 1)
        return start, max(start, stop)

    def _update_stats(self) -> None:
        if self.acq is None or self._capturing:
            return
        start, stop = self._visible_range()
        window = self.acq.values[start:stop]
        finite = window[np.isfinite(window)]
        if finite.size == 0:
            self.stats_label.setText(f"View: {len(window):,} samples")
            return
        unit = self.acq.unit
        fmt = lambda v: pg.siFormat(float(v), precision=6, suffix=unit)
        lo, hi = finite.min(), finite.max()
        self.stats_label.setText(
            f"View: {len(window):,} samples   mean {fmt(finite.mean())}   σ {fmt(finite.std())}   "
            f"min {fmt(lo)}   max {fmt(hi)}   p-p {fmt(hi - lo)}")

    def _on_mouse_moved(self, pos) -> None:
        vb = self.plot.getViewBox()
        if self.acq is None or self._capturing or not vb.sceneBoundingRect().contains(pos):
            return
        x = vb.mapSceneToView(pos).x()
        i = round(x / self.acq.interval)
        if 0 <= i < len(self.acq.values):
            t = pg.siFormat(i * self.acq.interval, precision=7, suffix="s")
            v = pg.siFormat(float(self.acq.values[i]), precision=7, suffix=self.acq.unit)
            self.cursor_label.setText(f"#{i:,}   t = {t}   {v}")

    def _save(self, view_only: bool) -> None:
        acq = self.acq
        if acq is None or self._capturing or len(acq.values) == 0:
            return
        start, stop = self._visible_range() if view_only else (0, len(acq.values))
        if stop <= start:
            QMessageBox.information(self, "Nothing to save", "No samples are inside the displayed time window.")
            return
        name = f"{acq.device.model}_{acq.config.function.name}_{acq.started:%Y%m%d-%H%M%S}"
        name += "_view.csv" if view_only else ".csv"
        directory = self.settings.value("save_dir", str(Path.home()), str)
        path, selected = QFileDialog.getSaveFileName(
            self, "Save displayed data" if view_only else "Save all data",
            str(Path(directory) / name), export.FILE_FILTERS)
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() not in (".csv", ".npz"):
            path = path.with_suffix(".npz" if "npz" in selected else ".csv")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            count = export.save(path, acq, start, stop)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.settings.setValue("save_dir", str(path.parent))
        self.statusBar().showMessage(f"Saved {count:,} samples to {path}", 10000)

    # --- Shutdown ------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._save_settings()
        self.worker.request_stop()
        # quit() takes effect once the running slot returns; then the session can be closed from here.
        self.thread.quit()
        self.thread.wait(30_000)
        self.worker.shutdown()
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Digitize with a Keysight 34465A/34470A over LAN.")
    parser.add_argument("--resource", help="IP address, hostname, or VISA resource string to prefill")
    parser.add_argument("--protocol", choices=PROTOCOLS, help="LAN protocol used with an IP/hostname")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Keysight Truevolt Digitizer")
    window = MainWindow(args.resource, args.protocol)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
