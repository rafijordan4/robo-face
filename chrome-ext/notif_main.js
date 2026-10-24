// Jalan di world "MAIN": bungkus Notification supaya isi notif (teks + gambar) bisa ditangkap.
(() => {
  const Orig = window.Notification;
  if (!Orig || Orig.__rf) return;

  const report = (title, opts) => {
    try {
      if (Orig.permission !== "granted") return;
      window.postMessage({
        __rf: "notif",
        title: String(title || ""),
        body: String((opts && opts.body) || ""),
        image: String((opts && opts.image) || ""),
      }, "*");
    } catch (e) {}
  };

  function Wrapped(title, opts) {
    report(title, opts);
    return new Orig(title, opts);
  }
  Wrapped.prototype = Orig.prototype;
  Object.defineProperty(Wrapped, "permission", { get: () => Orig.permission });
  Object.defineProperty(Wrapped, "maxActions", { get: () => Orig.maxActions });
  Wrapped.requestPermission = (...a) => Orig.requestPermission(...a);
  Wrapped.__rf = true;
  window.Notification = Wrapped;

  try {
    const proto = window.ServiceWorkerRegistration && ServiceWorkerRegistration.prototype;
    if (proto && proto.showNotification) {
      const orig = proto.showNotification;
      proto.showNotification = function (title, opts) {
        report(title, opts);
        return orig.call(this, title, opts);
      };
    }
  } catch (e) {}
})();
