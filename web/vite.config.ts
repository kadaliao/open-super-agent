import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建产物放进 osa/static/dist，由 FastAPI 直接提供；开发时把接口代理到 8765
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../osa/static/dist', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8765', '/ws': { target: 'ws://127.0.0.1:8765', ws: true } } },
})
