"""PhoneView: local browser viewer and controller for USB-connected phones.

Run: python -m phoneview   (then open http://127.0.0.1:8000)
"""
import argparse
import asyncio
import contextlib
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from phoneview import session

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"viewer.js": "text/javascript", "viewer.css": "text/css"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_socket(ws):
    """Require a local peer and same-origin browser; block DNS rebinding/CSWSH."""
    if not ws.client or not is_loopback(ws.client.host):
        return False
    origin = ws.headers.get("origin", "")
    expected = "http://" + ws.headers.get("host", "")
    try:
        parsed = urlsplit(origin)
        return origin == expected and parsed.hostname in LOCAL_HOSTS
    except ValueError:
        return False


def create_app(backend):
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(backend.run())
        try:
            yield
        finally:
            task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                await backend.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.state.controller = None
    app.state.backend = backend

    @app.middleware("http")
    async def local_only(request, call_next):
        if not request.client or not is_loopback(request.client.host):
            return PlainTextResponse("Local access only", status_code=403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; script-src 'self'; "
                                       "style-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        })
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in STATIC_FILES:
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(STATIC / name, media_type=STATIC_FILES[name])

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        if not allowed_socket(ws):
            await ws.close(code=1008, reason="Local same-origin browser required")
            return
        if app.state.controller is not None:
            await ws.close(code=1008, reason="Another browser controls this phone")
            return
        # Claim before the first await: two connections cannot both become controller.
        app.state.controller = ws
        try:
            await session.serve(backend, ws)
        finally:
            app.state.controller = None

    return app


def build_backend(args):
    from phoneview.android.adb import AdbClient
    from phoneview.android.backend import AndroidBackend, AndroidSettings
    from phoneview.backends.auto import AutoBackend, detect_android, detect_ios
    from phoneview.ios.backend import IOSBackend, IOSSettings

    android_settings = AndroidSettings(max_size=args.max_size, max_fps=args.max_fps,
                                       bit_rate_mbps=args.bit_rate, serial=args.serial,
                                       server_path=args.scrcpy_server)
    rsd_address = None
    if args.rsd:
        rsd_address = (args.rsd[0], int(args.rsd[1]))
        if not 1 <= rsd_address[1] <= 65535:
            raise ValueError("RSD port must be in 1..65535")
    ios_settings = IOSSettings(active_fps=args.active_fps, idle_fps=args.idle_fps, serial=args.serial,
                               short_edge=args.width, jpeg_quality=args.quality,
                               bandwidth_mbps=args.bandwidth_mbps, tunnel=args.tunnel,
                               rsd_address=rsd_address, video_mode=args.ios_video,
                               auto_mount_ddi=not args.no_auto_mount)
    adb = AdbClient(adb_path=args.adb)
    factories = {
        "android": lambda hub: AndroidBackend(android_settings, hub, adb),
        "ios": lambda hub: IOSBackend(ios_settings, hub),
    }
    if args.platform == "android":
        return factories["android"](None)
    if args.platform == "ios":
        return factories["ios"](None)
    detectors = {"android": lambda: detect_android(adb), "ios": detect_ios}
    return AutoBackend(factories, detectors=detectors)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="phoneview", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", choices=["auto", "android", "ios"], default="auto",
                        help="Which phone type to look for (default: whichever is plugged in)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--serial", help="adb serial or iPhone UDID to use when several are connected")
    parser.add_argument("-v", "--verbose", action="store_true")

    android = parser.add_argument_group("Android (scrcpy)")
    android.add_argument("--max-size", type=int, default=1280, help="Video long edge in px; 0 = native")
    android.add_argument("--max-fps", type=int, default=30)
    android.add_argument("--bit-rate", type=float, default=4.0, help="Video bit rate in Mbps (0.5..16)")
    android.add_argument("--adb", help="Path to the adb binary (default: from PATH or $ADB)")
    android.add_argument("--scrcpy-server", help="Path to scrcpy-server v5.0.1 (default: download once, SHA-256 checked)")

    ios = parser.add_argument_group("iPhone")
    ios.add_argument("--ios-video", choices=["auto", "stream", "screenshot"], default="auto",
                     help="auto: try the CoreDevice video stream (iOS 27+), fall back to screenshots")
    ios.add_argument("--width", type=int, default=480, choices=[0, 360, 480, 720],
                     help="Screenshot short edge in pixels; 0 passes original bytes")
    ios.add_argument("--quality", type=int, default=65, help="JPEG quality: 30..90")
    ios.add_argument("--active-fps", type=float, default=3)
    ios.add_argument("--idle-fps", type=float, default=0.5)
    ios.add_argument("--bandwidth-mbps", type=float, default=1.5, help="Average image budget: 0.1..10 Mbps")
    ios.add_argument("--no-auto-mount", action="store_true",
                     help="iOS 15-16: do not download/mount the Developer Disk Image automatically")
    connection = ios.add_mutually_exclusive_group()
    connection.add_argument("--tunnel", action="store_true", help="iOS 17+: use the local privileged tunneld")
    connection.add_argument("--rsd", nargs=2, metavar=("HOST", "PORT"), help="iOS 17+: RSD endpoint of a manual tunnel")
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        backend = build_backend(args)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"PhoneView: open http://127.0.0.1:{args.port} in a browser on this machine.")
    # No host flag: remote access needs an authenticated layer this app does not have yet.
    uvicorn.run(create_app(backend), host="127.0.0.1", port=args.port,
                ws_max_size=4096, ws_max_queue=32, ws_per_message_deflate=False,
                access_log=False, timeout_graceful_shutdown=5, log_level="warning")


if __name__ == "__main__":
    main()
