<script setup>
import { computed, onMounted, ref } from 'vue'

// ===== 数据层（沿用原仪表盘逻辑：三接口并发 + 优雅降级）=====
const backend = ref({ status: 'checking' })   // 后端进程本身
const infra = ref(null)                       // PostgreSQL / 代理池
const crawl = ref(null)                       // 爬虫数据统计
const loading = ref(false)
const activeNav = ref('dash')                 // dash | data | backtest | learn

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
const backendDown = computed(() => backend.value.status === 'down')
const checking = computed(() => backend.value.status === 'checking' || loading.value)
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
  const src = { sina: '新浪', eastmoney: '东财' }[r.source] || r.source || ''
  return `${t} · ${src} · ${r.mode === 'full' ? '全量' : '增量'} · 成 ${fmt(r.ok_stocks)} / 败 ${fmt(r.failed_stocks)}`
})
const sources = computed(() => {
  const bs = crawl.value?.by_source
  if (!bs || !Object.keys(bs).length) return []
  const names = { sina: '新浪', eastmoney: '东财' }
  return Object.entries(bs).map(([k, v]) => ({ name: names[k] || k, count: fmt(v) }))
})

onMounted(refresh)
</script>

<template>
  <!-- Web 外壳：全幅铺满视口 -->
  <div class="desktop">
    <div class="window">

      <!-- 标题栏 -->
      <div class="titlebar">
        <h1 class="title">honey-money — 服务状态</h1>
      </div>

      <div class="body">
        <!-- 侧栏 -->
        <aside class="sidebar">
          <div class="group">导航</div>
          <button v-for="n in [
              { id: 'dash', label: '仪表盘' }, { id: 'data', label: '数据' },
              { id: 'backtest', label: '回测' }, { id: 'learn', label: '教学' }]"
            :key="n.id" class="nav-item" :class="{ active: activeNav === n.id }"
            @click="activeNav = n.id">{{ n.label }}</button>
          <div class="group">数据源</div>
          <div class="src-item">新浪财经</div>
          <div class="src-item">东方财富</div>
          <div class="sidebar-foot">v0.1.0 · 工程骨架</div>
        </aside>

        <!-- 内容区 -->
        <main class="content">

          <!-- ===== 仪表盘 ===== -->
          <template v-if="activeNav === 'dash'">
            <div class="toolbar">
              <div>
                <h2 class="h1">{{ backendDown ? '服务状态 · 后端不可用' : '服务状态' }}</h2>
                <p class="sub">{{ checking ? '正在检查各服务…' : '工程化骨架 · 仪表盘 · v0.1.0' }}</p>
              </div>
              <button class="btn" :disabled="checking" @click="refresh">
                {{ checking ? '检查中…' : '重新检测' }}
              </button>
            </div>

            <!-- 错误横幅（后端不可用） -->
            <div v-if="backendDown" class="error-banner">
              <svg class="warn-icon" viewBox="0 0 18 16" fill="none">
                <path d="M9 2 L16 14 L2 14 Z" stroke="#ff3b30" stroke-width="2" stroke-linejoin="round"/>
                <line x1="9" y1="6" x2="9" y2="10" stroke="#ff3b30" stroke-width="2" stroke-linecap="round"/>
                <circle cx="9" cy="12.2" r="1.2" fill="#ff3b30"/>
              </svg>
              <span>后端 API 不可用：Connection refused · 请检查 backend 容器 / uvicorn 进程 / docker compose ps</span>
            </div>

            <!-- 三张状态卡 -->
            <div class="cards">
              <!-- 加载骨架 -->
              <template v-if="checking">
                <div v-for="i in 3" :key="i" class="card skeleton-card">
                  <div class="sk sk-title"></div><div class="sk sk-main"></div><div class="sk sk-line"></div>
                </div>
              </template>
              <template v-else>
                <div class="card">
                  <div class="name">后端 API</div>
                  <span class="dot" :class="backendDown ? 'down' : 'up'"></span>
                  <div class="state">{{ backendDown ? '不可用' : '运行中' }}</div>
                  <div class="detail">v{{ backend.version || '0.1.0' }} · FastAPI + uvicorn</div>
                </div>
                <div class="card" :class="{ idle: backendDown }">
                  <div class="name">PostgreSQL</div>
                  <span class="dot" :class="backendDown ? 'idle' : (infra?.postgres?.status === 'up' ? 'up' : 'down')"></span>
                  <div class="state">{{ backendDown ? '—' : (infra?.postgres?.status === 'up' ? '已连接' : '未连接') }}</div>
                  <div class="detail">{{ backendDown ? '等待后端恢复后展示' : (infra?.postgres?.detail || '') }}</div>
                </div>
                <div class="card" :class="{ idle: backendDown }">
                  <div class="name">代理池</div>
                  <span class="dot" :class="backendDown ? 'idle' : (infra?.proxy_pool?.status === 'up' ? 'up' : 'down')"></span>
                  <div class="state">{{ backendDown ? '—' : (infra?.proxy_pool?.status === 'up' ? '已连接' : '未连接') }}</div>
                  <div class="detail">{{ backendDown ? '等待后端恢复后展示' : (infra?.proxy_pool?.detail || '') }}</div>
                </div>
              </template>
            </div>

            <!-- 爬虫数据卡 -->
            <div v-if="checking" class="card crawl skeleton-card">
              <div class="sk sk-title"></div><div class="sk sk-main"></div>
              <div class="sk sk-bar"></div><div class="sk sk-line"></div><div class="sk sk-line short"></div>
            </div>
            <div v-else-if="crawl && crawl.status !== 'down'" class="card crawl">
              <div class="name">爬虫数据</div>
              <span class="dot" :class="crawl.initialized ? 'up' : 'warn'"></span>

              <!-- 空态：微场景插画（显示器 + 爬虫机器人） -->
              <div v-if="!crawl.initialized" class="empty">
                <svg class="empty-art" viewBox="0 0 140 130" fill="none">
                  <ellipse cx="70" cy="122" rx="56" ry="7" fill="#000" opacity=".06"/>
                  <g transform="rotate(-4 46 45)">
                    <rect x="4" y="6" width="88" height="62" rx="6" fill="#fff" stroke="#aeaeb2" stroke-width="3"/>
                    <rect x="22" y="30" width="7" height="16" rx="2" fill="#34c759"/>
                    <line x1="25.5" y1="24" x2="25.5" y2="30" stroke="#aeaeb2" stroke-width="2"/>
                    <rect x="44" y="22" width="7" height="24" rx="2" fill="#ff3b30"/>
                    <line x1="47.5" y1="16" x2="47.5" y2="22" stroke="#aeaeb2" stroke-width="2"/>
                    <rect x="66" y="36" width="7" height="12" rx="2" fill="#c7c7cc"/>
                    <line x1="69.5" y1="30" x2="69.5" y2="36" stroke="#aeaeb2" stroke-width="2"/>
                    <polyline points="14,52 32,46 54,50 76,38" stroke="#aeaeb2" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    <rect x="43" y="68" width="8" height="9" rx="2" fill="#aeaeb2"/>
                    <rect x="31" y="77" width="32" height="4" rx="2" fill="#aeaeb2"/>
                  </g>
                  <line x1="86" y1="27" x2="76" y2="19" stroke="#34c759" stroke-width="2" stroke-linecap="round"/>
                  <line x1="108" y1="27" x2="118" y2="19" stroke="#34c759" stroke-width="2" stroke-linecap="round"/>
                  <line x1="90" y1="29" x2="86" y2="20" stroke="#34c759" stroke-width="2" stroke-linecap="round"/>
                  <line x1="104" y1="29" x2="108" y2="20" stroke="#34c759" stroke-width="2" stroke-linecap="round"/>
                  <circle cx="97" cy="18" r="8" fill="#34c759"/>
                  <circle cx="94" cy="16" r="2" fill="#fff"/>
                  <circle cx="100" cy="16" r="2" fill="#fff"/>
                  <line x1="99" y1="10" x2="103" y2="3" stroke="#34c759" stroke-width="2" stroke-linecap="round"/>
                  <circle cx="104" cy="2.5" r="2.5" fill="#34c759"/>
                  <line x1="4" y1="8" x2="12" y2="8" stroke="#c7c7cc" stroke-width="2" stroke-linecap="round"/>
                  <line x1="8" y1="4" x2="8" y2="12" stroke="#c7c7cc" stroke-width="2" stroke-linecap="round"/>
                  <line x1="128" y1="36" x2="136" y2="36" stroke="#c7c7cc" stroke-width="2" stroke-linecap="round"/>
                  <line x1="132" y1="32" x2="132" y2="40" stroke="#c7c7cc" stroke-width="2" stroke-linecap="round"/>
                  <circle cx="128" cy="4" r="2.5" fill="#d1d1d6"/>
                  <circle cx="6" cy="64" r="2" fill="#d1d1d6"/>
                </svg>
                <div class="empty-text">
                  <div class="state">爬虫未运行</div>
                  <div class="detail">运行爬虫后开始积累日线数据（判定：crawl.initialized = false）</div>
                  <code class="cli">python -m app.crawler --source eastmoney --mode 增量</code>
                </div>
              </div>

              <!-- 正常态：K线统计 -->
              <template v-else>
                <div class="state">{{ fmt(crawl.daily_bars) }} 根K线</div>
                <div class="bar"><div class="bar-fill" :style="{ width: coverPct + '%' }"></div></div>
                <div class="detail">已爬 {{ fmt(crawl.stocks_covered) }} / {{ fmt(crawl.stocks_listed) }} 只 ·
                  待爬 {{ fmt(crawl.stocks_pending) }} 只（{{ coverPct }}%）</div>
                <div class="chips">
                  <span v-for="s in sources" :key="s.name" class="chip"
                    :class="s.name === '东财' ? 'ef' : 'sina'">{{ s.name }} {{ s.count }}</span>
                </div>
                <div class="detail">最近运行：{{ lastRunText }}</div>
              </template>
            </div>
            <div v-else class="card crawl">
              <div class="name">爬虫数据</div>
              <span class="dot down"></span>
              <div class="state muted">未连接</div>
              <div class="detail">/api/crawl/stats 请求失败 · 恢复后端后点击「重新检测」重试</div>
            </div>

            <p class="note">数据流：进入页面并发拉取 /healthz · /api/infra · /api/crawl/stats</p>
          </template>

          <!-- ===== 数据浏览（占位） ===== -->
          <template v-else-if="activeNav === 'data'">
            <div class="toolbar">
              <div>
                <h2 class="h1">数据浏览</h2>
                <p class="sub">K线查询与浏览 · v0.2+ 规划</p>
              </div>
              <span class="ph-tag">Placeholder · v0.2+</span>
            </div>
            <div class="ph-search">
              <svg class="mag" viewBox="0 0 16 16" fill="none">
                <circle cx="6.5" cy="6.5" r="4.5" stroke="#aeaeb2" stroke-width="2"/>
                <line x1="10" y1="10" x2="14" y2="14" stroke="#aeaeb2" stroke-width="2.5" stroke-linecap="round"/>
              </svg>
              <span>搜索代码 / 名称，如 600519 或 贵州茅台</span>
            </div>
            <div class="ph-grid">
              <div class="ph-table">
                <div class="ph-head">代码 · 名称 · 最新价 · 涨跌幅 · K线数 · 来源</div>
                <div v-for="i in 9" :key="i" class="ph-row">
                  <span class="sk" :style="{ width: (50 + (i * 17) % 45) + 'px' }"></span>
                  <span class="sk" :style="{ width: (28 + (i * 29) % 30) + 'px' }"></span>
                </div>
              </div>
              <div class="ph-chart">
                <div v-for="i in 16" :key="i" class="ph-candle"
                  :style="{ left: 18 + i * 23 + 'px', height: 40 + (i * 47) % 160 + 'px' }"></div>
                <span class="ph-chart-label">K线图 / 走势（图表库待选型）</span>
              </div>
            </div>
            <p class="note">范围：分页/筛选/导出 · 数据源：本地 PG（东财+新浪）</p>
          </template>

          <!-- ===== 回测工作台（占位） ===== -->
          <template v-else-if="activeNav === 'backtest'">
            <div class="toolbar">
              <div>
                <h2 class="h1">回测工作台</h2>
                <p class="sub">Backtrader 策略回测 · v0.2+ 规划</p>
              </div>
              <span class="ph-tag">Placeholder · v0.2+</span>
            </div>
            <div class="ph-grid">
              <div class="ph-form">
                <div class="ph-head">回测参数</div>
                <div v-for="f in ['标的池（全部A股 / 自选）', '时间区间（起止日期）', '策略（均线交叉 / 网格 / 自定义）', '初始资金 / 手续费率']"
                  :key="f" class="ph-field">
                  <label>{{ f }}</label>
                  <div class="ph-input">待实现…</div>
                </div>
                <button class="btn ph-btn">开始回测</button>
                <p class="note">运行方式：异步任务 · 进度回传 · 结果入库</p>
              </div>
              <div class="ph-result">
                <div class="ph-head">回测结果</div>
                <div class="ph-curve"><span>净值 / 回撤曲线（待实现）</span></div>
                <div class="ph-metrics"><span class="sk" style="width:90px"></span><span class="sk" style="width:90px"></span><span class="sk" style="width:90px"></span></div>
                <p class="note">指标：年化收益 / 最大回撤 / 夏普 / 胜率</p>
              </div>
            </div>
          </template>

          <!-- ===== 教学页（占位） ===== -->
          <template v-else>
            <div class="toolbar">
              <div>
                <h2 class="h1">教学页</h2>
                <p class="sub">量化入门课程 · v0.2+ 规划</p>
              </div>
              <span class="ph-tag">Placeholder · v0.2+</span>
            </div>
            <div class="ph-courses">
              <div v-for="c in [
                  { t: '01 · K线与均线基础', d: '日线数据结构 · MA/EMA · 行情软件对照', bg: '#EFF6FF' },
                  { t: '02 · 回测思维入门', d: '为什么回测 · 过拟合陷阱 · 样本内外', bg: '#ECFDF5' },
                  { t: '03 · 第一个策略', d: '均线交叉实战 · 参数敏感性 · 复盘报告', bg: '#FFF7ED' }]"
                :key="c.t" class="ph-course">
                <div class="ph-course-icon" :style="{ background: c.bg }"></div>
                <h3>{{ c.t }}</h3>
                <p>{{ c.d }}</p>
                <span class="ph-tag small">待编写</span>
              </div>
            </div>
            <p class="note">形态：图文卡片 + 代码示例 + 交互练习（信息架构待业务迁入后细化）</p>
          </template>

        </main>
      </div>
    </div>
  </div>
</template>

<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC',
    'Noto Sans SC', 'Microsoft YaHei', sans-serif;
  color: #1d1d1f;
}

/* ===== 外壳：全幅铺满视口（web 端不做桌面浮动窗口） ===== */
.desktop {
  height: 100vh;
  background: #f2f2f4;
  display: flex;
}
.window {
  flex: 1; min-width: 0;
  background: rgba(255, 255, 255, .96);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.titlebar {
  height: 40px; flex: none;
  display: flex; align-items: center;
  padding: 0 20px;
  border-bottom: 1px solid #e8e8ed;
  background: rgba(255, 255, 255, .8);
}
.title {
  margin: 0; font-size: 13px; font-weight: 600; color: #6e6e73;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.body { flex: 1; display: flex; min-height: 0; }

/* ===== 侧栏（半透明材质） ===== */
.sidebar {
  width: 200px; flex: none;
  background: rgba(242, 242, 244, .72);
  backdrop-filter: blur(20px) saturate(1.4);
  border-right: 1px solid #d8d8dc;
  padding: 16px 12px;
  display: flex; flex-direction: column; align-items: stretch;
}
.group { font-size: 11px; color: #aeaeb2; margin: 10px 10px 6px; }
.group:first-child { margin-top: 0; }
.nav-item {
  border: 0; background: transparent; text-align: left; cursor: pointer;
  font: inherit; font-size: 13px; color: #1d1d1f;
  padding: 7px 10px; border-radius: 8px; margin-bottom: 4px;
}
.nav-item:hover { background: rgba(0, 0, 0, .05); }
.nav-item.active { background: #007aff; color: #fff; font-weight: 600; }
.src-item { font-size: 13px; color: #1d1d1f; padding: 5px 10px; }
.sidebar-foot { margin-top: auto; font-size: 11px; color: #aeaeb2; padding: 0 10px 4px; }

/* ===== 内容区 ===== */
.content { flex: 1; padding: 28px 40px 24px; overflow-y: auto; min-width: 0; }
.toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.h1 { margin: 0; font-size: 22px; font-weight: 700; }
.sub { margin: 4px 0 0; font-size: 12px; color: #6e6e73; }
.btn {
  border: 1px solid #c7c7cc; background: rgba(255, 255, 255, .9);
  color: #007aff; font: inherit; font-size: 13px;
  padding: 7px 18px; border-radius: 8px; cursor: pointer; flex: none;
}
.btn:hover { background: #fff; }
.btn:disabled { color: #aeaeb2; cursor: default; }

/* ===== 卡片（毛玻璃） ===== */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; }
.card {
  position: relative;
  background: rgba(255, 255, 255, .85);
  backdrop-filter: blur(30px) saturate(1.8);
  border: 1px solid rgba(0, 0, 0, .07);
  border-radius: 12px;
  box-shadow: 0 3px 14px rgba(0, 0, 0, .08);
  padding: 18px 20px;
}
.card.idle { border-style: dashed; border-color: #c7c7cc; background: rgba(247, 247, 248, .8); }
.name { font-size: 12px; color: #6e6e73; }
.dot { position: absolute; top: 18px; right: 18px; width: 10px; height: 10px; border-radius: 50%; }
.dot.up { background: #34c759; box-shadow: 0 0 6px rgba(52, 199, 89, .55); }
.dot.down { background: #ff3b30; }
.dot.warn, .dot.idle { background: #aeaeb2; }
.state { font-size: 18px; font-weight: 700; margin: 10px 0 8px; }
.state.muted { color: #aeaeb2; }
.detail { font-size: 11px; color: #aeaeb2; word-break: break-all; }
.card.crawl { margin-top: 24px; }
.bar { height: 8px; border-radius: 4px; background: #ececf0; margin: 12px 0 10px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #34d399, #059669); transition: width .4s ease; }
.chips { display: flex; gap: 8px; margin: 8px 0; }
.chip { font-size: 11px; padding: 2px 10px; border-radius: 999px; }
.chip.ef { background: #ECFDF5; color: #047857; }
.chip.sina { background: #EFF6FF; color: #1D4ED8; }
.note { font-size: 11px; color: #aeaeb2; margin: 18px 0 0; }

/* ===== 错误横幅 ===== */
.error-banner {
  display: flex; align-items: center; gap: 10px;
  background: #ffebee; border: 1px solid #ffcdd2; border-radius: 10px;
  padding: 12px 16px; margin-bottom: 24px;
  font-size: 12px; color: #c62828;
}
.warn-icon { flex: none; }

/* ===== 骨架 ===== */
.sk { display: inline-block; background: #ececf0; border-radius: 5px; height: 10px; }
.sk-title { width: 72px; height: 12px; border-radius: 6px; background: #e5e5ea; }
.sk-main { width: 45%; height: 18px; border-radius: 9px; background: #e5e5ea; margin: 14px 0 12px; }
.sk-line { width: 75%; margin-top: 10px; }
.sk-line.short { width: 55%; }
.sk-bar { width: 100%; height: 8px; border-radius: 4px; margin: 16px 0 4px; }
.skeleton-card .sk { display: block; }

/* ===== 空态插画 ===== */
.empty { display: flex; align-items: center; gap: 22px; padding: 6px 0 2px; }
.empty-art { width: 148px; height: 138px; flex: none; }
.empty-text .state { margin-top: 0; }
.cli {
  display: inline-block; margin-top: 10px;
  background: #f5f5f7; border-radius: 8px;
  font: 12px/1.7 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  padding: 8px 14px; color: #1d1d1f;
}

/* ===== 占位页 ===== */
.ph-tag {
  flex: none; font-size: 11px; padding: 5px 14px; border-radius: 999px;
  background: #fff8e6; color: #9a6700;
}
.ph-tag.small { padding: 2px 10px; }
.ph-search {
  display: flex; align-items: center; gap: 12px;
  background: rgba(255, 255, 255, .85); backdrop-filter: blur(30px);
  border: 1px solid rgba(0, 0, 0, .07); border-radius: 10px;
  padding: 11px 16px; color: #aeaeb2; font-size: 13px;
}
.mag { flex: none; }
.ph-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 24px; }
.ph-table, .ph-chart, .ph-form, .ph-result {
  background: rgba(255, 255, 255, .85); backdrop-filter: blur(30px);
  border: 1px solid rgba(0, 0, 0, .07); border-radius: 12px;
  box-shadow: 0 3px 14px rgba(0, 0, 0, .08); padding: 18px 20px;
}
.ph-head { font-size: 13px; font-weight: 600; margin-bottom: 16px; }
.ph-row { display: flex; gap: 16px; margin-bottom: 12px; }
.ph-chart { position: relative; min-height: 320px; }
.ph-candle {
  position: absolute; bottom: 40px; width: 9px;
  background: #d1d5db; border-radius: 2px; opacity: .7;
}
.ph-chart-label {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: #aeaeb2; font-size: 12px;
}
.ph-field { margin-bottom: 16px; }
.ph-field label { display: block; font-size: 12px; color: #aeaeb2; margin-bottom: 6px; }
.ph-input {
  border: 1px dashed #c7c7cc; border-radius: 6px;
  padding: 9px 12px; font-size: 12px; color: #aeaeb2; background: #fff;
}
.ph-btn { margin-top: 4px; color: #aeaeb2; cursor: default; }
.ph-curve {
  height: 200px; background: #f0f0f3; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #aeaeb2; font-size: 12px; margin-bottom: 16px;
}
.ph-metrics { display: flex; gap: 24px; }
.ph-courses { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; }
.ph-course {
  background: rgba(255, 255, 255, .85); backdrop-filter: blur(30px);
  border: 1px solid rgba(0, 0, 0, .07); border-radius: 12px;
  box-shadow: 0 3px 14px rgba(0, 0, 0, .08); padding: 20px;
}
.ph-course-icon { height: 110px; border-radius: 8px; margin-bottom: 16px; }
.ph-course h3 { margin: 0 0 8px; font-size: 15px; }
.ph-course p { margin: 0 0 14px; font-size: 12px; color: #6e6e73; }

/* ===== 响应式兜底 ===== */
@media (max-width: 860px) {
  .body { flex-direction: column; }
  .sidebar { width: 100%; flex-direction: row; flex-wrap: wrap; align-items: center; gap: 4px; border-right: 0; border-bottom: 1px solid #d8d8dc; }
  .group, .sidebar-foot { display: none; }
  .content { padding: 20px; }
  .ph-grid { grid-template-columns: 1fr; }
  .empty { flex-direction: column; align-items: flex-start; }
}
</style>
