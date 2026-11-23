<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Select from 'primevue/select'
import MultiSelect from 'primevue/multiselect'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import Paginator from 'primevue/paginator'
import AutoComplete from 'primevue/autocomplete'
import * as echarts from 'echarts/core'
import { CandlestickChart, BarChart, LineChart, ScatterChart } from 'echarts/charts'
import { mountDca3DScatter } from './dca3d'
import { GridComponent, TooltipComponent, DataZoomComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([CandlestickChart, BarChart, LineChart, ScatterChart, GridComponent,
  TooltipComponent, DataZoomComponent, LegendComponent, CanvasRenderer])

// ===== 数据层（沿用原仪表盘逻辑：三接口并发 + 优雅降级）=====
const backend = ref({ status: 'checking' })   // 后端进程本身
const infra = ref(null)                       // PostgreSQL / 代理池
const crawl = ref(null)                       // 爬虫数据统计
const pendingBackend = ref(true)              // 三路探测各自独立结算（渐进上屏）
const pendingInfra = ref(true)
const pendingCrawl = ref(true)
// ===== 静态快照模式（GitHub Pages：VITE_STATIC=1 构建，数据内置于 /data） =====
// 数据为离线导出快照：排行/散点/分箱/每轮明细全客户端化；无净值曲线、无爬虫管理
const STATIC_MODE = import.meta.env.VITE_STATIC === '1'
const DATA_BASE = `${import.meta.env.BASE_URL}data`

const activeNav = ref(STATIC_MODE ? 'fundDca' : 'dash')   // dash | data | src-sina | src-ef | backtest | learn
const sidebarOpen = ref(false)   // ≤820px：侧栏折叠抽屉开关（桌面端恒展开无影响）
// 桌面端（>820px）侧栏折叠开关：收窄为 56px 图标窄条，状态记忆 localStorage。
// 汉堡按钮按视口宽分流：移动端开合抽屉（sidebarOpen），桌面端切换窄条。
const SIDEBAR_COLLAPSED_KEY = 'hm-sidebar-collapsed'
const sidebarCollapsed = ref(false)
try { sidebarCollapsed.value = localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1' } catch {}
function toggleSidebar() {
  if (window.matchMedia('(max-width: 820px)').matches) {
    sidebarOpen.value = !sidebarOpen.value
    return
  }
  sidebarCollapsed.value = !sidebarCollapsed.value
  try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed.value ? '1' : '0') } catch {}
}

// ===== 路由：URL 路径 ↔ 页面，查询串 ↔ 参数（刷新/分享/前进后退恢复） =====
// 手写 History API 同步（单组件架构，无需 vue-router）；nginx/vite preview
// 均有 SPA 回退，深链接刷新可用。页面切换 pushState，参数变化 replaceState。
const ROUTE_BY_NAV = {
  dash: '/', data: '/data', 'src-sina': '/data/sina',
  'src-ef': '/data/eastmoney', funds: '/data/funds', tasks: '/tasks',
  fundDca: '/data/fund-dca', market: '/data/market-compare',
  backtest: '/backtest', learn: '/learn', stock: '/stock', fund: '/fund',
}
const NAV_BY_PATH = Object.fromEntries(
  Object.entries(ROUTE_BY_NAV).map(([nav, path]) => [path, nav]))

function buildQuery() {
  const q = new URLSearchParams()
  const nav = activeNav.value
  if (nav === 'learn') {
    q.set('strategy', learnStrat.value)
    q.set('mode', learnMode.value)
    if (learnCodePick.value.length)
      q.set('codes', learnCodePick.value.map(_pickToCode).join(','))
    q.set('range', learnRange.value)
    q.set('exec', learnExec.value)
    q.set('fee', learnCommission.value)
    q.set('tax', learnSellTax.value)
    const pa = Object.entries(learnParams.value)
    if (pa.length) q.set('pa', pa.map(([k, v]) => `${k}:${v}`).join(','))
    q.set('fst', fullFilterSt.value ? 1 : 0)
    q.set('mbars', fullMinBars.value)
    q.set('adj', fullAdjust.value ? 1 : 0)
    q.set('incd', fullIncludeDelisted.value ? 1 : 0)
    q.set('mnc', fullMinCap.value)
    q.set('mxc', fullMaxCap.value)
    q.set('mlot', fullMaxLot.value)
  } else if (nav === 'stock') {
    if (stock.value?.code) q.set('code', stock.value.code)
    if (stockAdjust.value) q.set('adjust', stockAdjust.value)
    if (stock.value?.source) q.set('source', stock.value.source)
  } else if (nav === 'fund') {
    if (fund.value?.code) q.set('code', fund.value.code)
  } else if (nav === 'data' || nav === 'src-sina' || nav === 'src-ef') {
    if (browseQuery.value) q.set('q', browseQuery.value)
    if (browsePage.value) q.set('page', browsePage.value)
  } else if (nav === 'funds') {
    if (fundsQuery.value) q.set('q', fundsQuery.value)
    if (fundsPage.value) q.set('page', fundsPage.value)
    if (fundsDelisted.value) q.set('delisted', fundsDelisted.value)
    if (fundsShowBackend.value) q.set('backend', '1')
    if (fundsShowMoney.value) q.set('money', '1')
    if (fundsShowQdii.value) q.set('qdii', '1')
  } else if (nav === 'fundDca') {
    if (dcaQuery.value) q.set('q', dcaQuery.value)
    if (dcaPage.value) q.set('page', dcaPage.value)
    if (dcaAlive.value !== 'true') q.set('alive', dcaAlive.value)
    if (dcaAge.value) q.set('age', dcaAge.value)
    if (dcaScale.value) q.set('scale', dcaScale.value)
    if (dcaEquity.value) q.set('equity', '1')
    if (dcaTp.value !== 0.20) q.set('tp', dcaTp.value)
    if (dcaMode.value !== 'amount') q.set('mode', dcaMode.value)
    if (dcaSort.value !== 'w10_xirr') q.set('sort', dcaSort.value)
    if (dcaOrder.value !== 'desc') q.set('order', dcaOrder.value)
  }
  return q
}

function syncUrl() {
  if (STATIC_MODE) return   // Pages 子路径下 replaceState 会让刷新 404，静态模式停用 URL 同步
  const path = ROUTE_BY_NAV[activeNav.value] || '/'
  const qs = buildQuery().toString()
  history.replaceState(null, '', path + (qs ? `?${qs}` : ''))
}

let syncTimer = null
function scheduleSync() {
  clearTimeout(syncTimer)
  syncTimer = setTimeout(syncUrl, 250)         // 参数变化防抖，replace 不灌历史
}

function setNav(nav) {
  activeNav.value = nav
  sidebarOpen.value = false   // 移动端抽屉：选中即收起
  if (!STATIC_MODE) {
    history.pushState(null, '', ROUTE_BY_NAV[nav] || '/')
    scheduleSync()
  }
}

function applyUrl() {
  const u = new URL(window.location.href)
  const nav = NAV_BY_PATH[u.pathname]
  if (nav) activeNav.value = nav
  const q = u.searchParams
  // 教学（白名单校验，非法值静默忽略）
  if (STRATS.some(s => s.id === q.get('strategy')))
    learnStrat.value = q.get('strategy')
  if (['single', 'batch', 'full'].includes(q.get('mode')))
    learnMode.value = q.get('mode')
  if (q.get('codes') != null)
    learnCodePick.value = q.get('codes').split(',').filter(Boolean)
      .map(c => ({ code: c, name: c, label: c }))
  if (['1y', '3y', '5y', 'all'].includes(q.get('range')))
    learnRange.value = q.get('range')
  if (['next_open', 'close'].includes(q.get('exec')))
    learnExec.value = q.get('exec')
  if (q.get('fee') != null && !Number.isNaN(+q.get('fee')))
    learnCommission.value = +q.get('fee')
  if (q.get('tax') != null && !Number.isNaN(+q.get('tax')))
    learnSellTax.value = +q.get('tax')
  if (q.get('pa')) {
    const pa = {}
    for (const kv of q.get('pa').split(',')) {
      const [k, v] = kv.split(':')
      if (k) pa[k] = v !== '' && !Number.isNaN(+v) ? +v : v
    }
    if (Object.keys(pa).length) learnParams.value = pa
  }
  if (q.get('fst') != null) fullFilterSt.value = q.get('fst') === '1'
  if (q.get('mbars') != null && !Number.isNaN(+q.get('mbars')))
    fullMinBars.value = +q.get('mbars')
  if (q.get('adj') != null) fullAdjust.value = q.get('adj') === '1'
  if (q.get('incd') != null) fullIncludeDelisted.value = q.get('incd') === '1'
  if (q.get('mnc') != null && !Number.isNaN(+q.get('mnc')))
    fullMinCap.value = +q.get('mnc')
  if (q.get('mxc') != null && !Number.isNaN(+q.get('mxc')))
    fullMaxCap.value = +q.get('mxc')
  if (q.get('mlot') != null && !Number.isNaN(+q.get('mlot')))
    fullMaxLot.value = +q.get('mlot')
  // 个股详情
  if (NAV_BY_PATH[u.pathname] === 'stock' && q.get('code')) {
    stock.value = { code: q.get('code'), name: q.get('code'),
                    source: q.get('source') || 'sina' }
    stockAdjust.value = q.get('adjust') || ''
    detailPage.value = 0
  }
  // 基金净值详情
  if (NAV_BY_PATH[u.pathname] === 'fund' && q.get('code')) {
    fund.value = { code: q.get('code'), name: q.get('code'), fund_type: '' }
    fundDetailPage.value = 0
  }
  // 数据浏览
  if (['data', 'src-sina', 'src-ef'].includes(NAV_BY_PATH[u.pathname]) &&
      (q.get('q') != null || q.get('page'))) {
    browseQuery.value = q.get('q') ?? ''
    browsePage.value = +q.get('page') || 0
    loadBrowse()
  }
  // 基金列表
  if (NAV_BY_PATH[u.pathname] === 'funds' &&
      (q.get('q') != null || q.get('page') || q.get('delisted')
       || q.get('backend') || q.get('money') || q.get('qdii'))) {
    fundsQuery.value = q.get('q') ?? ''
    fundsPage.value = +q.get('page') || 0
    fundsDelisted.value = q.get('delisted') ?? ''
    fundsShowBackend.value = q.get('backend') === '1'
    fundsShowMoney.value = q.get('money') === '1'
    fundsShowQdii.value = q.get('qdii') === '1'
    loadFunds()
  }
  // 定投止盈回测排行
  if (NAV_BY_PATH[u.pathname] === 'fundDca' &&
      (q.get('q') != null || q.get('page') || q.get('alive') != null
       || q.get('age') || q.get('scale') || q.get('equity') != null
       || q.get('tp') != null || q.get('mode') != null
       || q.get('sort') || q.get('order'))) {
    dcaQuery.value = q.get('q') ?? ''
    dcaPage.value = +q.get('page') || 0
    if (q.get('alive') != null) dcaAlive.value = q.get('alive')
    if (q.get('age') != null && /^\d+$/.test(q.get('age'))) dcaAge.value = +q.get('age')
    if (q.get('scale') != null && /^\d+(\.\d+)?$/.test(q.get('scale'))) dcaScale.value = +q.get('scale')
    if (q.get('equity') != null) dcaEquity.value = ['1', 'true'].includes(q.get('equity'))
    if (q.get('tp') != null) {
      const v = parseFloat(q.get('tp'))
      if (DCA_TP_OPTIONS.includes(v)) dcaTp.value = v
    }
    if (q.get('mode') != null
        && DCA_MODE_OPTIONS.some(o => o.key === q.get('mode')))
      dcaMode.value = q.get('mode')
    if (DCA_SORTABLE.includes(q.get('sort'))) dcaSort.value = q.get('sort')
    if (['asc', 'desc'].includes(q.get('order'))) dcaOrder.value = q.get('order')
    loadDca()
  }
}



// 数据源子页（对应 Carbon 原型 ⑥⑦ 屏）：按来源筛选的数据浏览占位页
const SRC_ITEMS = [
  { id: 'src-sina', label: '新浪财经', field: 'sina', tagCls: 'src-sina' },
  { id: 'src-ef', label: '东方财富', field: 'eastmoney', tagCls: 'src-ef' },
]

// 仪表盘自愈：部署/重启窗口里 nginx 会短暂 502（compose 返回 ≠ 后端
// lifespan 完成），失败按 2/5/10/30s 退避重试（约 2 分钟自愈窗口），
// 全部成功即停；手动「重新检测」/聚焦回页面都会重置退避重取
const DASH_RETRY_DELAYS = [2000, 5000, 10000, 30000]
const dashRetryIdx = ref(0)
let dashRetryTimer = null

function refresh(fromRetry = false) {
  if (!fromRetry) {
    dashRetryIdx.value = 0
    clearTimeout(dashRetryTimer)
  }
  // 三路探测各自独立结算，谁先回来谁先上屏——/api/infra 要探 PG/代理池、
  // /api/crawl/stats 走大表统计，慢的一路不再拖着整页骨架空转
  pendingBackend.value = pendingInfra.value = pendingCrawl.value = true
  const probe = (url, pendingRef, onOk) => fetch(url).then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`)   // nginx 502 返回 HTML，非 JSON
    return r.json()
  }).then(v => { pendingRef.value = false; onOk(v); return true },
          () => { pendingRef.value = false; onOk(null); return false })
  Promise.all([
    probe('/healthz', pendingBackend, v => {
      backend.value = v || { status: 'down', detail: '探测失败' }
    }),
    probe('/api/infra', pendingInfra, v => { infra.value = v }),   // null → PG/代理池按未连接显示
    probe('/api/crawl/stats', pendingCrawl, v => { crawl.value = v }), // null → 爬虫卡按数据不可用显示
  ]).then(ok => {
    if (ok.every(Boolean) || dashRetryIdx.value >= DASH_RETRY_DELAYS.length)
      return
    const delay = DASH_RETRY_DELAYS[dashRetryIdx.value]
    dashRetryIdx.value += 1
    dashRetryTimer = setTimeout(() => refresh(true), delay)
  })
}

function _onDashVisible() {
  if (!document.hidden && activeNav.value === 'dash') refresh()
}
onMounted(() => document.addEventListener('visibilitychange', _onDashVisible))
onUnmounted(() => {
  document.removeEventListener('visibilitychange', _onDashVisible)
  clearTimeout(dashRetryTimer)
})

const fmt = n => (n ?? 0).toLocaleString('zh-CN')
const backendDown = computed(() => backend.value.status === 'down')
const checking = computed(() => backend.value.status === 'checking'
  || pendingBackend.value || pendingInfra.value || pendingCrawl.value)
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
// 退市过滤：'' 全部 / 'false' 仅在市 / 'true' 仅退市（值直接对应 stock_basic.delisted）
const browseDelisted = ref('')
const DELISTED_FILTERS = [
  { label: '退市：全部', value: '' },
  { label: '退市：仅在市', value: 'false' },
  { label: '退市：仅退市', value: 'true' },
]
const browseTotalPages = computed(() =>
  Math.max(1, Math.ceil((browse.value.total || 0) / BROWSE_PAGE_SIZE)))

async function loadBrowse() {
  browseLoading.value = true
  try {
    const q = encodeURIComponent(browseQuery.value.trim())
    const off = browsePage.value * BROWSE_PAGE_SIZE
    const src = srcPage.value ? `&source=${srcPage.value.field}` : ''
    const dl = browseDelisted.value ? `&delisted=${browseDelisted.value}` : ''
    const r = await fetch(`/api/stocks?q=${q}&limit=${BROWSE_PAGE_SIZE}&offset=${off}${src}${dl}`)
    browse.value = await r.json()
  } catch (e) {
    browse.value = { status: 'down', total: 0, stocks: [] }
  } finally {
    browseLoading.value = false
  }
}
function browseSearch() { browsePage.value = 0; loadBrowse() }

// ===== 基金净值浏览（数据子页：/api/funds 列表 + /api/funds/{code}/navs 详情） =====
const funds = ref({ status: 'loading', total: 0, funds: [] })
const fundsQuery = ref('')
const fundsPage = ref(0)                  // 0 起
// 退市过滤：'' 全部 / 'false' 仅在市 / 'true' 仅退市（fund_basic.delisted，
// 爬虫按天天基金当前净值列表与全集差集标记——差集=清盘/终止/不再披露）
const fundsDelisted = ref('')
// 后端收费份额显示开关：默认隐藏（东财此类份额名称带"(后端)"、源上普遍
// 无独立净值数据，列表常驻意义小），按钮打开后全量显示
const fundsShowBackend = ref(false)
// 货币基金显示开关：默认隐藏（摊余成本法货基净值恒 1、无单位净值走势，
// fund_type 含"货币"），按钮打开后显示
const fundsShowMoney = ref(false)
// QDII 显示开关：默认隐藏（fund_type 含"QDII"，净值披露 T+1/T+2 滞后）
const fundsShowQdii = ref(false)
const fundsLoading = ref(false)
const FUNDS_PAGE_SIZE = 50
// 波动指标列排序（服务端白名单）：'' 默认 code 序；lazy @sort 事件驱动
const fundsSort = ref('')
const fundsOrder = ref('desc')
const fundsTotalPages = computed(() =>
  Math.max(1, Math.ceil((funds.value.total || 0) / FUNDS_PAGE_SIZE)))

async function loadFunds() {
  fundsLoading.value = true
  try {
    const q = encodeURIComponent(fundsQuery.value.trim())
    const off = fundsPage.value * FUNDS_PAGE_SIZE
    const dl = fundsDelisted.value ? `&delisted=${fundsDelisted.value}` : ''
    const be = fundsShowBackend.value ? '&show_backend=1' : ''
    const mn = fundsShowMoney.value ? '&show_money=1' : ''
    const qd = fundsShowQdii.value ? '&show_qdii=1' : ''
    const so = fundsSort.value ? `&sort=${fundsSort.value}&order=${fundsOrder.value}` : ''
    const r = await fetch(`/api/funds?q=${q}&limit=${FUNDS_PAGE_SIZE}&offset=${off}${dl}${be}${mn}${qd}${so}`)
    funds.value = await r.json()
  } catch (e) {
    funds.value = { status: 'down', total: 0, funds: [] }
  } finally {
    fundsLoading.value = false
  }
}
function fundsSearch() { fundsPage.value = 0; loadFunds() }
function toggleFundsBackend() {
  fundsShowBackend.value = !fundsShowBackend.value
  fundsSearch()
}
function toggleFundsMoney() {
  fundsShowMoney.value = !fundsShowMoney.value
  fundsSearch()
}
function toggleFundsQdii() {
  fundsShowQdii.value = !fundsShowQdii.value
  fundsSearch()
}
function onFundsPage(e) { fundsPage.value = e.page; loadFunds() }
function onFundsSort(e) {
  // PrimeVue lazy 排序：sortField=列 field，sortOrder 1/-1；白名单在
  // 后端（_FUND_SORT_COLS），非法值回落 code——前端只透传
  fundsSort.value = e.sortField || ''
  fundsOrder.value = e.sortOrder === 1 ? 'asc' : 'desc'
  fundsPage.value = 0
  loadFunds()
}

// ===== 定投止盈回测排行（数据子页：/api/funds/dca 快照，导入见 app/dca_store） =====
const dca = ref({ status: 'loading', total: 0, rows: [], stats: null, meta: null })
const dcaQuery = ref('')
const dcaPage = ref(0)                    // 0 起
// 在市过滤：'true' 仅在市（默认）/ 'false' 仅清盘退市 / '' 全部；
// alive 由导入器按「最后净值日距数据集最新 45 天内」计算（delisted 字段对场内 ETF 不可靠）
const dcaAlive = ref('true')
const dcaSort = ref('w10_xirr')
const dcaOrder = ref('desc')
const dcaLoading = ref(false)
const DCA_PAGE_SIZE = 50
const DCA_SORTABLE = ['w10_xirr', 'full_xirr', 'w10_profit_rate',
  'full_profit_rate', 'w10_rounds', 'scale_yi', 'code',
  'dd_current', 'days_since_peak']   // 时机列（2026-11-17 表格合并）
const DCA_ALIVE_FILTERS = [
  { label: '仅在市', value: 'true' },
  { label: '仅清盘/退市', value: 'false' },
  { label: '全部', value: '' },
]
// 运作年限筛选：整数 N → 只看运作 >N 年的基金（首条净值距数据集最新净值日）；
// 0/留空 = 全部
const dcaAge = ref(null)
// 规模筛选：N（亿）→ 只看规模 >N 亿的基金；0/留空 = 全部（含无规模披露）
const dcaScale = ref(null)
// 权益类筛选：true 仅权益类（默认，2026-11-15 用户确认口径）/ false 全市场。
// 白名单=东财 fund_type 8 类（核心三类+灵活/平衡混合+海外权益），与后端
// app.dca_api.EQUITY_TYPES 保持一致；静态模式用它做客户端过滤（快照恒为全市场）
const DCA_EQUITY_TYPES = ['股票型', '混合型-偏股', '混合型-灵活', '混合型-平衡',
  '指数型-股票', '指数型-海外股票', 'QDII-普通股票', 'QDII-混合偏股']
const dcaEquity = ref(true)

// ===== 多止盈点市场级对比（2026-11-15 grilling 定案，同日追加 35%/40% → 5%~40% 八档） =====
// 阈值切换只改排行/统计/弹窗明细的数据源（tp≠0.2 走 /api/funds/dca/tp*，
// 行结构同旧端点 + tp 一列）；散点/分箱跟随定投方式与止盈点（顶部总开关）；
// 静态模式（冻结站点）无多阈值数据，切换器与对比区块整体隐藏
const DCA_TP_OPTIONS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
const dcaTp = ref(0.20)
// ===== 定投方式切换（2026-11-23：等额定投 / 等份额定投，四组总开关置页面最前） =====
// 等份额口径：每期固定到手份额 = 首期 1000 元折算，现金随净值波动（涨时
// 花得多、跌时花得少，成本摊平效果最差的反面基准）；顶部总开关四组
// （定投方式/止盈点/基金类型/费用口径）切换后排行/统计卡/对比卡/散点分箱/
// 弹窗明细全部跟随（起点条件与熊市
const DCA_MODE_OPTIONS = [
  { key: 'amount', label: '等额定投' },
  { key: 'share', label: '等份额定投' },
  { key: 'dd_ladder', label: '回撤加码' },
]
// 定投方式说明（2026-11-23 五轮：? 图标点击弹出全部说明；悬停按钮 title
// 显示对应方式详情）——brief 是面板里的一句话定位，detail 是完整口径
const DCA_MODE_HELP = {
  amount: {
    title: '等额定投',
    brief: '每期固定金额',
    detail: '每月首个净值日投入固定 1000 元（申购费按外扣法折份额）。最朴素的纪律买入，与工资现金流最匹配——任何行情下每期投入都一样，无需盯盘判断。',
  },
  share: {
    title: '等份额定投',
    brief: '每期固定份额',
    detail: '每期买入固定份额（以首期 1000 元折算为基准），现金支出随净值波动——涨时花得多、跌时花得少。成本摊平效果在三种方式中最差，通常作为对照基准。',
  },
  dd_ladder: {
    title: '回撤加码',
    brief: '跌得越深买得越多',
    detail: '每月基础 1000 元，买入日较历史最高回撤 ≥10% 投 1.5 倍、≥20% 投 2 倍（余者 1 倍）——深跌时买入更多便宜份额、摊低持仓成本。口径源自熊市加倍实验（全市场 9,576 只：回撤 ≤-20% 加倍投 2×，68% 基金优于坚持等额定投）。代价是回撤期投入放大，需预留现金。',
  },
}
const dcaModeHelpShow = ref(false)
const dcaMode = ref('amount')
const dcaModeLabel = computed(() =>
  (DCA_MODE_OPTIONS.find(o => o.key === dcaMode.value) || {}).label || '')
const dcaTpCompare = ref(null)
const dcaTpLoading = ref(false)
const dcaTpLabel = tp => (tp * 100).toFixed(0) + '%'
// 弹窗局部阈值（独立于页面级 dcaTp：弹窗内切换只改本弹窗的统计/轮次表，
// 不波及排行）；dcaRoundsAll 为全档位轮次（图上叠加全部止盈点，颜色按档位）
const dcaRoundsTp = ref(0.20)
const dcaRoundsAll = ref({})
const DCA_TP_COLORS = { 0.05: '#E69F00', 0.10: '#56B4E9', 0.15: '#009E73',
  0.20: '#D55E00', 0.25: '#CC79A7', 0.30: '#0072B2',
  0.35: '#999999', 0.40: '#111111' }
const dcaRoundsFee = ref('nom')          // 弹窗局部费率口径（初始随页面）
const dcaRoundsFeeLabel = computed(() =>
  (DCA_FEE_OPTIONS.find(o => o.key === dcaRoundsFee.value) || {}).label || '')
const dcaRoundsSummary = ref(null)       // 单基金全阈值汇总（总收益率/年化，恒带三口径）

let dcaAllRows = []   // 静态模式：全量行一次加载，之后纯客户端过滤/排序/分页

function pctCont(sorted, p) {
  if (!sorted.length) return null
  const k = (sorted.length - 1) * p
  const f = Math.floor(k), c = Math.min(f + 1, sorted.length - 1)
  return sorted[f] + (sorted[c] - sorted[f]) * (k - f)
}

// 静态模式：alive/年限/规模/权益过滤 + 排序 + 分页 + 统计卡（与 server 同语义：
// stats 跟随 alive/age/scale/equity 不随搜索词；基于当前费率口径年化非空行，
// 排序与统计取值都走 dcaFeeVal 按口径解析）
function dcaStaticView() {
  const q = dcaQuery.value.trim().toLowerCase()
  const alive = dcaAlive.value
  const ageN = dcaAge.value || 0
  const scaleN = dcaScale.value || 0
  const base = dcaAllRows.filter(r =>
    (alive === '' || (alive === 'true' ? r.alive === true : r.alive === false))
    && (!ageN || (r.age_years != null && r.age_years > ageN))
    && (!scaleN || (r.scale_yi != null && r.scale_yi > scaleN))
    && (!dcaEquity.value || DCA_EQUITY_TYPES.includes(r.fund_type)))
  const rows = base.filter(r =>
    !q || r.code.toLowerCase().includes(q) || r.name.toLowerCase().includes(q))
  const key = dcaSort.value, dir = dcaOrder.value === 'asc' ? 1 : -1
  rows.sort((a, b) => {
    const va = dcaFeeVal(a, key), vb = dcaFeeVal(b, key)
    if (va == null) return 1
    if (vb == null) return -1
    return va < vb ? -dir : va > vb ? dir : 0
  })
  const off = dcaPage.value * DCA_PAGE_SIZE
  const xs = base.map(r => dcaFeeVal(r, 'w10_xirr')).filter(v => v != null).sort((a, b) => a - b)
  const stats = xs.length ? {
    count: xs.length, p25: pctCont(xs, 0.25), median: pctCont(xs, 0.5),
    p75: pctCont(xs, 0.75), p90: pctCont(xs, 0.9),
    positive_share: xs.filter(v => v > 0).length / xs.length,
    take_profit_share: base.filter(r => (dcaFeeVal(r, 'w10_rounds') || 0) > 0).length / xs.length,
  } : null
  return { status: 'up', total: rows.length, rows: rows.slice(off, off + DCA_PAGE_SIZE),
           stats, meta: dcaStaticMeta }
}

let dcaStaticMeta = null
async function loadDca() {
  if (STATIC_MODE) {
    dcaLoading.value = true
    try {
      if (!dcaAllRows.length) {
        const r = await fetch(`${DATA_BASE}/funds/dca.json`)
        const body = await r.json()
        dcaAllRows = body.rows || []
        dcaStaticMeta = body.meta || null
      }
      dca.value = dcaStaticView()
    } catch (e) {
      dca.value = { status: 'down', detail: String(e), total: 0, rows: [],
                    stats: null, meta: null }
    } finally {
      dcaLoading.value = false
    }
    return
  }
  dcaLoading.value = true
  try {
    const q = encodeURIComponent(dcaQuery.value.trim())
    const off = dcaPage.value * DCA_PAGE_SIZE
    const age = dcaAge.value ? `&min_age=${dcaAge.value}` : ''
    const sc = dcaScale.value ? `&min_scale=${dcaScale.value}` : ''
    // 阈值/方式切换：等额 + 20%（默认）走旧端点（fund_dca_result），其余
    // 走多阈值端点（fund_dca_tp_result，行结构多 tp/method 维度，数值同源）
    const base = (dcaTp.value === 0.20 && dcaMode.value === 'amount')
      ? '/api/funds/dca?'
      : `/api/funds/dca/tp?tp=${dcaTp.value}&method=${dcaMode.value}&`
    const r = await fetch(`${base}q=${q}&alive=${dcaAlive.value}${age}${sc}`
      + `&equity=${dcaEquity.value}`
      + `&sort=${dcaSort.value}&order=${dcaOrder.value}`
      + `&fee=${dcaFeeMode.value}`
      + `&limit=${DCA_PAGE_SIZE}&offset=${off}${dcaRangeQuery()}`)
    dca.value = await r.json()
  } catch (e) {
    dca.value = { status: 'down', detail: String(e), total: 0, rows: [],
                  stats: null, meta: null }
  } finally {
    dcaLoading.value = false
  }
}
function dcaSearch() { dcaPage.value = 0; loadDca() }
// 阈值切换（顶部总开关）：排行/统计/对比高亮/散点分箱/弹窗明细跟随
function onDcaTpChange(tp) {
  if (dcaTp.value === tp) return
  dcaTp.value = tp
  dcaPage.value = 0
  loadDca()
  loadDcaScatter()
}
// 定投方式切换（页面置顶总开关）：排行/统计/对比卡/散点分箱/弹窗明细全部
// 跟随（起点条件与熊市断缴实验卡为等额固定快照）
function onDcaModeChange(mode) {
  if (dcaMode.value === mode) return
  dcaMode.value = mode
  dcaPage.value = 0
  loadDca()
  loadDcaTpCompare()
  loadDcaScatter()
}
// 八档对比聚合（跟随费用口径/定投方式/基金类型开关——equity=true 为权益+商品
// 池（对比专用口径），false 全市场对照；三轮需求：所有统计跟随总开关）
async function loadDcaTpCompare() {
  if (STATIC_MODE) return
  dcaTpLoading.value = true
  try {
    const r = await fetch(`/api/funds/dca/tp/compare?fee=${dcaFeeMode.value}&method=${dcaMode.value}&equity=${dcaEquity.value}`)
    dcaTpCompare.value = await r.json()
  } catch (e) {
    dcaTpCompare.value = { status: 'down', detail: String(e), rows: [] }
  } finally {
    dcaTpLoading.value = false
  }
}
// 中位年化·近10年 vs 止盈点 折线（手写 SVG 坐标，无图表库依赖；点可点击切换阈值）
const dcaTpChart = computed(() => {
  const rows = (dcaTpCompare.value?.rows || []).filter(r => r.w10_median != null)
  if (rows.length < 2) return null
  const xs = rows.map(r => r.tp)
  const ys = rows.map(r => r.w10_median)
  const x0 = Math.min(...xs), x1 = Math.max(...xs)
  const y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0)
  const W = 640, H = 150, padL = 56, padR = 16, padT = 26, padB = 26
  const sx = v => padL + (v - x0) / ((x1 - x0) || 1) * (W - padL - padR)
  const sy = v => padT + (y1 - v) / ((y1 - y0) || 1) * (H - padT - padB)
  return {
    W, H,
    pts: rows.map(r => ({ x: sx(r.tp), y: sy(r.w10_median), tp: r.tp, v: r.w10_median })),
    zeroY: sy(0),
  }
})

// ===== 定投时机（2026-11-16 假设验证：-25~-30% 回撤起定投首轮止盈最快/
// 成功率与年化最高；30%+ 超深回撤起不来风险）。2026-11-17 表格合并：
// 时机列（当前回撤/距前高）由后端 LEFT JOIN fund_dip_timing 并入下方排行
// 表，两张基金表合一；回撤桶按钮组删除（与区间筛选的当前回撤维度重复，
// 用户确认保留区间筛选）；五维度区间筛选迁到排行区作用于统一表格
const ddpct = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
// 波动指标格式化（fund_risk_metrics 小数口径：0.185 → 18.5%）
const volpct = v => v == null ? '—' : (v * 100).toFixed(1) + '%'          // 波动率
const mddneg = v => v == null ? '—' : (v > 0 ? '-' : '') + (v * 100).toFixed(1) + '%' // 最大回撤（存正数显负号）
const pctile = v => v == null ? '—' : Math.round(v * 100) + '%'           // 波动率历史分位
const oscyr = v => v == null ? '—' : v.toFixed(2) + ' 轮/年'               // 年均规则摆动轮数
const sharpe2 = v => v == null ? '—' : v.toFixed(2)                        // 夏普（裸数值两位）
// 五维度区间筛选（空=不筛；回撤输入正数幅度）。应用后服务端过滤（分页
// 表格客户端筛当前页无意义）；筛选面板常显（2026-11-17 用户反馈，去折叠
// 按钮）；会话内探索状态，不进 URL
const emptyRangeFilters = () => ({ age_min: null, age_max: null,
  gap_min: null, gap_max: null, xirr_min: null, xirr_max: null,
  dd_min: null, dd_max: null, pr_min: null, pr_max: null })
const dcaRangeFilters = ref(emptyRangeFilters())
const dcaRangeFiltersActive = ref(false)
// 区间筛选参数串：拼进统一表格 loadDca 的请求（旧端点与多阈值端点同支持）
function dcaRangeQuery() {
  let fs = ''
  if (dcaRangeFiltersActive.value) {
    for (const [k, v] of Object.entries(dcaRangeFilters.value)) {
      if (v != null && v !== '') fs += `&${k}=${v}`
    }
  }
  return fs
}
function applyDcaRangeFilters() {
  dcaRangeFiltersActive.value = true
  dcaSearch()
}
function resetDcaRangeFilters() {
  dcaRangeFilters.value = emptyRangeFilters()
  if (!dcaRangeFiltersActive.value) return
  dcaRangeFiltersActive.value = false
  dcaSearch()
}

// ===== 列显示过滤（2026-11-17 用户需求：用户自决定表格显示哪些列）=====
// 代码/名称/操作恒显，其余列可勾选（默认全显）；纯客户端裁剪，不影响
// 服务端分页/排序；会话内状态，不进 URL
const DCA_TOGGLE_COLS = [
  { key: 'fund_type', label: '类型' },
  { key: 'dd_current', label: '当前回撤' },
  { key: 'days_since_peak', label: '距前高' },
  { key: 'age_years', label: '运作年限' },
  { key: 'scale_yi', label: '规模' },
  { key: 'vol_all', label: '年化波动率' },
  { key: 'vol_down_all', label: '下行波动率' },
  { key: 'mdd_all', label: '最大回撤' },
  { key: 'sharpe_all', label: '夏普' },
  { key: 'vol_pctile', label: '波动率分位' },
  { key: 'risk_score', label: '综合波动分位' },
  { key: 'osc_pctile', label: '震荡分位' },
  { key: 'gap_avg_days', label: '止盈间隔' },
  { key: 'w10_rounds', label: '止盈次数' },
  { key: 'w10_profit_rate', label: '总收益率' },
  { key: 'w10_xirr', label: '年化·近10年' },
  { key: 'full_xirr', label: '年化·全史' },
  { key: 'full_cur_profit_rate', label: '止盈后收益率' },
  { key: 'full_cur_xirr', label: '止盈后年化' },
  { key: 'w10_range', label: '回测区间' },
]
const dcaVisibleCols = ref(DCA_TOGGLE_COLS.map(c => c.key))
const colVisible = key => dcaVisibleCols.value.includes(key)

// ===== 起点条件回测区块（假设验证统计：不同起点开始定投的结果差异；
// 静态快照 fund_dip_validation，验证重跑后刷新，不随口径变化）
const dcaDipReport = ref(null)
async function loadDcaDipReport() {
  if (STATIC_MODE) return
  try {
    const r = await fetch('/api/funds/dca/dip-report')
    dcaDipReport.value = await r.json()
  } catch (e) {
    dcaDipReport.value = { status: 'down', detail: String(e), groups: [],
                           buckets: [], years: [] }
  }
}
const dipDay = v => v == null ? '—' : Math.round(v).toLocaleString() + ' 天'
const dipPct = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
const DIP_BUCKET_LABELS = { '20-25': '回撤 -20%~-25%', '25-30': '回撤 -25%~-30%', '30+': '回撤 -30% 以上' }
function dipRowLabel(r) {
  if (r.bucket === 'all') {
    return { SYS: '随机起点（基线）', NODIP: '非深回撤起点', DIP: '深回撤起点（≥20%）' }[r.grp] || r.grp
  }
  if (r.grp === 'DIP') return DIP_BUCKET_LABELS[r.bucket] || r.bucket
  return `${r.bucket} 年起`
}

// ===== 熊市断缴实验区块（2026-11-19 scratch 实验产品化：熊市态断缴 vs 坚持
// 定投，同基金四条时间线配对比较；静态快照 fund_bear_pause_*，导入脚本
// scripts/bear_pause_import.py 重跑后刷新；gross 无费固定口径，不随费率/
// 权益开关变化，因此不挂 watch 只在进入子页时加载一次）
const dcaPauseExp = ref(null)
const dcaPauseMode = ref('tp')   // 口径：tp=止盈20%（主线）/ nt=无止盈（躺平）
const PAUSE_MODES = [{ key: 'tp', label: '止盈 20%' }, { key: 'nt', label: '无止盈（躺平）' }]
const PAUSE_SUBSET_ORDER = ['all', 'paused_only', 'near_high', 'mid', 'stuck']
const PAUSE_SUBSET_LABELS = { all: '全体基金', paused_only: '仅断缴过的基金',
  near_high: '末回撤 >-10%（接近前高）', mid: '末回撤 -30%~-10%（中间地带）',
  stuck: '末回撤 ≤-30%（起不来）' }
const PAUSE_ARMS = [
  { key: 'a', label: 'A 坚持定投', tip: '每月首个净值日投 1000，从不间断（基准组）' },
  { key: 'b', label: 'B 断缴闲置', tip: '熊市态停扣，钱囤现金 0 收益，恢复后继续月投' },
  { key: 'c', label: 'C 断缴补投', tip: '同 B 停扣，恢复确认日把囤的钱一次性补回' },
  { key: 'd', label: 'D 熊市加倍', tip: '熊市态每月投 2000（钱包差已按多投入 0 收益折算同额）' },
]
async function loadDcaPauseExperiment() {
  if (STATIC_MODE) return
  try {
    const r = await fetch('/api/funds/dca/pause-experiment')
    dcaPauseExp.value = await r.json()
  } catch (e) {
    dcaPauseExp.value = { status: 'down', detail: String(e), meta: {}, market: {}, cases: [] }
  }
}
const dcaPauseUniverse = computed(() => dcaPauseExp.value?.meta?.params?.universe || {})
const dcaPauseBlocks = computed(() => dcaPauseExp.value?.market?.[dcaPauseMode.value] || {})
// 断缴实验格式化（金额取整带符号；XIRR/占比百分位）
const pauseMoney = v => v == null ? '—' : (v > 0 ? '+' : '') + Math.round(v).toLocaleString() + ' 元'
const pauseXirr = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
const ddPct = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
// 弹窗「断缴对照」：单基金四组（打开时拉一次，固定全史 gross 口径，
// 不随弹窗止盈点/费率切换——实验数据只有这一种口径）
const dcaRoundsPause = ref(null)
// 年限/规模输入即时生效：model 随键入更新，但 InputNumber 在值未再变时回车不发
// change 事件（清空后回车失灵实测）——对两个输入防抖 400ms 直接触发搜索
let _dcaFilterTimer = null
watch([dcaAge, dcaScale], () => {
  clearTimeout(_dcaFilterTimer)
  _dcaFilterTimer = setTimeout(() => dcaSearch(), 400)
})
function onDcaPage(e) { dcaPage.value = e.page; loadDca() }
function onDcaSort(e) {
  // PrimeVue lazy 排序：{ sortField, sortOrder(1升/-1降) } → 服务端排序参数
  dcaSort.value = e.sortField
  dcaOrder.value = e.sortOrder === 1 ? 'asc' : 'desc'
  dcaPage.value = 0
  loadDca()
}
const pct1 = v => v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
const pctp = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
const pct0 = v => v == null ? '—' : (v * 100).toFixed(0) + '%'

// ===== 年限 × 规模 三维散点（/api/funds/dca/scatter：每基金一点 + 分箱统计） =====
const dcaScatter = ref(null)
const dcaScatterChartEl = ref(null)
let dcaScatterChart = null
// 年化口径：gross=不含费 / disc=含费·申购一折 / nom=含费·申购名义
// （赎回费三口径一致按持有天数阶梯收取；管理/托管/服务费已在净值内）
// 默认 nom 申购名义——保守上界，用户明确指定（2026-10-31）
const dcaFeeMode = ref('nom')
const dcaFeeLoading = ref(false)
let dcaFeeReqSeq = 0                       // 竞态防护：只采纳最后一次请求的响应
const DCA_FEE_OPTIONS = [
  { key: 'gross', label: '不含费' },
  { key: 'disc', label: '含费·申购一折' },
  { key: 'nom', label: '含费·申购名义' },
]
const dcaFeeLabel = computed(() =>
  (DCA_FEE_OPTIONS.find(o => o.key === dcaFeeMode.value) || {}).label || '')
// 行内按当前口径取值：gross 用原字段，disc/nom 用后缀字段（API 与静态快照
// 行结构一致，恒带三口径字段）；后缀字段不存在（旧快照未重导）时回落
// 原字段保持展示，存在但为 null（该基金无此口径数据）则如实显示 —
function dcaFeeVal(row, f) {
  if (dcaFeeMode.value === 'gross') return row[f]
  const v = row[`${f}_${dcaFeeMode.value}`]
  return v === undefined ? row[f] : v
}

// 静态模式散点权益过滤：快照恒为全市场（pages_export 导出 equity=False），
// 在客户端按行内 fund_type 过滤点集并重算分箱——与服务端 compute_bands
// 同口径（每基金落入唯一年限段×规模段格，均值/中位/正占比）。
// 仅当权益开关打开时重算；全市场直接透传快照原分箱。
function applyDcaEquity(sc) {
  if (!dcaEquity.value || sc?.status !== 'up' || !Array.isArray(sc.points)) return sc
  const pts = sc.points.filter(p => DCA_EQUITY_TYPES.includes(p[5]))
  return { ...sc, points: pts, ...recomputeDcaBands(pts) }
}

// 分箱重算（与后端 dca_api.compute_bands 同口径；标签函数逐字对齐；
// 段界常量复用下方 DCA_AGE_BANDS / DCA_SCALE_BANDS——函数只在调用时取值，
// setup 自上而下执行完毕后才会被触发）
const dcaAgeBandLabel = (lo, hi) => hi == null ? `≥${lo} 年` : `${lo}-${hi} 年`
const dcaScaleBandLabel = (lo, hi) =>
  hi == null ? `≥${lo} 亿` : (lo === 0 ? `<${hi} 亿` : `${lo}-${hi} 亿`)

function recomputeDcaBands(points) {
  const matrix = DCA_AGE_BANDS.map(() =>
    DCA_SCALE_BANDS.map(() => ({ count: 0, xs: [], positive: 0 })))
  for (const p of points) {
    const [age, scale, xirr] = p
    const ai = DCA_AGE_BANDS.findIndex(([lo, hi]) =>
      age >= lo && (hi == null || age < hi))
    const si = DCA_SCALE_BANDS.findIndex(([lo, hi]) =>
      scale >= lo && (hi == null || scale < hi))
    if (ai < 0 || si < 0) continue
    const c = matrix[ai][si]
    c.count++
    c.xs.push(xirr)
    if (xirr > 0) c.positive++
  }
  return {
    age_bands: DCA_AGE_BANDS.map(([lo, hi]) => dcaAgeBandLabel(lo, hi)),
    scale_bands: DCA_SCALE_BANDS.map(([lo, hi]) => dcaScaleBandLabel(lo, hi)),
    matrix: matrix.map(row => row.map(c => {
      const n = c.count
      const xs = c.xs.slice().sort((a, b) => a - b)
      return {
        count: n,
        mean_xirr: n ? +(xs.reduce((s, v) => s + v, 0) / n).toFixed(4) : null,
        median_xirr: n ? +pctCont(xs, 0.5).toFixed(4) : null,
        positive_share: n ? +(c.positive / n).toFixed(4) : null,
      }
    })),
  }
}

async function loadDcaScatter() {
  const seq = ++dcaFeeReqSeq
  dcaFeeLoading.value = true
  try {
    const r = await fetch(STATIC_MODE
      ? `${DATA_BASE}/funds/dca/scatter.${dcaFeeMode.value}.json`
      : `/api/funds/dca/scatter?fee=${dcaFeeMode.value}&equity=${dcaEquity.value}&method=${dcaMode.value}&tp=${dcaTp.value}`)
    const body = await r.json()
    // 期间又切换过 → 丢弃旧响应；静态模式权益过滤在客户端做
    if (seq === dcaFeeReqSeq) dcaScatter.value = STATIC_MODE ? applyDcaEquity(body) : body
  } catch (e) {
    if (seq === dcaFeeReqSeq)
      dcaScatter.value = { status: 'down', detail: String(e), points: [],
                           matrix: [] }
  } finally {
    if (seq === dcaFeeReqSeq) dcaFeeLoading.value = false
  }
  await nextTick()
  renderDcaScatterChart()
}
// 费用口径与权益开关都联动散点+排行（权益切换时统计卡/分箱同步重算）；
// 费用口径同时联动止盈点对比区块
watch([dcaFeeMode, dcaEquity], () => { loadDcaScatter(); loadDca(); loadDcaTpCompare() })

function renderDcaScatterChart() {
  if (dcaScatterChart) { dcaScatterChart.destroy(); dcaScatterChart = null }
  const sc = dcaScatter.value
  if (!dcaScatterChartEl.value || !sc || sc.status !== 'up' || !sc.points?.length) return
  // 原生 canvas 引擎：拖拽旋转 / 滚轮缩放 / 悬停明细（见 dca3d.js）
  dcaScatterChart = mountDca3DScatter(dcaScatterChartEl.value, sc)
}

// 数据与图表容器谁先就绪都能画：fetch 瞬间返回时可能早于分支挂载
// （chartEl 还没绑定）——watch 兜底，避免竞态下静默丢图
let _dcaScatterResizeTimer = null
const _onDcaScatterResize = () => {
  clearTimeout(_dcaScatterResizeTimer)
  _dcaScatterResizeTimer = setTimeout(() => {
    if (dcaScatterChart && dcaScatter.value?.status === 'up') renderDcaScatterChart()
  }, 300)
}
onMounted(() => window.addEventListener('resize', _onDcaScatterResize))
onUnmounted(() => {
  window.removeEventListener('resize', _onDcaScatterResize)
  clearTimeout(_dcaScatterResizeTimer)
})

watch(dcaScatterChartEl, el => {
  if (el && dcaScatter.value?.status === 'up' && dcaScatter.value?.points?.length)
    renderDcaScatterChart()
})

// ---- 每次止盈明细弹窗（点排行行触发，/api/funds/dca/{code}/rounds） ----
const dcaRoundsVisible = ref(false)
const dcaRoundsFund = ref(null)          // { code, name }
const dcaRounds = ref({ status: 'loading', rounds: [] })

const dcaRoundsNavs = ref([])            // 弹窗净值曲线（已复权链 [{date,value}]，起点=1）
// 弹窗风险/波动指标（navs 响应附带 risk，fund_risk_metrics 日更快照）；
// null = 无指标行 / 静态模式（快照不含 risk），指标区显示占位
const dcaRoundsRisk = ref(null)
let dcaRoundsChart = null
const dcaRoundsChartEl = ref(null)

function disposeDcaRoundsChart() {
  if (dcaRoundsChart) { dcaRoundsChart.dispose(); dcaRoundsChart = null }
  window.__dcaRoundsChart = null
}

// 复权净值链（与回测同口径：pct_chg 已是分红再投；缺失用单位净值比兜底）
function chainFromNavs(navs) {
  let v = 1
  return navs.map((n, i) => {
    if (i > 0) {
      const g = n.pct_chg != null ? n.pct_chg / 100
        : (n.unit_nav && navs[i - 1].unit_nav ? n.unit_nav / navs[i - 1].unit_nav - 1 : 0)
      v *= 1 + g
    }
    return { date: n.date, value: v }
  })
}

// 静态快照折线（export_navs 产物 {d0, d:天偏移, v:×1e4 整数}）→ 复权链
function chainFromStatic(body) {
  if (!body?.d0 || !body.d?.length || body.d.length !== body.v?.length) return []
  const [y, m, dd] = body.d0.split('-').map(Number)
  const utc0 = Date.UTC(y, m - 1, dd)
  return body.d.map((off, i) => ({
    date: new Date(utc0 + off * 86400000).toISOString().slice(0, 10),
    value: body.v[i] / 10000,
  }))
}

function openDcaRounds(e) {
  const data = e.data ?? e
  dcaRoundsFund.value = { code: data.code, name: data.name,
                          age_years: data.age_years ?? null }
  dcaRounds.value = { status: 'loading', rounds: [] }
  dcaRoundsNavs.value = []
  dcaRoundsRisk.value = null
  dcaRoundsTp.value = dcaTp.value        // 弹窗局部阈值从页面当前档位起步
  dcaRoundsFee.value = dcaFeeMode.value  // 弹窗局部费率口径同起步
  dcaRoundsAll.value = {}
  dcaRoundsSummary.value = null
  dcaRoundsPause.value = null
  disposeDcaRoundsChart()
  dcaRoundsVisible.value = true
  // 弹窗跟随页面级定投方式（无弹窗局部方式 tab；summary/轮次同 method）
  fetch(`/api/funds/dca/tp/${data.code}/summary?method=${dcaMode.value}`)
    .then(r => r.json())
    .then(body => {
      dcaRoundsSummary.value = (body && body.status === 'up'
        && Array.isArray(body.rows)) ? body.rows : null
      if (dcaRoundsSummary.value?.length
          && dcaRoundsFund.value?.code === data.code) {
        dcaRoundsFund.value.age_years
          = dcaRoundsSummary.value[0].age_years ?? null
      }
    })
    .catch(() => { dcaRoundsSummary.value = null })
  // 断缴实验对照（打开时拉一次；未参与实验的基金返回 fund=null 区块隐藏）
  fetch(`/api/funds/dca/bear-pause/${data.code}`)
    .then(r => r.json())
    .then(body => { dcaRoundsPause.value = (body && body.status === 'up') ? body.fund : null })
    .catch(() => { dcaRoundsPause.value = null })
  const emptyRounds = () => ({ status: 'up', code: data.code, fee: dcaFeeMode.value,
                               rounds: [], totals: { rounds: 0 } })
  if (STATIC_MODE) {
    // 静态：{ rounds: {gross,disc,nom}, totals: {gross,...} }，无文件 = 从未止盈；
    // 200 但非 JSON（本地 vite preview 的 SPA 回退页）同样按无文件处理
    fetch(`${DATA_BASE}/funds/dca/rounds/${data.code}.json`)
      .then(r => (r.ok ? r.json().catch(() => null) : null))
      .then(body => {
        const fee = dcaFeeMode.value
        dcaRounds.value = body
          ? { status: 'up', code: data.code, fee,
              rounds: body.rounds?.[fee] || [],
              totals: body.totals?.[fee] || { rounds: 0 } }
          : emptyRounds()
      })
      .catch(err => { dcaRounds.value = { status: 'down', detail: String(err), rounds: [] } })
    // 净值折线快照（近 10 年窗口）；404 = 窗口内净值不足不成线，曲线隐藏
    fetch(`${DATA_BASE}/funds/dca/navs/${data.code}.json`)
      .then(r => r.ok ? r.json() : null)
      .then(body => {
        dcaRoundsNavs.value = body ? chainFromStatic(body) : []
        nextTick(() => renderDcaRoundsChart())
      })
      .catch(() => { dcaRoundsNavs.value = [] })
    return
  }
  // live：风险指标随 navs 响应附带（静态快照无 risk，保持 null 显示占位）
  // 弹窗明细跟随阈值与定投方式：等额 + 20% 走旧端点，其余走 fund_dca_tp_rounds
  const roundsUrl = (dcaTp.value === 0.20 && dcaMode.value === 'amount')
    ? `/api/funds/dca/${data.code}/rounds?fee=${dcaFeeMode.value}`
    : `/api/funds/dca/tp/${data.code}/rounds?fee=${dcaFeeMode.value}&tp=${dcaTp.value}&method=${dcaMode.value}`
  fetch(roundsUrl)
    .then(r => r.json())
    .then(body => {
      dcaRounds.value = body
      nextTick(() => renderDcaRoundsChart())
    })
    .catch(err => { dcaRounds.value = { status: 'down', detail: String(err), rounds: [] } })
  // 净值曲线（近 2600 个净值日 ≈ 覆盖 10 年窗口）；无止盈轮次也照常渲染曲线
  fetch(`/api/funds/${data.code}/navs?limit=2600`)
    .then(r => r.json())
    .then(body => {
      dcaRoundsNavs.value = body.status === 'up' ? chainFromNavs(body.navs || []) : []
      dcaRoundsRisk.value = body.status === 'up' ? (body.risk ?? null) : null
      nextTick(() => renderDcaRoundsChart())
    })
    .catch(() => { dcaRoundsNavs.value = [] })
  loadDcaRoundsAll(data.code)
}

// 全档位轮次（8 并发，图上叠加全部止盈点三角）；到达后重渲染曲线。
// 只写 dcaRoundsAll 不动 dcaRounds——弹窗统计/轮次表始终跟随 dcaRoundsTp。
function loadDcaRoundsAll(code) {
  Promise.all(DCA_TP_OPTIONS.map(tp =>
    fetch(`/api/funds/dca/tp/${code}/rounds?fee=${dcaRoundsFee.value}&tp=${tp}&method=${dcaMode.value}`)
      .then(r => r.json()).then(b => [tp, (b && b.rounds) || []])
      .catch(() => [tp, []])
  )).then(entries => {
    dcaRoundsAll.value = Object.fromEntries(entries)
    nextTick(() => renderDcaRoundsChart())
  })
}

// 弹窗内切换止盈点：统计卡与轮次表跟随（tp 端点），图上当前档位三角高亮
function onDcaRoundsTpChange(tp) {
  if (dcaRoundsTp.value === tp || !dcaRoundsFund.value) return
  dcaRoundsTp.value = tp
  const code = dcaRoundsFund.value.code
  dcaRounds.value = { status: 'loading', rounds: [] }
  fetch(`/api/funds/dca/tp/${code}/rounds?fee=${dcaRoundsFee.value}&tp=${tp}&method=${dcaMode.value}`)
    .then(r => r.json())
    .then(body => {
      dcaRounds.value = body
      nextTick(() => renderDcaRoundsChart())
    })
    .catch(err => { dcaRounds.value = { status: 'down', detail: String(err), rounds: [] } })
}

// 弹窗内切换费率口径：轮次表/费用字段随口径重拉（份额折损使轮次位移），
// 全档位叠加同步重拉；summary 行内恒带三口径字段，卡值直接换列无需重拉
function onDcaRoundsFeeChange(fee) {
  if (dcaRoundsFee.value === fee || !dcaRoundsFund.value) return
  dcaRoundsFee.value = fee
  const code = dcaRoundsFund.value.code
  dcaRounds.value = { status: 'loading', rounds: [] }
  fetch(`/api/funds/dca/tp/${code}/rounds?fee=${fee}&tp=${dcaRoundsTp.value}&method=${dcaMode.value}`)
    .then(r => r.json())
    .then(body => {
      dcaRounds.value = body
      nextTick(() => renderDcaRoundsChart())
    })
    .catch(err => { dcaRounds.value = { status: 'down', detail: String(err), rounds: [] } })
  loadDcaRoundsAll(code)
}

// summary 行按弹窗费率口径取值（同 dcaFeeVal 约定：gross 原列，disc/nom 后缀）
function summaryFeeVal(row, f) {
  if (!row) return null
  if (dcaRoundsFee.value === 'gross') return row[f]
  const v = row[`${f}_${dcaRoundsFee.value}`]
  return v === undefined ? row[f] : v
}
// 当前弹窗档位的汇总行（总收益率/年化卡数据源）
const dcaRoundsSummaryRow = computed(() =>
  (dcaRoundsSummary.value || []).find(r => r && r.tp === dcaRoundsTp.value) || null)

// 弹窗轮次行：附加累计净收益（按 seq 累加，已了结轮次口径）
const dcaRoundsRows = computed(() => {
  const rounds = dcaRounds.value.rounds || []
  let acc = 0
  return rounds.map(r => {
    acc += (r.net_profit || 0)
    return { ...r, cum_net_profit: Math.round(acc * 100) / 100 }
  })
})

function buildDcaRoundsChart() {
  const chain = dcaRoundsNavs.value
  if (chain.length < 2) return null
  const dates = chain.map(p => p.date)
  const adj = chain.map(p => p.value)
  const tpMap = new Map(chain.map(p => [p.date, p.value]))
  // 全档位止盈点叠加（静态冻结站只有 20% 单档数据 → 回落现状单档）
  const all = (!STATIC_MODE && Object.keys(dcaRoundsAll.value).length)
    ? dcaRoundsAll.value
    : { [dcaRoundsTp.value]: dcaRounds.value.rounds || [] }
  const tpSeries = DCA_TP_OPTIONS
    .map(tp => {
      const pts = (all[tp] || [])
        .filter(r => r.tp_date && tpMap.has(r.tp_date))
        .map(r => ({ value: [r.tp_date, tpMap.get(r.tp_date)],
                     profit: r.profit, seq: r.seq, gap: r.gap_days }))
      return { tp, pts }
    })
    .filter(({ pts }) => pts.length)
    .map(({ tp, pts }) => {
      const active = tp === dcaRoundsTp.value
      return {
        name: dcaTpLabel(tp), type: 'scatter', data: pts,
        symbol: 'triangle', symbolSize: active ? 12 : 8, z: active ? 5 : 3,
        itemStyle: { color: DCA_TP_COLORS[tp] || '#da1e28',
                     opacity: active ? 1 : 0.35 },
        tooltip: { formatter: (p) => {
          const d = p.data
          return `<b>${p.seriesName} · 第 ${d.seq} 次止盈</b><br/>${d.value[0]}<br/>` +
            `实际收益率：${(d.profit * 100).toFixed(1)}%` +
            (d.gap != null ? `<br/>距上次止盈：${d.gap} 天` : '')
        } },
      }
    })
  const hasPts = tpSeries.length > 0
  return {
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: hasPts ? { data: ['复权净值', ...tpSeries.map(x => x.name)],
                       top: 0, itemWidth: 14, textStyle: { fontSize: 11 } } : null,
    grid: { left: 56, right: 16, top: hasPts ? 30 : 16, bottom: 46 },
    dataZoom: [{ type: 'inside' }],
    xAxis: { type: 'category', data: dates, boundary: false,
             axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#eee' } } },
    series: [
      { name: '复权净值', type: 'line', data: adj, showSymbol: false,
        lineStyle: { width: 1.3, color: '#0f62fe' } },
      ...tpSeries,
    ],
  }
}

function renderDcaRoundsChart() {
  disposeDcaRoundsChart()
  const opt = buildDcaRoundsChart()
  if (!opt || !dcaRoundsChartEl.value) return
  dcaRoundsChart = echarts.init(dcaRoundsChartEl.value)
  dcaRoundsChart.setOption(opt)
  window.__dcaRoundsChart = dcaRoundsChart      // E2E 断言钩子（series 检查）
}
// 弹窗宽度随视口（100vw-10vh）变化，窗口缩放时图表跟随重排
const _onDcaRoundsResize = () => dcaRoundsChart && dcaRoundsChart.resize()
onMounted(() => window.addEventListener('resize', _onDcaRoundsResize))
onUnmounted(() => window.removeEventListener('resize', _onDcaRoundsResize))
const gapLabel = d => d == null ? '—' : `${d} 天（≈${(d / 30.44).toFixed(1)} 月）`

function disposeDcaScatterChart() {
  if (dcaScatterChart) { dcaScatterChart.destroy(); dcaScatterChart = null }
}

// 结论（数据驱动，分箱口径=每基金只计一次）：年限/规模两维的边际均值 + 最优格子
const dcaScatterConclusion = computed(() => {
  const sc = dcaScatter.value
  if (!sc || sc.status !== 'up' || !sc.matrix?.length) return null
  const m = sc.matrix                       // matrix[ageIdx][scaleIdx]
  const ageMean = m.map(row => {
    const xs = row.flatMap(c => Array(c.count).fill(c.mean_xirr))
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null
  })
  const scaleMean = sc.scale_bands.map((_, si) => {
    const xs = m.flatMap(row => Array(row[si].count).fill(row[si].mean_xirr))
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null
  })
  let best = null
  for (let ai = 0; ai < m.length; ai++)
    for (let si = 0; si < m[ai].length; si++)
      if (m[ai][si].count >= 300 && m[ai][si].mean_xirr != null &&
          (!best || m[ai][si].mean_xirr > best.cell.mean_xirr))
        best = { cell: m[ai][si], ageIdx: ai, scaleIdx: si }
  return { ageMean, scaleMean, best }
})

// 中位口径的组合最优格（与均值口径同一 ≥300 样本门槛），供中位分箱表高亮
const dcaMedianBest = computed(() => {
  const sc = dcaScatter.value
  if (!sc || sc.status !== 'up' || !sc.matrix?.length) return null
  let best = null
  for (let ai = 0; ai < sc.matrix.length; ai++)
    for (let si = 0; si < sc.matrix[ai].length; si++) {
      const c = sc.matrix[ai][si]
      if (c.count >= 300 && c.median_xirr != null &&
          (!best || c.median_xirr > best.cell.median_xirr))
        best = { cell: c, ageIdx: ai, scaleIdx: si }
    }
  return best
})

// 分箱格点击 → 格内基金清单（client 端从散点点集过滤，与服务端分箱同界；
// points = [age, scale, xirr, code, name, fund_type]，年化为当前口径）
const DCA_AGE_BANDS = [[0, 1], [1, 3], [3, 5], [5, 10], [10, null]]
const DCA_SCALE_BANDS = [[0, 1], [1, 10], [10, 100], [100, null]]
const dcaCellFundsVisible = ref(false)
const dcaCellFunds = ref({ title: '', rows: [] })

function openDcaCellFunds(ai, si) {
  const sc = dcaScatter.value
  if (!sc || sc.status !== 'up' || !sc.points?.length) return
  const cell = sc.matrix[ai]?.[si]
  if (!cell || !cell.count) return
  const [alo, ahi] = DCA_AGE_BANDS[ai]
  const [slo, shi] = DCA_SCALE_BANDS[si]
  const rows = sc.points
    .filter(pt => pt[0] >= alo && (ahi == null || pt[0] < ahi)
               && pt[1] >= slo && (shi == null || pt[1] < shi))
    .map(pt => ({ code: pt[3], name: pt[4], fund_type: pt[5],
                  age: pt[0], scale: pt[1], xirr: pt[2] }))
    .sort((a, b) => b.xirr - a.xirr)
  dcaCellFunds.value = {
    title: `运作年限 ${sc.age_bands[ai]} × 规模 ${sc.scale_bands[si]}`,
    rows,
  }
  dcaCellFundsVisible.value = true
}

// 基金净值详情（对齐个股详情形态：单位+累计净值双线走势图 + 分页明细表）
const fund = ref(null)                    // { code, name, fund_type }
const fundNav = ref('funds')              // 详情页返回目标
const fundChart = ref({ status: 'loading', navs: [] })
// 风险/波动指标（navs 响应附带 risk 字段，fund_risk_metrics 日更快照）：
// null = 该基金无指标行 / 日更未跑过，详情页指标区按此显示占位
const fundRisk = ref(null)
// 多窗口对照表行序（与 risk_metrics.WINDOWS 同序）
const FUND_RISK_WINDOWS = [
  { key: '1y', label: '近1年' },
  { key: '3y', label: '近3年' },
  { key: '5y', label: '近5年' },
  { key: 'all', label: '全史' },
]
const fundRows = ref({ status: 'loading', rows: [] })
const fundDetailPage = ref(0)             // 0 起
const FUND_CHART_POINTS = 2500            // 图取近 2500 个净值日（约 10 年）
const FUND_DETAIL_PAGE_SIZE = 50
const fundChartEl = ref(null)
let fundChartInstance = null

function openFund(e) {
  // PrimeVue DataTable row-click 参数为 { originalEvent, data, index }
  const data = e.data ?? e
  fund.value = { code: data.code, name: data.name, fund_type: data.fund_type || '' }
  fundDetailPage.value = 0
  fundNav.value = activeNav.value
  setNav('fund')
}
function backToFunds() { setNav(fundNav.value || 'funds') }

async function loadFundChart() {
  fundChart.value = { status: 'loading', navs: [] }
  fundRisk.value = null
  try {
    const r = await fetch(`/api/funds/${fund.value.code}/navs?limit=${FUND_CHART_POINTS}&offset=0`)
    const body = await r.json()
    fundChart.value = { status: body.status, detail: body.detail, navs: body.navs || [] }
    fundRisk.value = body.status === 'up' ? (body.risk ?? null) : null
  } catch (e) {
    fundChart.value = { status: 'down', detail: String(e), navs: [] }
  }
  await nextTick()
  renderFundChart()
}

function renderFundChart() {
  if (fundChartInstance) { fundChartInstance.dispose(); fundChartInstance = null }
  const navs = fundChart.value.navs
  if (!fundChartEl.value || fundChart.value.status !== 'up' || !navs.length) return
  fundChartInstance = echarts.init(fundChartEl.value)
  const dates = navs.map(n => n.date)
  fundChartInstance.setOption({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: ['单位净值', '累计净值'], top: 0 },
    grid: { left: 72, right: 24, top: 32, bottom: 56 },
    xAxis: { type: 'category', data: dates, boundary: false, axisLabel: { fontSize: 10 } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: '#eee' } } },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', top: '92%', height: 18, start: 0, end: 100 },
    ],
    series: [
      { name: '单位净值', type: 'line', data: navs.map(n => n.unit_nav),
        showSymbol: false, lineStyle: { width: 1.4, color: '#0f62fe' } },
      { name: '累计净值', type: 'line', data: navs.map(n => n.accum_nav),
        showSymbol: false, lineStyle: { width: 1.2, color: '#ee538b' } },
    ],
  })
}

async function loadFundDetail() {
  fundRows.value = { status: 'loading', rows: [] }
  const offset = fundDetailPage.value * FUND_DETAIL_PAGE_SIZE
  try {
    const r = await fetch(`/api/funds/${fund.value.code}/navs?limit=${FUND_DETAIL_PAGE_SIZE}&offset=${offset}`)
    const body = await r.json()
    if (body.status !== 'up') {
      fundRows.value = { status: body.status, detail: body.detail, rows: [] }
      return
    }
    const rows = body.navs || []
    // 明细表最新日期在上（API 为图表返回升序，表格独立倒序展示）
    const tableRows = [...rows].reverse()
    const total = rows.length === FUND_DETAIL_PAGE_SIZE
      ? offset + rows.length + 1 : offset + rows.length
    fundRows.value = { status: 'up', rows: tableRows, total }
  } catch (e) {
    fundRows.value = { status: 'down', detail: String(e), rows: [] }
  }
}
const fundDetailTotal = computed(() =>
  fundRows.value.total ?? (fundRows.value.rows?.length || 0))

function onFundDetailPage(e) {
  fundDetailPage.value = e.page
  loadFundDetail()
}

function disposeFundChart() {
  if (fundChartInstance) { fundChartInstance.dispose(); fundChartInstance = null }
}

// ===== 519 vs 924 行情对比（数据子页：/api/market/519-924，指数日更任务供数；
// 上证指数同源复刻中信研报图：T 日收盘基线、交易日序偏移对齐 T-280~T+1040，
// 924 线随日更自动延长；研报原图为 wind全A、免费渠道不可得，页面注明口径） =====
const marketCompare = ref({ status: 'idle', detail: '', body: null })
const marketChartEl = ref(null)
let marketChartInstance = null

async function loadMarketCompare() {
  marketCompare.value = { status: 'loading', detail: '', body: null }
  try {
    const r = await fetch('/api/market/519-924')
    const body = await r.json()
    marketCompare.value = { status: body.status === 'up' ? 'up' : 'down',
                            detail: body.detail || '', body }
    await nextTick()
    renderMarketChart()
  } catch (e) {
    marketCompare.value = { status: 'down', detail: String(e), body: null }
  }
}

const _marketN = v => v === 0 ? 'T' : (v > 0 ? 'T+' + v : 'T' + v)

// 双 y 轴量纲（研报原图同款：左轴=5·19、右轴=9·24，各自量纲满幅铺展）。
// 两轴取相同格数 K 且共用同一个「零刻度以下格数 m」→ 网格线逐条对齐、
// 0% 刻度同高（虚线基准线对两线同时成立），读数必须各看各的轴。
const _MARKET_LADDER = []
for (const base of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 8, 10])
  for (let k = -2; k <= 2; k++) _MARKET_LADDER.push(base * 10 ** k)
_MARKET_LADDER.sort((a, b) => a - b)

function _marketAxisRange(lo, hi, m, K) {
  const step = _MARKET_LADDER.find(c => m * c >= -lo && (K - m) * c >= hi)
    || 10 ** Math.ceil(Math.log10(Math.max(-lo, hi, 1e-6)))
  return { min: -m * step, max: (K - m) * step, interval: step }
}

function _marketDualAxes(pts, eps) {
  const ext = key => {
    const vals = pts.filter(p => p[key] != null).map(p => p[key])
    return vals.length ? { lo: Math.min(...vals), hi: Math.max(...vals) } : null
  }
  const e1 = ext('r1'), e2 = ext('r2')
  if (!e1 || !e2) return null
  // 右轴量纲需覆盖 924 线与全部叠加脉冲线（同轴读数）
  for (const ep of eps || []) {
    for (const [, r] of ep.points || []) {
      if (r == null) continue
      e2.lo = Math.min(e2.lo, r)
      e2.hi = Math.max(e2.hi, r)
    }
  }
  const K = 10
  let best = null
  for (let m = 1; m < K; m++) {
    const a1 = _marketAxisRange(e1.lo, e1.hi, m, K)
    const a2 = _marketAxisRange(e2.lo, e2.hi, m, K)
    const waste = Math.max(K * a1.interval / (e1.hi - e1.lo),
                           K * a2.interval / (e2.hi - e2.lo))
    if (!best || waste < best.waste) best = { waste, a1, a2 }
  }
  return best
}

const _MAIN1 = '5·19 行情（T=1999-05-19）'
const _MAIN2 = '9·24 行情（T=2024-09-24）'

function renderMarketChart() {
  if (marketChartInstance) { marketChartInstance.dispose(); marketChartInstance = null }
  const body = marketCompare.value.body
  if (!marketChartEl.value || !body || body.status !== 'up' || !body.points.length) return
  marketChartInstance = echarts.init(marketChartEl.value)
  const pts = body.points
  const eps = body.episodes || []
  const line = key => pts.filter(p => p[key] != null).map(p => [p.n, p[key]])
  const axes = _marketDualAxes(pts, eps) || { a1: {}, a2: {} }
  marketChartInstance.setOption({
    animation: false,
    tooltip: { trigger: 'axis', confine: true, formatter: ps => {
      const n = ps.length ? ps[0].value[0] : null
      const hit = pts.find(p => p.n === n) || {}
      const lines = [`T 基准 ${_marketN(n)}`]
      if (hit.r1 != null) lines.push(`5·19（${hit.d1}）：<b>${pct1(hit.r1)}</b>`)
      if (hit.r2 != null) lines.push(`9·24（${hit.d2}）：<b>${pct1(hit.r2)}</b>`)
      for (const s of ps) {
        if (s.seriesName === _MAIN1 || s.seriesName === _MAIN2) continue
        lines.push(`${s.seriesName}：<b>${pct1(s.value[1])}</b>`)
      }
      return lines.join('<br>')
    } },
    legend: {
      data: [_MAIN1, _MAIN2, ...eps.map(e => `${e.t} ${e.label}`)],
      top: 0, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 10 },
    },
    grid: { left: 64, right: 56, top: 64, bottom: 48 },
    xAxis: { type: 'value', min: -body.back, max: body.fwd, interval: 40,
      axisLabel: { fontSize: 10, formatter: _marketN } },
    yAxis: [
      { type: 'value', min: axes.a1.min, max: axes.a1.max, interval: axes.a1.interval,
        axisLabel: { fontSize: 10, color: '#c9313d',
          formatter: v => (v * 100).toFixed(0) + '%' },
        axisLine: { show: true, lineStyle: { color: 'rgba(201,49,61,0.45)' } },
        splitLine: { lineStyle: { color: '#eee' } } },
      { type: 'value', position: 'right', min: axes.a2.min, max: axes.a2.max,
        interval: axes.a2.interval,
        axisLabel: { fontSize: 10, color: '#4e79a7',
          formatter: v => (v * 100).toFixed(0) + '%' },
        axisLine: { show: true, lineStyle: { color: 'rgba(78,121,167,0.45)' } },
        splitLine: { show: false } },
    ],
    series: [
      ...eps.map(ep => ({
        name: `${ep.t} ${ep.label}`, type: 'line', showSymbol: false,
        yAxisIndex: 1, z: 2, data: ep.points,
        lineStyle: { width: 1.1, color: ep.color, opacity: 0.9 },
        itemStyle: { color: ep.color },
      })),
      { name: _MAIN1, type: 'line', showSymbol: false,
        yAxisIndex: 0, z: 3,
        data: line('r1'), lineStyle: { width: 1.4, color: '#c9313d' },
        itemStyle: { color: '#c9313d' },
        markLine: { silent: true, symbol: 'none', label: { show: false },
          lineStyle: { type: 'dashed', color: '#666' },
          data: [{ xAxis: 0 }, { yAxis: 0 }] } },
      { name: _MAIN2, type: 'line', showSymbol: false,
        yAxisIndex: 1, z: 3,
        data: line('r2'), lineStyle: { width: 1.4, color: '#4e79a7' },
        itemStyle: { color: '#4e79a7' } },
    ],
  })
}

function disposeMarketChart() {
  if (marketChartInstance) { marketChartInstance.dispose(); marketChartInstance = null }
}
const _onMarketResize = () => marketChartInstance && marketChartInstance.resize()

// KPI 摘要：924 进度/当前累计、519 同期、两线区间峰值（down 态无 body 为 null）
const marketKpis = computed(() => {
  const body = marketCompare.value.body
  if (!body || body.status !== 'up' || !body.t2_progress) return null
  const pts = body.points, prog = body.t2_progress
  const same = pts.find(p => p.n === prog.n)
  const peak = key => {
    let best = null
    for (const p of pts) {
      if (p[key] != null && (best == null || p[key] > best.r)) best = { n: p.n, r: p[key] }
    }
    return best
  }
  return { prog, same, peak1: peak('r1'), peak2: peak('r2') }
})
onMounted(() => window.addEventListener('resize', _onMarketResize))
const _onFundResize = () => fundChartInstance && fundChartInstance.resize()

watch(activeNav, (nav, prev) => {
  if (nav === 'funds') loadFunds()
  if (nav === 'fundDca') { loadDca(); loadDcaScatter(); loadDcaTpCompare(); loadDcaDipReport(); loadDcaPauseExperiment() }
  if (nav === 'market') loadMarketCompare()
  if (nav === 'fund' && fund.value) {
    loadFundChart()
    loadFundDetail()
  }
  if (prev === 'fund' && nav !== 'fund') disposeFundChart()
  if (prev === 'fundDca' && nav !== 'fundDca') disposeDcaScatterChart()
  if (prev === 'market' && nav !== 'market') disposeMarketChart()
})

// ===== 东财 Cookie：状态 / 粘贴保存 / 验证 =====
const emCookie = ref({ configured: false, valid: false, masked: '', reason: '' })
const emCookieInput = ref('')
const emCookieSaving = ref(false)
const emCookieMsg = ref(null)   // {ok, text}
const showCookieHelp = ref(false)

async function loadEmCookie() {
  try {
    const r = await fetch('/api/cookies/em')
    emCookie.value = await r.json()
  } catch (e) { /* 静默：卡片显示未知状态 */ }
}
async function saveEmCookie() {
  if (!emCookieInput.value.trim()) return
  emCookieSaving.value = true
  emCookieMsg.value = null
  try {
    const r = await fetch('/api/cookies/em', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cookie: emCookieInput.value }),
    })
    const d = await r.json()
    emCookieMsg.value = d.valid
      ? { ok: true, text: 'Cookie 已保存并验证通过，可以启动东财爬虫了' }
      : { ok: false, text: 'Cookie 已保存但验证失败：' + (d.reason || '未知原因') }
    emCookieInput.value = ''
    await loadEmCookie()
  } catch (e) {
    emCookieMsg.value = { ok: false, text: '保存请求失败：' + e }
  } finally {
    emCookieSaving.value = false
  }
}
watch(activeNav, (nav) => { if (nav === 'data' || nav.startsWith('src-')) loadEmCookie() })
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

// ===== 爬虫任务管理（/api/tasks CRUD + 运行态 3s 轮询）=====
const tasksState = ref({ status: 'loading', tasks: [], queue: [] })
let tasksTimer = null

async function loadTasks() {
  try {
    const r = await fetch('/api/tasks')
    tasksState.value = await r.json()
  } catch (e) {
    tasksState.value = { status: 'down', detail: String(e), tasks: [] }
  }
}

function startTasksPolling() {
  loadTasks()
  stopTasksPolling()
  tasksTimer = setInterval(loadTasks, 3000)
}
function stopTasksPolling() {
  if (tasksTimer) { clearInterval(tasksTimer); tasksTimer = null }
}
watch(activeNav, nav => {
  if (nav === 'tasks') startTasksPolling()
  else stopTasksPolling()
})

const TASK_SOURCES = [{ v: 'sina', label: '新浪财经' }, { v: 'eastmoney', label: '东方财富' },
  { v: 'backtest', label: '定投回测' },
  { v: 'delisted', label: '退市股补爬' }, { v: 'fund', label: '基金净值' }]
const TASK_MODES = [{ v: 'incremental', label: '增量续爬' }, { v: 'full', label: '全量重爬' },
  { v: 'daily', label: '日更' }]
const sourceLabel = v => (TASK_SOURCES.find(s => s.v === v) || {}).label || v
const modeLabel = v => (TASK_MODES.find(m => m.v === v) || {}).label || v
const PHASES = { idle: ['空闲', 'idle'], queued: ['排队中', 'queued'], running: ['运行中', 'up'] }
const phaseInfo = t => PHASES[t.phase] || PHASES.idle

const taskDialog = ref(null)     // null | {mode:'create'|'edit', id?, name, source, taskMode, schedule_time}
const taskError = ref('')
const expandedRuns = ref({})     // task_id → bool
const runsMap = ref({})          // task_id → runs[]

function openCreate() {
  taskError.value = ''
  taskDialog.value = { mode: 'create', name: '', source: 'sina', taskMode: 'incremental', schedule_time: '18:00' }
}
function openEdit(t) {
  taskError.value = ''
  taskDialog.value = { mode: 'edit', id: t.id, name: t.name, source: t.source,
                       taskMode: t.mode, schedule_time: t.schedule_time || '18:00' }
}
async function saveTask() {
  const d = taskDialog.value
  const body = { source: d.source, mode: d.taskMode, schedule_time: d.schedule_time.trim(), name: d.name.trim() || null }
  taskError.value = ''
  try {
    const r = d.mode === 'create'
      ? await fetch('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      : await fetch(`/api/tasks/${d.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    if (!r.ok) { taskError.value = (await r.json()).detail || `保存失败（HTTP ${r.status}）`; return }
    taskDialog.value = null
    loadTasks()
  } catch (e) { taskError.value = String(e) }
}
async function actTask(t, action) {
  try {
    const r = await fetch(`/api/tasks/${t.id}/${action}`, { method: 'POST' })
    if (!r.ok) console.warn(`任务 ${t.id} ${action} 失败: HTTP ${r.status}`)
  } finally { loadTasks() }
}
async function toggleTask(t) {
  try {
    await fetch(`/api/tasks/${t.id}`, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !t.enabled }) })
  } finally { loadTasks() }
}
async function deleteTask(t) {
  if (!window.confirm(`确定删除任务「${t.name}」？（运行记录保留）`)) return
  try {
    const r = await fetch(`/api/tasks/${t.id}`, { method: 'DELETE' })
    if (r.status === 409) window.alert((await r.json()).detail)
  } finally { loadTasks() }
}
async function toggleRuns(t) {
  expandedRuns.value = { ...expandedRuns.value, [t.id]: !expandedRuns.value[t.id] }
  if (expandedRuns.value[t.id] && !runsMap.value[t.id]) {
    try {
      const r = await fetch(`/api/tasks/${t.id}/runs`)
      runsMap.value = { ...runsMap.value, [t.id]: (await r.json()).runs || [] }
    } catch { runsMap.value = { ...runsMap.value, [t.id]: [] } }
  }
}

const serverClock = computed(() => {
  const s = tasksState.value.server_time
  if (!s) return ''
  try {
    return new Date(s).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch { return s }
})
function nextRunText(t) {
  if (!t.enabled) return '已禁用'
  if (!t.next_run_at) return '手动'          // 启用的纯手动任务（无调度时刻）
  return fmtNext(t.next_run_at)
}
function fmtNext(iso) {
  try {
    const d = new Date(iso)
    const now = new Date()
    const sameDay = d.toDateString() === new Date(tasksState.value.server_time || now).toDateString()
    const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
    if (sameDay) return `今天 ${hm}`
    return `${d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })} ${hm}`
  } catch { return iso }
}
function taskLastRunText(r) {
  const t = r.started_at ? new Date(r.started_at).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '?'
  const trig = { schedule: '定时', catch_up: '补跑', manual: '手动' }[r.trigger] || r.trigger
  const outcome = r.aborted ? '熔停' : '完成'
  return `${t} · ${trig} · ${outcome} · 成 ${fmt(r.ok_stocks)} / 败 ${fmt(r.failed_stocks)}`
}
function riskNote(t) {
  if (t.source === 'backtest')
    return '定投回测日更：子进程全量重算 2.6 万只（约 20-30 分钟）后直写结果表刷新页面；串行队列排在净值日更（21:00）之后，净值爬取失败时按最新可得数据运行、次日自愈'
  if (t.source === 'eastmoney')
    return '东财源 drip 模式：每 30 秒 1 请求（出口 IP 曾被封），全市场一轮约需 40+ 小时，仅作解封后校验/补数用'
  if (t.source === 'fund' && t.mode === 'incremental')
    return '基金增量：当日净值快照（全市场最新已披露净值一次拉齐），约 5 分钟；清盘/滞后基金由全量任务补齐'
  if (t.source === 'fund')
    return '基金源全量：全部 2.8 万只、1 QPS 自适应限速，全量回补约 8 小时；货币基金无净值走势，记为跳过'
  if (t.mode === 'full')
    return '全量重爬：每轮 5200+ 请求、约 1~2 小时，确认需要重建数据时再开启'
  return ''
}
const dialogRisk = computed(() => {
  const d = taskDialog.value
  if (!d) return ''
  if (d.source === 'backtest')
    return '定投回测日更：子进程全量重算 2.6 万只（约 20-30 分钟）后直写结果表刷新页面；串行队列排在净值日更（21:00）之后，净值爬取失败时按最新可得数据运行、次日自愈'
  if (d.source === 'eastmoney')
    return '东财源 drip 模式：每 30 秒 1 请求（出口 IP 曾被封），全市场一轮约 40+ 小时，仅作解封后校验/补数用'
  if (d.source === 'fund' && d.taskMode === 'incremental')
    return '基金增量：当日净值快照（全市场最新已披露净值一次拉齐），约 5 分钟；清盘/滞后基金由全量任务补齐'
  if (d.source === 'fund')
    return '基金源全量：全部 2.8 万只、1 QPS 自适应限速，全量回补约 8 小时；货币基金无净值走势，记为跳过'
  if (d.taskMode === 'full')
    return '全量重爬：每轮 5200+ 请求、约 1~2 小时，确认需要重建数据时再开启'
  return ''
})

// ===== 个股历史行情详情（数据页行点击进入；K线近250根 + 全字段明细分页）=====
const stock = ref(null)            // { code, name, source, fromNav }
const stockAdjust = ref('')        // '' 不复权（默认） | 'qfq' 前复权
const chartBars = ref({ status: 'loading', bars: [] })
const detailRows = ref({ status: 'loading', rows: [] })
const detailPage = ref(0)          // 0 起
const DETAIL_PAGE_SIZE = 50
const chartEl = ref(null)
let chartInstance = null

function openStock(e) {
  // PrimeVue DataTable row-click 参数为 { originalEvent, data, index }
  const data = e.data ?? e
  // 入口来自数据源子页时沿用该来源，否则默认新浪
  const source = srcPage.value?.field || 'sina'
  stock.value = { code: data.code, name: data.name, source }
  detailPage.value = 0
  stockNav.value = activeNav.value
  setNav('stock')
}
const stockNav = ref('data')       // 详情页返回目标（数据页/来源子页）

function backToList() {
  setNav(stockNav.value || 'data')
}

// ===== 量化教学：经典回测策略（backend vectorbt 引擎，/api/backtest） =====
const STRATS = [
  { id: 'buy_hold', name: '买入持有', tag: '基准',
    brief: '期初买入、持有到期末——不是策略，是一切策略的对照基准线',
    lessons: ['跑不赢买入持有的策略是负贡献（白交手续费）', '绝对收益高 ≠ 跑赢基准'] },
  { id: 'ma_cross', name: '双均线交叉', tag: '趋势',
    brief: 'MA(short) 上穿 MA(long) 金叉买入、下穿死叉卖出——回测界的 Hello World',
    lessons: ['震荡市鞭尸（whipsaw）：横盘期反复金叉死叉被来回打脸', '参数敏感：5/20 与 20/60 的结果差异巨大，警惕过拟合'] },
  { id: 'donchian', name: '通道突破（海龟）', tag: '趋势',
    brief: '突破前 N 日最高价追入、跌破 M 日最低价离场——不预测，只承认事实',
    lessons: ['假突破频率高，离场纪律是生命线', '单边大行情抓得最全（对照双均线感受差异）'] },
  { id: 'bollinger', name: '布林带均值回归', tag: '反转',
    brief: '跌破均线下 α 倍标准差买入、回到均线上方卖出——赌偏离修复',
    lessons: ['与趋势策略哲学对立：震荡市赚得漂亮', '单边下跌越抄越深——策略没有好坏，只有适配行情'] },
  { id: 'grid', name: '网格交易', tag: '波动',
    brief: '以起点为基准每跌 step% 买一档、反弹减档——赚波动不赌方向',
    lessons: ['单边下跌满仓套牢（与均值回归同款风险）', '收益来自波动率而非方向，适合震荡标的'] },
  { id: 'momentum', name: '动量轮动', tag: '多标的',
    brief: '每月末按过去 N 日涨幅排名，等权买入前 K 名——强者恒强',
    lessons: ['动量崩溃：市场反转时前期最强跌最狠', '从单标的升级到股票池+定期调仓的框架'] },
]
const STRAT_FIELDS = {
  buy_hold: [],
  ma_cross: [{ k: 'fast', label: '短期均线', type: 'number', min: 2, max: 120 },
             { k: 'slow', label: '长期均线', type: 'number', min: 3, max: 250 }],
  donchian: [{ k: 'entry_n', label: '突破窗口', type: 'number', min: 3, max: 120 },
             { k: 'exit_n', label: '离场窗口', type: 'number', min: 2, max: 60 }],
  bollinger: [{ k: 'window', label: '均线窗口', type: 'number', min: 5, max: 120 },
              { k: 'alpha', label: 'σ 倍数', type: 'number', min: 0.5, max: 4, step: 0.5 }],
  grid: [{ k: 'step_pct', label: '步长 %', type: 'number', min: 0.5, max: 20, step: 0.5 },
         { k: 'levels', label: '档位数', type: 'number', min: 2, max: 10 }],
  momentum: [{ k: 'window', label: '动量窗口', type: 'number', min: 5, max: 120 },
             { k: 'top_k', label: '买前 K 名', type: 'number', min: 1, max: 10 },
             { k: 'rebalance', label: '调仓频率', type: 'select',
               options: [{ value: 'month', label: '每月末' },
                         { value: 'week', label: '每周末' }] }],
}
const DEFAULT_PARAMS = {
  buy_hold: {}, ma_cross: { fast: 5, slow: 20 }, donchian: { entry_n: 20, exit_n: 10 },
  bollinger: { window: 20, alpha: 2.0 }, grid: { step_pct: 5.0, levels: 4 },
  momentum: { window: 20, top_k: 2, rebalance: 'month' },
}
const learnStrat = ref('ma_cross')
const learnCodePick = ref([])         // 多选标签：[{code, name, label}]（1 只=单股模式）
const batchProgress = ref(null)       // {done, total} 批量进度
const batchResults = ref([])          // 批量模式每股结果
const batchFailed = ref([])           // 批量失败清单 [{code, name, reason}]
const learnMode = ref('single')       // single | batch | full（分段切换器）
function resolveCodes() {
  return learnCodePick.value
    .map(x => (typeof x === 'string' ? x : (x?.code ?? '')).trim())
    .filter(Boolean)
}
const batchMode = computed(() => learnMode.value === 'batch')
const learnRange = ref('3y')
const learnExec = ref('next_open')
const learnParams = ref({ ...DEFAULT_PARAMS.ma_cross })
const learnCommission = ref(2.5)   // 万分数输入（万分之 2.5）
const learnSellTax = ref(5)        // 万分数（卖出印花税 0.05%）
const learnResult = ref(null)
const learnError = ref('')
const learnLoading = ref(false)
const learnChartEl = ref(null)
let learnChart = null

// 代码下拉：一次性拉全清单（code+name），本地过滤（搜索零延迟）
const codeOptions = ref([])
const codeSuggestions = ref([])
async function loadCodeOptions() {
  if (codeOptions.value.length) return            // 已加载
  try {
    const r = await fetch('/api/stocks/basic')
    const body = await r.json()
    if (body.status === 'up')
      codeOptions.value = body.stocks.map(x => ({
        code: x.code, name: x.name, label: `${x.code} ${x.name}` }))
  } catch { /* 拉不到则退化为手输代码 */ }
}
function searchCodes(ev) {
  const q = (ev.query || '').trim().toLowerCase()
  const src = codeOptions.value
  codeSuggestions.value = q
    ? src.filter(x => x.code.toLowerCase().includes(q)
        || x.name.toLowerCase().includes(q)).slice(0, 50)
    : src.slice(0, 50)
}
watch(activeNav, nav => {
  if (nav === 'learn') {
    loadCodeOptions()
    loadFullRuns()                             // 历史全量回测列表 + 接上运行中任务
  }
})

const learnStratMeta = computed(() => STRATS.find(s => s.id === learnStrat.value))
const learnIsMomentum = computed(() => learnStrat.value === 'momentum')

watch(learnStrat, id => {
  learnParams.value = { ...DEFAULT_PARAMS[id] }
  // 动量为横截面组合策略：无单股形态；全市场段可用（组合级回测，输出单组合行）
  if (id === 'momentum' && learnMode.value === 'single') learnMode.value = 'batch'
})

function openLearnWith(code, name) {
  learnCodePick.value = [{ code, name: name || code,
                           label: name ? `${code} ${name}` : code }]
  learnMode.value = 'single'
  setNav('learn')
}

function _pickToCode(x) {
  return (typeof x === 'string' ? x : (x?.code ?? '')).trim()
}

const fmtSignedPct = v => (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'

// ===== 结果容器三态文案（按模式） =====
const codeFieldLabel = computed(() => learnIsMomentum.value
  ? '轮动股票池（可搜索多选，≥2 只）'
  : learnMode.value === 'batch'
    ? '股票代码（可搜索多选，≥2 只横向对比）'
    : '股票代码（可搜索多选，回测一只）')
const resultTitle = computed(() => ({
  single: '单股回测结果', batch: '批量对比', full: '全市场回测结果',
}[learnMode.value]))
const resultHint = computed(() => {
  if (learnMode.value === 'full' && fullRunning.value)
    return `已统计 ${fullRun.value?.done ?? 0} 只 · 实时更新`
  return {
    single: '收益对照买入持有',
    batch: '按总收益降序 · 失败股标注原因',
    full: 'Top 20 叠加 · 全量排行可导出',
  }[learnMode.value]
})
const loadingText = computed(() => learnMode.value === 'full'
  ? '全市场任务执行中…'
  : learnMode.value === 'batch' ? '批量回测中…' : '回测中…')
const emptyTitle = computed(() => ({
  single: '选好策略与参数，点「跑回测」开始',
  batch: '选择 ≥2 只股票做横向对比',
  full: '发起全市场任务后，这里展示统计与排行',
}[learnMode.value]))
const emptyDetail = computed(() => ({
  single: '收盘出信号：当日收盘成交 = 未来函数（作弊），次日开盘成交 = 规矩——切换对比两者收益差',
  batch: '每股独立回测（含复权因子懒加载），完成后生成对比表与叠加净值图',
  full: '历史任务可用上方下拉回看',
}[learnMode.value]))
const resultLoading = computed(() => {
  if (learnMode.value === 'full')
    return fullStarting.value || (fullRunning.value
      && (fullRun.value?.done ?? 0) === 0)   // 首只完成前骨架，之后实时结果上屏
  return learnLoading.value
})
const resultEmpty = computed(() => {
  if (learnMode.value === 'full') return !fullResultsVisible.value
  if (learnMode.value === 'batch')
    return !(batchResults.value.length || batchFailed.value.length)
  return !learnResult.value
})

async function _runOne(code, shared, raw = false) {
  const body = {
    strategy: learnStrat.value,
    codes: [code],
    range: learnRange.value,
    params: learnParams.value,
    exec_mode: learnExec.value,
    commission: (learnCommission.value || 0) / 10000,
    sell_tax: (learnSellTax.value || 0) / 10000,
    ...shared,
  }
  const r = await fetch('/api/backtest', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (raw) {
    // 单股/轮动：保留后端原始语义（message=区间空态、status=down=不可用），
    // 400/网关异常折算为 {ok:false, reason} 由调用方转错误横幅
    if (r.status === 400)
      return { ok: false, reason: (await r.json()).detail || '参数错误' }
    try { return await r.json() }
    catch {
      return { ok: false,
               reason: `服务返回异常（HTTP ${r.status}，可能为网关超时）` }
    }
  }
  if (r.status === 400) {
    return { code, ok: false, reason: (await r.json()).detail || '参数错误' }
  }
  let parsed
  try { parsed = await r.json() }
  catch {
    return { code, ok: false,
             reason: `服务返回异常（HTTP ${r.status}，可能为网关超时）` }
  }
  if (parsed.status !== 'up') return { code, ok: false, reason: parsed.detail }
  if (parsed.message) return { code, ok: false, reason: parsed.message }
  return { code, ok: true, ...parsed }
}

async function runLearn() {
  learnLoading.value = true
  learnError.value = ''
  learnResult.value = null
  batchResults.value = []
  batchFailed.value = []
  batchProgress.value = null
  const codes = resolveCodes()
  if (!codes.length) {
    learnLoading.value = false
    learnError.value = '请先选择至少一只股票'
    return
  }
  const batch = batchMode.value
  if (batch && !learnIsMomentum.value && codes.length < 2) {
    learnLoading.value = false
    learnError.value = '批量对比请至少选择 2 只股票（单只请切到单股模式）'
    return
  }
  try {
    if (!batch) {                                    // 单股 / 轮动（一次请求）
      const bodyCodes = learnIsMomentum.value ? codes : [codes[0]]
      const raw = await _runOne(bodyCodes[0], {}, true)
      if (raw.ok === false) throw new Error(raw.reason)   // 参数/网关异常 → 横幅
      learnResult.value = { ...raw, codes: bodyCodes }
      learnLoading.value = false            // 先落 loading，结果模板才挂载图表容器
      await nextTick()
      renderLearnChart()
      return
    }
    // 批量：逐股请求（并发 3），每股完成即入表渐进渲染
    const picks = learnCodePick.value
    const nameOf = code => {
      const hit = picks.find(x => _pickToCode(x) === code)
      return hit?.name || code
    }
    batchProgress.value = { done: 0, total: codes.length }
    const queue = [...codes]
    const results = []
    const failed = []
    async function worker() {
      while (queue.length) {
        const code = queue.shift()
        try {
          const r = await _runOne(code, {})
          if (r.ok) results.push(r)
          else failed.push({ code, name: nameOf(code), reason: r.reason || '失败' })
        } catch (e) {
          failed.push({ code, name: nameOf(code), reason: String(e) })
        }
        batchProgress.value = {
          done: batchProgress.value.done + 1, total: codes.length }
        renderBatchTable(results, failed)
      }
    }
    await Promise.all([worker(), worker(), worker(), worker()])
    learnLoading.value = false            // 先落 loading，结果模板才挂载图表容器
    batchResults.value = [...results].sort((a, b) =>
      (b.metrics?.total_return ?? -9) - (a.metrics?.total_return ?? -9))
    batchFailed.value = failed
    renderBatchTable(batchResults.value, failed)
    await nextTick()
    renderBatchChart(batchResults.value)
  } catch (e) {
    learnError.value = String(e)
  } finally { learnLoading.value = false }
}

function renderBatchTable(results, failed) {
  batchResults.value = [...results].sort((a, b) =>
    (b.metrics?.total_return ?? -9) - (a.metrics?.total_return ?? -9))
  batchFailed.value = failed
}

function renderBatchChart(results) {
  if (learnChart) { learnChart.dispose(); learnChart = null }
  if (!learnChartEl.value || !results.length) return
  learnChart = echarts.init(learnChartEl.value)
  window.__learnChart = learnChart               // E2E 断言钩子（series 数据检查）
  // 各股归一化净值（1.0 起步）按日期对齐（并集，缺日 null 断开）
  const dateSet = new Set()
  results.forEach(r => (r.equity || []).forEach(row => dateSet.add(row[0])))
  const dates = [...dateSet].sort()
  const series = results.map(r => {
    const map = Object.fromEntries((r.equity || []).map(row => [row[0], row[1]]))
    const pick = pickNameOf(r.code)
    return {
      name: pick, type: 'line', showSymbol: false,
      connectNulls: false, lineStyle: { width: 1.5 },
      data: dates.map(d => map[d] ?? null),
    }
  })
  learnChart.setOption({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: results.map(r => pickNameOf(r.code)), top: 0 },
    grid: { left: 60, right: 20, top: 34, bottom: 46 },
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: '#eee' } } },
    dataZoom: [{ type: 'inside' }],
    series,
  })
}

function pickNameOf(code) {
  const hit = learnCodePick.value.find(x => _pickToCode(x) === code)
  return hit ? (hit.name || hit.code || code) : code
}

// ===== 全量回测：同策略对全市场一次性后台回测（任务落库，历史可回看） =====
const fullFilterSt = ref(true)        // 剔除名称含 ST/退
const fullMinBars = ref(60)           // 区间内最少 K 线数（挡次新/长期停牌）
const fullAdjust = ref(true)          // 后复权口径（关闭提速但跨除权失真）
const fullIncludeDelisted = ref(true) // 含退市股（修幸存者偏差的本体）
const fullMinCap = ref(20)            // 选股域：流通市值下限（亿元，0=不限）
const fullMaxCap = ref(200)           // 选股域：流通市值上限（亿元，0=不限）
const fullMaxLot = ref(1000)          // 选股域：一手成本上限（元，0=不限）
const fullReuse = ref(true)           // 同参历史结果复用（成功股免算，失败股重跑）
const fullRuns = ref([])              // 历史 run 列表
const fullRunId = ref(null)           // 当前查看/运行中的 run
const fullRun = ref(null)             // 该 run 详情（进度/统计）
const fullRows = ref({ total: 0, rows: [] })
const fullRowsSort = ref('total_return')
const fullRowsDesc = ref(true)
const fullRowsPage = ref(0)
const FULL_PAGE_SIZE = 50
// 退市股总结（独立分组）：排行/分页/排序 + 该口径统计卡
const delistRows = ref({ total: 0, rows: [], stats: null })
const delistRowsSort = ref('total_return')
const delistRowsDesc = ref(true)
const delistRowsPage = ref(0)
const delistChartEl = ref(null)
const delistChartBox = { chart: null }
const fullCurves = ref([])            // Top-N 归一化净值曲线
const fullChartEl = ref(null)
const fullChartBox = { chart: null }
let fullPollTimer = null
const fullStarting = ref(false)
const fullError = ref('')

const fullRunning = computed(() =>
  ['running', 'canceling'].includes(fullRun.value?.status))
const fullViewable = computed(() =>
  ['done', 'canceled'].includes(fullRun.value?.status)
  && (fullRun.value?.done ?? 0) > 0)
// 运行中逐股实时上屏：已有完成股即渲染结果区（排行表/统计/Top20 增量刷新）
const fullResultsVisible = computed(() =>
  fullViewable.value || (fullRunning.value && (fullRun.value?.done ?? 0) > 0))
const fullPages = computed(() =>
  Math.max(1, Math.ceil((fullRows.value.total || 0) / FULL_PAGE_SIZE)))
const fullExportUrl = computed(() =>
  fullRunId.value ? `/api/backtest/full/runs/${fullRunId.value}/export` : '')

async function loadFullRuns() {
  try {
    const r = await fetch('/api/backtest/full/runs')
    fullRuns.value = (await r.json()).runs || []
    // 有任务在跑（或页面刷新前发起了任务）→ 自动接上轮询
    const active = fullRuns.value.find(x => x.status === 'running')
    if (active && fullRunId.value == null) {
      fullRunId.value = active.id
      fullRun.value = active
      startFullPoll()
    } else if (!fullRun.value && fullRuns.value.length) {
      fullRunId.value = fullRuns.value[0].id   // 默认回看最新一次（含结果）
      await loadFullRun()
    }
  } catch { /* 降级：无历史列表 */ }
}

async function loadFullRun(withResults = true) {
  try {
    const r = await fetch(`/api/backtest/full/runs/${fullRunId.value}`)
    if (!r.ok) { fullRun.value = null; return }
    fullRun.value = await r.json()
    if (withResults && fullViewable.value) {
      // 先等详情渲染（v-if="fullRun" 图表容器才挂载），再拉曲线并绘制——
      // 并行时序下 loadFullCurves 先回来时 el 为 null 直接 return，图永不画
      await nextTick()
      await Promise.all([loadFullRows(), loadDelistRows(), loadFullCurves(),
                         loadDelistTop()])
    }
  } catch (e) { fullError.value = String(e) }
}

async function loadFullRows() {
  const q = new URLSearchParams({
    sort: fullRowsSort.value, desc: String(fullRowsDesc.value),
    page: String(fullRowsPage.value), size: String(FULL_PAGE_SIZE),
  })
  try {
    fullRows.value = await (await fetch(
      `/api/backtest/full/runs/${fullRunId.value}/rows?${q}`)).json()
  } catch { fullRows.value = { total: 0, rows: [] } }
}

async function loadDelistRows() {
  const q = new URLSearchParams({
    sort: delistRowsSort.value, desc: String(delistRowsDesc.value),
    page: String(delistRowsPage.value), size: String(FULL_PAGE_SIZE),
    delisted: 'only',
  })
  try {
    delistRows.value = await (await fetch(
      `/api/backtest/full/runs/${fullRunId.value}/rows?${q}`)).json()
  } catch { delistRows.value = { total: 0, rows: [], stats: null } }
}

function sortDelist(col) {
  if (delistRowsSort.value === col) delistRowsDesc.value = !delistRowsDesc.value
  else { delistRowsSort.value = col; delistRowsDesc.value = true }
  delistRowsPage.value = 0
  loadDelistRows()
}

const delistPages = computed(() =>
  Math.max(1, Math.ceil((delistRows.value.total || 0) / FULL_PAGE_SIZE)))

async function loadDelistTop() {
  // 退市股全部曲线（快照直读；无快照旧任务回退 Top 20 现算）
  try {
    let curves = [], all = true
    const rc = await (await fetch(
      `/api/backtest/full/runs/${fullRunId.value}/curves?delisted=only`)).json()
    curves = rc.curves || []
    if (!curves.length) {
      const r = await (await fetch(
        `/api/backtest/full/runs/${fullRunId.value}/top?n=20&delisted=only`)).json()
      curves = r.curves || []
      all = false
    }
    await nextTick()
    drawCurves(delistChartEl, delistChartBox, '__delistChart', curves, all)
  } catch { /* 图表非关键路径 */ }
}

const _onDelistResize = () => delistChartBox.chart && delistChartBox.chart.resize()

const fullCurvesAll = ref(true)   // 主图当前是否为全部股票模式
const fullView = ref('all')        // 图表视图: all | top20 | representative
const FULL_VIEWS = [
  { id: 'all', label: '全部' },
  { id: 'top20', label: 'Top 20' },
  { id: 'representative', label: '四代表股' },
]

// 四代表股挑选：收益率最高 / 最低 / 最接近中位数 / 最接近平均数（按最终收益）
function pickRepresentative(curves) {
  const withRet = curves.map(c => {
    const eq = c.equity || []
    // 曲线末点是净值（起步 1.0），收益率 = 净值 - 1
    // （曾误把净值当收益率：左江退净值 0.0047 被标成 "+0%"，实际 -99.5%）
    return { ...c, _ret: eq.length ? eq[eq.length - 1][1] - 1 : null }
  }).filter(c => c._ret != null)
  if (!withRet.length) return []
  const rets = withRet.map(c => c._ret)
  const max = Math.max(...rets), min = Math.min(...rets)
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length
  const sortedRets = [...rets].sort((a, b) => a - b)
  const mid = sortedRets.length % 2
    ? sortedRets[(sortedRets.length - 1) / 2]
    : (sortedRets[sortedRets.length / 2 - 1] + sortedRets[sortedRets.length / 2]) / 2
  const pick = (target, exclude) => {
    const pool = withRet.filter(c => !exclude.has(c.code))
    return pool.length ? pool.reduce((best, c) =>
      Math.abs(c._ret - target) < Math.abs(best._ret - target) ? c : best)
      : null
  }
  const best = withRet.reduce((a, b) => (b._ret > a._ret ? b : a))
  const worst = withRet.reduce((a, b) => (b._ret < a._ret ? b : a))
  const used = new Set([best.code, worst.code])
  const nearMedian = pick(mid, used); if (nearMedian) used.add(nearMedian.code)
  const nearMean = pick(mean, used)
  const label = (c, tag) => ({ ...c, name: `${tag}(${c._ret >= 0 ? '+' : ''}${(c._ret * 100).toFixed(1)}%) ${c.code} ${c.name}` })
  return [
    label(best, '最高'), label(worst, '最低'),
    nearMedian && label(nearMedian, '近中位'),
    nearMean && label(nearMean, '近均值'),
  ].filter(Boolean)
}

const _curvesReqSeq = { v: 0 }   // 竞态守卫：慢请求返回时若已过期则丢弃

async function loadFullCurves() {
  const seq = ++_curvesReqSeq.v
  try {
    const runId = fullRunId.value
    if (fullView.value === 'top20') {
      const r = await (await fetch(
        `/api/backtest/full/runs/${runId}/top?n=20`)).json()
      if (seq !== _curvesReqSeq.v) return   // 过期响应丢弃
      fullCurves.value = r.curves || []
      fullCurvesAll.value = false
    } else if (fullView.value === 'representative') {
      // 代表股基于全部曲线（有快照）或 Top20 兜底
      let curves = []
      try {
        const rc = await (await fetch(
          `/api/backtest/full/runs/${runId}/curves`)).json()
        curves = rc.curves || []
      } catch { curves = [] }
      if (!curves.length) {
        const r = await (await fetch(
          `/api/backtest/full/runs/${runId}/top?n=20`)).json()
        curves = r.curves || []
      }
      if (seq !== _curvesReqSeq.v) return
      fullCurves.value = pickRepresentative(curves)
      fullCurvesAll.value = false
    } else {
      const [rc, rd] = await Promise.all([
        fetch(`/api/backtest/full/runs/${runId}/curves`).then(r => r.json()),
        fetch(`/api/backtest/full/runs/${runId}/curves?delisted=only`)
          .then(r => r.json()).catch(() => ({ curves: [] })),
      ])
      if (seq !== _curvesReqSeq.v) return
      fullCurves.value = rc.curves || []
      fullDelistedCodes.value = new Set((rd.curves || []).map(c => c.code))
      fullCurvesAll.value = true
    }
    if (seq !== _curvesReqSeq.v) return
    await nextTick()
    renderFullChart()
  } catch { fullCurves.value = [] }
}

function setFullView(v) {
  fullView.value = v
  loadFullCurves()
}

async function startFullRun() {
  fullStarting.value = true
  fullError.value = ''
  try {
    const r = await fetch('/api/backtest/full', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        strategy: learnStrat.value, range: learnRange.value,
        params: learnParams.value, exec_mode: learnExec.value,
        commission: (learnCommission.value || 0) / 10000,
        sell_tax: (learnSellTax.value || 0) / 10000,
        adjust: fullAdjust.value,
        reuse: fullReuse.value,
        filters: { exclude_st: fullFilterSt.value,
                   min_bars: fullMinBars.value || 0,
                   include_delisted: fullIncludeDelisted.value,
                   min_market_cap: (fullMinCap.value || 0) * 1e8,
                   max_market_cap: (fullMaxCap.value || 0) * 1e8,
                   max_lot_cost: fullMaxLot.value || 0 },
      }),
    })
    if (!r.ok) {
      fullError.value = (await r.json()).detail || `HTTP ${r.status}`
      return
    }
    fullRunId.value = (await r.json()).run_id
    fullRun.value = { id: fullRunId.value, status: 'running' }
    fullRows.value = { total: 0, rows: [] }
    delistRows.value = { total: 0, rows: [], stats: null }
    fullCurves.value = []
    delistRows.value = { total: 0, rows: [], stats: null }
    delistRowsPage.value = 0
    lastTopDone = 0
    lastTopAt = 0
    startFullPoll()
    loadFullRuns()                               // 刷新历史列表
  } catch (e) { fullError.value = String(e) }
  finally { fullStarting.value = false }
}

let lastTopDone = 0
let lastTopAt = 0

function maybeRefreshTop() {
  // Top20 / 退市 Top20 均为现算 20 只回测，节流：每新增 ≥25 只或每 30 秒重算
  const done = fullRun.value?.done ?? 0
  const now = Date.now()
  if (done > 0 && (done - lastTopDone >= 25 || now - lastTopAt >= 30000)) {
    lastTopDone = done
    lastTopAt = now
    loadFullCurves()
    loadDelistTop()
  }
}

function startFullPoll() {
  stopFullPoll()
  fullPollTimer = setInterval(async () => {
    await loadFullRun()                        // 详情（running 态带实时统计）
    if (fullRunning.value) {
      if ((fullRun.value?.done ?? 0) > 0)
        await Promise.all([loadFullRows(), loadDelistRows()])  // 排行表实时增长
      maybeRefreshTop()                                        // 图表节流刷新
      return
    }
    stopFullPoll()
    if (fullViewable.value)
      await Promise.all([loadFullRows(), loadDelistRows(), loadFullCurves(),
                         loadDelistTop()])
    loadFullRuns()
  }, 2000)
}

function stopFullPoll() {
  if (fullPollTimer) { clearInterval(fullPollTimer); fullPollTimer = null }
}

async function cancelFullRun() {
  try { await fetch(`/api/backtest/full/runs/${fullRunId.value}/cancel`,
                    { method: 'POST' }) } catch { /* 轮询会收敛终态 */ }
}

function sortFull(col) {
  if (fullRowsSort.value === col) fullRowsDesc.value = !fullRowsDesc.value
  else { fullRowsSort.value = col; fullRowsDesc.value = true }
  fullRowsPage.value = 0
  loadFullRows()
}

function pickFullStock(code) {
  // 点排行代码 → 填入单股回测控件（不自动跑，用户可调参后点跑回测）
  learnCodePick.value = [{ code, name: code, label: code }]
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

// 曲线通用绘制：全部股票模式（细线半透明、无图例无悬浮、渐进渲染，
// 数千 series 压测可用）；精选模式（≤20 条）保留图例与悬浮
const fullDelistedCodes = ref(new Set())  // 全部视图下退市股代码集合（高亮标注）

function drawCurves(el, box, hook, curves, all) {
  if (box.chart) { box.chart.dispose(); box.chart = null }
  if (!el.value || !curves.length) return
  box.chart = echarts.init(el.value)
  window[hook] = box.chart                       // E2E 断言钩子
  const dateSet = new Set()
  curves.forEach(c => c.equity.forEach(row => dateSet.add(row[0])))
  const dates = [...dateSet].sort()
  const series = curves.map(c => {
    const map = Object.fromEntries(c.equity.map(row => [row[0], row[1]]))
    // 全部视图：退市股红线加粗高亮（幸存者偏差可视化主角）；在市股细灰蓝
    const isDelisted = all && fullDelistedCodes.value.has(c.code)
    // 代表股曲线的 name 已是「标签(收益率) 代码 名称」，不再叠加代码前缀
    const name = c.name.includes(c.code)
      ? c.name : `${c.code} ${c.name}${isDelisted ? '［退市］' : ''}`
    return {
      name,
      type: 'line', showSymbol: false,
      connectNulls: false, sampling: all ? 'lttb' : undefined,
      progressive: all ? 2000 : undefined,
      progressiveThreshold: all ? 3000 : undefined,
      lineStyle: isDelisted ? { width: 1.4, opacity: 0.85, color: '#da1e28' }
        : all ? { width: 0.7, opacity: 0.3, color: '#4a6fa5' }
              : { width: 1.2 },
      data: dates.map(d => map[d] ?? null),
    }
  })
  box.chart.setOption({
    animation: false,
    tooltip: all ? { show: false }
                 : { trigger: 'axis',
                     valueFormatter: v => v == null ? '—'
                       : ((v - 1) * 100).toFixed(1) + '%' },
    legend: all ? { show: false }
                : { data: series.map(s => s.name), top: 0, type: 'scroll' },
    grid: { left: 60, right: 20, top: 40, bottom: 46 },
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    // Y 轴收益率口径（净值-1），与统计卡/代表股标签一致
    yAxis: { scale: true,
             axisLabel: { formatter: v => ((v - 1) * 100).toFixed(0) + '%' },
             splitLine: { lineStyle: { color: '#eee' } } },
    dataZoom: [{ type: 'inside' }],
    series,
  })
}

function renderFullChart(attempt = 0) {
  // setTimeout 而非 rAF：headless/后台标签页 rAF 不触发（曾致自动化环境图永不渲染）
  if (!fullChartEl.value && attempt < 50) {
    // 大响应加载期间详情区渲染慢，重试窗口 15s（50×300ms）
    return void setTimeout(() => renderFullChart(attempt + 1), 300)
  }
  drawCurves(fullChartEl, fullChartBox, '__fullChart',
             fullCurves.value, fullCurvesAll.value)
}

const _onFullResize = () => fullChartBox.chart && fullChartBox.chart.resize()
onMounted(() => window.addEventListener('resize', _onFullResize))
onUnmounted(() => { stopFullPoll() })

const FULL_SORT_COLS = [
  ['total_return', '总收益'], ['annual_return', '年化'], ['max_drawdown', '最大回撤'],
  ['sharpe', '夏普'], ['win_rate', '胜率'], ['trades', '交易次数'],
]

function fmtPctSigned(v) {
  return v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

function renderLearnChart() {
  const res = learnResult.value
  if (learnChart) { learnChart.dispose(); learnChart = null }
  if (!learnChartEl.value || !res || res.status !== 'up' || !res.equity?.length) return
  learnChart = echarts.init(learnChartEl.value)
  window.__learnChart = learnChart               // E2E 断言钩子（series 数据检查）
  const dates = res.equity.map(r => r[0])
  const strat = res.equity.map(r => r[1])
  const bh = res.equity.map(r => r[2])
  const dd = Object.fromEntries(res.drawdown.map(r => [r[0], r[1]]))
  const drawdown = dates.map(d => dd[d] ?? null)
  learnChart.setOption({
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: ['策略净值', '买入持有'], top: 0 },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [{ left: 60, right: 20, top: 30, height: '58%' },
           { left: 60, right: 20, top: '76%', height: '16%' }],
    xAxis: [
      { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
      { type: 'category', gridIndex: 1, data: dates, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, splitLine: { lineStyle: { color: '#eee' } } },
      { gridIndex: 1, axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' },
        splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      { name: '策略净值', type: 'line', data: strat, showSymbol: false,
        lineStyle: { width: 1.6, color: '#0f62fe' } },
      { name: '买入持有', type: 'line', data: bh, showSymbol: false,
        lineStyle: { width: 1.2, color: '#a8a8a8' } },
      { name: '回撤', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: drawdown,
        showSymbol: false, areaStyle: { color: 'rgba(218,30,40,.25)' },
        lineStyle: { width: 1, color: '#da1e28' } },
    ],
  })
}

const _onLearnResize = () => learnChart && learnChart.resize()
onMounted(() => window.addEventListener('resize', _onLearnResize))

async function fetchBars(code, { limit, offset, source, adjust = '' }) {
  const r = await fetch(`/api/stocks/${code}/bars?limit=${limit}&offset=${offset}&source=${source}&adjust=${adjust}`)
  return r.json()
}

async function loadChart() {
  chartBars.value = { status: 'loading', bars: [] }
  try {
    const body = await fetchBars(stock.value.code,
      { limit: 250, offset: 0, source: stock.value.source, adjust: stockAdjust.value })
    chartBars.value = { status: body.status, bars: body.bars || [] }
  } catch (e) {
    chartBars.value = { status: 'down', detail: String(e), bars: [] }
  }
  await nextTick()
  renderChart()
}

function renderChart() {
  if (chartInstance) { chartInstance.dispose(); chartInstance = null }
  const bars = chartBars.value.bars
  if (!chartEl.value || chartBars.value.status !== 'up' || !bars.length) return
  chartInstance = echarts.init(chartEl.value)
  const dates = bars.map(b => b.date)
  chartInstance.setOption({
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 64, right: 24, top: 16, height: '58%' },
      { left: 64, right: 24, top: '74%', height: '16%' },
    ],
    xAxis: [
      { type: 'category', data: dates, boundary: true, axisLabel: { fontSize: 10 } },
      { type: 'category', gridIndex: 1, data: dates, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, splitLine: { lineStyle: { color: '#eee' } } },
      { gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], top: '92%', height: 18, start: 0, end: 100 },
    ],
    series: [
      { type: 'candlestick', name: '日K', data: bars.map(b => [b.open, b.close, b.low, b.high]),
        itemStyle: { color: '#c50f1f', color0: '#0f7b0f', borderColor: '#c50f1f', borderColor0: '#0f7b0f' } },
      { type: 'bar', name: '成交量', xAxisIndex: 1, yAxisIndex: 1,
        data: bars.map(b => b.volume),
        itemStyle: { color: c => (c.value != null && bars[c.dataIndex] &&
          bars[c.dataIndex].close >= bars[c.dataIndex].open ? '#c50f1f' : '#0f7b0f') } },
    ],
  })
}

async function loadDetail() {
  detailRows.value = { status: 'loading', rows: [] }
  const offset = detailPage.value * DETAIL_PAGE_SIZE
  try {
    const body = await fetchBars(stock.value.code,
      { limit: DETAIL_PAGE_SIZE, offset, source: stock.value.source,
        adjust: stockAdjust.value })
    if (body.status !== 'up') {
      detailRows.value = { status: body.status, detail: body.detail, rows: [] }
      return
    }
    const rows = body.bars || []
    // 明细表最新日期在上（API 为图表返回升序，表格独立倒序展示）
    const tableRows = [...rows].reverse()
    // totalRecords 估算：满页则多给 1 触发下一页，末页精确
    const total = rows.length === DETAIL_PAGE_SIZE ? offset + rows.length + 1 : offset + rows.length
    detailRows.value = { status: 'up', rows: tableRows, total }
  } catch (e) {
    detailRows.value = { status: 'down', detail: String(e), rows: [] }
  }
}
const detailTotal = computed(() =>
  detailRows.value.total ?? (detailRows.value.rows?.length || 0))

function onDetailPage(e) {
  detailPage.value = e.page
  loadDetail()
}

function setStockSource(s) {
  if (stock.value.source === s) return
  stock.value = { ...stock.value, source: s }
  detailPage.value = 0
  loadChart()
  loadDetail()
}

function disposeChart() {
  if (chartInstance) { chartInstance.dispose(); chartInstance = null }
}

watch(stockAdjust, () => {
  if (activeNav.value === 'stock' && stock.value) {
    loadChart()
    loadDetail()
  }
})

const _onResize = () => chartInstance && chartInstance.resize()
// E2E/调试钩子：直设教学页多选代码（绕过 AutoComplete 交互细节）
if (typeof window !== 'undefined') {
  window.__learnSetCodes = (items, names = {}) => {
    learnCodePick.value = items.map(code => {
      const name = names[code] || code
      return { code, name, label: `${code} ${name}` }
    })
  }
  window.__learnState = () => ({
    strat: learnStrat.value,
    picks: JSON.parse(JSON.stringify(learnCodePick.value)),
    batchMode: batchMode.value,
    mode: learnMode.value,
    codes: resolveCodes(),
    loading: learnLoading.value,
    error: learnError.value,
    hasResult: !!learnResult.value,
    batchN: batchResults.value.length,
  })
}
onMounted(() => {
  window.addEventListener('resize', _onResize)
  window.addEventListener('resize', _onDelistResize)
  window.addEventListener('resize', _onFundResize)
})

watch(activeNav, (nav, prev) => {
  if (nav === 'stock' && stock.value) {
    loadChart()
    loadDetail()
  }
  if (prev === 'stock' && nav !== 'stock') disposeChart()
})

// 大数格式化：亿/万自适应（成交额、流通股本）
function fmtBig(n, unitDiv = 1) {
  if (n == null) return '—'
  const v = n / unitDiv
  if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(2) + '万'
  return v.toLocaleString('zh-CN')
}
const fmtAmount = n => fmtBig(n)
const fmtShares = n => fmtBig(n, 1)

onMounted(() => {
  if (STATIC_MODE) { loadDca(); loadDcaScatter(); return }  // 静态冻结站点无多阈值数据
  applyUrl(); refresh()
})
// 参数集合变化 → 防抖同步 URL（页面内参数变化 replace，不灌浏览历史）；
// 前进/后退 → 从 URL 恢复。函数引用在 setup 末尾注册，避开 TDZ。
window.addEventListener('popstate', applyUrl)
watch(
  [activeNav, learnStrat, learnMode, learnCodePick, learnRange, learnExec,
   learnCommission, learnSellTax, learnParams, fullFilterSt, fullMinBars,
   fullAdjust, fullIncludeDelisted, stock, stockAdjust, browseQuery,
   browsePage, dcaQuery, dcaPage, dcaAlive, dcaAge, dcaSort, dcaOrder],
  () => scheduleSync(), { deep: true })
</script>

<template>
  <!-- Web 外壳：全幅铺满视口 -->
  <div class="desktop">
    <div class="window">

      <!-- 标题栏（apple 标准标题 / carbon 复用为深色 UI Shell 头；fluent 按原型无标题栏，切换控件移至工具栏） -->
      <div class="titlebar">
        <svg class="shell-menu" role="button" aria-label="切换导航" @click="toggleSidebar" viewBox="0 0 16 16" fill="none" aria-hidden="true">
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
        <aside class="sidebar" :class="{ open: sidebarOpen, collapsed: sidebarCollapsed }">
          <div class="app-item">
            <svg class="app-item-icon" viewBox="0 0 24 24" fill="none">
              <path d="M12 2.5 20 7v10l-8 4.5L4 17V7z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
              <path d="M12 7.2 16 9.4v4.4l-4 2.2-4-2.2V9.4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
            </svg>
            <span>honey-money</span>
          </div>
          <div class="group">导航</div>
          <button v-for="n in (STATIC_MODE
              ? [{ id: 'fundDca', label: '定投止盈回测' }]
              : [
              { id: 'dash', label: '仪表盘' }, { id: 'data', label: '数据' },
              { id: 'tasks', label: '任务' },
              { id: 'backtest', label: '回测' }, { id: 'learn', label: '教学' }])"
            :key="n.id" class="nav-item"
            :class="{ active: activeNav === n.id, 'sub-active': (activeNav.startsWith('src-') || activeNav === 'stock' || activeNav === 'funds' || activeNav === 'fund' || activeNav === 'fundDca' || activeNav === 'market') && n.id === 'data' }"
            :title="sidebarCollapsed ? n.label : null"
            @click="setNav(n.id)">
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
            <svg v-else-if="n.id === 'tasks'" class="nav-icon" viewBox="0 0 20 20" fill="none">
              <rect x="6" y="2.5" width="8" height="3.4" rx="1" stroke="currentColor" stroke-width="1.5"/>
              <path d="M6 4.2H3.5v13h13v-13H14" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
              <path d="M7 9.5h6M7 13h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
            <svg v-else-if="n.id === 'backtest'" class="nav-icon" viewBox="0 0 20 20" fill="none">
              <path d="M3 17h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              <path d="M5 13l4-4.5 3 3 4-6.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <svg v-else class="nav-icon" viewBox="0 0 20 20" fill="none">
              <path d="M3 4.2c2.4-1.1 4.8-1.1 7 0 2.2-1.1 4.6-1.1 7 0v11.6c-2.4-1.1-4.8-1.1-7 0-2.2-1.1-4.6-1.1-7 0z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
              <path d="M10 4.2v11.6" stroke="currentColor" stroke-width="1.5"/>
            </svg>{{ n.label }}</button>
          <template v-if="!STATIC_MODE">
            <div class="group">数据源</div>
            <button v-for="s in SRC_ITEMS" :key="s.id" class="src-item"
              :class="{ active: activeNav === s.id }" :title="sidebarCollapsed ? s.label : null"
              @click="setNav(s.id)">
              <svg v-if="s.id === 'src-sina'" class="src-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <rect x="4" y="2.5" width="12" height="15" rx="1.5" stroke="currentColor" stroke-width="1.5"/>
                <path d="M7 6.5h6M7 10h6M7 13.5h3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              <svg v-else class="src-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <circle cx="10" cy="5.5" r="2.8" stroke="currentColor" stroke-width="1.5"/>
                <path d="M4 11c0 1.5 2.7 2.6 6 2.6s6-1.1 6-2.6M4 15c0 1.5 2.7 2.6 6 2.6s6-1.1 6-2.6"
                  stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              {{ s.label }}<span v-if="s.field === 'eastmoney'" class="src-dot"
                :class="emCookie.valid ? 'ok' : 'bad'" :title="emCookie.valid ? 'Cookie 有效' : '东财 Cookie 未配置/无效'"></span>
            </button>
            <button class="src-item" :class="{ active: activeNav === 'funds' }"
              :title="sidebarCollapsed ? '基金净值' : null" @click="setNav('funds')">
              <svg class="src-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <path d="M2.5 15.5c3 0 4-9.5 7.5-9.5 2.5 0 3 6 7.5 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              基金净值</button>
            <button class="src-item" :class="{ active: activeNav === 'fundDca' }"
              :title="sidebarCollapsed ? '定投止盈回测' : null" @click="setNav('fundDca')">
              <svg class="src-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <path d="M16.5 10a6.5 6.5 0 1 1-2-4.7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                <path d="M16.8 2.8v3.4h-3.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              定投止盈回测</button>
            <button class="src-item" :class="{ active: activeNav === 'market' }"
              :title="sidebarCollapsed ? '519 vs 924 行情' : null" @click="setNav('market')">
              <svg class="src-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <path d="M3 16.5V9.5m4.7 7V4.5m4.6 12V8m4.7 8.5v-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              519 vs 924 行情</button>
          </template>
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
              <span><b class="eb-title">后端 API 不可用：</b>可能正在部署/重启，页面会自动重试恢复 · 持续不可用请检查 backend 容器 / uvicorn 进程 / docker compose ps</span>
            </div>

            <!-- 三张状态卡：各自独立结算，谁先回来谁先上屏 -->
            <div class="cards">
              <div v-if="pendingBackend" class="card skeleton-card">
                <div class="sk sk-title"></div><div class="sk sk-main"></div><div class="sk sk-line"></div>
              </div>
              <div v-else class="card">
                <div class="name">后端 API</div>
                <span class="fbadge"><span class="dot" :class="backendDown ? 'down' : 'up'"></span><span class="fb-txt">{{ backendDown ? '不可用' : '运行中' }}</span></span>
                <div class="state">{{ backendDown ? '不可用' : '运行中' }}</div>
                <div class="detail">v{{ backend.version || '0.1.0' }} · FastAPI + uvicorn</div>
              </div>
              <div v-if="!backendDown && pendingInfra" class="card skeleton-card">
                <div class="sk sk-title"></div><div class="sk sk-main"></div><div class="sk sk-line"></div>
              </div>
              <div v-else class="card" :class="{ idle: backendDown }">
                <div class="name">PostgreSQL</div>
                <span class="fbadge"><span class="dot" :class="backendDown ? 'down' : (infra?.postgres?.status === 'up' ? 'up' : 'down')"></span><span class="fb-txt">{{ backendDown ? '数据不可用' : (infra?.postgres?.status === 'up' ? '已连接' : '未连接') }}</span></span>
                <div class="state">{{ backendDown ? '—' : (infra?.postgres?.status === 'up' ? '已连接' : '未连接') }}</div>
                <div class="detail">{{ backendDown ? '等待后端恢复后展示' : (infra?.postgres?.detail || '探测失败') }}</div>
              </div>
              <div v-if="!backendDown && pendingInfra" class="card skeleton-card">
                <div class="sk sk-title"></div><div class="sk sk-main"></div><div class="sk sk-line"></div>
              </div>
              <div v-else class="card" :class="{ idle: backendDown }">
                <div class="name">代理池</div>
                <span class="fbadge"><span class="dot" :class="backendDown ? 'down' : (infra?.proxy_pool?.status === 'up' ? 'up' : 'down')"></span><span class="fb-txt">{{ backendDown ? '数据不可用' : (infra?.proxy_pool?.status === 'up' ? '已连接' : '未连接') }}</span></span>
                <div class="state">{{ backendDown ? '—' : (infra?.proxy_pool?.status === 'up'
                  ? (infra.proxy_pool.count != null ? `已连接 · 在池 ${infra.proxy_pool.count}` : '已连接')
                  : '未连接') }}</div>
                <div class="detail">{{ backendDown ? '等待后端恢复后展示' : (infra?.proxy_pool?.detail || '探测失败') }}</div>
              </div>
            </div>

            <!-- 爬虫数据卡 -->
            <div v-if="pendingCrawl" class="card crawl skeleton-card">
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

            <p class="note">数据流：进入页面并发拉取 /healthz · /api/infra · /api/crawl/stats，各自返回即上屏互不等待</p>
          </template>

          <!-- ===== 数据浏览（占位；数据源子页为按来源筛选变体） ===== -->
          <template v-else-if="activeNav === 'data' || srcPage">
            <!-- 东财 Cookie 卡：仅东财子页显示 -->
            <div v-if="srcPage && srcPage.field === 'eastmoney'" class="card cookie-card"
              :class="emCookie.valid ? 'ok' : 'warn'">
              <div class="ck-head">
                <svg v-if="emCookie.valid" class="ck-icon" viewBox="0 0 16 16" aria-hidden="true">
                  <circle cx="8" cy="8" r="7" fill="#198038"/>
                  <path d="M4.8 8.2l2.2 2.2 4.2-4.6" stroke="#fff" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <svg v-else class="ck-icon" viewBox="0 0 16 16" aria-hidden="true">
                  <circle cx="8" cy="8" r="7" fill="#da1e28"/>
                  <path d="M5.2 5.2l5.6 5.6M10.8 5.2l-5.6 5.6" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>
                </svg>
                <b>东财爬虫 Cookie</b>
                <span class="ck-state" :class="emCookie.valid ? 'ok' : 'bad'">
                  {{ emCookie.valid ? '有效' : (emCookie.configured ? '已失效' : '未配置') }}
                </span>
              </div>
              <div v-if="emCookie.configured && emCookie.masked" class="ck-masked">已存：{{ emCookie.masked }}</div>
              <div v-if="!emCookie.valid" class="ck-help">
                <p><b>为什么需要 Cookie：</b>东财对无 Cookie 的裸请求会触发风控封 IP（上次封禁 5 天的教训）。</p>
                <p><b>获取步骤：</b>① 浏览器打开 <code>quote.eastmoney.com/center/</code> →
                  ② 按 F12 打开开发者工具 → 网络(Network) → 刷新页面 →
                  ③ 找到任一 <code>push2.eastmoney.com</code> 请求 →
                  ④ 在请求头里复制完整的 <code>Cookie:</code> 值（很长的一串）→
                  ⑤ 粘贴到下面保存。</p>
              </div>
              <div class="ck-form">
                <InputText v-model="emCookieInput" placeholder="粘贴完整 Cookie 值…"
                  class="ck-input" :disabled="emCookieSaving" />
                <Button label="保存并验证" size="small" @click="saveEmCookie"
                  :loading="emCookieSaving" :disabled="!emCookieInput.trim()" />
              </div>
              <div v-if="emCookieMsg" class="ck-msg" :class="emCookieMsg.ok ? 'ok' : 'bad'">{{ emCookieMsg.text }}</div>
            </div>
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
              <Select v-model="browseDelisted" :options="DELISTED_FILTERS"
                optionLabel="label" optionValue="value" size="small"
                placeholder="退市：全部" class="delisted-select"
                aria-label="按退市状态过滤" @change="browseSearch" />
            </div>
            <div class="browse-pv">
              <DataTable :value="browse.stocks || []" lazy paginator :rows="50"
                :first="browsePage * 50"
                :totalRecords="browse.total || 0" :loading="browseLoading"
                dataKey="code" stripedRows size="small"
                @page="onPage" @row-click="openStock">
                <template #empty>
                  <div class="browse-empty">
                    {{ browse.status === 'down' ? '数据库不可达：' + (browse.detail || '')
                      : (srcPage ? `${srcPage.label}源暂无入库数据（当前数据全部来自新浪源）` : '无匹配股票') }}
                  </div>
                </template>
                <Column field="code" header="代码" style="width: 100px">
                  <template #body="{ data }"><span class="mono">{{ data.code }}</span></template>
                </Column>
                <Column field="name" header="名称" />
                <Column field="delisted" header="退市" style="width: 80px">
                  <template #body="{ data }">
                    <span v-if="data.delisted" class="delisted-tag">已退市</span>
                    <span v-else>—</span>
                  </template>
                </Column>
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
            <p class="note">数据源：本地 PG（新浪/东财）· 点击行查看个股历史行情（K线图 + 全字段明细）· 图表：ECharts（按需引入）</p>
          </template>

          <!-- ===== 基金净值浏览（数据子页，行点击进净值详情） ===== -->
          <template v-else-if="activeNav === 'funds'">
            <div class="toolbar">
              <div>
                <h2 class="h1">基金净值</h2>
                <p class="sub">已入库公募基金净值一览 · 东财全集（含已清盘与 A/C 份额）· 任务「基金历史净值」爬取入库</p>
              </div>
            </div>
            <p class="note" v-if="STATIC_MODE && dca.meta?.generated_at"
              style="margin: 0 0 8px">静态快照（GitHub Pages）· 数据生成于 {{ dca.meta.generated_at.slice(0, 10) }} · 明细不含净值曲线 · 实时数据以内网部署为准</p>
            <div class="ph-search funds-search">
              <span class="p-inputtext-icon-wrap"><i class="pi pi-search"></i></span>
              <InputText v-model="fundsQuery" placeholder="搜索基金代码 / 名称，如 000001 或 华夏成长"
                class="browse-input" @keydown.enter="fundsSearch" />
              <Button label="搜索" size="small" severity="secondary" outlined @click="fundsSearch" />
              <Select v-model="fundsDelisted" :options="DELISTED_FILTERS"
                optionLabel="label" optionValue="value" size="small"
                placeholder="退市：全部" class="delisted-select"
                aria-label="按清盘/退市状态过滤" @change="fundsSearch" />
              <button class="filter-chip" :class="{ active: fundsShowBackend }"
                type="button" :aria-pressed="fundsShowBackend"
                title="后端收费份额（名称带 (后端) 标注，默认隐藏）"
                @click="toggleFundsBackend">
                <span class="chip-dot"></span>后端份额
              </button>
              <button class="filter-chip" :class="{ active: fundsShowMoney }"
                type="button" :aria-pressed="fundsShowMoney"
                title="货币基金（净值恒 1、无单位净值走势，默认隐藏）"
                @click="toggleFundsMoney">
                <span class="chip-dot"></span>货币基金
              </button>
              <button class="filter-chip" :class="{ active: fundsShowQdii }"
                type="button" :aria-pressed="fundsShowQdii"
                title="QDII 基金（净值披露滞后 1~2 日，默认隐藏）"
                @click="toggleFundsQdii">
                <span class="chip-dot"></span>QDII
              </button>
            </div>
            <div class="browse-pv">
              <DataTable :value="funds.funds || []" lazy paginator :rows="50"
                :first="fundsPage * 50"
                :totalRecords="funds.total || 0" :loading="fundsLoading"
                dataKey="code" stripedRows size="small"
                :sortField="fundsSort || undefined"
                :sortOrder="fundsOrder === 'asc' ? 1 : -1"
                @page="onFundsPage" @sort="onFundsSort" @row-click="openFund">
                <template #empty>
                  <div class="browse-empty">
                    {{ funds.status === 'down' ? '数据库不可达：' + (funds.detail || '')
                      : '暂无基金数据（运行任务「基金历史净值」爬取入库后在此展示）' }}
                  </div>
                </template>
                <Column field="code" header="代码" style="width: 100px">
                  <template #body="{ data }"><span class="mono">{{ data.code }}</span></template>
                </Column>
                <Column field="name" header="名称" />
                <Column field="fund_type" header="类型" style="width: 140px">
                  <template #body="{ data }">{{ data.fund_type || '—' }}</template>
                </Column>
                <Column field="unit_nav" header="最新单位净值" style="width: 120px" bodyClass="num-col">
                  <template #body="{ data }">{{ data.unit_nav ?? '—' }}</template>
                </Column>
                <Column field="pct_chg" header="日增长率" style="width: 100px">
                  <template #body="{ data }">
                    <span v-if="data.pct_chg != null" :class="data.pct_chg >= 0 ? 'chg-up' : 'chg-down'">
                      {{ (data.pct_chg >= 0 ? '+' : '') + data.pct_chg + '%' }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="vol_all" sortable bodyClass="num-col" style="width: 108px">
                  <template #header><span title="全史年化波动率：该基金全部历史净值日收益率标准差 × √252，复权口径；日更任务快照（2026-11-18 起由近1年改全史口径）">年化波动率</span></template>
                  <template #body="{ data }">{{ volpct(data.vol_all) }}</template>
                </Column>
                <Column field="vol_down_all" sortable bodyClass="num-col" style="width: 108px">
                  <template #header><span title="全史下行波动率：全部历史中只统计下跌日收益率的标准差 × √252（真实痛苦的方向）">下行波动率</span></template>
                  <template #body="{ data }">{{ volpct(data.vol_down_all) }}</template>
                </Column>
                <Column field="mdd_all" sortable bodyClass="num-col" style="width: 100px">
                  <template #header><span title="全史最大回撤：全部历史复权净值峰谷最大回撤（正数存储，显示为负号）">最大回撤</span></template>
                  <template #body="{ data }">{{ mddneg(data.mdd_all) }}</template>
                </Column>
                <Column field="sharpe_all" sortable bodyClass="num-col" style="width: 88px">
                  <template #header><span title="全史夏普比率：日收益均值/标准差 × √252，无风险利率 0（每单位波动换回的收益）">夏普</span></template>
                  <template #body="{ data }">{{ sharpe2(data.sharpe_all) }}</template>
                </Column>
                <Column field="vol_pctile" sortable bodyClass="num-col" style="width: 100px">
                  <template #header><span title="当前 60 日滚动年化波动率在该基金自身全史滚动序列中的百分位：越低=当前比历史更平静，越高=更颠簸">波动率分位</span></template>
                  <template #body="{ data }">{{ pctile(data.vol_pctile) }}</template>
                </Column>
                <Column field="osc_pctile" sortable bodyClass="num-col" style="width: 100px">
                  <template #header><span title="震荡分位：全史净值 zigzag（±10% 确认反转）检测的年均规则摆动轮数（频率 × 间隔均匀度）在全市场的百分位排名，越高 = 净值越像余弦一样规律地上上下下">震荡分位</span></template>
                  <template #body="{ data }">{{ pctile(data.osc_pctile) }}</template>
                </Column>
                <Column field="navs" header="净值条数" style="width: 100px">
                  <template #body="{ data }">{{ data.navs || 0 }}</template>
                </Column>
                <Column field="last_date" header="最新净值日" style="width: 120px">
                  <template #body="{ data }">{{ data.last_date || '—' }}</template>
                </Column>
                <Column field="first_date" header="最早净值日" style="width: 120px">
                  <template #body="{ data }">{{ data.first_date || '—' }}</template>
                </Column>
              </DataTable>
            </div>
            <p class="note">来源：本地 PG（fund_basic / fund_nav，任务源 fund）· 点击行查看基金净值走势与明细 · 货币基金无净值走势不在此列 · 波动指标列（年化/下行波动率、最大回撤、夏普）为全史口径 = 全部历史净值（2026-11-18 起由近1年改全史；样本不足 20 个收益为 —），日收益率取复权日增长率、年化系数 √252、无风险利率 0，日更任务（回测）顺带快照；分位 = 当前 60 日滚动波动率在该基金自身全史滚动序列中的百分位，越低越平静 · 震荡分位 = 全史净值 zigzag（±10% 确认反转）年均规则摆动轮数的横截面百分位，越高 = 净值越规律地上上下下——日更未跑过时全列为 —</p>
          </template>

          <!-- ===== 定投止盈回测排行（离线回测快照：scripts/fund_dca_backtest.py → app.dca_store 导入） ===== -->
          <template v-else-if="activeNav === 'fundDca'">
            <div class="toolbar">
              <div>
                <h2 class="h1">定投止盈回测</h2>
                <!-- 口径副标题已删（2026-11-23 五轮用户要求）：口径说明收进
                     顶部「定投方式」? 图标弹出面板与各按钮悬停提示 -->
              </div>
            </div>

            <!-- ===== 顶部总开关行（2026-11-23 三轮用户需求：四组口径切换全部置顶）：
                 定投方式/止盈点/基金类型/费用口径——切换后下方所有统计跟随
                 （统计卡/止盈点对比/排行表/散点分箱/详情弹窗；弹窗内另有局部
                 止盈点/费率 tab 属明细级控件）。定投方式组带 ? 图标：点击弹出
                 三种方式完整说明，悬停各按钮 title 显示对应方式详情。
                 起点条件/熊市断缴两实验卡为等额固定快照不随切（标题已标注）；
                 静态模式无多阈值/方式维度，对应组隐藏（类型/费用仍可用——客户端切换） ===== -->
            <div class="dca-filter-row dca-filter-segs" style="margin: 0 0 14px">
              <div class="seg-group mode-seg" v-if="!STATIC_MODE">
                <span class="seg-label">定投方式</span>
                <div class="src-seg" title="切换定投方式（悬停各按钮看对应说明，点 ? 查看全部）">
                  <button v-for="o in DCA_MODE_OPTIONS" :key="o.key"
                          class="src-seg-btn"
                          :class="{ active: dcaMode === o.key }"
                          :title="`${DCA_MODE_HELP[o.key].title}：${DCA_MODE_HELP[o.key].detail}`"
                          @click="onDcaModeChange(o.key)"
                          :data-mode="o.key">{{ o.label }}</button>
                </div>
                <button class="mode-help-btn" @click="dcaModeHelpShow = !dcaModeHelpShow"
                        :class="{ active: dcaModeHelpShow }"
                        data-mode-help aria-label="定投方式说明"
                        title="查看三种定投方式的完整说明">?</button>
                <div v-if="dcaModeHelpShow" class="mode-help-pop" data-mode-help-pop>
                  <div v-for="o in DCA_MODE_OPTIONS" :key="o.key" class="mode-help-item">
                    <b>{{ DCA_MODE_HELP[o.key].title }}</b>
                    <span class="mode-help-brief">{{ DCA_MODE_HELP[o.key].brief }}</span>
                    <p>{{ DCA_MODE_HELP[o.key].detail }}</p>
                  </div>
                  <p class="mode-help-note">切换后下方统计卡/止盈点对比/排行表/散点分箱/详情弹窗全部跟随；起点条件回测与熊市断缴两实验卡为等额口径固定快照，不随切换。历史统计不构成任何操作建议。</p>
                </div>
              </div>
              <div class="seg-group" v-if="!STATIC_MODE">
                <span class="seg-label">止盈点</span>
                <div class="src-seg" title="切换止盈线：排行/统计/对比高亮/散点分箱/明细跟随（对比区块行/圆点同款状态）">
                  <button v-for="t in DCA_TP_OPTIONS" :key="t"
                          class="src-seg-btn"
                          :class="{ active: dcaTp === t }"
                          @click="onDcaTpChange(t)"
                          :data-tp="t">{{ dcaTpLabel(t) }}</button>
                </div>
              </div>
              <div class="seg-group">
                <span class="seg-label">基金类型</span>
                <div class="src-seg" title="排行/统计/散点/对比池共用同一基金类型口径（默认仅权益类；对比卡池=权益 8 类+商品，全市场时为全市场对照）">
                  <button class="src-seg-btn"
                          :class="{ active: dcaEquity }"
                          @click="dcaEquity = true"
                          data-table-cat="equity">仅权益类</button>
                  <button class="src-seg-btn"
                          :class="{ active: !dcaEquity }"
                          @click="dcaEquity = false"
                          data-table-cat="all">全市场</button>
                </div>
              </div>
              <div class="seg-group">
                <span class="seg-label">费用口径</span>
                <div class="src-seg" title="排行/统计/散点/对比/详情共用同一费用口径（不含费 / 含费·申购一折 / 含费·申购名义）">
                  <button v-for="o in DCA_FEE_OPTIONS" :key="o.key"
                          class="src-seg-btn"
                          :class="{ active: dcaFeeMode === o.key }"
                          @click="dcaFeeMode = o.key"
                          :data-table-fee="o.key">{{ o.label }}</button>
                </div>
              </div>
            </div>

            <div class="learn-metrics" v-if="dca.stats">
              <div class="card lm"><div class="lm-k">在市基金</div><div class="lm-v">{{ dca.stats.count }}</div></div>
              <div class="card lm"><div class="lm-k">年化中位</div><div class="lm-v">{{ pctp(dca.stats.median) }}</div></div>
              <div class="card lm"><div class="lm-k">P25</div><div class="lm-v">{{ pctp(dca.stats.p25) }}</div></div>
              <div class="card lm"><div class="lm-k">P75</div><div class="lm-v">{{ pctp(dca.stats.p75) }}</div></div>
              <div class="card lm"><div class="lm-k">P90</div><div class="lm-v">{{ pctp(dca.stats.p90) }}</div></div>
              <div class="card lm"><div class="lm-k">年化为正</div><div class="lm-v">{{ pct0(dca.stats.positive_share) }}</div></div>
              <div class="card lm"><div class="lm-k">曾止盈</div><div class="lm-v">{{ pct0(dca.stats.take_profit_share) }}</div></div>
            </div>
            <!-- ===== 止盈点对比（市场级 5%~30% 六档，权益类+商品池；点击行/点切换排行阈值） ===== -->
            <div class="card" v-if="!STATIC_MODE && dcaTpCompare" style="margin-top: 16px; padding: 14px 16px 18px">
              <div class="toolbar" style="margin: 0 0 6px">
                <div>
                  <h2 class="h1" style="font-size: 18px">止盈点对比：止盈线设多少更划算？<span class="dc-hint" style="margin-left: 6px">当前口径：{{ dcaModeLabel }}</span></h2>
                  <p class="sub">同一「月定投（{{ dcaModeLabel }}）+ 止盈重来」策略只换止盈线（5%~40% 八档）在{{ dcaEquity ? '权益类+商品' : '全市场' }}在市基金上的整体表现 · 年化=XIRR 资金加权（钱在场的效率，跨阈值可比；总收益率天然偏向高阈值不作对比依据）· 每档采样 {{ dcaTpCompare.rows?.[0]?.count ?? 0 }} 只（在市且年化非空）· {{ dcaFeeLabel }} · {{ dcaEquity ? '权益+商品池' : '全市场池' }}（随顶部基金类型开关）</p>
                </div>
                <div class="toolbar-right">
                  <span v-if="dcaTpLoading" class="sub">重算中…</span>
                </div>
              </div>
              <div v-if="dcaTpCompare.status === 'down'" class="browse-empty">
                对比数据不可用：{{ dcaTpCompare.detail || '数据库连接失败' }}
              </div>
              <template v-else-if="dcaTpCompare.rows?.length">
                <div style="overflow-x: auto">
                  <table class="dca-matrix tp-compare" style="width: 100%; min-width: 640px">
                    <thead><tr>
                      <th>止盈点</th><th>中位年化·近10年</th><th>平均年化·近10年</th><th>年化为正</th><th>曾止盈</th><th>平均轮次</th><th>平均间隔</th><th>中位年化·全史</th>
                    </tr></thead>
                    <tbody>
                      <tr v-for="r in dcaTpCompare.rows" :key="r.tp"
                          :class="{ 'tp-row-active': dcaTp === r.tp }"
                          title="点击切换排行表格到该止盈点"
                          @click="onDcaTpChange(r.tp)">
                        <td><b>{{ dcaTpLabel(r.tp) }}</b><span v-if="r.tp === 0.20" class="dc-hint" style="margin-left: 6px">默认</span></td>
                        <td><span :class="r.w10_median >= 0 ? 'chg-up' : 'chg-down'">{{ pctp(r.w10_median) }}</span></td>
                        <td>{{ pctp(r.w10_mean) }}</td>
                        <td>{{ pct0(r.w10_positive_share) }}</td>
                        <td>{{ pct0(r.w10_success_share) }}</td>
                        <td>{{ r.w10_avg_rounds ?? '—' }}</td>
                        <td>{{ r.avg_gap_days != null ? '≈' + (r.avg_gap_days / 30.44).toFixed(1) + ' 月/次' : '—' }}</td>
                        <td>{{ pctp(r.full_median) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <svg v-if="dcaTpChart" class="tp-chart"
                     :viewBox="`0 0 ${dcaTpChart.W} ${dcaTpChart.H}`"
                     role="img" aria-label="中位年化（近10年）随止盈点变化折线">
                  <line :x1="dcaTpChart.pts[0].x" :y1="dcaTpChart.zeroY"
                        :x2="dcaTpChart.pts[dcaTpChart.pts.length - 1].x" :y2="dcaTpChart.zeroY" class="tp-zero" />
                  <polyline :points="dcaTpChart.pts.map(p => p.x + ',' + p.y).join(' ')" class="tp-line" />
                  <g v-for="p in dcaTpChart.pts" :key="p.tp">
                    <circle :cx="p.x" :cy="p.y" r="4" :class="{ 'tp-dot-active': dcaTp === p.tp }"
                            style="cursor: pointer" @click="onDcaTpChange(p.tp)" />
                    <text :x="p.x" :y="dcaTpChart.H - 8" text-anchor="middle" class="tp-x">{{ dcaTpLabel(p.tp) }}</text>
                    <text :x="p.x" :y="p.y - 9" text-anchor="middle" class="tp-v">{{ pctp(p.v) }}</text>
                  </g>
                </svg>
                <p class="note" style="margin: 10px 0 0">口径：权益类+商品池（权益 8 类白名单 + QDII-商品/商品；债基·FOF·货币·偏债·REITs 不在内——债基货基波动小到连 5% 都难触发，进平均会稀释阈值间差异）· 近 10 年窗口为主口径，「全史」自基金成立起 · 「曾止盈」= 窗口内触发过至少一轮止盈的基金占比 · 平均间隔按费用口径轮次聚合 · 点击表格行或折线圆点，排行表格切换到该止盈点；散点/分箱跟随顶部定投方式与止盈点开关 · 本区块为历史回测统计，不构成任何操作建议</p>
              </template>
              <div v-else class="browse-empty">多阈值对比数据尚未生成（日更回测首次写入后可用）</div>
            </div>

            <!-- ===== 起点条件回测（不同起点开始定投的结果差异；假设验证统计静态快照） ===== -->
            <div class="card dip-report-block" v-if="!STATIC_MODE && dcaDipReport" style="margin-top: 16px; padding: 14px 16px 18px">
              <div class="toolbar" style="margin: 0 0 6px">
                <div>
                  <h2 class="h1" style="font-size: 18px">起点条件回测：什么时候开始定投，结果差多少？<span class="dc-hint" style="margin-left: 6px">等额口径固定快照（不随顶部定投方式/止盈点/费用口径切换）</span></h2>
                  <p class="sub">同一「月定投 + 止盈重来」策略从不同起点开始（权益基金 2015~2026 全历史、98 万个起点、每起点独立模拟到首轮止盈）· 每行 = 首轮止盈天数分位 / 3 年内止盈成功率 / 首轮资金加权年化（XIRR）· 无费口径 · 静态验证快照（重跑后刷新）</p>
                </div>
              </div>
              <div v-if="dcaDipReport.status === 'down'" class="browse-empty">
                起点条件回测数据不可用：{{ dcaDipReport.detail || '数据库连接失败' }}
              </div>
              <template v-else>
                <template v-for="sec in [
                    { key: 'groups', title: '起点位置三组对照（止盈线 20%）' },
                    { key: 'buckets', title: '深回撤起点：按最大回撤深度分桶（止盈线 20%）' },
                    { key: 'years', title: '起点年份（随机起点，止盈线 20%）' }]">
                  <h3 style="font-size: 14px; margin: 12px 0 6px">{{ sec.title }}</h3>
                  <div style="overflow-x: auto">
                    <table class="dca-matrix" style="width: 100%; min-width: 560px">
                      <thead><tr>
                        <th>起点条件</th><th>样本数</th><th>首轮止盈中位</th><th>P25</th><th>P75</th><th>3 年内止盈成功率</th><th>首轮年化中位</th><th>年化 P25</th><th>年化 P75</th>
                      </tr></thead>
                      <tbody>
                        <tr v-for="r in dcaDipReport[sec.key]" :key="r.grp + r.bucket + r.tp">
                          <td>{{ dipRowLabel(r) }}</td>
                          <td>{{ r.n == null ? '—' : r.n.toLocaleString() }}</td>
                          <td>{{ dipDay(r.days_median) }}</td>
                          <td>{{ dipDay(r.days_p25) }}</td>
                          <td>{{ dipDay(r.days_p75) }}</td>
                          <td>{{ dipPct(r.success_rate) }}</td>
                          <td>{{ dipPct(r.xirr_median) }}</td>
                          <td>{{ dipPct(r.xirr_p25) }}</td>
                          <td>{{ dipPct(r.xirr_p75) }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </template>
                <p class="note" style="margin: 10px 0 0">口径：每月首个净值日投 1000 元（无费口径），任一净值日市值 ≥ 投入×(1+止盈线) 即首轮止盈并结束该次模拟；「3 年内止盈成功率」分母含数据结束仍未触发者（"起不来"如实计入）· 深回撤起点 = 回撤进入 ≥20% 的首日（不知是否谷底）· 「甜蜜点」在 -25%~-30% 桶（快/成功率高/年化高），&gt;-30% 超深回撤显著变差 · 历史统计不构成任何操作建议</p>
              </template>
            </div>

            <!-- ===== 熊市断缴实验（2026-11-19 scratch 实验产品化：熊市态断缴 vs 坚持
                 定投，同基金四条时间线配对比较；静态快照 fund_bear_pause_*，
                 scripts/bear_pause_import.py 重跑后刷新；gross 无费固定口径，
                 不随费率/权益开关变化，只在进入子页时加载一次） ===== -->
            <div class="card pause-exp-block" data-pause-exp v-if="!STATIC_MODE && dcaPauseExp" style="margin-top: 16px; padding: 14px 16px 18px">
              <div class="toolbar" style="margin: 0 0 6px">
                <div>
                  <h2 class="h1" style="font-size: 18px">熊市断缴实验：跌深了停扣，代价有多大？<span class="dc-hint" style="margin-left: 6px">等额口径固定快照（不随顶部定投方式/止盈点/费用口径切换）</span></h2>
                  <p class="sub">熊市态 = 距历史高点回撤 ≤-20% 进入、收窄回 &gt;-10% 才恢复 · 同一只基金四条平行时间线配对（A 坚持定投 / B 断缴闲置 / C 断缴+恢复补投 / D 熊市加倍）· 权益基金全史 {{ dcaPauseUniverse.backtested?.toLocaleString() || '—' }} 只（截数 {{ dcaPauseUniverse.data_end || '—' }}）· 无费口径 · 历史实验统计，不构成任何操作建议</p>
                </div>
                <div class="src-seg" data-pause-mode style="flex-shrink: 0">
                  <button v-for="m in PAUSE_MODES" :key="m.key" class="src-seg-btn"
                    :class="{ active: dcaPauseMode === m.key }" @click="dcaPauseMode = m.key">{{ m.label }}</button>
                </div>
              </div>
              <div v-if="dcaPauseExp.status === 'down'" class="browse-empty">
                断缴实验数据不可用：{{ dcaPauseExp.detail || '数据库连接失败' }}
              </div>
              <template v-else-if="Object.keys(dcaPauseBlocks).length">
                <div class="learn-metrics" data-pause-kpis style="margin: 8px 0 12px">
                  <div class="card lm"><div class="lm-k">曾断缴基金<br><span class="dc-hint">全史触发过 ≥20% 回撤</span></div><div class="lm-v">{{ pct0(dcaPauseUniverse.ever_paused_pct) }}</div></div>
                  <div class="card lm"><div class="lm-k">B 断缴闲置<br><span class="dc-hint">钱包差中位</span></div><div class="lm-v">{{ pauseMoney(dcaPauseBlocks.all?.b_wallet_delta_med) }}</div></div>
                  <div class="card lm"><div class="lm-k">C 断缴补投<br><span class="dc-hint">钱包差中位</span></div><div class="lm-v">{{ pauseMoney(dcaPauseBlocks.all?.c_wallet_delta_med) }}</div></div>
                  <div class="card lm"><div class="lm-k">D 熊市加倍<br><span class="dc-hint">同额钱包差中位</span></div><div class="lm-v">{{ pauseMoney(dcaPauseBlocks.all?.d_wallet_delta_med) }}</div></div>
                </div>
                <div style="overflow-x: auto" data-pause-strata>
                  <table class="dca-matrix" style="width: 100%; min-width: 820px">
                    <thead><tr>
                      <th>子样本</th><th>样本数</th><th>A 坚持·年化中位</th>
                      <th>B 钱包差中位</th><th>B 比 A 薄</th>
                      <th>C 钱包差中位</th><th>C 比 A 薄</th>
                      <th>D 同额钱包差中位</th><th>D 年化更低</th>
                    </tr></thead>
                    <tbody>
                      <tr v-for="s in PAUSE_SUBSET_ORDER" :key="s" v-show="dcaPauseBlocks[s]">
                        <td><b>{{ PAUSE_SUBSET_LABELS[s] }}</b><span v-if="s === 'paused_only'" class="dc-hint" style="margin-left: 6px">主比较</span></td>
                        <td>{{ dcaPauseBlocks[s]?.n?.toLocaleString() }}</td>
                        <td>{{ pauseXirr(dcaPauseBlocks[s]?.xirr_a_med) }}</td>
                        <td><span :class="(dcaPauseBlocks[s]?.b_wallet_delta_med ?? 0) < 0 ? 'chg-down' : 'chg-up'">{{ pauseMoney(dcaPauseBlocks[s]?.b_wallet_delta_med) }}</span></td>
                        <td>{{ pct0(dcaPauseBlocks[s]?.b_wallet_lower_pct) }}</td>
                        <td><span :class="(dcaPauseBlocks[s]?.c_wallet_delta_med ?? 0) < 0 ? 'chg-down' : 'chg-up'">{{ pauseMoney(dcaPauseBlocks[s]?.c_wallet_delta_med) }}</span></td>
                        <td>{{ pct0(dcaPauseBlocks[s]?.c_wallet_lower_pct) }}</td>
                        <td><span :class="(dcaPauseBlocks[s]?.d_wallet_delta_med ?? 0) < 0 ? 'chg-down' : 'chg-up'">{{ pauseMoney(dcaPauseBlocks[s]?.d_wallet_delta_med) }}</span></td>
                        <td>{{ pct0(dcaPauseBlocks[s]?.d_xirr_lower_pct) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <h3 style="font-size: 14px; margin: 14px 0 6px">案例基金（点击行看单基金四组明细）</h3>
                <div style="overflow-x: auto" data-pause-cases>
                  <table class="dca-matrix" style="width: 100%; min-width: 720px">
                    <thead><tr>
                      <th>基金</th><th>末回撤</th><th>断缴</th>
                      <th>A 坚持·钱包</th><th>B 断缴闲置·钱包</th><th>C 断缴补投·钱包</th><th>D 熊市加倍·钱包</th>
                    </tr></thead>
                    <tbody>
                      <tr v-for="c in dcaPauseExp.cases" :key="c.code" style="cursor: pointer"
                          title="点击打开该基金的定投止盈明细弹窗"
                          @click="openDcaRounds({ data: { code: c.code, name: (c.code || '') + ' ' + (c.name || '') } })">
                        <td><b class="mono">{{ c.code }}</b> {{ c.name }}</td>
                        <td>{{ ddPct(c.dd_end) }}</td>
                        <td>{{ c.ever_paused ? c.episodes + ' 次 / ' + c.pause_months + ' 月' : '从未' }}</td>
                        <td>{{ pauseMoney(c[dcaPauseMode + '_a_wallet']) }}</td>
                        <td>{{ pauseMoney(c[dcaPauseMode + '_b_wallet']) }}</td>
                        <td>{{ pauseMoney(c[dcaPauseMode + '_c_wallet']) }}</td>
                        <td>{{ pauseMoney(c[dcaPauseMode + '_d_wallet']) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p class="note" style="margin: 10px 0 0">口径：月投 1000（无费），钱包 = 止盈累计回款 + 期末市值 + 期末囤现金（0% 机会成本，最保守）·「钱包差中位」= 与同基金坚持定投组的期末钱包差的中位数：B/C 与坚持组花出去的钱完全一致可直接比，D 多投的钱按 0 收益折算 ·「止盈 20%」= 任一净值日市值 ≥ 投入×1.2 全清重来，「无止盈」= 只买不卖持有到期末 · 2026-11-19 全市场结论：断缴闲置 82% 基金更薄（中位 −7 千）、恢复补投基本追平（+68 元）、熊市加倍 68% 基金更好；断缴该不该做取决于标的是否涨得回来——而那在当下不可知 · 分层主结论见上表「仅断缴过的基金」行</p>
              </template>
              <div v-else class="browse-empty">断缴实验数据尚未生成（运行 scripts/bear_pause_import.py 后可用）</div>
            </div>
            <p class="note" v-if="STATIC_MODE && dca.meta?.generated_at"
              style="margin: 0 0 8px">静态快照（GitHub Pages）· 数据生成于 {{ dca.meta.generated_at.slice(0, 10) }} · 明细不含净值曲线 · 实时数据以内网部署为准</p>
            <!-- 统一筛选面板（2026-11-17 用户反馈：筛选区显乱）——一个面板三行分组，
                 行间独立 flex 互不搅动：行1 搜索与基础筛选（搜索框自带边框）·
                 行2 口径切换（组带小标签）· 行3 区间筛选（内嵌浅灰区块） -->
            <div class="dca-filter-panel">
              <div class="dca-filter-row funds-search">
                <span class="dca-search-box">
                  <span class="p-inputtext-icon-wrap"><i class="pi pi-search"></i></span>
                  <InputText v-model="dcaQuery" placeholder="搜索基金代码 / 名称，如 510300"
                    class="browse-input" @keydown.enter="dcaSearch" />
                </span>
                <Button label="搜索" size="small" severity="secondary" outlined @click="dcaSearch" />
                <Select v-model="dcaAlive" :options="DCA_ALIVE_FILTERS"
                  optionLabel="label" optionValue="value" size="small"
                  placeholder="仅在市" class="delisted-select"
                  aria-label="按在市状态过滤" @change="dcaSearch" />
                <span class="age-wrap" title="只看运作超过 N 年的基金；0 或留空 = 全部">
                  <span class="age-suffix">运作 ≥</span>
                  <InputNumber v-model="dcaAge" :min="0" :max="99"
                    :maxFractionDigits="0" :useGrouping="false" size="small"
                    inputClass="age-input" inputId="dca-age"
                    aria-label="按运作年限过滤，大于该年数" />
                  <span class="age-suffix">年</span>
                </span>
                <span class="age-wrap" title="只看规模超过 N 亿的基金；0 或留空 = 全部（含无规模披露）">
                  <span class="age-suffix">规模 ≥</span>
                  <InputNumber v-model="dcaScale" :min="0" :max="99999"
                    :maxFractionDigits="2" :useGrouping="false" size="small"
                    inputClass="age-input" inputId="dca-scale"
                    aria-label="按资产规模过滤，大于该数值（亿元）" />
                  <span class="age-suffix">亿</span>
                </span>
                <span class="dca-filter-spacer"></span>
                <!-- 列显示过滤（2026-11-17 用户需求）：勾选表格要显示的列；
                     代码/名称/操作恒显，纯客户端裁剪不影响服务端分页 -->
                <MultiSelect v-model="dcaVisibleCols" :options="DCA_TOGGLE_COLS"
                  optionLabel="label" optionValue="key" size="small"
                  placeholder="显示列" class="dca-col-select"
                  :maxSelectedLabels="0" selectedItemsLabel="已选 {0} 列"
                  title="勾选表格要显示的列" aria-label="选择表格显示列" />
              </div>
              <!-- 区间筛选：常显（2026-11-17 用户反馈：折叠按钮多余，且固定五列
                   网格被 InputNumber 的 min-content 撑破、在窄屏冲出页面）——
                   响应式 auto-fit 网格 + 输入框可收缩（min-width:0 / input 100%）。
                   表格合并后直接作用于统一排行表（服务端过滤；回撤桶按钮组已删，
                   与本面板的当前回撤维度重复） -->
              <div class="timing-filter timing-filter--inline">
                <div class="timing-filter-head">
                  <span class="timing-filter-title">区间筛选</span>
                  <button v-if="dcaRangeFiltersActive" class="src-seg-btn"
                          @click="resetDcaRangeFilters">清除筛选</button>
                  <span v-if="dcaRangeFiltersActive" class="sub">筛选已生效 · {{ dca.total ?? 0 }} 只</span>
                </div>
                <div class="timing-filter-grid">
                  <label>运作年限（年）
                    <div class="timing-filter-range">
                      <InputNumber v-model="dcaRangeFilters.age_min" :min="0" :max="99" :maxFractionDigits="1" :useGrouping="false" size="small" placeholder="最短" style="width: 100%" />
                      <span>~</span>
                      <InputNumber v-model="dcaRangeFilters.age_max" :min="0" :max="99" :maxFractionDigits="1" :useGrouping="false" size="small" placeholder="最长" style="width: 100%" />
                    </div>
                  </label>
                  <label>止盈间隔（天）
                    <div class="timing-filter-range">
                      <InputNumber v-model="dcaRangeFilters.gap_min" :min="0" :useGrouping="false" size="small" placeholder="最短" style="width: 100%" />
                      <span>~</span>
                      <InputNumber v-model="dcaRangeFilters.gap_max" :min="0" :useGrouping="false" size="small" placeholder="最长" style="width: 100%" />
                    </div>
                  </label>
                  <label>年化（0.3 = 30%）
                    <div class="timing-filter-range">
                      <InputNumber v-model="dcaRangeFilters.xirr_min" :min="-1" :max="20" :maxFractionDigits="2" :useGrouping="false" size="small" placeholder="最低" style="width: 100%" />
                      <span>~</span>
                      <InputNumber v-model="dcaRangeFilters.xirr_max" :min="-1" :max="20" :maxFractionDigits="2" :useGrouping="false" size="small" placeholder="最高" style="width: 100%" />
                    </div>
                  </label>
                  <label>当前回撤（%，正数幅度）
                    <div class="timing-filter-range">
                      <InputNumber v-model="dcaRangeFilters.dd_min" :min="0" :max="99" :maxFractionDigits="1" :useGrouping="false" size="small" placeholder="至少跌" style="width: 100%" />
                      <span>~</span>
                      <InputNumber v-model="dcaRangeFilters.dd_max" :min="0" :max="99" :maxFractionDigits="1" :useGrouping="false" size="small" placeholder="至多跌" style="width: 100%" />
                    </div>
                  </label>
                  <label>总收益率（0.1 = 10%）
                    <div class="timing-filter-range">
                      <InputNumber v-model="dcaRangeFilters.pr_min" :min="-1" :max="20" :maxFractionDigits="2" :useGrouping="false" size="small" placeholder="最低" style="width: 100%" />
                      <span>~</span>
                      <InputNumber v-model="dcaRangeFilters.pr_max" :min="-1" :max="20" :maxFractionDigits="2" :useGrouping="false" size="small" placeholder="最高" style="width: 100%" />
                    </div>
                  </label>
                  <div class="timing-filter-actions">
                    <Button label="应用筛选" size="small" @click="applyDcaRangeFilters" />
                    <Button label="重置" size="small" severity="secondary" outlined @click="resetDcaRangeFilters" />
                  </div>
                </div>
              </div>
            </div>
            <div class="browse-pv">
              <DataTable :value="dca.rows || []" lazy paginator :rows="DCA_PAGE_SIZE"
                :first="dcaPage * DCA_PAGE_SIZE"
                :totalRecords="dca.total || 0" :loading="dcaLoading"
                dataKey="code" stripedRows size="small"
                :sortField="dcaSort" :sortOrder="dcaOrder === 'asc' ? 1 : -1"
                @page="onDcaPage" @sort="onDcaSort" @row-click="openDcaRounds">
                <template #empty>
                  <div class="browse-empty">
                    {{ dca.status === 'down' ? '数据库不可达：' + (dca.detail || '')
                      : dca.status === 'loading' ? '加载中…'
                      : '暂无回测数据（运行 scripts/fund_dca_backtest.py 回测并经 app.dca_store 导入后展示）' }}
                  </div>
                </template>
                <Column field="code" header="代码" style="width: 90px" sortable>
                  <template #body="{ data }"><span class="mono">{{ data.code }}</span></template>
                </Column>
                <Column field="name" header="名称" />
                <Column field="fund_type" header="类型" style="width: 130px" v-if="colVisible('fund_type')">
                  <template #body="{ data }">{{ data.fund_type || '—' }}</template>
                </Column>
                <Column field="dd_current" header="当前回撤" style="width: 92px" sortable bodyClass="num-col" v-if="colVisible('dd_current')">
                  <template #body="{ data }">
                    <span v-if="data.dd_current != null" :class="data.dd_current <= -0.3 ? 'neg' : ''"
                          :title="data.dip_band === '25-30' ? '甜蜜点：历史上从该位置开始定投首轮止盈最快' : ''">{{ ddpct(data.dd_current) }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="days_since_peak" header="距前高" style="width: 88px" sortable bodyClass="num-col" v-if="colVisible('days_since_peak')">
                  <template #body="{ data }">{{ data.days_since_peak != null ? data.days_since_peak + ' 天' : '—' }}</template>
                </Column>
                <Column field="age_years" sortField="full_start" header="运作年限" style="width: 90px" sortable bodyClass="num-col" v-if="colVisible('age_years')">
                  <template #body="{ data }">{{ data.age_years != null ? data.age_years + ' 年' : '—' }}</template>
                </Column>
                <Column field="scale_yi" header="规模(亿)" style="width: 95px" sortable bodyClass="num-col" v-if="colVisible('scale_yi')">
                  <template #body="{ data }">{{ data.scale_yi != null ? data.scale_yi : '—' }}</template>
                </Column>
                <!-- 注意：header 文字只写在 #header 插槽里——同时给 Column
                     header 属性会被 PrimeVue 双重渲染（2026-11-18 用户报告） -->
                <Column field="vol_all" style="width: 104px" sortable bodyClass="num-col" v-if="colVisible('vol_all')">
                  <template #header><span title="全史年化波动率：该基金全部历史净值日收益率标准差 × √252，复权口径；fund_risk_metrics 日更快照（2026-11-18 起由近1年改全史口径）">年化波动率</span></template>
                  <template #body="{ data }">{{ volpct(data.vol_all) }}</template>
                </Column>
                <Column field="vol_down_all" style="width: 104px" sortable bodyClass="num-col" v-if="colVisible('vol_down_all')">
                  <template #header><span title="全史下行波动率：全部历史中只统计下跌日收益率的标准差 × √252（真实痛苦的方向）">下行波动率</span></template>
                  <template #body="{ data }">{{ volpct(data.vol_down_all) }}</template>
                </Column>
                <Column field="mdd_all" style="width: 96px" sortable bodyClass="num-col" v-if="colVisible('mdd_all')">
                  <template #header><span title="全史最大回撤：全部历史复权净值峰谷最大回撤（正数存储，显示为负号）">最大回撤</span></template>
                  <template #body="{ data }">{{ mddneg(data.mdd_all) }}</template>
                </Column>
                <Column field="sharpe_all" style="width: 84px" sortable bodyClass="num-col" v-if="colVisible('sharpe_all')">
                  <template #header><span title="全史夏普比率：日收益均值/标准差 × √252，无风险利率 0（每单位波动换回的收益）">夏普</span></template>
                  <template #body="{ data }">{{ sharpe2(data.sharpe_all) }}</template>
                </Column>
                <Column field="vol_pctile" style="width: 100px" sortable bodyClass="num-col" v-if="colVisible('vol_pctile')">
                  <template #header><span title="当前 60 日滚动年化波动率在该基金自身全史滚动序列中的百分位：越低=当前比历史更平静，越高=更颠簸">波动率分位</span></template>
                  <template #body="{ data }">{{ pctile(data.vol_pctile) }}</template>
                </Column>
                <Column field="risk_score" style="width: 108px" sortable bodyClass="num-col" v-if="colVisible('risk_score')">
                  <template #header><span title="综合波动分位：年化波动率/下行波动率/最大回撤三项在全市场同类基金中的百分位排名等权平均（percent_rank），越低越稳——一眼排序三指标">综合波动分位</span></template>
                  <template #body="{ data }">{{ pctile(data.risk_score) }}</template>
                </Column>
                <Column field="osc_pctile" style="width: 100px" sortable bodyClass="num-col" v-if="colVisible('osc_pctile')">
                  <template #header><span title="震荡分位：全史净值 zigzag（±10% 确认反转）检测的年均规则摆动轮数（频率 × 间隔均匀度）在全市场的百分位排名，越高 = 净值越像余弦一样规律地上上下下，定投止盈机会越频繁">震荡分位</span></template>
                  <template #body="{ data }">{{ pctile(data.osc_pctile) }}</template>
                </Column>
                <Column field="w10_rounds" header="止盈次数" style="width: 88px" sortable bodyClass="num-col" v-if="colVisible('w10_rounds')">
                  <template #body="{ data }">{{ dcaFeeVal(data, 'w10_rounds') ?? '—' }}</template>
                </Column>
                <Column field="gap_avg_days" header="止盈间隔" style="width: 100px" sortable bodyClass="num-col" v-if="colVisible('gap_avg_days')">
                  <template #body="{ data }">{{ dcaFeeVal(data, 'gap_avg_days') != null ? '≈' + (dcaFeeVal(data, 'gap_avg_days') / 30.44).toFixed(1) + ' 月/次' : '—' }}</template>
                </Column>
                <Column field="w10_profit_rate" header="总收益率" style="width: 98px" sortable bodyClass="num-col" v-if="colVisible('w10_profit_rate')">
                  <template #body="{ data }">
                    <span v-if="dcaFeeVal(data, 'w10_profit_rate') != null"
                      :class="dcaFeeVal(data, 'w10_profit_rate') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(dcaFeeVal(data, 'w10_profit_rate')) }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="w10_xirr" header="年化·近10年" style="width: 110px" sortable bodyClass="num-col" v-if="colVisible('w10_xirr')">
                  <template #body="{ data }">
                    <span v-if="dcaFeeVal(data, 'w10_xirr') != null"
                      :class="dcaFeeVal(data, 'w10_xirr') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(dcaFeeVal(data, 'w10_xirr')) }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="full_xirr" header="年化·全史" style="width: 105px" sortable bodyClass="num-col" v-if="colVisible('full_xirr')">
                  <template #body="{ data }">
                    <span v-if="dcaFeeVal(data, 'full_xirr') != null"
                      :class="dcaFeeVal(data, 'full_xirr') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(dcaFeeVal(data, 'full_xirr')) }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <!-- 「最近一次止盈后」当前轮（2026-11-19 用户需求，全史窗口
                     口径——与近10年列不可直接对比）：止盈重新开始定投至今
                     的进行中轮次收益；从未止盈显示 — -->
                <Column field="full_cur_profit_rate" style="width: 112px" sortable bodyClass="num-col" v-if="colVisible('full_cur_profit_rate')">
                  <template #header><span title="止盈后总收益率（全史口径）：最近一次止盈日全仓赎回后重新开始定投的当前轮（进行中）市值/已投入-1，随费用口径与止盈点切换；全史从未止盈的基金显示 —；与「总收益率」（近10年窗口）口径不同、不可直接对比">止盈后收益率</span></template>
                  <template #body="{ data }">
                    <span v-if="dcaFeeVal(data, 'full_cur_profit_rate') != null"
                      :class="dcaFeeVal(data, 'full_cur_profit_rate') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(dcaFeeVal(data, 'full_cur_profit_rate')) }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="full_cur_xirr" style="width: 104px" sortable bodyClass="num-col" v-if="colVisible('full_cur_xirr')">
                  <template #header><span title="止盈后年化（全史口径）：最近一次止盈日重新开始定投的当前轮 XIRR（资金加权，投入现金流=止盈后每月定投、回收=当前市值），轮次不足 90 天留空；随费用口径与止盈点切换；与「年化·近10年」口径不同、不可直接对比">止盈后年化</span></template>
                  <template #body="{ data }">
                    <span v-if="dcaFeeVal(data, 'full_cur_xirr') != null"
                      :class="dcaFeeVal(data, 'full_cur_xirr') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(dcaFeeVal(data, 'full_cur_xirr')) }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column header="回测区间" style="width: 205px" v-if="colVisible('w10_range')">
                  <template #body="{ data }">
                    <span class="mono">{{ data.w10_start || '—' }} ~ {{ data.w10_end || '—' }}</span>
                  </template>
                </Column>
                <Column header="操作" style="width: 72px">
                  <template #body="{ data }">
                    <button class="detail-btn" @click.stop="openDcaRounds(data)">详情</button>
                  </template>
                </Column>
              </DataTable>
            </div>
            <p class="note">口径：每月首个净值日买入（定投方式可切：等额=每期固定金额；等份额=每期固定份额、首期 1000 元折算、现金随净值波动），任一净值日持仓收益达止盈线即全仓赎回并重开新一轮（分红再投）；排行/统计/散点/对比/详情共用同一费用口径，页面顶部「费用口径」切换（不含费 / 含费·申购一折 / 含费·申购名义，赎回费各口径一致按持有天数阶梯收取）；排行/统计/散点/对比池共用同一基金类型口径，页面顶部「基金类型」切换（默认仅权益类：股票型/偏股·灵活·平衡混合/股票指数/海外权益共 8 类，全市场可切回，债基·FOF·货币·偏债不在权益类内；对比卡池在此基础上加 QDII-商品/商品） · 统计卡为当前类型与在市口径全集分位，不随搜索与区间筛选变化 · 新基金（回测窗口 &lt;90 天）年化留空 · 「当前回撤」= 复权净值距历史最高的跌幅（日更顺带刷新），非在市基金无此数据；「甜蜜点」结论来自 2015~2026 权益基金 98 万起点历史模拟（-25~-30% 回撤起定投首轮止盈中位 582 天、成功率 63.2%、年化 32.0%，20% 止盈线含费口径） · 波动指标列（年化/下行波动率、最大回撤、夏普 + 波动率分位）为基金净值快照，全史口径（全部历史复权净值，2026-11-18 起由近1年改全史；复权日增长率、√252 年化、无风险利率 0，fund_risk_metrics 日更顺带刷新，样本不足 20 个收益为 —），与回测口径独立、不随费用/止盈点切换 · 「综合波动分位」= 全史年化波动率/下行波动率/最大回撤三项在全市场同类基金中的百分位排名等权平均（percent_rank，0~100%，越低越稳），日更顺带重算（三指标缺任一按可得项平均，全缺为 —） · 「震荡分位」= 全史净值 zigzag（±10% 确认反转）年均规则摆动轮数（频率 × 间隔均匀度）的横截面百分位，越高 = 净值越规律地上上下下 · 「止盈后收益率/止盈后年化」= 最近一次止盈日全仓赎回后重新开始定投的当前轮（进行中）总收益率与 XIRR（全史窗口口径，与「总收益率/年化·近10年」列不可直接对比；随费用口径与止盈点切换，轮次不足 90 天年化留空，全史从未止盈显示 —，日更回测顺带刷新）· 数据为离线回测快照，重跑 scripts/fund_dca_backtest.py 并经 app.dca_store 导入后刷新 · 点行或「详情」查看每轮止盈明细 · 顶部总开关四组（定投方式/止盈点/基金类型/费用口径，2026-11-23 置顶）切换后排行/统计卡/对比卡/散点分箱/明细全部跟随（起点条件与熊市断缴两实验卡为等额口径固定快照） · 历史统计不构成任何操作建议</p>


            <!-- ===== 年限 × 规模 三维散点（每基金一点） + 分箱统计 ===== -->
            <div class="card" style="margin-top: 16px; padding: 14px 16px 18px">
              <div class="toolbar" style="margin: 0 0 6px">
                <div>
                  <h2 class="h1" style="font-size: 18px">年限 × 规模 × 年化 三维散点<span class="dc-hint" style="margin-left: 6px">{{ dcaModeLabel }} · 止盈 {{ dcaTpLabel(dcaTp) }}（随顶部总开关切换）</span></h2>
                  <p class="sub">每只在市基金一个点，落在自己的（运作年限，规模，年化）坐标上，不做任何分组重复计数 · 规模为对数轴 · 悬停点查看基金明细 · 统计截至 {{ dcaScatter?.ref_date || '—' }}</p>
                </div>
                <div class="toolbar-right">
                  <span v-if="dcaFeeLoading" class="sub" style="margin-right: 8px">重算中…</span>
                  <!-- 类型/费用切换已上移页面顶部总开关行（三轮需求），此处只留加载态 -->
                </div>
              </div>
              <div v-if="dcaScatter?.status === 'down'" class="browse-empty">
                散点分析不可用：{{ dcaScatter.detail || '数据库连接失败' }}
              </div>
              <template v-else-if="dcaScatter?.points?.length">
                <div ref="dcaScatterChartEl" class="dca-scatter-chart" style="width: 100%; height: 620px"></div>

                <!-- 结论（左） + 分箱矩阵（右）：宽屏并排、窄屏堆叠 -->
                <div class="dca-panel">
                  <div class="dca-conclusion" v-if="dcaScatterConclusion">
                    <div class="dc-head">
                      <b>结论</b>
                      <span class="dc-badge">{{ dcaModeLabel }} · {{ dcaFeeLabel }} · {{ dcaEquity ? '仅权益类' : '全市场' }} · 在市 {{ fmt(dcaScatter.points.length) }} 只 · 每基金只计一次</span>
                    </div>
                    <div class="dc-grid">
                      <div class="dc-item">
                        <div class="dc-label">年限维度</div>
                        <div class="dc-value">&lt;1 年 <b :class="dcaScatterConclusion.ageMean[0] >= 0 ? 'pos' : 'neg'">{{ pctp(dcaScatterConclusion.ageMean[0]) }}</b><span class="dc-arrow">→</span>≥10 年 <b :class="dcaScatterConclusion.ageMean[4] >= 0 ? 'pos' : 'neg'">{{ pctp(dcaScatterConclusion.ageMean[4]) }}</b></div>
                        <div class="dc-note">年限越长、定投止盈的年化越稳越高</div>
                      </div>
                      <div class="dc-item">
                        <div class="dc-label">规模维度</div>
                        <div class="dc-value"><template v-for="(sb, i) in dcaScatter.scale_bands" :key="sb"><span v-if="i" class="dc-arrow">→</span><b :class="dcaScatterConclusion.scaleMean[i] >= 0 ? 'pos' : 'neg'">{{ pctp(dcaScatterConclusion.scaleMean[i]) }}</b></template></div>
                        <div class="dc-note">&lt;1 亿 → ≥100 亿 · 影响弱于年限，差距不大且非单调</div>
                      </div>
                      <div class="dc-item dc-best" v-if="dcaScatterConclusion.best">
                        <div class="dc-label">★ 组合最优格<span class="dc-hint">样本 ≥300</span></div>
                        <div class="dc-value">{{ dcaScatter.age_bands[dcaScatterConclusion.best.ageIdx] }} × {{ dcaScatter.scale_bands[dcaScatterConclusion.best.scaleIdx] }}</div>
                        <div class="dc-note">平均年化 <b :class="dcaScatterConclusion.best.cell.mean_xirr >= 0 ? 'pos' : 'neg'">{{ pctp(dcaScatterConclusion.best.cell.mean_xirr) }}</b> · {{ fmt(dcaScatterConclusion.best.cell.count) }} 只 · 中位 {{ pctp(dcaScatterConclusion.best.cell.median_xirr) }} · 年化为正 {{ pct0(dcaScatterConclusion.best.cell.positive_share) }}</div>
                      </div>
                    </div>
                  </div>
                  <div class="dca-matrix-wrap">
                    <div class="dc-head">
                      <b>年限段 × 规模段 分箱统计（均值）</b>
                      <span class="dc-badge">格内 = 基金数 / 平均年化 · 点击格子查看基金清单</span>
                    </div>
                    <table class="dca-matrix">
                      <tr>
                        <th>规模 \ 年限</th>
                        <th v-for="ab in dcaScatter.age_bands" :key="ab">{{ ab }}</th>
                      </tr>
                      <tr v-for="(sb, si) in dcaScatter.scale_bands" :key="sb">
                        <th>{{ sb }}</th>
                        <td v-for="(ab, ai) in dcaScatter.age_bands" :key="ab"
                            :class="{ 'dca-matrix-best': dcaScatterConclusion?.best && dcaScatterConclusion.best.ageIdx === ai && dcaScatterConclusion.best.scaleIdx === si,
                                      'dca-cell-link': dcaScatter.matrix[ai][si].count }"
                            title="点击查看格内基金清单"
                            @click="openDcaCellFunds(ai, si)">
                          <template v-if="dcaScatter.matrix[ai][si].count">
                            {{ dcaScatter.matrix[ai][si].count }} 只<br/>
                            <span :class="dcaScatter.matrix[ai][si].mean_xirr >= 0 ? 'chg-up' : 'chg-down'">{{ pctp(dcaScatter.matrix[ai][si].mean_xirr) }}</span>
                          </template>
                          <template v-else>—</template>
                        </td>
                      </tr>
                    </table>
                    <div class="dc-head" style="margin-top: 16px">
                      <b>年限段 × 规模段 分箱统计（中位）</b>
                      <span class="dc-badge">格内 = 基金数 / 中位年化 · 中位不受极端值拉扯 · 点击查看清单</span>
                    </div>
                    <table class="dca-matrix dca-median">
                      <tr>
                        <th>规模 \ 年限</th>
                        <th v-for="ab in dcaScatter.age_bands" :key="ab">{{ ab }}</th>
                      </tr>
                      <tr v-for="(sb, si) in dcaScatter.scale_bands" :key="sb">
                        <th>{{ sb }}</th>
                        <td v-for="(ab, ai) in dcaScatter.age_bands" :key="ab"
                            :class="{ 'dca-matrix-best': dcaMedianBest && dcaMedianBest.ageIdx === ai && dcaMedianBest.scaleIdx === si,
                                      'dca-cell-link': dcaScatter.matrix[ai][si].count }"
                            title="点击查看格内基金清单"
                            @click="openDcaCellFunds(ai, si)">
                          <template v-if="dcaScatter.matrix[ai][si].count">
                            {{ dcaScatter.matrix[ai][si].count }} 只<br/>
                            <span :class="dcaScatter.matrix[ai][si].median_xirr >= 0 ? 'chg-up' : 'chg-down'">{{ pctp(dcaScatter.matrix[ai][si].median_xirr) }}</span>
                          </template>
                          <template v-else>—</template>
                        </td>
                      </tr>
                    </table>
                  </div>
                </div>
                <p class="note" style="margin-top: 8px">口径：在市且年化非空的基金 · 年化为定投+20%止盈的 XIRR，可切换 不含费 / 含费·申购一折 / 含费·申购名义（申购费外扣折份额，赎回费按每笔持有天数阶梯：权益类 7 天内 1.5%、7 天~1 年 0.5%、1~2 年 0.25%、之后免，债券与 C 类 7 天后免；管理/托管/销售服务费已在净值内不重复计） · 超出轴范围（年化 &lt;-100% / &gt;300%、年限 &gt;25 年）的点截断在边缘 · 结论由分箱统计自动生成</p>
              </template>
              <div v-else class="browse-empty">散点数据加载中…</div>
            </div>
            <!-- ===== 每次止盈明细弹窗（点排行行或「详情」按钮触发） ===== -->
            <!-- 尺寸 100vw-10vh × 90vh（2026-11-18 用户反馈四边距应统一）：
                 PrimeVue 弹窗在遮罩内居中，高度上限 90%（=90vh）→ 上下边距 5vh，
                 宽度跟随取 100vw-10vh 使左右边距同为 5vh——四边严格等距；
                 高度钉死 90vh 后内容不足部分由净值曲线区弹性吸收（.dca-rounds-body），
                 1920×1080 下由固定 1000×902（左右各空 460px）变为 1812×972 铺满 -->
            <Dialog v-model:visible="dcaRoundsVisible" modal
              :header="dcaRoundsFund ? `${dcaRoundsFund.code} ${dcaRoundsFund.name} · 每次止盈明细（${dcaModeLabel}${dcaRoundsFund.age_years != null ? ' · 运作 ' + dcaRoundsFund.age_years + ' 年' : ''}）` : '每次止盈明细'"
              :style="{ width: 'calc(100vw - 10vh)', height: '90vh' }" @hide="disposeDcaRoundsChart">
              <div class="dca-rounds-body">
              <!-- 两段按钮组同行排布（body 为 flex column 会块化子元素，内包一层恢复行内流）；
                   定投方式跟随页面级 tab（无弹窗局部方式切换），标题展示当前口径 -->
              <div class="dca-rounds-segrow">
              <div class="src-seg" v-if="!STATIC_MODE" style="margin: 0 0 10px"
                   title="切换止盈线：下方统计与轮次表跟随；曲线同时叠加全部档位止盈点（颜色=档位，实心大三角=当前查看档位）；不影响页面排行阈值">
                <button v-for="t in DCA_TP_OPTIONS" :key="t"
                        class="src-seg-btn"
                        :class="{ active: dcaRoundsTp === t }"
                        @click="onDcaRoundsTpChange(t)"
                        :data-modal-tp="t">{{ dcaTpLabel(t) }}</button>
              </div>
              <div class="src-seg" v-if="!STATIC_MODE" style="margin: 0 0 10px"
                   title="切换费率口径：轮次表/费用字段/总收益率与年化跟随（份额随申购费折损，轮次可能位移）；不影响页面费率开关">
                <button v-for="o in DCA_FEE_OPTIONS" :key="o.key"
                        class="src-seg-btn"
                        :class="{ active: dcaRoundsFee === o.key }"
                        @click="onDcaRoundsFeeChange(o.key)"
                        :data-modal-fee="o.key">{{ o.label }}</button>
              </div>
              </div>
              <div class="dc-grid" style="grid-template-columns: repeat(3, 1fr); margin: 0 0 12px">
                <div class="dc-item">
                  <div class="dc-label">已止盈轮次<span class="dc-hint">已了结</span></div>
                  <div class="dc-value">{{ dcaRounds.totals?.rounds || 0 }} 轮</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label">累计投入（已了结轮次）</div>
                  <div class="dc-value">{{ fmt(dcaRounds.totals?.invested) }} 元</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label">累计净收益（扣费后）</div>
                  <div class="dc-value"><b :class="(dcaRounds.totals?.net_profit || 0) >= 0 ? 'pos' : 'neg'">{{ fmt(dcaRounds.totals?.net_profit) }}</b> 元</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label">费用合计（申购+赎回）</div>
                  <div class="dc-value">{{ fmt(dcaRounds.totals?.sub_fee) }} + {{ fmt(dcaRounds.totals?.redeem_fee) }} 元</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label">总收益率·近10年<span class="dc-hint">{{ dcaRoundsFeeLabel }}</span></div>
                  <div class="dc-value"><span v-if="summaryFeeVal(dcaRoundsSummaryRow, 'w10_profit_rate') != null"
                    :class="summaryFeeVal(dcaRoundsSummaryRow, 'w10_profit_rate') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(summaryFeeVal(dcaRoundsSummaryRow, 'w10_profit_rate')) }}</span><span v-else>—</span></div>
                </div>
                <div class="dc-item">
                  <div class="dc-label">年化 XIRR·近10年<span class="dc-hint">{{ dcaRoundsFeeLabel }}</span></div>
                  <div class="dc-value"><span v-if="summaryFeeVal(dcaRoundsSummaryRow, 'w10_xirr') != null"
                    :class="summaryFeeVal(dcaRoundsSummaryRow, 'w10_xirr') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(summaryFeeVal(dcaRoundsSummaryRow, 'w10_xirr')) }}</span><span v-else>—</span></div>
                </div>
              </div>
              <!-- 「最近一次止盈后」当前轮（全史窗口，2026-11-19）：止盈重新
                   开始定投至今的进行中轮次；随弹窗止盈点/费率切换取值 -->
              <div class="dc-grid" style="grid-template-columns: repeat(4, 1fr); margin: 0 0 12px">
                <div class="dc-item">
                  <div class="dc-label" title="全史回测中最近一次止盈的日期（该日全仓赎回并重新开始定投）；随费用口径可能位移；从未止盈显示 —">上次止盈日<span class="dc-hint">全史</span></div>
                  <div class="dc-value mono">{{ summaryFeeVal(dcaRoundsSummaryRow, 'full_last_tp') || '—' }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="上次止盈日以来重新投入的定投金额（每月首个净值日投入，与费用口径无关）">本轮已投入<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_invested') != null ? fmt(summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_invested')) + ' 元' : '—' }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="止盈后总收益率（全史）：当前轮（进行中）市值/已投入-1，随费率口径切换；与上方「总收益率·近10年」口径不同、不可直接对比；从未止盈显示 —">止盈后收益率<span class="dc-hint">全史·{{ dcaRoundsFeeLabel }}</span></div>
                  <div class="dc-value"><span v-if="summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_profit_rate') != null"
                    :class="summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_profit_rate') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_profit_rate')) }}</span><span v-else>—</span></div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="止盈后年化（全史）：当前轮 XIRR（资金加权：止盈后每月定投为投入、当前市值为回收），轮次不足 90 天留空；随费率口径切换；与上方「年化·近10年」口径不同、不可直接对比">止盈后年化<span class="dc-hint">全史·{{ dcaRoundsFeeLabel }}</span></div>
                  <div class="dc-value"><span v-if="summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_xirr') != null"
                    :class="summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_xirr') >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(summaryFeeVal(dcaRoundsSummaryRow, 'full_cur_xirr')) }}</span><span v-else>—</span></div>
                </div>
              </div>
              <!-- 熊市断缴对照（2026-11-19 实验产品化：这只基金上四条平行时间线
                   的期末钱包与年化；全史·gross 无费·止盈 20% 固定口径，不随弹窗
                   tp/fee 切换；未参与实验的基金 fund=null 整块隐藏） -->
              <template v-if="dcaRoundsPause">
                <div class="dc-grid" data-pause-dialog style="grid-template-columns: repeat(4, 1fr); margin: 0 0 12px">
                  <div class="dc-item" v-for="a in PAUSE_ARMS" :key="a.key" :title="a.tip">
                    <div class="dc-label">{{ a.label }}<span class="dc-hint">断缴实验</span></div>
                    <div class="dc-value">{{ pauseMoney(dcaRoundsPause['tp_' + a.key + '_wallet']) }}</div>
                    <div class="dc-label" style="font-weight: 400">投入 {{ fmt(dcaRoundsPause['tp_' + a.key + '_out']) }} 元 · 年化
                      <span :class="(dcaRoundsPause['tp_' + a.key + '_xirr'] ?? 0) >= 0 ? 'chg-up' : 'chg-down'">{{ pauseXirr(dcaRoundsPause['tp_' + a.key + '_xirr']) }}</span>
                    </div>
                  </div>
                </div>
                <p class="note" style="margin: -6px 0 12px">熊市断缴对照：熊市态 = 回撤 ≤-20% 进入、收窄回 &gt;-10% 恢复；本基金断缴 {{ dcaRoundsPause.pause_months }} / 共 {{ dcaRoundsPause.months }} 月（{{ dcaRoundsPause.episodes }} 次）· 钱包 = 止盈回款 + 期末市值 + 囤现金 · 无费·止盈 20% 固定口径（不随上方止盈点/费率切换）· 市场级结论见「熊市断缴实验」区块</p>
              </template>
              <!-- 风险/波动指标（fund_risk_metrics 日更快照，随 navs 响应附带） -->
              <div v-if="dcaRoundsRisk" class="dc-grid"
                style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin: 0 0 12px">
                <div class="dc-item">
                  <div class="dc-label" title="全史年化波动率：全部历史净值日收益率标准差 × √252，复权口径（2026-11-18 起由近1年改全史）">年化波动率<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ volpct(dcaRoundsRisk.vol_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="全部历史中只统计下跌日收益率的标准差 × √252（真实痛苦的方向）">下行波动率<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ volpct(dcaRoundsRisk.vol_down_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="全部历史复权净值峰谷最大回撤">最大回撤<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ mddneg(dcaRoundsRisk.mdd_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="日收益均值/标准差 × √252，无风险利率 0——每单位波动换回的收益">夏普<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ sharpe2(dcaRoundsRisk.sharpe_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="当前 60 日滚动年化波动率及其在该基金自身全史滚动序列中的百分位（越低=比历史更平静）">当前滚动波动率<span class="dc-hint">60日</span></div>
                  <div class="dc-value">{{ volpct(dcaRoundsRisk.roll_vol) }} <span class="dc-hint">分位 {{ pctile(dcaRoundsRisk.vol_pctile) }}</span></div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="综合波动分位：全史年化波动率/下行波动率/最大回撤三项在全市场同类基金中的百分位排名等权平均（percent_rank），越低越稳——一眼比较三指标">综合波动分位<span class="dc-hint">全市场</span></div>
                  <div class="dc-value">{{ pctile(dcaRoundsRisk.risk_score) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="震荡分位：全史净值 zigzag（±10% 确认反转）检测的年均规则摆动轮数（频率 × 间隔均匀度）在全市场的百分位排名，越高 = 净值越像余弦一样规律地上上下下">震荡分位<span class="dc-hint">全市场</span></div>
                  <div class="dc-value">{{ pctile(dcaRoundsRisk.osc_pctile) }} <span class="dc-hint">年均 {{ oscyr(dcaRoundsRisk.osc_per_year) }}</span></div>
                </div>
              </div>
              <div v-if="dcaRoundsRisk" class="risk-table-wrap">
                <table class="risk-table">
                  <thead>
                    <tr><th>窗口</th><th>年化波动率</th><th>下行波动率</th><th>最大回撤</th><th>夏普</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="w in FUND_RISK_WINDOWS" :key="w.key">
                      <td class="mono">{{ w.label }}</td>
                      <td>{{ volpct(dcaRoundsRisk['vol_' + w.key]) }}</td>
                      <td>{{ volpct(dcaRoundsRisk['vol_down_' + w.key]) }}</td>
                      <td>{{ mddneg(dcaRoundsRisk['mdd_' + w.key]) }}</td>
                      <td>{{ sharpe2(dcaRoundsRisk['sharpe_' + w.key]) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p v-if="dcaRoundsRisk" class="note" style="margin: 0 0 8px">波动指标口径：复权日增长率（分红再投）· 年化系数 √252、无风险利率 0 · 指标卡为全史口径（全部历史净值，2026-11-18 起由近1年改全史），下方对照表可看近1/3/5 年窗口 · 快照截至 {{ dcaRoundsRisk.as_of || '—' }}，日更顺带生成 · 与回测指标独立（回测口径为策略收益，此为基金净值波动）</p>
              <p v-else class="note" style="margin: 0 0 8px">波动指标待生成：日更任务（回测）跑过一轮后在此展示（静态快照模式不含）</p>
              <div v-show="dcaRoundsNavs.length" ref="dcaRoundsChartEl" class="dca-rounds-chart"></div>
              <p class="note" style="margin: 0 0 8px"
                v-if="dcaRounds.rounds?.length && dcaRoundsNavs.length">
                ▲ = 每轮止盈卖点（颜色对应止盈线档位，实心大三角=当前查看档位）· 曲线为复权净值（分红再投，窗口起点=1，与回测同口径）
              </p>
              <p class="note" style="margin: 0 0 8px" v-else-if="dcaRoundsNavs.length">
                曲线为复权净值（分红再投，窗口起点=1，与回测同口径）· 该基金在该止盈线下尚未止盈，仍可观察其净值走势与其他档位止盈点
              </p>
              <div v-if="dcaRounds.status === 'down'" class="browse-empty">
                明细不可用：{{ dcaRounds.detail || '数据库连接失败' }}
              </div>
              <div v-else-if="dcaRoundsRows.length" style="overflow-x: auto">
                <table class="dca-matrix" style="width: 100%; min-width: 700px">
                  <tr>
                    <th>轮次</th><th>止盈日</th><th>止盈净值<br/>(复权)</th><th>实际收益率</th><th>距上次止盈</th><th>本轮持有</th>
                    <th>本轮投入(元)</th><th>申购费(元)</th><th>赎回费(元)</th><th>本轮净收益(元)</th><th>累计净收益(元)</th>
                  </tr>
                  <tr v-for="r in dcaRoundsRows" :key="r.seq">
                    <td>第 {{ r.seq }} 次</td>
                    <td class="mono">{{ r.tp_date || '—' }}</td>
                    <td class="mono">{{ r.tp_nav != null ? r.tp_nav.toFixed(4) : '—' }}</td>
                    <td><span :class="r.profit >= 0 ? 'chg-up' : 'chg-down'">{{ pctp(r.profit) }}</span></td>
                    <td>{{ gapLabel(r.gap_days) }}</td>
                    <td>{{ r.round_days != null ? r.round_days + ' 天' : '—' }}</td>
                    <td>{{ r.invested != null ? fmt(r.invested) : '—' }}</td>
                    <td>{{ r.sub_fee != null ? fmt(r.sub_fee) : '—' }}</td>
                    <td>{{ r.redeem_fee != null ? fmt(r.redeem_fee) : '—' }}</td>
                    <td><span :class="(r.net_profit || 0) >= 0 ? 'chg-up' : 'chg-down'">{{ r.net_profit != null ? fmt(r.net_profit) : '—' }}</span></td>
                    <td><span :class="(r.cum_net_profit || 0) >= 0 ? 'chg-up' : 'chg-down'">{{ fmt(r.cum_net_profit) }}</span></td>
                  </tr>
                </table>
              </div>
              <div v-else-if="dcaRounds.status === 'loading'" class="browse-empty">加载中…</div>
              <div v-else class="browse-empty">该基金在统计窗口内没有止盈记录（从未达到 +{{ dcaTpLabel(dcaRoundsTp) }}）</div>
              </div>
            </Dialog>

            <!-- ===== 分箱格点击 → 格内基金清单 ===== -->
            <Dialog v-model:visible="dcaCellFundsVisible" modal
              :header="dcaCellFunds.title ? `${dcaCellFunds.title} · 基金清单（${dcaCellFunds.rows.length} 只）` : '基金清单'"
              :style="{ width: '880px' }">
              <p class="sub" style="margin: 0 0 8px">
                落在该年限段 × 规模段格内的全部在市基金 · 年化为当前口径（{{ dcaFeeLabel }}）· 按年化降序 · 点「明细」查看该基金每轮止盈
              </p>
              <div style="overflow: auto; max-height: 560px">
                <table class="dca-matrix" style="width: 100%">
                  <tr><th>代码</th><th>名称</th><th>类型</th><th>运作年限</th><th>规模(亿)</th><th>年化</th><th>操作</th></tr>
                  <tr v-for="f in dcaCellFunds.rows" :key="f.code">
                    <td class="mono">{{ f.code }}</td>
                    <td style="text-align: left">{{ f.name }}</td>
                    <td>{{ f.fund_type || '—' }}</td>
                    <td>{{ f.age != null ? f.age.toFixed(1) + ' 年' : '—' }}</td>
                    <td>{{ f.scale != null ? f.scale : '—' }}</td>
                    <td><span :class="f.xirr >= 0 ? 'chg-up' : 'chg-down'">{{ pctp(f.xirr) }}</span></td>
                    <td><button class="detail-btn" @click="openDcaRounds({ code: f.code, name: f.name })">明细</button></td>
                  </tr>
                </table>
              </div>
            </Dialog>
          </template>

          <!-- ===== 519 vs 924 行情对比（数据子页：/api/market/519-924；
               T 日收盘基线、交易日序偏移对齐 T-280~T+1040 复刻中信研报图；
               上证指数同源（原图 wind全A 免费渠道不可得），指数日更任务
               每日 17:30 供数，924 线自动延长） ===== -->
          <template v-else-if="activeNav === 'market'">
            <div class="toolbar">
              <div>
                <h2 class="h1">519 vs 924：两次政策行情对齐复盘</h2>
                <p class="sub">T 日收盘为基线、按交易日序偏移对齐（T-280 ~ T+1040）· 上证指数同源（研报原图为 wind全A，免费渠道不可得，口径以本页为准）· 东财日线 ·「指数日更」任务每日 17:30 更新，924 线自动延长</p>
              </div>
              <div class="toolbar-right">
                <button class="btn" data-market-refresh
                  :disabled="marketCompare.status === 'loading'" @click="loadMarketCompare">
                  {{ marketCompare.status === 'loading' ? '加载中…' : '刷新' }}
                </button>
              </div>
            </div>
            <div class="card" v-if="marketCompare.status === 'loading'"
              style="padding: 28px 16px; text-align: center">
              <p class="note" style="margin: 0">正在加载行情对比数据…</p>
            </div>
            <div class="card" v-else-if="marketCompare.status === 'down'" style="padding: 28px 16px">
              <div class="browse-empty">行情对比数据不可用：{{ marketCompare.detail || '指数表暂无数据' }}</div>
            </div>
            <template v-else-if="marketCompare.body && marketKpis">
              <div class="learn-metrics" data-market-kpis style="margin: 12px 0">
                <div class="card lm"><div class="lm-k">924 已走<br><span class="dc-hint">截至 {{ marketKpis.prog.date }}</span></div><div class="lm-v">T+{{ marketKpis.prog.n }} <span style="font-size: 13px">交易日</span></div></div>
                <div class="card lm"><div class="lm-k">924 当前累计<br><span class="dc-hint">T 日收盘 = 基线</span></div><div class="lm-v"><span :class="marketKpis.prog.ret >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(marketKpis.prog.ret) }}</span></div></div>
                <div class="card lm"><div class="lm-k">519 同期（T+{{ marketKpis.prog.n }}）<br><span class="dc-hint">{{ marketKpis.same?.d1 || '—' }}</span></div><div class="lm-v"><span :class="(marketKpis.same?.r1 ?? 0) >= 0 ? 'chg-up' : 'chg-down'">{{ pct1(marketKpis.same?.r1) }}</span></div></div>
                <div class="card lm"><div class="lm-k">区间峰值<br><span class="dc-hint">5·19 T+{{ marketKpis.peak1?.n }} ｜ 9·24 T+{{ marketKpis.peak2?.n }}</span></div><div class="lm-v"><span class="chg-up">{{ pct1(marketKpis.peak1?.r) }}</span> <span style="font-size: 13px">vs</span> <span class="chg-up">{{ pct1(marketKpis.peak2?.r) }}</span></div></div>
              </div>
              <div class="card" style="padding: 12px 16px 8px">
                <div ref="marketChartEl" data-market-chart style="width: 100%; height: 560px"></div>
              </div>
              <p class="note" style="margin: 8px 0 0">口径：累计涨幅 = 各 T 日后第 n 个交易日收盘 ÷ T 日收盘 − 1（T 日 = 0%）；横轴为交易日序偏移，两线各自交易日历独立（1999 与 2024 日历不同）；<b>双 y 轴读数：左轴（红）= 5·19、右轴（蓝）= 9·24，各自量纲贴近满幅、0% 刻度对齐同高（研报原图同款）</b>，两线的纵向形态比例不可跨轴直接比较，读数以各自轴为准；细线为历史上其他 10 次 5 日爆发 ≥12% 的脉冲行情（T 后 120 个交易日，右轴读数，事件标注为人工知识；点图例可显隐）· 虚线为 T 日与 0% 基准 · 924 线止于最近已入库交易日，随「指数日更」自动延长 · 历史复盘仅供参考，不构成投资建议</p>
            </template>
            <div class="card" v-else style="padding: 28px 16px">
              <div class="browse-empty">暂无数据</div>
            </div>
          </template>

          <!-- ===== 个股历史行情详情（数据页行点击进入） ===== -->
          <template v-else-if="activeNav === 'stock' && stock">
            <div class="toolbar">
              <div>
                <h2 class="h1"><span class="mono">{{ stock.code }}</span> {{ stock.name }}</h2>
                <p class="sub">个股历史行情 · 日K · {{ stockAdjust === 'qfq' ? '前复权口径（最新价=真实价）' : '不复权口径' }} · 图为近 250 个交易日</p>
              </div>
              <div class="toolbar-right">
                <div class="src-seg">
                  <button v-for="s in TASK_SOURCES" :key="s.v" class="src-seg-btn"
                    :class="{ active: stock.source === s.v }"
                    @click="setStockSource(s.v)">{{ s.label }}</button>
                </div>
                <div class="src-seg">
                  <button class="src-seg-btn" :class="{ active: stockAdjust === '' }"
                    @click="stockAdjust = ''">不复权</button>
                  <button class="src-seg-btn" :class="{ active: stockAdjust === 'qfq' }"
                    @click="stockAdjust = 'qfq'">前复权</button>
                </div>
                <button class="btn" @click="openLearnWith(stock.code)">用该股教学回测</button>
                <button class="btn" @click="backToList">← 返回列表</button>
              </div>
            </div>

            <!-- K 线图（近 250 根，蜡烛 + 成交量副图，可缩放平移） -->
            <div class="card chart-card">
              <div v-if="chartBars.status === 'down'" class="chart-note">
                行情数据不可用：{{ chartBars.detail || '数据库连接失败' }}
              </div>
              <div v-else-if="!chartBars.bars.length" class="chart-note">
                {{ chartBars.status === 'loading' ? '行情加载中…'
                   : `${sourceLabel(stock.source)}源暂无该股K线数据（可切换来源）` }}
              </div>
              <div v-show="chartBars.status === 'up' && chartBars.bars.length"
                ref="chartEl" class="kchart"></div>
            </div>

            <!-- 全字段明细表（独立分页，可翻更早历史） -->
            <div class="card detail-table-card">
              <DataTable :value="detailRows.rows" lazy paginator :rows="DETAIL_PAGE_SIZE"
                :first="detailPage * DETAIL_PAGE_SIZE"
                :totalRecords="detailTotal" :loading="detailRows.status === 'loading'"
                dataKey="date" stripedRows size="small" @page="onDetailPage">
                <template #empty>
                  <div class="browse-empty">
                    {{ detailRows.status === 'down' ? '数据库不可达：' + (detailRows.detail || '')
                      : detailRows.status === 'loading' ? '加载中…' : '该源暂无该股行情数据' }}
                  </div>
                </template>
                <Column field="date" header="日期" style="width: 110px">
                  <template #body="{ data }"><span class="mono">{{ data.date }}</span></template>
                </Column>
                <Column field="open" header="开盘" style="width: 80px" bodyClass="num-col" />
                <Column field="close" header="收盘" style="width: 80px" bodyClass="num-col" />
                <Column field="high" header="最高" style="width: 80px" bodyClass="num-col" />
                <Column field="low" header="最低" style="width: 80px" bodyClass="num-col" />
                <Column field="change" header="涨跌额" style="width: 84px">
                  <template #body="{ data }">
                    <span v-if="data.change != null"
                      :class="data.change >= 0 ? 'chg-up' : 'chg-down'">
                      {{ (data.change >= 0 ? '+' : '') + data.change }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="pct_chg" header="涨跌幅" style="width: 90px">
                  <template #body="{ data }">
                    <span v-if="data.pct_chg != null"
                      :class="data.pct_chg >= 0 ? 'chg-up' : 'chg-down'">
                      {{ (data.pct_chg >= 0 ? '+' : '') + data.pct_chg + '%' }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="amplitude" header="振幅" style="width: 80px">
                  <template #body="{ data }">{{ data.amplitude != null ? data.amplitude + '%' : '—' }}</template>
                </Column>
                <Column field="volume" header="成交量(手)" style="width: 110px">
                  <template #body="{ data }">{{ data.volume != null ? fmt(data.volume) : '—' }}</template>
                </Column>
                <Column field="amount" header="成交额" style="width: 100px">
                  <template #body="{ data }">{{ fmtAmount(data.amount) }}</template>
                </Column>
                <Column field="turnover" header="换手率" style="width: 84px">
                  <template #body="{ data }">{{ data.turnover != null ? data.turnover + '%' : '—' }}</template>
                </Column>
                <Column v-if="stock.source === 'sina'" field="outstanding_share" header="流通股本">
                  <template #body="{ data }">{{ fmtShares(data.outstanding_share) }}</template>
                </Column>
              </DataTable>
            </div>
            <p class="note">数据来源：{{ sourceLabel(stock.source) }}源 · 主键 (code, trade_date, source)，与列表页来源 Tab 语义一致 · 点击「← 返回列表」或侧栏「数据」返回（搜索/分页状态保留）</p>
          </template>

          <!-- ===== 基金净值详情（基金列表行点击进入） ===== -->
          <template v-else-if="activeNav === 'fund' && fund">
            <div class="toolbar">
              <div>
                <h2 class="h1"><span class="mono">{{ fund.code }}</span> {{ fund.name }}</h2>
                <p class="sub">基金净值历史 · {{ fund.fund_type || '类型未知' }} · 单位净值/累计净值双线 · 图为近 {{ Math.round(FUND_CHART_POINTS / 100) / 10 }} 万个净值日内</p>
              </div>
              <div class="toolbar-right">
                <button class="btn" @click="backToFunds">← 返回列表</button>
              </div>
            </div>

            <!-- 净值走势图（单位/累计双线，可缩放平移） -->
            <div class="card chart-card">
              <div v-if="fundChart.status === 'down'" class="chart-note">
                净值数据不可用：{{ fundChart.detail || '数据库连接失败' }}
              </div>
              <div v-else-if="!fundChart.navs.length" class="chart-note">
                {{ fundChart.status === 'loading' ? '净值加载中…' : '该基金暂无净值数据' }}
              </div>
              <div v-show="fundChart.status === 'up' && fundChart.navs.length"
                ref="fundChartEl" class="kchart"></div>
            </div>

            <!-- 风险/波动指标（fund_risk_metrics 日更快照） -->
            <div v-if="fundRisk" class="card risk-card">
              <div class="dc-grid" style="grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); margin: 0 0 12px">
                <div class="dc-item">
                  <div class="dc-label" title="全史年化波动率：全部历史净值日收益率标准差 × √252，复权口径（2026-11-18 起由近1年改全史）">年化波动率<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ volpct(fundRisk.vol_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="全部历史中只统计下跌日收益率的标准差 × √252（真实痛苦的方向）">下行波动率<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ volpct(fundRisk.vol_down_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="全部历史复权净值峰谷最大回撤">最大回撤<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ mddneg(fundRisk.mdd_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="日收益均值/标准差 × √252，无风险利率 0——每单位波动换回的收益">夏普<span class="dc-hint">全史</span></div>
                  <div class="dc-value">{{ sharpe2(fundRisk.sharpe_all) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="当前 60 日滚动年化波动率及其在该基金自身全史滚动序列中的百分位（越低=比历史更平静）">当前滚动波动率<span class="dc-hint">60日</span></div>
                  <div class="dc-value">{{ volpct(fundRisk.roll_vol) }} <span class="dc-hint">分位 {{ pctile(fundRisk.vol_pctile) }}</span></div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="综合波动分位：全史年化波动率/下行波动率/最大回撤三项在全市场同类基金中的百分位排名等权平均（percent_rank），越低越稳——一眼比较三指标">综合波动分位<span class="dc-hint">全市场</span></div>
                  <div class="dc-value">{{ pctile(fundRisk.risk_score) }}</div>
                </div>
                <div class="dc-item">
                  <div class="dc-label" title="震荡分位：全史净值 zigzag（±10% 确认反转）检测的年均规则摆动轮数（频率 × 间隔均匀度）在全市场的百分位排名，越高 = 净值越像余弦一样规律地上上下下">震荡分位<span class="dc-hint">全市场</span></div>
                  <div class="dc-value">{{ pctile(fundRisk.osc_pctile) }} <span class="dc-hint">年均 {{ oscyr(fundRisk.osc_per_year) }}</span></div>
                </div>
              </div>
              <div class="risk-table-wrap">
                <table class="risk-table">
                  <thead>
                    <tr><th>窗口</th><th>年化波动率</th><th>下行波动率</th><th>最大回撤</th><th>夏普</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="w in FUND_RISK_WINDOWS" :key="w.key">
                      <td class="mono">{{ w.label }}</td>
                      <td>{{ volpct(fundRisk['vol_' + w.key]) }}</td>
                      <td>{{ volpct(fundRisk['vol_down_' + w.key]) }}</td>
                      <td>{{ mddneg(fundRisk['mdd_' + w.key]) }}</td>
                      <td>{{ sharpe2(fundRisk['sharpe_' + w.key]) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p class="note" style="margin: 10px 0 0">口径：日收益率取复权日增长率（分红再投）· 年化系数 √252、无风险利率 0 · 窗口按净值日个数（近1年=250 / 近3年=750 / 近5年=1250 / 全史=全部），样本不足的窗口为 — · 快照截至 {{ fundRisk.as_of || '—' }}，由日更任务（回测）顺带生成 · 分位 = 当前 60 日滚动波动率在该基金自身全史滚动序列中的百分位，越低越平静</p>
            </div>
            <p v-else class="note" style="margin: 0 0 10px">波动指标尚未生成：日更任务（回测）跑过一轮后在此展示该基金的波动率 / 最大回撤 / 夏普快照</p>

            <!-- 净值明细表（独立分页，可翻更早历史） -->
            <div class="card detail-table-card">
              <DataTable :value="fundRows.rows" lazy paginator :rows="FUND_DETAIL_PAGE_SIZE"
                :first="fundDetailPage * FUND_DETAIL_PAGE_SIZE"
                :totalRecords="fundDetailTotal" :loading="fundRows.status === 'loading'"
                dataKey="date" stripedRows size="small" @page="onFundDetailPage">
                <template #empty>
                  <div class="browse-empty">
                    {{ fundRows.status === 'down' ? '数据库不可达：' + (fundRows.detail || '')
                      : fundRows.status === 'loading' ? '加载中…' : '该基金暂无净值数据' }}
                  </div>
                </template>
                <Column field="date" header="日期" style="width: 110px">
                  <template #body="{ data }"><span class="mono">{{ data.date }}</span></template>
                </Column>
                <Column field="unit_nav" header="单位净值" style="width: 110px" bodyClass="num-col">
                  <template #body="{ data }">{{ data.unit_nav ?? '—' }}</template>
                </Column>
                <Column field="accum_nav" header="累计净值" style="width: 110px" bodyClass="num-col">
                  <template #body="{ data }">{{ data.accum_nav ?? '—' }}</template>
                </Column>
                <Column field="pct_chg" header="日增长率" style="width: 100px">
                  <template #body="{ data }">
                    <span v-if="data.pct_chg != null" :class="data.pct_chg >= 0 ? 'chg-up' : 'chg-down'">
                      {{ (data.pct_chg >= 0 ? '+' : '') + data.pct_chg + '%' }}</span>
                    <span v-else>—</span>
                  </template>
                </Column>
                <Column field="unit_money" header="分红" style="width: 120px">
                  <template #body="{ data }">{{ data.unit_money || '—' }}</template>
                </Column>
                <Column field="source" header="来源" style="width: 90px">
                  <template #body="{ data }"><span class="src-txt">{{ data.source || '—' }}</span></template>
                </Column>
              </DataTable>
            </div>
            <p class="note">数据来源：本地 PG fund_nav（任务源 fund）· 主键 (code, nav_date, source) · 累计净值含分红再投口径但不等于复权净值（回测口径换算见回测侧） · 点击「← 返回列表」返回（搜索/分页状态保留）</p>
          </template>

          <!-- ===== 爬虫任务管理（每日数据更新调度） ===== -->
          <template v-else-if="activeNav === 'tasks'">
            <div class="toolbar">
              <div>
                <h2 class="h1">爬虫任务</h2>
                <p class="sub">每日数据更新调度 · 到点自动执行 · 全局串行排队</p>
              </div>
              <div class="toolbar-right">
                <span v-if="serverClock" class="sub">服务器时间 {{ serverClock }}</span>
                <button class="btn btn-primary" @click="openCreate">＋ 新建任务</button>
              </div>
            </div>

            <!-- 数据不可用（PG down） -->
            <div v-if="tasksState.status === 'down'" class="error-banner">
              <span><b class="eb-title">任务数据不可用：</b>{{ tasksState.detail || '数据库连接失败' }} · 恢复后自动重试</span>
            </div>

            <!-- 空态 -->
            <div v-else-if="!tasksState.tasks.length" class="card task-empty">
              <div class="state">暂无爬虫任务</div>
              <div class="detail">点击右上角「新建任务」创建每日数据更新任务（推荐：新浪源 · 增量 · 每日 18:00）</div>
            </div>

            <!-- 任务卡片列表 -->
            <div v-else class="task-list">
              <div v-for="t in tasksState.tasks" :key="t.id" class="card task-card">
                <div class="task-head">
                  <div class="task-title">
                    <h3 class="task-name">{{ t.name }}</h3>
                    <div class="task-tags">
                      <span class="chip" :class="t.source === 'eastmoney' ? 'ef' : 'sina'">{{ sourceLabel(t.source) }}源</span>
                      <span class="chip mode">{{ modeLabel(t.mode) }}</span>
                      <span class="chip time">{{ t.schedule_time ? '每日 ' + t.schedule_time : '手动' }}</span>
                      <span class="phase-badge"><span class="pdot" :class="phaseInfo(t)[1]"></span>{{ phaseInfo(t)[0] }}</span>
                    </div>
                  </div>
                  <label class="switch" :title="t.enabled ? '点击禁用' : '点击启用'">
                    <input type="checkbox" :checked="t.enabled" @change="toggleTask(t)">
                    <span class="slider"></span>
                  </label>
                </div>

                <!-- 运行中：实时进度（3s 轮询刷新） -->
                <div v-if="t.phase === 'running'" class="task-progress">
                  <div class="bar"><div class="bar-fill"
                    :style="{ width: t.progress ? Math.min(100, Math.round(t.progress.done / Math.max(1, t.progress.total) * 100)) + '%' : '0%' }"></div></div>
                  <div class="detail">{{ t.progress ? `已完成 ${fmt(t.progress.done)} / ${fmt(t.progress.total)} 只` : '准备中（拉取股票清单）…' }}</div>
                </div>

                <div class="task-meta">
                  <span>下次执行：<b>{{ nextRunText(t) }}</b></span>
                  <span v-if="t.last_run">上次：{{ taskLastRunText(t.last_run) }}</span>
                </div>
                <div v-if="t.last_error" class="task-lasterr"
                  :title="'后端日志关键词：[task] #' + t.id">上次失败：{{ t.last_error }}</div>
                <div v-if="riskNote(t)" class="task-risk">{{ riskNote(t) }}</div>

                <div class="task-actions">
                  <button v-if="t.phase === 'running'" class="btn" @click="actTask(t, 'stop')">停止</button>
                  <button v-else-if="t.phase === 'queued'" class="btn" @click="actTask(t, 'cancel')">取消排队</button>
                  <button v-else class="btn" @click="actTask(t, 'run')">立即执行</button>
                  <button class="btn" @click="toggleRuns(t)">{{ expandedRuns[t.id] ? '收起历史' : '历史' }}</button>
                  <button class="btn" @click="openEdit(t)">编辑</button>
                  <button class="btn btn-danger" @click="deleteTask(t)">删除</button>
                </div>

                <!-- 运行历史 -->
                <div v-if="expandedRuns[t.id]" class="task-runs">
                  <div v-if="!(runsMap[t.id] || []).length" class="detail">暂无运行记录</div>
                  <table v-else class="runs-table">
                    <thead><tr><th>开始时间</th><th>触发</th><th>模式</th><th>结果</th><th>入库K线</th></tr></thead>
                    <tbody>
                      <tr v-for="(r, i) in runsMap[t.id]" :key="i">
                        <td class="mono">{{ (r.started_at || '').replace('T', ' ').slice(5, 16) }}</td>
                        <td>{{ ({ schedule: '定时', catch_up: '补跑', manual: '手动' })[r.trigger] || r.trigger }}</td>
                        <td>{{ modeLabel(r.mode) }}</td>
                        <td>{{ r.aborted ? '熔停' : '完成' }} · 成 {{ fmt(r.ok_stocks) }} / 败 {{ fmt(r.failed_stocks) }}</td>
                        <td class="mono">{{ fmt(r.bars_upserted) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            <p class="note">调度说明：每日到点自动执行（北京时间，不辨交易日——非交易日空转无害）；错过到点（重启/关机）下次启动时自动补跑；全局串行——同一时刻只跑一个任务，后到排队。手动「立即执行」不影响当晚定时。</p>

            <!-- 新建/编辑对话框 -->
            <div v-if="taskDialog" class="dialog-mask" @click.self="taskDialog = null">
              <div class="dialog">
                <h3 class="dialog-title">{{ taskDialog.mode === 'create' ? '新建爬虫任务' : '编辑任务' }}</h3>
                <label class="dialog-field">
                  <span>名称（可选，默认按源/模式生成）</span>
                  <InputText v-model="taskDialog.name" placeholder="如：新浪每日增量" />
                </label>
                <label class="dialog-field">
                  <span>数据源</span>
                  <select v-model="taskDialog.source" class="dialog-select">
                    <option v-for="s in TASK_SOURCES" :key="s.v" :value="s.v">{{ s.label }}</option>
                  </select>
                </label>
                <label class="dialog-field">
                  <span>模式</span>
                  <select v-model="taskDialog.taskMode" class="dialog-select">
                    <option v-for="m in TASK_MODES" :key="m.v" :value="m.v">{{ m.label }}</option>
                  </select>
                </label>
                <label class="dialog-field">
                  <span>每日执行时刻（北京时间，24 小时制）</span>
                  <InputText v-model="taskDialog.schedule_time" placeholder="18:00" />
                </label>
                <p class="task-risk">{{ dialogRisk || '防封参数（限速/抖动/分批/熔停）已内置安全默认值，无需配置' }}</p>
                <p v-if="taskError" class="task-error">{{ taskError }}</p>
                <div class="dialog-actions">
                  <button class="btn" @click="taskDialog = null">取消</button>
                  <button class="btn btn-primary" @click="saveTask">保存</button>
                </div>
              </div>
            </div>
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

          <!-- ===== 量化教学：经典回测策略 ===== -->
          <template v-else>
            <div class="toolbar">
              <div>
                <h2 class="h1">量化教学 · 经典回测策略</h2>
                <p class="sub">六大经典策略 · 真实行情回测 · 收益对照买入持有</p>
              </div>
              <div class="toolbar-right">
                <span class="tag-note">教学 · 数据为不复权口径</span>
              </div>
            </div>

            <div class="learn-grid">
              <!-- 左列：策略导航（原理卡移至右列设置区下方） -->
              <div class="learn-side">
                <button v-for="s in STRATS" :key="s.id" class="learn-nav"
                  :class="{ active: learnStrat === s.id }" @click="learnStrat = s.id">
                  <span class="learn-nav-name">{{ s.name }}</span>
                  <span class="learn-nav-tag">{{ s.tag }}</span>
                </button>
              </div>

              <!-- 右列：设置 → 原理 → 结果 -->
              <div class="learn-main">
                <div class="card learn-params">
                  <div class="card-head">
                    <span class="card-title">回测设置</span>
                    <div class="learn-mode-seg">
                      <button class="learn-mode-btn" data-mode="single"
                        :class="{ active: learnMode === 'single' }"
                        :disabled="learnIsMomentum" title="动量为多标的组合策略，无单股模式"
                        @click="learnMode = 'single'">单股</button>
                      <button class="learn-mode-btn" data-mode="batch"
                        :class="{ active: learnMode === 'batch' }"
                        @click="learnMode = 'batch'">批量对比</button>
                      <button class="learn-mode-btn" data-mode="full"
                        :class="{ active: learnMode === 'full' }"
                        title="动量在全市场为组合级回测：按月末动量持有前 top_k，结果为单组合曲线"
                        @click="learnMode = 'full'">全市场</button>
                    </div>
                  </div>

                  <!-- 单股 / 批量：共用参数区 -->
                  <template v-if="learnMode !== 'full'">
                    <div class="learn-params-row">
                      <label class="learn-field" :class="{ wide: learnIsMomentum }">
                        <span>{{ codeFieldLabel }}</span>
                        <AutoComplete v-model="learnCodePick" :suggestions="codeSuggestions"
                          optionLabel="label" dropdown multiple
                          placeholder="输入代码或名称搜索后选中，如 600519 / 茅台"
                          @complete="searchCodes" class="learn-ac" /></label>
                      <label class="learn-field"><span>回测区间</span>
                        <select v-model="learnRange" class="learn-select">
                          <option value="1y">近 1 年</option><option value="3y">近 3 年</option>
                          <option value="5y">近 5 年</option><option value="all">全史</option>
                        </select></label>
                      <label class="learn-field"><span>撮合模式</span>
                        <select v-model="learnExec" class="learn-select">
                          <option value="next_open">次日开盘（正确）</option>
                          <option value="close">当日收盘（未来函数）</option>
                        </select></label>
                    </div>
                    <div class="learn-params-row">
                      <label v-for="f in (STRAT_FIELDS[learnStrat] || [])" :key="f.k" class="learn-field">
                        <span>{{ f.label }}</span>
                        <select v-if="f.type === 'select'" v-model="learnParams[f.k]"
                          class="learn-select">
                          <option v-for="o in f.options" :key="o.value" :value="o.value">{{ o.label }}</option>
                        </select>
                        <input v-else :type="f.type === 'number' ? 'number' : 'text'" class="learn-input num"
                          :min="f.min" :max="f.max" :step="f.step"
                          v-model.number="learnParams[f.k]" />
                      </label>
                      <label class="learn-field"><span>佣金 ‱（双边）</span>
                        <input type="number" class="learn-input num" min="0" max="10" step="0.5"
                          v-model.number="learnCommission" /></label>
                      <label class="learn-field"><span>印花税 ‱（卖出）</span>
                        <input type="number" class="learn-input num" min="0" max="10" step="1"
                          v-model.number="learnSellTax" /></label>
                      <button class="btn btn-primary learn-run" :disabled="learnLoading" @click="runLearn">
                        <svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.8l7 4.2-7 4.2z" fill="currentColor"/></svg>
                        {{ learnLoading ? '回测中…' : '跑回测' }}</button>
                    </div>
                  </template>

                  <!-- 全市场：策略参数（动量 window/top_k/调仓频率）+ 区间/撮合 + 过滤面板 + 发起/历史 -->
                  <template v-else>
                    <div class="learn-params-row">
                      <label v-for="f in (STRAT_FIELDS[learnStrat] || [])" :key="'f-' + f.k" class="learn-field">
                        <span>{{ f.label }}</span>
                        <select v-if="f.type === 'select'" v-model="learnParams[f.k]"
                          class="learn-select">
                          <option v-for="o in f.options" :key="o.value" :value="o.value">{{ o.label }}</option>
                        </select>
                        <input v-else :type="f.type === 'number' ? 'number' : 'text'" class="learn-input num"
                          :min="f.min" :max="f.max" :step="f.step"
                          v-model.number="learnParams[f.k]" />
                      </label>
                      <label class="learn-field"><span>回测区间</span>
                        <select v-model="learnRange" class="learn-select">
                          <option value="1y">近 1 年</option><option value="3y">近 3 年</option>
                          <option value="5y">近 5 年</option><option value="all">全史</option>
                        </select></label>
                      <label class="learn-field"><span>撮合模式</span>
                        <select v-model="learnExec" class="learn-select">
                          <option value="next_open">次日开盘（正确）</option>
                          <option value="close">当日收盘（未来函数）</option>
                        </select></label>
                    </div>
                    <div class="learn-params-row">
                      <label class="learn-field wide"><span>全市场过滤</span>
                        <span class="learn-full-controls">
                          <label class="learn-check"><input type="checkbox" v-model="fullFilterSt" />剔除 ST/退</label>
                          <label class="learn-check">最少K线数
                            <input type="number" class="learn-input num" min="0" step="10"
                              v-model.number="fullMinBars" /></label>
                          <label class="learn-check"><input type="checkbox" v-model="fullAdjust" />后复权口径</label>
                          <label class="learn-check"><input type="checkbox" v-model="fullIncludeDelisted" />含退市股</label>
                          <label class="learn-check" title="同参数已有完成结果时，成功股直接复用（以源任务时点数据为准），仅重算其失败股与新进股票">
                            <input type="checkbox" v-model="fullReuse" />复用历史结果</label>
                        </span>
                      </label>
                    </div>
                    <div class="learn-params-row">
                      <label class="learn-field wide"
                        title="选股域过滤（实验）：参考时点=回测区间首日收盘（不复权）。流通市值=首日收盘×流通股本；一手成本=首日收盘×100（A股一手=100股，1000 元即股价 ≤10 元）。退市股无流通股本数据，仅受一手价过滤、豁免市值维度（保留修幸存者偏差对照组）。0=不限。">
                        <span>选股域（实验）</span>
                        <span class="learn-full-controls">
                          <label class="learn-check">流通市值下限(亿)
                            <input type="number" class="learn-input num" min="0" step="10"
                              data-testid="full-min-cap" v-model.number="fullMinCap" /></label>
                          <label class="learn-check">流通市值上限(亿)
                            <input type="number" class="learn-input num" min="0" step="10"
                              data-testid="full-max-cap" v-model.number="fullMaxCap" /></label>
                          <label class="learn-check">一手价上限(元)
                            <input type="number" class="learn-input num" min="0" step="100"
                              data-testid="full-max-lot" v-model.number="fullMaxLot" /></label>
                        </span>
                      </label>
                    </div>
                    <div class="learn-params-row">
                      <div class="learn-run-group">
                        <button v-if="fullRunning" class="btn" @click="cancelFullRun">
                          <svg class="ic" viewBox="0 0 12 12" aria-hidden="true">
                            <path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
                          取消</button>
                        <button class="btn btn-primary" :disabled="fullStarting || fullRunning"
                          @click="startFullRun">
                          <svg class="ic" viewBox="0 0 14 14" aria-hidden="true">
                            <circle cx="7" cy="7" r="5.4" fill="none" stroke="currentColor" stroke-width="1.3"/>
                            <path d="M1.6 7h10.8M7 1.6c-3.4 3.4-3.4 7.4 0 10.8 3.4-3.4 3.4-7.4 0-10.8z"
                              fill="none" stroke="currentColor" stroke-width="1.1"/></svg>
                          {{ fullStarting ? '发起中…' : '发起全量' }}</button>
                      </div>
                    </div>
                    <div v-if="fullError" class="error-banner"><span>{{ fullError }}</span></div>
                    <div v-if="fullRun" class="detail" data-testid="full-status">
                      <template v-if="fullRunning">
                        全量回测中：已完成 {{ fullRun.done }} / {{ fullRun.total }} 只（失败 {{ fullRun.failed }}，过滤跳过 {{ fullRun.skipped }}<template v-if="fullRun.reused_count > 0">，已复用 {{ fullRun.reused_count }} 只</template>）… 可随时取消
                      </template>
                      <template v-else-if="fullRun.status === 'interrupted'">
                        上次任务因后端重启中断（已完成 {{ fullRun.done }}/{{ fullRun.total }}），可重新发起
                      </template>
                      <template v-else-if="fullRun.status === 'failed'">
                        全量回测失败：{{ fullRun.error }}
                      </template>
                      <template v-else-if="fullViewable">
                        任务 #{{ fullRun.id }}（{{ fullRun.status === 'canceled' ? '已取消' : '已完成' }}）：参与 {{ fullRun.total }} 只 · 过滤跳过 {{ fullRun.skipped }} · 失败 {{ fullRun.failed }}<template v-if="fullRun.reused_count > 0"> · 已复用 run #{{ fullRun.reused_from }} 的 {{ fullRun.reused_count }} 只结果</template>
                      </template>
                    </div>
                  </template>
                </div>

                <!-- 原理卡（当前策略说明，自左列移入） -->
                <div class="card learn-card" v-if="learnStratMeta">
                  <div class="learn-brief">{{ learnStratMeta.brief }}</div>
                  <div class="learn-lessons">
                    <div class="learn-lesson-title">教学点 / 常见坑</div>
                    <ul><li v-for="l in learnStratMeta.lessons" :key="l">{{ l }}</li></ul>
                  </div>
                </div>

                <div v-if="learnError" class="error-banner"><span>{{ learnError }}</span></div>

                <!-- 结果容器：三态同框架（加载骨架 / 空态 / 按模式结果），消灭布局跳动 -->
                <div class="result-frame">
                  <div class="result-head">
                    <span class="card-title">{{ resultTitle }}</span>
                    <span class="card-hint">{{ resultHint }}</span>
                  </div>

                  <div v-if="resultLoading" class="card learn-empty sk-zone">
                    <div class="state">{{ loadingText }}</div>
                    <div class="sk sk-line"></div><div class="sk sk-main"></div>
                    <div class="sk sk-line"></div><div class="sk sk-line short"></div>
                  </div>

                  <div v-else-if="resultEmpty" class="card learn-empty">
                    <div class="state">{{ emptyTitle }}</div>
                    <div class="detail">{{ emptyDetail }}</div>
                  </div>

                  <!-- ===== 单股结果 ===== -->
                  <template v-else-if="learnMode === 'single'">
                    <div v-if="learnResult && learnResult.status === 'down'" class="card learn-empty">
                      <div class="state">回测不可用</div>
                      <div class="detail">{{ learnResult.detail }}</div>
                    </div>
                    <div v-else-if="learnResult && learnResult.message" class="card learn-empty">
                      <div class="state">{{ learnResult.message }}</div>
                    </div>
                    <template v-else-if="learnResult && learnResult.metrics">
                      <div v-if="learnResult.adjust_applied"
                        class="adj-ok">已按后复权口径计算收益（除权除息已修正）</div>
                      <div v-else class="task-risk">
                        复权因子不可用，本次为不复权口径——跨除权日的收益存在假跳空失真
                      </div>
                      <div class="learn-metrics">
                        <div class="card lm"><div class="lm-k">总收益</div><div class="lm-v" :class="learnResult.metrics.total_return >= 0 ? 'chg-up' : 'chg-down'">{{ (learnResult.metrics.total_return * 100).toFixed(1) }}%</div></div>
                        <div class="card lm"><div class="lm-k">年化收益</div><div class="lm-v" :class="learnResult.metrics.annual_return >= 0 ? 'chg-up' : 'chg-down'">{{ (learnResult.metrics.annual_return * 100).toFixed(1) }}%</div></div>
                        <div class="card lm"><div class="lm-k">最大回撤</div><div class="lm-v chg-down">{{ (learnResult.metrics.max_drawdown * 100).toFixed(1) }}%</div></div>
                        <div class="card lm"><div class="lm-k">夏普比率</div><div class="lm-v">{{ learnResult.metrics.sharpe?.toFixed(2) }}</div></div>
                        <div class="card lm"><div class="lm-k">胜率</div><div class="lm-v">{{ (learnResult.metrics.win_rate * 100).toFixed(0) }}%</div></div>
                        <div class="card lm"><div class="lm-k">盈亏比</div><div class="lm-v">{{ learnResult.metrics.profit_loss_ratio?.toFixed(2) }}</div></div>
                        <div class="card lm"><div class="lm-k">交易次数</div><div class="lm-v">{{ learnResult.metrics.trades }}</div></div>
                      </div>
                      <div class="card chart-card"><div ref="learnChartEl" class="learn-chart"></div></div>
                      <div class="card detail-table-card" v-if="learnResult.trades.length">
                        <table class="runs-table">
                          <thead><tr><th>方向</th><th>标的</th><th>买入日</th><th>买入价</th><th>卖出日</th><th>卖出价</th><th>收益率</th></tr></thead>
                          <tbody>
                            <tr v-for="(t, i) in learnResult.trades" :key="i">
                              <td>{{ t.direction === 'long' ? '多' : t.direction }}</td>
                              <td class="mono">{{ t.symbol || learnResult.codes?.[0] }}</td>
                              <td class="mono">{{ t.entry_date }}</td>
                              <td class="mono">{{ t.entry_price?.toFixed?.(2) ?? t.entry_price }}</td>
                              <td class="mono">{{ t.exit_date || '持有中' }}</td>
                              <td class="mono">{{ t.exit_price != null ? (t.exit_price?.toFixed?.(2) ?? t.exit_price) : '—' }}</td>
                              <td :class="(t.pnl_pct || 0) >= 0 ? 'chg-up' : 'chg-down'">
                                {{ t.pnl_pct != null ? ((t.pnl_pct >= 0 ? '+' : '') + (t.pnl_pct * 100).toFixed(2) + '%') : '—' }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div v-if="learnResult.gap_dates?.length" class="task-risk">
                        检测到疑似除权跳空日：{{ learnResult.gap_dates.join('、') }}——不复权数据跨除权日回测存在假亏损噪声，结果仅供参考
                      </div>
                    </template>
                  </template>

                  <!-- ===== 批量对比 ===== -->
                  <template v-else-if="learnMode === 'batch'">
                    <div class="card learn-empty" v-if="!batchResults.length">
                      <div class="state">全部股票回测失败</div>
                      <div class="detail" v-for="f in batchFailed" :key="f.code">
                        {{ f.code }} {{ f.name }}：{{ f.reason }}</div>
                    </div>
                    <template v-else>
                      <div class="adj-ok" v-if="batchResults.every(r => r.adjust_applied)">
                        已按后复权口径计算收益（除权除息已修正）· {{ batchResults.length }} 只对比</div>
                      <div v-else class="task-risk">
                        部分股票复权因子不可用（表中标注），不复权口径存在假跳空失真</div>
                      <div class="card chart-card"><div ref="learnChartEl" class="learn-chart"></div></div>
                      <div class="card detail-table-card">
                        <table class="runs-table learn-cmp">
                          <thead><tr>
                            <th>代码</th><th>名称</th><th>总收益</th><th>年化</th>
                            <th>最大回撤</th><th>夏普</th><th>胜率</th><th>交易次数</th>
                          </tr></thead>
                          <tbody>
                            <tr v-for="r in batchResults" :key="r.code">
                              <td class="mono">{{ r.code }}</td>
                              <td>{{ pickNameOf(r.code) }}</td>
                              <td :class="(r.metrics?.total_return ?? 0) >= 0 ? 'chg-up' : 'chg-down'">
                                {{ fmtSignedPct(r.metrics?.total_return ?? 0) }}</td>
                              <td>{{ ((r.metrics?.annual_return ?? 0) * 100).toFixed(1) }}%</td>
                              <td class="chg-down">{{ ((r.metrics?.max_drawdown ?? 0) * 100).toFixed(1) }}%</td>
                              <td>{{ r.metrics?.sharpe?.toFixed(2) }}</td>
                              <td>{{ ((r.metrics?.win_rate ?? 0) * 100).toFixed(0) }}%</td>
                              <td>{{ r.metrics?.trades ?? 0 }}</td>
                            </tr>
                            <tr v-for="f in batchFailed" :key="'f-' + f.code" class="learn-fail-row">
                              <td class="mono">{{ f.code }}</td>
                              <td>{{ f.name }}</td>
                              <td colspan="6" class="learn-fail-reason">失败：{{ f.reason }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                    </template>
                  </template>

                  <!-- ===== 全市场结果 ===== -->
                  <template v-else>
                    <div class="learn-metrics full-total-metrics">
                      <div class="card lm"><div class="lm-k">参与股票</div><div class="lm-v">{{ fullRun.stats?.count ?? 0 }}</div></div>
                      <div class="card lm"><div class="lm-k">正收益占比</div><div class="lm-v">{{ fullRun.stats ? ((fullRun.stats.positive_pct ?? 0) * 100).toFixed(1) + '%' : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">总收益中位数</div><div class="lm-v">{{ fullRun.stats?.median_return != null ? fmtPctSigned(fullRun.stats.median_return) : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">最好收益</div><div class="lm-v chg-up">{{ fullRun.stats?.best_return != null ? fmtPctSigned(fullRun.stats.best_return) : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">最差收益</div><div class="lm-v chg-down">{{ fullRun.stats?.worst_return != null ? fmtPctSigned(fullRun.stats.worst_return) : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">跑赢买入持有</div><div class="lm-v">{{ fullRun.stats?.beat_buyhold_pct != null ? ((fullRun.stats.beat_buyhold_pct) * 100).toFixed(1) + '%' : '—' }}</div></div>
                    </div>
                    <div class="chart-toolbar">
                      <span class="ct-label">视图：</span>
                      <button v-for="v in FULL_VIEWS" :key="v.id" class="ct-btn"
                        :class="{ on: fullView === v.id }" @click="setFullView(v.id)">{{ v.label }}</button>
                      <span v-if="fullView === 'all' && fullDelistedCodes.size"
                        class="ct-hint">红线 = 退市股（{{ fullDelistedCodes.size }} 只）</span>
                    </div>
                    <div class="card chart-card"><div ref="fullChartEl" class="learn-chart"></div>
                      <div class="detail" style="padding: 0 14px 10px">全市场累计收益率曲线（起点 = 0%）</div></div>

                    <!-- 退市股票总结：独立分组，样式与全市场回测结果一致 -->
                    <template v-if="delistRows.rows.length || delistRows.total > 0">
                    <div class="result-head delist-head" style="margin-top: 20px">
                      <span class="card-title">退市股票总结</span>
                      <span class="card-hint">已摘牌股票独立统计 · 修幸存者偏差口径<template v-if="fullRun.stats?.delisted_failed > 0"> · 另有 {{ fullRun.stats.delisted_failed }} 只回测失败</template></span>
                    </div>
                    <div class="learn-metrics delist-metrics" v-if="delistRows.stats">
                      <div class="card lm"><div class="lm-k">退市股参与</div><div class="lm-v">{{ delistRows.stats.count ?? 0 }}</div></div>
                      <div class="card lm"><div class="lm-k">正收益占比</div><div class="lm-v">{{ delistRows.stats.positive_pct != null ? (delistRows.stats.positive_pct * 100).toFixed(1) + '%' : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">总收益中位数</div><div class="lm-v">{{ delistRows.stats.median_return != null ? fmtPctSigned(delistRows.stats.median_return) : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">最好收益</div><div class="lm-v chg-up">{{ delistRows.stats.best_return != null ? fmtPctSigned(delistRows.stats.best_return) : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">最差收益</div><div class="lm-v chg-down">{{ delistRows.stats.worst_return != null ? fmtPctSigned(delistRows.stats.worst_return) : '—' }}</div></div>
                      <div class="card lm"><div class="lm-k">跑赢买入持有</div><div class="lm-v">{{ delistRows.stats.beat_buyhold_pct != null ? ((delistRows.stats.beat_buyhold_pct) * 100).toFixed(1) + '%' : '—' }}</div></div>
                    </div>
                    <div class="card chart-card"><div ref="delistChartEl" class="learn-chart"></div>
                      <div class="detail" style="padding: 0 14px 10px">退市股累计收益率曲线（起点 = 0%）</div></div>
                    <div class="card detail-table-card" v-if="delistRows.rows.length">
                      <table class="runs-table learn-delist-cmp">
                        <thead><tr>
                          <th>代码</th><th>名称</th>
                          <th v-for="([k, label]) in FULL_SORT_COLS" :key="k" class="full-sort"
                            @click="sortDelist(k)">{{ label }}{{ delistRowsSort === k ? (delistRowsDesc ? ' ↓' : ' ↑') : '' }}</th>
                        </tr></thead>
                        <tbody>
                          <tr v-for="r in delistRows.rows" :key="r.code" :class="{ 'learn-fail-row': r.status !== 'ok' }">
                            <td class="mono"><a href="javascript:void 0" class="full-code" @click="pickFullStock(r.code)">{{ r.code }}</a></td>
                            <td>{{ r.name }}<span v-if="r.reason" class="full-fail-reason">（失败：{{ r.reason }}）</span></td>
                            <td :class="(r.total_return ?? 0) >= 0 ? 'chg-up' : 'chg-down'">{{ fmtPctSigned(r.total_return) }}</td>
                            <td>{{ fmtPctSigned(r.annual_return) }}</td>
                            <td class="chg-down">{{ r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(1) + '%' : '—' }}</td>
                            <td>{{ r.sharpe?.toFixed(2) ?? '—' }}</td>
                            <td>{{ r.win_rate != null ? (r.win_rate * 100).toFixed(0) + '%' : '—' }}</td>
                            <td>{{ r.trades ?? '—' }}</td>
                          </tr>
                        </tbody>
                      </table>
                      <div class="detail" style="padding: 8px 14px">
                        共 {{ delistRows.total }} 只退市股 · 第 {{ delistRowsPage + 1 }} / {{ delistPages }} 页
                        <button class="btn" :disabled="delistRowsPage <= 0" @click="delistRowsPage--; loadDelistRows()">上一页</button>
                        <button class="btn" :disabled="delistRowsPage >= delistPages - 1" @click="delistRowsPage++; loadDelistRows()">下一页</button>
                      </div>
                    </div>
                    <div v-if="!delistRows.rows.length" class="card delisted-note muted">
                      本次回测样本中无退市股（或退市股全部失败未产出收益）。
                    </div>
                    </template>
                    <div class="card detail-table-card">
                      <table class="runs-table learn-full-cmp">
                        <thead><tr>
                          <th>代码</th><th>名称</th>
                          <th v-for="([k, label]) in FULL_SORT_COLS" :key="k" class="full-sort"
                            @click="sortFull(k)">{{ label }}{{ fullRowsSort === k ? (fullRowsDesc ? ' ↓' : ' ↑') : '' }}</th>
                        </tr></thead>
                        <tbody>
                          <tr v-for="r in fullRows.rows" :key="r.code" :class="{ 'learn-fail-row': r.status !== 'ok' }">
                            <td class="mono"><a href="javascript:void 0" class="full-code" @click="pickFullStock(r.code)">{{ r.code }}</a></td>
                            <td>{{ r.name }}<span v-if="r.reason" class="full-fail-reason">（失败：{{ r.reason }}）</span></td>
                            <td :class="(r.total_return ?? 0) >= 0 ? 'chg-up' : 'chg-down'">{{ fmtPctSigned(r.total_return) }}</td>
                            <td>{{ fmtPctSigned(r.annual_return) }}</td>
                            <td class="chg-down">{{ r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(1) + '%' : '—' }}</td>
                            <td>{{ r.sharpe?.toFixed(2) ?? '—' }}</td>
                            <td>{{ r.win_rate != null ? (r.win_rate * 100).toFixed(0) + '%' : '—' }}</td>
                            <td>{{ r.trades ?? '—' }}</td>
                          </tr>
                        </tbody>
                      </table>
                      <div class="detail full-pager" style="padding: 8px 14px">
                        共 {{ fullRows.total }} 行 · 第 {{ fullRowsPage + 1 }} / {{ fullPages }} 页
                        <button class="btn" :disabled="fullRowsPage <= 0" @click="fullRowsPage--; loadFullRows()">上一页</button>
                        <button class="btn" :disabled="fullRowsPage >= fullPages - 1" @click="fullRowsPage++; loadFullRows()">下一页</button>
                        <a class="btn" :href="fullExportUrl" download>
                          <svg class="ic" viewBox="0 0 12 12" aria-hidden="true">
                            <path d="M6 1.5v6M3.5 5l2.5 2.5L8.5 5M2 10.5h8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
                          导出 CSV</a>
                      </div>
                    </div>
                  </template>
                </div>
              </div>
            </div>
            <p class="note">引擎：vectorbt（撮合）· 数据：daily_bar 不复权日线（新浪源）· 成本按比例近似（未含最低佣金 5 元）· 教学用途，不构成投资建议</p>
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
  --content-pad: 32px 20px 24px;   /* 左右 20px（2026-11-18 用户反馈 40px 太宽） */
  --h1-size: 28px;
  --h1-weight: 600;
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
/* 退市过滤下拉：与搜索框同行，Carbon 方角 */
.delisted-select { min-width: 130px; border-radius: 0; }
.delisted-select .p-select-label { border-radius: 0; font-size: 13px; }
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
/* 退市标记（数据浏览列表，Carbon 内联通知色系：#FFF1F1 底 + #A2191F 字） */
.delisted-tag {
  display: inline-block; font-size: 11px; line-height: 1; padding: 4px 8px;
  background: #fff1f1; color: #a2191f; border: 1px solid #da1e28;
}
.browse-pager {
  display: flex; align-items: center; justify-content: center; gap: 18px;
  margin-top: 16px; font-size: 12px; color: var(--fg-2);
}
.browse-head { background: #f5f5f5; }
.browse-head { height: 48px; padding: 0 16px; background: #f4f4f4; color: #525252; }
.browse-row.up { color: #c50f1f; }
.browse-row.down { color: #0f7b0f; }

/* ===== 爬虫任务管理页 ===== */
.task-list { display: flex; flex-direction: column; gap: 16px; }
.task-card { padding: 20px 24px; }
.task-empty { padding: 40px 24px; text-align: center; }
.task-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.task-name { margin: 0 0 8px; font-size: 18px; font-weight: 600; color: var(--fg); }
.task-tags { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.chip.mode { background: #f0f0f0; color: var(--fg-2); }
.chip.time { background: #f0f0f0; color: var(--fg-2); font-variant-numeric: tabular-nums; }
/* 相位徽章：任务页专属（仪表盘 .fbadge/.dot 是 absolute 右上角定位体系，
   复用会脱离文档流压住右上角开关——用户过目发现的重叠 bug） */
.phase-badge {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; line-height: 1; color: var(--fg-2); white-space: nowrap;
}
.phase-badge .pdot { width: 8px; height: 8px; border-radius: 50%; background: var(--dot-idle); }
.phase-badge .pdot.up { background: var(--dot-up); }
.phase-badge .pdot.queued { background: var(--dot-warn); }
.task-progress { margin: 14px 0 4px; }
.task-progress .detail { margin-top: 6px; }
.task-meta { display: flex; gap: 24px; flex-wrap: wrap; margin-top: 14px; font-size: 13px; color: var(--fg-2); }
.task-meta b { color: var(--fg); font-weight: 600; }
.task-risk {
  margin-top: 10px; padding: 8px 12px; font-size: 12px;
  background: var(--ph-tag-bg); color: var(--ph-tag-fg); border-left: 3px solid var(--dot-warn);
}
.task-lasterr {
  margin-top: 10px; padding: 8px 12px; font-size: 12px;
  background: var(--error-bg); color: var(--dot-down); border-left: 3px solid var(--dot-down);
  word-break: break-all;
}
/* 基金页过滤 chips：短标签圆角胶囊，状态用圆点+配色；禁换行防挤压竖排 */
.filter-chip {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 6px 14px; font-size: 13px; line-height: 1;
  border-radius: 999px; border: 1px solid #c6c6c6; background: #fff;
  color: var(--fg-2, #525252); white-space: nowrap; cursor: pointer;
  transition: border-color .15s, color .15s, background .15s;
}
.filter-chip:hover { border-color: #0f62fe; color: #0f62fe; }
.filter-chip .chip-dot { width: 8px; height: 8px; border-radius: 50%;
  background: #c6c6c6; flex: none; transition: background .15s; }
.filter-chip.active { border-color: #0f62fe; color: #0f62fe; background: #edf5ff; }
.filter-chip.active .chip-dot { background: #0f62fe; }
/* 基金页搜索栏：宽度自适应 + 换行（.ph-search 的 480px 钉死宽度会导致
   控件挤压竖排溢出——2026-10-29 截图反馈） */
/* —— 统一筛选面板（2026-11-17 用户反馈：筛选区显乱）——
   一个面板三行分组：搜索/基础筛选 · 口径切换（带小标签）· 区间筛选；
   行间独立 flex，窄屏各自换行互不搅动；funds-search 类保留在行 1 仅为
   E2E 选择器契约（test_fund_dca_page 的 .funds-search .browse-input） */
.dca-filter-panel { display: flex; flex-direction: column; gap: 10px;
  margin: 0 0 12px; padding: 12px 14px; background: var(--card-bg, #fff);
  border: 1px solid var(--card-border-color, #e0e0e0); border-radius: 6px; }
.dca-filter-row { display: flex; flex-wrap: wrap; align-items: center;
  gap: 8px 12px; min-width: 0; }
.dca-filter-spacer { flex: 1 1 0; }
/* 行 1 搜索框：自带边框的紧凑输入（替代旧「整条边框大盒子」形态），
   focus 主蓝描边；.browse-input 全局透明无边框样式原样适用 */
.dca-search-box { display: inline-flex; align-items: center; gap: 8px;
  flex: 1 1 240px; max-width: 360px; min-width: 180px; height: 32px;
  padding: 0 10px; border: 1px solid #8d8d8d; background: #fff;
  transition: border-color .1s; }
.dca-search-box:focus-within { border-color: var(--accent, #0f62fe); }
.dca-search-box .pi-search { color: #525252; font-size: 13px; }
/* PrimeVue 运行时注入的 .p-inputtext 主题（白底描边）晚于打包 CSS 生效，
   同权重会盖掉 .browse-input 的透明无边框——三连类提高权重压回透明，
   搜索框只保留 .dca-search-box 自身的一圈描边 */
.dca-search-box input.p-inputtext.browse-input { border: 0; background: transparent;
  box-shadow: none; padding: 0; height: 100%; flex: 1 1 auto; min-width: 0; width: auto; }
.dca-filter-row .age-wrap .age-input { width: 64px; }
/* 行 2 口径组：12px 灰组标签 + 按钮组，组间留白大于组内 */
.dca-filter-segs { gap: 8px 18px; }
.seg-group { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.seg-label { font-size: 12px; color: #525252; white-space: nowrap; }
/* 定投方式 ? 说明按钮 + 弹出面板（2026-11-23 五轮）：? 与按钮组同行，
   面板绝对定位在组下方、点击 ? 开关；宽屏限宽防超长行 */
.mode-seg { position: relative; }
.mode-help-btn {
  width: 18px; height: 18px; line-height: 1; padding: 0;
  border: 1px solid #8d8d8d; border-radius: 50%;
  background: #f4f4f4; color: #525252;
  font-size: 12px; font-weight: 600; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto;
}
.mode-help-btn:hover, .mode-help-btn.active {
  border-color: #0f62fe; color: #0f62fe; background: #eaf1fb;
}
.mode-help-pop {
  position: absolute; z-index: 30; top: calc(100% + 8px); left: 0;
  width: min(520px, calc(100vw - 48px));
  background: #ffffff; border: 1px solid #8d8d8d;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .14);
  padding: 12px 14px; text-align: left;
}
.mode-help-item { margin: 0 0 10px; font-size: 12px; color: #161616; }
.mode-help-item b { margin-right: 6px; }
.mode-help-brief { color: #0f62fe; }
.mode-help-item p { margin: 3px 0 0; color: #525252; line-height: 1.55; }
.mode-help-note {
  margin: 8px 0 0; padding-top: 8px; border-top: 1px solid #e0e0e0;
  font-size: 11px; color: #8d8d8d; line-height: 1.5;
}
/* 列显示过滤（MultiSelect）：与搜索行 small 控件同高，窄屏可收缩 */
.funds-search .dca-col-select { min-width: 118px; max-width: 160px; }
.funds-search .dca-col-select .p-multiselect-label { font-size: 12px; padding: 5px 8px; }
/* 行 3 区间筛选：面板内嵌区块——浅灰底替代独立描边，视觉上从属于同一面板 */
.timing-filter--inline { margin: 0; border: 0; border-radius: 4px;
  background: var(--surface-50, #f4f4f4); }
.age-wrap { display: inline-flex; align-items: center; gap: 6px; }
.age-wrap .age-input { width: 76px; text-align: right; }
.age-suffix { font-size: 12px; opacity: .72; white-space: nowrap; }
/* 定投时机区间筛选：常显面板（2026-11-17 用户反馈）。auto-fit 网格窄屏自动
   换行；InputNumber 根元素 flex 可收缩 + 内部 input 撑满，避免其默认
   min-content 宽度把网格撑破、冲出页面 */
.timing-filter { margin: 4px 0 12px; padding: 10px 12px;
  border: 1px solid var(--card-border-color, #e0e0e0); border-radius: 6px; }
.timing-filter-head { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; }
.timing-filter-title { font-size: 13px; font-weight: 600; }
.timing-filter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px 16px; }
.timing-filter-grid > label { min-width: 0; }
.timing-filter-range { display: flex; gap: 4px; align-items: center; min-width: 0; }
.timing-filter-range .p-inputnumber { flex: 1 1 0; min-width: 0; }
.timing-filter-range .p-inputnumber .p-inputnumber-input { width: 100%; }
.timing-filter-actions { display: flex; gap: 8px; align-items: flex-end; }
/* 结论卡（整宽，三条数据条自适应横排） + 分箱矩阵（拉伸撑满卡片宽）
   （2026-10-31 截图反馈：原整段长句结论信息密度过高，重排后又留右侧空白；
   二稿按用户要求整行撑满） */
.dca-panel { margin-top: 10px; }
.dca-conclusion { background: #f4f7fb; border: 1px solid #d0e2ff;
  border-radius: 8px; padding: 12px 16px 14px; font-size: 13px; line-height: 1.6; }
.dc-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; }
.dc-head b { font-size: 14px; }
.dc-badge { font-size: 12px; color: #5a6b85; background: #e8f0fc; border-radius: 999px;
  padding: 2px 10px; white-space: nowrap; }
.dc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 12px 28px; margin-top: 12px; }
/* 止盈明细弹窗内容纵向弹性布局（弹窗钉死 90vh，四边边距统一 5vh）：
   各区块禁止压缩，净值曲线区吸收内容不足时的剩余高度（内容超出则弹窗滚动） */
.dca-rounds-body { display: flex; flex-direction: column; height: 100%; }
.dca-rounds-body > * { flex-shrink: 0; }
.dca-rounds-chart { flex: 1 1 auto; min-height: 300px; width: 100%; margin-bottom: 10px; }
.dc-label { font-size: 12px; color: #5a6b85; }
.dc-value { font-size: 16px; font-weight: 600; margin-top: 2px; }
.dc-value .pos { color: #da1e28; }   /* 红涨（同 .lm-v.chg-up 口径） */
.dc-value .neg { color: #198038; }   /* 绿跌 */
.dc-arrow { color: #8aa0c0; font-weight: 400; font-size: 13px; margin: 0 5px; }
.dc-note { font-size: 12px; color: #6b7a93; margin-top: 3px; }
.dc-hint { font-size: 11px; font-weight: 400; color: #5a6b85; background: #e8f0fc;
  border-radius: 999px; padding: 1px 8px; margin-left: 6px; }
.dc-best { background: #f3f9ea; border: 1px solid #c9e4a2; border-radius: 8px;
  padding: 10px 12px 12px; }
.dc-best .dc-label { color: #56742f; }
.dca-matrix-wrap { margin-top: 14px; overflow-x: auto; }
.dca-matrix { border-collapse: collapse; font-size: 12px; }
.dca-matrix th, .dca-matrix td { border: 1px solid #e0e0e0; padding: 6px 10px;
  text-align: center; white-space: nowrap; }
.dca-matrix th { background: #f4f4f4; font-weight: 600; }
.dca-matrix-best { background: #def1c6; font-weight: 600; }
.dca-matrix .chg-up { color: #da1e28; }    /* 红涨 */
.dca-matrix .chg-down { color: #198038; }  /* 绿跌 */
/* 散点区块内的矩阵拉伸撑满（基础 12px 版留给止盈明细弹窗） */
.dca-matrix-wrap .dca-matrix { width: 100%; min-width: 620px; font-size: 13px; }
.dca-matrix-wrap .dca-matrix th, .dca-matrix-wrap .dca-matrix td { padding: 9px 14px; }
.dca-matrix.dca-median { margin-top: 14px; }
/* 基金详情页风险/波动指标区：指标卡 + 多窗口对照表（宽度自适应窄屏） */
.risk-card { margin-top: 14px; }
.risk-table-wrap { overflow-x: auto; }
.risk-table { border-collapse: collapse; font-size: 12px; width: 100%;
  min-width: 480px; }
.risk-table th, .risk-table td { border: 1px solid #e0e0e0; padding: 6px 10px;
  text-align: center; white-space: nowrap; }
.risk-table th { background: #f4f4f4; font-weight: 600; }
.risk-table tbody td.mono { font-weight: 600; }
.risk-card .note { color: #6b7a93; }
.dca-cell-link { cursor: pointer; }
.dca-cell-link:hover { background: #eef4fc; }
.detail-btn {
  padding: 3px 12px; font: inherit; font-size: 12px; cursor: pointer;
  background: var(--btn-bg); color: var(--btn-fg);
  border: 1px solid var(--btn-border-color); border-radius: 4px;
}
.detail-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
.browse-pv tbody tr { cursor: pointer; }
/* 止盈点对比区块（市场级六档）：行可点、选中态高亮 + 手写 SVG 折线 */
.tp-compare td { cursor: pointer; }
.tp-compare tbody tr:hover td { background: #eef4fc; }
.tp-compare tr.tp-row-active td { background: #def1c6; }
.tp-chart { width: 100%; height: auto; max-width: 720px; margin: 12px auto 0; display: block; }
.tp-chart .tp-line { fill: none; stroke: var(--accent); stroke-width: 2; }
.tp-chart .tp-zero { stroke: #cbd5e1; stroke-width: 1; stroke-dasharray: 4 3; }
.tp-chart circle { fill: var(--accent); }
.tp-chart circle.tp-dot-active { stroke: #1d4ed8; stroke-width: 3; }
.tp-chart text.tp-x { font-size: 11px; fill: #64748b; }
.tp-chart text.tp-v { font-size: 11px; fill: #334155; }
.task-actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.task-actions .btn { font-size: 13px; padding: 6px 14px; }
.btn-danger { color: var(--dot-down); border-color: var(--dot-down); }
.btn-danger:hover { background: var(--error-bg); }
.task-runs { margin-top: 16px; border-top: 1px solid var(--card-border-color); padding-top: 14px; }
.runs-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.runs-table th { text-align: left; padding: 6px 10px; background: #f4f4f4; color: var(--fg-2); font-weight: 600; }
.runs-table td { padding: 7px 10px; border-bottom: 1px solid #ededed; color: var(--fg-2); }
.runs-table .mono { font-variant-numeric: tabular-nums; }

/* 开关（carbon 方角滑块） */
.switch { position: relative; display: inline-block; width: 44px; height: 24px; flex: none; cursor: pointer; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider {
  position: absolute; inset: 0; background: var(--dot-idle); transition: background .15s;
}
.switch .slider::before {
  content: ''; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px;
  background: #fff; transition: transform .15s;
}
.switch input:checked + .slider { background: var(--accent); }
.switch input:checked + .slider::before { transform: translateX(20px); }

/* 对话框（carbon 模态） */
.dialog-mask {
  position: fixed; inset: 0; background: rgba(22, 22, 22, .5); z-index: 60;
  display: flex; align-items: center; justify-content: center;
}
.dialog {
  width: min(440px, calc(100vw - 48px)); background: var(--card-bg);
  border: 1px solid var(--card-border-color); border-top: 4px solid var(--accent);
  padding: 24px; box-shadow: 0 8px 32px rgba(0, 0, 0, .18);
}
.dialog-title { margin: 0 0 18px; font-size: 18px; font-weight: 600; color: var(--fg); }
.dialog-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.dialog-field > span { font-size: 12px; color: var(--fg-2); }
.dialog-select {
  height: 38px; padding: 0 10px; font: inherit; font-size: 13.5px;
  border: 1px solid var(--ph-input-border); background: var(--ph-input-bg); color: var(--fg);
}
.dialog .task-risk { margin: 4px 0 0; }
.task-error { margin: 8px 0 0; font-size: 12px; color: var(--dot-down); }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }

/* ===== 个股历史行情详情页 ===== */
:deep(.browse-pv .p-datatable-tbody tr) { cursor: pointer; }
:deep(.browse-pv .p-datatable-tbody tr:hover) { background: var(--nav-hover-bg); }
.chart-card { padding: 12px; margin-bottom: 16px; }
.kchart { width: 100%; height: 440px; }
.chart-note {
  height: 220px; display: flex; align-items: center; justify-content: center;
  color: var(--fg-3); font-size: 13px;
}
.detail-table-card { padding: 8px; }
.src-seg { display: inline-flex; border: 1px solid var(--btn-border-color); }
.src-seg-btn {
  min-width: 72px; height: 34px; padding: 0 14px; font: inherit; font-size: 13px;
  background: var(--btn-bg); color: var(--btn-fg); border: 0; cursor: pointer;
}
.src-seg-btn + .src-seg-btn { border-left: 1px solid var(--btn-border-color); }
.src-seg-btn:hover { background: var(--btn-hover-bg); }
.src-seg-btn.active { background: var(--accent); color: #fff; }

/* ===== 量化教学页 ===== */
.adj-ok {
  display: inline-block; padding: 4px 10px; margin-bottom: 12px;
  font-size: 12px; background: #e9f4e9; color: var(--dot-up);
}
.learn-grid { display: grid; grid-template-columns: 260px 1fr; gap: 20px; align-items: start; }
.learn-side { display: flex; flex-direction: column; gap: 8px; }
.learn-nav {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; font: inherit; font-size: 13.5px; cursor: pointer;
  background: var(--card-bg); border: 1px solid var(--card-border-color);
  border-radius: var(--card-radius); color: var(--fg);
}
.learn-nav:hover { background: var(--nav-hover-bg); }
.learn-nav.active { background: var(--nav-active-bg); border-color: var(--accent); }
.learn-nav-name { font-weight: 500; }
.learn-nav-tag { font-size: 11px; color: var(--fg-3); }
.learn-card { padding: 14px 16px; font-size: 13px; }
.learn-brief { color: var(--fg); line-height: 1.6; }
.learn-lesson-title { margin: 10px 0 6px; font-weight: 600; color: var(--fg-2); font-size: 12px; }
.learn-lessons ul { margin: 0; padding-left: 18px; color: var(--fg-2); }
.learn-lessons li { margin-bottom: 4px; line-height: 1.5; }
.learn-params { padding: 14px 16px; margin-bottom: 16px; }
.learn-params-row { display: flex; gap: 14px; flex-wrap: wrap; align-items: flex-end; }
.learn-params-row + .learn-params-row { margin-top: 10px; }
.learn-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--fg-2); }
.learn-field.wide { flex: 1; min-width: 260px; }
.learn-input { width: 110px; }
.learn-ac { width: 220px; }
.learn-ac .p-autocomplete-input { width: 100%; }
.learn-input.num { width: 90px; }
.learn-select { height: 36px; border: 1px solid var(--ph-input-border); background: #fff; font: inherit; font-size: 13px; }
.learn-run { margin-left: auto; }
.learn-metrics { display: grid; grid-template-columns: repeat(7, 1fr); gap: 10px; margin-bottom: 16px; }
.lm { padding: 10px 12px; text-align: center; }
.lm-k { font-size: 11px; color: var(--fg-3); margin-bottom: 4px; }
.lm-v { font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }
.learn-chart { width: 100%; height: 420px; }
.learn-empty { padding: 36px; text-align: center; }
/* 卡片头部规范：标题 + 右侧控件（教学页各卡统一） */
.card-head, .result-head { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }
.card-head .card-title, .result-head .card-title { font-size: 14px; font-weight: 600; }
.result-head .card-hint { font-size: 12px; color: var(--fg-3); }
/* 模式分段切换器（单股/批量/全市场） */
.learn-mode-seg { display: inline-flex; border: 1px solid #c6c6c6; }
.learn-mode-btn { padding: 6px 16px; font-size: 12px; background: #fff; border: 0;
  border-right: 1px solid #c6c6c6; cursor: pointer; color: var(--fg-2); }
.learn-mode-btn:last-child { border-right: 0; }
.learn-mode-btn:hover:not(:disabled):not(.active) { background: #eaeaea; }
.learn-mode-btn.active { background: var(--accent); color: #fff; }
.learn-mode-btn:disabled { opacity: .4; cursor: not-allowed; }
/* 结果容器：三态同高框架 */
.result-frame { margin-top: 16px; }
.result-frame .result-head { padding-bottom: 10px; border-bottom: 1px solid #e0e0e0; }
.result-frame .sk-zone { text-align: left; }
.result-frame .sk-zone .sk { margin: 10px 2px; }
/* 中性说明徽章（警示黄仅用于真警示） */
.tag-note { font-size: 12px; color: var(--fg-2); background: #e8e8e8;
  padding: 4px 10px; border-radius: 3px; white-space: nowrap; }
/* 按钮内联线性图标 */
.btn .ic { width: 12px; height: 12px; vertical-align: -1px; margin-right: 5px; }
/* 全市场段按钮组：与「跑回测」同位右对齐 */
.learn-run-group { margin-left: auto; display: flex; gap: 10px; align-items: flex-end; }
.learn-full-controls { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; min-height: 32px; }
.learn-check { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--fg-2); white-space: nowrap; }
.learn-check .learn-input.num { width: 76px; }
.full-sort { cursor: pointer; user-select: none; }
.full-sort:hover { color: var(--fg-1); }
.full-code { color: inherit; text-decoration: underline dotted #bbb; }
.full-code:hover { color: var(--accent, #1976d2); }
.full-fail-reason { color: #b42318; font-size: 12px; }
.learn-empty .detail { margin-top: 8px; }

.learn-cmp { width: 100%; border-collapse: collapse; font-size: 12px; }
.learn-cmp th, .learn-full-cmp th, .learn-delist-cmp th { text-align: left; padding: 8px 10px; background: #f4f4f4; color: var(--fg-2); font-weight: 600; }
.learn-cmp td, .learn-full-cmp td, .learn-delist-cmp td { padding: 7px 10px; border-bottom: 1px solid #ededed; font-variant-numeric: tabular-nums; }
.learn-fail-row td { color: var(--dot-down); font-size: 12px; }
.learn-fail-reason { word-break: break-all; }

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
  .learn-grid { grid-template-columns: 1fr; }
  .learn-metrics { grid-template-columns: repeat(3, 1fr); }
}

/* 全量回测：最好/最差收益 + 退市股总结条 */
.lm-v.chg-up { color: #da1e28; }
.lm-v.chg-down { color: #198038; }
.delisted-note {
  margin-top: 12px; padding: 12px 16px; font-size: 13px; line-height: 1.7;
  background: #fff8e6; border: 1px solid #f1c21b; border-radius: 2px;
  color: #393939;
}
.delisted-note.muted { background: #f4f4f4; border-color: #e0e0e0; color: #525252; }

/* 东财 Cookie 卡（Carbon 内联通知规范：白底 + 左 4px 状态条 + 方角 + 状态图标） */
.cookie-card { margin-bottom: 20px; padding: 14px 20px; background: #ffffff; border-radius: 0; }
.cookie-card.warn { border: 1px solid #e0e0e0; border-left: 4px solid #da1e28; }
.cookie-card.ok { border: 1px solid #e0e0e0; border-left: 4px solid #198038; }
.ck-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 14px; }
.ck-icon { width: 16px; height: 16px; flex: none; }
.ck-state.ok { color: #198038; font-weight: 600; }
.ck-state.bad { color: #da1e28; font-weight: 600; }
.ck-masked { font-size: 12px; color: #525252; margin-bottom: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ck-help { font-size: 13px; line-height: 1.7; color: #393939; margin: 6px 0 10px; }
.ck-help p { margin: 4px 0; }
.ck-help code { background: #f4f4f4; padding: 1px 6px; border-radius: 0;
  font-size: 12px; }
.cookie-card .p-inputtext { border-radius: 0; }
.cookie-card .p-button { border-radius: 0; }
.ck-form { display: flex; gap: 10px; align-items: center; }
.ck-input { flex: 1; min-width: 0; font-size: 12px; }
.ck-msg { margin-top: 10px; font-size: 13px; }
.ck-msg.ok { color: #198038; }
.ck-msg.bad { color: #da1e28; }
.src-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-left: auto; }
.src-dot.ok { background: #198038; }
.src-dot.bad { background: #f1c21b; }

/* 图表视图切换（Carbon） */
.chart-toolbar { display: flex; align-items: center; gap: 8px; margin: 10px 0 6px; }
.ct-label { font-size: 12px; color: #525252; }
.ct-btn {
  border: 1px solid #8d8d8d; background: #fff; color: #161616;
  font: inherit; font-size: 12px; padding: 4px 14px; cursor: pointer;
}
.ct-btn.on { background: #0f62fe; border-color: #0f62fe; color: #fff; font-weight: 600; }
.ct-btn:hover:not(.on) { background: #e8e8e8; }
.ct-hint { font-size: 12px; color: #da1e28; margin-left: 8px; }

/* ===== 桌面端侧栏折叠（>820px）：收窄 56px 图标窄条，状态记忆见 toggleSidebar =====
   仅桌面端生效；移动端（≤820px）抽屉样式在下方媒体查询，不受 collapsed 类影响 */
.sidebar { transition: background-color .15s ease, width .2s ease; }
/* 数据源图标：展开态隐藏（保持文字缩进原样），仅窄条显示 */
.src-icon { display: none; width: 16px; height: 16px; flex: none; color: #525252; }
@media (min-width: 821px) {
  .sidebar.collapsed { width: 56px; }
  .sidebar.collapsed .app-item { justify-content: center; padding: 0; }
  .sidebar.collapsed .app-item span { display: none; }
  .sidebar.collapsed .group { display: none; }
  /* 分组分隔线：与展开态同一条（nth-of-type(2) 带 border-top），窄条下变纯分隔块 */
  .sidebar.collapsed .group:nth-of-type(2) { display: block; height: 0; overflow: hidden;
    font-size: 0; margin: 14px 10px 6px; width: auto; }
  .sidebar.collapsed .nav-item,
  .sidebar.collapsed .src-item { font-size: 0; gap: 0; justify-content: center;
    padding: 0; margin: 0 8px 4px; }
  .sidebar.collapsed .nav-item.active::before,
  .sidebar.collapsed .nav-item.active::after,
  .sidebar.collapsed .src-item.active::after { display: none; }
  .sidebar.collapsed .src-icon { display: block; }
  .sidebar.collapsed .src-dot { margin-left: 4px; }
  .sidebar.collapsed .sidebar-foot { display: none; }
}

/* ===== 移动端（≤820px）：侧栏折叠、表格横滚、弹窗自适应（2026-11-03） ===== */
@media (max-width: 820px) {
  /* .body 自任滚动容器：desktop 100vh + window overflow:hidden，
     块级布局下 content 高度不限，若不在此滚动则整页无法竖向滚动 */
  .body { display: block; height: calc(100vh - var(--titlebar-h, 48px));
    overflow-y: auto; -webkit-overflow-scrolling: touch; }
  .sidebar { width: auto; max-height: 0; overflow-y: auto;
    transition: max-height .25s ease;
    border-bottom: 1px solid var(--sidebar-border); }
  .sidebar.open { max-height: 72vh; }
  .shell-menu { cursor: pointer; }
  .content { padding: 14px 12px 20px; }
  .toolbar { flex-direction: column; gap: 10px; margin-bottom: 12px; }
  .h1 { white-space: normal; }
  .src-seg { flex-wrap: wrap; }
  .ph-search { width: 100%; max-width: 480px; }
  .browse-pv { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .browse-pv table { min-width: 860px; }
  .browse-pv th, .browse-pv td { white-space: nowrap; }
  /* PrimeVue Dialog 内联定宽（1000/880px）：媒体查询 !important 覆盖 */
  .p-dialog { width: calc(100vw - 16px) !important;
    max-width: calc(100vw - 16px); }
  .p-dialog-content { overflow-x: auto; }
  .p-dialog table { min-width: 680px; }
  .dca-scatter-chart { height: 430px !important; }
  .dca-matrix-wrap .dca-matrix { min-width: 560px; }
}
</style>
