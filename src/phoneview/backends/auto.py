"""Pick the backend for whatever phone is plugged in (USB only).

Every few seconds adb (Android) and usbmuxd (iPhone) are asked what is connected.
The active backend keeps running while its phone is present; when it disappears
and another kind of phone is present, the other backend takes over. All backends
share one MediaHub so open browser sessions survive the switch.
"""
import asyncio
import contextlib
import logging

from phoneview.backends.base import Backend, capabilities

log = logging.getLogger(__name__)

DETECT_INTERVAL = 2.0
GENERATION_STRIDE = 1_000_000


async def detect_android(adb):
    """True if an Android phone is on USB; raises with the reason if adb fails."""
    devices = await adb.devices()
    if devices and not any(d.usb for d in devices):
        raise RuntimeError("adb only lists non-USB devices: " + ", ".join(d.serial for d in devices))
    return any(d.usb for d in devices)


async def detect_ios():
    with contextlib.suppress(Exception):
        from pymobiledevice3 import usbmux
        devices = await asyncio.wait_for(usbmux.list_devices(), 3)
        return any(d.is_usb for d in devices)
    return False


class AutoBackend(Backend):
    platform = "auto"

    def __init__(self, factories, hub=None, detectors=None):
        """factories: {"android": callable(hub) -> Backend, "ios": ...}
        detectors: {"android": async () -> bool, ...}"""
        super().__init__(hub)
        self.factories = factories
        self.detectors = detectors or {}
        self.current = None
        self.task = None
        self.switches = 0
        self.detected = []
        self.detect_errors = {}

    # -- delegation --------------------------------------------------------------

    @property
    def generation(self):
        inner = self.current.generation if self.current else 0
        return self.switches * GENERATION_STRIDE + inner

    @generation.setter
    def generation(self, value):
        pass  # derived

    def capabilities(self):
        return self.current.capabilities() if self.current else capabilities()

    def jpeg_budget_mbps(self):
        return self.current.jpeg_budget_mbps() if self.current else None

    def full_status(self):
        if self.current is None:
            error = "No phone detected. Connect an Android phone (USB debugging on) or an iPhone by USB."
            if "android" in self.detect_errors:
                error += f" Android/adb: {self.detect_errors['android']}"
            out = {"type": "status", "platform": "none", "generation": self.generation, "device": None,
                   "error": error}
        else:
            out = self.current.full_status()
            out["generation"] = self.generation
        out["detected"] = list(self.detected)
        return out

    async def handle_input(self, event):
        if self.current is None:
            return {"error": "No phone connected"}
        return await self.current.handle_input(event)

    async def update_settings(self, values):
        if self.current is None:
            raise ValueError("No phone connected")
        await self.current.update_settings(values)

    async def setup_action(self, name):
        if self.current is None:
            raise ValueError("No phone connected")
        return await self.current.setup_action(name)

    async def set_viewer(self, present):
        await super().set_viewer(present)
        if self.current is not None:
            await self.current.set_viewer(present)

    # -- lifecycle ------------------------------------------------------------------

    async def _switch(self, platform):
        await self._stop_current()
        self.switches += 1
        self.current = self.factories[platform](self.hub)
        self.current.state_changed = self.state_changed
        await self.current.set_viewer(self.viewer)
        self.task = asyncio.create_task(self.current.run(), name=f"backend-{platform}")
        log.info("Using the %s backend", platform)
        self.notify()

    async def _stop_current(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.current is not None:
            await self.current.close()
            self.current = None
            self.hub.request_keyframe = None
            self.hub.clear()

    async def run(self):
        try:
            while True:
                found = []
                for platform, detect in self.detectors.items():
                    try:
                        if await detect():
                            found.append(platform)
                        self.detect_errors.pop(platform, None)
                    except Exception as exc:
                        message = str(exc) or type(exc).__name__
                        if self.detect_errors.get(platform) != message:
                            log.warning("%s detection: %s", platform, message)
                        self.detect_errors[platform] = message
                self.detected = found
                current = self.current.platform if self.current else None
                if current not in found and found:
                    await self._switch(found[0])
                elif self.task is not None and self.task.done():
                    # A backend should never exit; restart it if it did.
                    with contextlib.suppress(BaseException):
                        self.task.result()
                    await self._switch(current)
                await asyncio.sleep(DETECT_INTERVAL)
        finally:
            await self._stop_current()

    async def close(self):
        await self._stop_current()
