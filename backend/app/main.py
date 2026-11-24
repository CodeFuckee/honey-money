"""honey-money 后端：FastAPI 应用工厂。

接口分组：
- /healthz                存活探针
- /api/infra              PostgreSQL / 代理池 连通探测
- /api/stocks[/bars]      数据浏览
- /api/crawl/stats        爬虫数据统计
- /api/tasks[...]         爬虫任务管理（CRUD/触发/取消/停止/历史）

任务调度：生产拓扑下任务执行独立到 worker 容器（python -m app.worker，
见 app/worker.py）——backend 只留 API（compose 设 SCHEDULER_ENABLED=0），
流水线重建 backend 不再打断运行中任务；运行态/指令经 PG task_runtime
跨进程（见 app/task_runtime.py）。SCHEDULER_ENABLED=1 仍支持单进程开发
模式（调度器宿主于 backend，行为与拆分前一致）。
"""

from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI

from . import config
from . import em_cookie
from . import startup
from .backtest_api import register_backtest_routes
from .backtest_full_api import register_full_backtest_routes
from .browse import fetch_fund_list, fetch_fund_navs, fetch_stock_bars, fetch_stock_basic, fetch_stock_list
from .crawl_stats import fetch_crawl_stats
from .dca_api import register_fund_dca_routes
from .infra import check_postgres, check_proxy_pool
from .market_compare import register_market_routes
from .market_crises import register_crises_routes
from .settings_api import register_settings_routes
from .scheduler import PgAdvisoryLock, TaskScheduler, build_crawl_executor
from .task_runtime import PgTaskControl
from .task_store import TaskStore
from .tasks_api import register_task_routes


def create_app(*, scheduler: TaskScheduler | None = None,
               task_store: TaskStore | None = None, connect=None,
               task_control: PgTaskControl | None = None) -> FastAPI:
    """依赖注入参数供测试换假件；缺省按配置构造生产件。"""
    task_store = task_store or TaskStore()
    connect = connect or psycopg.connect
    task_control = task_control or PgTaskControl(config.DATABASE_URL,
                                                 connect=connect)
    if scheduler is None:
        scheduler = TaskScheduler(
            dsn=config.DATABASE_URL,
            executor=build_crawl_executor(config.DATABASE_URL, config.PROXY_POOL_URL),
            lock=PgAdvisoryLock(config.DATABASE_URL))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 兜底建表/补列清单在 app/startup.py（backend 与 worker 共用的单一
        # 事实源——教训2/3：清单漏表 → UndefinedTable / 新代码读新列整页
        # down；每项独立事务/失败域）
        startup.run_schema_inits(connect, task_store=task_store)
        if config.SCHEDULER_ENABLED:
            # 遗留运行清理只在宿主调度器的进程做：compose 下 backend 是纯
            # API（=0），worker 可能正在跑，重启 backend 不得误标它的活
            # 运行行——worker 启动时自己做（见 startup.cleanup_interrupted_runs）
            startup.cleanup_interrupted_runs(connect)
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

    # API 响应禁缓存（2026-11-24）：回测快照每次部署/回填后立即生效——
    # 公网入口雷池 SafeLine 会缓存无 Cache-Control 的 GET 响应，曾致
    # 数据更新后公网入口仍吐旧 JSON（内网入口正常，公网口径落后）。
    # 静态前端资源（/ 非 /api）不带此头，继续吃浏览器长缓存。
    @app.middleware("http")
    async def _no_store_api(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path == "/api":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.scheduler = scheduler
    app.state.task_control = task_control

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
    def stocks(q: str = "", limit: int = 50, offset: int = 0, source: str = "",
               delisted: str = ""):
        """数据浏览：股票列表（最新价/涨跌幅/K线数/区间/来源），q 模糊搜索。

        source 非空时仅返回该数据源（sina/eastmoney）下有K线的股票；
        delisted="false"/"true" 仅在市/仅退市，其余值不过滤。
        始终 200；PG 不可达在 status=down 表达（同 /api/infra 降级约定）。
        """
        return fetch_stock_list(config.DATABASE_URL, q=q, limit=limit,
                                offset=offset, source=source.strip().lower(),
                                delisted=delisted)

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
                                offset=offset, source=source,
                                adjust=adjust.strip().lower())

    @app.get("/api/funds")
    def funds(q: str = "", limit: int = 50, offset: int = 0,
              delisted: str = "", show_backend: str = "",
              show_money: str = "", show_qdii: str = "",
              sort: str = "code", order: str = "asc"):
        """基金列表（净值摘要 + 波动指标列：年化/下行波动率/最大回撤/夏普
        ·近1年 + 当前滚动波动率及分位）。

        语义与 /api/stocks 一致：始终 200，PG 不可达在 status=down 表达；
        delisted 三态过滤同 /api/stocks；show_backend=1 / show_money=1 /
        show_qdii=1 才显示后端收费份额 / 货币基金 / QDII（默认隐藏）；
        sort 白名单排序（code 默认 + 波动指标列，白名单外回落），order 仅
        asc 生效升序（同 /api/funds/dca 口径），恒 NULLS LAST + code 稳定序。
        """
        return fetch_fund_list(config.DATABASE_URL, q=q, limit=limit,
                               offset=offset,
                               delisted=delisted.strip().lower(),
                               show_backend=show_backend.strip().lower(),
                               show_money=show_money.strip().lower(),
                               show_qdii=show_qdii.strip().lower(),
                               sort=sort.strip().lower(),
                               order=order.strip().lower())

    @app.get("/api/funds/{code}/navs")
    def fund_navs(code: str, limit: int = 250, offset: int = 0):
        """单只基金净值历史（升序返回，供图表/明细分页，同 bars 约定）。"""
        return fetch_fund_navs(config.DATABASE_URL, code, limit=limit,
                               offset=offset)

    @app.get("/api/cookies/em")
    def em_cookie_status():
        """东财 Cookie 状态：是否已存 + 实时验证结果（前端提醒与表单用）。"""
        cookie = em_cookie.load_cookie()
        result = em_cookie.verify_cookie(cookie) if cookie else {
            "valid": False, "reason": "尚未设置 Cookie", "checked_at": None}
        masked = (cookie[:12] + "…" + cookie[-6:]) if len(cookie) > 20 else ""
        return {"status": "up", "configured": bool(cookie), "masked": masked,
                **result}

    @app.put("/api/cookies/em")
    def em_cookie_save(body: dict):
        """保存并立即验证前端粘贴的 Cookie。无效也保存（便于换 Cookie 后重验）。"""
        cookie = (body or {}).get("cookie", "")
        if not cookie.strip():
            return {"ok": False, "reason": "cookie 为空"}
        path = em_cookie.save_cookie(cookie)
        result = em_cookie.verify_cookie(cookie)
        return {"ok": True, "path": path, **result}

    @app.delete("/api/cookies/em")
    def em_cookie_delete():
        em_cookie.clear_cookie()
        return {"ok": True}

    @app.get("/api/crawl/stats")
    def crawl_stats():
        """爬虫数据统计：K 线总数 / 股票覆盖进度 / 待爬 / 最近运行。

        始终 200；PG 不可达在 status=down 表达（同 /api/infra 的降级约定）。
        """
        return fetch_crawl_stats(config.DATABASE_URL)

    register_task_routes(app, task_control=task_control, task_store=task_store,
                         connect=connect)
    register_backtest_routes(app)
    register_full_backtest_routes(app)
    register_fund_dca_routes(app)
    register_market_routes(app)
    register_crises_routes(app)
    register_settings_routes(app, connect=connect)

    return app


app = create_app()  # uvicorn 入口：uvicorn app.main:app
