<script setup>
import { computed, onMounted, ref } from 'vue'

const backend = ref({ status: 'checking' })   // 后端进程本身
const infra = ref(null)                       // PostgreSQL / 代理池
const crawl = ref(null)                       // 爬虫数据统计
const loading = ref(false)

async function refresh() {
  loading.value = true
  try {
    const [hz, inf, cr] = await Promise.all([
      fetch('/healthz').then(r => r.json()),
      fetch('/api/infra').then(r => r.json()),
      fetch('/api/crawl/stats').then(r => r.json()).catch(() => null),
    ])
    backend.value = hz
    infra.value = inf
    crawl.value = cr
  } catch (e) {
    backend.value = { status: 'down', detail: String(e) }
  } finally {
    loading.value = false
  }
}

const fmt = n => (n ?? 0).toLocaleString('zh-CN')
const coverPct = computed(() => {
  const c = crawl.value
  if (!c || !c.stocks_listed) return 0
  return Math.min(100, Math.round((c.stocks_covered / c.stocks_listed) * 100))
})
const lastRunText = computed(() => {
  const r = crawl.value?.last_run
  if (!r) return '尚未运行'
  const t = r.started_at ? new Date(r.started_at).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }) : '?'
  return `${t} · ${r.mode === 'full' ? '全量' : '增量'} · 成 ${r.ok_stocks ?? 0} / 败 ${r.failed_stocks ?? 0}`
})

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

      <div class="card" v-if="crawl">
        <div class="name">爬虫数据</div>
        <div class="dot" :class="crawl.status === 'up' ? 'up' : 'down'"></div>
        <div class="state" v-if="crawl.status === 'down'">未连接</div>
        <template v-else>
          <div class="state" v-if="!crawl.initialized">爬虫未运行</div>
          <div class="state" v-else>{{ fmt(crawl.daily_bars) }} 根K线</div>
          <div class="bar" v-if="crawl.initialized">
            <div class="bar-fill" :style="{ width: coverPct + '%' }"></div>
          </div>
          <div class="detail" v-if="crawl.initialized">
            已爬 {{ fmt(crawl.stocks_covered) }} / {{ fmt(crawl.stocks_listed) }} 只 ·
            待爬 {{ fmt(crawl.stocks_pending) }} 只（{{ coverPct }}%）
          </div>
          <div class="detail" v-else>运行爬虫后开始积累日线数据</div>
          <div class="detail">最近运行：{{ lastRunText }}</div>
        </template>
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
.bar { height: 8px; border-radius: 4px; background: #eef1f4; margin: 10px 0 8px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #34d399, #059669);
            transition: width .4s ease; }
button { margin-top: 24px; padding: 10px 24px; border: 1px solid #d1d5db; background: #fff;
         border-radius: 8px; font-size: 14px; cursor: pointer; }
button:hover { background: #f3f4f6; }
</style>
