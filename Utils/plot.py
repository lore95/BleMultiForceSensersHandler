from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Tuple, Optional

import matplotlib.pyplot as plt

def _read_force_csv(filepath: str | Path) -> Tuple[List[float], List[float]]:
   
    filepath = Path(filepath)

    times: List[float] = []
    forces: List[float] = []

    with filepath.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t = float(row["Host_Time_s"])
                force = float(row["Force_N"])
            except Exception:
                continue
            times.append(t)
            forces.append(force)

    return times, forces


def plot_force_over_time(
    filepath: str | Path,
    *,
    relative_time: bool = True,
    title: Optional[str] = None,
    show: bool = True,
) -> None:
   
    filepath = Path(filepath)
    times, forces = _read_force_csv(filepath)

    if not times or not forces:
        raise ValueError(f"No plottable data found in file: {filepath}")

    if relative_time:
        t0 = times[0]
        x = [t - t0 for t in times]
        xlabel = "Time (s) since start"
    else:
        x = times
        xlabel = "Host_Time_s"

    plt.figure()
    plt.plot(x, forces)
    plt.xlabel(xlabel)
    plt.ylabel("Force (N)")
    plt.title(title or f"Force over Time\n{filepath.name}")
    plt.grid(True)

    if show:
        # Non-blocking show (still fine if you prefer blocking)
        plt.show(block=False)