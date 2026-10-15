"""Save acquisitions (or a slice of one) to CSV or NumPy .npz."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .instrument import Acquisition

FILE_FILTERS = "CSV (*.csv);;NumPy archive (*.npz)"


def save(path: str | Path, acq: Acquisition, start: int = 0, stop: int | None = None) -> int:
    """Write samples [start:stop) with a metadata header. Returns the number of samples written."""
    path = Path(path)
    values = acq.values[start:stop]
    t = (start + np.arange(len(values))) * acq.interval
    meta = acq.metadata()
    meta["samples"] = str(len(values))
    meta["first_sample_index"] = str(start)

    if path.suffix.lower() == ".npz":
        np.savez(path, time=t, values=values, **{f"meta_{k}": v for k, v in meta.items()})
    else:
        # Metadata as '#' comments, then a plain column header, so pandas.read_csv(path, comment="#") works.
        column = "voltage_V" if acq.unit == "V" else "current_A"
        with open(path, "w", newline="") as f:
            f.writelines(f"# {k}: {v}\n" for k, v in meta.items())
            f.write(f"time_s,{column}\n")
            np.savetxt(f, np.column_stack((t, values)), delimiter=",", fmt="%.9g")
    return len(values)
