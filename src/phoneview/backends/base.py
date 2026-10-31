"""The contract every device backend implements.

The WebSocket session only talks to this interface; it never knows whether the
phone is an Android device driven by scrcpy or an iPhone driven by CoreDevice,
screenshotr or WebDriverAgent. Capabilities tell the browser what to show.
"""
import asyncio

from phoneview.media import MediaHub


def capabilities(*, video=None, touch=False, multitouch=False, live_touch=False, keys=(),
                 text=False, scroll=False, commands=(), clipboard=False, setup=(), settings=()):
    """Describe what the current device connection supports.

    video: None, "h264", "hevc" or "jpeg".
    live_touch: True when move events reach the device as they happen; False means
        the backend replays the gesture after release (WebDriverAgent).
    setup: setup actions the browser may offer, e.g. ("developer_mode_check",).
    settings: list of setting descriptors (see `setting`).
    """
    return {
        "video": video, "touch": touch, "multitouch": multitouch, "live_touch": live_touch,
        "keys": sorted(keys), "text": text, "scroll": scroll, "commands": sorted(commands),
        "clipboard": clipboard, "setup": list(setup), "settings": list(settings),
    }


def setting(name, label, value, *, options=None, minimum=None, maximum=None, step=None):
    """A stream setting the browser renders as a select (options) or number input."""
    item = {"name": name, "label": label, "value": value}
    if options is not None:
        item["options"] = [{"value": v, "label": l} for v, l in options]
    else:
        item.update(min=minimum, max=maximum, step=step)
    return item


class Backend:
    platform = "none"

    def __init__(self, hub=None):
        self.hub = hub or MediaHub()
        self.generation = 0
        self.viewer = False
        self.viewer_changed = asyncio.Event()
        self.state_changed = asyncio.Event()
        self.error = None
        self.device = None        # dict: name, model, os, version, serial

    # -- required ---------------------------------------------------------------

    def capabilities(self):
        return capabilities()

    async def run(self):
        """Connect, stream and reconnect until cancelled."""
        raise NotImplementedError

    async def handle_input(self, event):
        """Apply one validated event; return None or an {"error": ...} dict."""
        return {"error": "Control is not available"}

    async def update_settings(self, values):
        raise ValueError("No adjustable settings")

    async def setup_action(self, name):
        raise ValueError("Unknown setup action")

    def jpeg_budget_mbps(self):
        """Average JPEG payload budget, or None for no pacing."""
        return None

    def status(self):
        """Backend-specific status merged into the status message."""
        return {}

    async def close(self):
        pass

    # -- shared helpers ---------------------------------------------------------

    async def set_viewer(self, present):
        self.viewer = present
        self.viewer_changed.set()
        self.notify()

    def notify(self):
        self.state_changed.set()

    def full_status(self):
        out = {
            "type": "status", "platform": self.platform, "generation": self.generation,
            "device": self.device, "error": self.error,
        }
        out.update(self.status())
        return out

    async def wait_viewer_change(self, timeout=None):
        self.viewer_changed.clear()
        try:
            await asyncio.wait_for(self.viewer_changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass
