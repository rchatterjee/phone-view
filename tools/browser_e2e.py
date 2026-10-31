"""End-to-end check of the real viewer page in headless Chromium, without a phone.

A fake backend streams an ffmpeg-generated H.264 clip (as scrcpy would) and then
JPEG frames (as the iPhone screenshot path would). The script verifies that the
page decodes and draws both, and that clicks, drags and typing reach the backend
as validated events.

Needs: ffmpeg, `pip install playwright`, and a Chromium/Chrome with H.264
(set CHROME=/path/to/chrome, or let Playwright find its own browser).
Run: python tools/browser_e2e.py
"""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path

import uvicorn
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phoneview.android.scrcpy import h264_codec_string, split_annexb  # noqa: E402
from phoneview.backends.base import Backend, capabilities  # noqa: E402
from phoneview.server import create_app  # noqa: E402

W, H = 360, 640


def make_clip(w=W, h=H):
    out = Path(tempfile.mkdtemp()) / "clip.h264"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30",
                    "-t", "3", "-c:v", "libx264", "-profile:v", "baseline", "-g", "30", "-bf", "0",
                    "-x264-params", "aud=1", "-pix_fmt", "yuv420p", "-f", "h264", str(out)], check=True)
    data = out.read_bytes()
    units, current = [], []
    for nal in split_annexb(data):
        if nal[0] & 0x1F == 9 and current:          # access unit delimiter starts a new frame
            units.append(current)
            current = []
        current.append(nal)
    units.append(current)
    frames = []
    for nals in units:
        payload = b"".join(b"\x00\x00\x00\x01" + n for n in nals)
        frames.append((any(n[0] & 0x1F == 5 for n in nals), payload))
    return frames


class FakeBackend(Backend):
    platform = "android"

    def __init__(self, frames, rotated):
        super().__init__()
        self.frames = frames
        self.rotated = rotated
        self.events = []
        self.mode = "h264"
        self.generation = 1
        self.device = {"name": "Fake phone", "os": "Android", "version": "15"}

    def capabilities(self):
        return capabilities(video=self.mode, touch=True, multitouch=True, live_touch=True,
                            keys={"home", "back", "enter"}, text=True, scroll=True)

    async def handle_input(self, event):
        self.events.append(event)

    async def run(self):
        while True:
            if not self.viewer:
                await asyncio.sleep(0.05)
                continue
            if self.mode in ("h264", "rotated"):
                mode = self.mode
                frames = self.frames if mode == "h264" else self.rotated
                key, first = frames[0]
                # Config then key frame back to back, as scrcpy does after a rotation.
                self.hub.set_config({"codec": "h264", "codec_string": h264_codec_string(first),
                                     "width": W, "height": H, "format": "annexb"})
                pts = 0
                while self.mode == mode and self.viewer:
                    for key, data in frames:
                        if self.mode != mode:
                            break
                        self.hub.publish_video(data, key, pts)
                        pts += 33333
                        await asyncio.sleep(1 / 30)
            else:
                colour = 0
                while self.mode == "jpeg":
                    buf = BytesIO()
                    Image.new("RGB", (300, 500), (colour % 255, 80, 160)).save(buf, "JPEG", quality=70)
                    self.hub.publish_jpeg(buf.getvalue(), 300, 500)
                    colour += 40
                    await asyncio.sleep(0.1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def browser_checks(port, backend):
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get("CHROME") or None)
        # Playwright's probes use eval(); the page's own CSP forbids that (as it should).
        context = await browser.new_context(viewport={"width": 1100, "height": 900}, bypass_csp=True)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"http://127.0.0.1:{port}/")
        await page.wait_for_function("document.getElementById('screen').width === 360", timeout=15000)
        results["h264_supported"] = await page.evaluate(
            "VideoDecoder.isConfigSupported({codec: 'avc1.42c01e'}).then(r => r.supported)")
        await page.wait_for_timeout(1000)
        results["h264_center_pixel"] = await page.evaluate("""() => {
            const c = document.getElementById('screen');
            return Array.from(c.getContext('2d').getImageData(c.width / 2, c.height / 3, 1, 1).data);
        }""")
        results["fps_text"] = await page.text_content("#metrics")
        results["placeholder_visible"] = await page.is_visible("#placeholder")
        results["buttons"] = await page.eval_on_selector_all("#nav button", "els => els.map(e => e.textContent)")

        box = await page.locator("#screen").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await page.mouse.click(cx, cy)
        await page.mouse.move(cx, cy + 100)
        await page.mouse.down()
        await page.mouse.move(cx, cy - 100, steps=8)
        await page.mouse.up()
        await page.keyboard.type("hi")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(400)

        # Rotation: the first key frame right after the new config must be decoded
        # (the next one is a full second later in this clip).
        start = time.monotonic()
        backend.mode = "rotated"
        await page.wait_for_function("document.getElementById('screen').width === 640", timeout=10000)
        results["rotation_ms"] = (time.monotonic() - start) * 1000

        backend.mode = "jpeg"
        await page.wait_for_function("document.getElementById('screen').width === 300", timeout=10000)
        await page.wait_for_timeout(800)
        results["jpeg_pixel"] = await page.evaluate("""() => {
            const c = document.getElementById('screen');
            return Array.from(c.getContext('2d').getImageData(150, 250, 1, 1).data);
        }""")
        results["page_errors"] = errors
        await browser.close()
    return results


def main():
    backend = FakeBackend(make_clip(), make_clip(640, 360))
    port = free_port()
    config = uvicorn.Config(create_app(backend), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        results = asyncio.run(browser_checks(port, backend))
    finally:
        server.should_exit = True
        thread.join(10)

    events = backend.events
    kinds = [(e["type"], e.get("phase") or e.get("key") or e.get("text")) for e in events]
    touches = [e for e in events if e["type"] == "touch"]
    checks = {
        "H.264 decoded and drawn (non-black pixel)": sum(results["h264_center_pixel"][:3]) > 30,
        "Waiting placeholder hidden once video shows": not results["placeholder_visible"],
        "Nav buttons built from capabilities": results["buttons"] == ["Back", "Home"],
        "Click -> touch down/up near centre": touches[:2] and touches[0]["phase"] == "down"
            and abs(touches[0]["x"] - 32768) < 700 and touches[1]["phase"] == "up",
        "Drag streamed live (move events)": sum(1 for e in touches if e["phase"] == "move") >= 3,
        "Typing -> text event": ("text", "hi") in kinds,
        "Enter -> key event": ("key", "press") in kinds and any(e.get("key") == "enter" for e in events),
        "Rotation: first key frame after config decoded (<600 ms)": results["rotation_ms"] < 600,
        "JPEG fallback drawn with acks": results["jpeg_pixel"][1] in range(70, 91),
        "No page errors": not results["page_errors"],
    }
    print(f"browser H.264 support: {results['h264_supported']}; metrics: {results['fps_text']}")
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    if results["page_errors"]:
        print("page errors:", results["page_errors"])
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
