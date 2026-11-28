import { createHash } from 'node:crypto'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// 这一版界面的编号：所有产物的文件名（带内容哈希）合起来算个摘要，写进 index.html 的 <meta name="osa-build">。
// 服务端读它告诉开着的页面服务器上现在是哪一版，页面的和它不一样就提示更新（osa/server.py 的 web_build、web/src/store.ts）。
// 开发服务器里没有这个编号，不检查更新
const buildId = (): Plugin => ({
  name: 'osa-build',
  transformIndexHtml: {
    order: 'post',
    handler(html, ctx) {
      if (!ctx.bundle) return html
      const id = createHash('sha256').update(Object.keys(ctx.bundle).sort().join('\n')).digest('hex').slice(0, 12)
      return { html, tags: [{ tag: 'meta', attrs: { name: 'osa-build', content: id }, injectTo: 'head' }] }
    },
  },
})

// 构建产物放进 osa/static/dist，由服务直接提供；开发时把接口代理到 8765。
// 服务开在别的端口（比如拿一份演示数据另起一个）时用 OSA_API 指过去：OSA_API=http://127.0.0.1:8793 npx vite --port 5180
const api = process.env.OSA_API || 'http://127.0.0.1:8765'
export default defineConfig({
  plugins: [react(), buildId()],
  build: { outDir: '../osa/static/dist', emptyOutDir: true },
  server: { proxy: { '/api': api, '/ws': { target: api.replace(/^http/, 'ws'), ws: true } } },
})
