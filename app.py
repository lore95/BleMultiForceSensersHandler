# app.py
import os
import tkinter as tk
from tkinter import ttk, messagebox

from Controller.ble_controller import BLELoopThread, BLEManager


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Force BLE Sensors")
        self.geometry("720x420")

        # BLE loop in background thread
        self.ble_thread = BLELoopThread()
        self.ble_manager = BLEManager(self.ble_thread.loop)

        # UI state
        self.devices = []           # list of (address, name) from last scan
        self.addr_to_name = {}      # address -> name
        self.reading_active = False

        # Track last saved filename used in status bar at the end of saving
        self.last_saved_file = None

        self._build_ui()

        # Initial scan
        self.refresh_devices()

        # Proper shutdown
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI  ----------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        # ---------- Row 1 (devices + refresh/connect/disconnect) ----------
        row1 = ttk.Frame(root)
        row1.pack(fill="x")

        ttk.Label(row1, text="Devices containing:").pack(side="left")
        self.filter_var = tk.StringVar(value="force")
        ttk.Entry(row1, textvariable=self.filter_var, width=18).pack(side="left", padx=(6, 12))

        ttk.Button(row1, text="Refresh", command=self.refresh_devices).pack(side="left")
        ttk.Button(row1, text="Connect", command=self.connect_selected).pack(side="left", padx=6)
        ttk.Button(row1, text="Disconnect", command=self.disconnect_selected).pack(side="left")

        # ---------- Row 2 (athlete + distance + weight) ----------
        row2 = ttk.Frame(root)
        row2.pack(fill="x", pady=(8, 0))

        ttk.Label(row2, text="Athlete ID:").pack(side="left")
        self.athlete_var = tk.StringVar(value="UNKNOWN")
        ttk.Entry(row2, textvariable=self.athlete_var, width=18).pack(side="left", padx=(6, 12))

        ttk.Label(row2, text="Distance (cm):").pack(side="left")
        self.distance_var = tk.StringVar(value="0")
        ttk.Entry(row2, textvariable=self.distance_var, width=10).pack(side="left", padx=(6, 12))

        ttk.Label(row2, text="Weight (kg):").pack(side="left")
        self.weight_var = tk.StringVar(value="0")
        ttk.Entry(row2, textvariable=self.weight_var, width=10).pack(side="left", padx=(6, 12))

        # ---------- Middle (device list) ----------
        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True, pady=(12, 0))

        self.listbox = tk.Listbox(mid, height=14, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)

        # ---------- Bottom status + start/stop ----------
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(12, 0))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")

        btns = ttk.Frame(bottom)
        btns.pack(side="right")

        self.start_btn = ttk.Button(
            btns,
            text="🟢 Start Reading",
            command=self.start_reading,
            state="disabled",
            style="Start.Disabled.TButton",
        )
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(
            btns,
            text="🔴 Stop Reading",
            command=self.stop_reading,
            state="disabled",
            style="Stop.Disabled.TButton",
        )
        self.stop_btn.pack(side="left")

        # ---- Styles for Start/Stop (enabled vs disabled) ----
        style = ttk.Style(self)

        # Start button
        style.configure("Start.Enabled.TButton")
        style.configure("Start.Disabled.TButton")

        # Stop button
        style.configure("Stop.Enabled.TButton")
        style.configure("Stop.Disabled.TButton")
        # Make disabled look more disabled across themes (best effort)
        style.map(
            "Start.Enabled.TButton",
            foreground=[("disabled", "gray50")],
        )
        style.map(
            "Start.Disabled.TButton",
            foreground=[("disabled", "gray50")],
        )
        style.map(
            "Stop.Enabled.TButton",
            foreground=[("disabled", "gray50")],
        )
        style.map(
            "Stop.Disabled.TButton",
            foreground=[("disabled", "gray50")],
        )

    # ---------------- UI HELPERS ----------------
    def set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def _get_athlete_id(self) -> str:
        aid = (self.athlete_var.get() or "").strip()
        return aid if aid else "UNKNOWN"

    def _get_distance_cm(self) -> float:
        s = (self.distance_var.get() or "").strip()
        try:
            return float(s) if s else 0.0
        except ValueError:
            return 0.0

    def _get_weight_kg(self) -> int:
        s = (self.weight_var.get() or "").strip()
        try:
            return int(float(s)) if s else 0
        except ValueError:
            return 0

    def _get_connected_addresses(self):
        connected = []
        for addr, reader in self.ble_manager.readers.items():
            if reader and getattr(reader, "is_connected", False):
                connected.append(addr)
        return connected

    def _update_action_buttons(self):
        connected = self._get_connected_addresses()

        start_enabled = (len(connected) > 0 and not self.reading_active)
        stop_enabled = self.reading_active

        # Start
        self.start_btn.config(
            state=("normal" if start_enabled else "disabled"),
            style=("Start.Enabled.TButton" if start_enabled else "Start.Disabled.TButton"),
        )

        # Stop
        self.stop_btn.config(
            state=("normal" if stop_enabled else "disabled"),
            style=("Stop.Enabled.TButton" if stop_enabled else "Stop.Disabled.TButton"),
        )

    def _render_list(self):
        """
        Re-render listbox with live connection state, keeping selection best-effort.
        """
        old_selection = set(self.listbox.curselection())

        self.listbox.delete(0, tk.END)
        connected = set(self._get_connected_addresses())

        for i, (addr, name) in enumerate(self.devices):
            prefix = "🟢 " if addr in connected else "⚪ "
            self.listbox.insert(tk.END, f"{prefix}{name}   ({addr})")

        for idx in old_selection:
            if 0 <= idx < len(self.devices):
                self.listbox.selection_set(idx)

        self._update_action_buttons()

    def _get_selected_addresses(self):
        idxs = self.listbox.curselection()
        if not idxs:
            return []
        return [self.devices[i][0] for i in idxs if 0 <= i < len(self.devices)]

    def _poll_future(self, fut, on_done):
        if fut.done():
            try:
                result = fut.result()
            except Exception as e:
                self.set_status(f"Error: {e}")
                messagebox.showerror("Error", str(e))
                return
            on_done(result)
        else:
            self.after(50, lambda: self._poll_future(fut, on_done))

    # ---------------- SCAN / CONNECT / DISCONNECT ----------------
    def refresh_devices(self):
        self.set_status("Scanning...")
        needle = self.filter_var.get().strip() or "force"
        fut = self.ble_thread.submit(self.ble_manager.scan_force_devices(name_contains=needle, timeout=6.0))
        self.after(50, lambda: self._poll_future(fut, self._on_scan_result))

    def _on_scan_result(self, devices):
        self.devices = devices
        self.addr_to_name = {addr: name for addr, name in devices}
        self._render_list()
        self.set_status(f"Found {len(devices)} device(s).")

    def connect_selected(self):
        addrs = self._get_selected_addresses()
        if not addrs:
            messagebox.showinfo("Connect", "Select one or more devices first.")
            return

        self.set_status(f"Connecting to {len(addrs)} device(s)...")
        self._connect_next(addrs, idx=0)

    def _connect_next(self, addrs, idx: int):
        if idx >= len(addrs):
            self.set_status("Connect done.")
            self._render_list()
            return

        addr = addrs[idx]
        fut = self.ble_thread.submit(self.ble_manager.connect(addr))
        self.after(50, lambda: self._poll_future(fut, lambda ok: self._on_connect_one(addrs, idx, addr, ok)))

    def _on_connect_one(self, addrs, idx, addr, ok: bool):
        if not ok:
            messagebox.showwarning("Connect", f"Could not connect to {self.addr_to_name.get(addr, addr)}")
        self._render_list()
        self._connect_next(addrs, idx + 1)

    def disconnect_selected(self):
        addrs = self._get_selected_addresses()
        if not addrs:
            messagebox.showinfo("Disconnect", "Select one or more devices first.")
            return

        if self.reading_active:
            messagebox.showinfo("Disconnect", "Stop reading before disconnecting.")
            return

        self.set_status(f"Disconnecting {len(addrs)} device(s)...")
        self._disconnect_next(addrs, idx=0)

    def _disconnect_next(self, addrs, idx: int):
        if idx >= len(addrs):
            self.set_status("Disconnect done.")
            self._render_list()
            return

        addr = addrs[idx]
        fut = self.ble_thread.submit(self.ble_manager.disconnect(addr))
        self.after(50, lambda: self._poll_future(fut, lambda ok: self._on_disconnect_one(addrs, idx, addr, ok)))

    def _on_disconnect_one(self, addrs, idx, addr, ok: bool):
        self._render_list()
        self._disconnect_next(addrs, idx + 1)

    # ---------------- START / STOP READING ----------------
    def start_reading(self):
        connected = self._get_connected_addresses()
        if not connected:
            messagebox.showinfo("Start Reading", "No connected devices.")
            return

        athlete_id = self._get_athlete_id()
        distance_cm = self._get_distance_cm()
        weight_kg = self._get_weight_kg()

        self.last_saved_file = None

        self.set_status(
            f"Starting reading on {len(connected)} device(s)...  "
            f"Athlete ID: {athlete_id}, Distance: {distance_cm:g} cm, Weight: {weight_kg} kg"
        )
        self.start_btn.config(state="disabled")
        self._update_action_buttons()
        self._start_reading_next(connected, idx=0, athlete_id=athlete_id, distance_cm=distance_cm, weight_kg=weight_kg)

    def _start_reading_next(self, addrs, idx: int, athlete_id: str, distance_cm: float, weight_kg: int):
        if idx >= len(addrs):
            self.reading_active = True
            self.set_status(f"Reading started. Athlete ID: {athlete_id}")
            self._update_action_buttons()
            return

        addr = addrs[idx]
        reader = self.ble_manager.readers.get(addr)
        if not reader or not reader.is_connected:
            return self._start_reading_next(addrs, idx + 1, athlete_id, distance_cm, weight_kg)

        # If your controller supports these args, keep them. If not, remove distance/weight.
        fut = self.ble_thread.submit(
            reader.start_reading(athlete_id=athlete_id, distance_cm=distance_cm, weight_kg=weight_kg, direction=0)
        )

        self.after(50, lambda: self._poll_future(
            fut, lambda ok: self._on_start_reading_one(addrs, idx, addr, ok, athlete_id, distance_cm, weight_kg)
        ))

    def _on_start_reading_one(self, addrs, idx, addr, ok: bool, athlete_id: str, distance_cm: float, weight_kg: int):
        if not ok:
            messagebox.showwarning("Start Reading", f"Failed to start reading on {self.addr_to_name.get(addr, addr)}")
        self._start_reading_next(addrs, idx + 1, athlete_id, distance_cm, weight_kg)

    def stop_reading(self):
        if not self.reading_active:
            return

        connected = self._get_connected_addresses()
        if not connected:
            self.reading_active = False
            self._update_action_buttons()
            self.set_status("Reading stopped (no connected devices).")
            return

        athlete_id = self._get_athlete_id()
        distance_cm = self._get_distance_cm()
        weight_kg = self._get_weight_kg()

        self.set_status(f"Stopping reading on {len(connected)} device(s)...")
        self.stop_btn.config(state="disabled")
        self._update_action_buttons()
        self._stop_reading_next(connected, idx=0, athlete_id=athlete_id, distance_cm=distance_cm, weight_kg=weight_kg)

    def _stop_reading_next(self, addrs, idx: int, athlete_id: str, distance_cm: float, weight_kg: int):
        if idx >= len(addrs):
            self.reading_active = False
            self._update_action_buttons()

            if self.last_saved_file:
                self.set_status(f"Stop done. Last saved: {os.path.basename(self.last_saved_file)}")
            else:
                self.set_status("Stop done. No file saved.")

            return

        addr = addrs[idx]
        reader = self.ble_manager.readers.get(addr)
        if not reader:
            return self._stop_reading_next(addrs, idx + 1, athlete_id, distance_cm, weight_kg)

        fut = self.ble_thread.submit(
            reader.stop_reading(athlete_id=athlete_id, distance_cm=distance_cm, weight_kg=weight_kg)
        )
        self.after(50, lambda: self._poll_future(
            fut, lambda filename: self._on_stop_reading_one(addrs, idx, addr, filename, athlete_id, distance_cm, weight_kg)
        ))

    def _on_stop_reading_one(self, addrs, idx, addr, filename, athlete_id: str, distance_cm: float, weight_kg: int):
        if filename:
            self.last_saved_file = filename
            self.set_status(f"Saved: {os.path.basename(filename)}")
        else:
            self.set_status(f"No data saved for {self.addr_to_name.get(addr, addr)}")

        self._stop_reading_next(addrs, idx + 1, athlete_id, distance_cm, weight_kg)

    # ---------------- CLOSE ----------------
    def on_close(self):
        # If reading, try to stop first (best effort)
        try:
            if self.reading_active:
                athlete_id = self._get_athlete_id()
                distance_cm = self._get_distance_cm()
                weight_kg = self._get_weight_kg()
                connected = self._get_connected_addresses()

                for addr in connected:
                    reader = self.ble_manager.readers.get(addr)
                    if reader:
                        self.ble_thread.submit(
                            reader.stop_reading(athlete_id=athlete_id, distance_cm=distance_cm, weight_kg=weight_kg)
                        )
        except Exception:
            pass

        # Disconnect everything
        try:
            fut = self.ble_thread.submit(self.ble_manager.disconnect_all())
            fut.result(timeout=6)
        except Exception:
            pass

        try:
            self.ble_thread.stop()
        except Exception:
            pass

        self.destroy()


if __name__ == "__main__":
    App().mainloop()