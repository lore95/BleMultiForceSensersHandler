# Controllers/ble_manager.py
import asyncio
import threading
from typing import Dict, Optional, List, Tuple

from bleak import BleakScanner

from Controller.sensorcontroller import AsyncSensorReader

TX_UUID_DEFAULT = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify


class BLELoopThread:
    """Runs a dedicated asyncio loop in a background thread."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        """Schedule a coroutine onto the BLE loop, returns concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class BLEManager:
    """
    - Scans for devices
    - Keeps AsyncSensorReader instances per connected device
    """
    def __init__(self, ble_loop: asyncio.AbstractEventLoop):
        self.ble_loop = ble_loop
        self.readers: Dict[str, AsyncSensorReader] = {}  # key: address

    async def scan_force_devices(self, name_contains: str = "force", timeout: float = 6.0) -> List[Tuple[str, str]]:
        """
        Returns list of (address, name) for devices whose name contains name_contains (case-insensitive).
        """
        devices = await BleakScanner.discover(timeout=timeout)
        out = []
        needle = (name_contains or "").lower()
        for d in devices:
            name = d.name or ""
            if needle in name.lower():
                out.append((d.address, name))
        out.sort(key=lambda x: x[1].lower())
        return out

    async def connect(self, address: str, *, tx_uuid: str = TX_UUID_DEFAULT) -> bool:
        if address in self.readers and self.readers[address].is_connected:
            return True

        reader = self.readers.get(address)
        if reader is None:
            reader = AsyncSensorReader(
                ble_address=address,
                tx_uuid=tx_uuid,
                ble_loop=self.ble_loop,
                prompt_save_cb=None,  # you can wire a UI callback later
            )
            self.readers[address] = reader

        return await reader.connect_device()

    async def disconnect(self, address: str) -> bool:
        reader = self.readers.get(address)
        if not reader:
            return True
        ok = await reader.disconnect_device()
        return ok

    async def disconnect_all(self):
        for addr in list(self.readers.keys()):
            try:
                await self.disconnect(addr)
            except Exception:
                pass