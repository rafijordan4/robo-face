// Content script (isolated world): relay notif + deteksi fokus kolom password.
function safeSend(msg) {
  try { chrome.runtime.sendMessage(msg); } catch (e) {}
}

window.addEventListener("message", (e) => {
  const d = e.data;
  if (e.source !== window || !d || d.__rf !== "notif") return;
  const send = (img) => safeSend({ t: "notif", app: location.hostname, title: d.title, body: d.body, image: img });
  if (d.image && d.image.startsWith("blob:")) {
    fetch(d.image).then((r) => r.blob()).then((b) => {
      const fr = new FileReader();
      fr.onload = () => send(fr.result);
      fr.readAsDataURL(b);
    }).catch(() => send(""));
  } else {
    send(d.image || "");
  }
});

let pwdActive = false;
function checkPwd() {
  const a = document.activeElement;
  const on = !!(a && a.tagName === "INPUT" && (a.type || "").toLowerCase() === "password");
  if (on !== pwdActive) {
    pwdActive = on;
    safeSend({ t: "password", active: on });
  }
}
document.addEventListener("focusin", checkPwd, true);
document.addEventListener("focusout", () => setTimeout(checkPwd, 0), true);
