# honey-money

A 股量化研究与交易系统（工程化骨架阶段）。目标形态：Vue 前端 + FastAPI 后端 +
PostgreSQL + 代理池，Docker Compose 一键部署（本地 / 群晖 NAS），GitLab CI 门禁。

> 当前版本 **0.1.0 是工程骨架**：五个服务的编排、健康检查与 CI 已就绪；
> 业务代码（AKShare 数据层 / Backtrader 回测 / 策略 / 教学页）在后续版本迁入。

## 仓库结构

```
backend/            Python 后端（FastAPI）
  app/main.py         应用工厂：/healthz 存活探针、/api/infra 基础设施探测、
                      /api/tasks 爬虫任务管理
  app/infra.py        PostgreSQL / 代理池 连通性检查（快速失败、优雅降级）
  app/crawler/        日线爬虫：rate(按域名动态限速) / proxies(代理池经纪) /
                      eastmoney(接口客户端) / sina(新浪客户端) / store(存储) /
                      runner(编排) / __main__(CLI)
  app/task_store.py   爬虫任务持久化（crawl_task 表 CRUD + 预置任务播种）
  app/task_runtime.py 任务运行态持久化（task_runtime 单表：队列/phase/进度/
                      停止指令落 PG——任务执行独立 worker 容器后 API 与
                      worker 跨进程共享的唯一介质）+ PgTaskControl（API
                      侧控制面：入队/取消/停止/快照/last_error）
  app/scheduler.py    任务调度器（每日到点/启动补跑/全局串行/advisory lock
                      互斥；运行态经 TaskRuntime 落 PG，生产宿主于 worker
                      容器，SCHEDULER_ENABLED=1 时兼容单进程模式）
  app/worker.py       任务执行 worker 入口（python -m app.worker：schema
                      兜底 + 遗留运行清理 + 调度常驻；backend 只留 API，
                      流水线重建 backend 不再打断运行中任务）
  app/startup.py      启动兜底清单（建表/补列/遗留运行清理——backend 与
                      worker 共用的单一事实源）
  app/tasks_api.py    任务管理 API（CRUD/触发/取消/停止/历史）
  app/dca_backtest.py 定投回测核心（脚本与调度器日更共用；定投方式六档
                      DCA_METHODS：amount 等额 / share 等份额 /
                      dd_ladder 回撤加码 / dd_ladder_deep 深回撤加码 /
                      profit_dd_ladder 浮盈阶梯（浮盈档优先+深撤加码）/
                      pl_ladder 盈亏阶梯（持仓浮亏 ≥10% 投 1.5×、≥20% 投
                      2×；浮盈 ≥10% 投 0.5×、≥20% 停投——两半边均持仓口径）
                      ——止盈状态机共用，日更按 method 全量计算；
                      脏检测日更 run_full_backtest_tp_dirty：逐基金输入
                      指纹（nav 聚合+basic+scale）比对，只重算有变基金，
                      周末/节假日零更新夜仅刷派生列；引擎版本串 =
                      ENGINE_VERSION_BASE+常量哈希（改方法/阈值/费率/
                      设置页口径自动全量），BACKTEST_FORCE_FULL=1 强制；
                      回测宇宙排除（2026-11-25 十四轮）：仅权益类 8 类
                      （BACKTEST_UNIVERSE_TYPES，dca_api.EQUITY_TYPES 同源）
                      且全史窗口 ≥ 运作期限下限的基金产生结果行——非权益/
                      年轻基金不参与回测（此前年轻基金仅年化置空），被排除
                      基金历史行由版本变化自动全量的 merge 全删重插清理；
                      时机表/波动指标表维持全市场口径
                      全量；重算单机并行：脏基金按 BACKTEST_CHUNK_SIZE
                      （缺省 250）切块 × BACKTEST_WORKERS（缺省 min(4,cpu)，
                      =1 串行）个 spawn worker 按空闲领取，worker 只加载
                      本分片净值、end_ts 等全局锚由父进程从指纹下发；
                      真机 400 只实测 4.1×（401.9s→97.2s），NAS 内存吃紧
                      时 BACKTEST_WORKERS=2 BACKTEST_CHUNK_SIZE=100）
  app/risk_metrics.py 基金风险/波动指标纯函数（年化/下行波动率、最大回撤、
                      夏普、60 日滚动波动率及历史分位；复权口径 √252 年化；
                      定投日更顺带全量重算落 fund_risk_metrics）
  app/pages_export.py Pages 静态快照导出（手动脚本与日更发布共用）
  app/pages_publish.py Pages 自动发布（clone→导出→commit/push）
  app/adjust.py       复权因子（懒加载入库；后/前复权合成；降级不复权）
  app/backtest/       vectorbt 回测引擎（信号/指标纯 pandas，六大经典策略）
  app/backtest_api.py 回测 API（策略/区间/撮合模式/成本，降级 200 约定）
  app/dca_store.py    定投止盈回测快照存储（fund_dca_result / fund_dca_meta 两表 +
                      fund_dca_tp_result / fund_dca_tp_rounds / fund_dca_tp_meta
                      多阈值三表（5%~40% 八档 × 定投方式 method 维度
                      （五档 amount/share/dd_ladder/dd_ladder_deep/
                      profit_dd_ladder，2026-11-23~24，与旧表同构 +
                      tp/method 维度，存量表 ALTER 就地迁移））+
                      fund_dip_timing 时机表（当前回撤/距前高/回撤桶，日更顺带刷新）+
                      fund_risk_metrics 波动指标表（近1/3/5年与全史四窗口，日更顺带刷新）+
                      fund_dca_fp 基金输入指纹表（脏检测日更：merge_* 按 code
                      范围替换、未变基金行保留；alive 全表刷新/时机死行清理/
                      分位回填集合 SQL）+
                      fund_dip_validation 起点条件验证表 / fund_bear_pause_fund
                      · _market · _meta 断缴实验三表（离线脚本导入，重跑整表替换）；
                      python -m app.dca_store <csv> 导入 scripts 离线回测产物，幂等）
  app/dca_api.py      /api/funds/dca 回测排行 API（搜索/在市/年限/规模/权益类
                      （EQUITY_TYPES 白名单）过滤/排序白名单/分页 + 在市全集
                      分位统计 + 口径 meta，降级 200 约定；行内附波动指标列
                      vol_1y/vol_down_1y/mdd_1y/sharpe_1y/roll_vol/vol_pctile
                      （LEFT JOIN fund_risk_metrics，可按其排序））+
                      /api/funds/dca/tp（多阈值排行，tp 白名单回落 0.2，
                      method 定投方式白名单回落 amount，同附波动指标列与排序）+
                      /api/funds/dca/tp/compare（八档止盈点市场级对比聚合，
                      权益类+商品池）+ /api/funds/dca/tp/{code}/rounds
                      （单基金单阈值轮次明细）+ /api/funds/dca/tp/{code}/summary
                      （单基金全阈值总收益率/年化汇总）+ /api/funds/dca/timing
                      （定投时机：当前回撤桶 × 历史止盈素质，band 白名单；
                      2026-11-17 表格合并后前端不再调用，端点保留 API 兼容）；
                      排行两端点行内附时机列 dd_current/days_since_peak/dip_band
                      （LEFT JOIN fund_dip_timing）并支持五维度区间筛选参数
                      （age/gap/xirr/dd/pr 边界，语义同 timing 端点）+
                      「最近一次止盈后」当前轮十字段 full_last_tp(+_disc/_nom)/
                      full_cur_invested/full_cur_profit_rate(+_disc/_nom)/
                      full_cur_xirr(+_disc/_nom)（全史窗口，2026-11-19；
                      summary 端点行内同附，弹窗按 tp/fee 切换取值）+
                      /api/funds/dca/pause-experiment（熊市断缴实验市场级：
                      子样本×口径 A/B/C/D 配对统计 + meta + 案例基金）+
                      /api/funds/dca/bear-pause/{code}（单基金四组对照）
  app/settings_store.py 应用级键值设置（app_settings 表，key/value/updated_at，
                      启动兜底建表；当前唯一设置项 backtest_min_span_days=
                      回测年化运作期限下限，整数 90~3650 默认 90 不可关闭，
                      修改后下一次回测生效——日更编排与样本回填脚本启动时
                      读取，参数化引擎 MIN_SPAN_DAYS 两处判定：全窗口年化与
                      止盈后当前轮年化）
  app/settings_api.py 设置 API：GET /api/settings（设置全集 + backtest_task
                      任务状态快照：预置「定投回测日更」任务 id/运行中判定
                      （crawl_run 末行 finished_at IS NULL）/增量-全量预估
                      （fund_dca_fp 指纹 engine 串 vs engine_version
                      (min_span_days)，fp 表缺失兜底全量；server_now 供
                      进度推算校时）供设置页按钮确认框时间提示与进度显示；
                      + method_task 快照与 POST
                      /api/settings/backtest-method-run（2026-11-25 九轮：
                      单策略全量运行——「单策略全量回测」预置任务
                      source='backtest_method'，mode 列=目标策略；method
                      白名单 400/日更运行中 409/任务排队运行中 409 前置
                      拒绝，改 mode 后返回 task_id 供前端复用任务系统
                      POST /api/tasks/{id}/run 触发）；
                      PG 不可达降级 status=down）+ PUT
                      /api/settings/backtest-min-span（非法值 400 带 detail；
                      库不可达降级 200/down）；「立即运行全市场回测」按钮
                      复用任务系统触发端点 POST /api/tasks/{id}/run
                      （advisory lock + 409 防重）
  app/bear_pause.py   熊市断缴实验引擎（bear_state 迟滞带熊市态
                      （回撤 ≤-20% 进入 / >-10% 恢复）+ A 坚持定投/
                      B 断缴闲置/C 断缴补投/D 熊市加倍四组模拟 + 市场级
                      配对聚合；校验锚 A≡simulate_multi 转正单测）
  app/index_store.py  指数日线存储（index_daily 表 symbol+trade_date 主键
                      幂等 upsert；上证指数 sh000001 + 沪深300 sh000300
                      双指数，东财指数 K 线免 Cookie，由「指数日更」任务
                      遍历写入：各按自己增量 beg=最近-10 日自然日重叠容错、
                      逐指数提交互不影响，空表全史；东财被风控时逐指数落
                      新浪指数日线兜底（sina.fetch_index_daily，kjs 全史
                      接口股→手对齐东财口径，2026-11-24 924 线停更修复））
  app/market_compare.py 519 vs 924 行情对比（/api/market/519-924：同一
                      指数序列按两政策日 T 对齐的累计涨幅线，T-280~T+1040
                      交易日偏移复刻中信研报图；T 日收盘基线 r(n)=close(T+n)
                      /close(T)-1，对齐纯函数 align_lines（脏数据 ValueError
                      →端点降级）；研报原图为 wind全A、免费渠道不可得，
                      上证指数同源口径页面注明，降级 200 约定）
  app/market_crises.py 金融危机史（/api/market/crises：七次危机的区间
                      统计+双口径走势点列；CRISES 常量为人工知识（区间
                      锚点经 scratch/crises_verify.py 按生产库核实），
                      slice_window/crisis_stats/crisis_points 纯函数——
                      基线=起点收盘、见底=窗口最低收盘、恢复=见底后首次
                      收复、耗时按交易日；沪深300 表空/未发布时代危机不
                      降级（hs300_available=False 逐危机表达），PG 不可达
                      /上证无数据/脏数据降级 200 约定）
  tests/              离线 pytest 用例（API 骨架 + 爬虫 + 任务调度）
frontend/           Vue3 + Vite 前端
  src/App.vue         服务仪表盘（Carbon 风格）：四状态（加载骨架/空态插画/
                      错误横幅/降级占位）+ 数据·任务·回测·教学页 +
                      数据源子页（新浪/东财来源筛选变体）+ 基金净值子页
                      （列表含类型/最新单位净值/日增长率/净值条数/区间 +
                      波动指标五列（年化/下行波动率、最大回撤、夏普·近1年 +
                      波动率历史分位，服务端排序白名单，fund_risk_metrics
                      日更快照，无指标行显示 —），点行进净值详情：单位+累计
                      净值双线走势 + 风险指标区（近1年五指标卡 + 四窗口
                      对照表，无快照显示待生成占位）+ 分页明细）+
                      定投止盈回测子页（全市场公募基金「月定投+止盈」
                      离线回测排行：顶部四组口径总开关（2026-11-23 三轮：
                      定投方式/止盈点 5%~40%/费用口径全部置页面
                      最前，切换后统计卡/止盈点对比/排行/散点分箱/详情弹窗
                      全部跟随；定投方式六档：等额/等份额
                      （每期固定到手份额、现金随净值波动）/回撤加码
                      （dd_ladder：买入日回撤 ≥10% 投 1.5×、≥20% 投 2×）/
                      深回撤加码（dd_ladder_deep：回撤 ≥20% 投 1.5×、
                      ≥30% 投 2×，浅跌不追投）/浮盈阶梯（profit_dd_ladder：
                      当轮浮盈 ≥10% 投 0.5×、≥20% 投 0.25× 浮盈档优先，
                      未触发落深回撤档——贵时少买深跌加码）/盈亏阶梯
                      （pl_ladder：持仓浮亏 ≥10% 投 1.5×、≥20% 投 2×，
                      浮盈 ≥10% 投 0.5×、≥20% 停投——两半边均持仓口径
                      市值/已投入-1，无回撤分量），
                      ? 图标点击弹出六种方式完整说明、悬停按钮看对应详情；
                      口径副标题已删，?mode= 深链接）+「止盈点对比」
                      市场级区块（八档中位/均值年化、正收益占比、止盈成功率、
                      平均轮次与间隔 + SVG 折线，权益类+商品池，点击行/圆点
                      切换排行阈值）+ 统一基金表格（2026-11-17 表格合并：
                      「定投时机」表并入排行表——当前回撤/距前高时机列由
                      后端 LEFT JOIN fund_dip_timing 提供，服务端排序；
                      2026-11-18 追加波动指标五列（年化/下行波动率、最大
                      回撤、夏普·近1年 + 波动率分位，LEFT JOIN
                      fund_risk_metrics 可排序，与数据页基金净值表同源）；
                      2026-11-19 追加「止盈后收益率/止盈后年化」两列
                      （最近一次止盈日重新定投的当前轮·全史窗口，可排序，
                      未止盈显示 —，与近10年列不可直接对比）；
                      列显示过滤 MultiSelect 自选显隐列（代码/名称/操作
                      恒显）；五维度区间筛选面板（运作年限/止盈间隔/年化/
                      当前回撤/总收益率，回撤输入正数幅度，服务端过滤，
                      原回撤桶按钮组与该维度重复已删；URL tband 参数移除））+
                      「起点条件回测」区块（三组对照/
                      回撤深度梯度/起点年份三段统计表：不同起点开始定投的
                      首轮止盈周期、成功率与年化分布，静态验证快照）+
                      「熊市断缴实验」区块（KPI 四卡 + 子样本分层表 +
                      案例基金表 + 止盈20%/无止盈口径切换，静态实验快照
                      scripts/bear_pause_import.py 落数据；详情弹窗新增
                      「断缴对照」四卡——这只基金上 A/B/C/D 四条时间线的
                      期末钱包/投入/年化）+
                      7 张统计卡 + 服务端排序表格，费率口径
                      三档切换联动排行/统计/散点/详情（默认含费·申购名义；
                      不含费/含费·申购一折可选，搜索栏与散点区各有开关），
                      权益类筛选默认仅权益类（股票型/偏股·灵活·平衡混合/
                      股票指数/海外权益 8 类白名单，搜索栏与散点区可切
                      全市场，排行/统计/散点/分箱联动），
                      搜索/运作年限与规模输入过滤（>N 年 / >N 亿）/分页/排序
                      同步 URL + 止盈间隔列 + 「详情」按钮，弹窗看每轮
                      止盈日/复权净值/投入/申购赎回费/净收益/累计净收益
                      + 风险指标卡排（年化/下行波动率、最大回撤、夏普·
                      近1年 + 当前滚动波动率及分位，navs 响应 risk 附带，
                      静态快照模式显示待生成占位）
                      （明细跟随所选阈值：fund_dca_rounds 三口径 102k 轮
                      + fund_dca_tp_rounds 八阈值×三口径轮次，费用口径随
                      全页开关联动；弹窗内可切换止盈点（局部状态不波及排行），
                      净值曲线叠加全部档位止盈三角、按档位着色、当前档高亮；弹窗内可切
                      费率口径（局部状态），统计卡含总收益率/年化 XIRR（近 10 年、
                      随止盈点与费率口径，数据源 /api/funds/dca/tp/{code}/summary）
                      + 「止盈后当前轮」四卡（2026-11-19：上次止盈日/本轮已投入/
                      止盈后收益率/止盈后年化，全史窗口，随弹窗止盈点与费率
                      口径联动，未止盈显示 —））；
                      下方「年限 × 规模 × 年化」
                      可旋转三维散点，年化口径三档切换（默认含费·申购
                      名义；不含费/含费·申购一折可选——申赎费用建模见
                      scripts/fund_dca_backtest.py 费率表）+
                      年限×规模分箱均值/中位双表，点击格子弹出格内
                      基金清单并可下钻单基金止盈明细
                      （原生 canvas 自绘引擎：拖拽旋转/滚轮缩放/悬停明细，
                      每基金一点）+ 年限段×规模段分箱统计矩阵与自动结论；
                      数据链路见 scripts/）；
                      「任务」页管理每日爬虫任务（启停/手动触发/实时进度/
                      历史/上次失败原因展示）；
                      数据页点行进个股详情（ECharts K线近250根 + 全字段
                      明细分页 + 新浪/东财来源切换，返回保留列表状态；
                      列表含「退市」列标已退市股，可按退市状态过滤
                      全部/仅在市/仅退市）；
                      「519 vs 924 行情」子页（/data/market-compare：
                      同一上证指数序列按 1999-05-19 与 2024-09-24 两政策日
                      T 对齐的累计涨幅双线，T-280~T+1040 交易日偏移复刻
                      中信研报图——KPI 四卡（924 已走 T+N/当前累计、519
                      同期、两线区间峰值）+ ECharts 双线（T 虚线与 0% 基准、
                      悬停显两线日期与涨幅 + 横轴 T±40 步进刻度 + 双 y 轴
                      左 5·19/右 9·24 各自量纲满幅、0% 刻度同高对齐、
                      轴标签按线着色）+ 历史脉冲行情叠加（2000 后 5 日爆发
                      ≥12% 的 10 次行情细线异色右轴同读、T 后 120 交易日、
                      图例可显隐，端点 episodes 字段）+ 口径注明（原图 wind全A 免费
                      渠道不可得，上证指数同源），924 线随「指数日更」
                      自动延长——东财被风控时新浪兜底，天天更新不停更）；
                      「金融危机史」子页（/data/crises：历史上七次金融危机
                      期间 A 股的表现——1997 亚洲金融危机/2001 国有股减持
                      熊市/2008 全球金融危机/2015-16 股灾/2018 贸易战/
                      2020 新冠/2021-24 地产中概熊市；危机总览表（区间/
                      最大跌幅/见底日/见底耗时/恢复耗时/事件数，点击行
                      切换，?c= 深链接）+ 单危机 KPI 四卡（基线/最大跌幅/
                      见底耗时/收复前高）+ ECharts 双口径走势图（上证红线
                      +沪深300 蓝线、灰色虚线关键事件标注、见底/收复
                      点缀线）+ 事件时间线；口径：基线=危机起点日收盘
                      （人工锚定收盘高点，生产库真实数据核实）、见底=窗口
                      内最低收盘、恢复=见底后首次收复基线、耗时按交易日；
                      沪深300 基日 2004-12-31（2005-01-04 起有日线），
                      1997/2001 两危机仅上证线并注明；危机区间与事件为
                      人工知识硬编码（backend/app/market_crises.py CRISES，
                      scratch/crises_verify.py 可复核））；
                      「设置」顶级页（/settings，侧栏第 6 项：应用级设置——
                      回测年化运作期限下限（天）数字输入 + 保存，非法值
                      前端拦截/后端 400、保存成功提示「下一次回测生效」；
                      「立即运行全市场定投回测」按钮：确认对话框按指纹
                      状态动态提示运行时间（增量约 10 分钟/全量 6~10 小时），
                      触发「定投回测日更」任务、运行中禁用 + 状态提示 + 进度
                      显示（基金级实际进度=已完成/需回测基金数，引擎分块
                      回调逐块落 task_runtime.progress；指纹比对阶段暂无
                      信号时回落已运行时长/预估耗时推算，server_now 校正
                      时钟偏差，停留页面每 30 秒自动刷新）+ 上次回测用时；
                      「运行单个策略全量回测」按钮（2026-11-25 九轮）：
                      六种定投方式选择器（默认最新加的策略）+ 确认框
                      （约 10 分钟~2 小时 ≈ 全量 1/6，仅替换该策略结果），
                      经「单策略全量回测」任务执行——只算选定方式全市场
                      8 档 × 3 费率，method 域替换 fund_dca_tp_* + alive
                      method 域刷新 + 对比快照重建（不写指纹/旧口径/时机/
                      风险表——引擎陈旧判定留给日更兜底全量），日更或本
                      任务运行中禁用 + 409；运行历史进任务页；
                      定投回测页口径文案（回测窗口/轮次不足 N 天年化留空）
                      动态绑定该设置值）；
                      教学页六大经典回测策略（vectorbt 引擎，撮合模式可切换
                      对比未来函数，净值对照买入持有）+ 单股/批量/全市场
                      三模式分段切换 + 🌐 全量回测（同策略全市场后台任务：
                      过滤面板 剔ST/最少K线数/含退市股/选股域——流通市值
                      区间+一手价上限（参考=区间首日收盘×流通股本，退市股
                      豁免市值维度）、实时进度可取消、
                      统计卡/Top20 叠加图/收益排行表分页排序/CSV 导出；
                      动量轮动在全市场为组合级（月末动量 top_k，单组合行
                      _PORTFOLIO，池护栏 max_universe 默认 1000））；
                      退市股历史补爬（--source delisted / 任务页手动任务，
                      两所退市名单 367 只 + 新浪 klc_kl 全史，修幸存者偏差）
  nginx.conf          生产：静态托管 + /api 反代后端
deploy/             （预留：将来放 NAS 专属编排变体）
tests/e2e/          Playwright E2E（自拉后端 uvicorn + 前端 vite preview）
docker-compose.yml  四容器编排（frontend/backend/worker/mcp；PG 与代理池走宿主）
scripts/nas/        NAS 运维脚本（busy_check.py：部署守门的运行中任务查询）
.gitlab-ci.yml      main 流水线：backend-test（pytest 全绿才可部署）+
                    deploy-nas（空闲守门）+ deploy-catchup-nas（每小时补部署，
                    需配 Schedules）
.env.example        环境变量模板（复制为 .env，改密码/端口）
docs/               调研文档（A 股日线数据源实测等）
scripts/            离线研究/数据管道脚本（不入 CI）：fund_scale_fetch*.py
                    （东财规模双通道 gmbd/pingzhongdata）/ fund_nav_backfill.py
                    （短历史净值回补）/ fund_full_sync_missing.py（fund_basic
                    全集差集同步——ETF 等）/ fund_dca_backtest.py（定投+止盈
                    全市场回测 → scripts/data/*.csv，gitignore，经 app.dca_store
                    导入后页面可见；含申赎费用三场景建模：gross 不含费/
                    disc 申购一折/nom 申购名义，赎回费按每笔持有天数阶梯；
                    simulate_multi 单遍多阈值状态机供日更 run_full_backtest_tp
                    八档止盈点共用，tp=0.20 档与旧引擎逐字段一致）/
                    bear_pause_import.py（熊市断缴实验数据导入：权益池
                    全量 × A/B/C/D 四组 → fund_bear_pause_* 三表，
                    重跑整表替换，~3 分钟）
```

## MCP server（agent 接入回测与行情）

`backend/app/mcp_server.py`：独立 MCP server（官方 mcp SDK），让
ZCode / Claude Desktop 等 agent 直接调用本应用。8 个工具：

- **全市场回测**（后台任务，分钟~小时级）：`run_full_backtest`（提交）→
  `get_backtest_run`（轮询）→ `get_backtest_rows`（明细排行）+
  `list_backtest_runs` / `cancel_backtest_run`。单标的策略输出逐股排行；
  选股域过滤 `filters` 可配 `min_market_cap`/`max_market_cap`（流通市值
  元，默认 20亿~200亿）、`max_lot_cost`（一手成本上限元，默认 1000 即
  股价 ≤10 元；参考时点=区间首日收盘×流通股本，退市股无股本数据豁免
  市值维度）；momentum 为组合级（月末/周末动量 top_k 等权轮动，params 可配
  `window`/`top_k`/`rebalance`（month/week），结果为单组合行
  `_PORTFOLIO`，池默认截断至 K 线数最多的 1000 只，`filters.max_universe`
  可调）；
- **单股/批量回测**（同步秒级）：`run_backtest`（含 momentum 轮动，≥2 只）；
- **行情查询**：`list_stocks`（搜索，含退市过滤）、`get_stock_bars`
  （日K，前复权/来源可选）。

大体积曲线自动降采样 ≤120 点；错误一律转含状态码与下一步指引的可读文本。

接入方式二选一：

① **stdio（本机 agent，子进程方式）**——agent 的 MCP 配置加
（目标后端可用 `HONEY_MONEY_BACKEND_URL` 覆盖，缺省 `http://127.0.0.1:8000`）：

```json
{ "mcpServers": { "honey-money": {
    "command": "<仓库>/.venv/bin/python",
    "args": ["-m", "app.mcp_server"],
    "cwd": "<仓库>/backend" } } }
```

② **streamable HTTP（远程 agent 直连 NAS）**——compose 里的 `mcp` 服务
（宿主端口 `${MCP_PORT:-28081}`），agent 配置直接填 URL：

```json
{ "mcpServers": { "honey-money": { "url": "http://<NAS内网IP>:28081/mcp" } } }
```

典型用法：全市场用 `run_full_backtest(strategy="ma_cross",
params={"fast":5,"slow":20}, range="3y")` → 轮询 `get_backtest_run` 至
`status=done` → `get_backtest_rows` 取排行；指定股票直接
`run_backtest(strategy, codes)` 同步拿结果。HTTP 传输无鉴权，仅限内网
（与 backend 同安全级别），勿对公网映射 28081。

## 本地开发

```bash
# 后端（仓库根）
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000 --reload --app-dir backend
# 验证：curl http://127.0.0.1:8000/healthz

# 前端（另开终端）
cd frontend && npm install && npm run dev    # 默认 5173，/api 已反代到 8000

# 本地过目/预览（构建产物 + 反代生产后端，无需本地起后端；
# 绑 0.0.0.0，局域网设备可用 http://<本机IP>:4180 访问）
npm run preview:prod                         # 反代 http://nas.example.com:28080
# 自定义后端：BACKEND_URL=http://<host>:<port> npm run preview
# 反代本地后端：BACKEND_PORT=<port> npm run preview（缺省 8000）

# 测试（后端单测 + E2E，全部离线可跑）
.venv/Scripts/python.exe -m pytest
```

## 定投回测日更（定时任务，纯代码）

预置任务「定投回测日更」（backtest/daily，**23:00，默认启用**）：串行队列
保证排在「基金每日增量」（21:00）之后——先更新净值、后子进程全量重算
2.6 万只（约 20-30 分钟）并直写结果表，「定投止盈回测」页即查即新。
任务页可随时手动触发/停用；净值爬取失败时按最新可得数据运行、次日自愈。
规模数据存 PG fund_scale 表（`python -m app.dca_backtest --import-scale
scripts/data/fund_scale.csv` 刷新，规模季度披露后手动更新一次即可）。

## 公开快照站（三渠道）：ZCode Sites / GitHub Pages / 内网

**ZCode Sites（每日自动跟随）**：https://fund-dca-backtest-hov2.zai.site 。
页面/数据分离：页面为打包产物，`/data/*` 由其静态服务器实时反代
GitHub Pages 最新快照（上游故障回退本地快照）——数据链路：
开发机 cron 23:50 `scripts/publish_snapshot.sh` 导出快照并推送
（Pages 重建后 zai.site 自动跟随）。前端代码变更时加 `--deploy-zcode`
手动重发 zai.site。

**GitHub Pages（手动/周更快照）**：https://codefuckee.github.io/honey-money/

https://codefuckee.github.io/honey-money/ —— 纯静态（无后端）：
排行/散点/分箱/每轮止盈明细/单基金复权净值折线全部内置快照数据，
客户端过滤排序分页；无爬虫管理。数据刷新（两种方式）：
① **自动（推荐）**：NAS `.env` 配置 `PAGES_REPO_URL`（含 fine-grained
   PAT 的仓库 URL，见 .env.example 指引）并 `docker compose up -d`——
   回测日更完成后自动导出快照并推送，公开页每日随日更更新；
② **手动**：在有库环境跑
   `DATABASE_URL=... .venv/bin/python scripts/export_static_api.py`
   导出 `site-data/` 并提交，push main 由 .github/workflows/pages.yml
   自动构建发布（VITE_STATIC=1 + VITE_BASE 子路径）。

## Docker 部署（本地 / 群晖 NAS 通用）

```bash
cp .env.example .env     # 必改 POSTGRES_PASSWORD；端口按需
docker compose up -d --build
# 浏览器打开 http://<主机>:8080 —— 三张卡片应全绿（本地/NAS 需可拉镜像）
```

群晖（Container Manager）：项目 → 新建 → 路径选本仓库（或 git 克隆地址）→ 
使用现有 docker-compose.yml → 按 .env 配好变量后启动。要点：

- **编排仅 2 容器**（frontend + backend）：frontend 对外暴露 28080，
  backend 仅 compose 内网；
- **PostgreSQL 不由本编排创建**：连接 NAS 上既有 PG 容器（宿主 5433，库/用户
  honey_money），backend 经 host.docker.internal 访问；数据持久化由该容器自理，
  本编排仅 `redis_data` 一个 named volume；
- **代理池用 NAS 上的公共实例**（`proxy-pool:2.4.1-aclfix` + 专用 redis 8，
  宿主 5010，多项目共用）：backend 经 `PROXY_POOL_URL` 取代理与探活，
  本编排不再自带 proxy_pool/redis（切换前的官方镜像版本见 git 历史；
  官方镜像有 alpine 无 bash 的入口坑，公共池自建时已修复）。

## 日线数据爬虫（python -m app.crawler）

爬全市场 A 股历史日线（不复权）入 PostgreSQL，多数据源、断点续爬、动态限速、防封保护。

限速默认：**东财源 drip 模式——每 30 秒 1 请求 + ±30% 随机抖动（实际 21~39s），
min=max 锁死不提速**（出口 IP 曾被封）；新浪源 1 QPS 起步 AIMD 自适应（上限 5）；
基金源 1 QPS 起步 AIMD 自适应（0.1~2.0，基金接口与 push2his 不同子系统、无被封史）。
可用 `--initial-qps/--max-qps/--min-qps/--jitter` 覆盖。

```bash
# NAS 部署后，在 backend 容器内运行（编排已注入 DATABASE_URL）
docker exec honey-money-backend-1 python -m app.crawler --limit 20   # 试跑前 20 只（新浪源）
docker exec honey-money-backend-1 python -m app.crawler              # 全量/增量（新浪源，默认）
docker exec honey-money-backend-1 python -m app.crawler --source eastmoney  # 东财源（解封后校验用）
docker exec honey-money-backend-1 python -m app.crawler --source delisted   # 退市股历史补爬（367 只，新浪 klc_kl 全史；
                                                                            #  无成交额/换手率置 NULL；与主爬虫共用限速通道）
docker exec honey-money-backend-1 python -m app.crawler --source fund       # 基金历史净值全量（2.8 万只，约 8 小时；
                                                                            #  单请求全史 upsert 幂等，落 fund_nav 表）
docker exec honey-money-backend-1 python -m app.crawler --source fund --mode incremental  # 基金快照增量
                                                                            #  （当日净值列表一次拉全市场，约 5 分钟）

# 定投止盈回测：重跑回测并刷新「定投止盈回测」页数据（本机开发环境，非容器）
#   .venv/bin/python scripts/fund_dca_backtest.py                                 # 全市场回测 → scripts/data/fund_dca_backtest.csv
#   cd backend && ../.venv/bin/python -m app.dca_store ../scripts/data/fund_dca_backtest.csv   # 导入快照（幂等，秒级）

# 常用参数
#   --source sina|eastmoney|delisted|fund  默认 sina（delisted/fund 见上）
#   --mode incremental|full  默认 incremental：按库内断点续爬（按源隔离）；full 全量重爬
#   --initial-qps/--max-qps  每通道限速（默认按源：sina 1→5，AIMD 动态调整）
#   --jitter 0.3             请求间隔随机抖动 ±30%（防封，默认开）
#   --batch-size 300 --batch-pause 30   每 300 只歇 30s（防封，默认开）
#   --abort-after 15         连续 15 只失败自动熔停保存退出（默认开）
#   --limit N / --only CODES 试跑 / 指定股票
# 退出码：0 全部成功；1 有失败或熔停（计划任务可据此告警）
```

**数据源与多源对比**：K 线表主键 `(code, trade_date, source)`，不同来源并存不覆盖——
东财解封后重爬一遍即可直接 SQL 对比两源差异。字段口径已统一（volume 手、换手率
百分数；新浪源现算振幅/涨跌幅，流通股本为新浪专有字段）。仪表盘「爬虫数据」
卡片按来源展示 K 线分布。

**防封实测背景**（详见 docs/ashare-daily-data-sources.md）：东财 WAF 断连免费代理
的机房 IP 段且单 IP 频控严格（家宽出口曾被封 15h+）→ 新浪源直连 + 保守限速 +
抖动 + 分批 + 熔停是当前生产姿态；新浪清单分页有软限流截断现象，已内置空页
重试 + 官方总数校验（不完整即报错重跑，绝不静默漏爬）。

数据表：`stock_basic`（清单快照）、`daily_bar`（日线，全市场约 2500 万行/源）、
`crawl_run`（运行汇总与失败清单，`task_id`/`trigger_kind` 关联调度任务与触发方式）、
`crawl_task`（任务定义）。新浪源全量约 5200+ 请求，1 QPS 起步（AIMD
自动升速）约 1~2 小时；此后每日增量数分钟。

**基金历史净值（source=fund，2026-10 新增）**：爬全部公募基金（东财全集
28,169 只，含已清盘与 A/C 份额——全量爬取避免回测幸存者偏差）入
`fund_basic`（代码/名称/类型/退市标记）与 `fund_nav`（净值日，主键
`(code, nav_date, source)`：单位净值/累计净值/日增长率/分红原始串）。
`fund_risk_metrics`（风险/波动指标快照，code 主键）由「定投回测日更」
任务顺带全量重算：年化/下行波动率、最大回撤、夏普（近1/3/5年与全史
四窗口，复权日增长率口径、√252 年化、无风险利率 0）+ 当前 60 日滚动
波动率及其全史分位；基金列表排序（vol_1y 等白名单）与详情指标区共用。
两种模式：
- **full 全量**：天天基金 `pingzhongdata/{code}.js` 单请求全史逐只爬
  （约 8 小时；lsjz 分页接口单页上限 20 条不可用作爬虫通道）；预置手动
  任务「基金历史净值」默认禁用。清盘基金历史保留、照常入库（防幸存者
  偏差）；源上无数据的份额代码（301/404，如后端收费份额）记 0 行跳过。
- **incremental 增量**：Fund_JJJZ_Data 当日净值列表 243 页一次拉全市场
  最新已披露净值（实测 22,404 只/223 秒/2QPS），日期按槽位日历对齐解析
  （今日未发布自动落到昨日真实日期；QDII 滞后双空自动跳过不落错行）；
  预置任务「基金每日增量」每日 21:00 默认禁用，建议晚间净值发布后运行
  （白天跑当日行缺增长率，由全量任务补）。
退市标记口径：在市=当日列表（t=1 净值 ∪ t=7 货币）并集，与全集差集=
清盘/终止；数据页基金列表可按 全部/仅在市/仅退市 过滤。免 Cookie、带
Referer 即可；货币基金无单位净值走势按跳过处理。

## 每日数据更新调度（独立 worker 容器，无需外部计划任务）

任务（爬虫/定投回测日更/指数日更）由 **worker 容器**（`python -m
app.worker`，与 backend 同镜像）执行——backend 只留 API
（compose 设 `SCHEDULER_ENABLED=0`），到点自动增量爬取：

- **调度时刻**：北京时间（`Asia/Shanghai`，与容器时区无关）每日 HH:MM，
  不辨交易日——非交易日照常执行，增量断点机制保证空转无害；
- **错过补跑**：到点时恰在重启/关机 → worker 下次启动自动补跑一次
  （trigger=catch_up）；
- **全局串行**：同一时刻只跑一个任务，后到自动排队；执行前取 PostgreSQL
  advisory lock——手动 CLI 与调度任务并发写库也被互斥；
- **运行态落 PG（task_runtime 表）**：队列/阶段/进度/停止指令在 PG 而非
  进程内存——API 与 worker 跨进程共享；队列跨 worker 重启存活；
  「任务」页的触发/取消/停止写指令行、worker 轮询消费（停止 ≤5s 生效）；
- **部署不打断任务（2026-11-24）**：流水线重建 backend（API 新代码生效）
  与运行中任务互不干扰；worker 由 CI 空闲守门保护（部署前查 PG：有任务
  运行/排队 → worker 延后重建，每小时 `deploy-catchup-nas` 定时流水线在
  空闲后补上新镜像，镜像没变时空操作）；
- **防封参数内置**：任务仅可配 数据源/模式/时刻，限速/抖动/分批/熔停用安全
  默认值不可配；预置「新浪每日增量 18:00」默认禁用，页面上开开关即用；
  预置启用：「定投回测日更 23:00」「指数日更 17:30」（上证指数+沪深300
  日线，519 vs 924 对比页与金融危机史页数据源；东财被风控时逐指数
  新浪兜底，924 线不停更）；
- **管理**：前端「任务」页（启停/手动触发/实时进度 3s 轮询/历史）或
  `/api/tasks` 接口；`SCHEDULER_ENABLED=0` 可整体关闭调度（CLI 手动跑
  不受影响；单进程开发模式置 1，调度器宿主于 backend，行为同拆分前）。

## CI/CD

推送 main 触发两阶段流水线：

1. `backend-test`（任意 runner）：python:3.12-slim 跑 backend/tests；
2. `deploy-nas`（仅 main，runner #18 "nas"，tags `linux,docker,nas,deploy`）：
   test 全绿后自动在群晖上 `docker compose up -d --build`——该 runner 映射了
   NAS 宿主 docker，容器直接落在 NAS 上持久运行。
   **空闲守门（2026-11-24）**：部署前查 PG 运行态（`scripts/nas/
   busy_check.py` 经 docker exec 喂进旧 backend 容器跑）——任务运行中/排队
   → worker（与迁移期的 backend）延后重建；教学页全量回测运行中 → backend
   延后重建；各等最多 10 分钟仍忙则只重建安全子集，**流水线保持绿**；
   每小时 `deploy-catchup-nas` 定时流水线（需在 GitLab CI/CD → Schedules
   建 main 分支每小时计划）在空闲后补上被延后的服务——镜像没变化时
   compose 判定无需重建 = 秒级空操作。

前置：项目 CI/CD Variables 已配置 `POSTGRES_PASSWORD`（masked）。
部署后访问 `http://<NAS>:28080`（默认端口）。手动重部署也可以在 NAS 上
直接 `docker compose -p honey-money up -d --build`。

## 路线图（业务迁入顺序）

1. 数据层：✅ 日线爬虫（新浪/东财双源）与 ✅ 每日增量定时任务（进程内调度器
   + 「任务」页管理，见上节）已落地；待做：复权因子、财务/基本面数据；
2. 回测引擎与策略：✅ vectorbt 引擎 + 六大经典策略教学页 + ✅ 复权因子
   （回测后复权口径，除权失真消除；backtrader 已停维护，改选 vectorbt）；
   待做：参数寻优；
3. 前端页面：双均线教学页移植为 Vue 组件 + 回测结果展示；
4. 实盘信号与调仓单（原 live 体系）。
