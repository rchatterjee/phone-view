"""Android backend against a fake adb server that speaks the smart-socket and sync
protocols and plays a scripted scrcpy v5.0.1 stream."""
import asyncio
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from phoneview.android import backend as android_backend, scrcpy
from phoneview.android.adb import AdbClient, AdbError
from phoneview.android.backend import AndroidBackend, AndroidSettings, device_problem

SPS_PPS = b"\x00\x00\x00\x01\x67\x42\xc0\x1f\x00\x00\x00\x01\x68\xce"
KEY = b"\x00\x00\x00\x01\x65\x88"
DELTA = b"\x00\x00\x00\x01\x41\x9a"
WIDTH, HEIGHT = 720, 1600


def media(payload, *, config=False, key=False, pts=0):
    flags = scrcpy.PACKET_FLAG_CONFIG if config else (pts | (scrcpy.PACKET_FLAG_KEY_FRAME if key else 0))
    return struct.pack(">QI", flags, len(payload)) + payload


class FakeAdb:
    def __init__(self, devices="SERIAL1 device usb:1-1 product:p model:Pixel_8 device:d transport_id:1\n"):
        self.devices = devices
        self.pushed = {}
        self.shell_commands = []
        self.control = bytearray()
        self.control_event = asyncio.Event()
        self.abstract_connections = 0
        self.shell_closed = asyncio.Event()
        self.props = {"ro.product.manufacturer": "Google", "ro.product.model": "Pixel 8",
                      "ro.build.version.release": "15", "ro.build.version.sdk": "35"}

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()

    @staticmethod
    async def read_request(reader):
        length = int(await reader.readexactly(4), 16)
        return (await reader.readexactly(length)).decode()

    async def handle(self, reader, writer):
        try:
            request = await self.read_request(reader)
            if request == "host:devices-l":
                data = self.devices.encode()
                writer.write(b"OKAY" + b"%04x" % len(data) + data)
                await writer.drain()
                return
            assert request.startswith("host:transport:"), request
            writer.write(b"OKAY")
            service = await self.read_request(reader)
            if service.startswith("shell:getprop "):
                writer.write(b"OKAY" + self.props[service.split(" ", 1)[1]].encode() + b"\n")
            elif service.startswith("shell:CLASSPATH="):
                writer.write(b"OKAY")
                await writer.drain()
                self.shell_commands.append(service)
                await reader.read()          # until the client closes the shell
                self.shell_closed.set()
            elif service == "sync:":
                writer.write(b"OKAY")
                await self.sync(reader, writer)
            elif service.startswith("localabstract:scrcpy_"):
                writer.write(b"OKAY")
                await writer.drain()
                self.abstract_connections += 1
                if self.abstract_connections == 1:
                    await self.video(reader, writer)
                else:
                    while data := await reader.read(4096):
                        self.control += data
                        self.control_event.set()
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def sync(self, reader, writer):
        cmd = await reader.readexactly(4)
        assert cmd == b"SEND"
        length, = struct.unpack("<I", await reader.readexactly(4))
        path = (await reader.readexactly(length)).decode().split(",")[0]
        data = bytearray()
        while True:
            cmd = await reader.readexactly(4)
            length, = struct.unpack("<I", await reader.readexactly(4))
            if cmd == b"DONE":
                break
            data += await reader.readexactly(length)
        self.pushed[path] = bytes(data)
        writer.write(b"OKAY" + struct.pack("<I", 0))
        await writer.drain()
        await reader.readexactly(8)   # QUIT

    async def video(self, reader, writer):
        writer.write(b"\x00" + b"Pixel 8".ljust(64, b"\x00") + struct.pack(">I", scrcpy.CODEC_H264))
        writer.write(struct.pack(">III", 0x80000000, WIDTH, HEIGHT))
        writer.write(media(SPS_PPS, config=True))
        writer.write(media(KEY, key=True, pts=1000))
        writer.write(media(DELTA, pts=2000))
        await writer.drain()
        await reader.read()


class AndroidStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = await FakeAdb().start()
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b"fake-jar")
        tmp.close()
        self.jar = Path(tmp.name)
        self.addCleanup(self.jar.unlink)
        patcher = patch.object(scrcpy, "ensure_server", lambda path=None: self.jar)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.fake.close()

    async def collect(self, sub, count, timeout=5):
        items = []
        async with asyncio.timeout(timeout):
            while len(items) < count:
                item = await sub.next(1)
                if item is not None:
                    items.append(item)
        return items

    async def test_stream_relays_h264_and_injects_touch(self):
        backend = AndroidBackend(AndroidSettings(max_size=1024), adb=AdbClient(port=self.fake.port))
        sub = backend.hub.subscribe()
        await backend.set_viewer(True)
        task = asyncio.create_task(backend.run())
        try:
            items = await self.collect(sub, 3)
            config, key, delta = items
            self.assertEqual(config[0], "config")
            self.assertEqual(config[1]["codec_string"], "avc1.42c01f")
            self.assertEqual((config[1]["width"], config[1]["height"]), (WIDTH, HEIGHT))
            self.assertEqual(config[1]["format"], "annexb")
            self.assertEqual(key, ("video", True, 1000, SPS_PPS + KEY))   # parameter sets inline
            self.assertEqual(delta, ("video", False, 2000, DELTA))
            self.assertEqual(backend.device["name"], "Google Pixel 8")
            self.assertEqual(self.fake.pushed[scrcpy.REMOTE_PATH], b"fake-jar")
            command = self.fake.shell_commands[0]
            self.assertIn(f"com.genymobile.scrcpy.Server {scrcpy.VERSION} ", command)
            self.assertIn("max_size=1024", command)
            self.assertIsNone(await backend.handle_input(
                {"type": "touch", "phase": "down", "id": 0, "x": 65535, "y": 0}))
            self.assertIsNone(await backend.handle_input({"type": "key", "key": "home", "phase": "press"}))
            expected = (scrcpy.inject_touch(scrcpy.ACTION_DOWN, WIDTH - 1, 0, WIDTH, HEIGHT, 1.0, 0)
                        + scrcpy.inject_keycode(0, 3) + scrcpy.inject_keycode(1, 3))
            async with asyncio.timeout(3):
                while len(self.fake.control) < len(expected):
                    self.fake.control_event.clear()
                    await self.fake.control_event.wait()
            self.assertEqual(bytes(self.fake.control[:len(expected)]), expected)
            # Leaving releases the held finger before the session winds down.
            with patch.object(android_backend, "VIEWER_GRACE_SECONDS", 0):
                await backend.set_viewer(False)
                async with asyncio.timeout(5):
                    await self.fake.shell_closed.wait()
            self.assertIn(scrcpy.inject_touch(scrcpy.ACTION_UP, WIDTH - 1, 0, WIDTH, HEIGHT, 0, 0),
                          bytes(self.fake.control))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unauthorized_device_gives_guidance(self):
        self.fake.devices = "ABC unauthorized usb:1-2 transport_id:4\n"
        backend = AndroidBackend(adb=AdbClient(port=self.fake.port))
        self.assertIsNone(await backend.find_device())
        self.assertIn("Allow USB debugging", backend.error)

    async def test_input_before_video_is_refused(self):
        backend = AndroidBackend(adb=AdbClient(port=self.fake.port))
        result = await backend.handle_input({"type": "key", "key": "home", "phase": "press"})
        self.assertIn("error", result)

    async def test_settings_validation(self):
        backend = AndroidBackend(adb=AdbClient(port=self.fake.port))
        await backend.update_settings({"max_size": 800, "max_fps": 60, "bit_rate_mbps": 8})
        self.assertEqual(backend.settings.max_size, 800)
        for bad in ({"max_size": 801}, {"max_fps": 61}, {"bit_rate_mbps": 100}, {"serial": "x"}):
            with self.assertRaises(ValueError):
                await backend.update_settings(bad)


class AdbClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_server_without_binary(self):
        client = AdbClient(port=1, adb_path=None)
        client.adb_path = None
        with self.assertRaises(AdbError) as ctx:
            await client.devices()
        self.assertIn("adb was not found", str(ctx.exception))

    def test_device_problem_messages(self):
        self.assertIn("udev", device_problem("no permissions (missing udev rules?)"))
        self.assertIn("offline", device_problem("offline"))


if __name__ == "__main__":
    unittest.main()
