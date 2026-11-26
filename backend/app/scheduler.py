"""爬虫任务调度器：常驻进程内自治调度（生产=worker 容器，见 app/worker.py；
单进程开发模式 SCHEDULER_ENABLED=1 时宿主于 backend），替代外部计划任务。

职责边界：
- 调度：每 poll_interval 秒扫描 crawl_task（上海时区 HH:MM，不辨交易日——
  非交易日照常执行，增量断点机制保证空转无害）；启动时对已过点未跑的任务
  补跑一次（catch_up）；任务定义与「今日已计划执行」均以 PG 为准，重启恢复；
- 串行：全局单 worker 队列，同一时刻只跑一个任务（新浪防封限速按全局通道
  设计，并行=加倍请求压力）；后到排队；
- 互斥：执行前取 PostgreSQL advisory lock（会话级、专用连接）——防住手动
  CLI 与调度任务并发写库、未来多 worker 重复调度；取不到则等待重试；
- 执行：委托注入的 executor 协程（生产=build_crawl_executor：拉清单 +
  CrawlRunner，防封参数内置安全默认值），调度器不感知爬虫细节，可全假件测试；
- 停止：协作式——API 把停止指令写进 task_runtime.stop_requested（跨进程），
  本进程 5s 轮询翻译成 asyncio.Event，executor 各 job 照旧消费（runner
  停止派发、优雅落库，不算失败）；
- 状态：运行态（phase/progress/队列/结果）全经注入的 runtime（生产=
  TaskRuntime 落 PG）——队列跨重启存活、任务 API 跨进程读写，本进程内存
  不再持有任何运行态；历史在 crawl_run。

scan_once / drain_once 单步公开方法：测试直接驱动，不跑真实循环；
start() 拉起的后台循环只是反复调用它们。
"""

import asyncio
import datetime as dt
import os
from zoneinfo import ZoneInfo

import psycopg

from .crawler.eastmoney import EastmoneyClient
from .crawler.fund_store import FundStore
from .crawler.proxies import ProxyBroker
from .crawler.rate import RateLimiter
from .crawler.runner import CrawlRunner
from .crawler.sina import SinaClient
from .crawler.store import Store
from .task_runtime import TaskRuntime
from .task_store import TaskStore

SH_TZ = ZoneInfo("Asia/Shanghai")


def _fps_worker(dsn: str) -> tuple[dict, dict | None, str | None]:
    """全市场输入指纹聚合（spawn 池内跑——PG 侧 GROUP BY 全市场 ~3,200
    万行，NAS 实测 ~10 分钟）：日更 job 的分支判定与收尾落库共用同一份
    （2026-11-26 二十轮；旧流程引擎内部自算，指纹在收尾才落——全量被
    中断的夜晚指纹永不落地 → 每晚都判全量的恶性循环源头之一）。"""
    from . import dca_backtest, dca_store
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        cur_fps = dca_backtest.load_input_fingerprints(conn)
        prev_fps, prev_engine = dca_store.load_fingerprints(conn)
    return cur_fps, prev_fps, prev_engine


def _write_progress_offset(dsn, task_id, base: int, total: int,
                           done: int, n: int) -> None:
    """分阶段进度折算（模块级函数可被 spawn pickle——闭包进不了池）：
    引擎按单方法分母上报 (done, n)，job 折算为跨方法累计 (base+done,
    total)，前端进度条语义不变（已完成基金数 / 本次需回测总数）。"""
    from . import task_runtime
    task_runtime.write_progress(dsn, task_id, base + done, total)

# 各源防封默认值（与 CLI __main__.SOURCE_DEFAULTS 一致，任务不可配置——
# 限速/抖动/分批/熔停配错是有实际封 IP 事故史的，杜绝危险组合）
_SOURCE_DEFAULTS = {
    "sina": {"initial_qps": 1.0, "min_qps": 0.2, "max_qps": 5.0},
    "eastmoney": {"initial_qps": 1 / 30, "min_qps": 1 / 30, "max_qps": 1 / 30},
    "delisted": {"initial_qps": 1.0, "min_qps": 0.2, "max_qps": 2.0},
    # 基金接口与 push2his 不同子系统、无被封史：1 QPS 起步 AIMD 自适应，
    # 失败降速托底 0.1、成功最多回到 2，熔停/批休全套照旧
    "fund": {"initial_qps": 1.0, "min_qps": 0.1, "max_qps": 2.0},
}
_BATCH_SIZE, _BATCH_PAUSE, _ABORT_AFTER, _CONCURRENCY = 300, 30.0, 15, 4


def next_run_at(task: dict, now: dt.datetime, *, ran_today: bool) -> dt.datetime | None:
    """任务下次执行时刻（上海时区 aware）；禁用任务 None。

    已过今日计划点或今日已计划执行 → 明天同一时刻；否则今晚。
    """
    if not task["enabled"] or not task["schedule_time"]:
        return None                      # 禁用 / 纯手动任务不参与排程
    hh, mm = task["schedule_time"].split(":")
    due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if due > now and not ran_today:
        return due
    return due + dt.timedelta(days=1)


class PgAdvisoryLock:
    """会话级 advisory lock：专用连接上取/还，防多进程并发爬取。"""

    KEY = "honey-money-crawler"

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None

    def _connect(self):
        return psycopg.connect(self._dsn, connect_timeout=5)

    async def acquire(self) -> bool:
        if self._conn is None:
            self._conn = await asyncio.to_thread(self._connect)
        cur = await asyncio.to_thread(
            self._conn.execute,
            "SELECT pg_try_advisory_lock(hashtext(%s))", (self.KEY,))
        row = await asyncio.to_thread(cur.fetchone)
        return bool(row and row[0])

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await asyncio.to_thread(
                self._conn.execute,
                "SELECT pg_advisory_unlock(hashtext(%s))", (self.KEY,))
        finally:
            conn, self._conn = self._conn, None
            try:
                await asyncio.to_thread(conn.close)
            except Exception:
                pass


async def _run_backtest_job(dsn: str, task: dict, progress,
                            stop_event: asyncio.Event) -> dict:
    """定投回测日更：子进程重算（脏检测版，见 dca_backtest.
    run_full_backtest_tp_dirty——逐基金比对输入指纹，只重算净值/基础信息
    有变的基金，引擎版本变化或首跑自动全量；周末/节假日零更新夜全程仅
    刷派生列，分钟级）→ 单事务合并导入（按 code 范围替换 + alive 全表
    刷新 + 时机死行清理 + 横截面分位回填）→ crawl_run 记录。

    前置只做净值新鲜度告警不阻断（跑最新可得数据，爬取失败次日自愈）；
    stop_event 无法中断子进程，仅在各阶段间生效；kill/重启丢弃当日运行
    （脏快路径导入为末尾单事务，半途无脏数据），次日计划照常；强制全量
    逃生口 BACKTEST_FORCE_FULL=1（或清空 fund_dca_fp 表）。

    分阶段逐方法全量（2026-11-26 二十轮根治项）：指纹空/引擎变化判全量
    时不再「全部算完→末尾一次性合并」（中断=整晚白跑+指纹永不落地+每晚
    全量的恶性循环，crawl_run 147~165 六连 aborted 实证），改为逐方法
    「算完即合并即盖戳」（fund_dca_method_meta 新鲜度表）：已合并方法
    保留，次晚跳过它们接着算剩余方法，全部新鲜后一次性落指纹回归分钟级
    脏检测快路径。fp 匹配的脏快路径（分钟级）保持末尾单事务不变。
    日更退让：单策略全量回测排队/运行中 → 本轮跳过（落 skipped 运行行，
    日历可见），绝不与其 worker 池并发叠加内存（4GB NAS 全量编排已证
    脆弱；反向 409 守护已有）。
    """
    import concurrent.futures
    import functools
    import multiprocessing

    from . import dca_backtest, dca_store, settings_store, task_runtime
    from .crawler.store import Store
    from .dca_api import TP_COMPARE_EQUITY_TYPES   # 对比池白名单（API 层常量）

    store = Store()
    loop = asyncio.get_running_loop()
    with psycopg.connect(dsn) as conn:
        latest = conn.execute(
            "SELECT max(nav_date)::date FROM fund_nav").fetchone()[0]
        stale = latest is None or (dt.datetime.now(SH_TZ).date() - latest).days > 1
        if stale:
            print(f"[backtest] 净值最新 {latest}（≥2 天未更新），按现有数据继续")
        # 日更退让（二十轮）：task_runtime 相位（queued/running——与设置页
        # 409 守护同源信号，覆盖 start_run 前的排队窗）+ crawl_run 敞开行
        # 双保险；命中则 start_run 后立即收尾 skipped（日历按 detail->
        # 'skipped' 归类「跳过」，aborted=False 非失败）
        mrow = conn.execute(
            "SELECT (SELECT phase FROM task_runtime WHERE task_id = "
            "(SELECT id FROM crawl_task WHERE source = 'backtest_method' "
            "ORDER BY id LIMIT 1)), "
            "(SELECT count(*) FROM crawl_run WHERE source = "
            "'backtest_method' AND finished_at IS NULL)").fetchone()
        m_phase = (mrow[0] or "idle") if mrow else "idle"
        m_open = mrow[1] if mrow else 0
        run_id = store.start_run(conn, mode=task["mode"], source="backtest",
                                 task_id=task["id"],
                                 trigger=task.get("_trigger", "manual"))
        if m_phase in ("queued", "running") or m_open:
            reason = (f"单策略回测{m_phase}中"
                      if m_phase in ("queued", "running")
                      else "单策略回测运行中")
            print(f"[backtest] {reason}，本轮日更退让跳过（次日计划照常）")
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0, failures=[],
                             aborted=False,
                             detail={"failures": [], "skipped": reason})
            conn.commit()
            return {"total": 0, "ok": 0, "failed": 0, "bars": 0,
                    "failures": [], "aborted": False, "stopped": False,
                    "error": False, "skipped": True}
        conn.commit()
    if stop_event.is_set():
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0, failures=[],
                             aborted=True)
            conn.commit()
        return {"total": 0, "ok": 0, "failed": 0, "bars": 0, "failures": [],
                "aborted": True, "stopped": True, "error": False}

    ctx = multiprocessing.get_context("spawn")
    # 基金级进度（2026-11-25 十轮）：引擎在 spawn 池内逐块回调 → 经
    # partial(write_progress, dsn, task_id) 跨进程落库 task_runtime；
    # job 层不再写 (0,1)/(1,1) 占位——引擎独占进度生命周期（末块写满格，
    # merge 段保持 100% 至 finish 清除）
    progress_cb = (functools.partial(task_runtime.write_progress, dsn,
                                     task["id"])
                   if task.get("id") is not None else None)

    def _abort(exc: Exception) -> None:
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0,
                             failures=[f"{type(exc).__name__}: {str(exc)[:180]}"],
                             aborted=True)
            conn.commit()

    # 指纹聚合一次（~10 分钟，spawn 池内）：分支判定（全量 vs 脏快路径）
    # 与收尾落库共用——旧流程引擎内部自算且指纹收尾才落，全量被中断的
    # 夜晚指纹永不落地（fund_dca_fp 0 行实证）
    try:
        # 模块级函数 + 基元参数才可被 spawn pickle（嵌套闭包会
        # "Can't get local object" 崩掉子进程任务；partial 同理可 pickle）
        with concurrent.futures.ProcessPoolExecutor(max_workers=1,
                                                    mp_context=ctx) as pool:
            cur_fps, prev_fps, prev_engine = await loop.run_in_executor(
                pool, _fps_worker, dsn)
            with psycopg.connect(dsn) as conn:
                try:
                    min_span_days = settings_store.get_min_span_days(
                        conn, default=dca_backtest.MIN_SPAN_DAYS)
                except Exception as exc:
                    print(f"[backtest] 设置表读取失败，回退运作期限下限 "
                          f"{dca_backtest.MIN_SPAN_DAYS} 天：{exc}")
                    min_span_days = dca_backtest.MIN_SPAN_DAYS
                engine_str = dca_backtest.engine_version(min_span_days)
            force = os.environ.get("BACKTEST_FORCE_FULL") == "1"
            if force:
                prev_fps, prev_engine = None, None
                print("[backtest] BACKTEST_FORCE_FULL=1，强制全量重算")
            dirty, gone = dca_backtest.compute_dirty(cur_fps, engine_str,
                                                     prev_fps, prev_engine)
            full_run = not prev_fps or prev_engine != engine_str
            if full_run:
                # ===== 分阶段逐方法全量（二十轮根治项） =====
                all_end = max((fp[2] for fp in cur_fps.values()
                               if fp[2] and fp[0] >= 2), default=None)
                window_end = str(all_end)[:10] if all_end else None
                cutoff_iso = (str((dt.date.fromisoformat(window_end)
                                   - dt.timedelta(days=dca_backtest
                                                  ._ALIVE_WINDOW_DAYS)))
                              if window_end else None)
                excluded = {code for code, fp in cur_fps.items()
                            if dca_backtest.fund_excluded(
                                fp[7],
                                dt.date.fromisoformat(str(fp[1]))
                                if fp[1] else None,
                                dt.date.fromisoformat(str(fp[2]))
                                if fp[2] else None,
                                min_span_days)}
                universe = len(cur_fps) - len(excluded)
                all_codes = sorted(cur_fps.keys())
                with psycopg.connect(dsn) as conn:
                    method_meta = dca_store.load_method_meta(conn)
                    conn.commit()
                pending = [m for m in dca_backtest.DCA_METHODS
                           if force or method_meta.get(m)
                           != (engine_str, window_end)]
                total_funds = len(pending) * universe
                tp_params = {"amount": 1000.0, "frequency": "monthly",
                             "thresholds": [float(t) for t in
                                            dca_backtest.TP_THRESHOLDS],
                             "window_end": window_end,
                             "source": "scheduler/backtest",
                             "engine": engine_str}
                print(f"[backtest] 全量（分阶段逐方法）：待算 "
                      f"{len(pending)}/{len(dca_backtest.DCA_METHODS)} 方法"
                      f"（宇宙 {universe:,} 只），跳过已新鲜 "
                      f"{len(dca_backtest.DCA_METHODS) - len(pending)} 方法")
                base = 0
                rounds_total = 0
                staged_detail: dict = {"staged": True, "methods": {},
                                       "skipped_fresh": [
                                           m for m in dca_backtest.DCA_METHODS
                                           if m not in pending]}
                stopped = False
                for method in pending:
                    if stop_event.is_set():
                        stopped = True
                        break
                    with_side = (method == "amount")   # 首班次搭车旧口径/时机/风险
                    cb = (functools.partial(_write_progress_offset, dsn,
                                            task["id"], base, total_funds)
                          if progress_cb is not None else None)
                    tp_rows, tp_rounds, stats_m = await loop.run_in_executor(
                        pool, functools.partial(
                            dca_backtest.run_full_backtest_tp_method,
                            dsn, method, progress=cb,
                            with_side_tables=with_side,
                            cutoff_iso=cutoff_iso))
                    # 单方法单事务：算完即合并即盖戳（戳与行同事务原子生效
                    # ——中断后已合并方法次晚不再重算）
                    with psycopg.connect(dsn) as conn:
                        dca_store.ensure_tp_tables(conn)
                        dca_store.merge_tp_rows_method(conn, tp_rows, method)
                        dca_store.merge_tp_rounds_method(conn, tp_rounds,
                                                         method)
                        dca_store.refresh_alive_method(conn, cutoff_iso,
                                                       method)
                        side = stats_m.get("side")
                        if with_side and side is not None:
                            # 时机/风险与 amount 戳同事务绑定（新鲜度一致，
                            # 中断不留半套）
                            dca_store.merge_timing(conn, side["timing_rows"],
                                                   computed_at=window_end)
                            dca_store.merge_risk(conn, side["risk_rows"],
                                                 all_codes)
                        dca_store.mark_method_fresh(conn, method, engine_str,
                                                    window_end)
                        conn.commit()
                    rounds_total += stats_m.get("tp_round_count", 0)
                    staged_detail["methods"][method] = {
                        "funds": stats_m.get("fund_count"),
                        "rounds": stats_m.get("tp_round_count")}
                    base += universe
                    print(f"[backtest] 分阶段：{method} 完成（"
                          f"{stats_m.get('fund_count', 0):,} 只 / 轮次 "
                          f"{stats_m.get('tp_round_count', 0):,} 行），"
                          f"已合并并盖戳")
                if stopped:
                    with psycopg.connect(dsn) as conn:
                        store.finish_run(conn, run_id,
                                         total_stocks=base, ok_stocks=base,
                                         failed_stocks=0,
                                         bars_upserted=rounds_total,
                                         failures=[], aborted=True)
                        conn.commit()
                    return {"total": total_funds, "ok": base, "failed": 0,
                            "bars": rounds_total, "failures": [],
                            "aborted": True, "stopped": True, "error": False}
                # 收尾事务（全部方法新鲜后才落指纹——指纹代表「全部方法的
                # 输入已反映到库」，部分新鲜时落库会让次晚脏检测误判）
                with psycopg.connect(dsn) as conn:
                    dca_store.ensure_fp_schema(conn)
                    dca_store.refresh_alive(
                        conn, dt.date.fromisoformat(cutoff_iso)
                        if cutoff_iso else None)      # 全表兜底扫（幂等）
                    dca_store.touch_tp_meta(conn, universe, tp_params)
                    dca_store.replace_fingerprints(conn, cur_fps, engine_str)
                    store.finish_run(conn, run_id, total_stocks=universe,
                                     ok_stocks=universe, failed_stocks=0,
                                     bars_upserted=rounds_total, failures=[],
                                     aborted=False)
                    conn.commit()
                summary = {"total": universe, "ok": universe, "failed": 0,
                           "bars": rounds_total, "failures": [],
                           "aborted": False, "stopped": False,
                           "error": False}
                print(f"[backtest] 分阶段全量完成：{len(pending)} 方法 × "
                      f"{universe:,} 只 / 轮次 {rounds_total:,} 行，"
                      f"指纹已落库（次晚起回归脏检测快路径），"
                      f"窗口截至 {window_end}")
            else:
                tp_rows, tp_rounds, timing_rows, risk_rows, stats, \
                    fingerprints = await loop.run_in_executor(
                        pool, functools.partial(
                            dca_backtest.run_full_backtest_tp_dirty, dsn,
                            progress=progress_cb, cur_fps=cur_fps))
                if stop_event.is_set():
                    # 算完但等停：丢弃结果（不导入），下次计划照常全量重算
                    with psycopg.connect(dsn) as conn:
                        store.finish_run(
                            conn, run_id, total_stocks=stats["fund_count"],
                            ok_stocks=0, failed_stocks=0,
                            bars_upserted=stats["tp_round_count"], failures=[],
                            aborted=True)
                        conn.commit()
                    return {"total": stats["fund_count"], "ok": 0,
                            "failed": 0, "bars": stats["tp_round_count"],
                            "failures": [], "aborted": True, "stopped": True,
                            "error": False}
                # 引擎末块已把进度写到满格（total/total）——此处不再写占位，merge 段
                # 保持 100% 至 finish 清除
                tp_params = {"amount": 1000.0, "frequency": "monthly",
                             "thresholds": stats.get("thresholds", []),
                             "window_end": stats.get("window_end"),
                             "source": "scheduler/backtest",
                             "engine": stats.get("engine")}
                # 脏检测合并导入：只替换本次重算（∪ gone 清理）的基金行，未变基金
                # 原行保留；alive/时机表/横截面分位为全局派生，每晚集合 SQL 刷新
                dirty_codes = sorted(set(stats.get("dirty", []))
                                     | set(stats.get("gone", [])))
                cutoff = (dt.date.fromisoformat(str(stats["window_end"])[:10])
                          - dt.timedelta(days=dca_backtest._ALIVE_WINDOW_DAYS)) \
                    if stats.get("window_end") else None
                with psycopg.connect(dsn) as conn:
                    dca_store.ensure_tp_schema(conn)     # 多阈值新表 + 波动指标表幂等建表
                    dca_store.ensure_fp_schema(conn)     # 输入指纹表幂等建表
                    dca_store.merge_tp_rows(conn, tp_rows, dirty_codes,
                                            fund_count=stats["fund_count"],
                                            params=tp_params)
                    dca_store.merge_tp_rounds(conn, tp_rounds, dirty_codes)
                    dca_store.refresh_alive(conn, cutoff)  # 全表刷新（跳过基金也随窗口翻转）
                    dca_store.merge_timing(conn, timing_rows,
                                           computed_at=str(stats["window_end"])[:10]
                                           if stats.get("window_end") else None)
                    dca_store.merge_risk(conn, risk_rows, dirty_codes)
                    dca_store.replace_fingerprints(conn, fingerprints,
                                                   stats["engine"])
                    store.finish_run(conn, run_id,
                                     total_stocks=stats["fund_count"],
                                     ok_stocks=stats["fund_count"],
                                     failed_stocks=0,
                                     bars_upserted=stats["tp_round_count"],
                                     failures=[], aborted=False)
                    conn.commit()
                print(f"[backtest] 日更完成：基金 {stats['fund_count']:,} 只（重算 "
                      f"{stats.get('dirty_count', 0):,} / 跳过 "
                      f"{stats.get('skipped_count', 0):,} / 清理 "
                      f"{stats.get('gone_count', 0):,}，引擎 {stats.get('engine')}）/ "
                      f"多阈值轮次 {stats['tp_round_count']:,} 行 / "
                      f"风险行 {stats['risk_count']:,}，"
                      f"窗口截至 {stats.get('window_end')}")
                summary = {"total": stats["fund_count"],
                           "ok": stats["fund_count"], "failed": 0,
                           "bars": stats["tp_round_count"], "failures": [],
                           "aborted": False, "stopped": False, "error": False}
    except Exception as exc:
        _abort(exc)
        raise

    # 对比快照重建（独立事务，失败只告警——派生聚合不回滚日更导入，缺行
    # 时对比 API 回落现算聚合，夜间任务次日自愈；见 dca_store.rebuild_tp_compare）
    try:
        with psycopg.connect(dsn) as conn:
            n_cmp = dca_store.rebuild_tp_compare(
                conn, equity_types=TP_COMPARE_EQUITY_TYPES)
            conn.commit()
        print(f"[backtest] 对比快照重建：{n_cmp} 行"
              f"（method×equity×fee×tp，对比卡毫秒级取数）")
    except Exception as exc:      # noqa: BLE001 —— 快照失败不影响日更交付
        print(f"[backtest] 对比快照重建失败（对比卡暂回落现算，次日重试）: "
              f"{type(exc).__name__}: {str(exc)[:180]}")

    # Pages 自动发布（可选）：PAGES_REPO_URL 配置了含 token 的仓库 URL 才启用；
    # 失败只告警不回滚——回测结果是主交付物，公开页保持上一份快照次日自愈
    pages_url = os.environ.get("PAGES_REPO_URL", "")
    if pages_url:
        work_dir = os.environ.get("PAGES_WORKDIR", "/srv/pages")
        try:
            from . import pages_publish
            pub = await asyncio.to_thread(
                pages_publish.publish, dsn, pages_url, work_dir)
            print(f"[backtest] Pages 发布：{'已推送' if pub.get('pushed') else '快照无变更'}"
                  f"（排行 {pub.get('rows', 0):,} 行 / 轮次基金 {pub.get('round_funds', 0):,} 只）")
        except Exception as exc:      # noqa: BLE001 —— 发布失败不影响回测交付
            print(f"[backtest] Pages 发布失败（不影响回测结果，次日自愈）: {exc}")
    else:
        print("[backtest] 未配置 PAGES_REPO_URL，跳过 Pages 发布")
    return summary


async def _run_backtest_method_job(dsn: str, task: dict, progress,
                                   stop_event: asyncio.Event) -> dict:
    """单策略全量回测（设置页「运行单策略全量」按钮触发，2026-11-25 九轮）：
    预置任务「单策略全量回测」（source='backtest_method'，mode 列=目标
    策略，由设置端点改写）——只重算选定 method 的全市场 8 档止盈 × 3 费率
    （引擎 run_full_backtest_tp_method 分片并行），按 method 域替换
    fund_dca_tp_* 行 + alive method 域集合刷新 + 对比快照重建（独立事务）。

    与日更 _run_backtest_job 的刻意差异（正确性锚）：不写输入指纹表
    （引擎陈旧判定留给日更兜底全量，防止局部运行掩盖其余方法陈旧）、
    不产旧口径/时机/风险行（与 method 无关，日更每晚统一维护）、不做
    Pages 发布（旧口径表未动）。crawl_run 以 source='backtest_method'
    落行——日更运行中判定按 source='backtest' 过滤，两路互不污染。"""
    import concurrent.futures
    import functools
    import multiprocessing

    from . import dca_backtest, dca_store, task_runtime
    from .crawler.store import Store

    method = task["mode"]
    store = Store()
    loop = asyncio.get_running_loop()
    with psycopg.connect(dsn) as conn:
        run_id = store.start_run(conn, mode=method[:16],
                                 source="backtest_method",
                                 task_id=task["id"],
                                 trigger=task.get("_trigger", "manual"))
        conn.commit()
    if method not in dca_backtest.DCA_METHODS:
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0,
                             failures=[f"unknown dca method: {method}"],
                             aborted=True)
            conn.commit()
        raise ValueError(f"unknown dca method: {method!r}")
    ctx = multiprocessing.get_context("spawn")
    # 基金级进度（2026-11-25 十轮，与日更同通道）：引擎独占进度生命周期
    progress_cb = (functools.partial(task_runtime.write_progress, dsn,
                                     task["id"])
                   if task.get("id") is not None else None)
    try:
        # 模块级函数 + 基元参数才可被 spawn pickle（同日更教训）
        with concurrent.futures.ProcessPoolExecutor(max_workers=1,
                                                    mp_context=ctx) as pool:
            tp_rows, tp_rounds, stats = await loop.run_in_executor(
                pool, functools.partial(
                    dca_backtest.run_full_backtest_tp_method, dsn, method,
                    progress=progress_cb))
    except Exception as exc:
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0,
                             failures=[f"{type(exc).__name__}: "
                                       f"{str(exc)[:180]}"],
                             aborted=True)
            conn.commit()
        raise
    if stop_event.is_set():
        # 算完但等停：丢弃结果（不导入），下次照常
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=stats["fund_count"],
                             ok_stocks=0, failed_stocks=0,
                             bars_upserted=stats["tp_round_count"],
                             failures=[], aborted=True)
            conn.commit()
        return {"total": stats["fund_count"], "ok": 0, "failed": 0,
                "bars": stats["tp_round_count"], "failures": [],
                "aborted": True, "stopped": True, "error": False,
                "method": method}
    # 引擎末块已写满格进度——merge 段保持 100% 至 finish 清除（同日更）
    cutoff = (dt.date.fromisoformat(str(stats["window_end"])[:10])
              - dt.timedelta(days=dca_backtest._ALIVE_WINDOW_DAYS)) \
        if stats.get("window_end") else None
    with psycopg.connect(dsn) as conn:
        dca_store.ensure_tp_tables(conn)          # 多阈值三表幂等建表
        dca_store.merge_tp_rows_method(conn, tp_rows, method)
        dca_store.merge_tp_rounds_method(conn, tp_rounds, method)
        dca_store.refresh_alive_method(conn, cutoff, method)
        store.finish_run(conn, run_id, total_stocks=stats["fund_count"],
                         ok_stocks=stats["fund_count"], failed_stocks=0,
                         bars_upserted=stats["tp_round_count"], failures=[],
                         aborted=False)
        conn.commit()
    print(f"[backtest-method] 单策略全量完成：{method} / "
          f"{stats['fund_count']:,} 只 / 轮次 {stats['tp_round_count']:,} 行，"
          f"窗口截至 {stats.get('window_end')}")

    # 对比快照重建（独立事务，失败只告警——派生聚合不回滚结果导入，缺行
    # 时对比 API 回落现算，下次运行自愈；见 dca_store.rebuild_tp_compare）
    try:
        from .dca_api import TP_COMPARE_EQUITY_TYPES
        with psycopg.connect(dsn) as conn:
            n_cmp = dca_store.rebuild_tp_compare(
                conn, equity_types=TP_COMPARE_EQUITY_TYPES)
            conn.commit()
        print(f"[backtest-method] 对比快照重建：{n_cmp} 行")
    except Exception as exc:      # noqa: BLE001 —— 快照失败不影响结果交付
        print(f"[backtest-method] 对比快照重建失败（对比卡暂回落现算，"
              f"下次运行重试）: {type(exc).__name__}: {str(exc)[:180]}")
    return {"total": stats["fund_count"], "ok": stats["fund_count"],
            "failed": 0, "bars": stats["tp_round_count"], "failures": [],
            "aborted": False, "stopped": False, "error": False,
            "method": method}


async def _run_index_job(dsn: str, proxy_pool_url: str, task: dict, *,
                         progress, stop_event: asyncio.Event,
                         client=None, store=None, connect=None,
                         sina=None) -> dict:
    """指数日更：东财 K 线接口拉指数日线 → index_daily 幂等 upsert。

    指数接口免 Cookie（公开 ut 即可，与个股清单接口不同子系统）、
    家宽直连即可（proxy_mode="direct"，与东财个股同站低 QPS 无封禁史）；
    增量 beg=最近日期-10 自然日（重叠容错），空表全史。遍历全部注册
    指数（2026-11-24 起上证+沪深300，金融危机史页双口径），逐指数
    提交——单个指数失败回滚其事务并记录，不影响其他指数。

    新浪兜底（2026-11-24 生产踩坑：push2his 对家宽 IP 风控 RemoteProtocolError
    连日，东财 kline 拉不到数 → 519-924 页 924 线停更）：东财失败后逐指数
    落新浪指数日线（kjs 全史接口，与股票同族、新浪站独立未被封）重试同一段
    增量；双源皆败才记 failures。client/store/connect/sina 注入参数供离线
    测试（sina 缺省懒建——东财正常时零开销）；失败任务内消化（failures
    记录），run 正常收尾不抛出，次日计划照常。
    """
    from . import index_store

    store = store or Store()
    connect = connect or psycopg.connect
    sina_client = sina
    with connect(dsn) as conn:
        run_id = store.start_run(conn, mode=task["mode"], source="index",
                                 task_id=task["id"],
                                 trigger=task.get("_trigger", "manual"))
        conn.commit()
    summary = {"total": 1, "ok": 0, "failed": 0, "bars": 0, "failures": [],
               "aborted": True, "stopped": False, "error": False}

    def finish(conn, *, bars_n, failures):
        ok = 0 if failures else 1
        store.finish_run(conn, run_id, total_stocks=1, ok_stocks=ok,
                         failed_stocks=len(failures), bars_upserted=bars_n,
                         failures=failures[:50], aborted=bool(failures))
        conn.commit()

    if stop_event.is_set():
        with connect(dsn) as conn:
            finish(conn, bars_n=0, failures=[])
        return summary
    spec_all = index_store.INDEX_SYMBOLS
    total = len(spec_all)
    progress(0, total)
    if client is None:
        rate = RateLimiter(initial_qps=0.5, min_qps=0.1, max_qps=1.0, jitter=0.3)
        direct = httpx_async_client(timeout=30.0)
        pool = httpx_async_client(base_url=proxy_pool_url, timeout=5.0)
        client = EastmoneyClient(rate=rate, proxy_broker=ProxyBroker(http=pool),
                                 http=direct, proxy_mode="direct")
    failures, bars_n = [], 0
    with connect(dsn) as conn:
        for done, (symbol, spec) in enumerate(spec_all.items(), start=1):
            beg = "0"
            try:
                last = index_store.last_date(conn, symbol)
                beg = ((last - dt.timedelta(days=10)).strftime("%Y%m%d")
                       if last else "0")
                data = await client.fetch_daily_bars(spec["market"],
                                                     spec["code"], beg=beg)
                index_store.upsert_bars(conn, symbol, data.rows)
                conn.commit()
                bars_n += len(data.rows)
            except Exception as exc:            # noqa: BLE001 任务内消化
                conn.rollback()                 # 带毒事务不过夜，下一指数照常
                try:                            # 东财 kline 被风控 → 新浪兜底
                    if sina_client is None:
                        sina_client = SinaClient(
                            rate=RateLimiter(initial_qps=1.0, min_qps=0.2,
                                             max_qps=5.0, jitter=0.3),
                            http=httpx_async_client(timeout=30.0))
                    data = await sina_client.fetch_index_daily(symbol, beg=beg)
                    index_store.upsert_bars(conn, symbol, data.rows)
                    conn.commit()
                    bars_n += len(data.rows)
                    print(f"[index] {symbol} 东财失败({type(exc).__name__})"
                          f" → 新浪兜底 +{len(data.rows)} 根")
                except Exception as exc2:       # noqa: BLE001 双源皆败才记失败
                    conn.rollback()
                    failures.append(
                        f"{symbol} 东财 {type(exc).__name__}: {str(exc)[:100]}"
                        f" + 新浪兜底 {type(exc2).__name__}: {str(exc2)[:120]}")
            progress(done, total)
        finish(conn, bars_n=bars_n, failures=failures)
        conn.commit()
    await client.aclose()
    if sina_client is not None and hasattr(sina_client, "aclose"):
        await sina_client.aclose()
    summary.update(ok=0 if failures else 1, failed=len(failures),
                   bars=bars_n, failures=failures, aborted=bool(failures))
    print(f"[index] 指数日更：{'/'.join(spec_all)} +{bars_n} 根"
          + (f" / 失败 {failures[0]}" if failures else ""))
    return summary


def build_crawl_executor(dsn: str, proxy_pool_url: str):
    """生产执行体：拉清单 → CrawlRunner 增量/全量爬取，返回结果摘要。

    trigger 经 task['_trigger'] 注入（schedule/catch_up/manual），透传
    crawl_run 作「今日已计划执行」判定依据。
    """

    async def execute(task: dict, *, progress, stop_event: asyncio.Event) -> dict:
        if task["source"] == "backtest":
            return await _run_backtest_job(dsn, task, progress, stop_event)
        if task["source"] == "backtest_method":
            return await _run_backtest_method_job(dsn, task, progress,
                                                  stop_event)
        if task["source"] == "index":
            return await _run_index_job(dsn, proxy_pool_url, task,
                                        progress=progress, stop_event=stop_event)
        defaults = _SOURCE_DEFAULTS[task["source"]]
        rate = RateLimiter(initial_qps=defaults["initial_qps"],
                           min_qps=defaults["min_qps"], max_qps=defaults["max_qps"],
                           jitter=0.3)
        store = (FundStore() if task["source"] == "fund" else Store())
        if task["source"] == "sina":
            direct = httpx_async_client(timeout=30.0)
            client = SinaClient(rate=rate, http=direct)
        elif task["source"] == "delisted":
            from .crawler.delisted import DelistedClient
            direct = httpx_async_client(timeout=30.0)
            client = DelistedClient(rate=rate, http=direct)
            with psycopg.connect(dsn) as conn:  # 历史 delisted 源迁回 sina（幂等）
                n = store.migrate_delisted_source(conn)
                conn.commit()
                if n:
                    print(f"[migrate] {n} 行 source delisted → sina")
        elif task["source"] == "fund":
            from .crawler.fund import FundClient
            # 免 Cookie（带 Referer 即可），直连 + AIMD 自适应限速
            direct = httpx_async_client(timeout=30.0)
            client = FundClient(rate=rate, http=direct)
        else:
            from . import em_cookie
            cookie = em_cookie.load_cookie()
            if not cookie:
                raise RuntimeError(
                    "东财源需要浏览器 Cookie：请在网页端「数据源 → 东方财富」"
                    "的 Cookie 卡粘贴保存后重试")
            verdict = em_cookie.verify_cookie(cookie)
            if not verdict["valid"]:
                raise RuntimeError(
                    f"东财 Cookie 验证失败：{verdict['reason']}"
                    "——请更换新 Cookie 后重试")
            print(f"[cookie] 东财 Cookie 验证通过（{verdict.get('checked_at')}）")
            # 家宽/办公网直连 + drip 节奏是东财唯一存活路径（免费代理全断，
            # pool 模式实测 ProxyError 循环——与 CLI/爬虫历史结论一致）
            direct = httpx_async_client(timeout=15.0)
            pool = httpx_async_client(base_url=proxy_pool_url, timeout=5.0)
            client = EastmoneyClient(rate=rate, proxy_broker=ProxyBroker(http=pool),
                                     http=direct, proxy_mode="direct",
                                     cookie=cookie)
        try:
            if task["source"] == "fund" and task["mode"] == "incremental":
                # 快照增量：当日净值列表（t=1）一次拉全市场最新已披露净值，
                # 约 250 页/5 分钟（对比 full 逐只全史 8 小时）。快照不含
                # 货币列表，退市标记不在增量刷新（full 负责）
                from .crawler.fund import crawl_nav_snapshot
                with psycopg.connect(dsn) as conn:
                    summary = await crawl_nav_snapshot(
                        client, store, conn, mode="incremental",
                        source="fund", task_id=task["id"],
                        trigger=task.get("_trigger", "manual"),
                        progress=progress,
                        on_batch=lambda _done: conn.commit())
                return {"total": summary["total"], "ok": summary["ok"],
                        "failed": summary["failed"], "bars": summary["bars"],
                        "failures": summary["failures"][:50],
                        "aborted": False, "stopped": summary["stopped"],
                        "error": False}
            stocks = await client.fetch_stock_list()
            with psycopg.connect(dsn) as conn:
                if task["source"] == "fund":
                    store.upsert_fund_basic(conn, stocks)
                else:
                    store.upsert_stock_basic(conn, stocks)
                conn.commit()
                runner = CrawlRunner(
                    client=client, store=store, conn=conn,
                    concurrency=_CONCURRENCY, progress=progress,
                    upsert=(store.upsert_fund_navs
                            if task["source"] == "fund" else None))
                summary = await runner.run(
                    stocks, mode=task["mode"], source=task["source"],
                    batch_size=_BATCH_SIZE, batch_pause=_BATCH_PAUSE,
                    abort_after_consecutive_failures=_ABORT_AFTER,
                    stop_event=stop_event, task_id=task["id"],
                    trigger=task.get("_trigger", "manual"),
                    on_batch=lambda done: conn.commit())
        finally:
            if hasattr(client, "aclose"):
                await client.aclose()
        return {"total": summary.total_stocks, "ok": summary.ok_stocks,
                "failed": summary.failed_stocks, "bars": summary.bars_upserted,
                "failures": summary.failures[:50], "aborted": summary.aborted,
                "stopped": summary.stopped, "error": False}

    return execute


def httpx_async_client(**kw):
    """延迟导入 httpx（便于无网络依赖的纯调度测试装配）。"""
    import httpx
    return httpx.AsyncClient(**kw)


class TaskScheduler:
    def __init__(self, *, dsn: str, poll_interval: float = 30.0,
                 clock=None, sleeper=None, executor=None, lock=None,
                 connect=None, task_store: TaskStore | None = None,
                 runtime: TaskRuntime | None = None,
                 stop_poll_interval: float = 5.0):
        self._dsn = dsn
        self._poll_interval = poll_interval
        self._clock = clock or (lambda: dt.datetime.now(SH_TZ))
        self._sleeper = sleeper or asyncio.sleep
        self._executor = executor
        self._lock = lock
        self._connect = connect or psycopg.connect
        self._tasks = task_store or TaskStore()
        self._runtime = runtime or TaskRuntime(dsn, connect=self._connect)
        self._stop_poll_interval = stop_poll_interval
        self._loop_task: asyncio.Task | None = None

    # ---- 后台循环 ----

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def _run_forever(self) -> None:
        first = True
        while True:
            try:
                await self.scan_once(startup=first)
                while await self.drain_once():    # 串行清空队列
                    pass
            except Exception as exc:          # PG 短暂不可达等：记日志下轮再来
                print(f"[scheduler] 扫描异常（下轮重试）: {exc}")
            first = False
            await self._sleeper(self._poll_interval)

    # ---- 扫描与执行（单步，可测）----

    async def scan_once(self, *, startup: bool = False) -> None:
        """扫描任务表：到期（且今日未计划执行）的启用任务入队。"""
        now = self._clock()
        with self._connect(self._dsn) as conn:
            tasks = await asyncio.to_thread(self._tasks.list_tasks, conn)
            for task in tasks:
                if not task["enabled"]:
                    continue
                if not task["schedule_time"]:
                    continue          # 纯手动任务（无调度时刻）不参与排程
                if await asyncio.to_thread(self._runtime.phase, conn,
                                           task["id"]) in ("queued", "running"):
                    continue
                if await asyncio.to_thread(self._tasks.ran_today, conn, task["id"],
                                           now.date().isoformat()):
                    continue
                hh, mm = task["schedule_time"].split(":")
                due = now.replace(hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
                if now >= due:
                    await asyncio.to_thread(
                        self._runtime.push, conn, task["id"],
                        "catch_up" if startup else "schedule")

    async def drain_once(self) -> bool:
        """执行队列中下一个有效任务（一次一个，全局串行）。无任务返回 False。"""
        while True:
            with self._connect(self._dsn) as conn:
                entry = await asyncio.to_thread(self._runtime.pop_next, conn)
            if entry is None:
                return False
            task = await asyncio.to_thread(self._load_task, entry["task_id"])
            if task is None:                  # 排队期间被删除：跳过
                with self._connect(self._dsn) as conn:
                    await asyncio.to_thread(self._runtime.mark_idle, conn,
                                            entry["task_id"])
                continue
            await self._execute(entry, task)
            return True

    def _load_task(self, task_id: int) -> dict | None:
        with self._connect(self._dsn) as conn:
            return self._tasks.get_task(conn, task_id)

    async def _execute(self, entry: dict, task: dict) -> None:
        task_id = task["id"]
        trigger = entry["trigger"]
        stop_event = asyncio.Event()
        poller = asyncio.create_task(self._poll_stop(task_id, stop_event))
        exec_task = {**task, "_trigger": trigger}

        # 进度落库限频（5s）：爬虫 progress 是股级回调（全量可达万次），
        # 跨进程后逐股打库不可取；首末次立即写，运行中显示 ≤5s 粒度
        last_write, last_state = 0.0, None

        def progress(done: int, total: int) -> None:
            nonlocal last_write, last_state
            state = {"done": done, "total": total}
            if state == last_state:
                return
            now_ts = dt.datetime.now(SH_TZ).timestamp()
            if last_write and now_ts - last_write < 5.0:
                return
            last_write, last_state = now_ts, state
            try:
                with self._connect(self._dsn) as conn:
                    self._runtime.set_progress(conn, task_id, done, total)
            except Exception as exc:      # 进度落库失败不影响执行
                print(f"[task] #{task_id} 进度落库失败（忽略）: {exc}")

        result: dict = {"trigger": trigger, "error": False, "stopped": False,
                        "total": 0, "ok": 0, "failed": 0, "bars": 0,
                        "failures": [], "aborted": False}
        locked = False
        started = dt.datetime.now(SH_TZ)
        print(f"[task] #{task_id} {task['name']} 开始执行（{trigger}，"
              f"{task['source']}/{task['mode']}）")
        try:
            while not (locked := await self._lock.acquire()):
                if stop_event.is_set():       # 等锁期间被停止
                    break
                await self._sleeper(self._poll_interval)
            if locked and not stop_event.is_set():
                result.update(await self._executor(
                    exec_task, progress=progress, stop_event=stop_event))
            result["stopped"] = stop_event.is_set()
        except Exception as exc:              # 执行体异常：记录，不拖垮调度循环
            result["error"] = True
            result["detail"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"[task] #{task_id} 执行异常: {result['detail']}")
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
            if locked:
                await self._lock.release()
            result["started_at"] = started.isoformat()
            result["finished_at"] = dt.datetime.now(SH_TZ).isoformat()
            try:
                with self._connect(self._dsn) as conn:
                    self._runtime.finish(conn, task_id, result)
            except Exception as exc:      # 结果落库失败：行由下次启动清理兜底
                print(f"[task] #{task_id} 结果落库失败（行留 running 由重启清理）: {exc}")
            print(f"[task] #{task_id} 结束：{'已停止' if result['stopped'] else ''}"
                  f"{'异常' if result['error'] else ''} "
                  f"成 {result['ok']} / 败 {result['failed']}")

    async def _poll_stop(self, task_id: int, stop_event: asyncio.Event) -> None:
        """轮询 PG 停止指令 → 进程内 asyncio.Event（executor 各 job 已有
        消费逻辑，此处只是跨进程翻译层）。PG 抖动不放大为任务失败。"""
        while True:
            try:
                with self._connect(self._dsn) as conn:
                    if await asyncio.to_thread(self._runtime.is_stop_requested,
                                               conn, task_id):
                        stop_event.set()
                        return
            except Exception:
                pass
            await asyncio.sleep(self._stop_poll_interval)

    # ---- 管理操作（写 PG 指令行；worker 在另一进程消费——单进程开发
    # 模式下同一进程内即时可见）----

    def request_run(self, task_id: int) -> bool:
        """手动触发（不管 enabled）；排队/运行中拒绝。"""
        with self._connect(self._dsn) as conn:
            return self._runtime.push(conn, task_id, "manual")

    def cancel_queued(self, task_id: int) -> bool:
        with self._connect(self._dsn) as conn:
            return self._runtime.cancel_queued(conn, task_id)

    def stop_running(self, task_id: int) -> bool:
        """停止运行中任务：置 PG 停止标志，≤stop_poll_interval 生效。"""
        with self._connect(self._dsn) as conn:
            return self._runtime.request_stop(conn, task_id)

    def runtime_state(self) -> dict:
        with self._connect(self._dsn) as conn:
            state = self._runtime.snapshot(conn)
        state["server_time"] = self._clock().isoformat()
        return state
