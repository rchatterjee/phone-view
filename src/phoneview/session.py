"""One browser controller session over a single WebSocket.

Server -> browser
  text   {"type": "status", ...}            every 0.5 s
         {"type": "capabilities", ...}      when they change
         {"type": "video_config", ...}      before the first frame and on any change
         {"type": "input_result" | "settings_result" | "setup_result", ...}
  binary 0x01 flags(u8, bit0 = key) pts(u64 BE) + H.264 Annex-B / HEVC hvcC access unit
         0x02 + JPEG                        (browser must reply {"type": "frame_ack"})

Browser -> server (text only, small): input events (see events.py), frame_ack,
stream_settings, setup.
"""
import asyncio
import json
import struct
import time

from anyio import CancelScope
from starlette.websockets import WebSocketDisconnect

from phoneview.events import RateLimiter, validate_event
from phoneview.streaming import BandwidthPacer

MAX_MESSAGE = 2048
ACK_TIMEOUT = 10
STATUS_INTERVAL = 0.5
SETUP_INTERVAL = 3.0
SETTINGS_INTERVAL = 0.5
MSG_VIDEO = 1
MSG_JPEG = 2


def video_header(key, pts):
    return struct.pack(">BBQ", MSG_VIDEO, 1 if key else 0, pts & 0xFFFFFFFFFFFFFFFF)


async def serve(backend, ws):
    send_lock = asyncio.Lock()
    sub = backend.hub.subscribe()
    pacer = BandwidthPacer()
    limiter = RateLimiter()
    jpeg_sent_at = None
    background = set()

    async def send_json(value):
        async with send_lock:
            await asyncio.wait_for(ws.send_json(value), 5)

    async def send_bytes(data):
        async with send_lock:
            await asyncio.wait_for(ws.send_bytes(data), 5)

    def spawn(coro):
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def run_setup(name):
        try:
            result = await backend.setup_action(name)
            await send_json({"type": "setup_result", "action": name, **(result or {})})
        except ValueError as exc:
            await send_json({"type": "setup_result", "action": name, "error": str(exc)})
        except Exception as exc:
            await send_json({"type": "setup_result", "action": name,
                             "error": f"Setup failed ({type(exc).__name__})"})

    async def receive():
        nonlocal jpeg_sent_at
        last_settings = 0.0
        last_setup = -SETUP_INTERVAL
        last_error = 0.0
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE:
                await ws.close(code=1009)
                return
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            now = time.monotonic()
            if kind == "frame_ack":
                jpeg_sent_at = None
                sub.ack()
                continue
            if kind == "keyframe":
                backend.hub.request_key()   # rate-limited inside the hub
                continue
            if kind == "stream_settings":
                try:
                    if (set(message) != {"type", "generation", "settings"}
                            or message["generation"] != backend.generation
                            or now - last_settings < SETTINGS_INTERVAL):
                        raise ValueError("Stale or rate-limited settings request")
                    last_settings = now
                    await backend.update_settings(message["settings"])
                    await send_json({"type": "settings_result", "capabilities": backend.capabilities()})
                except (ValueError, TypeError) as exc:
                    await send_json({"type": "settings_result", "error": str(exc) or "Invalid settings"})
                continue
            if kind == "setup":
                name = message.get("action")
                if set(message) != {"type", "action"} or not isinstance(name, str) or len(name) > 40:
                    continue
                if now - last_setup < SETUP_INTERVAL:
                    await send_json({"type": "setup_result", "action": name,
                                     "error": "Please wait a few seconds before trying again."})
                    continue
                last_setup = now
                spawn(run_setup(name))
                continue
            # Input events.
            try:
                if message.get("generation") != backend.generation:
                    raise ValueError("Stale device session; input discarded")
                event = validate_event(message)
                ending = event["type"] == "touch" and event["phase"] in {"up", "cancel"}
                if not ending and not limiter.allow(event["type"]):
                    raise ValueError("Rate limited")
                result = await backend.handle_input(event)
            except (ValueError, TypeError) as exc:
                result = {"error": str(exc) or "Invalid input"}
            if result and result.get("error") and now - last_error > 0.5:
                last_error = now
                await send_json({"type": "input_result", **result})

    async def transmit():
        nonlocal jpeg_sent_at
        while True:
            if jpeg_sent_at is not None and time.monotonic() - jpeg_sent_at > ACK_TIMEOUT:
                await ws.close(code=1008, reason="Frame acknowledgement timed out")
                return
            item = await sub.next(timeout=0.5)
            if item is None:
                continue
            if item[0] == "config":
                config = item[1] or {"codec": None}
                await send_json({"type": "video_config", **config})
            elif item[0] == "video":
                _, key, pts, data = item
                await send_bytes(video_header(key, pts) + data)
            elif item[0] == "jpeg":
                _, data, _, _ = item
                budget = backend.jpeg_budget_mbps()
                if budget:
                    delay = pacer.delay(time.monotonic(), budget)
                    if delay > 0:
                        # Wait, but let a newer frame replace this one meanwhile.
                        await asyncio.sleep(delay)
                        if sub.jpeg is not None:
                            data = sub.jpeg[0]
                            sub.jpeg = None
                sent_at = time.monotonic()
                jpeg_sent_at = sent_at
                await send_bytes(bytes([MSG_JPEG]) + data)
                pacer.record(len(data), sent_at)

    async def status():
        last_caps = None
        while True:
            caps = backend.capabilities()
            encoded = json.dumps(caps, sort_keys=True)
            if encoded != last_caps:
                last_caps = encoded
                await send_json({"type": "capabilities", **caps})
            await send_json(backend.full_status())
            backend.state_changed.clear()
            try:
                await asyncio.wait_for(backend.state_changed.wait(), STATUS_INTERVAL)
                await asyncio.sleep(0.05)  # coalesce bursts of changes
            except asyncio.TimeoutError:
                pass

    tasks = []
    try:
        await ws.accept()
        await backend.set_viewer(True)
        tasks = [asyncio.create_task(receive()), asyncio.create_task(transmit()), asyncio.create_task(status())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    finally:
        # ASGI servers may cancel a session as soon as the browser closes. Release
        # held touches and private frames before honouring that cancellation.
        with CancelScope(shield=True):
            for task in tasks + list(background):
                task.cancel()
            await asyncio.gather(*tasks, *background, return_exceptions=True)
            backend.hub.unsubscribe(sub)
            await backend.set_viewer(False)
            try:
                await ws.close()
            except Exception:
                pass
