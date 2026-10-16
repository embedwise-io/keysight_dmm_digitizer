"""Save acquisitions (or a slice of one) to CSV or HDF5."""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np

from .instrument import Acquisition

# Export formats and their file suffixes; the first suffix is the default.
FORMATS = {"CSV": (".csv",), "HDF5": (".h5", ".hdf5")}

# Modern HDF5 file format (checksummed metadata, newer chunk index) that HDF5 1.14 tools can still
# read. A low bound of "latest" (HDF5 2.0) would make files unreadable by tools built on HDF5 1.x.
HDF5_LIBVER = ("v114", "latest")
HDF5_COMPRESSION = {"compression": "gzip", "compression_opts": 4, "shuffle": True}


def format_for(path: str | Path) -> str | None:
    """Export format implied by the file suffix, or None if it isn't one of FORMATS."""
    suffix = Path(path).suffix.lower()
    return next((name for name, suffixes in FORMATS.items() if suffix in suffixes), None)


def save(path: str | Path, acq: Acquisition, start: int = 0, stop: int | None = None) -> int:
    """Write samples [start:stop) with metadata, in the format given by the file suffix.

    Returns the number of samples written.
    """
    path = Path(path)
    fmt = format_for(path)
    if fmt is None:
        suffixes = ", ".join(s for group in FORMATS.values() for s in group)
        raise ValueError(f"Unknown export format for '{path.name}'; use one of {suffixes}.")
    values = acq.values[start:stop]
    meta = acq.metadata()
    meta["samples"] = str(len(values))
    meta["first_sample_index"] = str(start)
    if fmt == "HDF5":
        _save_hdf5(path, acq, values, start, meta)
    else:
        _save_csv(path, acq, values, start, meta)
    return len(values)


def _save_csv(path: Path, acq: Acquisition, values: np.ndarray, start: int, meta: dict[str, str]) -> None:
    t = (start + np.arange(len(values))) * acq.interval
    # Metadata as '#' comments, then a plain column header, so pandas.read_csv(path, comment="#") works.
    column = "voltage_V" if acq.unit == "V" else "current_A"
    with open(path, "w", newline="") as f:
        f.writelines(f"# {k}: {v}\n" for k, v in meta.items())
        f.write(f"time_s,{column}\n")
        np.savetxt(f, np.column_stack((t, values)), delimiter=",", fmt="%.9g")


def _save_hdf5(path: Path, acq: Acquisition, values: np.ndarray, start: int, meta: dict[str, str]) -> None:
    """One float64 dataset, "values", with the metadata as its attributes.

    Samples are evenly spaced, so time is not stored: t = t0_s + arange(n) * sample_interval_s.
    """
    with h5py.File(path, "w", libver=HDF5_LIBVER) as f:
        ds = f.create_dataset("values", data=values, **HDF5_COMPRESSION)
        ds.attrs.update(meta)
        # Numeric attributes as numbers rather than the strings used in the CSV header.
        ds.attrs.update({
            "units": acq.unit,
            "sample_interval_s": acq.interval,
            "sample_rate_hz": float(f"{acq.sample_rate:.9g}"),  # 50000.0, not 49999.99999999999
            "t0_s": start * acq.interval,
            "samples": len(values),
            "first_sample_index": start,
        })
