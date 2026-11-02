"""X1/X2 and Y1/Y2 markers, a selection box, and the Measurements panel that reads them out.

X markers are times, Y markers are levels in the trace's unit (V or A). The selection box is a
time span and a level range; its statistics cover the samples inside both. Modelled on the
R&S NGM FastLog GUI, here for a single trace.
"""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal as Signal
from PyQt6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .instrument import BoxStats, box_stats, samples_between

X_MARKERS = ("X1", "X2")
Y_MARKERS = ("Y1", "Y2")
X_MARKER_COLOR = "#f06bff"                  # magenta: X1, X2 (time)
Y_MARKER_COLOR = "#5df07d"                  # green: Y1, Y2 (level)
SELECTION_FILL = (255, 255, 255, 28)        # light enough to see the samples underneath
SELECTION_HANDLE_SIZE = 8                   # px
DEFAULT_POSITION = {"X1": 0.25, "X2": 0.75, "Y1": 0.25, "Y2": 0.75}    # fraction of the view
LABEL_POSITION = {"X1": 0.94, "X2": 0.86, "Y1": 0.03, "Y2": 0.10}      # along the line; staggered
PRECISION = 7                               # significant digits, as in the cursor readout
UPDATE_INTERVAL = 80                        # ms: readouts refresh at most this often while dragging
MIN_BOX_PX = 3                              # a smaller drag makes no selection
QUANTITY = {"V": "Voltage", "A": "Current"}
# Columns are sized for the longest text they can hold, so they don't shift as values change.
WIDEST_VALUE = "-888.8888 mV"
WIDEST_LABEL = "Voltage"
WIDEST_STAT = "888.8 ms (100.0%)"
WIDEST_STAT_LABEL = "Overloads in time range"


def si(value: float | None, unit: str, precision: int = PRECISION) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return pg.siFormat(value, precision=precision, suffix=unit)


def _reading(value: float | None, unit: str) -> str:
    """A sample's value: "—" without one, "overload" for a missing (NaN) sample."""
    if value is not None and math.isnan(value):
        return "overload"
    return si(value, unit)


def _difference(a: float | None, b: float | None, unit: str, precision: int = PRECISION) -> str:
    if a is None or b is None:
        return "—"
    return si(b - a, unit, precision)


def _reciprocal(delta: float, unit: str) -> str:
    """1/Δ: in Hz for time; for a level, without an SI prefix (12 mA⁻¹ would be ambiguous)."""
    if not delta or not math.isfinite(delta):
        return "—"
    return si(1 / delta, "Hz", 6) if unit == "s" else f"{1 / delta:#.6g} {unit}⁻¹"


def _table(header: list[str], rows: list[list[str]], column: int) -> str:
    cells = "".join(f"<th align='right' width='{column}'>{h}</th>" for h in header[1:])
    html = [f"<table cellspacing='0' cellpadding='2'><tr><th></th>{cells}</tr>"]
    for label, *values in rows:
        html.append(f"<tr><td>{label}&nbsp;</td>"
                    + "".join(f"<td align='right'>&nbsp;{v}</td>" for v in values) + "</tr>")
    return "".join(html) + "</table>"


def _tinted(text: str, color: str) -> str:
    return f"<span style='color:{color}; font-weight:bold'>{text}</span>"


class PlotViewBox(pg.ViewBox):
    """ViewBox that can draw a selection box instead of panning, and remembers where its menu opened.

    While `selecting` is set, a left-drag emits sigSelecting(start, end, finished) in data
    coordinates instead of panning. Other buttons, the wheel and axis drags work as usual.
    """

    sigSelecting = Signal(object, object, bool)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selecting = False
        self.menu_point: QPointF | None = None      # where the context menu was opened (data coordinates)
        self._origin = QPointF()

    def mouseDragEvent(self, ev, axis=None):
        if self.selecting and axis is None and ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()
            if ev.isStart():        # in data coordinates, in case the layout changes during the drag
                self._origin = self.mapSceneToView(ev.buttonDownScenePos())
            self.sigSelecting.emit(self._origin, self.mapSceneToView(ev.scenePos()), ev.isFinish())
            return
        super().mouseDragEvent(ev, axis)

    def raiseContextMenu(self, ev):
        self.menu_point = self.mapSceneToView(ev.scenePos())
        super().raiseContextMenu(ev)


class MarkerLine(pg.InfiniteLine):
    """A draggable marker: a vertical (time) or horizontal (level) line labelled with its name."""

    def __init__(self, name: str, vertical: bool, color: str, dashed: bool, label_position: float):
        style = Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine
        super().__init__(angle=90 if vertical else 0, movable=True, pen=pg.mkPen(color, width=1, style=style),
                         hoverPen=pg.mkPen(color, width=3), label=name,
                         labelOpts={"position": label_position, "color": color, "fill": (0, 0, 0, 220)})
        self.name = name
        self.setCursor(Qt.CursorShape.SizeHorCursor if vertical else Qt.CursorShape.SizeVerCursor)
        self.setZValue(20)


class SelectionBox(pg.ROI):
    """Translucent selection rectangle with a handle on each corner to resize it.

    Dragging the inside moves it only while `translatable` is set (Select mode); otherwise a drag
    there pans the plot as usual.
    """

    def __init__(self):
        super().__init__((0, 0), (1, 1), movable=False, rotatable=False, resizable=False,
                         pen=pg.mkPen("w", width=1), hoverPen=pg.mkPen("y", width=1),
                         handlePen=pg.mkPen("w", width=1), handleHoverPen=pg.mkPen("y", width=2))
        self.handleSize = SELECTION_HANDLE_SIZE
        for x, y in ((0, 0), (1, 0), (0, 1), (1, 1)):
            self.addScaleHandle((x, y), (1 - x, 1 - y))        # the opposite corner stays put
        self._brush = pg.mkBrush(SELECTION_FILL)
        self.setZValue(15)

    def set_corners(self, x0: float, y0: float, x1: float, y1: float, finish: bool = True) -> None:
        self.setPos((min(x0, x1), min(y0, y1)), update=False)
        self.setSize((abs(x1 - x0), abs(y1 - y0)), finish=finish)

    def bounds(self) -> tuple[float, float, float, float]:
        """(x0, x1, y0, y1), lowest first."""
        (x, y), (w, h) = self.pos(), self.size()
        return min(x, x + w), max(x, x + w), min(y, y + h), max(y, y + h)

    def paint(self, p, opt, widget):
        p.fillRect(QRectF(0, 0, self.state["size"][0], self.state["size"][1]).normalized(), self._brush)
        super().paint(p, opt, widget)


class PopOutWindow(QWidget):
    """A separate window, kept above its parent, that emits `closed` when the user closes it.

    Unlike a floating QDockWidget, it has a title bar drawn by the platform, so it can be moved
    on Wayland too (Qt moves floating docks itself, which Wayland doesn't allow).
    """

    closed = Signal()

    def __init__(self, title: str, parent: QWidget):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(title)
        QVBoxLayout(self).setContentsMargins(0, 0, 0, 0)

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        self.closed.emit()


class MeasurementsPanel(QWidget):
    """Owns the markers and the selection box on `plot`, and shows what they measure."""

    changed = Signal()              # markers or the selection box appeared or disappeared

    def __init__(self, plot: pg.PlotItem):
        super().__init__()
        self._plot = plot
        self._values: np.ndarray | None = None      # the trace on display
        self._interval = 1.0
        self._unit: str | None = None
        self._capturing = False
        self._placed: set[str] = set()
        self._timer = QTimer(self, singleShot=True, interval=UPDATE_INTERVAL)
        self._timer.timeout.connect(self.update_readouts)
        self._stats_cache: tuple[tuple, str] | None = None     # (what was computed, its table)

        self._lines: dict[str, MarkerLine] = {}
        for name in X_MARKERS + Y_MARKERS:
            vertical = name in X_MARKERS
            line = MarkerLine(name, vertical, X_MARKER_COLOR if vertical else Y_MARKER_COLOR,
                              name.endswith("2"), LABEL_POSITION[name])
            line.hide()
            plot.addItem(line, ignoreBounds=True)
            line.sigPositionChanged.connect(self._schedule)
            self._lines[name] = line
        self.box = SelectionBox()
        self.box.hide()
        self._has_box = False
        plot.addItem(self.box, ignoreBounds=True)
        self.box.sigRegionChanged.connect(self._schedule)

        layout = QVBoxLayout(self)
        group = QGroupBox("Markers")
        group.setToolTip("Drag a marker to move it. Right-click the plot to place a marker at that point.")
        box = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addStretch(1)
        self.clear_markers_btn = QPushButton("Clear")
        self.clear_markers_btn.setToolTip("Remove all markers")
        self.clear_markers_btn.clicked.connect(self.clear_markers)
        row.addWidget(self.clear_markers_btn)
        box.addLayout(row)
        self.markers_label = self._readout()
        box.addWidget(self.markers_label)
        layout.addWidget(group)

        group = QGroupBox("Selection")
        group.setToolTip("Points in time range: samples between the box's left and right edges.\n"
                         "Points in box: of those, the ones between its bottom and top edges.\n"
                         "Time in box: points in box × sample interval, and its share of the time range.\n"
                         "Min, mean, max and σ are of the points in box. Overloads are left out.")
        box = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addStretch(1)
        self.clear_box_btn = QPushButton("Clear")
        self.clear_box_btn.setToolTip("Remove the selection box (Esc)")
        self.clear_box_btn.clicked.connect(self.clear_box)
        row.addWidget(self.clear_box_btn)
        box.addLayout(row)
        self.selection_label = self._readout()
        box.addWidget(self.selection_label)
        layout.addWidget(group)
        layout.addStretch(1)

        fm = self.markers_label.fontMetrics()

        def cell(text: str) -> int:
            return fm.horizontalAdvance(" " + text) + 8                 # + the cell's &nbsp; and padding

        self._column = cell(WIDEST_VALUE)
        self._stat_column = cell(WIDEST_STAT)
        margins = 2 * (layout.contentsMargins().left() + box.contentsMargins().left() + 8)
        self.setMinimumWidth(margins + max(cell(WIDEST_LABEL) + 3 * self._column,
                                           cell(WIDEST_STAT_LABEL) + self._stat_column))
        self.update_readouts()

    @staticmethod
    def _readout() -> QLabel:
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return label

    # --- Data ----------------------------------------------------------------------------

    def set_data(self, values: np.ndarray | None, interval: float, unit: str, capturing: bool = False) -> None:
        """The trace to read at the markers. Selection statistics wait until the capture is complete.

        Y markers and the selection box are levels in the trace's unit, so they are cleared when
        the unit changes (DC voltage to DC current or back). X markers are times and stay.
        """
        if self._unit is not None and unit != self._unit:
            self._clear_y_markers()
            self.clear_box()
        self._values = values
        self._interval = interval
        self._unit = unit
        self._capturing = capturing
        self._schedule()

    # --- Markers -------------------------------------------------------------------------

    @property
    def has_markers(self) -> bool:
        return bool(self._placed)

    def marker(self, name: str) -> float | None:
        """A marker's position (time for X, level for Y), or None if it isn't placed."""
        return self._lines[name].value() if name in self._placed else None

    def place_markers(self) -> None:
        """Place every marker not placed yet: at 25 % and 75 % of the visible time and level ranges."""
        (t0, t1), (y0, y1) = self._plot.getViewBox().viewRange()
        for name in X_MARKERS + Y_MARKERS:
            if name not in self._placed:
                lo, hi = (t0, t1) if name in X_MARKERS else (y0, y1)
                self._set_marker(name, lo + DEFAULT_POSITION[name] * (hi - lo))
        self.changed.emit()

    def place_marker(self, name: str, point: QPointF) -> None:
        """Place one marker at a point in the plot (data coordinates)."""
        self._set_marker(name, point.x() if name in X_MARKERS else point.y())
        self.changed.emit()

    def _set_marker(self, name: str, value: float) -> None:
        line = self._lines[name]
        line.setValue(value)
        line.show()
        self._placed.add(name)
        self._schedule()

    def clear_markers(self) -> None:
        for line in self._lines.values():
            line.hide()
        self._placed.clear()
        self._schedule()
        self.changed.emit()

    def _clear_y_markers(self) -> None:
        if self._placed & set(Y_MARKERS):
            for name in Y_MARKERS:
                self._lines[name].hide()
                self._placed.discard(name)
            self.changed.emit()

    # --- Selection box -------------------------------------------------------------------

    @property
    def has_box(self) -> bool:
        return self._has_box

    def drag_box(self, start: QPointF, end: QPointF, finished: bool) -> None:
        """Draw the selection box from a drag (data coordinates), replacing any other.

        `changed` waits for the end of the drag: the panel appearing mid-drag would resize the plot
        under the mouse.
        """
        self.box.set_corners(start.x(), start.y(), end.x(), end.y(), finish=finished)
        self.box.show()
        self._has_box = True
        if finished:
            px, py = self._plot.getViewBox().viewPixelSize()
            x0, x1, y0, y1 = self.box.bounds()
            if (x1 - x0) < MIN_BOX_PX * px or (y1 - y0) < MIN_BOX_PX * py:
                self.clear_box()
            else:
                self.changed.emit()

    def clear_box(self) -> None:
        if not self._has_box:
            return
        self.box.hide()
        self._has_box = False
        self._schedule()
        self.changed.emit()

    def set_box_movable(self, movable: bool) -> None:
        """Let a drag inside the box move it (Select mode); otherwise it pans the plot."""
        self.box.translatable = movable

    # --- Readouts ------------------------------------------------------------------------

    def _schedule(self, *_) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def update_readouts(self) -> None:
        self.markers_label.setText(self._markers_html())
        self.selection_label.setText(self._selection_html())

    def _value_at(self, t: float | None) -> float | None:
        """The nearest sample's value at time t (NaN for an overload), or None if there is none."""
        if t is None or self._values is None:
            return None
        i = round(t / self._interval)
        return float(self._values[i]) if 0 <= i < len(self._values) else None

    def _markers_html(self) -> str:
        if not self._placed:
            return "<i>None. Turn on Markers in the toolbar, or right-click the plot to place one.</i>"
        unit = self._unit or ""
        quantity = QUANTITY.get(unit, "Level")
        parts = []
        x1, x2 = (self.marker(n) for n in X_MARKERS)
        if x1 is not None or x2 is not None:
            dx = x2 - x1 if x1 is not None and x2 is not None else math.nan
            a, b = self._value_at(x1), self._value_at(x2)
            rows = [["t", si(x1, "s"), si(x2, "s"), si(dx, "s", 6)],
                    ["1/ΔX", "", "", _reciprocal(dx, "s")]]
            if self._values is not None:
                rows.append([quantity, _reading(a, unit), _reading(b, unit), _difference(a, b, unit)])
            parts.append(_table(["", *(_tinted(h, X_MARKER_COLOR) for h in ("X1", "X2", "ΔX"))], rows,
                                self._column))
        y1, y2 = (self.marker(n) for n in Y_MARKERS)
        if y1 is not None or y2 is not None:
            dy = y2 - y1 if y1 is not None and y2 is not None else math.nan
            rows = [[quantity, si(y1, unit), si(y2, unit), si(dy, unit)],
                    ["1/ΔY", "", "", _reciprocal(dy, unit)]]
            parts.append(_table(["", *(_tinted(h, Y_MARKER_COLOR) for h in ("Y1", "Y2", "ΔY"))], rows,
                                self._column))
        return "<br>".join(parts)

    def _selection_html(self) -> str:
        if not self.has_box:
            return "<i>None. Turn on Select in the toolbar and drag a rectangle over the trace.</i>"
        unit = self._unit or ""
        quantity = QUANTITY.get(unit, "Level")
        x0, x1, y0, y1 = self.box.bounds()
        html = _table(["", "From", "To", "Δ"], [
            ["t", si(x0, "s"), si(x1, "s"), si(x1 - x0, "s", 6)],
            [quantity, si(y0, unit), si(y1, unit), si(y1 - y0, unit)]], self._column)
        values = self._values
        if values is None:
            return html
        if self._capturing:
            return html + "<br><i>Statistics follow when the capture is complete.</i>"
        key = (x0, x1, y0, y1, id(values), len(values), self._interval)
        if self._stats_cache is not None and self._stats_cache[0] == key:
            return html + self._stats_cache[1]
        s = box_stats(values, *samples_between(x0, x1, self._interval, len(values)), y0, y1)
        rows = [["Points in time range", f"{s.span:,}"]]
        if s.missing:
            rows.append(["Overloads in time range", f"{s.missing:,}"])
        rows += [["Points in box", f"{s.count:,}"],
                 ["Time in box", self._time_in_box(s)]]
        for label, field in (("Min", "min"), ("Mean", "mean"), ("Max", "max"), ("σ", "std")):
            rows.append([label, si(getattr(s, field), unit)])
        table = "<br>" + _table(["", quantity], rows, self._stat_column)
        self._stats_cache = (key, table)
        return html + table

    def _time_in_box(self, s: BoxStats) -> str:
        """Points in box × sample interval, and its share of the time range."""
        if s.span == 0:
            return "—"
        return f"{si(s.count * self._interval, 's', 4)} ({100 * s.count / s.span:.1f}%)"
