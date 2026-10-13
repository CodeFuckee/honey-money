"""honey-money 后端：FastAPI 应用工厂。

接口分组：
- /healthz                存活探针
- /api/infra              PostgreSQL / 代理池 连通探测
- /api/stocks[/bars]      数据浏览
- /api/crawl/stats        爬虫数据统计
- /api/tasks[...]         爬虫任务管理（CRUD/触发/取消/停止/历史）

任务调度：lifespan 启动时初始化任务表（含预置任务）并拉起进程内调度循环
（每日到点自动爬取 + 启动补跑，见 app/scheduler.py）；SCHEDULER_ENABLED=0
可整体关闭（任务 CRUD 仍可用，仅不再自动执行）。
"""

from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI

from . import config
from . import backtest_full
from .backtest_api import register_backtest_routes
from .backtest_full_api import register_full_backtest_routes
from .browse import fetch_stock_bars, fetch_stock_basic, fetch_stock_list
from .crawler.store import Store
from .crawl_stats import fetch_crawl_stats
from .infra import check_postgres, check_proxy_pool
from .scheduler import PgAdvisoryLock, TaskScheduler, build_crawl_executor
from .task_store import TaskStore
from .tasks_api import register_task_routes


def create_app(*, scheduler: TaskScheduler | None = None,
               task_store: TaskStore | None = None, connect=None) -> FastAPI:
    """依赖注入参数供测试换假件；缺省按配置构造生产件。"""
    task_store = task_store or TaskStore()
    connect = connect or psycopg.connect
    if scheduler is None:
        scheduler = TaskScheduler(
            dsn=config.DATABASE_URL,
            executor=build_crawl_executor(config.DATABASE_URL, config.PROXY_POOL_URL),
            lock=PgAdvisoryLock(config.DATABASE_URL))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 三个初始化各自独立事务/失败域：一个挂不回滚其他
        # （教训：任务 seed 曾与建表同事务，NULL 违约连带回滚新表）
        for name, init in (("基础表", Store().init_schema),
                           ("任务表", task_store.init_schema),
                           ("全量回测表", backtest_full.ensure_schema)):
            try:
                with connect(config.DATABASE_URL) as conn:
                    init(conn)
                    conn.commit()
            except Exception as exc:               # PG 暂不可达：服务照常起
                print(f"[startup] {name}初始化失败（不影响其他表）: {exc}")
        try:
            # 重启恢复：遗留 running 标 interrupted（须在建表提交后执行，
            # 否则第二连接看不到未提交的新表并使整个初始化回滚）
            backtest_full.mark_interrupted_on_startup()
        except Exception as exc:
            print(f"[startup] 全量回测中断标记失败（表未建好时跳过）: {exc}")
        if config.SCHEDULER_ENABLED:
            await scheduler.start()
        # vectorbt numba 编译预热（后台）：部署后首次用户回测从数十秒降到秒级
        import asyncio
        from .backtest.engine import warmup
        asyncio.create_task(asyncio.to_thread(warmup))
        yield
        await scheduler.stop()

    from fastapi.middleware.gzip import GZipMiddleware
    app = FastAPI(title="honey-money backend", version=config.SERVICE_VERSION,
                  lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1000)   # 全量曲线 ~10MB 压缩传输
    app.state.scheduler = scheduler

    @app.get("/healthz")
    def healthz():
        """存活探针：本进程是否正常（Kubernetes/容器层健康检查用）。"""
        return {"status": "ok", "service": "honey-money-backend",
                "version": config.SERVICE_VERSION}

    @app.get("/api/infra")
    def infra():
        """基础设施连通性：PostgreSQL 与代理池各自探测、互不影响。

        始终返回 200（本服务活着）；下游状态在 body 的 status 字段里表达，
        前端仪表盘据此渲染红绿灯。
        """
        return {
            "postgres": check_postgres(config.DATABASE_URL),
            "proxy_pool": check_proxy_pool(config.PROXY_POOL_URL),
        }

    @app.get("/api/stocks")
    def stocks(q: str = "", limit: int = 50, offset: int = 0, source: str = ""):
        """数据浏览：股票列表（最新价/涨跌幅/K线数/区间/来源），q 模糊搜索。

        source 非空时仅返回该数据源（sina/eastmoney）下有K线的股票。
        始终 200；PG 不可达在 status=down 表达（同 /api/infra 降级约定）。
        """
        return fetch_stock_list(config.DATABASE_URL, q=q, limit=limit,
                                offset=offset, source=source.strip().lower())

    @app.get("/api/stocks/basic")
    def stocks_basic():
        """全清单 code+name（教学页代码下拉数据源）；降级约定同 /api/infra。"""
        try:
            return fetch_stock_basic(config.DATABASE_URL)
        except Exception as exc:                       # PG 不可达：降级而非 5xx
            return {"status": "down",
                    "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
                    "total": 0, "stocks": []}

    @app.get("/api/stocks/{code}/bars")
    def stock_bars(code: str, limit: int = 250, offset: int = 0, source: str = "",
                   adjust: str = ""):
        """单股日K（默认最新 250 根，时间升序返回，供图表使用）。

        source 非空时仅取该来源（个股详情页来源切换）；adjust="qfq" 前复权
        （因子懒加载，失败降级不复权并在 adjust_applied 表达）。
        """
        return fetch_stock_bars(config.DATABASE_URL, code, limit=limit,
                                offset=offset, source=source.strip().lower(),
                                adjust=adjust.strip().lower())

    @app.get("/api/crawl/stats")
    def crawl_stats():
        """爬虫数据统计：K 线总数 / 股票覆盖进度 / 待爬 / 最近运行。

        始终 200；PG 不可达在 status=down 表达（同 /api/infra 的降级约定）。
        """
        return fetch_crawl_stats(config.DATABASE_URL)

    register_task_routes(app, scheduler=scheduler, task_store=task_store,
                         connect=connect)
    register_backtest_routes(app)
    register_full_backtest_routes(app)

    return app


app = create_app()  # uvicorn 入口：uvicorn app.main:app
