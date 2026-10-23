# Agent Sessions: notes for coding agents

How this repository is laid out, how to deploy and test on a real Mini Dock, and the firmware quirks learned while building it.

Agent Sessions: a Vobot Mini Dock app that shows running Claude Code sessions, published to the Vobot App Gallery. See README.md for what it does and how users set it up.

## Layout

- `agent_sessions/__init__.py`: the device app (MicroPython, LVGL 9.1). Entry points the firmware calls: `NAME`, `ICON`, `CAN_BE_AUTO_SWITCHED`, `get_settings_json`, `on_boot`, `on_start`, `on_running_foreground`, `on_stop`.
- `agent_sessions/manifest.yml`: app metadata, also read by Vobot's bundler. Bump `version` for every release.
- `bridge/agent_sessions_bridge.py`: HTTP server for the user's computer, standard library only. It reads `~/.claude/sessions/<pid>.json` and the transcripts' `ai-title`/`custom-title` entries, and answers UDP discovery on the same port (8765).
- `bridge/install-service.sh`: runs the bridge at login (launchd on macOS, systemd user unit on Linux).
- `deploy.sh <device>`: zips the app into `dist/` and uploads it to a device.

## Deploying and testing

- `./deploy.sh <device>` works only while **Settings → Application Settings** is open on the device. Otherwise `/apps` answers "Settings Not Available" and the script stops. Ask the user to switch the device into that mode, then deploy.
- Leaving Application Settings after an install usually reboots the device. Leaving without an install does not.
- An install unpacks over the existing app folder without removing old files, and MicroPython imports `__init__.py` before `__init__.mpy`. Before installing a `.vbt` (compiled) over a `deploy.sh` install (source), delete the app first: `curl -X DELETE "http://<device>/api/v1/app/Agent%20Sessions"` while in Application Settings. Otherwise the old source keeps running.
- There is no device log over the network (USB + Thonny only). To see device-side state, temporarily POST diagnostics from the app to a small HTTP receiver on the computer, and remove that code afterwards.
- `python3 bridge/agent_sessions_bridge.py --once` shows exactly what the device receives; `--demo` serves made-up sessions (use it for screenshots, never real session titles). A bridge service may already hold port 8765; use `--port` for test instances.
- To test the `waiting` state, make a Claude Code session ask the user a question; that session shows as `waiting` until it is answered. If you are a Claude Code session yourself, asking the user a question puts your own session into `waiting`. To test waking the screen, first wait about 130 s (the night schedule turns the screen off 60 s after the last wake-up), then ask. A session running a tool, including a shell command, shows as `busy`; one whose turn has ended while a background shell command still runs shows as `shell`.
- Gallery screenshots (`docs/screenshot-*.png`) are pixel-exact captures from a temporary build that shows `--demo` data. Firmware 1.2.6 has neither `lv.snapshot_take` nor `lv.obj_redraw`, so the build captures through the display's own draw buffer: for each 30-row strip, `scr.invalidate_area(area)`, take `lv.display_get_default().get_buf_active()`, call `lv.refr_now(None)`, then copy `buf.data.__dereference__(320 * 2 * rows)`. The buffer holds 19200 bytes (30 rows) and is double-buffered, so get the active buffer before each refresh. The panel driver swaps the two bytes of each RGB565 pixel in place, so decode them big-endian. POST the strips to a small receiver on the computer, which converts them to PNG.

## Publishing to the Vobot App Gallery

- Build the `.vbt` with Vobot's Dock App Bundler (GUI; downloads in the publishing guide). It reads `manifest.yml` (name, description, version, minimum_version, compatible_devices are required), writes the values into a generated `manifest.py` as `KEY = '...'` (so no single quotes in manifest values), adds `from . import manifest` to `__init__.py`, compiles everything to `.mpy` and zips it. Packages may be at most 650 KB. `mpy-cross` embeds the absolute source path in each `.mpy`, so build from a neutral copy (e.g. `/Users/Shared/agent_sessions`), never from a path containing a user name.
- The Gallery app name must equal `NAME` in the code. A listing needs a logo (shown cropped to a circle), a description, at least one screenshot, a minimum OS version and a tag. Vobot reviews every upload (up to 15 working days).

## Firmware facts (1.2.6) learned the hard way

- Do not use `arequests`: it marks the system busy for each request, and the status bar then flashes an hourglass. `_get_json` does a plain `asyncio.open_connection` GET instead.
- `time.ticks_ms()` wraps, and `ticks_diff` against `0` can be negative. Initialize every timer from a real `ticks_ms()` reading.
- `on_running_foreground` runs about twice per second, not every 200 ms as documented.
- An `lv.label` with `LONG.DOT` only truncates when it has a fixed height. Otherwise it wraps and overlaps the next line. Use `set_size(width, font.get_line_height())`.
- Every `set_*` call on a widget forces a redraw, and setting `LONG.SCROLL_CIRCULAR` again restarts the scroll animation. Row updates go through `_update`, which skips unchanged values.
- Screen sleep at night is handled by `variants.dock_hub.ui.scheduled_task` (undocumented). The app wakes the screen with `hide_sleep_screen()` and keeps it awake with `update_wakeup_timestamp()`. `lv.display_get_default().trigger_activity()` has no effect on this sleep.
- After the app calls `hide_sleep_screen()`, LVGL's normal refresh never reaches the panel again: the screen keeps showing a stale frame, although the refresh timer is not paused. The app therefore calls `lv.refr_now(None)` on every foreground tick until `on_stop` (`_force_refresh`). It skips this while the device sleeps.
- UDP broadcast from the app works (`SO_BROADCAST` to 255.255.255.255 and the /24 broadcast); `lv.qrcode` is available.
- The fonts cover ASCII, Latin, Greek and Cyrillic only (`font_ascii_10/14/18/22`, bold 18/22/28/48). The bridge folds typographic characters to ASCII and drops emoji and CJK (`device_text`). `font_ascii_10` looks poor; use 14 or larger.
- The device renames the app folder to `NAME` ("Agent Sessions"), so resources must use `f"A:apps/{NAME}/resources/..."`.
- The firmware runtime (frozen modules) can be inspected via the emulator image `vobot/mini-dock-app-dev` (`/work/micropython`, e.g. with `strings`). Its firmware version is older than the device's.

## Conventions

- The device code must stay MicroPython-compatible: no CPython-only modules or syntax. Check syntax with `python3 -c "import ast; ast.parse(open('agent_sessions/__init__.py').read())"`.
- The bridge must keep working with the Python standard library alone, on macOS, Linux and Windows (never `os.kill(pid, 0)` on Windows; it terminates the process).
- No personal data in the repository: no real IPs, names or session titles (examples use `192.168.1.10`).
- Markdown files: one line per paragraph, no hard wrapping.
