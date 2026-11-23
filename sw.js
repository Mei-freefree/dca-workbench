/* 定投工作台 service worker —— 让「添加到主屏幕」后以独立 App 形式打开，并支持离线看缓存 */
const CACHE = "dca-workbench-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-180.png",
  "./icon-192.png",
  "./icon-512.png"
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 只管本站资源

  // 页面与数据：先要网络（保证看到最新），失败再用缓存
  const freshFirst = req.mode === "navigate" || /data\.json$/.test(url.pathname);
  if (freshFirst) {
    e.respondWith(
      fetch(req, { cache: "no-store" })
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req.mode === "navigate" ? "./index.html" : req, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(req.mode === "navigate" ? "./index.html" : req))
    );
    return;
  }

  // 其他静态资源：缓存优先
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
      return res;
    }))
  );
});
