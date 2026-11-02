"""Shared fixtures: a Qt application (offscreen) and the DMM simulator on a free port."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import dmm_simulator  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sim():
    """A simulated 34465A (DIG, MEM); yields its VISA resource string."""
    server = dmm_simulator.Server(("127.0.0.1", 0), dmm_simulator.SimulatedDMM("34465A", "0,0,DIG,MEM"),
                                  verbose=False)
    server.log = lambda msg: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"TCPIP::127.0.0.1::{server.server_address[1]}::SOCKET"
    server.shutdown()
    server.server_close()


def wait_for(app, predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")
