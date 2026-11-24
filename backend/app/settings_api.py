"""应用设置 API：GET /api/settings 读取 + PUT 修改（当前唯一设置项
backtest_min_span_days）。降级约定同全站：PG 不可达仍 200，status=down。
"""

from fastapi import HTTPException

from . import dca_backtest, settings_store


def register_settings_routes(app, connect) -> None:
    """挂载设置路由（connect 注入供测试换假件，同 task 路由惯例）。"""

    @app.get("/api/settings")
    def get_settings():
        """设置全集（当前仅运作期限下限）+ 回测任务状态；PG 不可达降级
        status=down。backtest_task 供设置页「立即运行全市场回测」按钮：
        task_id=预置「定投回测日更」任务（触发走任务系统 POST /run，
        advisory lock + 409 防重）、running=crawl_run 最新行未收尾、
        estimate=指纹表与引擎版本串比对（dirty=增量分钟级 / full=全量
        6~10 小时，确认对话框的时间提示据此渲染）。"""
        try:
            with connect(_dsn()) as conn:
                value = settings_store.get_min_span_days(
                    conn, default=dca_backtest.MIN_SPAN_DAYS)
                task = _backtest_task_state(conn, value)
            return {"status": "up",
                    "settings": {"backtest_min_span_days": value},
                    "backtest_task": task}
        except Exception as exc:
            return {"status": "down", "detail": str(exc), "settings": None,
                    "backtest_task": None}

    @app.put("/api/settings/backtest-min-span")
    def put_min_span_days(body: dict):
        """修改运作期限下限（整数 90~3650）；下一次回测生效（存量行不重算）。"""
        try:
            value = settings_store.validate_min_span_days(
                (body or {}).get("value"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            with connect(_dsn()) as conn:
                settings_store.set_setting(
                    conn, settings_store.KEY_MIN_SPAN_DAYS, str(value))
                conn.commit()
        except Exception as exc:
            return {"status": "down", "detail": str(exc)}
        return {"status": "up", "value": value, "note": "下一次回测生效"}


def _dsn():
    """配置 DSN（经函数取，便于测试 monkeypatch config）。"""
    from . import config
    return config.DATABASE_URL


def _backtest_task_state(conn, min_span_days: int) -> dict:
    """「定投回测日更」任务状态快照（纯 SQL，不依赖 scheduler 对象）：
    预置任务 id / 是否运行中（crawl_run 最新行 finished_at IS NULL）/ 增量
    还是全量预估（fund_dca_fp 指纹的 engine 串与当前 engine_version 比较
    ——运作期限设置或引擎任一变化都会不匹配 → 全量）。fp 表缺失等异常
    一律兜底全量预估（保守方向，时间提示宁长勿短）。"""
    engine = dca_backtest.engine_version(min_span_days)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM crawl_task WHERE source = 'backtest' "
                    "ORDER BY id LIMIT 1")
        row = cur.fetchone()
        task_id = row[0] if row else None
        cur.execute("SELECT finished_at IS NULL, aborted, started_at, "
                    "finished_at FROM crawl_run WHERE source = 'backtest' "
                    "ORDER BY id DESC LIMIT 1")
        run = cur.fetchone()
        try:
            cur.execute("SELECT count(*), COALESCE(bool_or(engine = %s), "
                        "false) FROM fund_dca_fp", (engine,))
            fp_n, fp_matched = cur.fetchone()
        except Exception:
            fp_n, fp_matched = 0, False
    running = bool(task_id is not None and run is not None and run[0])
    return {"task_id": task_id,
            "running": running,
            "estimate": ("dirty" if fp_n and fp_matched else "full"),
            "last_started_at": _iso(run[2] if run else None),
            "last_finished_at": _iso(run[3] if run else None)}


def _iso(value):
    """时间戳转 ISO 串（None 直通；前端仅展示用）。"""
    return value.isoformat() if value is not None else None
