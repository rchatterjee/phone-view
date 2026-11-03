"""Android backend: scrcpy server over adb, H.264 relayed untouched to the browser.

The host never decodes or re-encodes video, so a Raspberry Pi or thin client only
copies packets. The scrcpy server runs only while a browser is watching (plus a
short grace period), so an idle phone is not encoding its screen.
"""
import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, replace

from phoneview.android import scrcpy
from phoneview.android.adb import AdbClient, AdbError, AdbUnavailableError
from phoneview.backends.base import Backend, ScreenshotUnavailable, capabilities, setting
from phoneview.events import COORD_MAX

log = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

MAX_SIZES = (0, 1920, 1600, 1280, 1024, 800, 640)
FPS_CHOICES = (15, 24, 30, 45, 60)
FALLBACK_SIZES = (1024, 800, 640)
VIEWER_GRACE_SECONDS = 10
CONTROL_QUEUE_LIMIT = 256


class StreamEnded(Exception):
    pass


@dataclass(frozen=True)
class AndroidSettings:
    max_size: int = 1280          # long edge in pixels; 0 = native
    max_fps: int = 30
    bit_rate_mbps: float = 4.0
    serial: str = None
    server_path: str = None

    def __post_init__(self):
        if self.max_size not in MAX_SIZES:
            raise ValueError(f"max_size must be one of {MAX_SIZES}")
        if self.max_fps not in FPS_CHOICES:
            raise ValueError(f"max_fps must be one of {FPS_CHOICES}")
        if type(self.bit_rate_mbps) not in (int, float) or not 0.5 <= self.bit_rate_mbps <= 16:
            raise ValueError("Bit rate must be 0.5..16 Mbps")


def device_problem(state):
    """User-facing guidance for adb device states other than 'device'."""
    if state == "unauthorized":
        return "Unlock the phone and accept the 'Allow USB debugging?' prompt (tick 'Always allow')."
    if state.startswith("no permissions"):
        return ("The host has no permission to open the USB device. On Linux add a udev rule for "
                "Android devices (e.g. install the 'android-sdk-platform-tools-common' package) "
                "and replug the phone.")
    if state == "offline":
        return "The phone is offline to adb. Replug the USB cable or toggle USB debugging."
    if state in {"authorizing", "connecting"}:
        return "Waiting for the phone to finish USB debugging authorisation..."
    return f"Android device is in state '{state}'."


class AndroidBackend(Backend):
    platform = "android"

    def __init__(self, settings=None, hub=None, adb=None):
        super().__init__(hub)
        self.settings = settings or AndroidSettings()
        self.adb = adb or AdbClient()
        self.serial = None
        self.streaming = False
        self.width = self.height = 0
        self.control = None                 # asyncio.Queue of bytes while streaming
        self.pointers = {}                  # browser pointer id -> (x, y) in video pixels
        self._config = b""
        self._last_key = 0.0
        self._restart = asyncio.Event()
        self._fallback = 0
        self._last_viewer = 0.0
        self._rate_window = (time.monotonic(), 0, 0)
        self._rates = (0.0, 0.0)
        self.hub.request_keyframe = self._request_keyframe

    # -- capabilities / settings ---------------------------------------------------

    def capabilities(self):
        s = self.settings
        return capabilities(
            video="h264" if self.device else None, touch=True, multitouch=True, live_touch=True,
            keys=set(scrcpy.KEYCODES) | {"siri"}, text=True, scroll=True, clipboard=True,
            commands={"expand_notifications", "expand_settings", "collapse_panels", "rotate",
                      "screen_off", "screen_on"},
            settings=[
                setting("max_size", "Max size (long edge)", s.max_size,
                        options=[(v, "Native" if v == 0 else f"{v} px") for v in MAX_SIZES]),
                setting("max_fps", "Max fps", s.max_fps, options=[(v, str(v)) for v in FPS_CHOICES]),
                setting("bit_rate_mbps", "Bit rate (Mbps)", s.bit_rate_mbps, minimum=0.5, maximum=16, step=0.5),
            ],
        )

    async def update_settings(self, values):
        if not isinstance(values, dict) or not set(values) <= {"max_size", "max_fps", "bit_rate_mbps"}:
            raise ValueError("Unknown Android settings")
        new = replace(self.settings, **values)
        if new != self.settings:
            self.settings = new
            self._fallback = 0
            self._restart.set()
            self.notify()

    def status(self):
        now = time.monotonic()
        start, frames, size = self._rate_window
        if now - start >= 2:
            elapsed = now - start
            self._rates = (round((self.hub.frames_published - frames) / elapsed, 1),
                           round((self.hub.bytes_published - size) * 8 / elapsed / 1e6, 2))
            self._rate_window = (now, self.hub.frames_published, self.hub.bytes_published)
        return {"streaming": self.streaming, "control": self.streaming, "width": self.width,
                "height": self.height, "fps": self._rates[0], "mbps": self._rates[1]}

    # -- lifecycle ---------------------------------------------------------------------

    async def find_device(self):
        """Return the adb device to use, or None; sets a helpful error otherwise."""
        devices = await self.adb.devices()
        if self.settings.serial:
            devices = [d for d in devices if d.serial == self.settings.serial]
        else:
            devices = [d for d in devices if d.usb]
        ready = [d for d in devices if d.state == "device"]
        if ready:
            return ready[0]
        if devices:
            self.error = device_problem(devices[0].state)
        else:
            self.error = "No Android device found. Connect the phone by USB and enable USB debugging."
        return None

    async def _describe(self, serial):
        props = await self.adb.getprops(serial, [
            "ro.product.manufacturer", "ro.product.model", "ro.build.version.release",
            "ro.build.version.sdk"])
        sdk = props["ro.build.version.sdk"]
        if sdk.isdigit() and int(sdk) < 21:
            raise AdbError("scrcpy needs Android 5.0 (API 21) or newer")
        return {"serial": serial, "name": f'{props["ro.product.manufacturer"]} {props["ro.product.model"]}'.strip(),
                "os": "Android", "version": props["ro.build.version.release"], "sdk": sdk}

    async def run(self):
        while True:
            try:
                dev = await self.find_device()
                if dev is None:
                    self._forget_device()
                    await self.wait_viewer_change(2)
                    continue
                if self.serial != dev.serial or self.device is None:
                    self.device = await self._describe(dev.serial)
                    self.serial = dev.serial
                    self.generation += 1
                    self._fallback = 0
                self.error = None
                self.notify()
                if not self.viewer:
                    await self.wait_viewer_change(2)
                    continue
                await self._stream(dev.serial)
            except asyncio.CancelledError:
                raise
            except AdbUnavailableError as exc:
                self._forget_device()
                self.error = str(exc)
            except StreamEnded as exc:
                self.error = str(exc) or None
            except (AdbError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
                self.error = f"Android stream stopped ({type(exc).__name__}: {exc}); retrying"
                log.warning(self.error)
            self.notify()
            await asyncio.sleep(1)

    async def screenshot(self):
        """Full-resolution PNG from the phone (`screencap -p`), independent of the video stream."""
        serial = self.serial
        if serial is None:
            raise ScreenshotUnavailable("No Android phone connected")
        try:
            data = await self.adb.exec_out(serial, "screencap -p")
        except (AdbError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ScreenshotUnavailable(f"The phone did not return a screenshot ({type(exc).__name__})") from exc
        if not data.startswith(PNG_SIGNATURE):
            # e.g. a secure window (banking apps) or a locked screen policy.
            raise ScreenshotUnavailable("The phone refused the screenshot (the app on screen may block captures).")
        return data, "image/png"

    def _forget_device(self):
        if self.device is not None:
            self.device = None
            self.serial = None
            self.generation += 1
            self.hub.clear()
            self.notify()

    def _max_size(self):
        if self._fallback:
            size = FALLBACK_SIZES[min(self._fallback, len(FALLBACK_SIZES)) - 1]
            return size if self.settings.max_size == 0 else min(size, self.settings.max_size)
        return self.settings.max_size

    async def _stream(self, serial):
        self._restart.clear()   # before reading settings: a later Apply still restarts
        jar = await asyncio.to_thread(scrcpy.ensure_server, self.settings.server_path)
        await self.adb.push(serial, await asyncio.to_thread(jar.read_bytes), scrcpy.REMOTE_PATH)
        scid = secrets.randbits(31)
        command = scrcpy.server_command(scid, self._max_size(), self.settings.max_fps,
                                        int(self.settings.bit_rate_mbps * 1_000_000))
        socket_name = f"localabstract:scrcpy_{scid:08x}"
        writers = []
        got_frame = False
        try:
            shell_reader, shell_writer = await self.adb.open_stream(serial, f"shell:{command}")
            writers.append(shell_writer)
            video, video_writer = await self._connect_video(serial, socket_name, shell_reader)
            writers.append(video_writer)
            control_reader, control_writer = await self.adb.open_stream(serial, socket_name)
            writers.append(control_writer)
            await asyncio.wait_for(video.readexactly(scrcpy.DEVICE_NAME_LENGTH), 5)
            codec = int.from_bytes(await asyncio.wait_for(video.readexactly(4), 10), "big")
            self._last_viewer = time.monotonic()
            if codec == scrcpy.CODEC_ERROR:
                raise StreamEnded("The phone's video encoder could not be configured.")
            if codec != scrcpy.CODEC_H264:
                raise StreamEnded("The phone disabled the video stream (is the screen capture blocked?).")
            self.control = asyncio.Queue(CONTROL_QUEUE_LIMIT)
            self.streaming = True
            self.error = None
            self.notify()
            frame_seen = asyncio.Event()
            tasks = [
                asyncio.create_task(self._read_video(video, frame_seen)),
                asyncio.create_task(self._write_control(control_writer)),
                asyncio.create_task(self._drain(control_reader)),
                asyncio.create_task(self._drain(shell_reader, log_lines=True)),
                asyncio.create_task(self._watch_viewer()),
                asyncio.create_task(self._restart.wait()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                got_frame = frame_seen.is_set()
                # Lift held fingers while the control writer can still deliver them.
                await self._release_pointers()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._control_drained(), 0.5)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()
            if not got_frame and not self._restart.is_set() and self.viewer:
                raise StreamEnded("The scrcpy server stopped before sending video.")
        except (StreamEnded, asyncio.IncompleteReadError, asyncio.TimeoutError):
            if not got_frame and self._fallback < len(FALLBACK_SIZES):
                self._fallback += 1
                log.warning("No video at this size; retrying at max_size=%s", self._max_size())
            raise
        finally:
            await self._release_pointers()
            self.streaming = False
            self.control = None
            self.width = self.height = 0
            self._config = b""
            self.hub.clear()
            for writer in writers:
                with contextlib.suppress(Exception):
                    writer.close()
            self.notify()

    async def _connect_video(self, serial, socket_name, shell_reader):
        """The server needs a moment to listen; retry until the dummy byte arrives."""
        deadline = time.monotonic() + 10
        while True:
            try:
                reader, writer = await self.adb.open_stream(serial, socket_name)
            except AdbError:
                reader = writer = None
            if reader is not None:
                try:
                    dummy = await asyncio.wait_for(reader.readexactly(1), 2)
                    if dummy == b"\x00":
                        return reader, writer
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
                    pass
                writer.close()
            if shell_reader.at_eof():
                raise StreamEnded("The scrcpy server exited during startup.")
            if time.monotonic() > deadline:
                raise StreamEnded("Timed out connecting to the scrcpy server on the phone.")
            await asyncio.sleep(0.1)

    async def _read_video(self, reader, frame_seen):
        while True:
            header = await reader.readexactly(scrcpy.HEADER_LENGTH)
            parsed = scrcpy.parse_header(header)
            if parsed[0] == "session":
                _, self.width, self.height = parsed
                self.notify()
                continue
            _, config, key, pts, size = parsed
            if size > scrcpy.MAX_PACKET_SIZE:
                raise StreamEnded("Oversized video packet")
            data = await reader.readexactly(size)
            if config:
                self._config = data
                self.hub.set_config({
                    "codec": "h264", "codec_string": scrcpy.h264_codec_string(data) or "avc1.42e01f",
                    "width": self.width, "height": self.height, "format": "annexb",
                })
                continue
            if key:
                self._last_key = time.monotonic()
            if key and self._config:
                data = self._config + data   # parameter sets inline: any key frame is a clean entry point
            frame_seen.set()
            self._fallback = 0
            self.hub.publish_video(data, key, pts)

    async def _write_control(self, writer):
        while True:
            message = await self.control.get()
            writer.write(message)
            await asyncio.wait_for(writer.drain(), 5)

    @staticmethod
    async def _drain(reader, log_lines=False):
        while True:
            data = await reader.read(4096)
            if not data:
                raise StreamEnded("The scrcpy server closed the connection.")
            if log_lines:
                for line in data.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        log.info("scrcpy-server: %s", line.strip()[:200])

    async def _watch_viewer(self):
        while True:
            if self.viewer:
                self._last_viewer = time.monotonic()
            elif time.monotonic() - self._last_viewer > VIEWER_GRACE_SECONDS:
                return
            await self.wait_viewer_change(1)

    # -- input -------------------------------------------------------------------------

    def _send(self, message):
        if self.control is None:
            return {"error": "Not connected to the phone"}
        try:
            self.control.put_nowait(message)
        except asyncio.QueueFull:
            return {"error": "Control channel busy"}
        return None

    async def _control_drained(self):
        while self.control is not None and not self.control.empty():
            await asyncio.sleep(0.02)

    def _request_keyframe(self):
        # RESET_VIDEO restarts the phone's encoder. Key frames already arrive every
        # 2 s (i-frame-interval), so only reset when they have stopped coming.
        if time.monotonic() - self._last_key < 2.5:
            return
        if self.control is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self.control.put_nowait(scrcpy.simple_command("reset_video"))

    def _point(self, x, y):
        w, h = self.width, self.height
        return round(x * (w - 1) / COORD_MAX), round(y * (h - 1) / COORD_MAX), w, h

    async def _release_pointers(self):
        if self.control is not None and self.width:
            for pid, (x, y) in list(self.pointers.items()):
                with contextlib.suppress(asyncio.QueueFull):
                    self.control.put_nowait(scrcpy.inject_touch(scrcpy.ACTION_UP, x, y, self.width, self.height, 0, pid))
        self.pointers.clear()

    async def set_viewer(self, present):
        if not present:
            await self._release_pointers()
        await super().set_viewer(present)

    async def handle_input(self, event):
        if self.control is None or not self.width:
            return {"error": "Video not started yet"}
        kind = event["type"]
        if kind == "touch":
            x, y, w, h = self._point(event["x"], event["y"])
            pid = event["id"]
            phase = event["phase"]
            if phase == "down":
                action, pressure = scrcpy.ACTION_DOWN, 1.0
                self.pointers[pid] = (x, y)
            elif phase == "move":
                if pid not in self.pointers:
                    return None
                action, pressure = scrcpy.ACTION_MOVE, 1.0
                self.pointers[pid] = (x, y)
            else:
                if self.pointers.pop(pid, None) is None:
                    return None
                action, pressure = scrcpy.ACTION_UP, 0.0
            return self._send(scrcpy.inject_touch(action, x, y, w, h, pressure, pid))
        if kind == "scroll":
            x, y, w, h = self._point(event["x"], event["y"])
            return self._send(scrcpy.inject_scroll(x, y, w, h, event["dx"], event["dy"]))
        if kind == "key":
            return self._key(event["key"], event["phase"])
        if kind == "text":
            text = event["text"]
            if text.isascii() and len(text.encode()) <= scrcpy.INJECT_TEXT_MAX_LENGTH:
                return self._send(scrcpy.inject_text(text))
            # Non-ASCII characters have no key events; paste them via the clipboard.
            return self._send(scrcpy.set_clipboard(text, paste=True))
        if kind == "command":
            name = event["name"]
            if name in {"screen_off", "screen_on"}:
                return self._send(scrcpy.set_display_power(name == "screen_on"))
            return self._send(scrcpy.simple_command(name))
        return {"error": "Unsupported event"}

    def _key(self, key, phase):
        actions = {"press": (scrcpy.ACTION_DOWN, scrcpy.ACTION_UP), "down": (scrcpy.ACTION_DOWN,),
                   "up": (scrcpy.ACTION_UP,)}[phase]
        for action in actions:
            if key == "back":
                message = scrcpy.back_or_screen_on(action)
            else:
                code = 219 if key == "siri" else scrcpy.KEYCODES[key]  # KEYCODE_ASSIST
                message = scrcpy.inject_keycode(action, code)
            error = self._send(message)
            if error:
                return error
        return None
