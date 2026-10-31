"""Fan-out of device media to browser sessions.

Two delivery models share one subscriber type:

* Video (H.264 / HEVC access units): every packet matters because P-frames depend
  on earlier ones, so packets are queued in order. A subscriber that falls behind
  has its queue flushed and waits for the next key frame; the backend is asked to
  produce one immediately.
* JPEG: only the newest image matters. Each subscriber holds one slot that a newer
  frame overwrites, and the browser must acknowledge a frame before the next is
  sent (the existing slow-decoder protection).
"""
import asyncio
import time
from collections import deque

VIDEO_QUEUE_LIMIT = 90          # ~1.5 s at 60 fps, 3 s at 30 fps
KEYFRAME_REQUEST_INTERVAL = 0.5  # do not flood the encoder with requests


class Subscriber:
    def __init__(self, hub):
        self.hub = hub
        self.queue = deque()
        self.needs_key = True
        self.jpeg = None          # (data, width, height, number)
        self.acked = True
        self.event = asyncio.Event()
        self.dropped = 0

    def ack(self):
        self.acked = True
        self.event.set()

    def _push(self, item):
        self.queue.append(item)
        self.event.set()

    async def next(self, timeout=None):
        """Return the next item: ("config", dict) | ("video", key, pts, data) | ("jpeg", data, w, h)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.queue:
                return self.queue.popleft()
            if self.jpeg is not None and self.acked:
                item, self.jpeg = self.jpeg, None
                self.acked = False
                return ("jpeg", item[0], item[1], item[2])
            self.event.clear()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self.event.wait(), remaining)
            except asyncio.TimeoutError:
                return None


class MediaHub:
    def __init__(self):
        self.subscribers = set()
        self.config = None              # current video config dict, or None for JPEG/no media
        self.latest_jpeg = None
        self.request_keyframe = None    # backend callback, may be None
        self._last_key_request = 0.0
        self.frames_published = 0
        self.bytes_published = 0

    # -- subscriptions --------------------------------------------------------

    def subscribe(self):
        sub = Subscriber(self)
        self.subscribers.add(sub)
        if self.config is not None:
            sub._push(("config", dict(self.config)))
            self.request_key()
        elif self.latest_jpeg is not None:
            sub.jpeg = self.latest_jpeg
            sub.event.set()
        return sub

    def unsubscribe(self, sub):
        self.subscribers.discard(sub)

    def request_key(self):
        now = time.monotonic()
        if self.request_keyframe is not None and now - self._last_key_request >= KEYFRAME_REQUEST_INTERVAL:
            self._last_key_request = now
            try:
                self.request_keyframe()
            except Exception:
                pass

    # -- publishing -----------------------------------------------------------

    def set_config(self, config, force=False):
        """New video configuration (codec, size, description); decoders must restart."""
        if not force and config is not None and config == self.config:
            return   # e.g. an encoder reset that kept the same parameters: no decoder restart
        self.config = dict(config) if config is not None else None
        self.latest_jpeg = None
        for sub in self.subscribers:
            sub.queue.clear()
            sub.jpeg = None
            sub.needs_key = True
            if config is not None:
                sub._push(("config", dict(config)))
            else:
                sub._push(("config", None))

    def publish_video(self, data, key, pts):
        self.frames_published += 1
        self.bytes_published += len(data)
        for sub in self.subscribers:
            if sub.needs_key:
                if not key:
                    continue
                sub.needs_key = False
            if len(sub.queue) >= VIDEO_QUEUE_LIMIT:
                # Behind: keep any pending config, drop media, resync on a key frame.
                sub.dropped += 1
                configs = [item for item in sub.queue if item[0] == "config"]
                sub.queue.clear()
                sub.queue.extend(configs[-1:])
                sub.needs_key = True
                self.request_key()
                if not key:
                    continue
                sub.needs_key = False
            sub._push(("video", key, pts, data))

    def publish_jpeg(self, data, width, height):
        if self.config is not None:
            self.set_config(None)
        self.frames_published += 1
        self.bytes_published += len(data)
        self.latest_jpeg = (data, width, height)
        for sub in self.subscribers:
            sub.jpeg = self.latest_jpeg
            sub.event.set()

    def clear(self):
        """Device gone: drop retained frames so no stale screen is shown."""
        self.latest_jpeg = None
        if self.config is not None:
            self.set_config(None)
        for sub in self.subscribers:
            sub.jpeg = None
            sub.queue.clear()
            sub._push(("config", None))
