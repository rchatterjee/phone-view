from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pymobiledevice3.exceptions import UserspaceTunnelUnavailableError, TunneldConnectionError

from phoneview.ios.connection import open_device, UnsupportedIOSVersionError, TunnelSetupError


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lockdown = SimpleNamespace(udid="selected-phone", all_values={"ProductVersion": "17.4"}, close=AsyncMock())
        self.rsd = SimpleNamespace(udid="selected-phone", all_values={"ProductVersion": "17.4"},
                                   connect=AsyncMock(), close=AsyncMock())
        self.usb = patch("phoneview.ios.connection.create_using_usbmux", AsyncMock(return_value=self.lockdown))
        self.rsd_patch = patch("phoneview.ios.connection.RemoteServiceDiscoveryService", Mock(return_value=self.rsd))
        self.usb_mock = self.usb.start()
        self.rsd_factory = self.rsd_patch.start()

    async def asyncTearDown(self):
        self.rsd_patch.stop()
        self.usb.stop()

    async def test_ios16_rejected_before_any_tunnel_attempt(self):
        self.lockdown.all_values["ProductVersion"] = "16.7.10"
        with patch("phoneview.ios.connection.PreferredRsdTunnel") as userspace, patch("phoneview.ios.connection.get_tunneld_tunnels") as daemon:
            with self.assertRaisesRegex(UnsupportedIOSVersionError, "iOS 16"):
                async with open_device():
                    self.fail("Unsupported backend should not open")
            userspace.assert_not_called()
            daemon.assert_not_called()
        self.lockdown.close.assert_awaited_once()

    async def test_normal_userspace_path_and_body_errors_do_not_fallback(self):
        @asynccontextmanager
        async def preferred(serial):
            self.assertEqual(serial, "selected-phone")
            yield self.rsd
        with patch("phoneview.ios.connection.PreferredRsdTunnel", preferred), patch("phoneview.ios.connection.get_tunneld_tunnels") as daemon:
            with self.assertRaises(UserspaceTunnelUnavailableError):
                async with open_device() as rsd:
                    self.assertIs(rsd, self.rsd)
                    raise UserspaceTunnelUnavailableError()
            daemon.assert_not_called()

    async def test_fallback_matches_selected_usb_phone_and_closes_rsd(self):
        @asynccontextmanager
        async def unavailable(serial):
            raise UserspaceTunnelUnavailableError()
            yield
        tunnels = {"other-phone": [{"tunnel-address": "fd00::2", "tunnel-port": 22}],
                   "selected-phone": [{"tunnel-address": "fd00::1", "tunnel-port": 12345}]}
        with patch("phoneview.ios.connection.PreferredRsdTunnel", unavailable), patch(
                "phoneview.ios.connection.get_tunneld_tunnels", AsyncMock(return_value=tunnels)):
            async with open_device() as rsd:
                self.assertIs(rsd, self.rsd)
                self.rsd_factory.assert_called_once_with(("fd00::1", 12345))
        self.rsd.close.assert_awaited_once()

    async def test_unavailable_daemon_reports_setup_instructions(self):
        with patch("phoneview.ios.connection.get_tunneld_tunnels", AsyncMock(side_effect=TunneldConnectionError())):
            with self.assertRaisesRegex(TunnelSetupError, "Administrator"):
                async with open_device(tunnel=True):
                    pass

    async def test_explicit_rsd_checks_identity_and_closes_on_mismatch(self):
        with self.assertRaisesRegex(TunnelSetupError, "different device"):
            async with open_device("another-phone", rsd_address=("fd00::1", 12345)):
                pass
        self.usb_mock.assert_not_called()
        self.rsd.close.assert_awaited_once()

    async def test_partial_rsd_connection_is_closed(self):
        self.rsd.connect.side_effect = OSError("connection failed")
        with self.assertRaises(OSError):
            async with open_device(rsd_address=("fd00::1", 12345)):
                pass
        self.rsd.close.assert_awaited_once()
