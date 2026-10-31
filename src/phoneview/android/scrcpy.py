"""scrcpy v5.0.1 wire protocol: control messages, stream headers, and the pinned server jar.

The scrcpy protocol is internal and changes between releases; the server jar and
this module must match exactly. Sources (tag v5.0.1): doc/develop.md,
server/.../control/ControlMessageReader.java, server/.../device/Streamer.java,
server/.../device/DesktopConnection.java, app/src/control_msg.c.

Stream layout in tunnel_forward mode (video + control, no audio):
  video socket: dummy byte (0x00), device name (64 bytes, NUL padded),
                codec id (u32 BE), then 12-byte headers each followed by a payload.
  control socket: client -> device control messages; device -> client messages
                  (clipboard, acks, UHID output) which we drain and ignore.
"""
import hashlib
import os
import struct
import sys
import tempfile
import urllib.request
from pathlib import Path

VERSION = "5.0.1"
SERVER_URL = f"https://github.com/Genymobile/scrcpy/releases/download/v{VERSION}/scrcpy-server-v{VERSION}"
SERVER_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
# Separate from scrcpy's own path so a concurrently running scrcpy is not disturbed.
REMOTE_PATH = "/data/local/tmp/phoneview-scrcpy-server.jar"

CODEC_H264 = 0x68323634  # "h264"
CODEC_DISABLED = 0       # stream explicitly disabled by the device
CODEC_ERROR = 1          # configuration error on the device
DEVICE_NAME_LENGTH = 64
HEADER_LENGTH = 12
MAX_PACKET_SIZE = 16 << 20

PACKET_FLAG_SESSION = 1 << 63
PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61
PTS_MASK = PACKET_FLAG_KEY_FRAME - 1

TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_EXPAND_SETTINGS_PANEL = 6
TYPE_COLLAPSE_PANELS = 7
TYPE_SET_CLIPBOARD = 9
TYPE_SET_DISPLAY_POWER = 10
TYPE_ROTATE_DEVICE = 11
TYPE_RESET_VIDEO = 17

INJECT_TEXT_MAX_LENGTH = 300

# android.view.KeyEvent / MotionEvent action codes.
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# Any pointer id other than -1 (mouse) is injected as a finger on the touchscreen.
POINTER_ID_GENERIC_FINGER = -2

# android.view.KeyEvent keycodes for the logical keys the viewer can send.
KEYCODES = {
    "home": 3, "back": 4, "app_switch": 187, "power": 26, "volume_up": 24,
    "volume_down": 25, "menu": 82, "enter": 66, "delete": 67, "forward_delete": 112,
    "tab": 61, "escape": 111, "arrow_up": 19, "arrow_down": 20, "arrow_left": 21,
    "arrow_right": 22, "move_home": 122, "move_end": 123, "page_up": 92, "page_down": 93,
}

# Commands without payload, plus display power.
SIMPLE_COMMANDS = {
    "expand_notifications": TYPE_EXPAND_NOTIFICATION_PANEL,
    "expand_settings": TYPE_EXPAND_SETTINGS_PANEL,
    "collapse_panels": TYPE_COLLAPSE_PANELS,
    "rotate": TYPE_ROTATE_DEVICE,
    "reset_video": TYPE_RESET_VIDEO,
}


def inject_touch(action, x, y, width, height, pressure, pointer_id=POINTER_ID_GENERIC_FINGER):
    """32 bytes: type, action, pointer id, position (x, y, w, h), u16 pressure, action button, buttons."""
    pressure = 0xFFFF if pressure >= 1 else max(0, int(pressure * 0x10000))
    return struct.pack(">BBqiiHHHii", TYPE_INJECT_TOUCH_EVENT, action, pointer_id,
                       x, y, width, height, pressure, 0, 0)


def _i16_fixed(value):
    """scrcpy encodes scroll in [-16, 16] as i16 fixed point over [-1, 1]."""
    norm = max(-1.0, min(1.0, value / 16))
    return max(-0x8000, min(0x7FFF, int(norm * 0x8000)))


def inject_scroll(x, y, width, height, hscroll, vscroll):
    """21 bytes: type, position (x, y, w, h), i16 hscroll, i16 vscroll, buttons."""
    return struct.pack(">BiiHHhhi", TYPE_INJECT_SCROLL_EVENT, x, y, width, height,
                       _i16_fixed(hscroll), _i16_fixed(vscroll), 0)


def inject_keycode(action, keycode, repeat=0, meta_state=0):
    """14 bytes: type, action, keycode, repeat, meta state."""
    return struct.pack(">BBiii", TYPE_INJECT_KEYCODE, action, keycode, repeat, meta_state)


def inject_text(text):
    """type, u32 length, UTF-8 text (at most 300 bytes)."""
    raw = text.encode("utf-8")
    if len(raw) > INJECT_TEXT_MAX_LENGTH:
        raise ValueError("Text too long")
    return struct.pack(">BI", TYPE_INJECT_TEXT, len(raw)) + raw


def set_clipboard(text, paste, sequence=0):
    """type, u64 sequence, u8 paste flag, u32 length, UTF-8 text."""
    raw = text.encode("utf-8")
    return struct.pack(">BQBI", TYPE_SET_CLIPBOARD, sequence, 1 if paste else 0, len(raw)) + raw


def back_or_screen_on(action):
    """Back, or wake the screen if it is off."""
    return struct.pack(">BB", TYPE_BACK_OR_SCREEN_ON, action)


def set_display_power(on):
    return struct.pack(">BB", TYPE_SET_DISPLAY_POWER, 1 if on else 0)


def simple_command(name):
    return struct.pack(">B", SIMPLE_COMMANDS[name])


def parse_header(header):
    """Return ("session", width, height) or ("media", config, key_frame, pts, size)."""
    first, = struct.unpack(">Q", header[:8])
    if first & PACKET_FLAG_SESSION:
        width, height = struct.unpack(">II", header[4:])
        return ("session", width, height)
    size, = struct.unpack(">I", header[8:])
    return ("media", bool(first & PACKET_FLAG_CONFIG), bool(first & PACKET_FLAG_KEY_FRAME),
            first & PTS_MASK, size)


def server_args(scid, max_size, max_fps, bit_rate):
    """Arguments after `app_process / com.genymobile.scrcpy.Server`; the first must equal VERSION.

    Keep this short: some Samsung firmware aborts the encoder ("stack corruption
    detected (-fstack-protector)") when the app_process command line reaches 255
    characters (scrcpy issue #2841). So options already at their scrcpy 5.0.1
    defaults are omitted: video_codec=h264, control, cleanup, power_on and
    downsize_on_error are all true/h264 by default.
    """
    args = [VERSION, f"scid={scid:08x}", "log_level=warn", "tunnel_forward=true", "audio=false"]
    if max_size:
        args.append(f"max_size={max_size}")
    args += [
        f"max_fps={max_fps}", f"video_bit_rate={bit_rate}",
        # Short key-frame interval so a dropped packet heals within ~2 s.
        "video_codec_options=i-frame-interval:int=2",
        # No clipboard sync: phone clipboard contents never leave the device.
        "clipboard_autosync=false",
    ]
    return args


def server_command(scid, max_size, max_fps, bit_rate):
    args = " ".join(server_args(scid, max_size, max_fps, bit_rate))
    return f"CLASSPATH={REMOTE_PATH} app_process / com.genymobile.scrcpy.Server {args}"


def cache_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "phoneview"


def ensure_server(path=None):
    """Return a local scrcpy-server path whose SHA-256 matches the pinned release.

    Downloads it once into the cache directory when no path is given.
    """
    target = Path(path) if path else cache_dir() / f"scrcpy-server-v{VERSION}"
    if target.is_file() and _sha256(target) == SERVER_SHA256:
        return target
    if path:
        raise RuntimeError(f"{target} is not scrcpy-server v{VERSION} (SHA-256 mismatch or missing)")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SERVER_URL, timeout=30) as response:
        data = response.read(8 << 20)
    if hashlib.sha256(data).hexdigest() != SERVER_SHA256:
        raise RuntimeError("Downloaded scrcpy-server does not match the pinned SHA-256")
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".scrcpy-server-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- H.264 helpers -----------------------------------------------------------

def split_annexb(data):
    """Split an Annex-B byte stream into NAL units (without start codes)."""
    nals, i, n = [], 0, len(data)
    starts = []
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and (data[i + 2] == 1 or (i + 4 <= n and data[i + 2] == 0 and data[i + 3] == 1)):
            length = 3 if data[i + 2] == 1 else 4
            starts.append((i, i + length))
            i += length
        else:
            i += 1
    for k, (_, begin) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else n
        if end > begin:
            nals.append(data[begin:end])
    return nals


def h264_codec_string(config):
    """WebCodecs codec string ("avc1.PPCCLL") from the SPS in a config packet."""
    for nal in split_annexb(config):
        if nal and nal[0] & 0x1F == 7 and len(nal) >= 4:
            return "avc1.%02x%02x%02x" % (nal[1], nal[2], nal[3])
    return None
