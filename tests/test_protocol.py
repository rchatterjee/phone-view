"""Byte-exact checks of the scrcpy v5.0.1 control/stream formats and shared models."""
import struct
import time
import unittest

from phoneview.android import scrcpy
from phoneview.android.adb import parse_devices
from phoneview.events import RateLimiter, validate_event
from phoneview.media import VIDEO_QUEUE_LIMIT, MediaHub


class ScrcpyMessages(unittest.TestCase):
    # Layouts follow ControlMessageReader.java (tag v5.0.1).

    def test_touch_is_32_bytes_with_pointer_and_size(self):
        msg = scrcpy.inject_touch(scrcpy.ACTION_DOWN, 10, 20, 1080, 2400, 1.0, pointer_id=3)
        self.assertEqual(len(msg), 32)
        kind, action, pid, x, y, w, h, pressure, action_button, buttons = struct.unpack(">BBqiiHHHii", msg)
        self.assertEqual((kind, action, pid, x, y, w, h), (2, 0, 3, 10, 20, 1080, 2400))
        self.assertEqual(pressure, 0xFFFF)
        self.assertEqual((action_button, buttons), (0, 0))

    def test_scroll_fixed_point(self):
        msg = scrcpy.inject_scroll(5, 6, 100, 200, 0.0, 16.0)
        self.assertEqual(len(msg), 21)
        kind, x, y, w, h, hs, vs, buttons = struct.unpack(">BiiHHhhi", msg)
        self.assertEqual((kind, x, y, w, h, hs, vs, buttons), (3, 5, 6, 100, 200, 0, 0x7FFF, 0))
        _, _, _, _, _, _, vs, _ = struct.unpack(">BiiHHhhi", scrcpy.inject_scroll(0, 0, 1, 1, 0, -1.0))
        self.assertEqual(vs, -0x800)   # -1/16 of full range

    def test_keycode_text_clipboard_and_simple(self):
        self.assertEqual(scrcpy.inject_keycode(1, 3), struct.pack(">BBiii", 0, 1, 3, 0, 0))
        self.assertEqual(scrcpy.inject_text("hi"), b"\x01\x00\x00\x00\x02hi")
        with self.assertRaises(ValueError):
            scrcpy.inject_text("x" * 301)
        clip = scrcpy.set_clipboard("é", paste=True)
        self.assertEqual(clip, struct.pack(">BQBI", 9, 0, 1, 2) + "é".encode())
        self.assertEqual(scrcpy.simple_command("reset_video"), b"\x11")
        self.assertEqual(scrcpy.simple_command("rotate"), b"\x0b")
        self.assertEqual(scrcpy.set_display_power(False), b"\x0a\x00")
        self.assertEqual(scrcpy.back_or_screen_on(0), b"\x04\x00")

    def test_headers(self):
        session = struct.pack(">III", 0x80000000, 1080, 2340)
        self.assertEqual(scrcpy.parse_header(session), ("session", 1080, 2340))
        config = struct.pack(">QI", scrcpy.PACKET_FLAG_CONFIG, 25)
        self.assertEqual(scrcpy.parse_header(config), ("media", True, False, 0, 25))
        key = struct.pack(">QI", scrcpy.PACKET_FLAG_KEY_FRAME | 1234, 999)
        self.assertEqual(scrcpy.parse_header(key), ("media", False, True, 1234, 999))

    def test_h264_codec_string_from_sps(self):
        config = b"\x00\x00\x00\x01\x67\x42\xc0\x1f\xaa\x00\x00\x00\x01\x68\xce"
        self.assertEqual(scrcpy.h264_codec_string(config), "avc1.42c01f")
        self.assertEqual(scrcpy.split_annexb(config), [b"\x67\x42\xc0\x1f\xaa", b"\x68\xce"])
        self.assertIsNone(scrcpy.h264_codec_string(b"\x00\x00\x01\x68\xce"))

    def test_server_args_start_with_version(self):
        args = scrcpy.server_args(0x1234, 1280, 30, 4_000_000)
        self.assertEqual(args[0], scrcpy.VERSION)
        self.assertIn("scid=00001234", args)
        self.assertIn("tunnel_forward=true", args)
        self.assertIn("clipboard_autosync=false", args)
        self.assertIn("audio=false", args)

    def test_command_stays_under_samsung_limit(self):
        # Samsung stagefright aborts at >= 255 chars counted from app_process (scrcpy #2841).
        command = scrcpy.server_command(0x7FFFFFFF, 1920, 60, 16_000_000)
        self.assertLess(len(command[command.index("app_process"):]), 230)
        self.assertNotIn("max_size", scrcpy.server_command(1, 0, 30, 4_000_000))


class AdbParsing(unittest.TestCase):
    def test_devices_l(self):
        text = ("R58M123 device usb:1-1 product:beyond model:SM_G973F device:beyond transport_id:3\n"
                "ABC unauthorized usb:1-2 transport_id:4\n"
                "XYZ no permissions (missing udev rules? user is in the plugdev group); see [http://x] usb:1-3\n"
                "192.168.1.5:5555 device product:p model:m device:d transport_id:5\n")
        devices = parse_devices(text)
        self.assertEqual([d.serial for d in devices], ["R58M123", "ABC", "XYZ", "192.168.1.5:5555"])
        self.assertEqual(devices[0].state, "device")
        self.assertEqual(devices[0].info["model"], "SM_G973F")
        self.assertEqual(devices[1].state, "unauthorized")
        self.assertTrue(devices[2].state.startswith("no permissions"))
        self.assertTrue(devices[0].usb)
        self.assertFalse(devices[3].usb)
        # Windows adb prints no usb:<bus> field for USB phones.
        windows = parse_devices("58080DLCR00481 device product:p model:m device:d transport_id:1\n")
        self.assertTrue(windows[0].usb)
        wireless = parse_devices("adb-R58M-abc._adb-tls-connect._tcp device product:p model:m transport_id:6\n")
        self.assertFalse(wireless[0].usb)


class Events(unittest.TestCase):
    def test_touch_and_key(self):
        event = validate_event({"type": "touch", "generation": 1, "phase": "move", "id": 2, "x": 0, "y": 65535})
        self.assertEqual(event, {"type": "touch", "phase": "move", "id": 2, "x": 0, "y": 65535})
        for bad in ({"type": "touch", "generation": 1, "phase": "move", "id": 2, "x": -1, "y": 0},
                    {"type": "touch", "generation": 1, "phase": "hover", "id": 0, "x": 0, "y": 0},
                    {"type": "touch", "generation": 1, "phase": "up", "id": 10, "x": 0, "y": 0},
                    {"type": "touch", "generation": 1, "phase": "up", "id": 0, "x": 1.5, "y": 0},
                    {"type": "key", "generation": 1, "key": "reboot", "phase": "press"},
                    {"type": "text", "generation": 1, "text": "x" * 301},
                    {"type": "text", "generation": 1, "text": "a\x00"},
                    {"type": "scroll", "generation": 1, "x": 0, "y": 0, "dx": float("nan"), "dy": 0},
                    {"type": "command", "generation": 1, "name": "reboot"},
                    {"type": "touch", "generation": 1, "phase": "up", "id": 0, "x": 0, "y": 0, "extra": 1}):
            with self.assertRaises(ValueError, msg=bad):
                validate_event(bad)

    def test_rate_limiter_buckets(self):
        clock = [0.0]
        limiter = RateLimiter(clock=lambda: clock[0])
        self.assertEqual(sum(limiter.allow("command") for _ in range(10)), 3)
        clock[0] += 1
        self.assertEqual(sum(limiter.allow("command") for _ in range(10)), 3)   # refilled, capped at burst
        self.assertEqual(sum(limiter.allow("text") for _ in range(30)), 20)   # paste chunks / fast typing
        self.assertEqual(sum(limiter.allow("touch") for _ in range(100)), 60)


class Media(unittest.IsolatedAsyncioTestCase):
    async def test_video_waits_for_key_and_resyncs_when_behind(self):
        hub = MediaHub()
        requests = []
        hub.request_keyframe = lambda: requests.append(time.monotonic())
        hub.set_config({"codec": "h264"})
        sub = hub.subscribe()
        hub.set_config({"codec": "h264"})          # identical config: no decoder reset
        self.assertEqual((await sub.next(0))[0], "config")
        hub.publish_video(b"d", False, 1)          # delta before any key: dropped
        self.assertIsNone(await sub.next(0))
        hub.publish_video(b"k", True, 2)
        hub.publish_video(b"d", False, 3)
        self.assertEqual(await sub.next(0), ("video", True, 2, b"k"))
        self.assertEqual(await sub.next(0), ("video", False, 3, b"d"))
        for i in range(VIDEO_QUEUE_LIMIT + 5):
            hub.publish_video(b"d", False, i)
        self.assertTrue(sub.needs_key)
        self.assertTrue(requests)
        hub.publish_video(b"k2", True, 99)
        self.assertEqual(await sub.next(0), ("video", True, 99, b"k2"))

    async def test_jpeg_latest_wins_and_requires_ack(self):
        hub = MediaHub()
        sub = hub.subscribe()
        hub.publish_jpeg(b"a", 1, 1)
        hub.publish_jpeg(b"b", 1, 1)
        self.assertEqual(await sub.next(0), ("jpeg", b"b", 1, 1))
        hub.publish_jpeg(b"c", 1, 1)
        self.assertIsNone(await sub.next(0))       # not acknowledged yet
        sub.ack()
        self.assertEqual(await sub.next(0), ("jpeg", b"c", 1, 1))

    async def test_clear_tells_viewer(self):
        hub = MediaHub()
        sub = hub.subscribe()
        hub.publish_jpeg(b"a", 1, 1)
        hub.clear()
        self.assertEqual(await sub.next(0), ("config", None))
        self.assertIsNone(await sub.next(0))


if __name__ == "__main__":
    unittest.main()
