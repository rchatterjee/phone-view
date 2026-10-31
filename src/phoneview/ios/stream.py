"""CoreDevice screen stream ("serve-web"): real HEVC video from iOS 27+.

pymobiledevice3's ScreenStreamServer already handles the hard parts (RTP/HEVC
depacketising, RTCP key-frame requests, encoder stall recovery). We run it
in-process on a random loopback port and consume its two public endpoints:

  GET /codec       -> {"codec": "hev1....", "description": base64 hvcC}
  GET /stream.bin  -> chunked body of [u32 length][u8 type][4-byte-length NALUs]
                      type 0 = key, 1 = delta, 2 = key that needs a decoder reset

Input goes through its /touch and /button endpoints so only one owner talks to
the HID services. Anything unexpected raises, and the caller falls back to the
screenshot path.
"""
import asyncio
import base64
import contextlib
import json
import logging
import socket
import time

from packaging.version import Version

from phoneview import httpmini

log = logging.getLogger(__name__)

STARTUP_TIMEOUT = 30
BUTTON_NAMES = {"home": "home", "power": "lock", "volume_up": "volume-up",
                "volume_down": "volume-down", "siri": "siri"}
STREAM_KEYS = set(BUTTON_NAMES) | {"app_switch"}


class StreamUnavailable(RuntimeError):
    pass


def stream_supported(product_version):
    from pymobiledevice3.remote.core_device.display_service import VIDEO_STREAM_MIN_VERSION
    try:
        return Version(product_version) >= VIDEO_STREAM_MIN_VERSION
    except Exception:
        return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def no_loop_signal_handlers(loop):
    """ScreenStreamServer.serve() installs SIGINT/SIGTERM handlers on the loop,
    which would take Ctrl-C away from Uvicorn. Refuse them while it runs (it
    already tolerates NotImplementedError, as on Windows)."""
    def refuse(*_args, **_kwargs):
        raise NotImplementedError
    loop.add_signal_handler = refuse
    try:
        yield
    finally:
        with contextlib.suppress(AttributeError):
            del loop.add_signal_handler


class CoreDeviceStream:
    def __init__(self, rsd, hub):
        self.rsd = rsd
        self.hub = hub
        self.port = free_port()
        self.server = None
        self.codec = None
        self.input_queue = asyncio.Queue(256)
        self.touching = False
        self.last_point = (32768, 32768)
        self.frames = 0

    def _open(self):
        return asyncio.open_connection("127.0.0.1", self.port)

    async def _post(self, path, body):
        status, _, data = await httpmini.request(self._open, "POST", path, body, timeout=5)
        if status >= 400:
            raise httpmini.HttpError(status, data[:120].decode("utf-8", "replace"))

    async def _fetch_codec(self, deadline):
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, httpmini.HttpError, asyncio.TimeoutError, ValueError):
                status, _, data = await httpmini.request(self._open, "GET", "/codec", timeout=10)
                if status == 200:
                    codec = json.loads(data)
                    if codec.get("codec") and codec.get("description"):
                        return codec
            await asyncio.sleep(1)
        raise StreamUnavailable("The device did not start its screen stream in time.")

    def _set_config(self, codec, force=False):
        base64.b64decode(codec["description"], validate=True)
        self.codec = codec
        self.hub.set_config({"codec": "hevc", "codec_string": codec["codec"],
                             "description": codec["description"], "width": 0, "height": 0,
                             "format": "hvcc"}, force=force)

    def request_keyframe(self):
        request = getattr(self.server, "_request_recovery_idr", None)
        if request is not None:
            with contextlib.suppress(Exception):
                request(reason="phoneview subscriber")

    async def run(self, started):
        """Stream until cancelled or failure. `started` is set once frames flow."""
        from pymobiledevice3.remote.core_device.screen_stream import ScreenStreamServer
        self.server = ScreenStreamServer(self.rsd, bind="127.0.0.1", http_port=self.port,
                                         audio_default_on=False)
        loop = asyncio.get_running_loop()
        with no_loop_signal_handlers(loop):
            serve = asyncio.create_task(self.server.serve(), name="coredevice-stream")
            worker = asyncio.create_task(self._input_worker())
            try:
                pump = asyncio.create_task(self._pump(started))
                done, _ = await asyncio.wait({serve, pump}, return_when=asyncio.FIRST_COMPLETED)
                for task in (serve, pump):
                    if task not in done:
                        task.cancel()
                await asyncio.gather(serve, pump, return_exceptions=True)
                for task in done:
                    if task.exception() is not None:
                        raise task.exception()
                raise StreamUnavailable("The screen stream ended.")
            finally:
                worker.cancel()
                serve.cancel()
                await asyncio.gather(worker, serve, return_exceptions=True)
                self.hub.request_keyframe = None

    async def _pump(self, started):
        codec = await self._fetch_codec(time.monotonic() + STARTUP_TIMEOUT)
        self._set_config(codec)
        self.hub.request_keyframe = self.request_keyframe
        reader, writer = await asyncio.wait_for(self._open(), 5)
        try:
            writer.write(httpmini.build_request("GET", "/stream.bin"))
            await writer.drain()
            status, headers = await asyncio.wait_for(httpmini.read_head(reader), 15)
            if status != 200:
                raise StreamUnavailable(f"Stream endpoint returned HTTP {status}")
            buffer = bytearray()
            async for piece in httpmini.iter_chunked(reader):
                buffer += piece
                while len(buffer) >= 5:
                    length = int.from_bytes(buffer[:4], "big")
                    if length < 1 or length > 16 << 20:
                        raise StreamUnavailable("Malformed stream framing")
                    if len(buffer) < 4 + length:
                        break
                    kind = buffer[4]
                    data = bytes(buffer[5:4 + length])
                    del buffer[:4 + length]
                    if kind == 2:
                        # Decoder reset requested; parameter sets may have changed.
                        # Keep the previous ones if the refetch is slow.
                        with contextlib.suppress(StreamUnavailable):
                            self._set_config(await self._fetch_codec(time.monotonic() + 5), force=True)
                    self.frames += 1
                    self.hub.publish_video(data, kind in (0, 2), self.frames)
                    if not started.is_set():
                        started.set()
        finally:
            writer.close()

    async def release(self):
        if self.touching:
            self.touching = False
            with contextlib.suppress(Exception):
                x, y = self.last_point
                await self._post("/touch", {"type": "release", "x": x, "y": y})

    # -- input -----------------------------------------------------------------

    def submit(self, event):
        try:
            self.input_queue.put_nowait(event)
        except asyncio.QueueFull:
            return {"error": "Controller busy"}
        return None

    async def _input_worker(self):
        while True:
            event = await self.input_queue.get()
            try:
                await self._apply(event)
            except Exception as exc:  # keep streaming even if one input fails
                log.warning("stream input failed: %s", type(exc).__name__)

    async def _apply(self, event):
        if event["type"] == "touch":
            if event["id"] != 0:
                return
            if event["phase"] in {"down", "move"}:
                if event["phase"] == "move":
                    if not self.touching:
                        return
                    # Coalesce queued moves.
                    while not self.input_queue.empty():
                        nxt = self.input_queue._queue[0]
                        if nxt["type"] == "touch" and nxt["phase"] == "move" and nxt["id"] == 0:
                            event = self.input_queue.get_nowait()
                        else:
                            break
                self.touching = True
                self.last_point = (event["x"], event["y"])
                await self._post("/touch", {"type": "contact", "x": event["x"], "y": event["y"]})
            elif self.touching:
                self.touching = False
                await self._post("/touch", {"type": "release", "x": event["x"], "y": event["y"]})
        elif event["type"] == "key":
            key = event["key"]
            if key == "app_switch":
                await self._post("/button", {"name": "home", "state": "press"})
                await asyncio.sleep(0.12)
                await self._post("/button", {"name": "home", "state": "press"})
            elif key in BUTTON_NAMES:
                await self._post("/button", {"name": BUTTON_NAMES[key], "state": event["phase"]})
