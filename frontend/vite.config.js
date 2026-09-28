import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 本地开发/preview：/api 与 /healthz 反代到本地后端（与 nginx 生产行为一致）。
// 后端端口可用 BACKEND_PORT 覆盖（测试用动态端口时注入）。
const backend = `http://127.0.0.1:${process.env.BACKEND_PORT || 8000}`

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': backend,
      '/healthz': backend,
    },
  },
  preview: {
    proxy: {
      '/api': backend,
      '/healthz': backend,
    },
  },
})
