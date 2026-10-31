"""Persistent RSD selection with a local kernel-tunnel fallback."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import logging

from pymobiledevice3.exceptions import UserspaceTunnelUnavailableError, TunneldConnectionError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.tunneld.api import get_tunneld_tunnels

log = logging.getLogger(__name__)


class TunnelSetupError(RuntimeError):
    """Only fixed, application-controlled setup guidance is exposed to the browser."""


class UnsupportedIOSVersionError(TunnelSetupError):
    pass


class DeveloperModeRequiredError(TunnelSetupError):
    pass


async def _preflight(provider, on_device):
    mode = await on_device(provider) if on_device is not None else None
    check_ios_version(provider.all_values)
    if mode is False:
        raise DeveloperModeRequiredError("Developer Mode is off. Follow the setup steps in the browser, "
                                         "restart the iPhone when prompted, and confirm Turn On after unlocking.")


def check_ios_version(values):
    version = values.get("ProductVersion", "")
    major = str(version).split(".", 1)[0]
    if major.isdigit() and int(major) < 17:
        raise UnsupportedIOSVersionError(
            "This app's CoreDevice screenshot/HID backend requires iOS 17 or later. "
            "For iOS 16, a legacy screenshot backend and a separate control backend "
            "such as a signed WebDriverAgent are needed; starting tunneld will not fix this."
        )


TUNNEL_HELP = (
    "USB userspace tunnel unavailable. Start 'python -m pymobiledevice3 remote tunneld' "
    "in an Administrator terminal on Windows, or with sudo on Linux, and leave it running. "
    "Then start this app with --tunnel. For a manual tunnel use --rsd HOST PORT."
)


async def _connect(stack, address, serial, on_device):
    rsd = RemoteServiceDiscoveryService(address)
    # Register cleanup before connect so failed/partial handshakes are closed too.
    stack.push_async_callback(rsd.close)
    await asyncio.wait_for(rsd.connect(), 15)
    if serial is not None and rsd.udid != serial:
        raise TunnelSetupError("The RSD endpoint belongs to a different device than --serial.")
    await _preflight(rsd, on_device)
    return rsd


async def _local_tunnel(stack, target, on_device):
    try:
        tunnels = await get_tunneld_tunnels()  # Default is loopback 127.0.0.1:49151.
    except TunneldConnectionError as exc:
        raise TunnelSetupError(TUNNEL_HELP) from exc
    details = tunnels.get(target, []) if isinstance(tunnels, dict) else []
    if not details:
        raise TunnelSetupError("Local tunneld has no tunnel for the selected USB iPhone. "
                               "Keep it unlocked, trusted, and in Developer Mode; check the tunneld terminal.")
    entry = details[0]
    if (not isinstance(entry, dict) or not isinstance(entry.get("tunnel-address"), str)
            or type(entry.get("tunnel-port")) is not int or not 1 <= entry["tunnel-port"] <= 65535):
        raise TunnelSetupError("Local tunneld returned invalid tunnel details.")
    return await _connect(stack, (entry["tunnel-address"], entry["tunnel-port"]), target, on_device)


@asynccontextmanager
async def open_device(serial=None, *, tunnel=False, rsd_address=None, on_device=None):
    """Use an explicit RSD, a local tunneld, or the normal no-admin USB path.

    The app never starts a privileged daemon or changes the viewer's local bind.
    """
    async with AsyncExitStack() as stack:
        if rsd_address is not None:
            rsd = await _connect(stack, rsd_address, serial, on_device)
        else:
            # Detect iOS 16 before attempting an unavailable CoreDevice tunnel.
            # Match the USB device instead of selecting an unrelated tunneld phone.
            lockdown = await create_using_usbmux(serial=serial)
            try:
                target = lockdown.udid
                await _preflight(lockdown, on_device)
            finally:
                await lockdown.close()
            if tunnel:
                rsd = await _local_tunnel(stack, target, on_device)
            else:
                try:
                    rsd = await stack.enter_async_context(PreferredRsdTunnel(serial=target))
                except UserspaceTunnelUnavailableError:
                    log.info("Userspace tunnel unavailable; checking local tunneld")
                    rsd = await _local_tunnel(stack, target, on_device)
        yield rsd
