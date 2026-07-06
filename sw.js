/* Service Worker：网络优先、缓存兜底 —— 有网拿最新，没网(地铁/断网)还能看上一次的结果 */
const CACHE = 'zhanfa-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (/data_[A-Z]+\.json$/.test(url.pathname)) return;   // 全量数据太大，不进缓存
  e.respondWith(
    fetch(req).then(resp => {
      const copy = resp.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return resp;
    }).catch(() => caches.match(req))
  );
});
