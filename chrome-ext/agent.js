// Agen browser: di-inject on-demand oleh background.js ke tab yang dikendalikan AI.
// Aturan keamanan ada di sini: tidak mengisi password/kartu/OTP, tidak klik link
// berbahaya/download, aksi sensitif butuh konfirmasi user, kursor AI hanya overlay
// (tidak menggerakkan mouse Windows asli).
(() => {
  if (window.__rfAgent) return;
  window.__rfAgent = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const map = new Map();
  let nextId = 1;

  const SENSITIVE_CLICK = /(bayar|beli sekarang|checkout|purchase|buy now|\bpay\b|place order|pesan sekarang|delete|hapus|remove account|transfer|kirim uang|send money|sign out|log ?out|keluar akun|unsubscribe|konfirmasi pembayaran|subscribe now|berlangganan sekarang)/i;
  const BAD_INPUT = /(password|passwd|kata ?sandi|\bsandi\b|cvv|cvc|card ?number|nomor kartu|kartu kredit|credit|\botp\b|\bpin\b|\bssn\b|\bnik\b|verification code|kode verifikasi)/i;

  function inputBlocked(el) {
    const type = (el.type || "").toLowerCase();
    if (type === "password") return true;
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (/cc-|one-time-code|current-password|new-password/.test(ac)) return true;
    const meta = [el.name, el.id, el.placeholder, el.getAttribute("aria-label")].join(" ");
    return BAD_INPUT.test(meta);
  }

  function badLink(el) {
    const a = el.closest && el.closest("a");
    if (!a) return false;
    const h = a.getAttribute("href") || "";
    if (/^\s*(javascript|data|file|blob|vbscript):/i.test(h)) return true;
    if (a.hasAttribute("download")) return true;
    try {
      const p = new URL(h, location.href).pathname;
      if (/\.(exe|msi|bat|cmd|ps1|scr|vbs|jar|apk|dmg|pkg|zip|rar|7z|iso)$/i.test(p)) return true;
    } catch (e) {}
    return false;
  }

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && +s.opacity > 0.05;
  }

  function labelOf(el) {
    let t = el.getAttribute("aria-label") || el.getAttribute("title") || "";
    if (!t) t = (el.innerText || el.textContent || "").trim();
    if (!t) t = el.getAttribute("placeholder") || el.getAttribute("alt") || "";
    if (!t && el.type !== "password") t = el.value || "";
    if (!t && el.querySelector) {
      const img = el.querySelector("img[alt]");
      if (img) t = img.alt;
    }
    return String(t).replace(/\s+/g, " ").trim().slice(0, 90);
  }

  // ---------- kursor AI kuning ----------
  let host = null, root = null, cur = null, cx = null, hideT = null;

  function ensureUI() {
    if (!host || !host.isConnected) {
      host = document.createElement("div");
      host.style.cssText = "all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;";
      root = host.attachShadow({ mode: "closed" });
      root.innerHTML = `<style>
        .cur{position:fixed;left:0;top:0;width:28px;height:28px;margin:-14px 0 0 -14px;border-radius:50%;
          background:rgba(255,214,10,.95);box-shadow:0 0 0 3px rgba(0,0,0,.35),0 0 20px 7px rgba(255,214,10,.65);
          transition:transform .55s cubic-bezier(.22,1,.36,1);pointer-events:none;display:flex;
          align-items:center;justify-content:center;font:800 10px system-ui,sans-serif;color:#3a2f00}
        .rip{position:fixed;width:20px;height:20px;margin:-10px 0 0 -10px;border-radius:50%;
          border:3px solid #ffd60a;pointer-events:none;animation:rip .6s ease-out forwards}
        @keyframes rip{from{transform:scale(.5);opacity:1}to{transform:scale(3.2);opacity:0}}
        .stop{position:fixed;right:14px;bottom:14px;pointer-events:auto;border:0;border-radius:999px;
          padding:9px 16px;background:#e5383b;color:#fff;font:700 13px system-ui,sans-serif;
          box-shadow:0 4px 16px rgba(0,0,0,.45);cursor:pointer}
      </style><div class="cur">AI</div><button class="stop">■ Stop AI</button>`;
      cur = root.querySelector(".cur");
      root.querySelector(".stop").addEventListener("click", () => {
        try { chrome.runtime.sendMessage({ t: "ai_stop" }); } catch (e) {}
        const b = root.querySelector(".stop");
        b.textContent = "Dihentikan";
        setTimeout(() => { host.style.display = "none"; }, 1200);
      });
      (document.body || document.documentElement).appendChild(host);
      cx = null;
    }
    host.style.display = "block";
    clearTimeout(hideT);
    hideT = setTimeout(() => { if (host) host.style.display = "none"; }, 9000);
  }

  async function moveTo(x, y) {
    ensureUI();
    if (cx === null) {
      cur.style.transition = "none";
      cur.style.transform = `translate(${innerWidth - 60}px,${innerHeight - 60}px)`;
      void cur.offsetWidth;
      cur.style.transition = "";
    }
    cx = x;
    cur.style.transform = `translate(${x}px,${y}px)`;
    await sleep(620);
  }

  function ripple(x, y) {
    const d = document.createElement("div");
    d.className = "rip";
    d.style.left = x + "px";
    d.style.top = y + "px";
    root.appendChild(d);
    setTimeout(() => d.remove(), 700);
  }

  // ---------- tool ----------
  function snapshot() {
    map.clear();
    nextId = 1;
    const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[contenteditable=""],[contenteditable="true"],summary';
    const out = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(sel)) {
      if (out.length >= 60) break;
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.bottom < -150 || r.top > innerHeight * 1.6) continue;
      const tag = el.tagName.toLowerCase();
      const isInput = tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
      const label = labelOf(el);
      if (!label && !isInput) continue;
      let href = "";
      const a = el.closest("a[href]");
      if (a) {
        try {
          const u = new URL(a.href);
          href = u.origin === location.origin ? (u.pathname + u.search).slice(0, 70) : u.host + u.pathname.slice(0, 40);
        } catch (e) {}
      }
      const key = tag + "|" + label + "|" + href;
      if (seen.has(key)) continue;
      seen.add(key);
      const id = nextId++;
      map.set(id, el);
      const role = el.getAttribute("role");
      const kind = tag === "a" ? "link"
        : (tag === "button" || role === "button") ? "button"
        : isInput ? "input:" + (tag === "input" ? (el.type || "text") : tag)
        : (role || tag);
      let line = `[${id}] ${kind} "${label}"`;
      if (href) line += ` -> ${href}`;
      if (isInput) {
        if (inputBlocked(el)) line += " [DIBLOKIR: sensitif, jangan diisi]";
        else if (el.value) line += ` value="${String(el.value).slice(0, 40)}"`;
      }
      out.push(line);
    }
    const text = ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").trim().slice(0, 500);
    return {
      url: location.href, title: document.title, elements: out.join("\n"), page_text: text,
      scroll: { y: Math.round(scrollY), max: Math.max(0, Math.round(document.documentElement.scrollHeight - innerHeight)) },
    };
  }

  function getEl(id) {
    const el = map.get(Number(id));
    if (!el || !el.isConnected) return null;
    return el;
  }

  async function submitFrom(el) {
    const fire = (t) => el.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    const before = location.href;
    const handled = !fire("keydown");
    fire("keypress");
    fire("keyup");
    await sleep(700);
    if (handled || location.href !== before) return;
    const form = el.form || el.closest("form");
    if (form) {
      try { form.requestSubmit(); return; } catch (e) {}
    }
    for (const b of document.querySelectorAll("button,[role=button]")) {
      const l = (b.getAttribute("aria-label") || b.innerText || "").toLowerCase();
      if (/search|cari|telusuri/.test(l) && visible(b)) { b.click(); return; }
    }
  }

  async function run(name, a) {
    if (name === "snapshot") return { res: snapshot() };

    if (name === "click") {
      const el = getEl(a.id);
      if (!el) return { res: { error: "Elemen sudah berubah. Panggil snapshot lagi." } };
      if (badLink(el)) return { res: { error: "Link/aksi download atau skrip diblokir demi keamanan." } };
      const label = labelOf(el);
      if (SENSITIVE_CLICK.test(label) && !a.confirm) {
        return { res: { error: `BUTUH_KONFIRMASI: aksi sensitif "${label}". Tanya user dulu; kalau setuju panggil lagi dengan confirm=true.` } };
      }
      el.scrollIntoView({ block: "center", behavior: "instant" });
      await sleep(150);
      const r = el.getBoundingClientRect();
      await moveTo(r.left + r.width / 2, r.top + r.height / 2);
      ripple(r.left + r.width / 2, r.top + r.height / 2);
      return { res: { ok: true, clicked: label }, after: () => { try { el.focus && el.focus(); } catch (e) {} el.click(); } };
    }

    if (name === "type_text") {
      const el = getEl(a.id);
      if (!el) return { res: { error: "Elemen sudah berubah. Panggil snapshot lagi." } };
      const tag = el.tagName.toLowerCase();
      if (!(tag === "input" || tag === "textarea" || el.isContentEditable)) {
        return { res: { error: "Elemen itu bukan kotak ketik." } };
      }
      if (inputBlocked(el)) return { res: { error: "DIBLOKIR: kolom sensitif (password/OTP/PIN/kartu). Minta user mengisi sendiri." } };
      const text = String(a.text || "").slice(0, 400);
      el.scrollIntoView({ block: "center", behavior: "instant" });
      await sleep(150);
      const r = el.getBoundingClientRect();
      await moveTo(r.left + Math.min(r.width / 2, 120), r.top + r.height / 2);
      ripple(r.left + Math.min(r.width / 2, 120), r.top + r.height / 2);
      el.focus();
      const setVal = (v) => {
        const proto = Object.getPrototypeOf(el);
        const d = Object.getOwnPropertyDescriptor(proto, "value");
        if (d && d.set) d.set.call(el, v); else el.value = v;
      };
      const editable = el.isContentEditable;
      if (editable) {
        document.execCommand("selectAll", false, null);
        document.execCommand("delete", false, null);
      } else {
        setVal("");
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const delay = text.length > 80 ? 8 : 30;
      for (const ch of text) {
        if (editable) document.execCommand("insertText", false, ch);
        else {
          setVal(el.value + ch);
          el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
        }
        await sleep(delay);
      }
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { res: { ok: true, typed: text.length + " huruf", submitted: !!a.submit }, after: a.submit ? () => submitFrom(el) : null };
    }

    if (name === "scroll") {
      const dir = a.direction === "up" ? -1 : 1;
      const amt = Math.max(0.1, Math.min(3, Number(a.amount) || 0.8));
      await moveTo(innerWidth * 0.7, innerHeight * 0.55);
      scrollBy({ top: dir * innerHeight * amt, behavior: "smooth" });
      await sleep(600);
      const max = Math.max(0, document.documentElement.scrollHeight - innerHeight);
      return { res: { ok: true, scrollY: Math.round(scrollY), at_bottom: scrollY >= max - 4, at_top: scrollY <= 4 } };
    }

    if (name === "media") {
      let best = null, area = 0;
      for (const v of document.querySelectorAll("video,audio")) {
        const r = v.getBoundingClientRect();
        const ar = r.width * r.height + (v.paused ? 0 : 1e6);
        if (ar >= area) { best = v; area = ar; }
      }
      if (!best) return { res: { error: "Tidak ada video/audio di halaman ini." } };
      const act = a.action;
      const sec = Math.max(1, Math.min(600, Number(a.seconds) || 10));
      if (act === "play") await best.play().catch(() => {});
      else if (act === "pause") best.pause();
      else if (act === "toggle") { if (best.paused) await best.play().catch(() => {}); else best.pause(); }
      else if (act === "mute") best.muted = true;
      else if (act === "unmute") best.muted = false;
      else if (act === "forward") best.currentTime = Math.min(best.duration || 1e9, best.currentTime + sec);
      else if (act === "back") best.currentTime = Math.max(0, best.currentTime - sec);
      else return { res: { error: "Aksi media tidak dikenal." } };
      return { res: { ok: true, paused: best.paused, muted: best.muted, time: Math.round(best.currentTime), duration: Math.round(best.duration || 0) } };
    }

    return { res: { error: "Tool agen tidak dikenal: " + name } };
  }

  chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
    if (!m || m.t !== "agent") return;
    (async () => {
      try {
        const r = await run(m.name, m.args || {});
        sendResponse(r.res);
        if (r.after) setTimeout(() => { try { r.after(); } catch (e) {} }, 80);
      } catch (e) {
        try { sendResponse({ error: String((e && e.message) || e) }); } catch (_) {}
      }
    })();
    return true;
  });
})();
