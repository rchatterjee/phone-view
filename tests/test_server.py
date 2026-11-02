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
from phoneview.security import COOKIE, Access, normalise_origin, viewer_identity
from phoneview.server import create_app, parse_args, build_backend

TOKEN = "t" * 43

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
        self.access = Access(TOKEN, 8000, ["https://Laptop.tail1.ts.net:8443/"])
        self.app = create_app(self.backend, self.access)

    def raw_client(self, base_url="http://127.0.0.1:8000"):
        return TestClient(self.app, base_url=base_url, client=("127.0.0.1", 50123), follow_redirects=False)

    def client(self):
        """A signed-in browser."""
        client = self.raw_client()
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login = client.get(f"/?token={TOKEN}")
        self.assertEqual(login.status_code, 303)
        return _Entered(client)

    def test_token_login_sets_cookie_and_strips_token(self):
        with self.raw_client() as client:
            self.assertEqual(client.get("/").status_code, 403)
            self.assertIn("Access denied", client.get("/").text)
            self.assertEqual(client.get("/?token=wrong").status_code, 403)
            self.assertEqual(client.get("/static/viewer.js").status_code, 200)   # no secrets in static files
            login = client.get(f"/?token={TOKEN}")
            self.assertEqual((login.status_code, login.headers["location"]), (303, "/"))
            cookie = login.headers["set-cookie"]
            self.assertIn("httponly", cookie.lower())
            self.assertIn("samesite=strict", cookie.lower())
            self.assertNotIn("secure", cookie.lower())      # plain http on loopback
            self.assertNotIn(TOKEN, cookie)                 # the cookie is a session id, not the token
            self.assertEqual(client.get("/").status_code, 200)
            client.cookies.set(COOKIE, "forged")
            self.assertEqual(client.get("/").status_code, 403)

    def test_websocket_needs_session(self):
        with self.raw_client() as client:
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect(WS_URL, headers=ORIGIN) as ws:
                    ws.receive_text()
            self.assertEqual(ctx.exception.code, 1008)
            self.assertIsNone(self.app.state.controller)

    def test_tailscale_serve_origin_and_identity(self):
        base = "https://laptop.tail1.ts.net:8443"
        with self.raw_client(base) as client:
            login = client.get(f"/?token={TOKEN}")
            self.assertIn("secure", login.headers["set-cookie"].lower())   # reached over HTTPS
            headers = {"origin": base, "tailscale-user-login": "alice@example.com",
                       "tailscale-user-name": "Alice"}
            with client.websocket_connect("wss://laptop.tail1.ts.net:8443/ws", headers=headers) as ws:
                status = receive_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "status")
                self.assertEqual(status["viewer"], "Alice <alice@example.com>")
            for origin in ("https://evil.tail1.ts.net:8443", "http://laptop.tail1.ts.net:8443"):
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("wss://laptop.tail1.ts.net:8443/ws",
                                                  headers={"origin": origin}) as ws:
                        ws.receive_text()
        with self.raw_client("http://other.tail1.ts.net") as client:
            self.assertEqual(client.get("/").status_code, 400)          # unknown Host

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
        with TestClient(self.app, base_url="http://127.0.0.1:8000", client=("192.168.1.2", 12)) as client:
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


class _Entered:
    """Lets `with self.client() as client:` work on an already-open client."""
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


class SecurityUnitTests(unittest.TestCase):
    def test_origins(self):
        self.assertEqual(normalise_origin("https://A.ts.net:443/"), ("https://a.ts.net", "a.ts.net"))
        for bad in ("ftp://x", "https://x/path", "https://x?q=1", "laptop"):
            with self.assertRaises(ValueError):
                normalise_origin(bad)
        access = Access(TOKEN, 8000, ["https://a.ts.net:8443"])
        self.assertTrue(access.origin_allowed("http://127.0.0.1:8000"))
        self.assertTrue(access.origin_allowed("https://a.ts.net:8443"))
        self.assertFalse(access.origin_allowed("http://127.0.0.1:9999"))
        self.assertFalse(access.origin_allowed("null"))
        with self.assertRaises(ValueError):
            Access("short", 8000)

    def test_identity_only_from_loopback(self):
        headers = {"tailscale-user-login": "bob@example.com"}
        self.assertEqual(viewer_identity(headers, "127.0.0.1"), "bob@example.com")
        self.assertIsNone(viewer_identity(headers, "10.0.0.5"))
        self.assertEqual(viewer_identity({}, "::1"), "local browser")

    def test_session_cap(self):
        access = Access(TOKEN, 8000)
        first = access.new_session()
        for _ in range(40):
            access.new_session()
        self.assertFalse(access.valid_session(first))
        self.assertEqual(len(access.sessions), 32)


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
