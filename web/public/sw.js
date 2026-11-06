// Service Worker：让网页能“添加到主屏幕”当应用用，接收推送通知，连不上服务器时给一个提示页。
// 不缓存接口和页面内容，数据总是从服务器拿最新的。提示页连不上服务器、不知道设置的语言，所以中英文都写。

const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Super Agent</title><body style="margin:0;height:100vh;display:grid;place-items:center;background:#0d0d0e;color:#a8a8ad;
font-family:-apple-system,'PingFang SC',sans-serif;text-align:center"><div><div style="font-size:32px">◆</div>
<p>连不上服务器 · Can't reach the server</p><p style="font-size:13px">确认电脑上的服务在运行、网络（例如 Tailscale）已连接，然后刷新。<br>
Make sure the server is running and your network (e.g. Tailscale) is connected, then refresh.</p></div>`

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()))

self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, { headers: { 'content-type': 'text/html; charset=utf-8' } })))
})

self.addEventListener('push', e => {
  let d = {}
  try { d = e.data ? e.data.json() : {} } catch { d = { body: e.data && e.data.text() } }
  e.waitUntil(self.registration.showNotification(d.title || 'Super Agent', {
    body: d.body || '', tag: d.tag || undefined, renotify: !!d.tag, data: { url: d.url || '/' },
    icon: '/icon-192.png', badge: '/icon-192.png',
  }))
})

// 点通知：已经开着页面就切过去并打开对应的对话，否则新开一个
self.addEventListener('notificationclick', e => {
  e.notification.close()
  const url = (e.notification.data && e.notification.data.url) || '/'
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    const c = list.find(x => 'focus' in x)
    if (c) { c.postMessage({ type: 'open', url }); return c.focus() }
    return self.clients.openWindow(url)
  }))
})
