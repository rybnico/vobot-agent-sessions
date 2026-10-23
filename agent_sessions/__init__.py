import asyncio
import json
import sys
import time

import lvgl as lv
import net
import peripherals

try:
    import socket
except ImportError:
    import usocket as socket

NAME = "Agent Sessions"
CAN_BE_AUTO_SWITCHED = True
ICON = f"A:apps/{NAME}/resources/icon.png"

_REPO_URL = "github.com/rybnico/vobot-agent-sessions"
_PORT = 8765  # bridge HTTP and UDP discovery port
_DISCOVERY_REQUEST = b"agent-sessions/discover"
_DISCOVERY_TIMEOUT_MS = 1500
_REQUEST_TIMEOUT = 8  # seconds
_FAILURES_BEFORE_ERROR = 3  # keep showing the last data through short network hiccups
_PULSE_MS = 600

try:
    _SCR_W, _SCR_H = peripherals.screen.screen_resolution
except Exception:
    _SCR_W, _SCR_H = 320, 240
_HEADER_H = 30
_ROW_H = 52

_BG = 0x000000
_TEXT = 0xFFFFFF
_TEXT_DIM = 0x9A9A9A
_OUTLINE_SELECTED = 0x6E6E6E
_WAITING = 0xFF9F0A
_WAITING_PULSE = (0x241600, 0x8A5200)  # row background of a waiting session alternates between these
# status -> (accent color, pill text)
_STYLES = {
    "waiting": (_WAITING, "Needs you"),
    "busy": (0x30D158, "Working"),
    "shell": (0x64D2FF, "Shell"),
    "idle": (0x8E8E93, "Idle"),
}

_app_mgr = None
_cfg = {}
_scr = None
_header = None
_header_title = None
_summary = None
_list = None
_message = None
_qr = None
_rows = []  # one dict of widgets per row
_sessions = []
_selected = 0
_selected_id = None

_fetching = False
_last_fetch = 0
_failures = 0
_discovered_url = None
_waiting_ids = set()
_light_on = False
_pulse = 0
_last_pulse = 0
_SCHEDULE_MODULE = "variants.dock_hub.ui.scheduled_task"
_force_refresh = False  # set once the app has woken the display itself


def get_settings_json():
    return {
        "title": "Agent Sessions",
        "hint": {"url": "https://" + _REPO_URL, "label": "Setup guide and bridge download"},
        "form": [
            {
                "type": "input",
                "default": "",
                "caption": "Bridge URL",
                "name": "bridge_url",
                "attributes": {"maxLength": 80, "placeholder": "found automatically"},
                "tip": "Leave empty to find the bridge on your network automatically. Set it, e.g. "
                       "http://192.168.1.10:8765, if discovery doesn't work.",
            },
            {
                "type": "input",
                "default": "",
                "caption": "Token",
                "name": "token",
                "attributes": {"maxLength": 64, "placeholder": "none"},
                "tip": "Only needed if the bridge was started with --token.",
            },
            {
                "type": "select",
                "default": "3",
                "caption": "Refresh interval",
                "name": "refresh",
                "options": [("2 seconds", "2"), ("3 seconds", "3"), ("5 seconds", "5"), ("10 seconds", "10")],
            },
            {
                "type": "switch",
                "default": True,
                "caption": "Keep the screen awake while a session needs an answer",
                "name": "wake_screen",
            },
            {
                "type": "switch",
                "default": True,
                "caption": "Light up the ambient light while a session needs an answer",
                "name": "ambient_alert",
            },
            {
                "type": "switch",
                "default": False,
                "caption": "Beep when a session starts waiting for an answer",
                "name": "beep_alert",
            },
        ],
    }


def _load_config():
    global _cfg
    try:
        _cfg = (_app_mgr and _app_mgr.config()) or {}
    except Exception:
        _cfg = {}


def _enabled(value):
    return value in (True, 1, "1", "true", "True", "on")


def _configured_url():
    url = (_cfg.get("bridge_url") or "").strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "http://" + url
    return url or None


def _bridge_url():
    url = _configured_url() or _discovered_url
    return url + "/sessions" if url else None


def _refresh_ms():
    try:
        return max(2, int(_cfg.get("refresh", 3))) * 1000
    except Exception:
        return 3000


def _format_age(seconds):
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _plain(obj, bg):
    obj.set_style_radius(0, lv.PART.MAIN)
    obj.set_style_border_width(0, lv.PART.MAIN)
    obj.set_style_pad_all(0, lv.PART.MAIN)
    obj.set_style_bg_color(lv.color_hex(bg), lv.PART.MAIN)
    obj.remove_flag(lv.obj.FLAG.SCROLLABLE)


def _line_height(font, fallback):
    try:
        return font.get_line_height()
    except Exception:
        return fallback


def _label(parent, font, color):
    label = lv.label(parent)
    label.set_style_text_font(font, lv.PART.MAIN)
    label.set_style_text_color(lv.color_hex(color), lv.PART.MAIN)
    return label


def _build_screen():
    global _scr, _header, _header_title, _summary, _list, _message
    _scr = lv.obj()
    _plain(_scr, _BG)

    _header = lv.obj(_scr)
    _plain(_header, _BG)
    _header.set_size(_SCR_W, _HEADER_H - 1)
    _header.set_pos(0, 0)

    _header_title = _label(_header, lv.font_ascii_bold_18, _TEXT)
    _header_title.set_text("Sessions")
    _header_title.align(lv.ALIGN.LEFT_MID, 8, 0)

    _summary = _label(_header, lv.font_ascii_14, _TEXT_DIM)
    _summary.set_size(_SCR_W - 90, _line_height(lv.font_ascii_14, 17))
    _summary.set_long_mode(lv.label.LONG.DOT)
    _summary.set_style_text_align(lv.TEXT_ALIGN.RIGHT, lv.PART.MAIN)
    _summary.set_text("")
    _summary.align(lv.ALIGN.RIGHT_MID, -8, 0)

    line = lv.obj(_scr)
    _plain(line, 0x333333)
    line.set_size(_SCR_W, 1)
    line.set_pos(0, _HEADER_H - 1)

    _list = lv.obj(_scr)
    _plain(_list, _BG)
    _list.add_flag(lv.obj.FLAG.SCROLLABLE)
    _list.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    _list.set_size(_SCR_W, _SCR_H - _HEADER_H)
    _list.set_pos(0, _HEADER_H)

    _message = _label(_scr, lv.font_ascii_18, _TEXT_DIM)
    _message.set_width(_SCR_W - 32)
    _message.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN)
    _message.set_long_mode(lv.label.LONG.WRAP)
    _message.align(lv.ALIGN.CENTER, 0, _HEADER_H // 2)
    _message.set_text("Connecting...")

    # Route encoder/button keys to the screen
    _scr.add_event(_on_event, lv.EVENT.ALL, None)
    group = lv.group_get_default()
    group.add_obj(_scr)
    lv.group_focus_obj(_scr)
    group.set_editing(True)

    lv.scr_load(_scr)


def _make_row(index):
    row = lv.obj(_list)
    _plain(row, _BG)
    row.set_size(_SCR_W, _ROW_H)
    row.set_pos(0, index * _ROW_H)
    # Selection is an inset outline so the background stays free for the status
    row.set_style_outline_pad(-2, lv.PART.MAIN)
    row.set_style_outline_color(lv.color_hex(_OUTLINE_SELECTED), lv.PART.MAIN)
    row.set_style_outline_width(0, lv.PART.MAIN)

    stripe = lv.obj(row)
    _plain(stripe, _TEXT_DIM)
    stripe.set_size(5, _ROW_H - 8)
    stripe.align(lv.ALIGN.LEFT_MID, 3, 0)

    title = _label(row, lv.font_ascii_bold_18, _TEXT)
    # A fixed single-line height is what makes LONG.DOT truncate instead of wrapping
    title.set_size(_SCR_W - 60, _line_height(lv.font_ascii_bold_18, 22))
    title.set_long_mode(lv.label.LONG.DOT)
    title.set_pos(14, 5)

    age = _label(row, lv.font_ascii_14, _TEXT_DIM)
    age.align(lv.ALIGN.TOP_RIGHT, -8, 8)

    detail = _label(row, lv.font_ascii_14, _TEXT_DIM)
    detail.set_size(_SCR_W - 110, _line_height(lv.font_ascii_14, 17))
    detail.set_long_mode(lv.label.LONG.DOT)
    detail.set_pos(14, 30)

    pill = lv.obj(row)
    _plain(pill, _TEXT_DIM)
    pill.set_style_radius(10, lv.PART.MAIN)
    pill.set_size(84, 20)
    pill.align(lv.ALIGN.BOTTOM_RIGHT, -8, -4)
    pill_text = _label(pill, lv.font_ascii_14, _BG)
    pill_text.center()

    sep = lv.obj(row)
    _plain(sep, 0x1C1C1C)
    sep.set_size(_SCR_W, 1)
    sep.align(lv.ALIGN.BOTTOM_LEFT, 0, 0)

    return {"row": row, "stripe": stripe, "title": title, "age": age,
            "detail": detail, "pill": pill, "pill_text": pill_text, "shown": {}}


def _update(widgets, key, value, apply):
    # Only touch LVGL when a value changed; every set_* call invalidates and redraws the widget
    if widgets["shown"].get(key) != value:
        widgets["shown"][key] = value
        apply(value)


def _fill_row(widgets, session, selected):
    status = session.get("status", "idle")
    accent, pill_label = _STYLES.get(status, _STYLES["idle"])
    waiting = status == "waiting"

    detail = session.get("project", "")
    if waiting and session.get("waiting_for"):
        detail = f"{detail}: {session['waiting_for']}"

    _update(widgets, "bg", _WAITING_PULSE[_pulse] if waiting else _BG,
            lambda c: widgets["row"].set_style_bg_color(lv.color_hex(c), lv.PART.MAIN))
    _update(widgets, "outline", 2 if selected else 0,
            lambda w: widgets["row"].set_style_outline_width(w, lv.PART.MAIN))
    _update(widgets, "accent", accent, lambda c: (
        widgets["stripe"].set_style_bg_color(lv.color_hex(c), lv.PART.MAIN),
        widgets["pill"].set_style_bg_color(lv.color_hex(c), lv.PART.MAIN)))
    _update(widgets, "pill", pill_label, widgets["pill_text"].set_text)
    _update(widgets, "title", session.get("title", ""), widgets["title"].set_text)
    _update(widgets, "scroll", selected, lambda on: widgets["title"].set_long_mode(
        lv.label.LONG.SCROLL_CIRCULAR if on else lv.label.LONG.DOT))
    _update(widgets, "detail", detail, widgets["detail"].set_text)
    _update(widgets, "detail_color", accent if waiting else _TEXT_DIM,
            lambda c: widgets["detail"].set_style_text_color(lv.color_hex(c), lv.PART.MAIN))
    _update(widgets, "age", _format_age(session.get("age", 0)), widgets["age"].set_text)


def _render():
    global _rows, _selected
    if not _scr:
        return

    while len(_rows) > len(_sessions):
        _rows.pop()["row"].delete()
    while len(_rows) < len(_sessions):
        _rows.append(_make_row(len(_rows)))

    # Keep the selection on the same session when the order changes
    _selected = 0
    for i, session in enumerate(_sessions):
        if session.get("id") == _selected_id:
            _selected = i
            break
    for i, session in enumerate(_sessions):
        _fill_row(_rows[i], session, i == _selected)

    if _sessions:
        _show_message(None)
    else:
        _show_message("No Claude Code sessions running")


def _show_message(text, qr_data=None):
    if text is None:
        _message.add_flag(lv.obj.FLAG.HIDDEN)
        _show_qr(None)
        return
    with_qr = _show_qr(qr_data)
    # With a QR code the text moves to the left half in a smaller font, otherwise it is centered
    _message.set_style_text_font(lv.font_ascii_14 if with_qr else lv.font_ascii_18, lv.PART.MAIN)
    _message.set_width(_SCR_W - 140 if with_qr else _SCR_W - 32)
    _message.set_style_text_align(lv.TEXT_ALIGN.LEFT if with_qr else lv.TEXT_ALIGN.CENTER, lv.PART.MAIN)
    if with_qr:
        _message.align(lv.ALIGN.LEFT_MID, 14, _HEADER_H // 2)
    else:
        _message.align(lv.ALIGN.CENTER, 0, _HEADER_H // 2)
    if _message.get_text() != text:
        _message.set_text(text)
    _message.remove_flag(lv.obj.FLAG.HIDDEN)


def _show_qr(data):
    """Show a QR code for data on the right; returns False if hidden or the firmware has no QR widget."""
    global _qr
    if data is None:
        if _qr:
            _qr.add_flag(lv.obj.FLAG.HIDDEN)
        return False
    if _qr is None:
        try:
            _qr = lv.qrcode(_scr)
            _qr.set_size(100)
            _qr.set_dark_color(lv.color_hex(_BG))
            _qr.set_light_color(lv.color_hex(_TEXT))
            _qr.update(data, len(data))
            # Light border as the quiet zone scanners need around the code
            _qr.set_style_border_color(lv.color_hex(_TEXT), lv.PART.MAIN)
            _qr.set_style_border_width(6, lv.PART.MAIN)
            _qr.align(lv.ALIGN.RIGHT_MID, -14, _HEADER_H // 2)
        except Exception as e:
            print(f"{NAME}: no QR code: {_describe(e)}")
            if _qr:
                _qr.delete()
            _qr = None
            return False
    _qr.remove_flag(lv.obj.FLAG.HIDDEN)
    return True


def _set_summary(text, alert=False):
    if _summary.get_text() != text:
        _summary.set_text(text)
    # While a session waits for an answer the whole header turns into an orange banner
    _header.set_style_bg_color(lv.color_hex(_WAITING if alert else _BG), lv.PART.MAIN)
    _header_title.set_style_text_color(lv.color_hex(_BG if alert else _TEXT), lv.PART.MAIN)
    _summary.set_style_text_color(lv.color_hex(_BG if alert else _TEXT_DIM), lv.PART.MAIN)


def _render_summary(counts, total):
    waiting = counts.get("waiting", 0)
    parts = []
    if waiting:
        parts.append(f"{waiting} {'needs' if waiting == 1 else 'need'} you")
    if counts.get("working"):
        parts.append(f"{counts['working']} working")
    if counts.get("idle"):
        parts.append(f"{counts['idle']} idle")
    _set_summary("  ".join(parts) if parts else f"{total} sessions", bool(waiting))


def _render_error(text, qr_data=None):
    global _sessions
    _sessions = []
    _render()
    _set_summary("offline")
    _show_message(text, qr_data)


def _select(index, animate=True):
    global _selected, _selected_id
    previous = _selected
    _selected = index
    _selected_id = _sessions[index].get("id")
    if previous < len(_rows):
        _fill_row(_rows[previous], _sessions[previous], False)
    _fill_row(_rows[index], _sessions[index], True)
    _rows[index]["row"].scroll_to_view(lv.ANIM.ON if animate else lv.ANIM.OFF)


def _move_selection(step):
    if _sessions:
        _select(min(max(_selected + step, 0), len(_sessions) - 1))


def _on_event(e):
    code = e.get_code()
    if code == lv.EVENT.KEY:
        key = e.get_key()
        if key == lv.KEY.LEFT:  # encoder clockwise
            _move_selection(1)
        elif key == lv.KEY.RIGHT:  # encoder counter-clockwise
            _move_selection(-1)
        # ESC is handled by the system and leaves the app
    elif code == lv.EVENT.FOCUSED:
        group = lv.group_get_default()
        if not group.get_editing():
            group.set_editing(True)


def _pulse_waiting_rows():
    global _pulse
    _pulse = 1 - _pulse
    for i, session in enumerate(_sessions):
        if session.get("status") == "waiting" and i < len(_rows):
            _fill_row(_rows[i], session, i == _selected)


def _set_light(on):
    global _light_on
    if on == _light_on:
        return
    try:
        if on:
            if peripherals.ambient_light.acquire():
                peripherals.ambient_light.set_color([_rgb(_WAITING)], True)
                _light_on = True
        else:
            peripherals.ambient_light.release()
            _light_on = False
    except Exception as e:
        print(f"{NAME}: ambient light: {e}")


def _rgb(color):
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def _wake_screen():
    """Keep the screen on while a session waits; returns True if a sleeping screen was woken."""
    try:
        lv.display_get_default().trigger_activity()
    except Exception:
        pass
    # During the night schedule the firmware's scheduled_task module puts the screen to sleep
    # SLEEP_CYCLE seconds after the last wake-up. There is no public API for it, so use it when present.
    schedule = sys.modules.get(_SCHEDULE_MODULE)
    if not schedule:
        return False
    woke = False
    try:
        if schedule.is_device_sleeping():
            schedule.hide_sleep_screen()
            woke = True
        schedule.update_wakeup_timestamp()
    except Exception as e:
        print(f"{NAME}: wake: {_describe(e)}")
    return woke


def _device_sleeping():
    try:
        return sys.modules[_SCHEDULE_MODULE].is_device_sleeping()
    except Exception:
        return False


def _screen_is_ours():
    try:
        return lv.screen_active() == _scr
    except Exception:
        return None


def _refresh_now():
    # After hide_sleep_screen() LVGL's normal refresh no longer reaches the panel (a key press on the
    # device restores it, the API call does not); an explicit refresh still does.
    try:
        lv.refr_now(None)
    except Exception as e:
        print(f"{NAME}: refr_now: {_describe(e)}")


def _redraw_all():
    # Widget changes made while the display slept were never painted; apply and paint everything again
    if _screen_is_ours() is False:
        lv.scr_load(_scr)
    for widgets in _rows:
        widgets["shown"].clear()
    _render()
    _scr.invalidate()
    _refresh_now()


def _on_woke():
    global _force_refresh
    _force_refresh = True
    _redraw_all()


async def _beep():
    try:
        if not peripherals.buzzer.enabled or not peripherals.buzzer.acquire():
            return
        try:
            for freq in (1320, 1760):
                peripherals.buzzer.set_freq(freq)
                peripherals.buzzer.set_volume(40)
                await asyncio.sleep_ms(90)
                peripherals.buzzer.set_volume(0)
                await asyncio.sleep_ms(40)
        finally:
            peripherals.buzzer.set_volume(0)
            peripherals.buzzer.release()
    except Exception as e:
        print(f"{NAME}: buzzer: {e}")


def _alert(sessions):
    global _waiting_ids
    waiting = set(s.get("id") for s in sessions if s.get("status") == "waiting")
    new = waiting - _waiting_ids
    _waiting_ids = waiting

    _set_light(bool(waiting) and _enabled(_cfg.get("ambient_alert", True)))
    if waiting and _enabled(_cfg.get("wake_screen", True)) and _wake_screen():
        _on_woke()
    if new:
        # Jump to the session that just started waiting
        for i, session in enumerate(sessions):
            if session.get("id") in new:
                _select(i)
                break
        if _enabled(_cfg.get("beep_alert", False)):
            asyncio.create_task(_beep())


def _describe(e):
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class _HttpError(Exception):
    pass


async def _get_json(url, token):
    # Plain socket GET: the firmware's arequests flags the system busy for every request,
    # which shows an hourglass in the status bar each time the list refreshes.
    hostport, _, path = url.split("://", 1)[1].partition("/")
    host, _, port = hostport.partition(":")
    reader, writer = await asyncio.open_connection(host, int(port or 80))
    try:
        auth = f"Authorization: Bearer {token}\r\n" if token else ""
        writer.write(f"GET /{path} HTTP/1.0\r\nHost: {hostport}\r\n{auth}Connection: close\r\n\r\n".encode())
        await writer.drain()
        status = (await reader.readline()).split()
        code = int(status[1]) if len(status) > 1 else 0
        if code != 200:
            raise _HttpError(code)
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        body = b""
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                break
            body += chunk
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return json.loads(body)


async def _discover():
    """Broadcast a discovery request; returns the bridge's base URL from the first answer, or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        sock.setblocking(False)
        targets = ["255.255.255.255"]
        try:
            # Also the /24 subnet broadcast, which some routers forward when they drop 255.255.255.255
            targets.append(net.config()["IP"].rsplit(".", 1)[0] + ".255")
        except Exception:
            pass
        for target in targets:
            try:
                sock.sendto(_DISCOVERY_REQUEST, (target, _PORT))
            except Exception as e:
                print(f"{NAME}: discovery to {target}: {_describe(e)}")
        deadline = time.ticks_add(time.ticks_ms(), _DISCOVERY_TIMEOUT_MS)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                data, addr = sock.recvfrom(256)
            except OSError:
                await asyncio.sleep_ms(50)
                continue
            try:
                info = json.loads(data)
            except ValueError:
                continue
            if info.get("service") != "agent-sessions":
                continue
            if isinstance(addr, tuple):
                host = addr[0]
            else:  # raw sockaddr_in: family (2 bytes), port (2), IPv4 address (4)
                host = ".".join(str(b) for b in addr[4:8])
            return f"http://{host}:{info.get('port', _PORT)}"
    finally:
        sock.close()
    return None


def _fetch_failed(url, error):
    global _failures, _discovered_url
    _failures += 1
    print(f"{NAME}: fetch {url} failed ({_failures}x): {_describe(error) if error else 'no bridge found'}")
    if url and not _configured_url() and _failures >= _FAILURES_BEFORE_ERROR:
        _discovered_url = None  # the bridge may have moved to another address; look for it again
    if not _scr:
        return
    if _sessions and _failures < _FAILURES_BEFORE_ERROR:
        _set_summary("reconnecting...")
        return
    if url is None and error is not None:
        _render_error(f"Can't look for the bridge\n{_describe(error)}")
    elif url is None:
        # Break the URL at its slash ourselves; LVGL would break it after "." and "-"
        owner, _, repo = _REPO_URL.rpartition("/")
        _render_error(f"No bridge found\n\nStart the bridge on your computer. Setup guide:\n\n{owner}/\n{repo}",
                      "https://" + _REPO_URL)
    elif isinstance(error, _HttpError) and error.args and error.args[0] == 401:
        _render_error("The bridge wants a token\n\nSet it in the app settings on the device's web page.")
    else:
        _render_error(f"Can't reach the bridge\n{url}\n{_describe(error)}")
    _alert([])


async def _fetch():
    global _fetching, _failures, _sessions, _selected_id, _discovered_url
    url = None
    try:
        if not net.connected():
            raise Exception("no network")
        if not _bridge_url():
            _discovered_url = await _discover()
        url = _bridge_url()
        if not url:
            _fetch_failed(None, None)
            return
        data = await asyncio.wait_for(_get_json(url, (_cfg.get("token") or "").strip()), _REQUEST_TIMEOUT)
    except Exception as e:
        _fetch_failed(url, e)
        return
    finally:
        _fetching = False

    _failures = 0
    try:
        if not _scr:
            return
        _sessions = data.get("sessions", [])
        if _selected_id is None and _sessions:
            _selected_id = _sessions[0].get("id")
        _render()
        _render_summary(data.get("counts", {}), data.get("total", len(_sessions)))
        _alert(_sessions)
    except Exception as e:
        sys.print_exception(e)
        if _scr:
            _show_message(f"Display error\n{_describe(e)}")


async def on_boot(apm):
    global _app_mgr
    _app_mgr = apm


async def on_start():
    global _last_fetch, _last_pulse
    _last_pulse = time.ticks_ms()  # ticks wrap, so timers must start from a real reading
    _load_config()
    if not _scr:
        _build_screen()
    if _app_mgr:
        _app_mgr.enter_root_page()
    _last_fetch = time.ticks_add(time.ticks_ms(), -_refresh_ms())  # fetch right away


async def on_running_foreground():
    global _fetching, _last_fetch, _last_pulse
    if not _scr:
        return
    now = time.ticks_ms()
    if _force_refresh and not _device_sleeping():
        _refresh_now()  # paints whatever changed since the last tick; nothing when nothing did
    if _waiting_ids and time.ticks_diff(now, _last_pulse) >= _PULSE_MS:
        _last_pulse = now
        _pulse_waiting_rows()
    if _fetching or time.ticks_diff(now, _last_fetch) < _refresh_ms():
        return
    _fetching = True
    _last_fetch = now
    asyncio.create_task(_fetch())


async def on_stop():
    global _scr, _header, _header_title, _summary, _list, _message, _qr, _rows, _sessions, _waiting_ids, _failures, _force_refresh
    _set_light(False)
    _waiting_ids = set()
    _failures = 0
    _force_refresh = False
    if _scr:
        _scr.clean()
        _scr.delete_async()
    _scr = _header = _header_title = _summary = _list = _message = _qr = None
    _rows = []
    _sessions = []
    if _app_mgr:
        _app_mgr.leave_root_page()
