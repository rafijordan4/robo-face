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

from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

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
screen_wants = {}       # ws HP -> (lebar, fps)
screen_dirty = False
last_phone_move = 0.0   # kapan HP terakhir menggerakkan mouse (untuk warna penanda)
audio_ws = None         # halaman penangkap suara di Chrome PC (/audio)
audio_sharing = False   # halaman itu sudah memilih layar + suara sistem
audio_wants = set()     # HP yang mau mendengar suara PC
inflight = {}           # ws HP -> [frame belum diterima, waktu aktivitas terakhir]
held = set()            # tombol mouse yang sedang ditahan dari HP


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
    user32.SetCursorPos.argtypes = [c_int, c_int]
    user32.mouse_event.argtypes = [c_uint32, c_uint32, c_uint32, c_uint32, c_void_p]


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


# ---------- mouse dari HP (menggerakkan mouse Windows asli) ----------
BTN = {"l": (0x0002, 0x0004), "r": (0x0008, 0x0010), "m": (0x0020, 0x0040)}


def pm_move(dx, dy):
    global last_phone_move
    if not IS_WIN:
        return
    dx = max(-400, min(400, int(dx)))
    dy = max(-400, min(400, int(dy)))
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    sw = max(1, user32.GetSystemMetrics(0))
    sh = max(1, user32.GetSystemMetrics(1))
    user32.SetCursorPos(max(0, min(sw - 1, p.x + dx)), max(0, min(sh - 1, p.y + dy)))
    last_phone_move = time.time()


def pm_button(b, action):
    global last_phone_move
    if not IS_WIN or b not in BTN:
        return
    down, up = BTN[b]
    if action == "down":
        user32.mouse_event(down, 0, 0, 0, None)
        held.add(b)
    elif action == "up":
        user32.mouse_event(up, 0, 0, 0, None)
        held.discard(b)
    elif action == "click":
        user32.mouse_event(down, 0, 0, 0, None)
        user32.mouse_event(up, 0, 0, 0, None)
    last_phone_move = time.time()


def pm_wheel(n):
    global last_phone_move
    if not IS_WIN:
        return
    n = max(-10, min(10, int(n)))
    if n:
        user32.mouse_event(0x0800, 0, 0, (n * 120) & 0xFFFFFFFF, None)
        last_phone_move = time.time()


def pm_release_all():
    for b in list(held):
        pm_button(b, "up")


# ---------- layar PC -> HP ----------
# Jalur cepat: GDI + GDI+ (bawaan Windows) perkecil layar lalu encode JPEG, tanpa library tambahan.
# Jalur cadangan: PNG murni Python (lambat, ~2 fps).
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


def capture_png(width, driver="pc"):
    g = grab_screen(width)
    if g is None:
        return None
    data, w, h = g
    mx, my = mouse_pos()
    cx, cy = int(mx * w), int(my * h)
    col = b"\xe5\xe5\x00" if driver == "phone" else b"\x30\x30\xff"   # BGR: cyan = HP, merah = PC
    for yy in range(max(0, cy - 4), min(h, cy + 5)):
        for xx in range(max(0, cx - 4), min(w, cx + 5)):
            o = (yy * w + xx) * 4
            data[o:o + 3] = col
    return bgra_to_png(data, w, h)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _EncParam(ctypes.Structure):
    _fields_ = [("Guid", _GUID), ("NumberOfValues", ctypes.c_uint32),
                ("Type", ctypes.c_uint32), ("Value", ctypes.c_void_p)]


class _EncParams(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EncParam * 1)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_int), ("SuppressExternalCodecs", ctypes.c_int)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _guid(text):
    import uuid
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _com_release(ptr):
    vt = ctypes.cast(ctypes.c_void_p(ptr), ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(ctypes.c_void_p(vt), ctypes.POINTER(ctypes.c_void_p))[2]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(fn)(ptr)


class FastGrabber:
    def __init__(self):
        from ctypes import byref, c_int, c_size_t, c_uint32, c_ulong, c_void_p
        self.gp = ctypes.WinDLL("gdiplus")
        self.ole = ctypes.WinDLL("ole32")
        self.k32 = ctypes.WinDLL("kernel32")
        gp, ole, k = self.gp, self.ole, self.k32
        gp.GdiplusStartup.argtypes = [c_void_p, c_void_p, c_void_p]
        gp.GdipCreateBitmapFromHBITMAP.argtypes = [c_void_p, c_void_p, c_void_p]
        gp.GdipSaveImageToStream.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
        gp.GdipDisposeImage.argtypes = [c_void_p]
        ole.CreateStreamOnHGlobal.argtypes = [c_void_p, c_int, c_void_p]
        ole.GetHGlobalFromStream.argtypes = [c_void_p, c_void_p]
        k.GlobalSize.argtypes = [c_void_p]
        k.GlobalSize.restype = c_size_t
        k.GlobalLock.argtypes = [c_void_p]
        k.GlobalLock.restype = c_void_p
        k.GlobalUnlock.argtypes = [c_void_p]
        user32.FillRect.argtypes = [c_void_p, c_void_p, c_void_p]
        gdi32.CreateSolidBrush.argtypes = [c_uint32]
        gdi32.CreateSolidBrush.restype = c_void_p
        token = c_ulong()
        si = _GdiplusStartupInput(1, None, 0, 0)
        if gp.GdiplusStartup(byref(token), byref(si), None) != 0:
            raise RuntimeError("GdiplusStartup gagal")
        self.jpeg = _guid("557CF401-1A04-11D3-9A73-0000F81EF32E")      # encoder JPEG
        self.qguid = _guid("1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB")     # parameter Quality
        self.brush_w = gdi32.CreateSolidBrush(0xFFFFFF)
        self.brush_pc = gdi32.CreateSolidBrush(0x003030FF)             # merah (BGR)
        self.brush_ph = gdi32.CreateSolidBrush(0x00FFE500)             # cyan
        self.hdc = user32.GetDC(None)
        self.mdc = gdi32.CreateCompatibleDC(self.hdc)
        self.bmp = None
        self.old = None
        self.size = None

    def frame(self, out_w, quality, driver, mx, my):
        from ctypes import byref, c_uint32, c_void_p
        gp, ole, k = self.gp, self.ole, self.k32
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        out_w = max(160, min(out_w, sw))
        out_h = max(90, round(sh * out_w / sw))
        if self.size != (out_w, out_h):
            if self.bmp:
                gdi32.SelectObject(self.mdc, self.old)
                gdi32.DeleteObject(self.bmp)
            self.bmp = gdi32.CreateCompatibleBitmap(self.hdc, out_w, out_h)
            self.old = gdi32.SelectObject(self.mdc, self.bmp)
            gdi32.SetStretchBltMode(self.mdc, 4)
            self.size = (out_w, out_h)
        if not gdi32.StretchBlt(self.mdc, 0, 0, out_w, out_h, self.hdc, 0, 0, sw, sh, 0x00CC0020):
            return None
        cx, cy = int(mx * out_w), int(my * out_h)
        col = self.brush_ph if driver == "phone" else self.brush_pc
        for brush, r in ((self.brush_w, 7), (col, 5)):
            rect = _RECT(cx - r, cy - r, cx + r + 1, cy + r + 1)
            user32.FillRect(self.mdc, byref(rect), brush)
        img = c_void_p()
        if gp.GdipCreateBitmapFromHBITMAP(self.bmp, None, byref(img)) != 0:
            return None
        stream = c_void_p()
        try:
            if ole.CreateStreamOnHGlobal(None, 1, byref(stream)) != 0:
                return None
            q = c_uint32(quality)
            params = _EncParams()
            params.Count = 1
            params.Parameter[0].Guid = self.qguid
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = 4          # EncoderParameterValueTypeLong
            params.Parameter[0].Value = ctypes.addressof(q)
            if gp.GdipSaveImageToStream(img, stream, byref(self.jpeg), byref(params)) != 0:
                return None
            hg = c_void_p()
            ole.GetHGlobalFromStream(stream, byref(hg))
            size = k.GlobalSize(hg)
            ptr = k.GlobalLock(hg)
            data = ctypes.string_at(ptr, size)
            k.GlobalUnlock(hg)
            return data
        finally:
            if stream.value:
                _com_release(stream.value)
            gp.GdipDisposeImage(img)


_fast = None
_fast_failed = False


def capture_frame(width):
    """Satu frame layar (JPEG kalau bisa, kalau tidak PNG)."""
    global _fast, _fast_failed
    driver = "phone" if time.time() - last_phone_move < 1.5 else "pc"
    if IS_WIN and not _fast_failed:
        try:
            if _fast is None:
                _fast = FastGrabber()
                print("[layar] mode cepat JPEG (GDI+) aktif")
            mx, my = mouse_pos()
            data = _fast.frame(width, 60, driver, mx, my)
            if data:
                return data
            raise RuntimeError("frame kosong")
        except Exception as e:
            _fast_failed = True
            print("[layar] mode cepat gagal, pakai PNG (lambat):", e)
    return capture_png(width, driver)


def ready_for_frame(p, v):
    """HP yang mengirim ack dibatasi maksimal 2 frame 'di jalan' supaya tidak ada antrean."""
    if not v[2]:
        return True
    st = inflight.get(p)
    if not st or st[0] < 2:
        return True
    if time.time() - st[1] > 2:          # ack macet 2 detik: reset
        st[0] = 0
        return True
    return False


async def screen_loop():
    global screen_dirty
    last = None
    static = 0
    warned = False
    frames = 0
    sent_bytes = 0
    t_stat = time.perf_counter()
    while True:
        if not screen_wants:
            last, static = None, 0
            await asyncio.sleep(0.3)
            continue
        ready = [p for p, v in screen_wants.items() if ready_for_frame(p, v)]
        if not ready:                    # HP belum selesai menerima frame sebelumnya: jangan tangkap layar
            await asyncio.sleep(0.004)
            continue
        t0 = time.perf_counter()
        width = max(v[0] for v in screen_wants.values())
        fps = max(v[1] for v in screen_wants.values())
        data = await asyncio.to_thread(capture_frame, width)
        if data is None:
            if not warned:
                print("[layar] gagal mengambil layar")
                warned = True
            await asyncio.sleep(2)
            continue
        h = hashlib.md5(data).digest()
        if h != last or screen_dirty:
            last, screen_dirty, static = h, False, 0
            for p in ready:
                try:
                    await p.send(data)
                    if screen_wants.get(p, (0, 0, False))[2]:
                        st = inflight.setdefault(p, [0, time.time()])
                        if st[0] == 0:
                            st[1] = time.time()
                        st[0] += 1
                except Exception:
                    screen_wants.pop(p, None)
            frames += 1
            sent_bytes += len(data)
        else:
            static += 1
        now = time.perf_counter()
        if now - t_stat >= 5:
            if frames:
                print(f"[layar] {frames / (now - t_stat):.0f} fps, {sent_bytes / frames / 1024:.0f} KB/frame, {width}px")
            frames, sent_bytes, t_stat = 0, 0, now
        interval = 1.0 / max(5, min(60, fps))
        if static > 30:
            interval = max(interval, 0.1)   # layar diam: hemat CPU
        await asyncio.sleep(max(0, interval - (time.perf_counter() - t0)))


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


AUDIO_PAGE = r"""<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Suara PC ke HP</title>
<style>body{font:16px system-ui,sans-serif;background:#0b0f1a;color:#eaf0ff;max-width:560px;margin:30px auto;padding:0 16px}
button{font:700 18px system-ui;padding:14px 22px;border:0;border-radius:14px;background:#ffc857;color:#1a1a2e;cursor:pointer}
#st{margin:16px 0;padding:12px 14px;border-radius:12px;background:#1b2236}small{color:#9fb0d8;line-height:1.5}</style>
<h2>Suara PC &rarr; HP</h2>
<p><button id="go">Bagikan suara PC</button></p>
<div id="st">Menyambung ke server&hellip;</div>
<p><small>Di jendela pilihan Chrome: buka tab <b>Entire screen</b>, klik layarnya, lalu <b>centang "Also share system audio"</b>
(bagikan juga audio sistem) dan klik <b>Share</b>. Biarkan tab ini tetap terbuka (boleh diminimalkan).
Suara tetap keluar di speaker/Bluetooth PC seperti biasa; HP hanya ikut mendengar.</small></p>
<script>
const $=id=>document.getElementById(id), test=location.hash==='#test';
let ws=null, want=false, sharing=false, quiet=0, node=null, ctx=null;
function ui(){ $('st').textContent = !ws||ws.readyState!==1 ? 'Tidak tersambung ke server (server.py jalan?)' :
  !sharing ? 'Tersambung. Klik "Bagikan suara PC".' : want ? 'Mengirim suara ke HP…' : 'Siap. Menunggu HP menyalakan suara (tombol speaker di app).'; }
function connect(){
  ws=new WebSocket('ws://'+location.host); ws.binaryType='arraybuffer';
  ws.onopen=()=>{ ws.send(JSON.stringify({t:'hello',role:'audio'})); if(sharing) ws.send(JSON.stringify({t:'audio_status',sharing:true})); ui(); };
  ws.onmessage=e=>{ if(typeof e.data!=='string') return; let m; try{m=JSON.parse(e.data)}catch(_){return} if(m.t==='audio_want'){ want=!!m.on; quiet=0; ui(); } };
  ws.onclose=()=>{ ui(); setTimeout(connect,1500); };
}
const WORKLET=`class P extends AudioWorkletProcessor{constructor(){super();this.b=new Int16Array(1280);this.n=0}
process(i){const x=i[0];if(!x||!x.length)return true;const L=x[0],R=x[1]||x[0];
for(let k=0;k<L.length;k++){const l=Math.max(-1,Math.min(1,L[k])),r=Math.max(-1,Math.min(1,R[k]));
this.b[this.n++]=l<0?l*32768:l*32767;this.b[this.n++]=r<0?r*32768:r*32767;
if(this.n>=this.b.length){this.port.postMessage(this.b.buffer.slice(0));this.n=0}}return true}}
registerProcessor('cap',P)`;
async function start(){
  let stream;
  try{
    if(test){ const c=new AudioContext(); const o=c.createOscillator(); o.frequency.value=440; const d=c.createMediaStreamDestination(); o.connect(d); o.start(); stream=d.stream; }
    else stream=await navigator.mediaDevices.getDisplayMedia({video:{frameRate:1,width:320,height:180},audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
  }catch(e){ $('st').textContent='Dibatalkan: '+e.message; return; }
  const tr=stream.getAudioTracks()[0];
  if(!tr){ stream.getTracks().forEach(t=>t.stop()); $('st').textContent='Tidak ada suara ikut terbagi. Ulangi, pilih Entire screen dan centang "Also share system audio".'; return; }
  stream.getVideoTracks().forEach(v=>v.applyConstraints({frameRate:1,width:160,height:90}).catch(()=>{}));
  ctx=new AudioContext({sampleRate:32000}); await ctx.resume();
  const url=URL.createObjectURL(new Blob([WORKLET],{type:'application/javascript'}));
  await ctx.audioWorklet.addModule(url);
  node=new AudioWorkletNode(ctx,'cap',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2]});
  const mute=ctx.createGain(); mute.gain.value=0;
  ctx.createMediaStreamSource(new MediaStream([tr])).connect(node); node.connect(mute); mute.connect(ctx.destination);
  node.port.onmessage=e=>{
    if(!want||!ws||ws.readyState!==1) return;
    const v=new Int16Array(e.data); let mx=0; for(let i=0;i<v.length;i+=8){const a=Math.abs(v[i]); if(a>mx) mx=a;}
    if(mx<8){ if(++quiet>50) return; } else quiet=0;      // diam >1 detik: berhenti kirim
    ws.send(e.data);
  };
  tr.onended=()=>{ sharing=false; if(ws&&ws.readyState===1) ws.send(JSON.stringify({t:'audio_status',sharing:false})); ui(); };
  sharing=true; if(ws&&ws.readyState===1) ws.send(JSON.stringify({t:'audio_status',sharing:true})); ui();
}
$('go').onclick=start; connect(); ui(); if(test) start();
</script></html>"""


def http_page(connection, request):
    if request.path.split("?")[0] == "/audio":
        body = AUDIO_PAGE.encode("utf-8")
        return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")]), body)
    return None


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


async def push_audio_want():
    if audio_ws is not None:
        try:
            await audio_ws.send(json.dumps({"t": "audio_want", "on": bool(audio_wants)}))
        except Exception:
            pass


async def relay_audio(raw):
    pkt = struct.pack("<BBH", 0xA5, 2, 320) + raw        # penanda 0xA5, stereo, 32000 Hz
    for p in list(audio_wants):
        try:
            await p.send(pkt)
        except Exception:
            audio_wants.discard(p)


async def from_phone(ws, m):
    global halted, screen_dirty
    t = m.get("t")
    if t == "fa":                                        # HP sudah menerima satu frame layar
        st = inflight.get(ws)
        if st:
            st[0] = max(0, st[0] - 1)
            st[1] = time.time()
        return
    if t == "audio":
        if m.get("on"):
            audio_wants.add(ws)
        else:
            audio_wants.discard(ws)
        await push_audio_want()
        await ws.send(json.dumps({"t": "audio_state", "ready": audio_sharing}))
        return
    if t == "cmd":
        await to_phones(await run_command(str(m.get("text", ""))))
    elif t == "tool":
        asyncio.create_task(phone_tool(ws, m))
    elif t == "ai_start":
        halted = False
    elif t == "screen":
        if m.get("on"):
            try:
                w = max(160, min(1280, int(m.get("w", 480))))
                fps = max(5, min(60, int(m.get("fps", 30))))
            except Exception:
                w, fps = 480, 30
            screen_wants[ws] = (w, fps, bool(m.get("ack")))
            screen_dirty = True
        else:
            screen_wants.pop(ws, None)
    elif t == "pm":
        try:
            a = m.get("a")
            if a == "move":
                pm_move(m.get("dx", 0), m.get("dy", 0))
            elif a in ("down", "up", "click"):
                pm_button(str(m.get("b", "l")), a)
            elif a == "wheel":
                pm_wheel(m.get("n", 0))
        except Exception as e:
            print("[mouse]", e)


async def push_notif(app, title, body, image=""):
    key = (title + body).strip().lower()[:200]
    if key and is_dup(key):
        return
    await to_phones({"t": "notif", "app": app, "title": title[:120],
                     "body": body[:300], "image": image})


async def handler(ws):
    global ext, audio_ws, audio_sharing
    ip = ws.remote_address[0] if ws.remote_address else ""
    role = None
    try:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                if role == "audio" and audio_wants:
                    await relay_audio(bytes(raw))
                continue
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if role is None:
                if m.get("t") != "hello":
                    continue
                if m.get("role") == "audio" and ip in ("127.0.0.1", "::1"):
                    role = "audio"
                    audio_ws = ws
                    await ws.send(json.dumps({"t": "audio_want", "on": bool(audio_wants)}))
                elif m.get("role") == "ext" and ip in ("127.0.0.1", "::1"):
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
            elif role == "audio":
                if m.get("t") == "audio_status":
                    audio_sharing = bool(m.get("sharing"))
                    await to_phones({"t": "audio_state", "ready": audio_sharing})
            else:
                await from_phone(ws, m)
    except Exception:
        pass
    finally:
        phones.discard(ws)
        screen_wants.pop(ws, None)
        inflight.pop(ws, None)
        if ws in audio_wants:
            audio_wants.discard(ws)
            await push_audio_want()
        if audio_ws is ws:
            audio_ws = None
            audio_sharing = False
            await to_phones({"t": "audio_state", "ready": False})
        pm_release_all()
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
    if IS_WIN:
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)   # timer 1 ms supaya 60 fps bisa tercapai
        except Exception:
            pass
    asyncio.create_task(mouse_loop())
    asyncio.create_task(notif_loop())
    asyncio.create_task(screen_loop())
    async with serve(handler, "0.0.0.0", PORT, max_size=8 * 1024 * 1024, compression=None, process_request=http_page):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
