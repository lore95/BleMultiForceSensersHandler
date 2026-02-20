import csv
from dataclasses import dataclass
from typing import List, Tuple, Optional

import numpy as np


@dataclass(frozen=True)
class CalibrationPoint:
    force_n: float
    raw_v3: float


class V3ForceCalibrator:
    """
    Converts raw V3 readings to Force (Newtons) using a calibration CSV with columns:
        Force_N,V3_mean

    Supports:
      - piecewise-linear interpolation (recommended)
      - optional best-fit line as a fallback / summary model
    """

    def __init__(self, csv_path: str, *, method: str = "piecewise", allow_extrapolation: bool = True):
        """
        method: "piecewise" (default) or "linear_fit"
        allow_extrapolation:
            - for piecewise: if False, clamps to min/max calibrated force
            - for linear_fit: always extrapolates (it's a line)
        """
        self.csv_path = csv_path
        self.method = method
        self.allow_extrapolation = allow_extrapolation
        self._points: List[CalibrationPoint] = self._load_points(csv_path)
        if len(self._points) < 2:
            raise ValueError("Need at least 2 calibration points.")

        # Sort by raw (V3) so interpolation works even if Force_N is not strictly monotonic.
        self._points.sort(key=lambda p: p.raw_v3)

        # Arrays for interpolation
        self._raw = np.array([p.raw_v3 for p in self._points], dtype=float)
        self._force = np.array([p.force_n for p in self._points], dtype=float)

        # Optional linear fit Force = a*raw + b (computed once)
        # This is also handy as a stable extrapolation reference.
        self._a, self._b = self._linear_fit(self._raw, self._force)

        if self.method not in ("piecewise", "linear_fit"):
            raise ValueError("method must be 'piecewise' or 'linear_fit'.")

    @staticmethod
    def _load_points(csv_path: str) -> List[CalibrationPoint]:
        points: List[CalibrationPoint] = []
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("CSV has no header.")
            required = {"Force_N", "V3_mean"}
            if not required.issubset(set(reader.fieldnames)):
                raise ValueError(f"CSV must contain columns {required}, got {reader.fieldnames}")

            for row in reader:
                try:
                    force_n = float(row["Force_N"])
                    raw_v3 = float(row["V3_mean"])
                except (TypeError, ValueError):
                    continue
                points.append(CalibrationPoint(force_n=force_n, raw_v3=raw_v3))

        return points

    @staticmethod
    def _linear_fit(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
        # y = a*x + b
        a, b = np.polyfit(x, y, 1)
        return float(a), float(b)

    @property
    def points(self) -> List[CalibrationPoint]:
        return list(self._points)

    @property
    def linear_model(self) -> Tuple[float, float]:
        """Returns (a, b) for Force_N = a*V3_raw + b"""
        return self._a, self._b

    def raw_to_force(self, raw_v3: float, currentMeanValue: float) -> float:
        """
        Convert a single raw V3 value to force in Newtons,
        applying offset to the calibration raw values.
        """
        x = float(raw_v3)

        # Shift calibration raw axis
        offset = float(currentMeanValue) - float(self._raw[0])
        raw_shifted = self._raw + offset

        if self.method == "linear_fit":
            # Recompute linear model with shifted raw
            a, b = np.polyfit(raw_shifted, self._force, 1)
            return a * x + b

        if self.allow_extrapolation:
            if x <= raw_shifted[0]:
                return self._extrapolate_with_raw(x, raw_shifted, i0=0, i1=1)
            if x >= raw_shifted[-1]:
                return self._extrapolate_with_raw(x, raw_shifted, i0=-2, i1=-1)

        return float(np.interp(x, raw_shifted, self._force))
    
    def _extrapolate_with_raw(self, x: float, raw_arr: np.ndarray, i0: int, i1: int) -> float:
        x0, y0 = raw_arr[i0], self._force[i0]
        x1, y1 = raw_arr[i1], self._force[i1]

        if x1 == x0:
            return float(y0)

        slope = (y1 - y0) / (x1 - x0)
        return float(y0 + slope * (x - x0))
    
    def _extrapolate(self, x: float, i0: int, i1: int) -> float:
        x0, y0 = self._raw[i0], self._force[i0]
        x1, y1 = self._raw[i1], self._force[i1]
        if x1 == x0:
            # degenerate; fall back to y0
            return float(y0)
        slope = (y1 - y0) / (x1 - x0)
        return float(y0 + slope * (x - x0))

    def raw_to_force_and_mass(self, raw_v3: float, *, g: float = 9.81) -> Tuple[float, float]:
        """
        Returns (force_N, mass_kg), using mass = force/g.
        Since your setup is hanging weight, force is full gravitational force.
        """
        force_n = self.raw_to_force(raw_v3)
        mass_kg = force_n / float(g)
        return force_n, mass_kg


# ---------------- Example usage ----------------
if __name__ == "__main__":
    cal = V3ForceCalibrator("calibrationWeight/V3_calibration.csv", method="piecewise")

    raw = 25000.0
    force_n = cal.raw_to_force(raw)
    force_n2, mass_kg = cal.raw_to_force_and_mass(raw)

    print("Linear model Force = a*raw + b:", cal.linear_model)
    print(f"raw={raw:.2f} -> force={force_n:.2f} N (mass≈{mass_kg:.3f} kg)")