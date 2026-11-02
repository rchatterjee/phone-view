"""PhoneView: local browser viewer and controller for USB-connected phones.

Run: phoneview   (it prints, and opens, http://127.0.0.1:8000/?token=...)
"""
import argparse
import asyncio
import contextlib
import logging
import os
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from phoneview import __version__, session
from phoneview.security import COOKIE, FORBIDDEN_PAGE, Access, AuditLog, is_loopback, viewer_identity

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"viewer.js": "text/javascript", "viewer.css": "text/css"}


def create_app(backend, access, audit=None):
    audit = audit or AuditLog()

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
    app.state.controller = None
    app.state.backend = backend
    app.state.access = access

    @app.middleware("http")
    async def guard(request, call_next):
        # Only this machine connects: browsers on it, or the tailscale serve proxy.
        if not request.client or not is_loopback(request.client.host):
            return PlainTextResponse("Local access only", status_code=403)
        if request.url.path.startswith("/static/"):
            response = await call_next(request)          # no device or client data
        elif access.token_matches(request.query_params.get("token")):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, access.new_session(), httponly=True, samesite="strict",
                                secure=access.cookie_secure(request.headers.get("host")), path="/")
        elif not access.valid_session(request.cookies.get(COOKIE)):
            response = HTMLResponse(FORBIDDEN_PAGE, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; script-src 'self'; "
                                       "style-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        })
        return response

    # Added last so it runs first: only our own host names reach the app (blocks DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=access.allowed_hosts)

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
        peer = ws.client.host if ws.client else ""
        if not is_loopback(peer) or not access.origin_allowed(ws.headers.get("origin", "")):
            await ws.close(code=1008, reason="Same-origin browser required")
            return
        if not access.valid_session(ws.cookies.get(COOKIE)):
            await ws.close(code=1008, reason="Not signed in: open the PhoneView link again")
            return
        if app.state.controller is not None:
            await ws.close(code=1008, reason="Another browser controls this phone")
            return
        # Claim before the first await: two connections cannot both become controller.
        app.state.controller = ws
        viewer = viewer_identity(ws.headers, peer)
        device = (backend.full_status().get("device") or {}).get("name")
        audit.write("view_start", viewer=viewer, device=device)
        started = asyncio.get_running_loop().time()
        try:
            await session.serve(backend, ws, viewer=viewer)
        finally:
            app.state.controller = None
            audit.write("view_end", viewer=viewer, device=device,
                        seconds=round(asyncio.get_running_loop().time() - started))

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
    parser.add_argument("--public-origin", action="append", default=[], metavar="URL",
                        help="Origin of a reverse proxy on this machine, e.g. the tailscale serve URL "
                             "https://laptop.tailnet.ts.net:8443 (repeatable)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser at startup")
    parser.add_argument("--audit-log", metavar="FILE", help="Append view start/end records (JSON lines)")
    parser.add_argument("--exit-on-stdin-eof", action="store_true",
                        help="Exit when stdin closes (used when another program, such as ISDi, launches PhoneView)")
    parser.add_argument("--serial", help="adb serial or iPhone UDID to use when several are connected")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"phoneview {__version__}")

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


def watch_stdin(server):
    """Stop when the launching program goes away, however it ended (its end of the pipe closes)."""
    def wait():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        logging.getLogger(__name__).info("Parent closed stdin; shutting down")
        server.should_exit = True
        fallback = threading.Timer(10, os._exit, args=(0,))   # if a clean shutdown hangs
        fallback.daemon = True
        fallback.start()
    threading.Thread(target=wait, name="stdin-watch", daemon=True).start()


def main(argv=None):
    parser, args = parse_args(argv)
    token = os.environ.get("PHONEVIEW_TOKEN") or Access.generate_token()
    try:
        backend = build_backend(args)
        access = Access(token, args.port, args.public_origin)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    link = f"http://127.0.0.1:{args.port}/?token={token}"
    if "PHONEVIEW_TOKEN" not in os.environ:
        print(f"PhoneView: open {link}")
    for origin in args.public_origin:
        print(f"PhoneView: also reachable through the proxy at {origin} (append ?token=...)")
    # Always loopback: remote viewers come through a local proxy such as tailscale serve.
    config = uvicorn.Config(create_app(backend, access, AuditLog(args.audit_log)), host="127.0.0.1",
                            port=args.port, ws_max_size=4096, ws_max_queue=32,
                            ws_per_message_deflate=False, access_log=False,
                            timeout_graceful_shutdown=5, log_level="warning")
    server = uvicorn.Server(config)
    if args.exit_on_stdin_eof:
        watch_stdin(server)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(link,)).start()
    server.run()


if __name__ == "__main__":
    main()
