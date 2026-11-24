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
  状态可见。
运行：python -m pytest tests/e2e/test_settings_page.py
"""

import json

from playwright.sync_api import expect

MOCK_SETTINGS = {"status": "up",
                 "settings": {"backtest_min_span_days": 90},
                 "backtest_task": {"task_id": 7, "running": False,
                                   "estimate": "full",
                                   "last_started_at": None,
                                   "last_finished_at": None}}


def _install_mock(page, put_status=200, put_body=None,
                  run_status=200, run_body=None, task=MOCK_SETTINGS):
    """拦截 /api/settings*（GET 回显/PUT 记录）与 /api/tasks/*/run
    （POST 记录并可指定 409 等应答）。"""
    calls = {"get": [], "put": [], "run": []}

    def _route(route):
        req = route.request
        if "/tasks/" in req.url:
            calls["run"].append(req.url)
            body = run_body if run_body is not None else \
                {"triggered": 7, "queue_position": 0}
            route.fulfill(status=run_status, content_type="application/json",
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
              ).to_contain_text("约 6~10 小时")
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
