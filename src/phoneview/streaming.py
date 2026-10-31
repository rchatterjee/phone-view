"""In-memory screenshot conversion and bounded image-bandwidth pacing."""
from dataclasses import dataclass
from io import BytesIO

from PIL import Image


@dataclass(frozen=True)
class EncodedFrame:
    data: bytes
    width: int
    height: int


def encode_frame(data: bytes, short_edge: int, quality: int) -> EncodedFrame:
    """Preserve aspect ratio and never upscale. Zero keeps original compressed bytes."""
    with Image.open(BytesIO(data)) as image:
        if short_edge == 0:
            return EncodedFrame(data, *image.size)
        scale = min(1, short_edge / min(image.size))
        size = tuple(max(1, round(dimension * scale)) for dimension in image.size)
        image.thumbnail(size, Image.Resampling.BILINEAR, reducing_gap=2.0)
        rgb = image.convert("RGB")
        try:
            with BytesIO() as output:
                rgb.save(output, format="JPEG", quality=quality, subsampling=2,
                         optimize=False, progressive=False)
                return EncodedFrame(output.getvalue(), *rgb.size)
        finally:
            rgb.close()


class BandwidthPacer:
    """Allow one initial frame, then space sends by the preceding frame's byte cost.

    This bounds average image payload, with one frame of burst allowance. It is
    not a packet-level shaper and excludes HTTP/WebSocket/TCP/VPN overhead.
    """
    def __init__(self):
        self.last_send = None
        self.last_size = 0

    def delay(self, now, mbps):
        if self.last_send is None:
            return 0.0
        return max(0.0, self.last_send + self.last_size * 8 / (mbps * 1_000_000) - now)

    def record(self, size, now):
        self.last_send = now
        self.last_size = size
