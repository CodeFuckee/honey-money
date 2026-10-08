<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'
import Paginator from 'primevue/paginator'

// ===== 数据层（沿用原仪表盘逻辑：三接口并发 + 优雅降级）=====
const backend = ref({ status: 'checking' })   // 后端进程本身
const infra = ref(null)                       // PostgreSQL / 代理池
const crawl = ref(null)                       // 爬虫数据统计
const loading = ref(false)
const activeNav = ref('dash')                 // dash | data | src-sina | src-ef | backtest | learn

// 数据源子页（对应 Carbon 原型 ⑥⑦ 屏）：按来源筛选的数据浏览占位页
const SRC_ITEMS = [
  { id: 'src-sina', label: '新浪财经', field: 'sina', tagCls: 'src-sina' },
  { id: 'src-ef', label: '东方财富', field: 'eastmoney', tagCls: 'src-ef' },
]

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
const srcPage = computed(() => SRC_ITEMS.find(s => s.id === activeNav.value) || null)
// ===== 数据浏览：真实爬取数据（/api/stocks 分页 + 搜索）=====
const browse = ref({ status: 'loading', total: 0, stocks: [] })
const browseQuery = ref('')
const browsePage = ref(0)                 // 0 起
const BROWSE_PAGE_SIZE = 50
const browseLoading = ref(false)
const browseTotalPages = computed(() =>
  Math.max(1, Math.ceil((browse.value.total || 0) / BROWSE_PAGE_SIZE)))

async function loadBrowse() {
  browseLoading.value = true
  try {
    const q = encodeURIComponent(browseQuery.value.trim())
    const off = browsePage.value * BROWSE_PAGE_SIZE
    const r = await fetch(`/api/stocks?q=${q}&limit=${BROWSE_PAGE_SIZE}&offset=${off}`)
    browse.value = await r.json()
  } catch (e) {
    browse.value = { status: 'down', total: 0, stocks: [] }
  } finally {
    browseLoading.value = false
  }
}
function browseSearch() { browsePage.value = 0; loadBrowse() }
function onPage(e) {
  browsePage.value = e.page; loadBrowse()
}
watch([activeNav, srcPage], ([nav]) => {
  if (nav === 'data' || srcPage.value) loadBrowse()
})

const srcSubText = computed(() => {
  const p = srcPage.value
  if (!p) return ''
  const n = crawl.value?.by_source?.[p.field]
  return n != null
    ? `按来源筛选 · ${p.label}源 K线 ${fmt(n)} 根 · v0.2+ 规划`
    : `按来源筛选 · 仅显示${p.label}源 K线 · v0.2+ 规划`
})

onMounted(refresh)
</script>

<template>
  <!-- Web 外壳：全幅铺满视口 -->
  <div class="desktop">
    <div class="window">

      <!-- 标题栏（apple 标准标题 / carbon 复用为深色 UI Shell 头；fluent 按原型无标题栏，切换控件移至工具栏） -->
      <div class="titlebar">
        <svg class="shell-menu" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <line x1="2" y1="3.5" x2="14" y2="3.5" stroke="currentColor" stroke-width="1.6"/>
          <line x1="2" y1="8" x2="14" y2="8" stroke="currentColor" stroke-width="1.6"/>
          <line x1="2" y1="12.5" x2="14" y2="12.5" stroke="currentColor" stroke-width="1.6"/>
        </svg>
        <h1 class="title">honey-money<span class="title-sub"> · A 股量化研究与交易系统</span></h1>

        <svg class="shell-set" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="2.2" stroke="currentColor" stroke-width="1.4"/>
          <path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.6 3.6 5 5M11 11l1.4 1.4M12.4 3.6 11 5M5 11l-1.4 1.4"
            stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        </svg>
      </div>

      <div class="body">
        <!-- 侧栏 -->
        <aside class="sidebar">
          <div class="app-item">
            <svg class="app-item-icon" viewBox="0 0 24 24" fill="none">
              <path d="M12 2.5 20 7v10l-8 4.5L4 17V7z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
              <path d="M12 7.2 16 9.4v4.4l-4 2.2-4-2.2V9.4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
            </svg>
            <span>honey-money</span>
          </div>
          <div class="group">导航</div>
          <button v-for="n in [
              { id: 'dash', label: '仪表盘' }, { id: 'data', label: '数据' },
              { id: 'backtest', label: '回测' }, { id: 'learn', label: '教学' }]"
            :key="n.id" class="nav-item"
            :class="{ active: activeNav === n.id, 'sub-active': activeNav.startsWith('src-') && n.id === 'data' }"
            @click="activeNav = n.id">
            <svg v-if="n.id === 'dash'" class="nav-icon" viewBox="0 0 20 20" fill="none">
              <rect x="2.5" y="2.5" width="6.5" height="6.5" rx="1.2" stroke="currentColor" stroke-width="1.5"/>
              <rect x="11" y="2.5" width="6.5" height="6.5" rx="1.2" stroke="currentColor" stroke-width="1.5"/>
              <rect x="2.5" y="11" width="6.5" height="6.5" rx="1.2" stroke="currentColor" stroke-width="1.5"/>
              <rect x="11" y="11" width="6.5" height="6.5" rx="1.2" stroke="currentColor" stroke-width="1.5"/>
            </svg>
            <svg v-else-if="n.id === 'data'" class="nav-icon" viewBox="0 0 20 20" fill="none">
              <ellipse cx="10" cy="5" rx="7" ry="2.6" stroke="currentColor" stroke-width="1.5"/>
              <path d="M3 5v10c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V5" stroke="currentColor" stroke-width="1.5"/>
              <path d="M3 10c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6" stroke="currentColor" stroke-width="1.5"/>
            </svg>
            <svg v-else-if="n.id === 'backtest'" class="nav-icon" viewBox="0 0 20 20" fill="none">
              <path d="M3 17h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              <path d="M5 13l4-4.5 3 3 4-6.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <svg v-else class="nav-icon" viewBox="0 0 20 20" fill="none">
              <path d="M3 4.2c2.4-1.1 4.8-1.1 7 0 2.2-1.1 4.6-1.1 7 0v11.6c-2.4-1.1-4.8-1.1-7 0-2.2-1.1-4.6-1.1-7 0z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
              <path d="M10 4.2v11.6" stroke="currentColor" stroke-width="1.5"/>
            </svg>{{ n.label }}</button>
          <div class="group">数据源</div>
          <button v-for="s in SRC_ITEMS" :key="s.id" class="src-item"
            :class="{ active: activeNav === s.id }" @click="activeNav = s.id">{{ s.label }}</button>
          <div class="sidebar-foot">v0.1.0 · 工程骨架</div>
        </aside>

        <!-- 内容区 -->
        <main class="content">

          <!-- ===== 仪表盘 ===== -->
          <template v-if="activeNav === 'dash'">
            <div class="toolbar">
              <div>
                <h2 class="h1">服务状态</h2>
                <p class="sub">{{ checking ? '正在检查各服务…' : '工程化骨架 · 仪表盘 · v0.1.0' }}</p>
              </div>
              <div class="toolbar-right">
                <button class="btn btn-primary" :disabled="checking" @click="refresh">
                  {{ checking ? '检查中…' : '重新检测' }}
                </button>

              </div>
            </div>

            <!-- 错误横幅（后端不可用；carbon 为内联通知样式：实心图标 + 加粗标题 + 同行正文） -->
            <div v-if="backendDown" class="error-banner">
              <svg class="warn-icon" viewBox="0 0 18 16" fill="none">
                <path d="M9 2 L16 14 L2 14 Z" stroke="#ff3b30" stroke-width="2" stroke-linejoin="round"/>
                <line x1="9" y1="6" x2="9" y2="10" stroke="#ff3b30" stroke-width="2" stroke-linecap="round"/>
                <circle cx="9" cy="12.2" r="1.2" fill="#ff3b30"/>
              </svg>
              <svg class="warn-icon err-filled" viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="8" cy="8" r="7" fill="#da1e28"/>
                <path d="M5.2 5.2l5.6 5.6M10.8 5.2l-5.6 5.6" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>
              </svg>
              <span><b class="eb-title">后端 API 不可用：</b>Connection refused · 请检查 backend 容器 / uvicorn 进程 / docker compose ps</span>
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
                  <span class="fbadge"><span class="dot" :class="backendDown ? 'down' : 'up'"></span><span class="fb-txt">{{ backendDown ? '不可用' : '运行中' }}</span></span>
                  <div class="state">{{ backendDown ? '不可用' : '运行中' }}</div>
                  <div class="detail">v{{ backend.version || '0.1.0' }} · FastAPI + uvicorn</div>
                </div>
                <div class="card" :class="{ idle: backendDown }">
                  <div class="name">PostgreSQL</div>
                  <span class="fbadge"><span class="dot" :class="backendDown ? 'down' : (infra?.postgres?.status === 'up' ? 'up' : 'down')"></span><span class="fb-txt">{{ backendDown ? '数据不可用' : (infra?.postgres?.status === 'up' ? '已连接' : '未连接') }}</span></span>
                  <div class="state">{{ backendDown ? '—' : (infra?.postgres?.status === 'up' ? '已连接' : '未连接') }}</div>
                  <div class="detail">{{ backendDown ? '等待后端恢复后展示' : (infra?.postgres?.detail || '') }}</div>
                </div>
                <div class="card" :class="{ idle: backendDown }">
                  <div class="name">代理池</div>
                  <span class="fbadge"><span class="dot" :class="backendDown ? 'down' : (infra?.proxy_pool?.status === 'up' ? 'up' : 'down')"></span><span class="fb-txt">{{ backendDown ? '数据不可用' : (infra?.proxy_pool?.status === 'up' ? '已连接' : '未连接') }}</span></span>
                  <div class="state">{{ backendDown ? '—' : (infra?.proxy_pool?.status === 'up'
                    ? (infra.proxy_pool.count != null ? `已连接 · 在池 ${infra.proxy_pool.count}` : '已连接')
                    : '未连接') }}</div>
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
              <span class="fbadge" :class="crawl.initialized ? 'brand' : 'warn'"><span class="dot" :class="crawl.initialized ? 'up' : 'warn'"></span><span class="fb-txt">{{ crawl.initialized ? fmt(crawl.daily_bars) + ' 根K线' : '爬虫未运行' }}</span></span>

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
                  <div class="state">爬虫尚未运行，日线数据为空</div>
                  <div class="detail">运行爬虫后开始积累日线数据（判定：crawl.initialized = false）</div>
                  <code class="cli"><span class="cli-ps">$ </span>python -m app.crawler --source eastmoney --mode 增量</code>
                </div>
              </div>

              <!-- 正常态：K线统计 -->
              <template v-else>
                <div class="state">{{ fmt(crawl.daily_bars) }} 根K线</div>
                <div class="bar"><div class="bar-fill" :style="{ width: coverPct + '%' }"></div></div>
                <div class="detail">已爬 {{ fmt(crawl.stocks_covered) }} / {{ fmt(crawl.stocks_listed) }} 只 ·
                  待爬 {{ fmt(crawl.stocks_pending) }} 只（{{ coverPct }}%）</div>
                <div v-if="sources.length" class="detail src-line">K线来源：{{ sources.map(s => `${s.name} ${s.count}`).join(' · ') }}</div>
                <div class="chips">
                  <span v-for="s in sources" :key="s.name" class="chip"
                    :class="s.name === '东财' ? 'ef' : 'sina'">{{ s.name }} {{ s.count }}</span>
                </div>
                <div class="detail">最近运行：{{ lastRunText }}</div>
              </template>
            </div>
            <div v-else class="card crawl down">
              <div class="name">爬虫数据</div>
              <span class="fbadge"><span class="dot down"></span><span class="fb-txt">数据不可用</span></span>
              <div class="state muted">未连接</div>
              <div class="detail">/api/crawl/stats 请求失败 · 恢复后端后点击「重新检测」重试</div>
            </div>

            <p class="note">数据流：进入页面并发拉取 /healthz · /api/infra · /api/crawl/stats</p>
          </template>

          <!-- ===== 数据浏览（占位；数据源子页为按来源筛选变体） ===== -->
          <template v-else-if="activeNav === 'data' || srcPage">
            <div class="toolbar">
              <div>
                <h2 class="h1">{{ srcPage ? `数据浏览 · ${srcPage.label}` : '数据浏览' }}</h2>
                <p class="sub">{{ srcPage ? srcSubText : 'K线查询与浏览 · 已入库股票行情一览' }}</p>
              </div>
              <div class="toolbar-right">
                <span v-if="srcPage" class="ph-tag" :class="srcPage.tagCls">{{ srcPage.label }}</span>

              </div>
            </div>
            <div class="ph-search">
              <span class="p-inputtext-icon-wrap"><i class="pi pi-search"></i></span>
              <InputText v-model="browseQuery" placeholder="搜索代码 / 名称，如 600519 或 贵州茅台"
                class="browse-input" @keydown.enter="browseSearch" />
              <Button label="搜索" size="small" severity="secondary" outlined @click="browseSearch" />
            </div>
            <div class="browse-pv">
              <DataTable :value="browse.stocks || []" lazy paginator :rows="50"
                :totalRecords="browse.total || 0" :loading="browseLoading"
                dataKey="code" stripedRows size="small"
                @page="onPage">
                <template #empty>
                  <div class="browse-empty">
                    {{ browse.status === 'down' ? '数据库不可达：' + (browse.detail || '') : '无匹配股票' }}
                  </div>
                </template>
                <Column field="code" header="代码" style="width: 100px">
                  <template #body="{ data }"><span class="mono">{{ data.code }}</span></template>
                </Column>
                <Column field="name" header="名称" />
                <Column field="close" header="最新价" style="width: 100px" bodyClass="num-col">
                  <template #body="{ data }">{{ data.close ?? '—' }}</template>
                </Column>
                <Column field="pct_chg" header="涨跌幅" style="width: 100px">
                  <template #body="{ data }">
                    <span v-if="data.pct_chg != null" :class="data.pct_chg >= 0 ? 'chg-up' : 'chg-down'">
                      {{ (data.pct_chg >= 0 ? '+' : '') + data.pct_chg + '%' }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="bars" header="K线数" style="width: 90px">
                  <template #body="{ data }">{{ data.bars || 0 }}</template>
                </Column>
                <Column field="last_date" header="最新交易日" style="width: 120px">
                  <template #body="{ data }">{{ data.last_date || '—' }}</template>
                </Column>
                <Column field="sources" header="来源" style="width: 90px">
                  <template #body="{ data }"><span class="src-txt">{{ data.sources || '—' }}</span></template>
                </Column>
              </DataTable>
            </div>
            <p class="note">数据源：本地 PG（新浪/东财）· 爬虫持续入库中 · 单股K线明细接口已就绪（/api/stocks/{code}/bars），图表待选型</p>
          </template>

          <!-- ===== 回测工作台（占位） ===== -->
          <template v-else-if="activeNav === 'backtest'">
            <div class="toolbar">
              <div>
                <h2 class="h1">回测工作台</h2>
                <p class="sub">Backtrader 策略回测 · v0.2+ 规划</p>
              </div>
              <div class="toolbar-right">
                <span class="ph-tag">Placeholder · v0.2+</span>

              </div>
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
              <div class="toolbar-right">
                <span class="ph-tag">Placeholder · v0.2+</span>

              </div>
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
/* ===== Carbon 主题 token（IBM Carbon Design：#0F62FE / #F4F4F4 / #161616，方角）===== */
:root { color-scheme: light; }
:root {
  --font: 'IBM Plex Sans', 'Segoe UI', system-ui, 'PingFang SC', 'Noto Sans SC', 'Microsoft YaHei', sans-serif;
  --fg: #161616; --fg-2: #525252; --fg-3: #a8a8a8;
  --accent: #0f62fe;
  --shell-bg: #f4f4f4;
  --window-bg: #f4f4f4;
  --titlebar-bg: #161616;
  --titlebar-border: #161616;
  --titlebar-h: 48px;
  --title-color: #f4f4f4;
  --sidebar-w: 256px;
  --content-pad: 32px 40px 24px;
  --h1-size: 28px;
  --h1-weight: 400;
  --bar-h: 6px;
  --nav-active-bar: none;
  --sidebar-bg: #ffffff;
  --sidebar-border: #e0e0e0;
  --sidebar-blur: none;
  --nav-radius: 0;
  --nav-hover-bg: #e8e8e8;
  --nav-active-bg: #e0e0e0;
  --nav-active-fg: #161616;
  --card-bg: #ffffff;
  --card-blur: none;
  --card-border-color: #e0e0e0;
  --card-radius: 2px;
  --card-shadow: none;
  --card-idle-bg: #fafafa;
  --card-idle-border: #c6c6c6;
  --dot-up: #198038; --dot-up-glow: none;
  --dot-down: #da1e28; --dot-warn: #f1c21b; --dot-idle: #a8a8a8;
  --bar-track: #e0e0e0;
  --bar-fill: #0f62fe;
  --chip-ef-bg: #e9f4e9; --chip-ef-fg: #198038;
  --chip-sina-bg: #eaf1fb; --chip-sina-fg: #0f62fe;
  --btn-fg: #161616; --btn-bg: #ffffff; --btn-hover-bg: #e8e8e8;
  --btn-border-color: #c6c6c6; --btn-radius: 0;
  --error-bg: #fff1f1; --error-border-color: #da1e28; --error-fg: #a2191f;
  --sk: #e0e0e0; --sk-strong: #c6c6c6;
  --cli-bg: #f4f4f4;
  --ph-tag-bg: #fff8e6; --ph-tag-fg: #615500;
  --ph-input-border: #8d8d8d; --ph-input-bg: #ffffff;
  --ph-curve-bg: #f4f4f4;
  --seg-track: rgba(0, 0, 0, .05);
  --seg-btn-radius: 0;
  --seg-active-bg: #ffffff;
  --seg-active-shadow: none;
}
/* IBM Carbon（按 Penpot 原型「honey-money · IBM Carbon 风格仪表盘」校准，2026-10-08）：
   深色 UI Shell 头 48px #161616、侧栏 256px 白底 1px #E0E0E0、方角 1px 边框白卡、
   蓝主按钮 #0F62FE 96×40、8px 方角进度条、#FFF1F1 内联错误通知、
   灰阶 #161616 / #525252 / #A8A8A8，状态色 #24A148 / #DA1E28 / #F1C21B */

/* —— Carbon 固定形态：无标题栏（应用名在侧栏 app item），工具栏带切换位 —— */
.titlebar { display: none; }
.tb-toggle { display: none; }
.src-line { display: none; }
.shell-menu, .shell-set { display: none; flex: none; }

/* —— 徽章（Carbon 原型 Badge：点 + 文字，右上角）—— */
.fbadge {
  position: absolute; top: 18px; right: 18px;
  display: flex; align-items: center; gap: 6px;
  font-size: 10px; line-height: 1; color: var(--fg-2);
}
.fbadge .dot { position: static; width: 12px; height: 12px; }
.fbadge.brand { color: var(--accent); background: #eaf1fb; padding: 2px 8px; white-space: nowrap; }
.fbadge.brand .dot { background: var(--accent); box-shadow: none; }
.fbadge:has(.dot.up) { color: var(--dot-up); }
.fbadge:has(.dot.down) { color: var(--dot-down); }
.fbadge:has(.dot.warn) { color: var(--dot-warn); }

/* —— fluent 状态卡：标题左上 + 徽章右上 + 详情沉底（原型 364×132）—— */
.cards > .card { height: 132px; }
.cards > .card > .state { display: none; }
.cards > .card > .detail { position: absolute; left: 20px; bottom: 20px; margin: 0; font-size: 12px; color: var(--fg-2);
}
.name { font-size: 14px; font-weight: 400; color: var(--fg); }

/* —— fluent 侧栏导航（原型 NavNode 40 高浅灰行 + 左侧品牌蓝横条选中指示）—— */
.nav-item { position: relative; }
.sidebar { padding: 10px 8px 16px; }
.app-item { display: flex; align-items: center; gap: 12px;
  height: 48px; padding: 0 10px 0 12px; margin-bottom: 18px;
  background: #f0f0f0; border-radius: 4px;
  font-size: 16px; color: var(--fg);
}
.app-item-icon { width: 26px; height: 26px; flex: none; color: var(--fg); }
.group { font-size: 12px; margin: 18px 24px 4px; }
.nav-item { display: flex; align-items: center; gap: 16px;
  height: 40px; margin: 0 10px; padding: 0 10px;
  font-size: 14px; color: #424242;
}
.nav-item.active { color: #242424; }
.nav-icon { width: 20px; height: 20px; flex: none; color: #616161; }
.nav-item.active .nav-icon { color: var(--accent); }
.nav-item.active::before { content: ''; position: absolute; left: -6px; top: 50%;
  transform: translateY(-50%);
  width: 4px; height: 16px; border-radius: 2px; background: var(--accent);
}
.src-item { display: flex; align-items: center; height: 32px;
  padding: 0 10px 0 46px; font-size: 14px; color: #424242;
}
.sidebar-foot { padding: 0 24px 12px; font-size: 11px; }

/* —— fluent 工具栏/标题排版（28px 常规字重大标题）—— */
.toolbar-right { display: flex; align-items: center; gap: 12px; flex: none; }
.h1 { font-size: var(--h1-size); font-weight: var(--h1-weight); }
.sub { font-size: 13px; }

/* —— fluent 空态 / 命令 chip / 错误条 —— */
.empty-art { display: none; }
.empty { display: block; padding: 40px 0 0 80px; }
.empty .state { font-size: 14px; font-weight: 400; }
.cli { background: #f5f5f5; border-radius: 6px; height: 36px;
  display: inline-flex; align-items: center; padding: 0 14px;
  font-size: 12px; color: var(--fg); margin-top: 14px;
}
.error-banner { padding: 8px 12px; border-radius: 4px; }

* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font-family: var(--font);
  color: var(--fg);
}
.window, .titlebar, .sidebar, .card { transition: background-color .15s ease; }

/* ===== 外壳：全幅铺满视口（web 端不做桌面浮动窗口） ===== */
.desktop {
  height: 100vh;
  background: var(--shell-bg);
  display: flex;
}
.window {
  flex: 1; min-width: 0;
  background: var(--window-bg);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.titlebar {
  height: var(--titlebar-h); flex: none;
  display: flex; align-items: center; gap: 12px;
  padding: 0 20px;
  border-bottom: 1px solid var(--titlebar-border);
  background: var(--titlebar-bg);
}
.title {
  flex: 1; min-width: 0;
  margin: 0; font-size: 13px; font-weight: 600; color: var(--title-color);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* ===== 风格切换（分段控件，apple/macOS 分段与 Fluent 圆角4两种质感） ===== */

.body { flex: 1; display: flex; min-height: 0; }

/* ===== 侧栏（apple 半透明毛玻璃 / fluent 实色） ===== */
.sidebar {
  width: var(--sidebar-w, 200px); flex: none;
  background: var(--sidebar-bg);
  backdrop-filter: var(--sidebar-blur);
  border-right: 1px solid var(--sidebar-border);
  padding: 16px 12px;
  display: flex; flex-direction: column; align-items: stretch;
}
.group { font-size: 11px; color: var(--fg-3); margin: 10px 10px 6px; }
.group:first-child { margin-top: 0; }
.nav-item {
  border: 0; background: transparent; text-align: left; cursor: pointer;
  font: inherit; font-size: 13px; color: var(--fg);
  padding: 7px 10px; border-radius: var(--nav-radius); margin-bottom: 4px;
}
.nav-item:hover { background: var(--nav-hover-bg); }
.nav-item.active {
  background: var(--nav-active-bg); color: var(--nav-active-fg); font-weight: 600;
  box-shadow: var(--nav-active-bar);
}
.src-item {
  border: 0; background: transparent; text-align: left; cursor: pointer; font: inherit;
  font-size: 13px; color: var(--fg); padding: 5px 10px;
}
.src-item:hover { background: var(--nav-hover-bg); }
.src-item.active { font-weight: 600; background: var(--nav-hover-bg); }
.sidebar-foot { margin-top: auto; font-size: 11px; color: var(--fg-3); padding: 0 10px 4px; }

/* ===== 内容区 ===== */
.content { flex: 1; padding: var(--content-pad, 28px 40px 24px); overflow-y: auto; min-width: 0; }
.toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.h1 { margin: 0; font-size: var(--h1-size, 22px); font-weight: var(--h1-weight, 700); }
.sub { margin: 4px 0 0; font-size: 12px; color: var(--fg-2); }
.btn {
  border: 1px solid var(--btn-border-color); background: var(--btn-bg);
  color: var(--btn-fg); font: inherit; font-size: 13px;
  padding: 7px 18px; border-radius: var(--btn-radius); cursor: pointer; flex: none;
}
.btn:hover { background: var(--btn-hover-bg); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn-primary:hover { background: #0353e9; border-color: #0353e9; }
.btn-primary:disabled { background: var(--accent); opacity: .5; color: #fff; }
.btn:disabled { color: var(--fg-3); cursor: default; }

/* ===== 卡片（apple 毛玻璃 / fluent 实色描边卡） ===== */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; }
.card {
  position: relative;
  background: var(--card-bg);
  backdrop-filter: var(--card-blur);
  border: 1px solid var(--card-border-color);
  border-radius: var(--card-radius);
  box-shadow: var(--card-shadow);
  padding: 18px 20px;
}
.card.idle { border-style: dashed; border-color: var(--card-idle-border); background: var(--card-idle-bg); }
.name { font-size: 12px; color: var(--fg-2); }
.dot { position: absolute; top: 18px; right: 18px; width: 10px; height: 10px; border-radius: 50%; }
.dot.up { background: var(--dot-up); box-shadow: var(--dot-up-glow); }
.dot.down { background: var(--dot-down); }
.dot.warn { background: var(--dot-warn); }
.dot.idle { background: var(--dot-idle); }
.state { font-size: 18px; font-weight: 700; margin: 10px 0 8px; }
.state.muted { color: var(--fg-3); }
.detail { font-size: 11px; color: var(--fg-3); word-break: break-all; }
.card.crawl { margin-top: 24px; }
.bar { height: var(--bar-h, 8px); border-radius: 4px; background: var(--bar-track); margin: 12px 0 10px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; background: var(--bar-fill); transition: width .4s ease; }
.chips { display: flex; gap: 8px; margin: 8px 0; }
.chip { font-size: 11px; padding: 2px 10px; border-radius: 999px; }
.chip.ef { background: var(--chip-ef-bg); color: var(--chip-ef-fg); }
.chip.sina { background: var(--chip-sina-bg); color: var(--chip-sina-fg); }
.note { font-size: 11px; color: var(--fg-3); margin: 18px 0 0; }

/* ===== 错误横幅 ===== */
.error-banner {
  display: flex; align-items: center; gap: 10px;
  background: var(--error-bg); border: 1px solid var(--error-border-color); border-radius: 10px;
  padding: 12px 16px; margin-bottom: 24px;
  font-size: 12px; color: var(--error-fg);
}
.warn-icon { flex: none; width: 18px; height: 16px; }

/* ===== 骨架 ===== */
.sk { display: inline-block; background: var(--sk); border-radius: 5px; height: 10px; }
.sk-title { width: 72px; height: 12px; border-radius: 6px; background: var(--sk-strong); }
.sk-main { width: 45%; height: 18px; border-radius: 9px; background: var(--sk-strong); margin: 14px 0 12px; }
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
  background: var(--cli-bg); border-radius: 8px;
  font: 12px/1.7 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  padding: 8px 14px; color: var(--fg);
}

/* ===== 占位页 ===== */
.ph-tag {
  flex: none; font-size: 11px; padding: 5px 14px; border-radius: 999px;
  background: var(--ph-tag-bg); color: var(--ph-tag-fg);
}
.ph-tag.small { padding: 2px 10px; }
.ph-tag.src-sina { background: #0f62fe; color: #fff; }
.ph-tag.src-ef { background: #24a148; color: #fff; }
.ph-search {
  display: flex; align-items: center; gap: 12px;
  background: var(--card-bg); backdrop-filter: var(--card-blur);
  border: 1px solid var(--card-border-color); border-radius: 10px;
  padding: 11px 16px; color: var(--fg-3); font-size: 13px;
}
.mag { flex: none; width: 16px; height: 16px; }
.ph-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 24px; }
.ph-table, .ph-chart, .ph-form, .ph-result {
  background: var(--card-bg); backdrop-filter: var(--card-blur);
  border: 1px solid var(--card-border-color); border-radius: var(--card-radius);
  box-shadow: var(--card-shadow); padding: 18px 20px;
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
  color: var(--fg-3); font-size: 12px;
}
.ph-field { margin-bottom: 16px; }
.ph-field label { display: block; font-size: 12px; color: var(--fg-3); margin-bottom: 6px; }
.ph-input {
  border: 1px dashed var(--ph-input-border); border-radius: 6px;
  padding: 9px 12px; font-size: 12px; color: var(--fg-3); background: var(--ph-input-bg);
}
.ph-btn { margin-top: 4px; color: var(--fg-3); cursor: default; }
.ph-curve {
  height: 200px; background: var(--ph-curve-bg); border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: var(--fg-3); font-size: 12px; margin-bottom: 16px;
}
.ph-metrics { display: flex; gap: 24px; }
.ph-courses { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; }
.ph-course {
  background: var(--card-bg); backdrop-filter: var(--card-blur);
  border: 1px solid var(--card-border-color); border-radius: var(--card-radius);
  box-shadow: var(--card-shadow); padding: 20px;
}
.ph-course-icon { height: 110px; border-radius: 8px; margin-bottom: 16px; }
.ph-course h3 { margin: 0 0 8px; font-size: 15px; }
.ph-course p { margin: 0 0 14px; font-size: 12px; color: var(--fg-2); }

/* —— 第二轮真稿对比修正（导航行底色/卡片间距/骨架灰块/错误降级卡/数据页单列）—— */
.nav-item { background: #f0f0f0; }
.src-item { background: #f0f0f0; margin: 0 10px 4px; border-radius: 4px; }
.src-item.active { background: #eaeaea; }
.sidebar .group:nth-of-type(2) { border-top: 1px solid #e5e5e5; padding-top: 14px; margin: 14px 14px 4px; width: 232px;
}
.cards { gap: 12px; }
.card.crawl .detail { font-size: 13px; color: var(--fg-2); }
.card.crawl > .state { display: none; }
.skeleton-card { background: #f0f0f0; border-color: transparent; box-shadow: none; }
.cards > .card.skeleton-card { height: 132px; }
.skeleton-card .sk,
 .skeleton-card .sk-title,
 .skeleton-card .sk-main { background: #fafafa; }
.warn-icon path,
 .warn-icon line { stroke: var(--dot-down); }
.warn-icon circle { fill: var(--dot-down); }
.card.crawl.down { background: #fafafa; box-shadow: none; height: 220px; }
.card.crawl.down .fbadge { top: 78px; left: 50%; right: auto; transform: translateX(-50%); }
.card.crawl.down .detail { display: none; }
.ph-search { width: 480px; }
.ph-grid { grid-template-columns: 1fr; }
.ph-chart { min-height: 160px; background: #fafafa; border-color: transparent; box-shadow: none; }
.ph-candle { display: none; }
.ph-table .ph-head { background: #f5f5f5; border-radius: 4px; height: 44px; display: flex; align-items: center; padding: 0 12px;
}

/* ===== IBM Carbon 结构规则（按原型逐项：深色头/侧栏/方角卡/Tag/内联通知/数据表） ===== */
/* —— 深色 UI Shell 头：菜单图标 + 品牌 + 副题浅灰 + 右侧设置图标 —— */
.titlebar { gap: 16px; }
.title { font-size: 14px; font-weight: 600; }
.title-sub { font-size: 12px; font-weight: 400; color: #a8a8a8; }
.shell-menu, .shell-set { display: block; width: 16px; height: 16px; color: #f4f4f4; }
.shell-set { margin-left: 8px; }
.seg-btn { color: #c6c6c6; }
.seg-btn.on { color: #161616; }

/* —— 侧栏 256：48px 导航行、选中灰底 + 右缘 chevron、分组分隔线、脚注 —— */
.sidebar { padding: 8px 0 0; }
.group { font-size: 12px; color: #525252; margin: 16px 16px 4px; }
.sidebar .group:nth-of-type(2) { border-top: 1px solid #e0e0e0; padding-top: 16px; margin-top: 16px;
}
.nav-item { display: flex; align-items: center;
  height: 48px; margin: 0; padding: 0 16px;
  font-size: 14px; font-weight: 400; color: #161616;
}
.nav-item.active { font-weight: 600; }
.nav-item.active::after { content: ''; margin-left: auto; width: 7px; height: 7px;
  border-top: 2px solid #525252; border-right: 2px solid #525252;
  transform: rotate(45deg);
}
.src-item { display: flex; align-items: center;
  height: 32px; padding: 0 16px 0 36px; font-size: 14px; color: #525252;
}
.src-item:hover { background: #f4f4f4; }
.src-item.active { background: #e0e0e0; color: #161616; }
.src-item.active::after { content: ''; margin-left: auto; width: 7px; height: 7px;
  border-top: 2px solid #525252; border-right: 2px solid #525252;
  transform: rotate(45deg);
}
.nav-item.sub-active { background: #e0e0e0; }
.sidebar-foot { padding: 0 16px 12px; font-size: 12px; color: #525252; }

/* —— 徽章：状态卡为 点+12px 文字；爬虫卡按原型为实心 Tag（蓝/黄/红） —— */
.fb-txt { display: inline; }
.fbadge { position: absolute; top: 16px; right: 20px;
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; line-height: 1; color: #525252;
}
.fbadge .dot { position: static; width: 8px; height: 8px; }

.fbadge.brand .dot,
 .fbadge.warn .dot,
 
/* —— 状态卡 140：标题 14/600、大字状态、详情沉底 12px —— */
.cards { gap: 32px; }
.cards > .card { height: 140px; padding: 16px 20px; }
.cards > .card > .state { font-size: 20px; font-weight: 600; margin: 14px 0 0; }
.cards > .card > .detail { position: absolute; left: 20px; right: 20px; bottom: 16px; margin: 0;
  font-size: 12px; color: #525252;
}
.name { font-size: 14px; font-weight: 600; color: #161616; }

/* —— 爬虫卡：标题 16/600、8px 方角进度条、细节 12px —— */
.card.crawl { margin-top: 32px; padding: 20px; }
.card.crawl > .name { font-size: 16px; }
.card.crawl > .state { display: none; }
.card.crawl .bar,  .card.crawl .bar-fill { border-radius: 0; }
.card.crawl .detail { font-size: 12px; color: #525252; margin-top: 6px; }
.card.crawl .detail.src-line { color: #161616; }

/* —— 空态：无插画 + 警示黄 Tag（fbadge.warn）+ 命令 chip 灰底等宽 —— */
.empty-art { display: none; }
.empty { display: block; padding: 16px 0 0; }
.empty .state { font-size: 16px; font-weight: 600; }
.empty .detail { font-size: 12px; }
.cli { background: #e0e0e0; border-radius: 0; height: 40px;
  display: inline-flex; align-items: center; padding: 0 16px;
  font-size: 12px; color: #161616; margin-top: 16px;
}
.cli-ps { color: #525252; }

/* —— 错误降级卡：红 Tag 居中 + 说明文字居中 —— */
.card.crawl.down { min-height: 200px; }
.card.crawl.down .fbadge { top: 64px; left: 50%; right: auto; transform: translateX(-50%); }
.card.crawl.down .state { display: none; }
.card.crawl.down .detail { position: absolute; left: 0; right: 0; bottom: 48px;
  text-align: center; font-size: 12px; color: #525252;
}

/* —— 内联错误通知：#FFF1F1 + 实心红图标 + 加粗标题同行正文，14px 方角 —— */
.error-banner { border-radius: 0; border-color: transparent; padding: 14px 16px;
  font-size: 14px; margin-bottom: 32px;
}
.error-banner .warn-icon:first-of-type { display: none; }
.error-banner .err-filled { display: block; }
.eb-title { font-weight: 600; color: #161616; }
.err-filled { display: none; flex: none; width: 16px; height: 16px; }
.eb-title { font-weight: inherit; }

/* —— 主按钮 96×40 蓝底白字方角；占位按钮次级样式 —— */
.btn { min-width: 96px; height: 40px; padding: 0 16px;
  font-size: 14px; box-shadow: none;
}
.btn:disabled { background: #c6c6c6; border-color: #c6c6c6; color: #f4f4f4; }
.ph-btn { background: #fff; border-color: #161616; color: #161616;
  min-width: 96px; height: 40px; padding: 0 16px; font-size: 14px;
}

/* —— 占位页：蓝色 Tag、40px 搜索框、DataTable 表头/48px 行、虚线 K 线占位 —— */
.sub { font-size: 12px; }
.note { font-size: 12px; color: #525252; }
.ph-tag { border-radius: 0; height: 20px; display: inline-flex; align-items: center;
  padding: 0 12px; font-size: 12px; line-height: 1;
}
.ph-tag.small { background: #e0e0e0; color: #161616; padding: 0 8px; }
.ph-search { height: 40px; border-radius: 0; border-color: #8d8d8d;
  padding: 0 12px; font-size: 14px; background: #fff;
}
.ph-grid { grid-template-columns: 1fr; gap: 32px; margin-top: 32px; }
.ph-table, .ph-chart,
 .ph-form,  .ph-result,
 .ph-course { border-radius: 0; box-shadow: none; }
.ph-table { padding: 0; overflow: hidden; }
.ph-table .ph-head { height: 48px; display: flex; align-items: center; padding: 0 16px;
  font-size: 12px; font-weight: 600; color: #525252;
  border-bottom: 1px solid #e0e0e0; margin-bottom: 0;
}
.ph-row { height: 48px; align-items: center; margin: 0; padding: 0 16px;
  border-bottom: 1px solid #e0e0e0;
}
.ph-row:last-child { border-bottom: 0; }
.ph-row .sk { height: 8px; }
.ph-chart { min-height: 200px; border-style: dashed; border-color: #a8a8a8; }
.ph-candle { display: none; }
.ph-chart-label { color: #525252; font-size: 12px; }
.ph-input, .ph-curve { border-radius: 0; }

/* —— 骨架卡：白卡 + #E5E5E5 灰条 —— */
.cards > .card.skeleton-card { height: 140px; }

/* ===== 数据浏览真数据表 ===== */
.browse-input {
  flex: 1; min-width: 0; border: 0; background: transparent; outline: none;
  font: inherit; font-size: 13px; color: var(--fg);
}
.browse-input::placeholder { color: var(--fg-3); }
.browse-btn { padding: 5px 14px; font-size: 12px; }
.browse-table {
  margin-top: 20px; background: var(--card-bg); backdrop-filter: var(--card-blur);
  border: 1px solid var(--card-border-color); border-radius: var(--card-radius);
  box-shadow: var(--card-shadow); overflow: hidden;
}
.browse-head, .browse-row {
  display: grid; grid-template-columns: 90px 1fr 90px 90px 80px 110px 90px;
  gap: 8px; align-items: center; padding: 9px 16px;
}
.browse-head {
  font-size: 12px; font-weight: 600; color: var(--fg-2);
  background: var(--bar-track); border-bottom: 1px solid var(--card-border-color);
}
.browse-head .num, .browse-row .num,
.browse-head .date, .browse-row .date,
.browse-head .src, .browse-row .src { text-align: center; }
.browse-head .src { font-size: 12px; }
.browse-row { font-size: 13px; color: var(--fg); border-bottom: 1px solid var(--card-border-color); }
.browse-row:last-child { border-bottom: 0; }
.browse-row:hover:not(.no-data) { background: var(--nav-hover-bg); }
.browse-row.no-data { color: var(--fg-3); }
.browse-row .code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.browse-row .num { text-align: center; font-variant-numeric: tabular-nums; }
.browse-row .up { color: var(--dot-down); font-weight: 600; }   /* A 股红涨 */
.browse-row .down { color: var(--dot-up); font-weight: 600; }   /* 绿跌 */
.browse-row .src { font-size: 11px; color: var(--fg-3); }
.browse-empty { padding: 28px 16px; font-size: 13px; color: var(--fg-3); text-align: center; }
.browse-pager {
  display: flex; align-items: center; justify-content: center; gap: 18px;
  margin-top: 16px; font-size: 12px; color: var(--fg-2);
}
.browse-head { background: #f5f5f5; }
.browse-head { height: 48px; padding: 0 16px; background: #f4f4f4; color: #525252; }
.browse-row.up { color: #c50f1f; }
.browse-row.down { color: #0f7b0f; }

/* ===== 响应式兜底 ===== */
@media (max-width: 860px) {
  .body { flex-direction: column; }
  .sidebar { width: 100%; flex-direction: row; flex-wrap: wrap; align-items: center; gap: 4px; border-right: 0; border-bottom: 1px solid var(--sidebar-border); }
  .group, .sidebar-foot { display: none; }
  .content { padding: 20px; }
  .ph-grid { grid-template-columns: 1fr; }
  .browse-table { overflow-x: auto; }
  .browse-head, .browse-row { min-width: 640px; }
  .empty { flex-direction: column; align-items: flex-start; }
}
</style>
