"""WebDriverAgent: control (and faster MJPEG video) for iPhones without CoreDevice HID.

WDA must already be installed on the phone. Without a Mac it can be re-signed and
installed from a prebuilt runner (see docs/ios-wda.md). We reach it through usbmux
(no port forwarding, no Xcode) and, if it is not running, launch the installed
runner with pymobiledevice3's XCUITest service.

WDA cannot stream a touch as it happens, so a gesture is recorded in the browser
order (down, moves, up) and replayed as one W3C pointer action chain on release.
The path and timing are kept, so drags, swipes and long presses still work.
"""
import asyncio
import contextlib
import logging
import time
from io import BytesIO

from PIL import Image

from phoneview import httpmini

log = logging.getLogger(__name__)

WDA_PORT = 8100
MJPEG_PORT = 9100
BUTTONS = {"home": "home", "volume_up": "volumeUp", "volume_down": "volumeDown"}
WDA_KEYS = set(BUTTONS) | {"enter", "delete"}
MAX_PATH_POINTS = 40
MIN_STEP = 0.01   # normalised distance between recorded path points


class WdaUnavailable(RuntimeError):
    pass


def open_usbmux(udid, port):
    async def opener():
        from pymobiledevice3 import usbmux
        device = await usbmux.select_device(udid)
        if device is None:
            raise WdaUnavailable("iPhone not found on usbmux")
        sock = await device.connect(port)
        return await asyncio.open_connection(sock=sock)
    return opener


async def find_runner(lockdown):
    """Bundle id of an installed WebDriverAgent runner, or None."""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    async with InstallationProxyService(lockdown=lockdown) as proxy:
        apps = await proxy.get_apps(application_type="User")
    for bundle_id in sorted(apps):
        if bundle_id.endswith(".xctrunner") and "webdriveragent" in bundle_id.lower():
            return bundle_id
    return None


def build_actions(points, width, height):
    """W3C pointer actions for a recorded gesture: [(t_seconds, nx, ny)], nx/ny in 0..1."""
    t0, x0, y0 = points[0]
    actions = [{"type": "pointerMove", "duration": 0, "x": round(x0 * width), "y": round(y0 * height)},
               {"type": "pointerDown", "button": 0}]
    previous = t0
    for t, x, y in points[1:]:
        duration = max(0, int((t - previous) * 1000))
        actions.append({"type": "pointerMove", "duration": min(duration, 2000),
                        "x": round(x * width), "y": round(y * height)})
        previous = t
    if len(points) == 1:
        actions.append({"type": "pause", "duration": 50})
    actions.append({"type": "pointerUp", "button": 0})
    return {"actions": [{"type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"},
                         "actions": actions}]}


class WdaController:
    def __init__(self, udid, port=WDA_PORT):
        self.udid = udid
        self.open = open_usbmux(udid, port)
        self.session = None
        self.size = (0, 0)              # points
        self.gesture = None             # [(t, nx, ny)] while a touch is down
        self.queue = asyncio.Queue(64)
        self.dispatch_ms = 0.0

    async def call(self, method, path, body=None, timeout=15):
        data = await httpmini.request_json(self.open, method, path, body, timeout)
        if isinstance(data, dict) and data.get("status") not in (None, 0, "0"):
            raise WdaUnavailable(str(data.get("value"))[:200])
        return data

    async def ready(self):
        with contextlib.suppress(Exception):
            status = await self.call("GET", "/status", timeout=3)
            return isinstance(status, dict)
        return False

    async def connect(self):
        data = await self.call("POST", "/session", {"capabilities": {"alwaysMatch": {}}, "desiredCapabilities": {}})
        session = data.get("sessionId") or (data.get("value") or {}).get("sessionId")
        if not session:
            raise WdaUnavailable("WebDriverAgent did not create a session")
        self.session = session
        size = (await self.call("GET", f"/session/{session}/window/size"))["value"]
        self.size = (float(size["width"]), float(size["height"]))
        # Smoother MJPEG than the 10 fps default; WDA ignores unknown settings.
        with contextlib.suppress(Exception):
            await self.call("POST", f"/session/{session}/appium/settings",
                            {"settings": {"mjpegServerFramerate": 15, "mjpegServerScreenshotQuality": 60}})

    # -- input -----------------------------------------------------------------

    def submit(self, event):
        if event["type"] == "touch":
            if event["id"] != 0:
                return None
            now = time.monotonic()
            point = (now, event["x"] / 65535, event["y"] / 65535)
            phase = event["phase"]
            if phase == "down":
                self.gesture = [point]
                return None
            if self.gesture is None:
                return None
            last = self.gesture[-1]
            if phase == "move":
                if (abs(point[1] - last[1]) + abs(point[2] - last[2]) >= MIN_STEP
                        and len(self.gesture) < MAX_PATH_POINTS):
                    self.gesture.append(point)
                return None
            if phase == "up":
                if point[1:] != last[1:]:
                    self.gesture.append(point)
                event = {"type": "gesture", "points": self.gesture}
            self.gesture = None
            if phase == "cancel":
                return None
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            return {"error": "Controller busy"}
        return None

    async def run(self):
        while True:
            event = await self.queue.get()
            start = time.monotonic()
            try:
                await self._apply(event)
            except (httpmini.HttpError, WdaUnavailable) as exc:
                log.warning("WDA input failed: %s", exc)
            self.dispatch_ms = (time.monotonic() - start) * 1000

    async def _apply(self, event):
        s = self.session
        if event["type"] == "gesture":
            await self.call("POST", f"/session/{s}/actions", build_actions(event["points"], *self.size))
        elif event["type"] == "key":
            key = event["key"]
            if event["phase"] == "up":
                return
            if key in BUTTONS:
                await self.call("POST", f"/session/{s}/wda/pressButton", {"name": BUTTONS[key]})
            elif key == "enter":
                await self.call("POST", f"/session/{s}/wda/keys", {"value": ["\n"]})
            elif key == "delete":
                await self.call("POST", f"/session/{s}/wda/keys", {"value": ["\b"]})
        elif event["type"] == "text":
            await self.call("POST", f"/session/{s}/wda/keys", {"value": list(event["text"])})


async def launch_runner(lockdown, bundle_id):
    """Start the installed WDA runner through XCUITest; returns the running task."""
    from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
    cfg = await TestConfig.create_for(lockdown, runner_bundle_id=bundle_id)
    return asyncio.create_task(XCUITestService(lockdown).run(cfg), name="wda-runner")


async def mjpeg_frames(udid, port=MJPEG_PORT):
    """Yield (jpeg bytes, width, height) from WDA's MJPEG server."""
    reader, writer = await asyncio.wait_for(open_usbmux(udid, port)(), 5)
    try:
        writer.write(httpmini.build_request("GET", "/", keep_alive=True))
        await writer.drain()
        status, headers = await asyncio.wait_for(httpmini.read_head(reader), 5)
        if status != 200 or "multipart" not in headers.get("content-type", ""):
            raise WdaUnavailable("MJPEG server unavailable")
        while True:
            part_headers = {}
            line = b""
            while not line.strip():               # skip boundary/blank lines
                line = await asyncio.wait_for(reader.readuntil(b"\n"), 10)
                if line.strip().startswith(b"--"):
                    line = b""
            while line.strip():
                if b":" in line:
                    key, value = line.decode("latin-1").split(":", 1)
                    part_headers[key.strip().lower()] = value.strip()
                line = await asyncio.wait_for(reader.readuntil(b"\n"), 10)
            length = int(part_headers.get("content-length", "0"))
            if not 0 < length < 8 << 20:
                raise WdaUnavailable("MJPEG part without a valid length")
            data = await asyncio.wait_for(reader.readexactly(length), 10)
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
            yield data, width, height
    finally:
        writer.close()
