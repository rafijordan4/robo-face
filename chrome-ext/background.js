// Service worker: sambung ke server PC (ws://127.0.0.1:8765), kirim status, jalankan perintah.
let ws = null;
const audibleTabs = new Set();
const pwdFrames = new Set(); // "tabId:frameId"
let mediaTimer = null;
let lastMedia = null;

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
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.t === "ext_cmd") runCmd(m.action, m.url);
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
