// Service worker: sambung ke server PC (ws://127.0.0.1:8765), kirim status, jalankan perintah + tool AI.
let ws = null;
const audibleTabs = new Set();
const pwdFrames = new Set(); // "tabId:frameId"
let mediaTimer = null;
let lastMedia = null;
let aiTabId = null;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function send(o) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(o));
}

function connect() {
  if (ws && ws.readyState <= 1) return;
  try { ws = new WebSocket("ws://127.0.0.1:8765"); } catch (e) { return; }
  ws.onopen = () => {
    send({ t: "hello", role: "ext" });
    lastMedia = null;
    pushMedia(true);
    send({ t: "password", active: pwdFrames.size > 0 });
  };
  ws.onmessage = async (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.t === "ext_cmd") runCmd(m.action, m.url);
    else if (m.t === "ext_tool") {
      let result;
      try { result = await runTool(m.name, m.args || {}); }
      catch (e) { result = { error: String((e && e.message) || e) }; }
      send({ t: "tool_result", id: m.id, result });
    }
  };
  ws.onclose = () => { ws = null; setTimeout(connect, 2000); };
  ws.onerror = () => {};
}

function pushMedia(now) {
  clearTimeout(mediaTimer);
  const playing = audibleTabs.size > 0;
  const fire = () => {
    const p = audibleTabs.size > 0;
    if (p !== lastMedia) { lastMedia = p; send({ t: "media", playing: p }); }
  };
  // matikan dengan jeda 1.5 dtk supaya tidak kedip saat video buffering
  if (now || playing) fire(); else mediaTimer = setTimeout(fire, 1500);
}

async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return t;
}

// ---------- perintah suara sederhana (tanpa Gemini) ----------
async function runCmd(action, url) {
  try {
    if (action === "new_tab" || action === "open_url") {
      const tab = await chrome.tabs.create({ url: url || "chrome://newtab" });
      chrome.windows.update(tab.windowId, { focused: true });
      return;
    }
    const t = await activeTab();
    if (!t) return;
    if (action === "close_tab") chrome.tabs.remove(t.id);
    else if (action === "reload") chrome.tabs.reload(t.id);
    else if (action === "back") chrome.tabs.goBack(t.id);
    else if (action === "next_tab" || action === "prev_tab") {
      const tabs = await chrome.tabs.query({ windowId: t.windowId });
      tabs.sort((a, b) => a.index - b.index);
      const i = tabs.findIndex((x) => x.id === t.id);
      const d = action === "next_tab" ? 1 : -1;
      const n = tabs[(i + d + tabs.length) % tabs.length];
      chrome.tabs.update(n.id, { active: true });
    }
  } catch (e) {}
}

// ---------- tool AI ----------
function safeUrl(u) {
  u = String(u || "").trim();
  if (!u) return null;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(u)) u = "https://" + u;
  try {
    const p = new URL(u);
    if (p.protocol !== "http:" && p.protocol !== "https:") return null;
    return p.href;
  } catch (e) { return null; }
}

async function waitComplete(tabId, max) {
  const end = Date.now() + max;
  while (Date.now() < end) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t.status === "complete") return;
    } catch (e) { return; }
    await sleep(150);
  }
}

async function targetTab() {
  if (aiTabId != null) {
    try { return await chrome.tabs.get(aiTabId); } catch (e) { aiTabId = null; }
  }
  const t = await activeTab();
  if (t) aiTabId = t.id;
  return t;
}

async function agent(name, args) {
  const t = await targetTab();
  if (!t) return { error: "Tidak ada tab Chrome. Pakai open_url dulu." };
  if (!/^https?:/i.test(t.url || "")) {
    return { error: "Tab ini halaman khusus (" + (t.url || "kosong") + "). Pakai open_url untuk membuka situs dulu." };
  }
  try {
    await chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["agent.js"] });
    const res = await chrome.tabs.sendMessage(t.id, { t: "agent", name, args }, { frameId: 0 });
    if (res && !res.error && (name === "click" || name === "type_text")) {
      await sleep(1500);
      await waitComplete(t.id, 4000);
      try { const t2 = await chrome.tabs.get(t.id); res.page = { url: t2.url, title: t2.title }; } catch (e) {}
    }
    return res || { error: "Tidak ada respons dari halaman" };
  } catch (e) {
    return { error: "Gagal mengendalikan halaman: " + String((e && e.message) || e) };
  }
}

async function runTool(name, a) {
  if (name === "open_url") {
    const url = safeUrl(a.url);
    if (!url) return { error: "URL tidak valid atau tidak diizinkan (hanya http/https)." };
    let tab = null;
    if (!a.new_tab && aiTabId != null) {
      try { tab = await chrome.tabs.update(aiTabId, { url, active: true }); } catch (e) { tab = null; }
    }
    if (!tab) tab = await chrome.tabs.create({ url, active: true });
    aiTabId = tab.id;
    try { chrome.windows.update(tab.windowId, { focused: true }); } catch (e) {}
    await sleep(600);
    await waitComplete(tab.id, 8000);
    const t2 = await chrome.tabs.get(tab.id);
    return { ok: true, url: t2.url, title: t2.title };
  }

  if (name === "browser_nav") {
    const act = a.action;
    if (act === "new_tab") {
      const tab = await chrome.tabs.create({ active: true });
      aiTabId = tab.id;
      try { chrome.windows.update(tab.windowId, { focused: true }); } catch (e) {}
      return { ok: true, note: "Tab kosong dibuka. Pakai open_url untuk isi situs." };
    }
    const t = await targetTab();
    if (!t) return { error: "Tidak ada tab." };
    if (act === "back") await chrome.tabs.goBack(t.id);
    else if (act === "forward") await chrome.tabs.goForward(t.id);
    else if (act === "reload") await chrome.tabs.reload(t.id);
    else if (act === "close_tab") { await chrome.tabs.remove(t.id); aiTabId = null; return { ok: true }; }
    else if (act === "next_tab" || act === "prev_tab") {
      const tabs = (await chrome.tabs.query({ windowId: t.windowId })).sort((x, y) => x.index - y.index);
      const i = tabs.findIndex((x) => x.id === t.id);
      const n = tabs[(i + (act === "next_tab" ? 1 : -1) + tabs.length) % tabs.length];
      await chrome.tabs.update(n.id, { active: true });
      aiTabId = n.id;
      return { ok: true, title: n.title, url: n.url };
    } else return { error: "Aksi navigasi tidak dikenal." };
    await waitComplete(t.id, 6000);
    const t2 = await chrome.tabs.get(t.id);
    return { ok: true, url: t2.url, title: t2.title };
  }

  if (name === "list_tabs") {
    const tabs = await chrome.tabs.query({ currentWindow: true });
    return { tabs: tabs.slice(0, 20).map((t) => ({ id: t.id, title: (t.title || "").slice(0, 60), url: (t.url || "").slice(0, 80), active: t.active, ai: t.id === aiTabId })) };
  }

  if (name === "focus_tab") {
    try {
      const t = await chrome.tabs.update(Number(a.tab_id), { active: true });
      aiTabId = t.id;
      return { ok: true, title: t.title };
    } catch (e) { return { error: "Tab tidak ditemukan." }; }
  }

  if (["snapshot", "click", "type_text", "scroll", "media"].includes(name)) return agent(name, a);
  return { error: "Tool tidak dikenal: " + name };
}

// ---------- status ----------
chrome.tabs.onUpdated.addListener((id, ci) => {
  if ("audible" in ci) {
    if (ci.audible) audibleTabs.add(id); else audibleTabs.delete(id);
    pushMedia(false);
  }
  if (ci.status === "loading") clearPwd(id);
});
chrome.tabs.onRemoved.addListener((id) => {
  audibleTabs.delete(id);
  pushMedia(false);
  clearPwd(id);
  if (id === aiTabId) aiTabId = null;
});

function clearPwd(tabId) {
  let changed = false;
  for (const k of [...pwdFrames]) {
    if (k.startsWith(tabId + ":")) { pwdFrames.delete(k); changed = true; }
  }
  if (changed) send({ t: "password", active: pwdFrames.size > 0 });
}

chrome.runtime.onMessage.addListener((m, sender) => {
  if (m.t === "password" && sender.tab) {
    const k = sender.tab.id + ":" + (sender.frameId || 0);
    if (m.active) pwdFrames.add(k); else pwdFrames.delete(k);
    send({ t: "password", active: pwdFrames.size > 0 });
  } else if (m.t === "notif") {
    handleNotif(m);
  } else if (m.t === "ai_stop") {
    send({ t: "ai_stop" });
  }
});

async function toDataUrl(url) {
  if (!url) return "";
  try {
    const r = await fetch(url);
    const b = await r.blob();
    const bmp = await createImageBitmap(b);
    const s = Math.min(1, 480 / Math.max(bmp.width, bmp.height));
    const c = new OffscreenCanvas(Math.max(1, Math.round(bmp.width * s)), Math.max(1, Math.round(bmp.height * s)));
    const g = c.getContext("2d");
    g.fillStyle = "#fff";
    g.fillRect(0, 0, c.width, c.height);
    g.drawImage(bmp, 0, 0, c.width, c.height);
    const ob = await c.convertToBlob({ type: "image/jpeg", quality: 0.75 });
    const buf = new Uint8Array(await ob.arrayBuffer());
    let bin = "";
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return "data:image/jpeg;base64," + btoa(bin);
  } catch (e) {
    return "";
  }
}

async function handleNotif(m) {
  const image = await toDataUrl(m.image);
  send({ t: "notif", app: m.app, title: m.title, body: m.body, image });
}

// seed tab yang sedang bunyi + jaga koneksi tetap hidup
chrome.tabs.query({ audible: true }).then((ts) => { ts.forEach((t) => audibleTabs.add(t.id)); });
chrome.alarms.create("keep", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { if (!ws || ws.readyState > 1) connect(); });
connect();
