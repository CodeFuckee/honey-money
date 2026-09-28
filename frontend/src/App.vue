<script setup>
import { onMounted, ref } from 'vue'

const backend = ref({ status: 'checking' })   // 后端进程本身
const infra = ref(null)                       // PostgreSQL / 代理池
const loading = ref(false)

async function refresh() {
  loading.value = true
  try {
    const [hz, inf] = await Promise.all([
      fetch('/healthz').then(r => r.json()),
      fetch('/api/infra').then(r => r.json()),
    ])
    backend.value = hz
    infra.value = inf
  } catch (e) {
    backend.value = { status: 'down', detail: String(e) }
  } finally {
    loading.value = false
  }
}
onMounted(refresh)
</script>

<template>
  <main class="page">
    <header>
      <h1>honey-money</h1>
      <p class="sub">工程化骨架 · 服务状态仪表盘</p>
    </header>

    <section class="cards">
      <div class="card">
        <div class="name">后端 API</div>
        <div class="dot" :class="backend.status === 'ok' ? 'up' : 'down'"></div>
        <div class="state">{{ backend.status === 'ok' ? '运行中' : '不可用' }}</div>
        <div class="detail">v{{ backend.version || '?' }}</div>
      </div>

      <div class="card" v-if="infra">
        <div class="name">PostgreSQL</div>
        <div class="dot" :class="infra.postgres.status"></div>
        <div class="state">{{ infra.postgres.status === 'up' ? '已连接' : '未连接' }}</div>
        <div class="detail">{{ infra.postgres.detail }}</div>
      </div>

      <div class="card" v-if="infra">
        <div class="name">代理池</div>
        <div class="dot" :class="infra.proxy_pool.status"></div>
        <div class="state">{{ infra.proxy_pool.status === 'up' ? '已连接' : '未连接' }}</div>
        <div class="detail">{{ infra.proxy_pool.detail }}</div>
      </div>
    </section>

    <button @click="refresh" :disabled="loading">
      {{ loading ? '检查中…' : '重新检测' }}
    </button>
  </main>
</template>

<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #f5f6f8; }
.page { max-width: 860px; margin: 0 auto; padding: 40px 20px; }
h1 { margin: 0 0 4px; font-size: 26px; }
.sub { margin: 0 0 28px; color: #6b7280; font-size: 14px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 16px; }
.card { background: #fff; border: 1px solid #e3e6ea; border-radius: 12px; padding: 18px; }
.name { font-size: 13px; color: #6b7280; }
.state { font-size: 20px; font-weight: 700; margin: 8px 0 6px; }
.detail { font-size: 12px; color: #9ca3af; word-break: break-all; min-height: 16px; }
.dot { width: 12px; height: 12px; border-radius: 50%; float: right; margin-top: 2px; }
.dot.up, .dot.ok { background: #22c55e; box-shadow: 0 0 6px #22c55e88; }
.dot.down { background: #ef4444; }
button { margin-top: 24px; padding: 10px 24px; border: 1px solid #d1d5db; background: #fff;
         border-radius: 8px; font-size: 14px; cursor: pointer; }
button:hover { background: #f3f4f6; }
</style>
