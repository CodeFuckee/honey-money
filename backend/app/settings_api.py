"""应用设置 API：GET /api/settings 读取 + PUT 修改（当前唯一设置项
backtest_min_span_days）。降级约定同全站：PG 不可达仍 200，status=down。
"""

from fastapi import HTTPException

from . import dca_backtest, settings_store


def register_settings_routes(app, connect) -> None:
    """挂载设置路由（connect 注入供测试换假件，同 task 路由惯例）。"""

    @app.get("/api/settings")
    def get_settings():
        """设置全集（当前仅运作期限下限）；PG 不可达降级 status=down。"""
        try:
            with connect(_dsn()) as conn:
                value = settings_store.get_min_span_days(
                    conn, default=dca_backtest.MIN_SPAN_DAYS)
            return {"status": "up",
                    "settings": {"backtest_min_span_days": value}}
        except Exception as exc:
            return {"status": "down", "detail": str(exc), "settings": None}

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
