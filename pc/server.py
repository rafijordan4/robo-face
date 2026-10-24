"""Robo Face - server PC (Windows).

Jalankan:  python server.py
- HP konek lewat WebSocket (port 8765) pakai PIN.
- Ekstensi Chrome konek dari localhost (tanpa PIN).
"""
import asyncio
import ctypes
import json
import os
import random
import re
import socket
import subprocess
import time
import urllib.parse
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
user32 = ctypes.windll.user32 if IS_WIN else None
if IS_WIN:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


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


# ---------- parser perintah suara ----------
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
        # fallback tanpa ekstensi
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


# ---------- handler koneksi ----------
async def from_ext(m):
    t = m.get("t")
    if t == "media":
        state["media"] = bool(m.get("playing"))
        await to_phones({"t": "media", "playing": state["media"]})
    elif t == "password":
        state["password"] = bool(m.get("active"))
        await to_phones({"t": "password", "active": state["password"]})
    elif t == "notif":
        await push_notif(m.get("app", ""), m.get("title", ""), m.get("body", ""), m.get("image") or "")


async def from_phone(m):
    if m.get("t") == "cmd":
        res = await run_command(str(m.get("text", "")))
        await to_phones(res)


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
                await from_phone(m)
    except Exception:
        pass
    finally:
        phones.discard(ws)
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
    print(" ROBO FACE SERVER")
    print(" IP PC  :", ", ".join(local_ips()) or "(tidak terdeteksi)")
    print(" Port   :", PORT)
    print(" PIN    :", cfg["pin"])
    print("=" * 44)
    asyncio.create_task(mouse_loop())
    asyncio.create_task(notif_loop())
    async with serve(handler, "0.0.0.0", PORT, max_size=8 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
