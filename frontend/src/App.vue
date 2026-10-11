<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'
import Paginator from 'primevue/paginator'
import AutoComplete from 'primevue/autocomplete'
import * as echarts from 'echarts/core'
import { CandlestickChart, BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([CandlestickChart, BarChart, LineChart, GridComponent, TooltipComponent,
  DataZoomComponent, CanvasRenderer])

// ===== 数据层（沿用原仪表盘逻辑：三接口并发 + 优雅降级）=====
const backend = ref({ status: 'checking' })   // 后端进程本身
const infra = ref(null)                       // PostgreSQL / 代理池
const crawl = ref(null)                       // 爬虫数据统计
const pendingBackend = ref(true)              // 三路探测各自独立结算（渐进上屏）
const pendingInfra = ref(true)
const pendingCrawl = ref(true)
const activeNav = ref('dash')                 // dash | data | src-sina | src-ef | backtest | learn

// 数据源子页（对应 Carbon 原型 ⑥⑦ 屏）：按来源筛选的数据浏览占位页
const SRC_ITEMS = [
  { id: 'src-sina', label: '新浪财经', field: 'sina', tagCls: 'src-sina' },
  { id: 'src-ef', label: '东方财富', field: 'eastmoney', tagCls: 'src-ef' },
]

function refresh() {
  // 三路探测各自独立结算，谁先回来谁先上屏——/api/infra 要探 PG/代理池、
  // /api/crawl/stats 走大表统计，慢的一路不再拖着整页骨架空转
  pendingBackend.value = pendingInfra.value = pendingCrawl.value = true
  fetch('/healthz').then(r => r.json())
    .then(hz => { backend.value = hz })
    .catch(e => { backend.value = { status: 'down', detail: String(e) } })
    .finally(() => { pendingBackend.value = false })
  fetch('/api/infra').then(r => r.json())
    .then(inf => { infra.value = inf })
    .catch(() => { infra.value = null })          // null → PG/代理池按未连接显示
    .finally(() => { pendingInfra.value = false })
  fetch('/api/crawl/stats').then(r => r.json())
    .then(cr => { crawl.value = cr })
    .catch(() => { crawl.value = null })          // null → 爬虫卡按数据不可用显示
    .finally(() => { pendingCrawl.value = false })
}

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
const browseTotalPages = computed(() =>
  Math.max(1, Math.ceil((browse.value.total || 0) / BROWSE_PAGE_SIZE)))

async function loadBrowse() {
  browseLoading.value = true
  try {
    const q = encodeURIComponent(browseQuery.value.trim())
    const off = browsePage.value * BROWSE_PAGE_SIZE
    const src = srcPage.value ? `&source=${srcPage.value.field}` : ''
    const r = await fetch(`/api/stocks?q=${q}&limit=${BROWSE_PAGE_SIZE}&offset=${off}${src}`)
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

const TASK_SOURCES = [{ v: 'sina', label: '新浪财经' }, { v: 'eastmoney', label: '东方财富' }]
const TASK_MODES = [{ v: 'incremental', label: '增量续爬' }, { v: 'full', label: '全量重爬' }]
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
                       taskMode: t.mode, schedule_time: t.schedule_time }
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
  if (!t.next_run_at) return '已禁用'
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
  if (t.source === 'eastmoney')
    return '东财源 drip 模式：每 30 秒 1 请求（出口 IP 曾被封），全市场一轮约需 40+ 小时，仅作解封后校验/补数用'
  if (t.mode === 'full')
    return '全量重爬：每轮 5200+ 请求、约 1~2 小时，确认需要重建数据时再开启'
  return ''
}
const dialogRisk = computed(() => {
  const d = taskDialog.value
  if (!d) return ''
  if (d.source === 'eastmoney')
    return '东财源 drip 模式：每 30 秒 1 请求（出口 IP 曾被封），全市场一轮约 40+ 小时，仅作解封后校验/补数用'
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
  activeNav.value = 'stock'
}
const stockNav = ref('data')       // 详情页返回目标（数据页/来源子页）

function backToList() {
  activeNav.value = stockNav.value || 'data'
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
             { k: 'top_k', label: '买前 K 名', type: 'number', min: 1, max: 10 }],
}
const DEFAULT_PARAMS = {
  buy_hold: {}, ma_cross: { fast: 5, slow: 20 }, donchian: { entry_n: 20, exit_n: 10 },
  bollinger: { window: 20, alpha: 2.0 }, grid: { step_pct: 5.0, levels: 4 },
  momentum: { window: 20, top_k: 2 },
}
const learnStrat = ref('ma_cross')
const learnCodePick = ref([])         // 多选标签：[{code, name, label}]（1 只=单股模式）
const batchProgress = ref(null)       // {done, total} 批量进度
const batchResults = ref([])          // 批量模式每股结果
const batchFailed = ref([])           // 批量失败清单 [{code, name, reason}]
function resolveCodes() {
  return learnCodePick.value
    .map(x => (typeof x === 'string' ? x : (x?.code ?? '')).trim())
    .filter(Boolean)
}
const batchMode = computed(() =>
  learnStrat.value !== 'momentum' && resolveCodes().length >= 2)
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
  if (nav === 'learn') loadCodeOptions()
})

const learnStratMeta = computed(() => STRATS.find(s => s.id === learnStrat.value))
const learnIsMomentum = computed(() => learnStrat.value === 'momentum')

watch(learnStrat, id => { learnParams.value = { ...DEFAULT_PARAMS[id] } })

function openLearnWith(code, name) {
  learnCodePick.value = [{ code, name: name || code,
                           label: name ? `${code} ${name}` : code }]
  activeNav.value = 'learn'
}

function _pickToCode(x) {
  return (typeof x === 'string' ? x : (x?.code ?? '')).trim()
}

const fmtSignedPct = v => (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'

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
  try {
    if (!batch) {                                    // 单股 / 轮动（一次请求）
      const bodyCodes = learnIsMomentum.value ? codes : [codes[0]]
      const raw = await _runOne(bodyCodes[0], {}, true)
      if (raw.ok === false) throw new Error(raw.reason)   // 参数/网关异常 → 横幅
      learnResult.value = { ...raw, codes: bodyCodes }
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
    codes: resolveCodes(),
    loading: learnLoading.value,
    error: learnError.value,
    hasResult: !!learnResult.value,
    batchN: batchResults.value.length,
  })
}
onMounted(() => window.addEventListener('resize', _onResize))

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
              { id: 'tasks', label: '任务' },
              { id: 'backtest', label: '回测' }, { id: 'learn', label: '教学' }]"
            :key="n.id" class="nav-item"
            :class="{ active: activeNav === n.id, 'sub-active': (activeNav.startsWith('src-') || activeNav === 'stock') && n.id === 'data' }"
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
                      <span class="chip time">每日 {{ t.schedule_time }}</span>
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
                <span class="ph-tag">教学 · 数据为不复权口径</span>
              </div>
            </div>

            <div class="learn-grid">
              <!-- 左列：策略导航 + 原理卡 -->
              <div class="learn-side">
                <button v-for="s in STRATS" :key="s.id" class="learn-nav"
                  :class="{ active: learnStrat === s.id }" @click="learnStrat = s.id">
                  <span class="learn-nav-name">{{ s.name }}</span>
                  <span class="learn-nav-tag">{{ s.tag }}</span>
                </button>
                <div class="card learn-card" v-if="learnStratMeta">
                  <div class="learn-brief">{{ learnStratMeta.brief }}</div>
                  <div class="learn-lessons">
                    <div class="learn-lesson-title">教学点 / 常见坑</div>
                    <ul><li v-for="l in learnStratMeta.lessons" :key="l">{{ l }}</li></ul>
                  </div>
                </div>
              </div>

              <!-- 右列：参数面板 + 结果 -->
              <div class="learn-main">
                <div class="card learn-params">
                  <div class="learn-params-row">
                    <label class="learn-field" :class="{ wide: learnIsMomentum }">
                      <span>{{ learnIsMomentum
                        ? '轮动股票池（可搜索多选，≥2 只）'
                        : '股票代码（可搜索多选：选 1 只单股回测，选多只自动批量对比）' }}</span>
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
                      <input :type="f.type === 'number' ? 'number' : 'text'" class="learn-input num"
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
                      {{ learnLoading ? '回测中…' : '▶ 跑回测' }}</button>
                  </div>
                </div>

                <div v-if="learnError" class="error-banner"><span>{{ learnError }}</span></div>

                <div v-if="batchMode && learnLoading && batchProgress" class="card learn-empty">
                  <div class="state">批量回测中… 已完成 {{ batchProgress.done }} / {{ batchProgress.total }} 只</div>
                  <div class="detail">每股独立回测（含复权因子懒加载），完成后自动生成对比表与叠加净值图</div>
                </div>

                <!-- 有单股结果或批量结果才渲染结果区（批量分支不写 learnResult） -->
                <template v-if="learnResult || batchResults.length || batchFailed.length">
                  <div v-if="learnResult && learnResult.status === 'down'" class="card learn-empty">
                    <div class="state">回测不可用</div>
                    <div class="detail">{{ learnResult.detail }}</div>
                  </div>
                  <div v-else-if="learnResult && learnResult.message" class="card learn-empty">
                    <div class="state">{{ learnResult.message }}</div>
                  </div>
                  <!-- 批量对比视图：每股一行指标 + 归一化净值叠加 -->
                  <template v-else-if="batchResults.length || batchFailed.length">
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
                <div v-else-if="!learnLoading" class="card learn-empty">
                  <div class="state">选好策略与参数，点「跑回测」开始</div>
                  <div class="detail">收盘出信号：当日收盘成交 = 未来函数（作弊），次日开盘成交 = 规矩——切换对比两者收益差</div>
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
  margin-top: 10px; padding: 8px 12px; font-size: 12.5px;
  background: var(--ph-tag-bg); color: var(--ph-tag-fg); border-left: 3px solid var(--dot-warn);
}
.task-actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.task-actions .btn { font-size: 13px; padding: 6px 14px; }
.btn-danger { color: var(--dot-down); border-color: var(--dot-down); }
.btn-danger:hover { background: var(--error-bg); }
.task-runs { margin-top: 16px; border-top: 1px solid var(--card-border-color); padding-top: 14px; }
.runs-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
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
.dialog-field > span { font-size: 12.5px; color: var(--fg-2); }
.dialog-select {
  height: 38px; padding: 0 10px; font: inherit; font-size: 13.5px;
  border: 1px solid var(--ph-input-border); background: var(--ph-input-bg); color: var(--fg);
}
.dialog .task-risk { margin: 4px 0 0; }
.task-error { margin: 8px 0 0; font-size: 12.5px; color: var(--dot-down); }
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
.learn-empty .detail { margin-top: 8px; }

.learn-cmp { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.learn-cmp th { text-align: left; padding: 8px 10px; background: #f4f4f4; color: var(--fg-2); font-weight: 600; }
.learn-cmp td { padding: 7px 10px; border-bottom: 1px solid #ededed; font-variant-numeric: tabular-nums; }
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
</style>
