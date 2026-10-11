import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 本地开发/preview：/api 与 /healthz 反代到后端（与 nginx 生产行为一致）。
// 目标优先级：BACKEND_URL 环境变量（完整 URL，任意后端）
//   > --mode prod-backend（转发生产环境，配 npm run preview:prod，本地过目常用）
//   > 本机 127.0.0.1:BACKEND_PORT || 8000（默认本地联调；E2E 注入动态端口走这里）。
export default defineConfig(({ mode }) => {
  const backend = process.env.BACKEND_URL
    || (mode === 'prod-backend' ? 'http://nas.example.com:28080' : null)
    || `http://127.0.0.1:${process.env.BACKEND_PORT || 8000}`
  return {
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
  }
})
