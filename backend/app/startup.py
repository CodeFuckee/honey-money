"""启动兜底初始化：建表/补列/遗留运行清理，backend lifespan 与 worker 入口共用。

单一事实源——新增表/列一律加进 run_schema_inits 清单（教训2/3：清单漏表
→ 首次任务 UndefinedTable 秒退无 run 记录（2026-10-26）/ 新代码 SELECT
新列后、当日日更前生产接口整页 down（2026-11-19，同根：启动即兜底建表
补列）。backend 与 worker 两个进程都跑同一清单，谁先启动谁兜底。

遗留运行清理（cleanup_interrupted_runs）只允许「宿主调度器的进程」调用：
backend 单进程开发模式（SCHEDULER_ENABLED=1，调度器在自己进程里）或
worker。compose 下 backend 是纯 API（SCHEDULER_ENABLED=0）绝不调用——
worker 可能正在跑，重启 backend 不得误标它的活运行行（这正是任务执行
独立到 worker 后才成立的新约束，反了会把运行中任务的状态标成 aborted）。
"""

from . import backtest_full
from . import dca_store
from . import index_store
from . import settings_store
from .crawler.fund_store import FundStore
from .crawler.store import Store
from .task_runtime import TaskRuntime


def run_schema_inits(connect, task_store=None, dsn: str | None = None,
                     runtime: TaskRuntime | None = None) -> list[str]:
    """逐项兜底建表/补列，返回失败项名（每项独立事务/失败域：一个挂不
    回滚其他；教训：任务 seed 曾与建表同事务，NULL 违约连带回滚新表）。"""
    from . import config

    dsn = dsn or config.DATABASE_URL
    task_store = task_store or TaskStore()
    inits = (
        ("基础表", Store().init_schema),
        ("任务表", task_store.init_schema),
        ("任务运行态表", (runtime or TaskRuntime(dsn, connect=connect)).ensure_schema),
        ("基金表", FundStore().init_schema),
        ("全量回测表", backtest_full.ensure_schema),
        # （旧口径表 fund_dca_result/rounds 已退役——2026-11-26 二十二轮
        #  单一数据源，启动清单不再建表；生产旧表由运维 DROP）
        ("多阈值回测表", dca_store.ensure_tp_schema),
        ("回测指纹表", dca_store.ensure_fp_schema),
        ("基金波动表", dca_store.ensure_risk_schema),
        ("熊市断缴表", dca_store.ensure_bear_pause_schema),
        ("指数表", index_store.ensure_schema),
        ("设置表", settings_store.ensure_settings_schema),
    )
    failures = []
    for name, init in inits:
        try:
            with connect(dsn) as conn:
                init(conn)
                conn.commit()
        except Exception as exc:               # PG 暂不可达：服务照常起
            print(f"[startup] {name}初始化失败（不影响其他表）: {exc}")
            failures.append(name)
    return failures


def cleanup_interrupted_runs(connect, dsn: str | None = None,
                             runtime: TaskRuntime | None = None) -> None:
    """遗留运行清理（仅宿主调度器的进程调用，见模块 docstring）：
    ① crawl_run 未收尾行标 aborted（重启杀掉运行与 spawn 子进程后行会
       永远停在 finished_at IS NULL，任务状态与回测进度判定被陈旧行污染
       ——2026-11-24）；
    ② task_runtime running 行回 idle（同源兜底；排队行保留=队列跨重启
       存活）。须在建表提交后执行，否则第二连接看不到未提交的新表。"""
    from . import config

    dsn = dsn or config.DATABASE_URL
    runtime = runtime or TaskRuntime(dsn, connect=connect)
    try:
        # 重启恢复：遗留 running 标 interrupted
        backtest_full.mark_interrupted_on_startup()
    except Exception as exc:
        print(f"[startup] 全量回测中断标记失败（表未建好时跳过）: {exc}")
    try:
        with connect(dsn) as conn:
            n = Store().mark_interrupted_on_startup(conn)
            conn.commit()
        if n:
            print(f"[startup] 遗留运行标记 interrupted：{n} 行")
    except Exception as exc:
        print(f"[startup] 遗留运行标记失败（跳过）: {exc}")
    try:
        with connect(dsn) as conn:
            n = runtime.mark_running_idle(conn)
            conn.commit()
        if n:
            print(f"[startup] 运行态遗留 running 行回 idle：{n} 行")
    except Exception as exc:
        print(f"[startup] 运行态遗留行清理失败（跳过）: {exc}")
