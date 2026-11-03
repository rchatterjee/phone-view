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


HEVC_NAL_SPS = 33


def sps_of(access_unit):
    """The SPS NAL in a 4-byte-length-prefixed HEVC access unit, or None."""
    i, n = 0, len(access_unit)
    while i + 4 <= n:
        size = int.from_bytes(access_unit[i:i + 4], "big")
        start, i = i + 4, i + 4 + size
        if size < 2 or i > n:
            return None
        if (access_unit[start] >> 1) & 0x3F == HEVC_NAL_SPS:
            return bytes(access_unit[start:i])
    return None


class _Bits:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def u(self, n):
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]          # IndexError on truncated data
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def ue(self):
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)


def sps_size(sps):
    """(coded_w, coded_h, visible_w, visible_h) from an HEVC SPS NAL unit (with its 2-byte header)."""
    rbsp, zeros = bytearray(), 0
    for byte in sps[2:]:                              # drop emulation-prevention bytes
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        rbsp.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    r = _Bits(bytes(rbsp))
    r.u(4)                                            # sps_video_parameter_set_id
    max_sub_layers = r.u(3)
    r.u(1)
    r.u(88)                                           # general profile space/tier/idc, flags
    r.u(8)                                            # general_level_idc
    present = [(r.u(1), r.u(1)) for _ in range(max_sub_layers)]
    if max_sub_layers:
        r.u(2 * (8 - max_sub_layers))
    for profile, level in present:
        if profile:
            r.u(88)
        if level:
            r.u(8)
    r.ue()                                            # sps_seq_parameter_set_id
    chroma = r.ue()
    if chroma == 3:
        r.u(1)
    width, height = r.ue(), r.ue()
    left = right = top = bottom = 0
    if r.u(1):                                        # conformance_window_flag
        left, right, top, bottom = r.ue(), r.ue(), r.ue(), r.ue()
    sub_w = 2 if chroma in (1, 2) else 1
    sub_h = 2 if chroma == 1 else 1
    return width, height, width - sub_w * (left + right), height - sub_h * (top + bottom)


def hvcc_size(description):
    """Picture size from the SPS in an hvcC record, or None if it cannot be read."""
    try:
        i, arrays = 23, description[22]
        for _ in range(arrays):
            nal_type = description[i] & 0x3F
            count = int.from_bytes(description[i + 1:i + 3], "big")
            i += 3
            for _ in range(count):
                length = int.from_bytes(description[i:i + 2], "big")
                nal = description[i + 2:i + 2 + length]
                i += 2 + length
                if nal_type == HEVC_NAL_SPS:
                    return sps_size(nal)
    except (IndexError, ValueError):
        pass
    return None


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
        self.description = b""        # hvcC record the browser's decoder is configured with
        self._config_checked = 0.0
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
        self.description = base64.b64decode(codec["description"], validate=True)
        self.codec = codec
        # The picture size goes to the browser too: Chrome's HEVC decoder on Windows,
        # configured without codedWidth/codedHeight, reported 1280x720 frames for this
        # 1184x2576 stream and showed only the top of the screen.
        coded_w, coded_h, width, height = hvcc_size(self.description) or (0, 0, 0, 0)
        self.hub.set_config({"codec": "hevc", "codec_string": codec["codec"],
                             "description": codec["description"], "width": width, "height": height,
                             "coded_width": coded_w, "coded_height": coded_h,
                             "format": "hvcc"}, force=force)

    def _sps_changed(self, access_unit):
        """True if this key frame carries an SPS the browser's decoder was not given."""
        sps = sps_of(access_unit)
        if sps is None or not self.description or sps in self.description:
            return False
        now = time.monotonic()
        if now - self._config_checked < 1.0:     # the refetch lags: don't loop on it
            return False
        self._config_checked = now
        log.info("iPhone stream parameters changed; reconfiguring the viewer")
        return True

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
                    if kind == 2 or (kind == 0 and self._sps_changed(data)):
                        # Decoder reset requested, or the phone changed its picture size
                        # (it can start at 1280x720 and switch to the portrait screen size
                        # without a reset): send the new parameter sets so the browser
                        # reconfigures before this key frame. Keep the old ones if slow.
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
