"""Markers, the selection box and the Measurements panel, driven with the mouse where it matters."""

import re
import time
from datetime import datetime

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDockWidget

from keysight_daq.gui import MainWindow
from keysight_daq.instrument import (
    Acquisition, DeviceInfo, DigitizerConfig, Function, Timing, box_stats, samples_between,
)

DEVICE = DeviceInfo("TCPIP::127.0.0.1::5025::SOCKET", "Keysight Technologies", "34465A", "MY54500244",
                    "A.03.03-02.40-03.03-00.52-02-01", frozenset({"DIG", "MEM"}))
N = 10_000                          # samples at 1 kSa/s: 10 s
OVERLOADS = (7000, 7001, 7002)      # missing samples, away from the regions the tests select
MOVE_INTERVAL = 0.015               # s between mouse moves: pyqtgraph drops moves faster than ~100 per second


def make_acquisition(function: Function = Function.DCI) -> Acquisition:
    k = np.arange(N)
    if function is Function.DCI:
        values = (k % 100) * 1e-4                          # 0 ... 9.9 mA sawtooth
    else:
        values = 4.2 + (k % 100) * 1e-4                    # 4.2 V with a 9.9 mV sawtooth
    values[list(OVERLOADS)] = np.nan
    cfg = DigitizerConfig(function, 0.01 if function is Function.DCI else 10.0, 1e3, N / 1e3)
    return Acquisition(cfg, DEVICE, Timing(1e-3, N, "aperture 998 µs", ()), values,
                       datetime(2026, 11, 2, 12, 0).astimezone())


@pytest.fixture
def win(app, tmp_path):
    w = MainWindow(settings=QSettings(str(tmp_path / "keysight.conf"), QSettings.Format.IniFormat))
    w.resize(1500, 900)
    w.show()
    w._show(make_acquisition())
    w._fit()
    settle(app)
    yield w
    w.close()


def settle(app):
    for _ in range(3):
        app.processEvents()


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.replace("&nbsp;", " "))).strip()


def readouts(app, w) -> tuple[str, str]:
    settle(app)
    m = w.measurements
    m.update_readouts()
    return plain(m.markers_label.text()), plain(m.selection_label.text())


def point(w, x: float, y: float) -> QPoint:
    """Where a data point is, in the plot widget's viewport."""
    return w.plot.mapFromScene(w.plot.getViewBox().mapViewToScene(QPointF(x, y)))


def send(widget, kind, pos: QPoint, button, buttons):
    global_pos = QPointF(widget.mapToGlobal(pos))
    QApplication.sendEvent(widget, QMouseEvent(kind, QPointF(pos), global_pos, button, buttons,
                                               Qt.KeyboardModifier.NoModifier))


def drag(app, w, a: QPoint, b: QPoint, button=Qt.MouseButton.LeftButton):
    vp = w.plot.viewport()
    send(vp, QEvent.Type.MouseMove, a, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)    # hover first
    send(vp, QEvent.Type.MouseButtonPress, a, button, button)
    for k in range(1, 11):
        time.sleep(MOVE_INTERVAL)
        send(vp, QEvent.Type.MouseMove, a + (b - a) * (k / 10), Qt.MouseButton.NoButton, button)
        app.processEvents()
    send(vp, QEvent.Type.MouseButtonRelease, b, button, Qt.MouseButton.NoButton)
    settle(app)


def x_range(w) -> list[float]:
    return w.plot.getViewBox().viewRange()[0]


# --- Markers -----------------------------------------------------------------------------

def test_markers_button_places_and_clears_them(app, win):
    m = win.measurements
    assert not win.measure_dock.isVisible()
    (x0, x1), (y0, y1) = win.plot.getViewBox().viewRange()
    win.markers_btn.click()
    settle(app)
    assert win.markers_btn.isChecked() and win.measure_dock.isVisible()
    assert m.marker("X1") == pytest.approx(x0 + 0.25 * (x1 - x0))
    assert m.marker("X2") == pytest.approx(x0 + 0.75 * (x1 - x0))
    assert y0 < m.marker("Y1") < m.marker("Y2") < y1
    assert all(line.isVisible() for line in m._lines.values())
    win.markers_btn.click()
    settle(app)
    assert not m.has_markers and not win.measure_dock.isVisible()
    assert m.marker("X1") is None and not any(line.isVisible() for line in m._lines.values())


def test_readout_values_deltas_and_reciprocals(app, win):
    m = win.measurements
    m.place_marker("X1", QPointF(2.0123, 0))     # sample 2012: 1.2 mA
    m.place_marker("X2", QPointF(2.0573, 0))     # sample 2057: 5.7 mA
    m.place_marker("Y1", QPointF(0, 0.001))
    m.place_marker("Y2", QPointF(0, 0.005))
    markers, _ = readouts(app, win)
    for text in ("2.0123 s", "2.0573 s", "45 ms",           # X1, X2, ΔX
                 "22.2222 Hz",                              # 1/ΔX
                 "Current 1.2 mA 5.7 mA 4.5 mA",            # at X1, X2 and the difference
                 "4 mA", "250.000 A⁻¹"):                    # ΔY and 1/ΔY
        assert text in markers, text


def test_marker_on_an_overload_reads_overload(app, win):
    m = win.measurements
    m.place_marker("X1", QPointF(OVERLOADS[1] * 1e-3, 0))
    m.place_marker("X2", QPointF(2.0, 0))
    markers, _ = readouts(app, win)
    assert "Current overload 0 A —" in markers


def test_dragging_a_marker_moves_it(app, win):
    m = win.measurements
    m.place_marker("X1", QPointF(3.0, 0))
    settle(app)
    drag(app, win, point(win, 3.0, 0.005), point(win, 6.0, 0.005))
    assert m.marker("X1") == pytest.approx(6.0, abs=0.05)
    assert x_range(win) == pytest.approx([0, (N - 1) * 1e-3], abs=1e-3)     # the drag didn't pan


def test_right_click_places_a_marker_there(app, win):
    m = win.measurements
    vp = win.plot.viewport()
    where = point(win, 4.0, 0.006)
    send(vp, QEvent.Type.MouseButtonPress, where, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
    send(vp, QEvent.Type.MouseButtonRelease, where, Qt.MouseButton.RightButton, Qt.MouseButton.NoButton)
    settle(app)
    menu = win.plot.getViewBox().menu
    assert menu.isVisible()
    next(a for a in menu.actions() if a.text() == "Place Y2 Here").trigger()
    next(a for a in menu.actions() if a.text() == "Place X1 Here").trigger()
    menu.close()
    assert m.marker("Y2") == pytest.approx(0.006, rel=0.02)
    assert m.marker("X1") == pytest.approx(4.0, rel=0.01)
    assert win.measure_dock.isVisible() and win.markers_btn.isChecked()


def test_unit_change_clears_y_markers_and_box_but_keeps_x(app, win):
    m = win.measurements
    win.markers_btn.click()
    m.drag_box(QPointF(2.0, 0.003), QPointF(4.0, 0.007), True)
    x1 = m.marker("X1")
    win._show(make_acquisition(Function.DCV))                # volts now: levels in amps mean nothing
    settle(app)
    assert m.marker("X1") == pytest.approx(x1)
    assert m.marker("Y1") is None and m.marker("Y2") is None and not m.has_box
    markers, _ = readouts(app, win)
    assert "Voltage" in markers and "Y1" not in markers


# --- Selection box -----------------------------------------------------------------------

def test_select_drag_draws_a_box_and_does_not_pan(app, win):
    m = win.measurements
    before = x_range(win)
    win.select_btn.click()
    drag(app, win, point(win, 2.0, 0.007), point(win, 4.0, 0.003))
    assert x_range(win) == pytest.approx(before)
    assert m.has_box and win.measure_dock.isVisible()
    x0, x1, y0, y1 = m.box.bounds()
    assert (x0, x1) == pytest.approx((2.0, 4.0), abs=0.03)
    assert (y0, y1) == pytest.approx((0.003, 0.007), abs=1e-4)
    _, selection = readouts(app, win)
    s = box_stats(win.acq.values, *samples_between(x0, x1, 1e-3, N), y0, y1)
    assert s.count > 0 and f"Points in box {s.count:,}" in selection and "Mean" in selection


def test_box_zoom_and_select_are_exclusive(app, win):
    win.box_zoom_btn.click()
    win.select_btn.click()
    assert not win.box_zoom_btn.isChecked()
    win.box_zoom_btn.click()
    assert not win.select_btn.isChecked() and not win.plot.getViewBox().selecting


def test_corner_handle_resizes_the_box(app, win):
    m = win.measurements
    win.select_btn.click()
    drag(app, win, point(win, 2.0, 0.003), point(win, 4.0, 0.007))
    win.select_btn.click()                                   # resizing works without Select too
    drag(app, win, point(win, 4.0, 0.007), point(win, 6.0, 0.008))
    x0, x1, y0, y1 = m.box.bounds()
    assert (x0, y0) == pytest.approx((2.0, 0.003), abs=0.03)          # the opposite corner stayed
    assert (x1, y1) == pytest.approx((6.0, 0.008), abs=0.05)


def test_inside_of_the_box_moves_it_only_in_select_mode(app, win):
    m = win.measurements
    win.select_btn.click()
    drag(app, win, point(win, 2.0, 0.003), point(win, 4.0, 0.007))
    drag(app, win, point(win, 3.0, 0.005), point(win, 5.0, 0.005))   # move it
    x0, x1, _, _ = m.box.bounds()
    assert (x0, x1) == pytest.approx((4.0, 6.0), abs=0.05)
    win.plot.getViewBox().setXRange(1.0, 9.0, padding=0)              # room to pan
    settle(app)
    win.select_btn.click()                                   # Select off: dragging inside pans
    drag(app, win, point(win, 5.0, 0.005), point(win, 4.0, 0.005))
    assert x_range(win) == pytest.approx([2.0, 10.0], abs=0.05)
    assert m.box.bounds()[:2] == pytest.approx((x0, x1))


def test_escape_and_clear_remove_the_box(app, win):
    m = win.measurements
    win.select_btn.click()
    drag(app, win, point(win, 1.0, 0.002), point(win, 2.0, 0.008))
    assert m.has_box
    QTest.keyClick(win, Qt.Key.Key_Escape)
    assert not m.has_box and not win.measure_dock.isVisible() and not m.box.isVisible()
    drag(app, win, point(win, 1.0, 0.002), point(win, 2.0, 0.008))
    m.clear_box_btn.click()
    assert not m.has_box


def test_a_new_box_replaces_the_old_one(app, win):
    m = win.measurements
    win.select_btn.click()
    drag(app, win, point(win, 1.0, 0.002), point(win, 2.0, 0.008))
    drag(app, win, point(win, 5.0, 0.001), point(win, 7.0, 0.003))
    x0, x1, _, _ = m.box.bounds()
    assert (x0, x1) == pytest.approx((5.0, 7.0), abs=0.03)


def test_tiny_drag_makes_no_box(app, win):
    m = win.measurements
    vb = win.plot.getViewBox()
    px, py = vb.viewPixelSize()
    vb.sigSelecting.emit(QPointF(2.0, 0.005), QPointF(2.0 + px, 0.005 + 20 * py), True)
    assert not m.has_box


def test_selection_counts_time_in_box_and_overloads(app, win):
    m = win.measurements
    # 2.000 ... 3.999 s: 2,000 samples. The sawtooth is between 3 and 7 mA for 41 of every 100.
    m.drag_box(QPointF(2.0, 0.00295), QPointF(3.9995, 0.00705), True)
    _, selection = readouts(app, win)
    assert selection.index("Points in time range") < selection.index("Points in box")
    for text in ("Points in time range 2,000", "Points in box 820", "Time in box 820 ms (41.0%)",
                 "Min 3 mA", "Mean 5 mA", "Max 7 mA"):
        assert text in selection, text
    assert "Overloads" not in selection
    m.drag_box(QPointF(6.0, 0.00295), QPointF(7.9995, 0.00705), True)     # includes the 3 overloads
    _, selection = readouts(app, win)
    assert "Points in time range 1,997" in selection and "Overloads in time range 3" in selection


def test_selection_statistics_wait_for_a_complete_capture(app, win):
    m = win.measurements
    m.drag_box(QPointF(2.0, 0.003), QPointF(4.0, 0.007), True)
    m.set_data(win.acq.values, 1e-3, "A", capturing=True)
    _, selection = readouts(app, win)
    assert "when the capture is complete" in selection and "Points in box" not in selection


def test_live_data_feeds_the_marker_readouts(app, win):
    """During a capture the readouts follow the trace being drawn, not the last capture."""
    m = win.measurements
    m.place_marker("X1", QPointF(1.0, 0))
    live = np.full(2000, 0.0042)                             # 2 s of a new capture so far
    m.set_data(live, 1e-3, "A", capturing=True)
    markers, _ = readouts(app, win)
    assert "Current 4.2 mA" in markers


# --- Readouts under the plot -------------------------------------------------------------

def hover(app, w, t: float):
    w._on_mouse_moved(w.plot.getViewBox().mapViewToScene(QPointF(t, 0.005)))
    settle(app)


def sizes(w) -> tuple[int, int, int]:
    return w.cursor_label.width(), w.stats_label.height(), w.plot.height()


@pytest.mark.parametrize("width", [1500, 1000])          # 1000: the statistics wrap
def test_readouts_under_the_plot_keep_their_size(app, win, width):
    """The cursor readout and the statistics share a row. If either changed size with the values
    shown, the plot would resize as the mouse moves, worst with the Measurements panel open."""
    win.resize(width, 900)
    win.markers_btn.click()                                  # the panel narrows the plot
    settle(app)
    hover(app, win, 2.0)                                     # 0 A
    before = sizes(win)
    for t in (2.057, 2.0123, 5.0991, OVERLOADS[0] * 1e-3, 9.999):
        hover(app, win, t)
        assert sizes(win) == before, t
    win.plot.getViewBox().setXRange(6.9, 7.1, padding=0)   # other statistics, with overloads
    win._update_stats()
    settle(app)
    assert "missing" in win.stats_label.text()
    assert sizes(win)[1:] == before[1:]
    label = win.stats_label
    assert label.height() >= label.heightForWidth(label.width())       # the text fits


# --- Docked or popped out ----------------------------------------------------------------

def test_pop_out_moves_the_panel_into_its_own_window(app, win):
    win.markers_btn.click()
    settle(app)
    assert win.measure_dock.isVisible() and not win.measure_window.isVisible()
    win.pop_out_btn.click()
    settle(app)
    assert win.measure_window.isVisible() and not win.measure_dock.isVisible()
    assert win.measurements.window() is win.measure_window and win.pop_out_btn.text() == "Dock"
    win.pop_out_btn.click()
    settle(app)
    assert win.measure_dock.isVisible() and not win.measure_window.isVisible()
    assert win.measurements.window() is win and win.pop_out_btn.text() == "Pop Out"


def test_closing_the_window_clears_markers_and_selection(app, win):
    m = win.measurements
    win.select_btn.click()
    drag(app, win, point(win, 2.0, 0.003), point(win, 4.0, 0.007))
    win.markers_btn.click()
    win.pop_out_btn.click()
    settle(app)
    win.measure_window.close()
    settle(app)
    assert not m.has_markers and not m.has_box and not win.markers_btn.isChecked()
    assert not win.measure_window.isVisible() and not win.measure_dock.isVisible()
    win.markers_btn.click()                                  # still popped out: the window opens again
    settle(app)
    assert win.measure_window.isVisible() and not win.measure_dock.isVisible()


def test_the_dock_does_not_float(app, win):
    """Qt moves a floating dock itself, which Wayland doesn't allow: it would be stuck in place."""
    assert win.measure_dock.features() == QDockWidget.DockWidgetFeature.NoDockWidgetFeatures


def test_popped_out_is_remembered(app, win, tmp_path):
    win.markers_btn.click()
    win.pop_out_btn.click()
    win.close()
    again = MainWindow(settings=QSettings(str(tmp_path / "keysight.conf"), QSettings.Format.IniFormat))
    again.show()
    settle(app)
    assert again._popped_out and again.pop_out_btn.text() == "Dock"
    assert not again.measure_window.isVisible()              # nothing to show until there are markers
    again.close()


def test_value_columns_fit_the_longest_values(app, win):
    m = win.measurements
    fm = m.markers_label.fontMetrics()
    for text in ("39.99998 s", "-888.8888 mV", "-16.28771 ms", "-9.999999 mA", "overload"):
        assert fm.horizontalAdvance(" " + text) + 4 <= m._column, text     # 4: cell padding
