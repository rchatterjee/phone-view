"""iOS pieces without hardware: HID dispatch, serve-web stream parsing, WDA gestures."""
import asyncio
import base64
import json
import time
import unittest

from phoneview.ios import stream as stream_module
from phoneview.ios.backend import IOSBackend, IOSSettings, ios_major
from phoneview.ios.hid import BUTTONS, HidController, text_usages
from phoneview.ios.wda import WdaController, build_actions
from phoneview.media import MediaHub
from pymobiledevice3.remote.core_device.hid_service import (
    KEY_ENTER, KEY_LEFT_SHIFT, TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE)


class FakeHID:
    def __init__(self):
        self.reports = []

    async def send_touchscreen(self, state, x, y, **kwargs):
        self.reports.append(("touch", state, x, y))

    async def send_button(self, page, code, state):
        self.reports.append(("button", page, code, state))

    async def create_keyboard_service(self):
        return 77

    async def send_keyboard(self, service, usages):
        self.reports.append(("kbd", service, list(usages)))


def touch(phase, x=100, y=200, id=0):
    return {"type": "touch", "phase": phase, "id": id, "x": x, "y": y}


class HidTests(unittest.IsolatedAsyncioTestCase):
    async def run_events(self, events):
        hid = FakeHID()
        controller = HidController(hid, hid)
        for event in events:
            self.assertIsNone(controller.submit(event))
        task = asyncio.create_task(controller.run())
        async with asyncio.timeout(5):
            while not controller.queue.empty() or controller.queue._unfinished_tasks and False:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return hid.reports

    async def test_live_touch_coalesces_moves(self):
        reports = await self.run_events([touch("down"), touch("move", 1, 1), touch("move", 2, 2),
                                         touch("move", 3, 3), touch("up", 3, 3)])
        self.assertEqual(reports[0], ("touch", TOUCHSCREEN_STATE_CONTACT, 100, 200))
        self.assertEqual(reports[-1], ("touch", TOUCHSCREEN_STATE_RELEASE, 3, 3))
        self.assertLess(len(reports), 5)          # moves were merged
        self.assertIn(("touch", TOUCHSCREEN_STATE_CONTACT, 3, 3), reports)

    async def test_second_finger_ignored_and_cancel_releases(self):
        reports = await self.run_events([touch("down"), touch("down", id=1), touch("cancel", 5, 6)])
        self.assertEqual(reports, [("touch", TOUCHSCREEN_STATE_CONTACT, 100, 200),
                                   ("touch", TOUCHSCREEN_STATE_RELEASE, 5, 6)])

    async def test_app_switcher_is_two_home_presses(self):
        reports = await self.run_events([{"type": "key", "key": "app_switch", "phase": "press"}])
        page, code, _ = BUTTONS["home"]
        self.assertEqual(reports, [("button", page, code, 1), ("button", page, code, 2)] * 2)

    async def test_typing_uses_keyboard_service(self):
        reports = await self.run_events([{"type": "text", "text": "aB\n"}])
        chords = [r[2] for r in reports if r[0] == "kbd" and r[2]]
        self.assertEqual(chords, [[4], [KEY_LEFT_SHIFT, 5], [KEY_ENTER]])
        self.assertEqual(text_usages("é"), [])    # unmappable characters are skipped

    async def test_stale_input_dropped_but_release_kept(self):
        hid = FakeHID()
        controller = HidController(hid, hid)
        old = time.monotonic() - 5
        controller.touching = (1, 1)
        controller.queue.put_nowait((old, touch("down")))
        controller.queue.put_nowait((old, touch("up", 9, 9)))
        task = asyncio.create_task(controller.run())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(hid.reports, [("touch", TOUCHSCREEN_STATE_RELEASE, 9, 9)])


class FakeSidecar:
    """Stands in for ScreenStreamServer's HTTP endpoints."""
    def __init__(self, frames):
        self.frames = frames
        self.posts = []

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def handle(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        line = head.split("\r\n")[0]
        length = 0
        for h in head.split("\r\n")[1:]:
            if h.lower().startswith("content-length:"):
                length = int(h.split(":")[1])
        body = await reader.readexactly(length) if length else b""
        if line.startswith("GET /codec"):
            data = json.dumps({"codec": "hev1.1.6.L93.B0", "description": base64.b64encode(b"hvcc").decode()}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(data) + data)
        elif line.startswith("GET /stream.bin"):
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            blob = b"".join((len(d) + 1).to_bytes(4, "big") + bytes([t]) + d for t, d in self.frames)
            for i in range(0, len(blob), 7):     # awkward chunk boundaries on purpose
                piece = blob[i:i + 7]
                writer.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
            writer.write(b"0\r\n\r\n")
        else:
            self.posts.append((line.split(" ")[1], json.loads(body)))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_pump_parses_framing_and_posts_input(self):
        sidecar = await FakeSidecar([(0, b"KEY"), (1, b"DELTA"), (2, b"RESETKEY")]).start()
        hub = MediaHub()
        sub = hub.subscribe()
        stream = stream_module.CoreDeviceStream.__new__(stream_module.CoreDeviceStream)
        stream.__init__(rsd=None, hub=hub)
        stream.port = sidecar.port
        started = asyncio.Event()
        items = []

        async def consume():
            while True:
                items.append(await sub.next())

        consumer = asyncio.create_task(consume())
        await stream._pump(started)                # ends when the fake stream ends
        await asyncio.sleep(0.05)
        consumer.cancel()
        self.assertTrue(started.is_set())
        self.assertEqual(items[0][0], "config")
        # A type-2 frame re-sends the config so the browser rebuilds its decoder.
        self.assertEqual([i[0] for i in items].count("config"), 2)
        self.assertEqual(items[0][1]["codec"], "hevc")
        self.assertEqual(items[0][1]["format"], "hvcc")
        videos = [i for i in items if i[0] == "video"]
        self.assertEqual([(v[1], v[3]) for v in videos], [(True, b"KEY"), (False, b"DELTA"), (True, b"RESETKEY")])
        # Input goes to the sidecar's HID endpoints.
        worker = asyncio.create_task(stream._input_worker())
        stream.submit(touch("down", 1, 2))
        stream.submit(touch("up", 3, 4))
        stream.submit({"type": "key", "key": "volume_up", "phase": "press"})
        async with asyncio.timeout(5):
            while len(sidecar.posts) < 3:
                await asyncio.sleep(0.02)
        worker.cancel()
        self.assertEqual(sidecar.posts, [("/touch", {"type": "contact", "x": 1, "y": 2}),
                                         ("/touch", {"type": "release", "x": 3, "y": 4}),
                                         ("/button", {"name": "volume-up", "state": "press"})])

    async def test_viewer_leaving_lifts_stream_finger(self):
        sidecar = await FakeSidecar([]).start()
        backend = IOSBackend()
        stream = stream_module.CoreDeviceStream(rsd=None, hub=backend.hub)
        stream.port = sidecar.port
        stream.touching = True
        stream.last_point = (10, 20)
        backend.stream = stream
        await backend.set_viewer(True)
        await backend.set_viewer(False)
        self.assertEqual(sidecar.posts, [("/touch", {"type": "release", "x": 10, "y": 20})])
        self.assertFalse(stream.touching)

    def test_stream_needs_ios_27(self):
        self.assertFalse(stream_module.stream_supported("16.7.10"))
        self.assertFalse(stream_module.stream_supported("26.1"))
        self.assertTrue(stream_module.stream_supported("27.0"))

    def test_loop_signal_handlers_refused_then_restored(self):
        loop = asyncio.new_event_loop()
        try:
            with stream_module.no_loop_signal_handlers(loop):
                with self.assertRaises(NotImplementedError):
                    loop.add_signal_handler(2, lambda: None)
            self.assertNotIn("add_signal_handler", vars(loop))
        finally:
            loop.close()


class WdaTests(unittest.TestCase):
    def test_gesture_recorded_then_replayed(self):
        wda = WdaController("udid")
        wda.size = (375.0, 667.0)
        self.assertIsNone(wda.submit(touch("down", 0, 0)))
        wda.submit(touch("move", 100, 100))            # below MIN_STEP: skipped
        wda.submit(touch("move", 65535, 65535))
        wda.submit(touch("up", 65535, 65535))
        event = wda.queue.get_nowait()
        self.assertEqual(event["type"], "gesture")
        self.assertEqual(len(event["points"]), 2)
        body = build_actions(event["points"], *wda.size)
        actions = body["actions"][0]["actions"]
        self.assertEqual(actions[0], {"type": "pointerMove", "duration": 0, "x": 0, "y": 0})
        self.assertEqual(actions[1]["type"], "pointerDown")
        self.assertEqual((actions[2]["x"], actions[2]["y"]), (375, 667))
        self.assertEqual(actions[-1]["type"], "pointerUp")

    def test_tap_has_pause(self):
        actions = build_actions([(0.0, 0.5, 0.5)], 100, 200)["actions"][0]["actions"]
        self.assertEqual([a["type"] for a in actions], ["pointerMove", "pointerDown", "pause", "pointerUp"])

    def test_cancel_drops_gesture(self):
        wda = WdaController("udid")
        wda.submit(touch("down"))
        wda.submit(touch("cancel"))
        self.assertTrue(wda.queue.empty())


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_validation(self):
        IOSSettings(video_mode="screenshot")
        for bad in ({"active_fps": 0.05}, {"idle_fps": 5, "active_fps": 3}, {"short_edge": 500},
                    {"jpeg_quality": 95}, {"video_mode": "magic"}, {"tunnel": True, "rsd_address": ("h", 1)}):
            with self.assertRaises(ValueError):
                IOSSettings(**bad)

    def test_ios_major(self):
        self.assertEqual(ios_major("16.7.10"), 16)
        self.assertIsNone(ios_major(""))

    async def test_capabilities_follow_mode(self):
        backend = IOSBackend()
        self.assertIsNone(backend.capabilities()["video"])
        backend.mode = "legacy"
        caps = backend.capabilities()
        self.assertEqual(caps["video"], "jpeg")
        self.assertFalse(caps["touch"])            # view only without WebDriverAgent
        backend.wda = WdaController("u")
        caps = backend.capabilities()
        self.assertTrue(caps["touch"] and caps["text"])
        self.assertFalse(caps["live_touch"])
        backend.mode = "stream"
        backend.wda = None
        self.assertEqual(backend.capabilities()["video"], "hevc")
        self.assertIn("use_screenshots", backend.capabilities()["setup"])
        result = await backend.handle_input(touch("down"))
        self.assertIn("error", result)             # no stream object attached

    async def test_settings_update_bumps_revision(self):
        backend = IOSBackend()
        await backend.update_settings({"short_edge": 360})
        self.assertEqual(backend.stream_revision, 1)
        with self.assertRaises(ValueError):
            await backend.update_settings({"video_mode": "stream"})

    async def test_capture_loop_publishes_changed_frames(self):
        from io import BytesIO
        from PIL import Image
        png = BytesIO()
        Image.new("RGB", (750, 1334), "red").save(png, format="PNG")
        backend = IOSBackend(IOSSettings(active_fps=30, idle_fps=30))
        sub = backend.hub.subscribe()
        await backend.set_viewer(True)
        calls = 0

        async def grab():
            nonlocal calls
            calls += 1
            return png.getvalue()

        task = asyncio.create_task(backend._capture_loop(grab, stop=lambda: calls >= 4))
        await asyncio.wait_for(task, 5)
        item = await sub.next(0)
        self.assertEqual(item[0], "jpeg")
        self.assertEqual((item[2], item[3]), (480, 854))
        sub.ack()
        self.assertIsNone(await sub.next(0))       # duplicates suppressed
        await backend.close()


if __name__ == "__main__":
    unittest.main()
