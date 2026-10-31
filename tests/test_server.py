"""HTTP/WebSocket layer with a fake backend, plus backend auto-detection."""
import asyncio
import json
import struct
import unittest
import unittest.mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from phoneview.backends.auto import AutoBackend
from phoneview.backends.base import Backend, capabilities
from phoneview.server import create_app, parse_args, build_backend

ORIGIN = {"origin": "http://127.0.0.1:8000"}
WS_URL = "ws://127.0.0.1:8000/ws"


class FakeBackend(Backend):
    platform = "android"

    def __init__(self, hub=None):
        super().__init__(hub)
        self.events = []
        self.settings_seen = []
        self.generation = 7
        self.device = {"name": "Fake", "os": "Android", "version": "15"}

    def capabilities(self):
        return capabilities(video="h264", touch=True, multitouch=True, live_touch=True,
                            keys={"home", "back"}, text=True, setup=("developer_mode_check",))

    async def run(self):
        await asyncio.Event().wait()

    async def handle_input(self, event):
        self.events.append(event)
        return None

    async def update_settings(self, values):
        if values != {"max_fps": 60}:
            raise ValueError("bad settings")
        self.settings_seen.append(values)

    async def setup_action(self, name):
        if name != "developer_mode_check":
            raise ValueError("Unknown setup action")
        return {"message": "checked"}


def receive_until(ws, predicate, limit=50):
    for _ in range(limit):
        message = ws.receive()
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(message.get("code", 1000))
        if "text" in message and message["text"] is not None:
            data = json.loads(message["text"])
        else:
            data = message.get("bytes")
        if predicate(data):
            return data
    raise AssertionError("expected message not received")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.app = create_app(self.backend)

    def client(self, **kwargs):
        return TestClient(self.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50123), **kwargs)

    def test_page_and_static_with_security_headers(self):
        with self.client() as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("viewer.js", page.text)
            self.assertIn("script-src 'self';", page.headers["content-security-policy"])
            self.assertEqual(client.get("/static/viewer.js").status_code, 200)
            self.assertEqual(client.get("/static/../server.py").status_code, 404)
            self.assertEqual(client.get("/static/secret.txt").status_code, 404)

    def test_remote_peers_and_bad_hosts_refused(self):
        with TestClient(self.app, client=("192.168.1.2", 12)) as client:
            self.assertEqual(client.get("/").status_code, 403)
        with self.client() as client:
            self.assertEqual(client.get("/", headers={"host": "evil.example"}).status_code, 400)

    def test_origin_check_and_single_controller(self):
        with self.client() as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(WS_URL, headers={"origin": "https://evil.example"}) as ws:
                    ws.receive_text()
            with client.websocket_connect(WS_URL, headers=ORIGIN) as ws:
                receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "status")
                with self.assertRaises(WebSocketDisconnect) as ctx:
                    with client.websocket_connect(WS_URL, headers=ORIGIN) as second:
                        second.receive_text()
                self.assertEqual(ctx.exception.code, 1008)
            self.assertIsNone(self.app.state.controller)

    def test_capabilities_status_video_and_input(self):
        with self.client() as client, client.websocket_connect(WS_URL, headers=ORIGIN) as ws:
            caps = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "capabilities")
            self.assertEqual(caps["video"], "h264")
            status = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "status")
            self.assertEqual(status["generation"], 7)
            self.assertTrue(self.backend.viewer)
            hub = self.backend.hub
            client.portal.call(hub.set_config, {"codec": "h264", "codec_string": "avc1.42c01f"})
            client.portal.call(hub.publish_video, b"KEYDATA", True, 1234)
            config = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "video_config")
            self.assertEqual(config["codec_string"], "avc1.42c01f")
            frame = receive_until(ws, lambda m: isinstance(m, bytes))
            self.assertEqual(frame[:10], struct.pack(">BBQ", 1, 1, 1234))
            self.assertEqual(frame[10:], b"KEYDATA")
            ws.send_text(json.dumps({"type": "touch", "generation": 7, "phase": "down", "id": 0, "x": 5, "y": 6}))
            ws.send_text(json.dumps({"type": "touch", "generation": 6, "phase": "up", "id": 0, "x": 5, "y": 6}))
            stale = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "input_result")
            self.assertIn("Stale", stale["error"])
            ws.send_text(json.dumps({"type": "stream_settings", "generation": 7, "settings": {"max_fps": 60}}))
            result = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "settings_result")
            self.assertNotIn("error", result)
            ws.send_text(json.dumps({"type": "setup", "action": "developer_mode_check"}))
            setup = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "setup_result")
            self.assertEqual(setup["message"], "checked")
            ws.send_text("x" * 3000)
            with self.assertRaises(WebSocketDisconnect):
                receive_until(ws, lambda m: False)
        self.assertEqual(self.backend.events, [{"type": "touch", "phase": "down", "id": 0, "x": 5, "y": 6}])
        self.assertFalse(self.backend.viewer)

    def test_jpeg_requires_ack(self):
        with self.client() as client, client.websocket_connect(WS_URL, headers=ORIGIN) as ws:
            receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "status")
            hub = self.backend.hub
            client.portal.call(hub.publish_jpeg, b"JPEG1", 1, 1)
            self.assertEqual(receive_until(ws, lambda m: isinstance(m, bytes)), b"\x02JPEG1")
            client.portal.call(hub.publish_jpeg, b"JPEG2", 1, 1)
            client.portal.call(hub.publish_jpeg, b"JPEG3", 1, 1)
            ws.send_text(json.dumps({"type": "frame_ack"}))
            self.assertEqual(receive_until(ws, lambda m: isinstance(m, bytes)), b"\x02JPEG3")

    def test_cli_builds_auto_backend(self):
        _, args = parse_args([])
        self.assertIsInstance(build_backend(args), AutoBackend)
        _, args = parse_args(["--platform", "android", "--max-size", "800"])
        self.assertEqual(build_backend(args).settings.max_size, 800)
        _, args = parse_args(["--platform", "ios", "--ios-video", "screenshot"])
        self.assertEqual(build_backend(args).settings.video_mode, "screenshot")


class AutoBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_to_plugged_in_platform(self):
        present = {"android": False, "ios": False}

        async def detector(name):
            return present[name]

        class IOSFake(FakeBackend):
            platform = "ios"

        auto = AutoBackend({"android": FakeBackend, "ios": IOSFake},
                           detectors={"android": lambda: detector("android"), "ios": lambda: detector("ios")})
        with unittest.mock.patch("phoneview.backends.auto.DETECT_INTERVAL", 0.01):
            task = asyncio.create_task(auto.run())
            try:
                await asyncio.sleep(0.05)
                self.assertIsNone(auto.current)
                self.assertIn("No phone detected", auto.full_status()["error"])
                self.assertIn("error", await auto.handle_input({"type": "key"}))
                await auto.set_viewer(True)
                present["ios"] = True
                await asyncio.sleep(0.05)
                self.assertEqual(auto.current.platform, "ios")
                self.assertTrue(auto.current.viewer)
                first_generation = auto.generation
                present.update(ios=False, android=True)
                await asyncio.sleep(0.05)
                self.assertEqual(auto.current.platform, "android")
                self.assertNotEqual(auto.generation, first_generation)
                await auto.handle_input({"type": "key", "key": "home", "phase": "press"})
                self.assertEqual(len(auto.current.events), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(auto.current)


if __name__ == "__main__":
    unittest.main()
