import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from phoneview.ios.developer_mode import DeveloperModeMonitor
from phoneview.ios.connection import open_device, DeveloperModeRequiredError, UnsupportedIOSVersionError


class DeveloperModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.monitor = DeveloperModeMonitor()
        self.service = SimpleNamespace(send_recv_plist=AsyncMock(return_value={"success": True}), close=AsyncMock())
        self.phone = SimpleNamespace(udid="phone", all_values={"ProductVersion": "16.7.10"},
                                     get_value=AsyncMock(return_value=False),
                                     start_lockdown_service=AsyncMock(return_value=self.service), close=AsyncMock())

    async def test_disabled_reveals_once_and_never_requests_enable_or_restart(self):
        self.assertFalse(await self.monitor.check(self.phone))
        self.assertFalse(await self.monitor.check(self.phone))
        self.service.send_recv_plist.assert_awaited_once_with({"action": 0})
        self.service.close.assert_awaited_once()
        self.assertEqual(self.monitor.snapshot()["state"], "disabled")
        self.assertEqual(self.monitor.snapshot()["reveal"], "revealed")
        # iOS 16 is served by the legacy screenshotr/WebDriverAgent backend now.
        self.assertTrue(self.monitor.snapshot()["ios_compatible"])

    async def test_enabled_does_not_reveal_and_resets_off_episode(self):
        await self.monitor.check(self.phone)
        self.phone.get_value.return_value = True
        self.assertTrue(await self.monitor.check(self.phone))
        self.assertEqual(self.monitor.state, "enabled")
        self.assertEqual(self.service.send_recv_plist.await_count, 1)
        self.phone.get_value.return_value = False
        await self.monitor.check(self.phone)
        self.assertEqual(self.service.send_recv_plist.await_count, 2)

    async def test_unknown_status_does_not_assume_off_or_reveal(self):
        for value in [None, "false", {}, 2]:
            self.phone.get_value.return_value = value
            self.assertIsNone(await self.monitor.check(self.phone))
            self.assertEqual(self.monitor.state, "unknown")
        self.phone.start_lockdown_service.assert_not_called()

    async def test_failed_reveal_is_retryable_without_repeated_automatic_requests(self):
        self.service.send_recv_plist.return_value = {"success": False, "private": "must not reach UI"}
        await self.monitor.check(self.phone)
        await self.monitor.check(self.phone)
        self.assertEqual(self.monitor.reveal, "failed")
        self.assertEqual(self.service.send_recv_plist.await_count, 1)
        self.service.send_recv_plist.return_value = {"success": True}
        await self.monitor.check(self.phone, retry_reveal=True)
        self.assertEqual(self.monitor.reveal, "revealed")
        self.assertNotIn("private", str(self.monitor.snapshot()))

    async def test_check_usb_closes_connection_and_handles_disconnection(self):
        self.phone.get_value.return_value = True
        with patch("phoneview.ios.developer_mode.create_using_usbmux", AsyncMock(return_value=self.phone)):
            result = await self.monitor.check_usb()
            self.assertEqual(result["state"], "enabled")
        self.phone.close.assert_awaited_once()
        with patch("phoneview.ios.developer_mode.create_using_usbmux", AsyncMock(side_effect=OSError())):
            result = await self.monitor.check_usb()
            self.assertEqual(result["state"], "unavailable")
            self.assertIsNone(result["ios_compatible"])

    async def test_cancelling_reveal_closes_amfi_service(self):
        started = asyncio.Event()
        async def waiting(*args):
            started.set()
            await asyncio.Event().wait()
        self.service.send_recv_plist.side_effect = waiting
        task = asyncio.create_task(self.monitor.check(self.phone))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.service.close.assert_awaited_once()

    async def test_ios16_setup_is_checked_before_reporting_backend_incompatibility(self):
        with patch("phoneview.ios.connection.create_using_usbmux", AsyncMock(return_value=self.phone)):
            with self.assertRaises(UnsupportedIOSVersionError):
                async with open_device(on_device=self.monitor.check):
                    pass
        self.assertEqual(self.monitor.state, "disabled")
        self.assertEqual(self.monitor.reveal, "revealed")
        self.phone.close.assert_awaited_once()

    async def test_ios17_off_blocks_tunnel_until_user_finishes_setup(self):
        self.phone.all_values["ProductVersion"] = "17.4"
        with patch("phoneview.ios.connection.create_using_usbmux", AsyncMock(return_value=self.phone)), patch(
                "phoneview.ios.connection.PreferredRsdTunnel", Mock()) as tunnel:
            with self.assertRaises(DeveloperModeRequiredError):
                async with open_device(on_device=self.monitor.check):
                    pass
            tunnel.assert_not_called()
