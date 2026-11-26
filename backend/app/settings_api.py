"""应用设置 API：GET /api/settings 读取 + PUT 修改（当前唯一设置项
backtest_min_span_days）。降级约定同全站：PG 不可达仍 200，status=down。
"""

import datetime as dt

from fastapi import HTTPException

from . import dca_backtest, dca_store, settings_store


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

    @app.get("/api/settings/backtest-calendar")
    def backtest_calendar(days: int = 120):
        """日更日历数据源（2026-11-26 二十轮）：crawl_run source='backtest'
        按自然日聚合——日状态 成功>中断>跳过（同日多跑，如夜间中断后白天
        手动补跑成功则该日=成功）；runs 明细供 tooltip。days 钳制 1~365。
        DB down 降级 200 空形状（沿设置端点惯例，日历缺失不拖垮设置页）。"""
        n_days = max(1, min(365, int(days or 120)))
        try:
            with connect(_dsn()) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT (started_at AT TIME ZONE 'Asia/Shanghai')::date "
                    "AS day, count(*), "
                    "count(*) FILTER (WHERE finished_at IS NOT NULL "
                    "AND NOT aborted "
                    "AND COALESCE(detail->>'skipped','') = ''), "
                    "count(*) FILTER (WHERE aborted), "
                    "count(*) FILTER (WHERE "
                    "COALESCE(detail->>'skipped','') <> '') "
                    "FROM crawl_run WHERE source = 'backtest' "
                    "GROUP BY day ORDER BY day DESC LIMIT %s", (n_days,))
                day_rows = cur.fetchall()
                run_rows = []
                if day_rows:
                    cur.execute(
                        "SELECT (started_at AT TIME ZONE 'Asia/Shanghai')"
                        "::date, mode, started_at, finished_at, aborted, "
                        "COALESCE(detail->>'skipped',''), total_stocks, "
                        "ok_stocks FROM crawl_run WHERE source = 'backtest' "
                        "AND (started_at AT TIME ZONE 'Asia/Shanghai')::date "
                        "= ANY(%s::date[]) ORDER BY started_at",
                        ([str(r[0]) for r in day_rows],))
                    run_rows = cur.fetchall()
        except Exception:
            return {"status": "down", "days": []}
        runs_by_day: dict = {}
        for (day, mode, started, finished, aborted, skipped, total,
             ok) in run_rows:
            runs_by_day.setdefault(str(day), []).append({
                "mode": mode, "started_at": _iso(started),
                "finished_at": _iso(finished), "aborted": bool(aborted),
                "skipped": skipped or None, "total_stocks": total,
                "ok_stocks": ok})
        days = [{"date": str(day),
                 "status": ("ok" if ok else "fail" if fail
                            else "skip" if skip else "none"),
                 "runs": runs_by_day.get(str(day), [])}
                for (day, _n, ok, fail, skip) in day_rows]
        return {"status": "up", "days": days,
                "server_now": _iso(dt.datetime.now(dt.timezone.utc))}

    @app.get("/api/settings/backtest-method-full-dates")
    def backtest_method_full_dates():
        """每策略最近「全量回测成功」时间（2026-11-26 二十一轮，用户需求
        ——哪个策略数据还停在旧宇宙一眼可见）：两路并集取最新——①夜更
        分阶段合并的新鲜度戳 fund_dca_method_meta.computed_at（二十轮）；
        ②单策略按钮成功运行（crawl_run source='backtest_method' mode=
        策略、已收尾、未中断、非 skipped）。仅 DCA_METHODS 白名单内计入；
        从未成功 → null；DB down 降级 200 空形状（设置端点惯例）。"""
        try:
            with connect(_dsn()) as conn, conn.cursor() as cur:
                # 夜更分阶段今晚才首跑——生产库可能尚无该表，先幂等 ensure
                # （ UndefinedTable 会让端点误入 down 降级，显示全「—」）
                dca_store.ensure_method_meta_schema(conn)
                cur.execute("SELECT method, computed_at "
                            "FROM fund_dca_method_meta")
                meta_rows = cur.fetchall()
                cur.execute(
                    "SELECT mode, max(finished_at) FROM crawl_run "
                    "WHERE source = 'backtest_method' "
                    "AND finished_at IS NOT NULL AND NOT aborted "
                    "AND COALESCE(detail->>'skipped','') = '' "
                    "GROUP BY mode")
                run_rows = cur.fetchall()
        except Exception:
            return {"status": "down", "methods": {}}
        latest: dict = {}
        for method, computed_at in meta_rows:
            if computed_at is not None:
                latest[str(method)] = computed_at
        for mode, finished_at in run_rows:
            if finished_at is not None and \
                    (latest.get(str(mode)) is None
                     or finished_at > latest[str(mode)]):
                latest[str(mode)] = finished_at
        methods = {m: (_iso(latest[m]) if m in latest else None)
                   for m in dca_backtest.DCA_METHODS}
        return {"status": "up", "methods": methods,
                "server_now": _iso(dt.datetime.now(dt.timezone.utc))}

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
    aborted / blocking（串行队列前方运行中任务名，排队提示）/ last_error
    （上次运行错误摘要，静默失败可见性）。查询关键词与日更快照逐条可
    区分（假连接按关键词路由）。"""
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
        # 排队可见性（2026-11-25 十一轮）：串行队列前方运行中的任务名
        cur.execute("SELECT c.name FROM crawl_run r "
                    "JOIN crawl_task c ON c.id = r.task_id "
                    "WHERE r.finished_at IS NULL ORDER BY r.id DESC LIMIT 1")
        brow = cur.fetchone()
        # 静默失败修复（同轮）：上次运行错误摘要（last_result.error 时取
        # detail——旧 worker 时代 KeyError 吞掉点击后 UI 毫无提示的教训）
        cur.execute("SELECT last_result FROM task_runtime WHERE task_id = %s",
                    (mt_id,) if mt_id else (None,))
        lrow = cur.fetchone()
    lr = lrow[0] if lrow else None
    last_error = (str(lr["detail"])[:200]
                  if isinstance(lr, dict) and lr.get("error")
                  and lr.get("detail") else None)
    return {"task_id": trow[0] if trow else None,
            "method": trow[1] if trow else None,
            "phase": (prow[0] if prow else None) or "idle",
            "running": bool(trow is not None and run is not None and run[0]),
            "last_started_at": _iso(run[2] if run else None),
            "last_finished_at": _iso(run[3] if run else None),
            "last_aborted": (bool(run[1]) if run else None),
            "blocking": (brow[0] if brow and isinstance(brow[0], str)
                         else None),
            "last_error": last_error,
            **progress,
            "server_now": _iso(dt.datetime.now(dt.timezone.utc))}
