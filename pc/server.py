"""Robo Face - server PC (Windows) v2.

Jalankan:  python server.py
- HP konek lewat WebSocket (port 8765) pakai PIN.
- Ekstensi Chrome konek dari localhost (tanpa PIN).
- Tool AI dari HP (Gemini) diteruskan ke ekstensi Chrome atau dijalankan di sini
  (buka aplikasi dari shortcut, volume/media). Tidak ada akses shell / ketik bebas.
"""
import asyncio
import ctypes
import difflib
import hashlib
import json
import os
import random
import re
import socket
import struct
import subprocess
import time
import urllib.parse
import zlib
from pathlib import Path

from websockets.asyncio.server import serve

PORT = 8765
CFG_FILE = Path(__file__).with_name("config.json")
IS_WIN = os.name == "nt"


# ---------- config / PIN ----------
def load_cfg():
    if CFG_FILE.exists():
        try:
            return json.loads(CFG_FILE.read_text())
        except Exception:
            pass
    cfg = {"pin": str(random.randint(1000, 9999))}
    CFG_FILE.write_text(json.dumps(cfg))
    return cfg


cfg = load_cfg()

# ---------- state ----------
phones = set()
ext = None
state = {"media": False, "password": False}
recent = {}
halted = False          # true setelah tombol "Stop AI" ditekan di Chrome
pending = {}            # id tool -> future (hasil dari ekstensi)
tool_seq = 0
screen_wants = {}       # ws HP -> lebar frame
screen_dirty = False


async def to_phones(msg):
    data = json.dumps(msg)
    for p in list(phones):
        try:
            await p.send(data)
        except Exception:
            phones.discard(p)


def is_dup(key):
    now = time.time()
    for k, v in list(recent.items()):
        if now - v > 6:
            del recent[k]
    if key in recent:
        return True
    recent[key] = now
    return False


# ---------- Windows helpers ----------
user32 = gdi32 = None
if IS_WIN:
    from ctypes import c_int, c_uint, c_uint32, c_void_p
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    user32.GetDC.restype = c_void_p
    user32.GetDC.argtypes = [c_void_p]
    user32.ReleaseDC.argtypes = [c_void_p, c_void_p]
    gdi32.CreateCompatibleDC.restype = c_void_p
    gdi32.CreateCompatibleDC.argtypes = [c_void_p]
    gdi32.CreateCompatibleBitmap.restype = c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [c_void_p, c_int, c_int]
    gdi32.SelectObject.restype = c_void_p
    gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
    gdi32.DeleteObject.argtypes = [c_void_p]
    gdi32.DeleteDC.argtypes = [c_void_p]
    gdi32.SetStretchBltMode.argtypes = [c_void_p, c_int]
    gdi32.StretchBlt.argtypes = [c_void_p, c_int, c_int, c_int, c_int, c_void_p,
                                 c_int, c_int, c_int, c_int, c_uint32]
    gdi32.GetDIBits.argtypes = [c_void_p, c_void_p, c_uint, c_uint, c_void_p, c_void_p, c_uint]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def mouse_pos():
    if not IS_WIN:
        return 0.5, 0.5
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    w = max(1, user32.GetSystemMetrics(0))
    h = max(1, user32.GetSystemMetrics(1))
    return p.x / w, p.y / h


VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "w": 0x57, "tab": 0x09, "r": 0x52,
    "left": 0x25, "right": 0x27, "mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF,
    "next": 0xB0, "prev": 0xB1, "playpause": 0xB3,
}


def press(*names):
    if not IS_WIN:
        return
    codes = [VK[n] for n in names]
    for c in codes:
        user32.keybd_event(c, 0, 0, 0)
    for c in reversed(codes):
        user32.keybd_event(c, 0, 2, 0)


def os_open(url):
    try:
        r = subprocess.run(["cmd", "/c", "start", "", "chrome", url],
                           capture_output=True, creationflags=0x08000000)
        if r.returncode == 0:
            return
    except Exception:
        pass
    try:
        os.startfile(url)  # browser default
    except Exception:
        import webbrowser
        webbrowser.open(url)


# ---------- layar PC -> HP (tanpa library tambahan) ----------
def bgra_to_png(bgra, w, h):
    stride = w * 4
    raw = bytearray()
    rgb = bytearray(w * 3)
    for y in range(h):
        px = bgra[y * stride:(y + 1) * stride]
        rgb[0::3] = px[2::4]
        rgb[1::3] = px[1::4]
        rgb[2::3] = px[0::4]
        raw.append(0)
        raw += rgb

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 1))
            + chunk(b"IEND", b""))


def grab_screen(out_w):
    """Return (bgra bytearray, w, h) layar utama yang sudah diperkecil, atau None."""
    if not IS_WIN:
        return None
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    out_w = max(160, min(out_w, sw))
    out_h = max(90, round(sh * out_w / sw))
    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, out_w, out_h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        gdi32.SetStretchBltMode(mdc, 4)  # HALFTONE
        if not gdi32.StretchBlt(mdc, 0, 0, out_w, out_h, hdc, 0, 0, sw, sh, 0x00CC0020):
            return None
        bmi = BITMAPINFOHEADER(40, out_w, -out_h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = (ctypes.c_ubyte * (out_w * out_h * 4))()
        if not gdi32.GetDIBits(mdc, bmp, 0, out_h, buf, ctypes.byref(bmi), 0):
            return None
        return bytearray(buf), out_w, out_h
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, hdc)


def capture_png(width):
    g = grab_screen(width)
    if g is None:
        return None
    data, w, h = g
    mx, my = mouse_pos()          # titik merah = posisi mouse asli
    cx, cy = int(mx * w), int(my * h)
    for yy in range(max(0, cy - 4), min(h, cy + 5)):
        for xx in range(max(0, cx - 4), min(w, cx + 5)):
            o = (yy * w + xx) * 4
            data[o:o + 3] = b"\x30\x30\xff"  # BGR merah
    return bgra_to_png(data, w, h)


async def screen_loop():
    global screen_dirty
    last = None
    warned = False
    while True:
        if not screen_wants:
            last = None
            await asyncio.sleep(0.3)
            continue
        t0 = time.time()
        png = await asyncio.to_thread(capture_png, max(screen_wants.values()))
        if png is None:
            if not warned:
                print("[layar] gagal mengambil layar")
                warned = True
            await asyncio.sleep(2)
            continue
        h = hashlib.md5(png).digest()
        if h != last or screen_dirty:
            last, screen_dirty = h, False
            for p in list(screen_wants):
                try:
                    await p.send(png)
                except Exception:
                    screen_wants.pop(p, None)
        await asyncio.sleep(max(0.05, 0.45 - (time.time() - t0)))


# ---------- buka aplikasi (hanya dari shortcut Start Menu / Desktop) ----------
APP_ROOTS = [
    os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%USERPROFILE%\Desktop"),
    os.path.expandvars(r"%PUBLIC%\Desktop"),
]
BLOCK_APPS = re.compile(
    r"(cmd|command prompt|powershell|terminal|regedit|registry|task ?manager|msconfig|"
    r"services|computer management|group policy|diskpart|\bwsl\b|bash|python|\bidle\b|"
    r"\bpip\b|script|installer|setup|uninstall|hapus)", re.I)


def norm_name(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def index_apps():
    apps = {}
    for root in APP_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        for dp, _, files in os.walk(root):
            for f in files:
                stem, ext_ = os.path.splitext(f)
                if ext_.lower() not in (".lnk", ".url"):
                    continue
                if BLOCK_APPS.search(stem):
                    continue
                apps.setdefault(stem.lower(), (stem, os.path.join(dp, f)))
    return list(apps.values())


def pick_apps(query, apps):
    q = norm_name(query)
    if not q:
        return []
    scored = []
    for stem, path in apps:
        n = norm_name(stem)
        if n == q:
            s = 0
        elif n.startswith(q):
            s = 1
        elif q in n.split():
            s = 2
        elif q in n:
            s = 3
        elif difflib.SequenceMatcher(None, q, n).ratio() > 0.78:
            s = 4
        else:
            continue
        scored.append((s, len(stem), stem, path))
    scored.sort()
    return [(s[2], s[3]) for s in scored]


def launch(path):
    os.startfile(path)  # noqa: hanya shortcut hasil index_apps


def tool_open_app(name):
    if BLOCK_APPS.search(name or ""):
        return {"error": "Aplikasi itu diblokir demi keamanan"}
    hits = pick_apps(name, index_apps())
    if not hits:
        return {"error": f"Gak nemu aplikasi '{name}' di Start Menu / Desktop"}
    stem, path = hits[0]
    try:
        launch(path)
    except Exception as e:
        return {"error": f"Gagal membuka {stem}: {e}"}
    out = {"ok": True, "opened": stem}
    if len(hits) > 1:
        out["mirip"] = [h[0] for h in hits[1:4]]
    return out


SYSTEM_KEYS = {
    "volume_up": ("vol_up", 5), "volume_down": ("vol_down", 5), "mute": ("mute", 1),
    "play_pause": ("playpause", 1), "next_track": ("next", 1), "prev_track": ("prev", 1),
}


def tool_system(action):
    if action not in SYSTEM_KEYS:
        return {"error": "Aksi sistem tidak dikenal"}
    key, reps = SYSTEM_KEYS[action]
    for _ in range(reps):
        press(key)
    return {"ok": True, "action": action}


# ---------- parser perintah suara (mode tanpa Gemini) ----------
SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "github": "https://github.com",
    "whatsapp": "https://web.whatsapp.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "tiktok": "https://www.tiktok.com",
    "twitter": "https://x.com", "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai", "telegram": "https://web.telegram.org",
    "maps": "https://maps.google.com", "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com", "tokopedia": "https://www.tokopedia.com",
    "shopee": "https://shopee.co.id", "vercel": "https://vercel.com",
    "roblox": "https://www.roblox.com", "discord": "https://discord.com/app",
}
FILLER = re.compile(r"\b(tolong|dong|ya|deh|please|coba|aja|saja|nih)\b")


def norm(text):
    t = text.lower().strip()
    t = re.sub(r"[^\w\s\.\-/:]", " ", t)
    t = FILLER.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def site_url(name):
    name = name.strip()
    if name in SITES:
        return SITES[name]
    n = name.replace(" ", "")
    if re.fullmatch(r"[a-z0-9\-]+(\.[a-z0-9\-]+)+(/\S*)?", n):
        return "https://" + n
    return None


def parse_command(text):
    """Return (action, arg, pesan_balasan) atau None kalau tidak dimengerti."""
    t = norm(text)
    if not t:
        return None

    if re.search(r"\b(tab baru|new tab|tab kosong|tab baru kosong)\b", t):
        rest = re.sub(r"\b(buka|open|bikin|buat|tab baru|new tab|tab kosong|di|dengan)\b", " ", t).strip()
        u = site_url(rest) if rest else None
        if u:
            return ("open_url", u, f"Buka {rest} di tab baru")
        return ("new_tab", None, "Tab baru dibuka")
    if re.search(r"\b(tutup|close)\b.*\btab\b", t):
        return ("close_tab", None, "Tab ditutup")
    if re.search(r"(tab (berikut|selanjutnya|kanan)|next tab)", t):
        return ("next_tab", None, "Tab berikutnya")
    if re.search(r"(tab (sebelum|kiri)|prev(ious)? tab)", t):
        return ("prev_tab", None, "Tab sebelumnya")
    if re.search(r"\b(refresh|muat ulang|reload|segarkan)\b", t):
        return ("reload", None, "Dimuat ulang")
    if re.fullmatch(r"(kembali|back|mundur|halaman sebelumnya)", t):
        return ("back", None, "Kembali")
    if re.search(r"\bvolume\b", t):
        if re.search(r"(naik|up|keras|besar|tambah)", t):
            return ("vol_up", None, "Volume naik")
        if re.search(r"(turun|down|kecil|kurang|pelan)", t):
            return ("vol_down", None, "Volume turun")
        if re.search(r"(mute|bisu|diam|senyap)", t):
            return ("mute", None, "Mute")
    if re.fullmatch(r"(mute|bisukan|senyap)", t):
        return ("mute", None, "Mute")
    if re.search(r"\b(jeda|pause|putar|play|lanjut(kan)? (video|musik|lagu))\b", t):
        return ("playpause", None, "Play/Pause")
    if re.search(r"(lagu|video|musik) (berikut|selanjutnya)|next (song|track)", t):
        return ("media_next", None, "Berikutnya")
    if re.search(r"(lagu|video|musik) (sebelum)|prev(ious)? (song|track)", t):
        return ("media_prev", None, "Sebelumnya")

    m = re.fullmatch(r"(?:cari|search|googling|telusuri)\s+(.+?)\s+di\s+youtube", t)
    if m:
        q = urllib.parse.quote_plus(m.group(1))
        return ("open_url", f"https://www.youtube.com/results?search_query={q}", f"Cari {m.group(1)} di YouTube")
    m = re.fullmatch(r"(?:cari|search|googling|telusuri)\s+(.+)", t)
    if m:
        q = urllib.parse.quote_plus(m.group(1))
        return ("open_url", f"https://www.google.com/search?q={q}", f"Cari {m.group(1)}")
    m = re.fullmatch(r"(?:buka|open|go to|ke|kunjungi)\s+(.+)", t)
    if m:
        rest = m.group(1)
        u = site_url(rest)
        if u:
            return ("open_url", u, f"Buka {rest}")
        q = urllib.parse.quote_plus(rest)
        return ("open_url", f"https://www.google.com/search?q={q}", f"Cari {rest}")
    return None


async def ext_send(action, url=None):
    if ext is None:
        return False
    try:
        await ext.send(json.dumps({"t": "ext_cmd", "action": action, "url": url}))
        return True
    except Exception:
        return False


async def run_command(text):
    parsed = parse_command(text)
    if not parsed:
        return {"t": "cmd_result", "ok": False, "text": f"Gak ngerti: {text}"}
    action, arg, reply = parsed

    if action in ("new_tab", "close_tab", "next_tab", "prev_tab", "reload", "back", "open_url"):
        if await ext_send(action, arg):
            return {"t": "cmd_result", "ok": True, "text": reply}
        if action == "open_url":
            await asyncio.to_thread(os_open, arg)
        elif action == "new_tab":
            await asyncio.to_thread(os_open, "chrome://newtab")
        elif action == "close_tab":
            press("ctrl", "w")
        elif action == "next_tab":
            press("ctrl", "tab")
        elif action == "prev_tab":
            press("ctrl", "shift", "tab")
        elif action == "reload":
            press("ctrl", "r")
        elif action == "back":
            press("alt", "left")
        return {"t": "cmd_result", "ok": True, "text": reply}

    keymap = {"vol_up": ("vol_up",), "vol_down": ("vol_down",), "mute": ("mute",),
              "playpause": ("playpause",), "media_next": ("next",), "media_prev": ("prev",)}
    if action in keymap:
        reps = 5 if action in ("vol_up", "vol_down") else 1
        for _ in range(reps):
            press(*keymap[action])
        return {"t": "cmd_result", "ok": True, "text": reply}
    return {"t": "cmd_result", "ok": False, "text": "Belum didukung"}


# ---------- tool AI (dari Gemini di HP) ----------
EXT_TOOLS = {"open_url", "snapshot", "click", "type_text", "scroll", "media",
             "browser_nav", "list_tabs", "focus_tab"}


async def dispatch_tool(name, args):
    global tool_seq
    if halted:
        return {"error": "Dihentikan user lewat tombol Stop AI. Tanya user dulu sebelum lanjut."}
    try:
        if name == "open_app":
            return await asyncio.to_thread(tool_open_app, str(args.get("name", "")))
        if name == "system":
            return tool_system(args.get("action"))
        if name == "web_search":
            q = urllib.parse.quote_plus(str(args.get("query", ""))[:200])
            if args.get("site") == "youtube":
                url = f"https://www.youtube.com/results?search_query={q}"
            else:
                url = f"https://www.google.com/search?q={q}"
            return await dispatch_tool("open_url", {"url": url, "new_tab": bool(args.get("new_tab"))})
        if name in EXT_TOOLS:
            if ext is None:
                return {"error": "Ekstensi Chrome belum tersambung. Minta user buka Chrome dan cek ekstensi Robo Face Bridge."}
            tool_seq += 1
            sid = str(tool_seq)
            fut = asyncio.get_running_loop().create_future()
            pending[sid] = fut
            try:
                await ext.send(json.dumps({"t": "ext_tool", "id": sid, "name": name, "args": args}))
                return await asyncio.wait_for(fut, 30)
            except asyncio.TimeoutError:
                return {"error": "Timeout: Chrome tidak merespon"}
            finally:
                pending.pop(sid, None)
    except Exception as e:
        return {"error": str(e)}
    return {"error": "Tool tidak dikenal: " + str(name)}


async def phone_tool(ws, m):
    result = await dispatch_tool(m.get("name"), m.get("args") or {})
    try:
        await ws.send(json.dumps({"t": "tool_result", "id": m.get("id"), "result": result}))
    except Exception:
        pass


# ---------- handler koneksi ----------
async def from_ext(m):
    global halted
    t = m.get("t")
    if t == "media":
        state["media"] = bool(m.get("playing"))
        await to_phones({"t": "media", "playing": state["media"]})
    elif t == "password":
        state["password"] = bool(m.get("active"))
        await to_phones({"t": "password", "active": state["password"]})
    elif t == "notif":
        await push_notif(m.get("app", ""), m.get("title", ""), m.get("body", ""), m.get("image") or "")
    elif t == "tool_result":
        fut = pending.get(str(m.get("id")))
        if fut and not fut.done():
            fut.set_result(m.get("result"))
    elif t == "ai_stop":
        halted = True
        for f in list(pending.values()):
            if not f.done():
                f.set_result({"error": "Dihentikan user"})
        await to_phones({"t": "ai_stop"})


async def from_phone(ws, m):
    global halted, screen_dirty
    t = m.get("t")
    if t == "cmd":
        await to_phones(await run_command(str(m.get("text", ""))))
    elif t == "tool":
        asyncio.create_task(phone_tool(ws, m))
    elif t == "ai_start":
        halted = False
    elif t == "screen":
        if m.get("on"):
            try:
                screen_wants[ws] = max(160, min(1280, int(m.get("w", 480))))
            except Exception:
                screen_wants[ws] = 480
            screen_dirty = True
        else:
            screen_wants.pop(ws, None)


async def push_notif(app, title, body, image=""):
    key = (title + body).strip().lower()[:200]
    if key and is_dup(key):
        return
    await to_phones({"t": "notif", "app": app, "title": title[:120],
                     "body": body[:300], "image": image})


async def handler(ws):
    global ext
    ip = ws.remote_address[0] if ws.remote_address else ""
    role = None
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if role is None:
                if m.get("t") != "hello":
                    continue
                if m.get("role") == "ext" and ip in ("127.0.0.1", "::1"):
                    role = "ext"
                    ext = ws
                elif m.get("role") == "phone" and str(m.get("pin")) == cfg["pin"]:
                    role = "phone"
                    phones.add(ws)
                    await ws.send(json.dumps({"t": "hello_ok"}))
                    await ws.send(json.dumps({"t": "media", "playing": state["media"]}))
                    await ws.send(json.dumps({"t": "password", "active": state["password"]}))
                else:
                    await ws.send(json.dumps({"t": "auth_fail"}))
                    await ws.close()
                    return
                continue
            if role == "ext":
                await from_ext(m)
            else:
                await from_phone(ws, m)
    except Exception:
        pass
    finally:
        phones.discard(ws)
        screen_wants.pop(ws, None)
        if ext is ws:
            ext = None
            state["media"] = False
            state["password"] = False
            await to_phones({"t": "media", "playing": False})
            await to_phones({"t": "password", "active": False})


# ---------- loop ----------
async def mouse_loop():
    last = None
    while True:
        if phones:
            p = mouse_pos()
            if last is None or abs(p[0] - last[0]) > 0.002 or abs(p[1] - last[1]) > 0.002:
                last = p
                await to_phones({"t": "mouse", "x": round(p[0], 4), "y": round(p[1], 4)})
        await asyncio.sleep(0.04)


async def notif_loop():
    if not IS_WIN:
        return
    try:
        from winsdk.windows.ui.notifications import KnownNotificationBindings, NotificationKinds
        from winsdk.windows.ui.notifications.management import (
            UserNotificationListener, UserNotificationListenerAccessStatus)
    except Exception as e:
        print("[notif] winsdk tidak ada, notif native Windows mati:", e)
        return
    try:
        listener = UserNotificationListener.current
        status = await listener.request_access_async()
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            print("[notif] Izin notif ditolak. Aktifkan di Settings > Privacy & security > Notifications")
            return
    except Exception as e:
        print("[notif] gagal minta izin:", e)
        return
    seen, first = set(), True
    while True:
        try:
            items = await listener.get_notifications_async(NotificationKinds.TOAST)
            for n in items:
                if n.id in seen:
                    continue
                seen.add(n.id)
                if first:
                    continue
                app = n.app_info.display_info.display_name
                if ext is not None and "chrome" in app.lower():
                    continue  # sudah ditangani ekstensi
                binding = n.notification.visual.get_binding(KnownNotificationBindings.get_toast_generic())
                texts = [t.text for t in binding.get_text_elements()] if binding else []
                title = texts[0] if texts else app
                body = " ".join(texts[1:])
                await push_notif(app, title, body)
            first = False
        except Exception as e:
            print("[notif] error:", e)
        await asyncio.sleep(1)


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


async def main():
    print("=" * 44)
    print(" ROBO FACE SERVER v2")
    print(" IP PC  :", ", ".join(local_ips()) or "(tidak terdeteksi)")
    print(" Port   :", PORT)
    print(" PIN    :", cfg["pin"])
    print("=" * 44)
    asyncio.create_task(mouse_loop())
    asyncio.create_task(notif_loop())
    asyncio.create_task(screen_loop())
    async with serve(handler, "0.0.0.0", PORT, max_size=8 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
