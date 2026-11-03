"""iPhone backend: picks the best capture/control route the connected iOS version allows.

  iOS 27+     CoreDevice HEVC stream (pymobiledevice3 serve-web), HID through it.
              If the stream cannot start, falls back to the iOS 17+ route below.
  iOS 17+     DVT screenshots (JPEG) + CoreDevice Universal/Indigo HID (live touch,
              buttons, typing).
  iOS 15-16   screenshotr (needs the Developer Disk Image, auto-mounted) for viewing.
              Control only if WebDriverAgent is installed; then WDA's MJPEG
              stream is used for faster video too.

The app never enables Developer Mode, reboots the phone, or starts a privileged
tunnel daemon on its own.
"""
import asyncio
import contextlib
import hashlib
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from phoneview.backends.base import Backend, ScreenshotUnavailable, capabilities, setting
from phoneview.ios.connection import TunnelSetupError, open_device
from phoneview.ios.developer_mode import DeveloperModeMonitor
from phoneview.streaming import encode_frame

log = logging.getLogger(__name__)

VIDEO_MODES = ("auto", "stream", "screenshot")
SHORT_EDGES = (0, 360, 480, 720)
SETTING_NAMES = ("short_edge", "jpeg_quality", "active_fps", "idle_fps", "bandwidth_mbps")


class StreamDropped(RuntimeError):
    """The live stream worked and then failed: reconnect, don't give up on it."""


class ReconnectNow(RuntimeError):
    """Not an error: start a new device session right away (e.g. after mounting the DDI)."""


DISPLAY_SERVICE = "com.apple.coredevice.displayservice"


def offered_services(rsd):
    """Service names from the RSD handshake (empty if unknown)."""
    try:
        return set((rsd.peer_info or {}).get("Services") or {})
    except Exception:
        return set()


class SetupNeeded(RuntimeError):
    """A user action on the phone is required; the message says which."""


@dataclass(frozen=True)
class IOSSettings:
    active_fps: float = 3
    idle_fps: float = 0.5
    active_seconds: float = 2
    serial: str = None
    short_edge: int = 480
    jpeg_quality: int = 65
    bandwidth_mbps: float = 1.5
    tunnel: bool = False
    rsd_address: tuple = None
    video_mode: str = "auto"
    auto_mount_ddi: bool = True
    wda_port: int = 8100

    def __post_init__(self):
        if (type(self.active_fps) not in (int, float) or type(self.idle_fps) not in (int, float)
                or not (0.1 <= self.idle_fps <= self.active_fps <= 30)):
            raise ValueError("FPS must satisfy 0.1 <= idle <= active <= 30")
        if not math.isfinite(self.active_seconds) or self.active_seconds <= 0:
            raise ValueError("active_seconds must be finite and positive")
        if type(self.short_edge) is not int or self.short_edge not in SHORT_EDGES:
            raise ValueError("Resolution must be Native (0), 360, 480, or 720")
        if type(self.jpeg_quality) is not int or not 30 <= self.jpeg_quality <= 90:
            raise ValueError("JPEG quality must be an integer in 30..90")
        if type(self.bandwidth_mbps) not in (int, float) or not 0.1 <= self.bandwidth_mbps <= 10:
            raise ValueError("Bandwidth must be finite and in 0.1..10 Mbps")
        if self.video_mode not in VIDEO_MODES:
            raise ValueError(f"video_mode must be one of {VIDEO_MODES}")
        if self.tunnel and self.rsd_address is not None:
            raise ValueError("Choose --tunnel or --rsd, not both")


def as_png(data):
    """Screenshot services return PNG (or TIFF on some versions); store PNG."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()


def ios_major(version):
    head = str(version or "").split(".", 1)[0]
    return int(head) if head.isdigit() else None


class IOSBackend(Backend):
    platform = "ios"

    def __init__(self, settings=None, hub=None):
        super().__init__(hub)
        self.settings = settings or IOSSettings()
        self.devmode = DeveloperModeMonitor()
        self.mode = None              # "stream", "screenshot", "legacy"
        self.note = None              # non-fatal explanation (e.g. why we fell back)
        self.capture_ready = False
        self.hid = None               # HidController (iOS 17+ screenshot mode)
        self.stream = None            # CoreDeviceStream
        self.wda = None               # WdaController once a session exists
        self.wda_state = None         # human-readable WDA status (legacy mode)
        self.runner = None            # installed WDA runner bundle id
        self.udid = None
        self.major = None
        self._stream_failed = {}      # udid -> reason
        self._encoder = None
        self._wake = asyncio.Event()
        self._retry = asyncio.Event()
        self._session_task = None
        self._mjpeg_failed = False
        self.active_until = 0.0
        self.stream_revision = 0
        self.capture_ms = self.encode_ms = 0.0
        self.source_bytes = 0
        self.frame_width = self.frame_height = 0
        self._counts = [0, 0]
        self._rates = [0.0, 0.0]
        self._window = time.monotonic()
        self._rsd = None              # iOS 17+ RemoteServiceDiscovery while connected
        self._capturing = False       # _capture_loop is running
        self._raw_waiters = []        # futures for the next full-resolution capture

    # -- capabilities / status -------------------------------------------------

    def _jpeg_settings(self):
        s = self.settings
        return [
            setting("short_edge", "Portrait width", s.short_edge,
                    options=[(v, "Native" if v == 0 else f"{v} px") for v in SHORT_EDGES]),
            setting("jpeg_quality", "JPEG quality", s.jpeg_quality, minimum=30, maximum=90, step=1),
            setting("active_fps", "Active fps", s.active_fps, minimum=0.1, maximum=30, step=0.1),
            setting("idle_fps", "Idle fps", s.idle_fps, minimum=0.1, maximum=30, step=0.1),
            setting("bandwidth_mbps", "Image budget (Mbps)", s.bandwidth_mbps, minimum=0.1, maximum=10, step=0.1),
        ]

    def capabilities(self):
        from phoneview.ios import hid as hid_module, stream as stream_module, wda as wda_module
        setup = ["developer_mode_check", "developer_mode_reveal"]
        if self.mode == "stream":
            return capabilities(video="hevc", touch=True, live_touch=True,
                                keys=stream_module.STREAM_KEYS, setup=setup + ["use_screenshots"])
        if self.mode == "screenshot":
            if self.udid in self._stream_failed:
                setup.append("retry_stream")
            return capabilities(video="jpeg", touch=self.hid is not None, live_touch=True,
                                keys=hid_module.HID_KEYS if self.hid else (), text=self.hid is not None,
                                setup=setup, settings=self._jpeg_settings())
        if self.mode == "legacy":
            setup.append("mount_ddi")
            if self.wda is None and self.runner:
                setup.append("start_wda")
            return capabilities(video="jpeg", touch=self.wda is not None, live_touch=False,
                                keys=wda_module.WDA_KEYS if self.wda else (), text=self.wda is not None,
                                setup=setup, settings=self._jpeg_settings())
        return capabilities(setup=setup)

    def jpeg_budget_mbps(self):
        return self.settings.bandwidth_mbps

    def metrics(self):
        now = time.monotonic()
        elapsed = now - self._window
        if elapsed >= 2:
            self._rates = [round(c / elapsed, 2) for c in self._counts]
            self._counts = [0, 0]
            self._window = now
        return dict(capture_fps=self._rates[0], published_fps=self._rates[1],
                    screenshot_ms=round(self.capture_ms, 1), encode_ms=round(self.encode_ms, 1),
                    source_kb=round(self.source_bytes / 1000, 1),
                    width=self.frame_width, height=self.frame_height)

    def status(self):
        control = None
        if self.mode == "stream":
            control = "CoreDevice HID (via stream)"
        elif self.hid is not None:
            control = "CoreDevice HID"
        elif self.wda is not None:
            control = "WebDriverAgent"
        return dict(mode=self.mode, note=self.note, capture=self.capture_ready, control=control,
                    wda=self.wda_state, developer_mode=self.devmode.snapshot(),
                    stream_settings={name: getattr(self.settings, name) for name in SETTING_NAMES},
                    **self.metrics())

    # -- settings / setup ------------------------------------------------------

    async def update_settings(self, values):
        if not isinstance(values, dict) or not set(values) <= set(SETTING_NAMES):
            raise ValueError("Expected resolution, quality, active/idle FPS, and bandwidth")
        new = replace(self.settings, **values)
        if new != self.settings:
            self.settings = new
            self.stream_revision += 1
            self.mark_active()
            if self.wda is not None:
                # Not awaited: a slow WDA call must not stall the WebSocket reader.
                task = asyncio.create_task(self._apply_mjpeg_settings())
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self.notify()

    async def setup_action(self, name):
        if name in {"developer_mode_check", "developer_mode_reveal"}:
            result = await self.devmode.check_usb(self.settings.serial, retry_reveal=name == "developer_mode_reveal")
            self._retry.set()
            self.notify()
            return {"developer_mode": result}
        if name == "use_screenshots":
            if self.udid:
                self._stream_failed[self.udid] = "Switched to screenshots from the viewer."
            self._restart_session()
            return {"message": "Switching to screenshots..."}
        if name == "retry_stream":
            self._stream_failed.pop(self.udid, None)
            self._restart_session()
            return {"message": "Retrying the screen stream..."}
        if name in {"mount_ddi", "start_wda"}:
            self._restart_session()
            return {"message": "Retrying setup..."}
        raise ValueError("Unknown setup action")

    def _restart_session(self):
        if self._session_task is not None and not self._session_task.done():
            self._session_task.cancel()
        self._retry.set()

    # -- main loop ---------------------------------------------------------------

    async def run(self):
        while True:
            self._retry.clear()
            self._session_task = asyncio.create_task(self._session())
            try:
                await self._session_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    self._session_task.cancel()
                    raise
                self.error = None
            except ReconnectNow as exc:
                log.info("%s; reconnecting", exc)
                self.error = None
                self._retry.set()
            except SetupNeeded as exc:
                self._set_error(str(exc))
            except TunnelSetupError as exc:
                self._set_error(str(exc))
            except Exception as exc:
                name = type(exc).__name__
                if name in {"NoDeviceConnectedError", "ConnectionFailedToUsbmuxdError", "DeviceNotFoundError"}:
                    self._forget_device()
                    self._set_error("No iPhone found. Connect it by USB, unlock it, and accept Trust This Computer."
                                    + (" (usbmuxd / Apple Mobile Device Support is not running.)"
                                       if name == "ConnectionFailedToUsbmuxdError" else ""))
                elif name in {"NotPairedError", "PairingDialogResponsePendingError", "UserDeniedPairingError"}:
                    self._set_error("Unlock the iPhone and tap Trust on the 'Trust This Computer?' prompt.")
                else:
                    self.devmode.disconnected()
                    self._set_error(f"iPhone unavailable ({name}); reconnecting")
            finally:
                await self._reset()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._retry.wait(), 3)

    def _set_error(self, message):
        if self.error != message:
            log.warning(message)
        self.error = message
        self.notify()

    def _forget_device(self):
        if self.device is not None:
            self.device = None
            self.udid = None
            self.generation += 1

    async def _reset(self):
        self.mode = None
        self.capture_ready = False
        self.hid = self.stream = self.wda = None
        self.generation += 1
        self.capture_ms = self.encode_ms = 0.0
        self.source_bytes = self.frame_width = self.frame_height = 0
        self.hub.request_keyframe = None
        self.hub.clear()
        self.notify()

    async def _session(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        lockdown = await asyncio.wait_for(create_using_usbmux(serial=self.settings.serial), 8)
        try:
            values = lockdown.all_values
            self.udid = lockdown.udid
            version = values.get("ProductVersion", "")
            self.major = ios_major(version)
            self.device = {"serial": self.udid, "name": values.get("DeviceName") or "iPhone",
                           "model": values.get("ProductType"), "os": "iOS", "version": version}
            mode_on = await self.devmode.check(lockdown)
            self.error = None
            self.notify()
            if self.major is not None and self.major < 15:
                raise SetupNeeded(f"iOS {version} is not supported; iOS 15 or newer is required.")
            if self.major is not None and self.major < 17:
                if mode_on is False:
                    raise SetupNeeded("Developer Mode is off. Follow the setup steps, restart the iPhone "
                                      "when prompted, and confirm Turn On after unlocking.")
                await self._legacy(lockdown)
                return
        except BaseException:
            with contextlib.suppress(Exception):
                await lockdown.close()
            raise
        await lockdown.close()
        await self._coredevice()

    # -- iOS 17+ -------------------------------------------------------------------

    async def _inspect(self, provider):
        mode = await self.devmode.check(provider)
        self.notify()
        return mode

    async def _coredevice(self):
        s = self.settings
        async with open_device(self.udid, tunnel=s.tunnel, rsd_address=s.rsd_address, on_device=self._inspect) as rsd:
            self._rsd = rsd
            try:
                await self._coredevice_session(rsd)
            finally:
                self._rsd = None

    async def _coredevice_session(self, rsd):
        from phoneview.ios.stream import StreamUnavailable, stream_supported
        s = self.settings
        if await self._ensure_ddi(rsd):
            # The RSD handshake listed the services before the mount; reconnect to see the new ones.
            raise ReconnectNow("Developer Disk Image mounted")
        self.generation += 1
        want_stream = s.video_mode != "screenshot" and self.udid not in self._stream_failed
        services = offered_services(rsd)
        if want_stream and services and DISPLAY_SERVICE not in services:
            self._stream_failed[self.udid] = "This iPhone does not offer the live screen stream; using screenshots."
            want_stream = False
            log.warning("%s (no %s)", self._stream_failed[self.udid], DISPLAY_SERVICE)
        if want_stream and stream_supported(rsd.product_version):
            try:
                await self._run_stream(rsd)
            except (asyncio.CancelledError, StreamDropped):
                raise
            except Exception as exc:
                if s.video_mode == "stream":
                    raise
                reason = str(exc) if isinstance(exc, StreamUnavailable) else type(exc).__name__
                self._stream_failed[self.udid] = reason
                self.note = f"Screen stream unavailable ({reason}); using screenshots."
                log.warning(self.note)
                self.hub.clear()
        elif want_stream:
            self.note = (f"Live video streaming needs iOS 27 or later (this iPhone runs "
                         f"{rsd.product_version}); using screenshots.")
        if self.udid in self._stream_failed and self.note is None:
            self.note = self._stream_failed[self.udid]
        await self._screenshot_mode(rsd)

    async def _run_stream(self, rsd):
        from phoneview.ios.stream import CoreDeviceStream, StreamUnavailable
        self.stream = CoreDeviceStream(rsd, self.hub)
        self.mode = "stream"
        self.note = "Starting the CoreDevice screen stream..."
        self.notify()
        started = asyncio.Event()
        task = asyncio.create_task(self.stream.run(started))
        waiter = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, timeout=45, return_when=asyncio.FIRST_COMPLETED)
            if not started.is_set():
                if task in done and task.exception() is not None:
                    raise task.exception()
                raise StreamUnavailable("no video within 45 s")
            self.note = None
            self.capture_ready = True
            self.notify()
            try:
                await task
            except Exception as exc:
                # It worked before: treat as a transient failure and reconnect.
                raise StreamDropped(str(exc) or type(exc).__name__) from exc
        finally:
            for t in (task, waiter):
                t.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)
            self.stream = None
            self.capture_ready = False

    async def _screenshot_mode(self, rsd):
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
        self.mode = "screenshot"
        self.notify()

        async def capture():
            async with DvtProvider(rsd) as dvt, Screenshot(dvt) as screenshot:
                await self._capture_loop(screenshot.get_screenshot)

        async with asyncio.TaskGroup() as group:
            group.create_task(capture())
            group.create_task(self._hid_loop(rsd))

    async def _hid_loop(self, rsd):
        from phoneview.ios.hid import HidController, direct_hid
        while True:
            try:
                async with direct_hid(rsd) as (uhs, indigo):
                    self.hid = HidController(uhs, indigo)
                    self.notify()
                    await self.hid.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log event bodies, screenshots, or device-supplied content.
                self.note = f"HID unavailable ({type(exc).__name__}); retrying"
                log.warning(self.note)
            finally:
                self.hid = None
                self.notify()
            await asyncio.sleep(3)

    # -- iOS 15-16 -------------------------------------------------------------------

    async def _ensure_ddi(self, lockdown):
        """Mount the Developer Disk Image if needed; True if it was mounted just now.

        iOS 15-16 use the shared "Developer" image (over lockdown). iOS 17+ use a
        "Personalized" image, which provides the CoreDevice developer services
        (display stream, HID, screenshots); from iOS 27 it can only be installed
        over the RSD tunnel, so pass the RSD there.
        """
        from pymobiledevice3.exceptions import AlreadyMountedError
        from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService, auto_mount
        kind = "Personalized" if (self.major or 0) >= 17 else "Developer"
        async with MobileImageMounterService(lockdown=lockdown) as mounter:
            if await mounter.is_image_mounted(kind):
                return False
        if not self.settings.auto_mount_ddi:
            raise SetupNeeded("The Developer Disk Image is not mounted. Restart PhoneView without "
                              "--no-auto-mount, or mount it with 'pymobiledevice3 mounter auto-mount'.")
        self.note = "Mounting the Developer Disk Image (first run downloads it)..."
        self.notify()
        try:
            await asyncio.wait_for(auto_mount(lockdown), 180)
        except AlreadyMountedError:
            self.note = None
            return False
        except Exception as exc:
            log.warning("Developer Disk Image mount failed: %s: %s", type(exc).__name__, exc)
            raise SetupNeeded(f"Could not mount the Developer Disk Image ({type(exc).__name__}). "
                              "Keep the iPhone unlocked with Developer Mode on, check the internet "
                              "connection for the first download, then retry.") from exc
        self.note = None
        log.info("Mounted the %s Developer Disk Image", kind)
        return True

    async def _legacy(self, lockdown):
        await self._ensure_ddi(lockdown)
        self.mode = "legacy"
        self.generation += 1
        self._mjpeg_failed = False
        self.wda_state = "Looking for WebDriverAgent..."
        self.notify()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._legacy_video(lockdown))
            group.create_task(self._wda_loop(lockdown))

    async def _legacy_video(self, lockdown):
        from pymobiledevice3.services.screenshot import ScreenshotService
        from phoneview.ios.wda import mjpeg_frames
        while True:
            if self.wda is not None and not self._mjpeg_failed:
                try:
                    await self._mjpeg_loop(mjpeg_frames(self.udid))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.info("MJPEG unavailable (%s); using screenshotr", type(exc).__name__)
                    self._mjpeg_failed = True
            async with ScreenshotService(lockdown) as service:
                await self._capture_loop(service.take_screenshot,
                                         stop=lambda: self.wda is not None and not self._mjpeg_failed)

    async def _mjpeg_loop(self, frames):
        previous = None
        async for data, width, height in frames:
            self.capture_ready = True
            self._counts[0] += 1
            if self.wda is None:
                return
            if not self.viewer:
                continue
            digest = hashlib.blake2s(data, digest_size=16).digest()
            if digest != previous:
                previous = digest
                self.source_bytes = len(data)
                self.frame_width, self.frame_height = width, height
                self._counts[1] += 1
                self.hub.publish_jpeg(data, width, height)

    async def _apply_mjpeg_settings(self):
        wda = self.wda
        if wda is None or not wda.session:
            return
        scale = 100
        if self.settings.short_edge and wda.size[0]:
            native = min(wda.size) * 2   # points -> pixels on @2x phones (approximate on @3x)
            scale = max(10, min(100, round(self.settings.short_edge * 100 / native)))
        with contextlib.suppress(Exception):
            await wda.call("POST", f"/session/{wda.session}/appium/settings", {"settings": {
                "mjpegScalingFactor": scale, "mjpegServerScreenshotQuality": self.settings.jpeg_quality,
                "mjpegServerFramerate": max(1, min(30, round(self.settings.active_fps * 5)))}})

    async def _wda_loop(self, lockdown):
        from phoneview.ios.wda import WdaController, find_runner, launch_runner
        controller = WdaController(self.udid, self.settings.wda_port)
        runner_task = None
        last_launch = 0.0
        try:
            with contextlib.suppress(Exception):
                self.runner = await find_runner(lockdown)
            while True:
                if await controller.ready():
                    try:
                        await controller.connect()
                    except Exception as exc:
                        # e.g. a system alert blocks session creation; keep viewing, retry later.
                        self.wda_state = f"WebDriverAgent is running but refused a session ({type(exc).__name__}); retrying"
                        self.notify()
                        await asyncio.sleep(5)
                        continue
                    self.wda = controller
                    self.wda_state = "Connected"
                    await self._apply_mjpeg_settings()
                    self.notify()
                    worker = asyncio.create_task(controller.run())
                    try:
                        while await controller.ready():
                            await asyncio.sleep(5)
                    finally:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                        self.wda = None
                        self._mjpeg_failed = False
                        self.wda_state = "WebDriverAgent stopped responding"
                        self.notify()
                    continue
                if self.runner is None:
                    self.wda_state = ("View only: WebDriverAgent is not installed, so the iPhone cannot be "
                                      "controlled on iOS 16. See docs/ios-wda.md to install it without a Mac.")
                    self.notify()
                    await asyncio.sleep(30)
                    with contextlib.suppress(Exception):
                        self.runner = await find_runner(lockdown)
                    continue
                if (runner_task is None or runner_task.done()) and time.monotonic() - last_launch > 30:
                    last_launch = time.monotonic()
                    self.wda_state = f"Starting {self.runner}..."
                    self.notify()
                    try:
                        runner_task = await launch_runner(lockdown, self.runner)
                    except Exception as exc:
                        self.wda_state = (f"Could not start WebDriverAgent ({type(exc).__name__}). If it was "
                                          "signed with a free Apple ID, re-sign it (certificates expire after 7 days).")
                        self.notify()
                for _ in range(30):
                    if await controller.ready() or (runner_task is not None and runner_task.done()):
                        break
                    await asyncio.sleep(1)
                if runner_task is not None and runner_task.done() and not runner_task.cancelled():
                    exc = runner_task.exception()
                    if exc is not None:
                        self.wda_state = f"WebDriverAgent exited ({type(exc).__name__})"
                        self.notify()
        finally:
            if runner_task is not None:
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)

    # -- capture -------------------------------------------------------------------

    def mark_active(self):
        self.active_until = time.monotonic() + self.settings.active_seconds
        self._wake.set()

    async def set_viewer(self, present):
        await super().set_viewer(present)
        if present:
            self.mark_active()
        else:
            if self.hid is not None:
                await self.hid.release()
            if self.wda is not None:
                self.wda.gesture = None
            if self.stream is not None:
                await self.stream.release()
        self._wake.set()

    async def _sleep(self, delay):
        self._wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), max(0, delay))

    async def _capture_loop(self, grab, stop=None):
        """Poll screenshots at active/idle fps; encode changed ones off the event loop."""
        previous = None
        last_capture = 0.0
        self._capturing = True
        try:
            await self._capture_frames(grab, stop, previous, last_capture)
        finally:
            self._capturing = False
            for waiter in self._raw_waiters:
                if not waiter.done():
                    waiter.set_exception(ScreenshotUnavailable("The iPhone connection was interrupted"))
            self._raw_waiters.clear()

    async def _capture_frames(self, grab, stop, previous, last_capture):
        while stop is None or not stop():
            if not self._raw_waiters:
                await self._sleep(last_capture + 1 / self.settings.active_fps - time.monotonic())
            probe = not self.viewer
            revision = self.stream_revision
            start = last_capture = time.monotonic()
            image = await asyncio.wait_for(grab(), 5)
            waiters, self._raw_waiters = self._raw_waiters, []
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(bytes(image))
            self.capture_ms = (time.monotonic() - start) * 1000
            self._counts[0] += 1
            self.capture_ready = True
            if not probe:
                digest = hashlib.blake2s(image, digest_size=16).digest()
                if (digest, revision) != previous:
                    if self._encoder is None:
                        self._encoder = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screenshot")
                    encode_start = time.monotonic()
                    encoded = await asyncio.get_running_loop().run_in_executor(
                        self._encoder, encode_frame, image, self.settings.short_edge, self.settings.jpeg_quality)
                    if self.viewer and revision == self.stream_revision:
                        self.encode_ms = (time.monotonic() - encode_start) * 1000
                        self.source_bytes = len(image)
                        self.frame_width, self.frame_height = encoded.width, encoded.height
                        previous = (digest, revision)
                        self._counts[1] += 1
                        self.hub.publish_jpeg(encoded.data, encoded.width, encoded.height)
                    del encoded
            del image
            fps = self.settings.active_fps if time.monotonic() < self.active_until else self.settings.idle_fps
            # Without a viewer, one probe every five seconds detects unplugging.
            if not self._raw_waiters:     # a screenshot was requested: capture again now
                await self._sleep(max(0, 1 / fps - (time.monotonic() - start)) if self.viewer else 5)

    # -- screenshots ---------------------------------------------------------------

    async def screenshot(self):
        """Full-resolution capture from the iPhone, as PNG."""
        if self.mode == "stream" and self._rsd is not None:
            # The live stream is video; ask the screenshot service for one image.
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
            try:
                async with DvtProvider(self._rsd) as dvt, Screenshot(dvt) as service:
                    raw = await asyncio.wait_for(service.get_screenshot(), 10)
            except Exception as exc:
                raise ScreenshotUnavailable(f"The iPhone did not return a screenshot ({type(exc).__name__})") from exc
        elif self._capturing:
            waiter = asyncio.get_running_loop().create_future()
            self._raw_waiters.append(waiter)
            self._wake.set()                 # capture now instead of at the next frame time
            try:
                raw = await asyncio.wait_for(waiter, 10)
            except asyncio.TimeoutError as exc:
                raise ScreenshotUnavailable("The iPhone did not return a screenshot in time") from exc
        else:
            return await super().screenshot()   # e.g. WebDriverAgent MJPEG frames
        return await asyncio.to_thread(as_png, raw), "image/png"

    # -- input -------------------------------------------------------------------

    async def handle_input(self, event):
        self.mark_active()
        if self.mode == "stream" and self.stream is not None:
            return self.stream.submit(event)
        if self.hid is not None:
            return self.hid.submit(event)
        if self.wda is not None:
            return self.wda.submit(event)
        return {"error": "This iPhone can be viewed but not controlled right now"}

    async def close(self):
        if self._encoder is not None:
            await asyncio.to_thread(self._encoder.shutdown, wait=True, cancel_futures=True)
            self._encoder = None
