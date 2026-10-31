# Controlling an iOS 15–16 iPhone (WebDriverAgent, no Mac)

iOS 15 and 16 lack the CoreDevice HID services PhoneView uses on iOS 17+. Without
extra software, those iPhones are **view-only**. Control needs
[WebDriverAgent](https://github.com/appium/WebDriverAgent) (WDA), Appium's
open-source automation server, installed on the phone.

> Status: implemented, **not yet hardware-verified** in this project.

## What PhoneView does once WDA is installed

- Finds any installed app whose bundle id ends in `.xctrunner` and contains
  `WebDriverAgent`, then starts it through pymobiledevice3's XCUITest service
  over USB. No Xcode needed.
- Talks to WDA through usbmux (device port 8100); no port forwarding.
- Uses WDA's MJPEG server (port 9100) for video, which is faster than screenshotr.
  Its scale, quality and frame rate follow **Stream settings**.
- Replays each gesture (tap, drag, swipe, long press) on release as one W3C pointer
  action chain that keeps the path and timing. WDA cannot stream a touch live, so a
  drag shows up on the phone when you release the mouse.
- Home and Volume buttons, plus typing (including Enter and Backspace).

## Installing WDA without a Mac

You need a signed build of the WDA runner app. Without Xcode the usual route is:

1. Download a prebuilt **real-device** runner (`WebDriverAgentRunner-Runner.zip`)
   from the [appium/WebDriverAgent releases](https://github.com/appium/WebDriverAgent/releases).
   Choose a release that supports your iOS version.
2. Re-sign and install it with a sideloading tool that runs on Windows, such as
   Sideloadly or AltStore/AltServer, using your Apple ID. A free Apple ID works, but
   the signature expires after **7 days**, and you must re-sign it after that.
3. On the iPhone, trust the developer profile:
   **Settings → General → VPN & Device Management → (your Apple ID) → Trust**.
4. Keep **Developer Mode** on (iOS 16) and the phone unlocked, then start PhoneView.
   The status line shows "Starting …xctrunner" and then "Control: WebDriverAgent".

If WDA starts but control stops after a week, re-sign it (step 2).
If PhoneView says WebDriverAgent is not installed, check that the runner's bundle
id ends in `.xctrunner`. Some sideloading tools rename bundle ids.
