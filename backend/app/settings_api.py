"""应用设置 API：GET /api/settings 读取 + PUT 修改（当前唯一设置项
backtest_min_span_days）。降级约定同全站：PG 不可达仍 200，status=down。
"""

import datetime as dt

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
        6~10 小时，确认对话框的时间提示据此渲染）。method_task 供「运行
        单策略全量」按钮（2026-11-25 九轮）：「单策略全量回测」任务快照，
        快照异常降级 None 不拖垮整个 GET。"""
        try:
            with connect(_dsn()) as conn:
                value = settings_store.get_min_span_days(
                    conn, default=dca_backtest.MIN_SPAN_DAYS)
                task = _backtest_task_state(conn, value)
                try:
                    method_task = _method_task_state(conn)
                except Exception:
                    method_task = None       # 局部快照失败不拖垮整体
            return {"status": "up",
                    "settings": {"backtest_min_span_days": value},
                    "backtest_task": task, "method_task": method_task}
        except Exception as exc:
            return {"status": "down", "detail": str(exc), "settings": None,
                    "backtest_task": None, "method_task": None}

    @app.post("/api/settings/backtest-method-run")
    def post_backtest_method_run(body: dict):
        """单策略全量运行前置（2026-11-25 九轮定案：六策略全可选/进任务
        系统/日更运行中直接 409）：校验 method → 改写预置任务「单策略
        全量回测」的 mode（目标策略随任务行走，任务页可见历史可停止）→
        返回 task_id 供前端复用任务系统 POST /api/tasks/{id}/run 触发
        （全局串行队列 + advisory lock + 409 防重，零新触发逻辑）。
        前置拒绝：method 非白名单 400；日更运行中（crawl_run
        source='backtest' 未收尾）409；方法任务排队/运行中
        （task_runtime.phase）409。"""
        method = (body or {}).get("method")
        if method not in dca_backtest.DCA_METHODS:
            raise HTTPException(
                status_code=400,
                detail="method 须为 " + "/".join(dca_backtest.DCA_METHODS))
        try:
            with connect(_dsn()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT finished_at IS NULL FROM crawl_run "
                                "WHERE source = 'backtest' "
                                "ORDER BY id DESC LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        raise HTTPException(
                            status_code=409,
                            detail="回测日更运行中，请等待完成后再运行"
                                   "单策略全量")
                    cur.execute("SELECT id FROM crawl_task WHERE source = "
                                "'backtest_method' ORDER BY id LIMIT 1")
                    trow = cur.fetchone()
                    if trow is None:
                        raise HTTPException(
                            status_code=404,
                            detail="预置任务「单策略全量回测」不存在"
                                   "（旧库未升级，重启后端自愈）")
                    task_id = trow[0]
                    cur.execute("SELECT phase FROM task_runtime "
                                "WHERE task_id = %s", (task_id,))
                    prow = cur.fetchone()
                    phase = (prow[0] if prow else None) or "idle"
                    if phase in ("queued", "running"):
                        raise HTTPException(
                            status_code=409,
                            detail="单策略全量任务已在"
                                   + ("排队中" if phase == "queued"
                                      else "运行中")
                                   + "，请等待完成")
                    cur.execute("UPDATE crawl_task SET mode = %s "
                                "WHERE id = %s", (method, task_id))
                conn.commit()
        except HTTPException:
            raise
        except Exception as exc:
            return {"status": "down", "detail": str(exc)}
        return {"status": "up", "task_id": task_id, "method": method,
                "note": "目标策略已更新，触发经任务系统执行"}

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
        progress = _progress_fields(cur, task_id) if task_id else \
            {"progress_done": None, "progress_total": None}
        return {"task_id": task_id,
                "running": running,
                "estimate": ("dirty" if fp_n and fp_matched else "full"),
                "last_started_at": _iso(run[2] if run else None),
                "last_finished_at": _iso(run[3] if run else None),
                **progress,
                # 进度推算时间基准：已运行时长/进度条按服务端时刻差渲染
                # （客户端时钟不可信；含时区偏移的 ISO 串）
                "server_now": _iso(dt.datetime.now(dt.timezone.utc))}


def _iso(value):
    """时间戳转 ISO 串（None 直通；前端仅展示用）。"""
    return value.isoformat() if value is not None else None


def _progress_fields(cur, task_id):
    """task_runtime.progress JSONB {done,total} → progress_done/progress_total
    （引擎分块回调写入，2026-11-25 十轮；无行/形状不符一律 None——前端
    回落时间推算）。查询关键词与 phase 查询可区分（假连接按词路由）。"""
    if task_id is None:
        return {"progress_done": None, "progress_total": None}
    try:
        cur.execute("SELECT progress FROM task_runtime WHERE task_id = %s",
                    (task_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    prog = row[0] if row else None
    if isinstance(prog, dict) and isinstance(prog.get("done"), int) \
            and isinstance(prog.get("total"), int):
        return {"progress_done": prog["done"],
                "progress_total": prog["total"]}
    return {"progress_done": None, "progress_total": None}


def _method_task_state(conn) -> dict:
    """「单策略全量回测」任务状态快照（纯 SQL，不依赖 scheduler 对象）：
    task_id / mode（当前目标策略）/ 排队相位（task_runtime.phase，缺行
    视为 idle）/ running（crawl_run source='backtest_method' 最新行未
    收尾——与日更快照的 source='backtest' 过滤互不污染）/ 最近起止与
    aborted。查询关键词与日更快照逐条可区分（假连接按关键词路由）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT id, mode FROM crawl_task "
                    "WHERE source = 'backtest_method' ORDER BY id LIMIT 1")
        trow = cur.fetchone()
        cur.execute("SELECT phase FROM task_runtime WHERE task_id = %s",
                    (trow[0],) if trow else (None,))
        prow = cur.fetchone()
        cur.execute("SELECT finished_at IS NULL, aborted, started_at, "
                    "finished_at FROM crawl_run WHERE source = "
                    "'backtest_method' ORDER BY id DESC LIMIT 1")
        run = cur.fetchone()
        mt_id = trow[0] if trow else None
        progress = _progress_fields(cur, mt_id) if mt_id else \
            {"progress_done": None, "progress_total": None}
    return {"task_id": trow[0] if trow else None,
            "method": trow[1] if trow else None,
            "phase": (prow[0] if prow else None) or "idle",
            "running": bool(trow is not None and run is not None and run[0]),
            "last_started_at": _iso(run[2] if run else None),
            "last_finished_at": _iso(run[3] if run else None),
            "last_aborted": (bool(run[1]) if run else None),
            **progress,
            "server_now": _iso(dt.datetime.now(dt.timezone.utc))}
