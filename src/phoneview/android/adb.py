"""Minimal asyncio client for the local adb server (smart-socket protocol, port 5037).

Talking to the adb server directly avoids parsing CLI output that differs between
platform-tools versions. The `adb` binary is only used to start the server when
it is not already running.

Protocol: https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/SERVICES.TXT
and .../SYNC.TXT
"""
import asyncio
import logging
import os
import shutil
import struct
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5037
SYNC_DATA_MAX = 64 * 1024


class AdbError(RuntimeError):
    pass


class AdbUnavailableError(AdbError):
    """The adb server is not running and could not be started."""


@dataclass
class AdbDevice:
    serial: str
    state: str                       # device, unauthorized, offline, no permissions, ...
    info: dict = field(default_factory=dict)

    @property
    def usb(self):
        # Judge by the serial: TCP serials are host:port, wireless-debugging (mDNS)
        # serials contain "._adb", emulators start with "emulator-". The usb:<bus>
        # field of devices-l is only a positive hint: Windows adb never prints it.
        if "usb" in self.info:
            return True
        return (":" not in self.serial and not self.serial.startswith("emulator-")
                and "._adb" not in self.serial)


class AdbClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, adb_path=None):
        self.host = host
        self.port = port
        self.adb_path = adb_path or os.environ.get("ADB") or shutil.which("adb")
        self._last_start_attempt = 0.0

    # -- connection helpers ---------------------------------------------------

    async def _open(self):
        try:
            return await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 3)
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await self._start_server()
            try:
                return await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 3)
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
                raise AdbUnavailableError("adb server is not running") from exc

    async def _start_server(self):
        now = time.monotonic()
        if not self.adb_path or now - self._last_start_attempt < 10:
            if not self.adb_path:
                raise AdbUnavailableError(
                    "adb was not found. Install Android platform-tools (Linux: 'sudo apt install adb') "
                    "or set the ADB environment variable to its path.")
            raise AdbUnavailableError("adb server is not running")
        self._last_start_attempt = now
        log.info("Starting adb server")
        proc = await asyncio.create_subprocess_exec(
            self.adb_path, "start-server",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except asyncio.TimeoutError:
            proc.kill()
            raise AdbUnavailableError("adb start-server timed out")

    @staticmethod
    async def _send(reader, writer, request):
        data = request.encode("utf-8")
        writer.write(b"%04x" % len(data) + data)
        await writer.drain()
        status = await reader.readexactly(4)
        if status == b"OKAY":
            return
        if status == b"FAIL":
            raise AdbError(await AdbClient._read_string(reader))
        raise AdbError(f"Unexpected adb response {status!r}")

    @staticmethod
    async def _read_string(reader):
        length = int(await reader.readexactly(4), 16)
        return (await reader.readexactly(length)).decode("utf-8", "replace")

    @staticmethod
    def _close(writer):
        try:
            writer.close()
        except Exception:
            pass

    # -- host services ----------------------------------------------------------

    async def devices(self):
        reader, writer = await self._open()
        try:
            await self._send(reader, writer, "host:devices-l")
            text = await self._read_string(reader)
        finally:
            self._close(writer)
        return parse_devices(text)

    async def open_stream(self, serial, service, timeout=5):
        """Connect to `service` on the device; returns (reader, writer)."""
        reader, writer = await self._open()
        try:
            await asyncio.wait_for(self._send(reader, writer, f"host:transport:{serial}"), timeout)
            await asyncio.wait_for(self._send(reader, writer, service), timeout)
        except BaseException:
            self._close(writer)
            raise
        return reader, writer

    async def shell(self, serial, command, timeout=10, limit=1 << 20):
        reader, writer = await self.open_stream(serial, f"shell:{command}")
        try:
            data = await asyncio.wait_for(reader.read(limit), timeout)
            chunks = [data]
            while data:
                data = await asyncio.wait_for(reader.read(limit), timeout)
                chunks.append(data)
            return b"".join(chunks).decode("utf-8", "replace")
        finally:
            self._close(writer)

    async def getprops(self, serial, names):
        out = {}
        for name in names:
            out[name] = (await self.shell(serial, f"getprop {name}")).strip()
        return out

    async def push(self, serial, data, remote_path, mode=0o644, timeout=30):
        """Push bytes to `remote_path` using the sync protocol."""
        reader, writer = await self.open_stream(serial, "sync:")
        try:
            spec = f"{remote_path},{mode}".encode()
            writer.write(b"SEND" + struct.pack("<I", len(spec)) + spec)
            for offset in range(0, len(data), SYNC_DATA_MAX):
                chunk = data[offset:offset + SYNC_DATA_MAX]
                writer.write(b"DATA" + struct.pack("<I", len(chunk)) + chunk)
                await writer.drain()
            writer.write(b"DONE" + struct.pack("<I", int(time.time())))
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(8), timeout)
            if reply[:4] == b"FAIL":
                length, = struct.unpack("<I", reply[4:])
                message = (await reader.readexactly(length)).decode("utf-8", "replace")
                raise AdbError(f"push failed: {message}")
            if reply[:4] != b"OKAY":
                raise AdbError(f"Unexpected sync reply {reply[:4]!r}")
            writer.write(b"QUIT" + struct.pack("<I", 0))
            await writer.drain()
        finally:
            self._close(writer)


def parse_devices(text):
    """Parse `host:devices-l` output into AdbDevice objects."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # States can contain spaces ("no permissions (...)"); info fields are key:value.
        serial, _, rest = line.partition(" ")
        if not serial:
            continue
        rest = rest.strip()
        info = {}
        state_words = []
        for word in rest.split():
            key, sep, value = word.partition(":")
            if sep and key in {"usb", "product", "model", "device", "transport_id"}:
                info[key] = value
            else:
                state_words.append(word)
        devices.append(AdbDevice(serial, " ".join(state_words) or "unknown", info))
    return devices
