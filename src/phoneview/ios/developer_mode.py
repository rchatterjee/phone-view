"""Developer Mode status and reveal-only setup; never enables or reboots a phone."""
import asyncio
from contextlib import suppress

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.amfi import AmfiService


class DeveloperModeMonitor:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._udid = None
        self._reveal_attempted = False
        self.state = "checking"
        self.reveal = "not_requested"
        self.ios_compatible = None
        self.message = "Waiting to check the connected iPhone."

    def snapshot(self):
        return dict(state=self.state, reveal=self.reveal, message=self.message,
                    ios_compatible=self.ios_compatible)

    def disconnected(self):
        self.state = "unavailable"
        self.ios_compatible = None
        self.message = "Connect the iPhone by USB, unlock it, and accept Trust This Computer."

    async def check(self, lockdown, *, retry_reveal=False):
        async with self._lock:
            if self._udid != lockdown.udid:
                self._udid = lockdown.udid
                self._reveal_attempted = False
                self.reveal = "not_requested"
            major = str(lockdown.all_values.get("ProductVersion", "")).split(".", 1)[0]
            self.ios_compatible = int(major) >= 15 if major.isdigit() else None
            if major.isdigit() and int(major) < 16:
                self.state = "not_required"
                self.message = "This iOS version predates the Developer Mode setting."
                return None
            try:
                # Read the same AMFI key as get_developer_mode_status(), but do
                # not misinterpret a missing key (None) as a confirmed False.
                value = await asyncio.wait_for(lockdown.get_value(
                    domain="com.apple.security.mac.amfi", key="DeveloperModeStatus"), 5)
            except Exception:
                value = None
            if type(value) not in (bool, int) or value not in (0, 1):
                self.state = "unknown"
                self.message = "Could not read Developer Mode. Keep the phone unlocked and trusted, then check again."
                return None
            if bool(value):
                self.state = "enabled"
                self.reveal = "not_requested"
                self._reveal_attempted = False
                self.message = "Developer Mode is enabled."
                return True
            self.state = "disabled"
            self.message = "Enable Developer Mode on the iPhone using the steps below."
            if retry_reveal or not self._reveal_attempted:
                self._reveal_attempted = True
                service = None
                try:
                    service = await asyncio.wait_for(lockdown.start_lockdown_service(AmfiService.SERVICE_NAME), 5)
                    response = await asyncio.wait_for(service.send_recv_plist(
                        {"action": AmfiService.DEVELOPER_MODE_REVEAL}), 5)
                    self.reveal = "revealed" if isinstance(response, dict) and response.get("success") is True else "failed"
                except Exception:
                    self.reveal = "failed"
                finally:
                    if service is not None:
                        with suppress(Exception):
                            await asyncio.wait_for(service.close(), 2)
            return False

    async def check_usb(self, serial=None, *, retry_reveal=False):
        lockdown = None
        try:
            lockdown = await asyncio.wait_for(create_using_usbmux(serial=serial), 8)
            await self.check(lockdown, retry_reveal=retry_reveal)
        except Exception:
            self.disconnected()
        finally:
            if lockdown is not None:
                with suppress(Exception):
                    await asyncio.wait_for(lockdown.close(), 2)
        return self.snapshot()
