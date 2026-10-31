"""iOS 17+ input over CoreDevice Universal HID (touch, keyboard) and Indigo HID (buttons).

Touches are forwarded live: CONTACT reports on down/move, RELEASE on up, so the
finger follows the pointer instead of replaying a straight line after release.
Coordinates are the same 0..65535 range the viewer sends.
"""
import asyncio
import contextlib
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager

from pymobiledevice3.remote.core_device.hid_service import (
    ASCII_TO_HID,
    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
    HID_BUTTON_STATE_DOWN,
    HID_BUTTON_STATE_UP,
    KEY_BACKSPACE,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT,
    KEY_LEFT_SHIFT,
    KEY_RIGHT,
    KEY_TAB,
    KEY_UP,
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    IndigoHIDService,
    UniversalHIDServiceService,
)

log = logging.getLogger(__name__)

# (usage page, usage code, hold seconds) for Indigo hardware buttons.
BUTTONS = {
    "home": (0x0C, 0x40, 0.05),
    "power": (0x0C, 0x30, 0.5),
    "volume_up": (0x0C, 0xE9, 0.05),
    "volume_down": (0x0C, 0xEA, 0.05),
    "siri": (0x0C, 0xCF, 1.0),
}
KEYBOARD_KEYS = {
    "enter": KEY_ENTER, "delete": KEY_BACKSPACE, "tab": KEY_TAB, "escape": KEY_ESC,
    "arrow_up": KEY_UP, "arrow_down": KEY_DOWN, "arrow_left": KEY_LEFT, "arrow_right": KEY_RIGHT,
}
HID_KEYS = set(BUTTONS) | {"app_switch"} | set(KEYBOARD_KEYS)
STALE_SECONDS = 0.75


@asynccontextmanager
async def direct_hid(rsd):
    """Match ScreenStreamServer._ensure_hid: UHS then Indigo; no media session."""
    async with AsyncExitStack() as stack:
        uhs = UniversalHIDServiceService(rsd)
        stack.push_async_callback(uhs.close)
        await asyncio.wait_for(uhs.connect(), 5)
        indigo = IndigoHIDService(rsd)
        stack.push_async_callback(indigo.close)
        await asyncio.wait_for(indigo.connect(), 5)
        yield uhs, indigo


def text_usages(text):
    """[(usages held), ...] for each character the HID keyboard can type."""
    out = []
    for char in text:
        if char == "\n":
            out.append([KEY_ENTER])
            continue
        mapped = ASCII_TO_HID.get(char)
        if mapped is None:
            continue
        code, shift = mapped
        out.append([KEY_LEFT_SHIFT, code] if shift else [code])
    return out


class HidController:
    """Serialises input onto the HID services; moves are coalesced."""

    def __init__(self, uhs, indigo):
        self.uhs = uhs
        self.indigo = indigo
        self.queue = asyncio.Queue(256)
        self.touching = None          # (x, y) while a contact is down
        self.keyboard = None
        self.dispatch_ms = 0.0

    def submit(self, event):
        try:
            self.queue.put_nowait((time.monotonic(), event))
        except asyncio.QueueFull:
            return {"error": "Controller busy"}
        return None

    async def run(self):
        try:
            while True:
                queued, event = await self.queue.get()
                # Coalesce consecutive moves: only the newest position matters.
                if event["type"] == "touch" and event["phase"] == "move":
                    while not self.queue.empty():
                        _, nxt = self.queue._queue[0]
                        if nxt["type"] == "touch" and nxt["phase"] == "move":
                            queued, event = self.queue.get_nowait()
                        else:
                            break
                stale = time.monotonic() - queued > STALE_SECONDS
                if stale and not (event["type"] == "touch" and event["phase"] in {"up", "cancel"}):
                    continue
                start = time.monotonic()
                await self._apply(event)
                self.dispatch_ms = (time.monotonic() - start) * 1000
        finally:
            await self.release()

    async def release(self):
        if self.touching is not None:
            x, y = self.touching
            self.touching = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.uhs.send_touchscreen(
                    TOUCHSCREEN_STATE_RELEASE, x, y, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN), 1)

    async def _apply(self, event):
        kind = event["type"]
        if kind == "touch":
            if event["id"] != 0:
                return            # single-contact path (the hardware-verified one)
            x, y = event["x"], event["y"]
            if event["phase"] in {"down", "move"}:
                if event["phase"] == "move" and self.touching is None:
                    return
                self.touching = (x, y)
                await self.uhs.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y,
                                                service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)
            elif self.touching is not None:
                self.touching = None
                await asyncio.wait_for(self.uhs.send_touchscreen(
                    TOUCHSCREEN_STATE_RELEASE, x, y, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN), 1)
        elif kind == "key":
            await self._key(event["key"], event["phase"])
        elif kind == "text":
            await self._type(event["text"])

    async def _press(self, page, code, hold):
        try:
            await self.indigo.send_button(page, code, HID_BUTTON_STATE_DOWN)
            await asyncio.sleep(hold)
        finally:
            await asyncio.wait_for(self.indigo.send_button(page, code, HID_BUTTON_STATE_UP), 1)

    async def _key(self, key, phase):
        if key == "app_switch":
            # Two complete Home presses (user-verified; edge swipe-and-hold was not).
            page, code, hold = BUTTONS["home"]
            await self._press(page, code, hold)
            await asyncio.sleep(0.12)
            await self._press(page, code, hold)
        elif key in BUTTONS:
            page, code, hold = BUTTONS[key]
            if phase == "press":
                await self._press(page, code, hold)
            else:
                await self.indigo.send_button(page, code, HID_BUTTON_STATE_DOWN if phase == "down" else HID_BUTTON_STATE_UP)
        elif key in KEYBOARD_KEYS:
            await self._chord([KEYBOARD_KEYS[key]])

    async def _ensure_keyboard(self):
        if self.keyboard is None:
            self.keyboard = await asyncio.wait_for(self.uhs.create_keyboard_service(), 10)
        return self.keyboard

    async def _chord(self, usages):
        keyboard = await self._ensure_keyboard()
        try:
            await self.uhs.send_keyboard(keyboard, usages)
            await asyncio.sleep(0.02)
        finally:
            await self.uhs.send_keyboard(keyboard, [])

    async def _type(self, text):
        for usages in text_usages(text):
            await self._chord(usages)
