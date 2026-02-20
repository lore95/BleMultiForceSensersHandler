import asyncio
import threading
from typing import Dict, Optional, List, Tuple, Callable, Awaitable

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

    def __init__(self, ble_loop: asyncio.AbstractEventLoop):
        self.ble_loop = ble_loop
        self.readers: Dict[str, AsyncSensorReader] = {}  # key: address
        self.on_state_change: Optional[Callable[[], None]] = None
        self.prompt_save_cb: Optional[Callable[[str, Optional[str]], Awaitable[bool]]] = None

    async def connect(self, address: str, *, tx_uuid: str = TX_UUID_DEFAULT) -> bool:
        if address in self.readers and self.readers[address].is_connected:
            return True

        reader = self.readers.get(address)
        if reader is None:
            async def _reader_prompt():
                if self.prompt_save_cb:
                    return await self.prompt_save_cb(address, None)
                return True
            reader = AsyncSensorReader(
                ble_address=address,
                tx_uuid=tx_uuid,
                ble_loop=self.ble_loop,
                prompt_save_cb=_reader_prompt,
            )
            self.readers[address] = reader
            reader.state_change_cb = lambda: (self.on_state_change() if self.on_state_change else None)
        reader.disconnected_callback = self._make_disconnected_cb(address)

        # connect
        return await reader.connect_device()

    def _make_disconnected_cb(self, addr: str):
        def _on_disconnect(_client):
            asyncio.create_task(self._handle_unexpected_disconnect(addr))
        return _on_disconnect

    async def _handle_unexpected_disconnect(self, addr: str):
        reader = self.readers.get(addr)
        if not reader:
            return
        try:
            reader.is_connected = False
        except Exception:
            pass
        if not getattr(reader, "is_reading", False):
            return

        # Ask user whether to save/stop
        if not self.prompt_save_cb:
            return
        name = getattr(reader, "name", None)

        try:
            want_save = await self.prompt_save_cb(addr, name)
        except Exception:
            want_save = False

        if not want_save:
            return

        try:
            if hasattr(reader, "stop_reading"):
                await reader.stop_reading(...)
            elif hasattr(reader, "save_collected_data"):
                await reader.save_collected_data()
        except Exception:
            print(Exception)
            pass

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
    