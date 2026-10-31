"""Browser -> server input events: one validated model shared by every backend.

Coordinates are normalised to 0..65535 over the displayed video frame, so the
browser never needs to know the device resolution or orientation.
"""
import math
import time

COORD_MAX = 65535
MAX_POINTERS = 10
TEXT_MAX_CHARS = 300

KEYS = {
    "home", "back", "app_switch", "power", "volume_up", "volume_down", "siri", "menu",
    "enter", "delete", "forward_delete", "tab", "escape", "arrow_up", "arrow_down",
    "arrow_left", "arrow_right", "move_home", "move_end", "page_up", "page_down",
}
KEY_PHASES = {"press", "down", "up"}
TOUCH_PHASES = {"down", "move", "up", "cancel"}
COMMANDS = {
    "expand_notifications", "expand_settings", "collapse_panels", "rotate",
    "screen_off", "screen_on",
}


def _coord(value):
    if type(value) is not int or not 0 <= value <= COORD_MAX:
        raise ValueError("Coordinates must be integers in 0..65535")
    return value


def _exact(message, fields):
    if set(message) != fields:
        raise ValueError("Unexpected or missing fields")


def validate_event(message):
    """Return a normalised event dict (without `generation`) or raise ValueError."""
    if not isinstance(message, dict):
        raise ValueError("Expected an object")
    kind = message.get("type")
    if kind == "touch":
        _exact(message, {"type", "generation", "phase", "id", "x", "y"})
        if message["phase"] not in TOUCH_PHASES:
            raise ValueError("Unknown touch phase")
        if type(message["id"]) is not int or not 0 <= message["id"] < MAX_POINTERS:
            raise ValueError("Pointer id must be 0..9")
        return {"type": "touch", "phase": message["phase"], "id": message["id"],
                "x": _coord(message["x"]), "y": _coord(message["y"])}
    if kind == "key":
        _exact(message, {"type", "generation", "key", "phase"})
        if message["key"] not in KEYS or message["phase"] not in KEY_PHASES:
            raise ValueError("Unknown key or phase")
        return {"type": "key", "key": message["key"], "phase": message["phase"]}
    if kind == "text":
        _exact(message, {"type", "generation", "text"})
        text = message["text"]
        if not isinstance(text, str) or not 0 < len(text) <= TEXT_MAX_CHARS:
            raise ValueError("Text must be 1..300 characters")
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise ValueError("Control characters are not allowed")
        return {"type": "text", "text": text}
    if kind == "scroll":
        _exact(message, {"type", "generation", "x", "y", "dx", "dy"})
        for key in ("dx", "dy"):
            value = message[key]
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 16:
                raise ValueError("Scroll amounts must be finite and within -16..16")
        return {"type": "scroll", "x": _coord(message["x"]), "y": _coord(message["y"]),
                "dx": float(message["dx"]), "dy": float(message["dy"])}
    if kind == "command":
        _exact(message, {"type", "generation", "name"})
        if message["name"] not in COMMANDS:
            raise ValueError("Unknown command")
        return {"type": "command", "name": message["name"]}
    raise ValueError("Unknown event type")


class RateLimiter:
    """Token buckets per event category; live drags need far more than keys do."""

    LIMITS = {  # category: (tokens per second, burst)
        "touch": (240.0, 60.0),
        "scroll": (30.0, 10.0),
        "key": (30.0, 10.0),
        "text": (20.0, 20.0),   # one message per keystroke batch / 300-char paste chunk
        "command": (5.0, 3.0),
    }

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.buckets = {name: [burst, clock()] for name, (_, burst) in self.LIMITS.items()}

    def allow(self, category):
        rate, burst = self.LIMITS[category]
        tokens, last = self.buckets[category]
        now = self.clock()
        tokens = min(burst, tokens + (now - last) * rate)
        if tokens < 1:
            self.buckets[category] = [tokens, now]
            return False
        self.buckets[category] = [tokens - 1, now]
        return True
