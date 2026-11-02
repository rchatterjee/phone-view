"""Read-only live check against a running PhoneView with a phone attached.

Never saves frames or sends input. Run while `python -m phoneview` is serving:
    PHONEVIEW_TOKEN=<token from the startup link> python tools/smoke_test.py --port 8000
"""
import argparse
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

import websockets


async def check(port, seconds):
    base = f"http://127.0.0.1:{port}"
    token = os.environ.get("PHONEVIEW_TOKEN")
    assert token, "Set PHONEVIEW_TOKEN to the token from the PhoneView startup link"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(f"{base}/?token={token}", timeout=5)
        raise AssertionError("Expected a redirect after signing in")
    except urllib.error.HTTPError as exc:
        assert exc.code == 303, f"Sign-in failed (HTTP {exc.code}); is the token right?"
        cookie = exc.headers["Set-Cookie"].split(";", 1)[0]
    video = jpeg = keys = 0
    status, caps, config = {}, {}, None
    deadline = time.monotonic() + seconds
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=base, max_size=16 << 20,
                                  additional_headers={"Cookie": cookie}) as ws:
        while time.monotonic() < deadline:
            message = await asyncio.wait_for(ws.recv(), 5)
            if isinstance(message, bytes):
                if message[0] == 1:
                    video += 1
                    keys += message[1] & 1
                elif message[0] == 2:
                    assert message[1:4] == b"\xff\xd8\xff", "Expected JPEG"
                    jpeg += 1
                    await ws.send(json.dumps({"type": "frame_ack"}))
                continue
            data = json.loads(message)
            if data["type"] == "status":
                status = data
            elif data["type"] == "capabilities":
                caps = data
            elif data["type"] == "video_config":
                config = data
    device = status.get("device") or {}
    print(f"Device: {device.get('name')} ({device.get('os')} {device.get('version')}); platform {status.get('platform')}"
          f"{'; mode ' + status['mode'] if status.get('mode') else ''}")
    if status.get("error"):
        print("Error:", status["error"])
    if status.get("note"):
        print("Note:", status["note"])
    print(f"Capabilities: video={caps.get('video')} touch={caps.get('touch')} live={caps.get('live_touch')} "
          f"text={caps.get('text')} keys={caps.get('keys')}")
    if config and config.get("codec"):
        print(f"Video config: {config.get('codec_string')} {config.get('width')}x{config.get('height')}")
    print(f"Received in {seconds}s: {video} video packets ({keys} key frames), {jpeg} JPEG frames")
    assert video or jpeg, "No frames received"
    print("PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seconds", type=float, default=8)
    args = parser.parse_args()
    asyncio.run(check(args.port, args.seconds))
