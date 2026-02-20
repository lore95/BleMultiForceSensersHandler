import asyncio
import time
import os
import csv
import numpy as np
from datetime import date
import re
from typing import Callable, Optional, Awaitable, Any, Dict, Tuple

from bleak import BleakClient
from bleak.exc import BleakError, BleakDBusError

from Utils.sensorForceConverter import V3ForceCalibrator

LINE_RE = re.compile(
    r"Time:(-?\d+),V1:(-?\d+(?:\.\d+)?),V2:(-?\d+(?:\.\d+)?),V3:(-?\d+(?:\.\d+)?),V4:(-?\d+(?:\.\d+)?)"
)


def hampel_filter(vals, window_size=11, n_sigmas=5.0):
    """Simple Hampel filter to remove spikes."""
    n = len(vals)
    half_w = window_size // 2
    k = 1.4826
    filtered = vals.copy()
    for i in range(n):
        start = max(0, i - half_w)
        end = min(n, i + half_w + 1)
        window = vals[start:end]
        med = np.median(window)
        mad = np.median(np.abs(window - med))
        if mad == 0:
            continue
        threshold = n_sigmas * k * mad
        if abs(vals[i] - med) > threshold:
            filtered[i] = med
    return filtered


class AsyncSensorReader:
    """
    BLE sensor reader with:
      - start_notify handler to collect raw + force
      - unexpected disconnect handling via Bleak disconnected_callback
      - async prompt_save_cb(addr, name) shown ONLY on unexpected disconnect
      - disconnect_error flag for UI red dot
      - state_change_cb callback for UI redraw
      - stop_reading returns saved filename (or None)
    """

    def __init__(
        self,
        ble_address: str,
        tx_uuid: str,
        ble_loop: asyncio.AbstractEventLoop,
        *,
        prompt_save_cb: Optional[Callable[[str, Optional[str]], Awaitable[bool]]] = None,
        calibration_csv: str = "calibrationWeight/V3_calibration.csv",
    ):
        self.ble_address = ble_address
        self.tx_uuid = tx_uuid
        self.ble_loop = ble_loop

        self.client: Optional[BleakClient] = None
        self.is_connected = False
        self.is_reading = False

        # UI flags
        self.disconnect_error = False  # True only for unexpected disconnects
        self.state_change_cb: Optional[Callable[[], None]] = None

        # internal disconnect bookkeeping
        self._intentional_disconnect = False
        self._disconnect_lock = asyncio.Lock()

        # baseline / conversion
        self.offSetValue = 0.0
        self.calibrator = V3ForceCalibrator(
            calibration_csv,
            method="piecewise",
            allow_extrapolation=True,
        )

        # data
        self.collected_raw_data: list[Tuple[float, float]] = []
        self.collected_force_data: list[Tuple[float, float]] = []

        # optional UI prompt on unexpected disconnect
        self.prompt_save_cb = prompt_save_cb

        # meta used for saving on stop or error disconnect
        self._pending_meta: Dict[str, Any] = {
            "athlete_id": "UNKNOWN",
            "distance_cm": 0.0,
            "weight_kg": 0,
        }

        # optional (if you ever want to show a friendly name in the popup)
        self.name: Optional[str] = None

    # -------------------- Notifications --------------------
    def notification_handler(self, sender: int, data: bytearray):
        if not self.is_reading:
            return

        host_time = time.time()
        try:
            line = data.decode("utf-8", errors="ignore").strip()
            m = LINE_RE.match(line)
            if not m:
                return

            v3_raw = float(m.group(4))
            self.collected_raw_data.append((host_time, v3_raw))

            v3_force = self.calibrator.raw_to_force(v3_raw, self.offSetValue)
            self.collected_force_data.append((host_time, v3_force))

        except Exception as e:
            print(f"[SENSOR] notification_handler error: {e}")

    # -------------------- Disconnect handling --------------------
    def _on_disconnect(self, _client: BleakClient):
        # If we triggered disconnect ourselves, do NOT treat it as an error.
        if self._intentional_disconnect:
            return

        print("[SENSOR] ⚠️ Device disconnected unexpectedly.")
        self.disconnect_error = True
        self.is_reading = False
        self.is_connected = False
        self._notify_state_change()

        try:
            asyncio.run_coroutine_threadsafe(self._handle_disconnect(), self.ble_loop)
        except Exception as e:
            print(f"[SENSOR] Failed to schedule disconnect handler: {e}")

    async def _handle_disconnect(self):
        async with self._disconnect_lock:
            was_reading = bool(self.collected_raw_data) and bool(self.collected_force_data)

            # Best-effort cleanup
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None

            # Only show popup on ERROR disconnect
            save = True
            if self.disconnect_error and self.prompt_save_cb is not None and was_reading:
                try:
                    save = await self.prompt_save_cb(self.ble_address, self.name)
                except Exception as e:
                    print(f"[SENSOR] prompt_save_cb failed: {e}")
                    save = True

            if (not was_reading) or (not save):
                self._clear_buffers()
                return

            meta = dict(self._pending_meta)
            athlete_id = meta.get("athlete_id", "UNKNOWN")
            distance_cm = float(meta.get("distance_cm", 0.0))
            weight_kg = int(meta.get("weight_kg", 0))

            try:
                filename = await asyncio.to_thread(
                    self._save_data,
                    self.collected_raw_data,
                    self.collected_force_data,
                    athlete_id,
                    distance_cm,
                    weight_kg,
                )
                print(f"[SENSOR] Saved partial data: {filename}")
            finally:
                self._clear_buffers()

    def _clear_buffers(self):
        self.collected_raw_data.clear()
        self.collected_force_data.clear()
        self._pending_meta = {"athlete_id": "UNKNOWN", "distance_cm": 0.0, "weight_kg": 0}

    def _notify_state_change(self):
        cb = self.state_change_cb
        if cb:
            try:
                cb()
            except Exception:
                pass

    # -------------------- Connect / Disconnect --------------------
    async def connect_device(self) -> bool:
        # reset flags for a fresh session
        self._intentional_disconnect = False
        self.disconnect_error = False
        self._notify_state_change()

        # close any old client
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        print(f"\n[SENSOR] Attempting connection to BLE address: {self.ble_address}...")
        try:
            self.client = BleakClient(
                self.ble_address,
                timeout=20.0,
                disconnected_callback=self._on_disconnect,
            )
            await self.client.connect()

            if not self.client.is_connected:
                print("[SENSOR] Failed to connect.")
                self.is_connected = False
                self._notify_state_change()
                return False

            # Baseline offset calibration
            baseline_samples: list[float] = []
            print("[SENSOR] Calibrating baseline for force conversion...")

            def _baseline_handler(sender: int, data: bytearray):
                try:
                    line = data.decode("utf-8", errors="ignore").strip()
                    m = LINE_RE.match(line)
                    if not m:
                        return
                    v3_raw = float(m.group(4))
                    baseline_samples.append(v3_raw)
                except Exception:
                    return

            await self.client.start_notify(self.tx_uuid, _baseline_handler)
            print("[SENSOR] Collecting baseline for 5 seconds...")
            await asyncio.sleep(5.0)

            try:
                await self.client.stop_notify(self.tx_uuid)
            except Exception:
                pass

            if baseline_samples:
                self.offSetValue = float(np.median(np.array(baseline_samples, dtype=float)))
                print(f"[SENSOR] Baseline median set: offSetValue={self.offSetValue:.3f} (n={len(baseline_samples)})")
            else:
                self.offSetValue = 0.0
                print("[SENSOR] No baseline samples received. offSetValue set to 0.0")

            # Start real notifications
            await self.client.start_notify(self.tx_uuid, self.notification_handler)

            self.is_connected = True
            self.disconnect_error = False
            self._notify_state_change()
            print("[SENSOR] ✅ Connected. Notifications activated.")
            return True

        except (BleakError, BleakDBusError) as e:
            print(f"[SENSOR] ❌ Connection/Discovery Error: {e}")
        except Exception as e:
            print(f"[SENSOR] ❌ Unexpected error: {e}")

        self.is_connected = False
        self.disconnect_error = False
        self.client = None
        self._notify_state_change()
        return False

    async def disconnect_device(self) -> bool:
        # Intentional disconnect: no popup, no red dot
        self._intentional_disconnect = True
        self.is_reading = False

        try:
            if self.client:
                try:
                    await self.client.stop_notify(self.tx_uuid)
                except Exception:
                    pass
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None

            self.is_connected = False
            self.disconnect_error = False
            self._notify_state_change()
            print("[SENSOR] Explicitly disconnected.")
            return True
        finally:
            # reset so future disconnects can be treated as error if they happen
            self._intentional_disconnect = False

    close = disconnect_device

    # -------------------- Reading control --------------------
    async def start_reading(
        self,
        athlete_id: str = "UNKNOWN",
        distance_cm: float = 0.0,
        weight_kg: int = 0,
        direction: int = 0,
    ) -> bool:
        if not (self.client and self.client.is_connected):
            return False

        self.collected_raw_data.clear()
        self.collected_force_data.clear()

        # store meta for filename
        self._pending_meta = {
            "athlete_id": str(athlete_id or "UNKNOWN"),
            "distance_cm": float(distance_cm),
            "weight_kg": int(weight_kg),
        }

        if direction == 0:
            self.is_reading = True

        return True

    async def stop_reading(self, athlete_id: str = "UNKNOWN", distance_cm: float = 0.0, weight_kg: int = 0):
        self.is_reading = False
        print("[SENSOR] Data logging stopped. Saving data...")

        filename = await asyncio.to_thread(
            self._save_data,
            self.collected_raw_data,
            self.collected_force_data,
            athlete_id,
            distance_cm,
            weight_kg,
        )
        self._clear_buffers()
        return filename

    # -------------------- Save --------------------
    def _save_data(self, log_raw_data, log_force_data, athlete_id: str, distance_cm: float, weight_kg: int):
        os.makedirs("readings", exist_ok=True)

        today_str = date.today().isoformat()
        athlete_id = (athlete_id or "").strip()

        save_dir = os.path.join("readings", f"{today_str}_{athlete_id}" if athlete_id else today_str)
        os.makedirs(save_dir, exist_ok=True)

        timestamp_s = int(time.time())
        dist_str = f"{int(distance_cm)}cm"
        weight_str = f"{int(weight_kg)}kg"

        filename = os.path.join(save_dir, f"{timestamp_s}_{dist_str}_{weight_str}_grip_data.csv")

        combined = [
            (t_raw, raw, force)
            for (t_raw, raw), (_, force) in zip(log_raw_data, log_force_data)
        ]

        if not combined:
            print("[SAVE] No data to save.")
            return None

        raw_values = np.array([raw for _, raw, _ in combined], dtype=float)
        filtered_raw = hampel_filter(raw_values, window_size=11, n_sigmas=5.0)

        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Host_Time_s", "Raw_V3", "Force_N", "Raw_V3_Filtered"])
            for (host_time, raw, force), raw_filt in zip(combined, filtered_raw):
                writer.writerow([f"{host_time:.6f}", float(raw), float(force), float(raw_filt)])

        print(f"[SAVE] Saved {len(combined)} samples to {filename}")
        return filename