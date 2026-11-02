"""The main window against the simulator: trigger modes, gap navigation and saving."""

from datetime import datetime

import h5py
import numpy as np
import pytest
from PyQt6.QtCore import QSettings

from conftest import wait_for
from keysight_daq import gui
from keysight_daq.instrument import Acquisition, DeviceInfo, DigitizerConfig, Function, Timing

DEVICE = DeviceInfo("sim", "Keysight Technologies", "34465A", "MY54500244", "A.03.03-02.40-03.03-00.52-02-01",
                    frozenset({"DIG", "MEM"}))


@pytest.fixture
def win(app, tmp_path):
    w = gui.MainWindow(settings=QSettings(str(tmp_path / "keysight.conf"), QSettings.Format.IniFormat))
    w.resize(1400, 800)
    w.show()
    yield w
    w.close()


def connect(app, w, resource: str) -> None:
    w.address.setEditText(resource)
    w._on_connect()
    wait_for(app, lambda: w.info is not None)


def setup(w, mode: int, rate: str, duration: float) -> None:
    w.trigger_mode.setCurrentIndex(mode)                     # 0 Auto, 1 Manual
    w.rate.setCurrentIndex(w.rate.findText(rate))
    w.duration.setValue(duration)


def test_manual_mode_arms_triggers_rearms_and_stops(app, win, sim):
    connect(app, win, sim)
    setup(win, 1, "20 kHz", 0.3)
    assert not win.trigger_btn.isEnabled()
    win._on_run()
    wait_for(app, lambda: win._armed)
    assert win.trigger_btn.isEnabled() and win.progress.text() == "Armed — press Trigger (F6)"
    win._on_trigger()
    wait_for(app, lambda: win._armed and win._run_captures == 1)
    assert win.acq.complete and len(win.acq.values) == 6000
    assert win.measurements._values is win.acq.values and not win.measurements._capturing
    win._on_run()                                            # Stop while armed
    wait_for(app, lambda: not win._running)
    assert win.statusBar().currentMessage() == "Stopped after 1 capture(s)"
    assert win.run_btn.text() == "Run" and not win.trigger_btn.isEnabled()


def test_auto_mode_stop_keeps_the_last_complete_capture(app, win, sim):
    connect(app, win, sim)
    setup(win, 0, "50 kHz", 0.5)
    win._on_run()
    wait_for(app, lambda: win._run_captures >= 2)
    wait_for(app, lambda: win._capturing and win.progress.value() > 2000)
    win._on_run()                                            # Stop part-way through a capture
    wait_for(app, lambda: not win._running)
    assert win.acq.complete and len(win.acq.values) == 25_000


def gapped_acquisition() -> Acquisition:
    n, dt = 100_000, 20e-6
    v = 0.9 + 0.25 * np.sin(2 * np.pi * 3 * np.arange(n) * dt)
    v[[50, 15_000, 40_000, 40_050, 99_990]] = np.nan
    v[75_000:75_040] = np.nan
    return Acquisition(DigitizerConfig(Function.DCV, 1.0, 50e3, 2.0), DEVICE, Timing(dt, n, "test", ()), v,
                       datetime.now().astimezone())


def test_gap_buttons_step_through_every_gap(app, win):
    assert not win.prev_gap_btn.isEnabled() and not win.next_gap_btn.isEnabled()
    win._show(gapped_acquisition())
    win._fit()
    app.processEvents()
    assert len(win._gaps) == 6 and win.prev_gap_btn.isEnabled() and win.next_gap_btn.isEnabled()
    visited = []
    for _ in range(6):
        win._goto_gap(1)
        visited.append(win._gap_nav[0])
    assert visited == list(range(6)) and not win.next_gap_btn.isEnabled()
    assert win.statusBar().currentMessage().startswith("Gap 6 of 6: 1 sample")
    win._goto_gap(-1)
    assert win._gap_nav[0] == 4
    win._fit()
    app.processEvents()
    win._goto_gap(-1)                                        # from the full view: the last gap
    assert win._gap_nav[0] == 5


def test_save_dialog_formats_and_suffixes(app, win, tmp_path, monkeypatch):
    typed = []
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a: (str(tmp_path / typed.pop(0)), a[3])))
    win._show(gapped_acquisition())
    for fmt, name, expected in [("CSV", "a.csv", "a.csv"), ("HDF5", "b", "b.h5"), ("HDF5", "c.csv", "c.h5"),
                                ("HDF5", "d.hdf5", "d.hdf5"), ("CSV", "e.h5", "e.csv"), ("HDF5", "run.v2", "run.v2.h5")]:
        next(b for b in win.export_format.buttons() if b.text() == fmt).setChecked(True)
        typed.append(name)
        win._save(view_only=False)
        assert (tmp_path / expected).exists(), expected
    with h5py.File(tmp_path / "b.h5") as f:
        ds = f["values"]
        assert ds.shape == (100_000,) and ds.attrs["units"] == "V"
        assert np.array_equal(ds[...], win.acq.values, equal_nan=True)
