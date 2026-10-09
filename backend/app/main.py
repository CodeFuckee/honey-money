"""honey-money 后端：FastAPI 应用工厂。

当前为工程化骨架——只有健康检查与基础设施连通探测两类接口，
业务（行情数据/回测/实盘信号）在后续版本逐步迁入。
"""

from fastapi import FastAPI

from . import config
from .browse import fetch_stock_bars, fetch_stock_list
from .crawl_stats import fetch_crawl_stats
from .infra import check_postgres, check_proxy_pool


def create_app() -> FastAPI:
    app = FastAPI(title="honey-money backend", version=config.SERVICE_VERSION)

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

    @app.get("/api/stocks/{code}/bars")
    def stock_bars(code: str, limit: int = 250, offset: int = 0):
        """单股日K（默认最新 250 根，时间升序返回，供图表使用）。"""
        return fetch_stock_bars(config.DATABASE_URL, code, limit=limit, offset=offset)

    @app.get("/api/crawl/stats")
    def crawl_stats():
        """爬虫数据统计：K 线总数 / 股票覆盖进度 / 待爬 / 最近运行。

        始终 200；PG 不可达在 status=down 表达（同 /api/infra 的降级约定）。
        """
        return fetch_crawl_stats(config.DATABASE_URL)

    return app


app = create_app()  # uvicorn 入口：uvicorn app.main:app
