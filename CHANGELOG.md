# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，语义化版本。
（注：本仓库 2026-09-28 重建，v0.1.0 之前的本地历史未迁移。）

## [Unreleased]

### Added
- **教学页股票代码改为可搜索下拉**：PrimeVue AutoComplete（带下拉按钮），
  进页一次性拉取全市场清单（新增 `GET /api/stocks/basic`，仅 code+name），
  本地按代码/名称即时过滤（输入 600519 或「茅台」均可），选中真实存在的
  股票；手输任意代码仍允许（清单拉取失败的兜底）；详情页「用该股教学回测」
  联动带入不变。后端 2 用例（形状/降级），E2E 2 用例（搜索选中带码回测/
  下拉列清单），全套 218 后端 + 29 E2E 全绿。
- **量化教学页（六大经典回测策略 + vectorbt 回测引擎）**：教学页从占位重构为
  「经典回测策略教学」——买入持有/双均线交叉/通道突破（海龟）/布林带均值回归/
  网格交易/动量轮动（横截面多标的），每个策略 = 原理卡 + 教学点/常见坑 +
  参数面板 + 一键回测 + 结果可视化。
  - **回测引擎**（backend/app/backtest/，vectorbt 1.1.1 撮合 + 纯 pandas
    信号/指标层）：数据读 daily_bar 不复权日线；区间四档（近1/3/5年/全史）；
    **撮合模式可切换**——当日收盘（未来函数作弊模式）vs 次日开盘（正确），
    切换对比直观展示未来函数的收益作弊幅度；成本可调（佣金/卖出印花税，
    vectorbt 1.x 无卖出差分费用，印花税按双边均摊近似）；**pandas 锁 <3**
    （vectorbt 1.x 与 pandas 3 不兼容：numba 收到 object 数组）。
  - **API** POST /api/backtest：策略枚举 + 参数白名单（未知参数名丢弃防
    500）+ 轮动池 <2 只 400 + PG 不可达降级 200 + 区间无数据空态 message。
  - **前端**：策略导航/原理卡 + 参数面板 + 7 项指标卡（总收益/年化/最大回撤/
    夏普/胜率/盈亏比/交易次数）+ ECharts 净值曲线（策略 vs 买入持有对照）+
    回撤副图 + 交易明细表 + 疑似除权跳空警示（不复权局限明示）；个股详情页
    「用该股教学回测」入口带入代码。
  - 测试：后端新增 33（信号层合成数据手算/指标成本精确断言/数据与 API 契约/
    vectorbt 撮合成交价与双模式收益差）共 216 全绿；E2E 新增 test_learn_page
    5 用例，全套 26/26 两轮稳定。
- **个股历史行情详情页（数据页行点击进入）**：数据页/数据源子页的表格行
  可点击（hover 高亮 + 手型），进入该股详情子页——
  - **K 线图**：ECharts 6.1.0 按需引入（candlestick + bar + grid/tooltip/
    dataZoom，bundle 增量约 +350KB），红涨绿跌蜡烛 + 成交量副图（涨跌同色），
    固定近 250 个交易日，滚轮缩放/拖动平移/底部滑块；
  - **全字段明细表**：日期/开/收/高/低/涨跌额/涨跌幅/振幅/成交量(手)/成交额/
    换手率%（新浪源另带流通股本），最新日期在上，每页 50 独立分页可翻全史；
  - **来源切换**：详情页 新浪/东财 分段切换，`/api/stocks/{code}/bars` 新增
    `source` 参数（(code,trade_date,source) 主键下同一天不再混返两源；缺省
    不过滤兼容旧行为），SELECT 补 `outstanding_share` 字段（新浪专有）；
  - **导航与状态**：「← 返回列表」或侧栏「数据」返回，列表的搜索词/页码/
    来源 Tab 全保留（数据页 DataTable 改受控分页 `:first` 修复 v-if 卸载
    重挂后 PrimeVue 内部分页状态归零）；入口来自数据源子页时详情默认沿用
    该来源，否则默认新浪；降级沿用项目约定（源无数据空态 / PG down 文案）。
  - E2E 新增 test_stock_detail 5 用例（行点击/返回状态/来源切换与空态/
    明细分页/无 PG 降级）+ 后端 6 用例（source 过滤/兼容/outstanding_share/
    端点透传）。
- **爬虫任务管理（每日数据更新系统内自治调度）**：每日增量不再依赖外部计划
  任务（NAS DSM cron），由 backend 进程内调度循环自治执行；前端新增「任务」
  主 Tab（与 数据/回测/教学 并列）做任务全生命周期管理。
  - 调度器（`app/scheduler.py`，自研 asyncio 循环，零新依赖）：每日固定时刻
    （北京时间 `Asia/Shanghai`，不辨交易日——非交易日照常执行，增量断点机制
    保证空转无害）；**启动补跑**（到点时恰在重启/关机 → 启动时补跑一次，
    trigger=catch_up）；**全局串行 + 排队**（同一时刻只跑一个任务，防封限速
    按全局通道设计不容许并行加倍请求）；**PG advisory lock 跨进程互斥**
    （防手动 CLI 与调度任务并发写库、防未来多 worker 重复调度）；执行委托
    `CrawlRunner`（防封参数内置安全默认值不可配——限速/抖动/分批/熔停配置
    错误有实际封 IP 事故史）。`SCHEDULER_ENABLED=0` 可整体关闭。
  - 任务模型：可创建多任务，字段仅 数据源（sina/eastmoney）/模式
    （incremental/full）/执行时刻（HH:MM）；预置「新浪每日增量 18:00」默认
    禁用（开开关即用）；任务定义存 PG `crawl_task` 表（重启恢复），运行
    历史复用 `crawl_run` 并新增 `task_id`/`trigger_kind`（manual/schedule/
    catch_up——「今日已计划执行」只认后两者，手动触发不抑制当晚定时）；
    `CrawlRunner` 新增协作式 `stop_event`（停止派发、优雅落库、不算失败）。
  - API（`/api/tasks`）：查询始终 200（PG down 在 body 降级表达，同
    /api/infra 约定），写操作标准状态码（404/409/422）；CRUD + run/cancel/
    stop + 单任务历史；删除运行中任务拒绝（先停止再删）、排队中删除自动
    移出队列。
  - 前端「任务」页：任务卡片（名称/源 Tag/模式/时刻/相位徽章/启用开关/
    下次执行/上次结果）、运行中实时进度条（3s 轮询）、相位对应操作
    （空闲→立即执行、排队中→取消排队、运行中→停止）、历史表格、
    新建/编辑对话框（东财源与全量重爬展示风险提示文案）、删除二次确认。
    E2E 新增 test_tasks_page（mock /api/tasks，10 用例：导航/列表渲染/
    新建/编辑回填/启停/相位操作/历史/删除确认/风险提示/无 PG 降级）+
    后端 46 个单测（task_store/scheduler/tasks_api/runner 停止信号）。
- **数据源子页（按来源筛选，对应 Carbon 原型 ⑥⑦ 屏）**：侧栏「新浪财经」
  「东方财富」改为可点导航，进入数据浏览占位页的来源筛选变体：标题
  「数据浏览 · <来源>」、来源 Tag（新浪蓝 #0F62FE / 东财绿 #24A148 白字）、
  副题带来源统计（有爬虫数据时显示「<来源>源 K线 N 根」，无数据降级文案）；
  父项「数据」在子页保留选中高亮（灰底无箭头）。三主题通用。原型侧同步：
  新增 ⑥⑦ 两屏 + 数据源两行行高 40→32px 收紧（七屏统一）+ 清理画布角落
  6 条陈旧文本 + 修正 ⑤ 屏 chevron 残留在未选中「仪表盘」行。E2E 新增
  test_source_pages（子页选中态/Tag/行高/文字垂直居中/父项高亮）。
- **IBM Carbon 第三套界面风格**：风格切换扩为「苹果 / Fluent 2 / Carbon」三选，
  **按 Penpot 原型「honey-money · IBM Carbon 风格仪表盘」逐项落地**（Carbon v11
  productive 规范 + 原型校准色板）：深色 UI Shell 头 48px #161616（汉堡菜单 +
  品牌白字 + 副题浅灰 + 设置图标，切换控件入深色头）、侧栏 256px 白底 1px #E0E0E0
  右边框（48px 导航行、选中灰底 #E0E0E0 + 右缘 chevron、分组分隔线、12px 脚注）、
  内容区 #F4F4F4 + 28px 常规字重大标题、方角 1px 边框白卡（无阴影无毛玻璃）、
  状态徽章 点+12px 文字（#24A148/#DA1E28）、爬虫卡实心 Tag（蓝 #0F62FE /
  黄 #F1C21B / 红 #DA1E28）、8px 方角进度条（#E0E0E0 轨 / #0F62FE 填充）、
  蓝主按钮 96×40 白字方角（禁用 #C6C6C6）、错误态 #FFF1F1 内联通知（实心红
  圆叉图标 16px + 加粗标题 + 同行正文 14px）、错误降级卡红 Tag 居中、空态
  命令 chip 灰底带 `$` 前缀、数据页 DataTable（48px 表头 12/600 #525252 +
  1px 行线、40px 搜索框 1px #8D8D8D、虚线 K 线占位、蓝色 Placeholder Tag）、
  字体栈 IBM Plex Sans → 系统回退。index.html 首帧预处理支持 carbon。
  E2E 新增 Carbon 主题外壳断言（深色头/侧栏/方角/主按钮/表头/Tag/持久化）。
- **应用图标（favicon / PWA）**：按 Penpot「App Icon」设计（蜂蜜金渐变圆角块
  + 白蜂巢 + 三根上升K线）生成矢量 SVG + 16/32/180/192/512 PNG；接入
  `index.html`（SVG favicon + PNG 兜底 + apple-touch-icon + theme-color）与
  `manifest.webmanifest`（PWA 安装图标）。E2E 补图标/manifest 资产断言。
- **界面风格切换**：一键切换「苹果风格 / Fluent 2」（标题栏/工具栏分段控件，
  apple 与 fluent 各有一份、按主题显示），localStorage 持久化 + index.html
  首帧预处理（刷新不闪回）。全部样式 token 化（html[data-theme] 切 CSS 变量），
  苹果主题像素级保持原样；**Fluent 2 主题按 Penpot 原型「04 · Fluent 2 风格
  仪表盘」真稿逐项校准**：无标题栏、侧栏 260px 白底 + app item 与四项导航图标、
  导航浅灰行（#F0F0F0）+ 左侧品牌蓝竖条选中指示（4×16 #0F6CBD 居中）、状态徽章化
  （#107C10 绿/#C50F1F 红/#8A3707 棕/#0F6CBD 蓝 点+文字）、2px 细进度条
  （#E6E6E6/#0F6CBD）、K线来源改文字行（去 chips）、无边框双层阴影白卡
  radius 8、28px 常规字重大标题、内容区 #E8E9EA、错误条 #FDF3F4/#C50F1F。
  E2E 新增主题切换/持久化/徽章断言。第二轮全五屏对比修正：加载态整卡灰块
  骨架、错误态 #FAFAFA 降级卡 + 红字「数据不可用」、数据页单列
  （搜索 480 / 行情表 / K线图占位条 160）。

### Fixed
- **教学页「跑回测」报 SyntaxError（HTML 响应）**：首次回测触发 numba JIT
  编译（部署后第一次可达数十秒），超过 nginx 反代默认 60s 读超时 → 504 HTML
  错误页被前端 json() 解析崩。三层修复：① nginx `/api/` 反代放宽
  read/send_timeout 至 300s；② backend 启动时后台预热 numba 编译（微型回测，
  部署后首次用户回测从数十秒降到秒级）；③ 前端对非 JSON 响应给出友好提示
  （E2E 回归：mock 504 HTML → 断言错误横幅而非崩溃）。
- **增量爬取的首根K线涨跌额/涨跌幅/振幅全空（2026-10-09 全市场 5208 根踩坑）**：
  增量断点 beg 恰作为 akshare start_date，返回数据从 beg 当天起——df_to_rows
  的「保留前收」递推拿不到任何前收行，每天增量新增的首根（=当天最新）三字段
  全 NULL（全量爬取不受影响，故 10-08 及以前完好）。修复：取数起点前移 15 个
  自然日缓冲（kjs 接口本就全量取回后过滤，零额外成本），缓冲行在 df_to_rows
  中被过滤但参与前收递推；回归测试守护「增量首根三字段必有值 + start_date
  必带缓冲」。历史空值行由 ops 一次性 SQL 回填（同源前交易日 close 现算），
  并清理 2 只股票的未来日期脏数据（603100/605589 的 10-12/10-13 假K线）。
- **任务页相位徽章与启用开关重叠（用户过目发现）**：相位徽章复用了仪表盘
  状态卡的 `.fbadge` 样式（`position: absolute` 右上角定位，`.dot` 同属该
  体系），在任务卡内脱离文档流、绝对定位到卡片右上角压住启用开关。任务页
  改用专属 `.phase-badge`（inline-flex 文档流内，独立 `.pdot` 圆点样式）
  与仪表盘徽章体系彻底解耦；E2E 回归断言徽章 `position=static` 且与开关
  边界框不相交（tests/e2e/test_tasks_page.py::test_phase_badge_not_overlapping_switch）。
- **数据源 Tab 过滤未生效**：侧栏「新浪财经/东方财富」子页此前只是换标题
  的同一份列表（后端无来源参数）。`/api/stocks` 新增 `source` 参数
  （EXISTS 按 (code,trade_date,source) 主键过滤，深库毫秒级），前端 Tab
  传参；空态区分"该来源暂无入库数据"。实测：sina=5,223 只、eastmoney=0 只
  （东财源尚未有数据入库，与爬虫运行史一致）。
- **数据浏览深分页慢查询（8~10s → 0.11s）**：列表 SQL 的 LATERAL 聚合
  作用于全表分页前（对大表 1,667 万行的 daily_bar 数千次聚合），改为两段式
  ——先在 stock_basic 小表分页，再仅对当页 ≤200 只聚合；daily_bar 补
  `(code)` 单列索引（init_schema 幂等创建）。
- **爬虫统计瞬时失败误报"数据不可用"**：大批量入库期间 /api/crawl/stats
  偶发超时被前端定格为不可用。改为自动指数退避重试（最多 3 次）+ 卡片文案
  区分"统计暂不可用（后台入库不受影响，重试中）"。
- **爬虫全 run 单事务（终极根因）**：全代码仅清单入库后一处 commit，逐股
  INSERT 的数百万行全在一个巨型事务里、跑完才提交——库内进度长期不可见
  （仪表盘不动）、锁持有到结束（并发实例全卡 init_schema）、崩溃全丢。
  runner 增加 `on_batch` 钩子，批次边界（默认每 300 只）conn.commit +
  `[commit]` 日志。回归：批边界回调序列断言。
- **新浪日线爬虫在 NAS 上永久停摆**：akshare 同步库内部 requests 不带超时，
  特定请求在 NAS 网络下挂起 → `to_thread` 永久阻塞 → 事件循环停摆 + PG
  事务悬空锁表（后续实例全部卡在 init_schema 等锁）。外层加
  `asyncio.wait_for` 60s 兜底，挂起降级为单股失败交熔停机制。
- **Carbon 侧栏数据源行文字贴顶/行距过大（用户过目发现）**：`src-item` 曾
  `display:block`，40px 行内文字贴顶不居中，且两行行距观感过大。改
  `display:flex; align-items:center` 并按原型行高 40→32px（与 Fluent 行为一致）。
- **数据页搜索框放大镜图标撑爆搜索框（三主题共用既有 bug，用户过目发现）**：
  `.mag` SVG 只有 viewBox 无显式宽高，被渲染成 1126×1126 巨图、搜索框跟着
  撑到 1150px 高。固定 16×16。全量体检页面所有 SVG 尺寸确认无同类漏网；
  E2E 补数据页图标/搜索框尺寸回归断言。
- **错误横幅警告图标撑满页面（三主题共用既有 bug）**：`.warn-icon` SVG 只有
  viewBox 无显式宽高，后端不可用横幅出现时被默认尺寸规则渲染成千像素巨图、
  挤垮整页布局（本地后端常在线 + E2E 后端在线，从未被看见）。给
  `.warn-icon`/`.err-filled` 固定 18×16 / 16×16。E2E 补拦截 /healthz 的
  图标尺寸回归断言。
- **苹果主题状态文字重复显示**：徽章（fbadge）内的文本节点在
  `display:contents` 下仍随文档流渲染，与大字状态各出现一次；文字包进
  `.fb-txt` 并在苹果主题隐藏，Fluent 徽章不受影响。E2E 补防重复回归断言。
- **流水线镜像拉取卡死（3370）**：本网络直连 Docker Hub/公共镜像加速全部
  挂起，NAS 构建镜像缓存被清理任务清掉后 `docker build` 拉基础镜像无限
  卡死。CI 在构建/部署前经国内镜像源轮换预取基础镜像并 retag 成规范名
  （Dockerfile/compose 保持官方名不动），backend-test 与 deploy-nas 增加
  40min 作业超时防挂死。
- **构建期包源龟速**：pypi.org 官方 CDN 实测 ~14KB/s、registry.npmjs.org
  ~135KB/s，akshare 全家桶根本装不完；Dockerfile 固定 pip 走阿里源、
  npm 走 npmmirror（实测 MB/s 级）。

### Changed
- **东财源默认锁 drip 节奏**：每 30 秒 1 请求（±30% 随机抖动 → 实际 21~39s），
  `min=max=initial=1/30` QPS 锁死，AIMD 不升不降（出口 IP 曾被封，宁慢勿封）；
  清单加载同节奏。新增 `--min-qps` 参数；新浪源节奏不变。测试锁默认值 +
  抖动间隔界 + AIMD 不变速三重守护。
- **代理池卡片显示在池数量（对齐原型）**：`/api/infra` 探活成功后加取
  `/count`（jhao104 协议 `[可用, 总数]`），响应新增 `count`/`count_useful`
  字段、detail 改为「host:port · 在池 N」（原先输出原始 JSON）；计数取不到
  静默降级 count=null。前端苹果主题状态位显示「已连接 · 在池 N」，Fluent
  徽章保持短文案、计数在详情行。后端补计数三用例（正常/404/坏格式）。
- **代理池切换为 NAS 公共实例**（容器 4→2）：删除编排内自带的
  proxy_pool/redis 服务，backend `PROXY_POOL_URL` 指向宿主公共代理池
  （`proxy-pool:2.4.1-aclfix`，5010，多项目共用，`.env` 可覆盖）；
  CI 预取清单与 nas-diag 端到端探活同步更新。
- **外壳适配 web 端**：移除标题栏 macOS 红绿灯三按钮；去掉"桌面壁纸 +
  浮动窗口"的四周留白/圆角/投影，改为全幅铺满视口（内容区内部滚动）。
  E2E 外壳断言同步更新（无红绿灯 + 窗口盒等于视口）。
- **前端按 macOS 风格 Penpot 原型重写**（App.vue 单文件，数据层逻辑不变）：
  macOS 窗口壳（渐变壁纸/红绿灯标题栏/半透明侧栏导航：仪表盘·数据·回测·教学）+
  毛玻璃卡片 + 四状态完备（加载骨架 / 空态微场景插画：显示器+爬虫机器人 SVG /
  错误横幅：矢量警告三角 / 降级虚线占位）；数据·回测·教学为 Placeholder v0.2+ 占位页。
- E2E 新增 macOS 外壳与导航切换断言（红绿灯/侧栏四项/占位页标记）。

### Added
- **新浪数据源**（默认源，`--source sina`）：akshare 新浪日线（kjs 全史，含
  换手率/流通股本）+ 自研清单分页；字段口径统一（volume 手、换手率百分数、
  振幅/涨跌幅基于前收现算）；清单完整性防护（空页重试 + 官方总数校验，
  截断即报错绝不静默漏爬）。
- **数据来源标注**：daily_bar 主键升级 (code, trade_date, source)，多源数据
  并存不覆盖，可直接 SQL 对比；crawl_run/crawl_stats/前端卡片均带来源。
- **防封四件套**（默认开启）：限速间隔随机抖动 ±30%、分批批间休眠
  （300 只/30s）、连续 15 只失败自动熔停保存退出、按源隔离断点续爬。
- 仪表盘新增「爬虫数据」卡片（K线总数/覆盖进度/待爬/来源分布/最近运行）
  + 后端 `GET /api/crawl/stats`；表未建显示"爬虫未运行"，PG 不可达优雅降级。
- 东财 A 股历史日线爬虫（backend/app/crawler/，CLI：`python -m app.crawler`）：
  - 沪深 A 股全量清单（clist 分页，实测 5561 只）+ 全史日线（push2his
    单请求全史，fqt=0 不复权）；
  - 按域名分通道动态限速：滑动窗口成功率 AIMD（升/降速）+ 连续失败熔断
    冷却（数字前缀子域归并同通道）；
  - 代理池经纪：jhao104/proxy_pool `/get/` 取代理、失败 `/delete/` 降分、
    成功复用、空池/池挂自动回退直连；`--proxy-mode direct|pool` 可切换；
  - PostgreSQL 落库：stock_basic / daily_bar（(code,trade_date) 主键幂等
    覆盖更新）/ crawl_run 运行汇总；断点续爬（库内每股最新日期 +1 续拉）；
  - 失败隔离：传输层失败换代理重试，单股最终失败仅记录不中断整体；
    退出码区分成败便于计划任务告警；
  - 测试：限速器/代理经纪/东财客户端/存储/编排/CLI 共 57 个离线用例
    （backend/tests/test_crawler_*.py，MockTransport/假时钟全离线）。

### 决策记录（实测驱动）
- 东财 WAF 实测断连免费代理的机房 IP 段（19 个免费代理 0 成功），家宽直连
  低频可靠但单 IP 频控严格（短时密集请求触发分钟级封禁）→ 生产建议
  `--proxy-mode direct --initial-qps 1`；代理池保留给未来其他数据源。
- 传输层异常分类：ProxyError/ConnectError 归代理问题（换代理重试，不计通道
  降速）；RemoteProtocolError/Timeout 归目标层瞬时故障（换代理 + 降速）。

## [0.1.1] - 2026-09-28

### Changed
- PostgreSQL 改用 NAS 上既有容器（宿主 5433，库/用户 honey_money）：
  移除编排内 postgres 服务与 pg_data 卷，backend 经
  extra_hosts host.docker.internal:host-gateway 直连；DATABASE_URL 可用
  .env 覆盖。nas-diag 增加 /api/infra 端到端检查（PG+代理池状态）。

## [0.1.0] - 2026-09-28

### Added
- 工程化骨架（monorepo）：backend（FastAPI 最小 API：/healthz、/api/infra
  基础设施探测，PG/代理池不可达时优雅降级）+ frontend（Vue3+Vite 服务状态
  仪表盘，nginx 反代 /api）。
- docker-compose 五服务编排：frontend（唯一对外端口，默认 8080）/ backend /
  postgres:16（healthcheck + 持久卷）/ redis:7 / jhao104 代理池；.env.example。
- backend/.frontend 各自多阶段 Dockerfile 与 .dockerignore。
- GitLab CI：backend-test 流水线（pytest）。
- 测试：后端 6 个离线用例 + Playwright E2E 1 条（动态端口自拉双服务，
  覆盖页面渲染与优雅降级路径）。

### 决策记录
- PostgreSQL 完全替代 SQLite（storage 层后续用 SQLAlchemy 重写）。
- 单端口暴露策略：仅前端对外，其余服务内网互通。
- vite preview 在 Node 26/Windows 默认仅绑 IPv6 ::1，E2E 显式 --host 127.0.0.1。
