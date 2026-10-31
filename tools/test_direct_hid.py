
import asyncio

from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
from pymobiledevice3.remote.core_device.hid_service import (
    UniversalHIDServiceService,
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
)


async def main():
    async with PreferredRsdTunnel(serial=None) as rsd:
        async with UniversalHIDServiceService(rsd) as hid:
            print("HID service connected.")

            services = await hid.list_connected_services()
            print("Registered HID surfaces:", services)

            input("Press Enter to tap the center of the iPhone...")

            x, y = 32768, 32768

            await hid.send_touchscreen(
                TOUCHSCREEN_STATE_CONTACT,
                x,
                y,
                service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
            )

            await asyncio.sleep(0.05)

            await hid.send_touchscreen(
                TOUCHSCREEN_STATE_RELEASE,
                x,
                y,
                service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
            )

            print("Tap reports sent.")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
