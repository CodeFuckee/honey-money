"""设置页 E2E（2026-11-24）：回测年化运作期限下限（app_settings 表）。

用例表（确认门 1）：
- E2E-471 P0 主流程：侧栏第 6 项「设置」→ 页面渲染（h2「设置」）→ 输入框
  回显当前值 90（mock GET /api/settings）→ 改 120 → 保存 → PUT
  /api/settings/backtest-min-span 请求体 {"value":120} → 成功提示
  「下一次回测生效」可见；
- E2E-472 P1 非法值：输入 50 → 后端 400（mock 返回 detail）→ 错误提示含
  detail 文案；清空输入 → 前端拦截提示、不发包（PUT 次数不增）；
- E2E-473 P1 深链 /settings 直达 + down 降级（不 mock，走 conftest 坏
  DSN 真后端 →「设置数据不可用」占位；
- E2E-474 P0 立即运行主流程：按钮 → 确认框含时间文案「约 6~10 小时」
  → 确认 → POST /api/tasks/7/run 契约命中 + 「已触发」提示 + 按钮转
  运行中禁用；
- E2E-475 P1 取消不发包 + 409 已在运行分支错误提示；
- E2E-476 P1 运行中状态：mock GET running=true → 按钮禁用 + 运行中
  状态可见；
- E2E-481 P0 单策略全量主流程（2026-11-25 九轮）：策略选择器六枚、默认
  选中「盈亏阶梯」→ 切「深回撤加码」→ 运行按钮 → 确认框含「10 分钟~
  2 小时」与策略名 → 确认 → POST /api/settings/backtest-method-run
  载荷 {"method":"dd_ladder_deep"} + POST /api/tasks/9/run + 已触发提示
  + 按钮转禁用；
- E2E-482 P1 单策略守卫：取消不发包；前置 409（日更运行中）→ 错误提示
  含「日更」且不触发任务运行；method_task running=true → 按钮禁用 +
  运行中状态可见。
运行：python -m pytest tests/e2e/test_settings_page.py
"""

import json
import re

from playwright.sync_api import expect

MOCK_SETTINGS = {"status": "up",
                 "settings": {"backtest_min_span_days": 90},
                 "backtest_task": {"task_id": 7, "running": False,
                                   "estimate": "full",
                                   "last_started_at": None,
                                   "last_finished_at": None},
                 "method_task": {"task_id": 9, "method": "pl_ladder",
                                 "phase": "idle", "running": False,
                                 "last_started_at": None,
                                 "last_finished_at": None,
                                 "last_aborted": None}}


def _install_mock(page, put_status=200, put_body=None,
                  run_status=200, run_body=None, task=MOCK_SETTINGS,
                  method_pre_status=200, method_pre_body=None):
    """拦截 /api/settings*（GET 回显/PUT 记录/方法前置 POST 记录）与
    /api/tasks/*/run（POST 记录并可指定 409 等应答）。"""
    calls = {"get": [], "put": [], "run": [], "method_pre": []}

    def _route(route):
        req = route.request
        if "/tasks/" in req.url:
            calls["run"].append(req.url)
            body = run_body if run_body is not None else \
                {"triggered": 7, "queue_position": 0}
            route.fulfill(status=run_status, content_type="application/json",
                          body=json.dumps(body))
            return
        if "backtest-method-run" in req.url:
            calls["method_pre"].append(req.post_data)
            if method_pre_body is not None:
                body = method_pre_body
            else:
                method = (json.loads(req.post_data or "{}")).get("method")
                body = {"status": "up", "task_id": 9, "method": method,
                        "note": "目标策略已更新，触发经任务系统执行"}
            route.fulfill(status=method_pre_status,
                          content_type="application/json",
                          body=json.dumps(body))
            return
        if req.method == "GET":
            calls["get"].append(req.url)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(task))
            return
        calls["put"].append(req.post_data)
        if put_body is not None:
            body = put_body
        else:
            value = (json.loads(req.post_data or "{}")).get("value")
            body = {"status": "up", "value": value, "note": "下一次回测生效"}
        route.fulfill(status=put_status, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/settings**", _route)
    page.route("**/api/tasks/*/run", _route)
    return calls


class TestSettingsPage:
    def test_settings_main_flow(self, servers, page):
        """E2E-471：设置页渲染 + 保存契约（PUT 载荷 + 成功提示）。"""
        calls = _install_mock(page)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        expect(page.locator(".sidebar .nav-item")).to_have_count(6)
        page.locator(".sidebar .nav-item", has_text="设置").click()
        expect(page.get_by_role("heading", name="设置")).to_be_visible()
        inp = page.locator("[data-min-span-input]")
        expect(inp).to_have_value("90")          # mock GET 回显
        inp.fill("120")
        page.locator("[data-min-span-save]").click()
        expect(page.locator("[data-settings-saved]")
              ).to_contain_text("下一次回测生效", timeout=10_000)
        assert calls["put"], calls
        assert json.loads(calls["put"][-1]) == {"value": 120}

    def test_settings_invalid_value_error(self, servers, page):
        """E2E-472：后端 400 detail 透出 + 前端非整数拦截不发包。"""
        calls = _install_mock(page, put_status=400,
                              put_body={"detail": "运作期限天数须在 90~3650 之间：50"})
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        inp = page.locator("[data-min-span-input]")
        expect(inp).to_have_value("90")
        inp.fill("50")
        page.locator("[data-min-span-save]").click()
        expect(page.locator("[data-settings-error]")
              ).to_contain_text("90~3650 之间：50", timeout=10_000)
        assert len(calls["put"]) == 1
        inp.fill("")
        page.locator("[data-min-span-save]").click()
        expect(page.locator("[data-settings-error]")
              ).to_contain_text("请输入", timeout=10_000)
        assert len(calls["put"]) == 1            # 前端拦截，未再发包

    def test_settings_deeplink_and_down(self, servers, page):
        """E2E-473：/settings 深链直达 + 坏 DSN 后端 down 占位。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.get_by_role("heading", name="设置")).to_be_visible()
        expect(page.locator(".sidebar .nav-item.active")).to_have_text("设置")
        expect(page.locator(".browse-empty")
              ).to_contain_text("设置数据不可用", timeout=30_000)


class TestRunBacktestButton:
    def test_run_backtest_main_flow(self, servers, page):
        """E2E-474：按钮 → 确认框（时间文案）→ 确认 → POST 契约 +
        已触发提示 + 按钮转运行中禁用。"""
        calls = _install_mock(page)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        btn = page.locator("[data-run-backtest]")
        expect(btn).to_be_enabled()
        btn.click()
        expect(page.locator("[data-run-confirm]")).to_be_visible()
        expect(page.locator("[data-run-confirm-time]")
              ).to_contain_text("约 7~12 小时")
        page.locator("[data-run-confirm-ok]").click()
        expect(page.locator("[data-run-backtest-triggered]")
              ).to_contain_text("已触发", timeout=10_000)
        assert calls["run"] and "/api/tasks/7/run" in calls["run"][-1], calls
        expect(page.locator("[data-run-backtest]")).to_be_disabled()

    def test_run_backtest_cancel_and_409(self, servers, page):
        """E2E-475：取消不发包；已在运行时 409 → 错误提示可见。"""
        calls = _install_mock(page, run_status=409,
                              run_body={"detail": "任务已在排队/运行中"})
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        page.locator("[data-run-backtest]").click()
        expect(page.locator("[data-run-confirm]")).to_be_visible()
        page.locator("[data-run-confirm-cancel]").click()
        expect(page.locator("[data-run-confirm]")).to_have_count(0)
        assert not calls["run"], calls                    # 取消不发包
        page.locator("[data-run-backtest]").click()
        page.locator("[data-run-confirm-ok]").click()
        expect(page.locator("[data-run-backtest-error]")
              ).to_contain_text("已有回测在排队/运行中", timeout=10_000)
        assert len(calls["run"]) == 1

    def test_run_backtest_running_state(self, servers, page):
        """E2E-476：运行中 → 按钮禁用 + 运行中状态可见。"""
        import copy
        task = copy.deepcopy(MOCK_SETTINGS)
        task["backtest_task"]["running"] = True
        task["backtest_task"]["last_started_at"] = "2026-11-24T15:00:00"
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-backtest]")).to_be_disabled()
        expect(page.locator("[data-run-backtest-running]")
              ).to_contain_text("回测运行中", timeout=10_000)


class TestBacktestProgress:
    def _running_mock(self, estimate="full", elapsed_hours=2.0):
        """构造 running 态 mock：started_at = server_now − elapsed_hours。"""
        import copy
        task = copy.deepcopy(MOCK_SETTINGS)
        now = new_date = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)
        started = now - __import__("datetime").timedelta(hours=elapsed_hours)
        task["backtest_task"].update({
            "running": True, "estimate": estimate,
            "last_started_at": started.isoformat(),
            "last_finished_at": None,
            "server_now": now.isoformat()})
        return task

    def test_progress_bar_rendering(self, servers, page):
        """E2E-477：运行中 → 进度条/已运行时长/预计耗时渲染（full 2h≈25%）。"""
        _install_mock(page, task=self._running_mock("full", 2.0))
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-progress]")).to_be_visible()
        expect(page.locator("[data-run-progress-text]")
              ).to_contain_text("已运行", timeout=10_000)
        expect(page.locator("[data-run-progress-text]")
              ).to_contain_text("预计约 8 小时")
        width = page.locator("[data-run-progress-fill]").evaluate(
            "el => parseFloat(el.style.width)")
        assert 15 <= width <= 35, f"2h/8h 进度应约 25%：{width}"

    def test_completed_state_and_dirty_confirm(self, servers, page):
        """E2E-478：完成态进度块消失 + 上次回测用时；dirty 确认框文案
        含「约 10 分钟」。"""
        import copy
        import datetime as dt
        task = copy.deepcopy(MOCK_SETTINGS)
        now = dt.datetime.now(dt.timezone.utc)
        task["backtest_task"].update({
            "running": False, "estimate": "full",
            "last_started_at": (now - dt.timedelta(hours=3)).isoformat(),
            "last_finished_at": (now - dt.timedelta(hours=1)).isoformat(),
            "server_now": now.isoformat()})
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-progress]")).to_have_count(0)
        expect(page.locator("[data-run-backtest-last]")
              ).to_contain_text("用时", timeout=10_000)
        expect(page.locator("[data-run-backtest-last]")
              ).to_contain_text("2 小时")
        # dirty 预估：确认框时间文案为增量语义（未运行时按钮才可点）
        task2 = copy.deepcopy(MOCK_SETTINGS)
        task2["backtest_task"].update({
            "running": False, "estimate": "dirty",
            "last_started_at": (now - dt.timedelta(hours=1)).isoformat(),
            "last_finished_at": (now - dt.timedelta(minutes=30)).isoformat(),
            "server_now": now.isoformat()})
        _install_mock(page, task=task2)
        page.reload()
        page.locator("[data-run-backtest]").click()
        expect(page.locator("[data-run-confirm-time]")
              ).to_contain_text("约 10 分钟", timeout=10_000)


class TestRunMethodButton:
    """单策略全量运行按钮（2026-11-25 九轮）：六策略选择器 + 前置校验 +
    任务系统触发。"""

    def test_run_method_main_flow(self, servers, page):
        """E2E-481：选择器默认「盈亏阶梯深档」→ 切「深回撤加码」→ 确认框
        （耗时文案 + 策略名）→ 确认 → 前置 POST 载荷 {"method":…} +
        POST /api/tasks/9/run + 已触发提示 + 按钮转禁用。"""
        calls = _install_mock(page)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        seg = page.locator("[data-method-target-seg]")
        expect(seg.locator("button")).to_have_count(7)   # 七种全可选
        expect(page.locator('[data-method-target="pl_ladder_deep"]')
              ).to_have_class(re.compile("active"))      # 缺省最新策略
        page.locator('[data-method-target="dd_ladder_deep"]').click()
        expect(page.locator('[data-method-target="dd_ladder_deep"]')
              ).to_have_class(re.compile("active"))
        page.locator("[data-run-method]").click()
        expect(page.locator("[data-method-confirm]")).to_be_visible()
        expect(page.locator("[data-method-confirm-time]")
              ).to_contain_text("10 分钟~2 小时")
        expect(page.locator("[data-method-confirm-time]")
              ).to_contain_text("深回撤加码")
        page.locator("[data-method-confirm-ok]").click()
        expect(page.locator("[data-run-method-triggered]")
              ).to_contain_text("深回撤加码", timeout=10_000)
        assert calls["method_pre"] and \
            json.loads(calls["method_pre"][-1]) == {"method": "dd_ladder_deep"}
        assert calls["run"] and "/api/tasks/9/run" in calls["run"][-1], calls
        expect(page.locator("[data-run-method]")).to_be_disabled()

    def test_run_method_cancel_and_409(self, servers, page):
        """E2E-482a：取消不发包；前置 409（日更运行中）→ 错误提示含
        「日更」且不触发任务运行。"""
        calls = _install_mock(page, method_pre_status=409,
                              method_pre_body={
                                  "detail": "回测日更运行中，请等待完成后再运行单策略全量"})
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        page.locator("[data-run-method]").click()
        expect(page.locator("[data-method-confirm]")).to_be_visible()
        page.locator("[data-method-confirm-cancel]").click()
        expect(page.locator("[data-method-confirm]")).to_have_count(0)
        assert not calls["method_pre"] and not calls["run"], calls
        page.locator("[data-run-method]").click()
        page.locator("[data-method-confirm-ok]").click()
        expect(page.locator("[data-run-method-error]")
              ).to_contain_text("日更", timeout=10_000)
        assert len(calls["method_pre"]) == 1
        assert not calls["run"]            # 前置拒绝后不触发任务运行

    def test_run_method_running_state(self, servers, page):
        """E2E-482b：method_task running=true → 按钮禁用 + 运行中状态可见。"""
        import copy
        task = copy.deepcopy(MOCK_SETTINGS)
        task["method_task"].update({"running": True, "phase": "running",
                                    "last_started_at": "2026-11-25T10:00:00"})
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-method]")).to_be_disabled()
        expect(page.locator("[data-run-method-running]")
              ).to_contain_text("运行中", timeout=10_000)


class TestRealProgress:
    def test_progress_real_fund_counts(self, servers, page):
        """E2E-483：基金级真实进度（2026-11-25 十轮）——mock progress
        12345/26548 → 进度条按真实比例（≈46.4%）渲染 + 文案「已完成
        12345 / 26548 只基金」。无 progress 字段回落时间推算（E2E-477
        已覆盖），本用例锁真实分母优先。"""
        import copy
        import datetime as dtm
        task = copy.deepcopy(MOCK_SETTINGS)
        now = dtm.datetime.now(dtm.timezone.utc)
        task["backtest_task"].update({
            "running": True, "estimate": "full",
            "progress_done": 12345, "progress_total": 26548,
            "last_started_at": (now - dtm.timedelta(hours=2)).isoformat(),
            "last_finished_at": None, "server_now": now.isoformat()})
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-progress]")).to_be_visible()
        expect(page.locator("[data-run-progress-text]")
              ).to_contain_text("已完成 12345 / 26548 只基金", timeout=10_000)
        expect(page.locator("[data-run-progress-text]")
              ).to_contain_text("46.5%")            # round(46.4646…, 1)
        width = page.locator("[data-run-progress-fill]").evaluate(
            "el => parseFloat(el.style.width)")
        assert 45.5 <= width <= 47.5, width       # 12345/26548 ≈ 46.46%


class TestMethodQueuedAndError:
    def test_queued_blocking_and_last_error(self, servers, page):
        """E2E-484：排队态显示等待对象（blocking）+ 空闲态显示上次失败原因
        （静默失败可见性，2026-11-25 十一轮）。"""
        import copy
        task = copy.deepcopy(MOCK_SETTINGS)
        task["method_task"].update({"phase": "queued", "running": False,
                                    "blocking": "东财每日增量"})
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-method-running]")
              ).to_contain_text("排队中（等待「东财每日增量」完成，完成后自动开始）",
                                timeout=10_000)
        # 排队阶段不显示进度条（十六轮：上轮 last_started_at 会把旧运行
        # 时长错算给排队任务——已运行条只在 running 态出现）
        expect(page.locator("[data-run-method-progress]")).to_have_count(0)
        # 失败可见性：idle + last_error → 红色错误行
        task2 = copy.deepcopy(MOCK_SETTINGS)
        task2["method_task"].update({"phase": "idle", "running": False,
                                     "blocking": None,
                                     "last_error": "KeyError: 'backtest_method'",
                                     "last_started_at": "2026-11-25T14:20:00",
                                     "last_finished_at": "2026-11-25T14:20:01"})
        _install_mock(page, task=task2)
        page.reload()
        expect(page.locator("[data-run-method-last-error]")
              ).to_contain_text("上次单策略回测失败：KeyError: 'backtest_method'",
                                timeout=10_000)


class TestTriggerStartsPolling:
    def test_method_trigger_starts_settings_polling(self, servers, page):
        """E2E-485：停留在页面点「运行」成功后，30 秒设置轮询立即启动
        （2026-11-25 十二轮——原缺口：定时器只在进入页面时按运行态开启，
        点完按钮后进度快照永不刷新，基金级进度永远不出现）。"""
        import copy
        calls = _install_mock(page)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.get_by_role("heading", name="设置")).to_be_visible()
        gets_before = len(calls["get"])
        page.locator("[data-run-method]").click()
        page.locator("[data-method-confirm-ok]").click()
        expect(page.locator("[data-run-method-triggered]")
              ).to_contain_text("已触发", timeout=10_000)
        # 30 秒自动刷新：≥1 次新 GET /api/settings（5s 余量吸收定时器相位）
        page.wait_for_timeout(36_000)
        assert len(calls["get"]) >= gets_before + 1, \
            (gets_before, len(calls["get"]))


class TestMethodProgressBar:
    def test_method_running_progress_bar(self, servers, page):
        """E2E-486：单策略运行中显示进度条（真实分母优先）——fill 宽度按
        已完成/总数比例、文案「已完成 N / M 只基金」（2026-11-25 十三轮：
        此前只有文字计数没有条）。"""
        import copy
        import datetime as dtm
        task = copy.deepcopy(MOCK_SETTINGS)
        now = dtm.datetime.now(dtm.timezone.utc)
        task["method_task"].update({
            "running": True, "phase": "running",
            "last_started_at": (now - dtm.timedelta(minutes=10)).isoformat(),
            "progress_done": 1000, "progress_total": 26831,
            "server_now": now.isoformat()})
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-method-progress]")).to_be_visible()
        expect(page.locator("[data-run-method-progress-text]")
              ).to_contain_text("已完成 1000 / 26831 只基金", timeout=10_000)
        expect(page.locator("[data-run-method-progress-text]")
              ).to_contain_text("3.7%")            # round(1000/26831,1)=3.7
        width = page.locator("[data-run-method-progress-fill]").evaluate(
            "el => parseFloat(el.style.width)")
        assert 3.0 <= width <= 4.5, width

    def test_method_running_fingerprint_fallback_bar(self, servers, page):
        """E2E-487：指纹比对阶段（无 progress 字段）→ 回落预估条（文案
        「指纹比对阶段尚无基金级进度」）。"""
        import copy
        import datetime as dtm
        task = copy.deepcopy(MOCK_SETTINGS)
        now = dtm.datetime.now(dtm.timezone.utc)
        task["method_task"].update({
            "running": True, "phase": "running",
            "last_started_at": (now - dtm.timedelta(minutes=2)).isoformat(),
            "server_now": now.isoformat()})
        _install_mock(page, task=task)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/settings")
        expect(page.locator("[data-run-method-progress]")).to_be_visible()
        expect(page.locator("[data-run-method-progress-text]")
              ).to_contain_text("指纹比对阶段尚无基金级进度", timeout=10_000)
