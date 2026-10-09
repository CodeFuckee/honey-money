"""爬虫任务管理页 E2E：mock /api/tasks* 注入任务数据（本地无 PG）。

用例表（确认门 1 已确认）：E2E-101~110，覆盖主流程/相位操作/历史/编辑/
删除/风险提示/降级/导航回归。调度逻辑与边界由后端单测覆盖，此处只测
用户可见页面行为。运行：python -m pytest tests/e2e/test_tasks_page.py
"""

import json
import re

import pytest
from playwright.sync_api import expect

SERVER_TIME = "2026-10-09T12:00:00+08:00"

# 预置任务（禁用 · 空闲 · 有历史）
PRESET_TASK = {
    "id": 1, "name": "新浪每日增量", "source": "sina", "mode": "incremental",
    "schedule_time": "18:00", "enabled": False, "phase": "idle",
    "next_run_at": "2026-10-10T18:00:00+08:00", "progress": None,
    "last_run": {"started_at": "2026-10-08T18:00:05+08:00",
                 "finished_at": "2026-10-08T18:41:00+08:00",
                 "mode": "incremental", "source": "sina", "ok_stocks": 5560,
                 "failed_stocks": 2, "bars_upserted": 11120, "aborted": False,
                 "trigger": "schedule"},
}
# 运行中任务（带实时进度）
RUNNING_TASK = {
    "id": 2, "name": "全量重建", "source": "sina", "mode": "full",
    "schedule_time": "03:00", "enabled": True, "phase": "running",
    "next_run_at": "2026-10-10T03:00:00+08:00",
    "progress": {"done": 1200, "total": 5560}, "last_run": None,
}
# 排队中任务
QUEUED_TASK = {
    "id": 3, "name": "东财补数", "source": "eastmoney", "mode": "incremental",
    "schedule_time": "07:30", "enabled": True, "phase": "queued",
    "next_run_at": "2026-10-10T07:30:00+08:00", "progress": None, "last_run": None,
}

RUNS = [{
    "started_at": "2026-10-08T18:00:05+08:00", "finished_at": "2026-10-08T18:41:00+08:00",
    "mode": "incremental", "source": "sina", "ok_stocks": 5560, "failed_stocks": 2,
    "bars_upserted": 11120, "aborted": False, "trigger": "schedule",
}]


@pytest.fixture
def api_calls(page):
    """拦截 /api/tasks*：GET 返回三条任务（含运行中/排队中），写操作记录
    请求并返回成功；返回调用记录列表 [(method, url, body), ...]。"""
    calls = []

    def handler(route):
        req = route.request
        method, url = req.method, req.url
        if re.search(r"/api/tasks/\d+/runs$", url):
            calls.append((method, url, None))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"task_id": 1, "runs": RUNS}))
        elif method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "status": "up", "server_time": SERVER_TIME,
                "tasks": [PRESET_TASK, RUNNING_TASK, QUEUED_TASK],
                "queue": [3], "running": 2,
            }))
        else:
            calls.append((method, url, req.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))

    page.route("**/api/tasks**", handler)
    return calls


def open_tasks(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="任务").click()
    expect(page.get_by_role("heading", name="爬虫任务")).to_be_visible()


def card(page, name):
    return page.locator(".task-card", has_text=name)


class TestNavAndDegraded:
    def test_nav_has_tasks_entry(self, servers, page):
        """E2E-110 回归：侧栏五项且「任务」可进入任务页。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        nav = page.locator(".sidebar .nav-item")
        expect(nav).to_have_count(5)
        expect(nav.filter(has_text="任务")).to_contain_text("任务")
        page.get_by_role("button", name="任务").click()
        expect(page.get_by_role("heading", name="爬虫任务")).to_be_visible()

    def test_degraded_without_pg(self, servers, page):
        """E2E-101：本地无 PG（未 mock）→ 错误横幅而非崩溃，可切回仪表盘。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        expect(page.locator(".error-banner")).to_be_visible()
        page.get_by_role("button", name="仪表盘").click()
        expect(page.locator(".card")).to_have_count(4, timeout=20000)


class TestTaskList:
    def test_list_renders_tasks_and_progress(self, servers, page, api_calls):
        """E2E-102：三张任务卡渲染名称/源/模式/时刻/相位/进度。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)

        # 预置任务：禁用 · 空闲 · 下次执行、上次结果
        p = card(page, "新浪每日增量")
        expect(p).to_contain_text("新浪财经源")
        expect(p).to_contain_text("增量续爬")
        expect(p).to_contain_text("每日 18:00")
        expect(p).to_contain_text("空闲")
        expect(p).to_contain_text("10/10 18:00")   # 当天已跑 → 显示明天（mock 时刻）
        expect(p).to_contain_text("成 5,560 / 败 2")

        # 运行中：进度条 + 已完成数 + 停止按钮（E2E-105 可见性部分）
        r = card(page, "全量重建")
        expect(r).to_contain_text("运行中")
        expect(r).to_contain_text("已完成 1,200 / 5,560 只")
        expect(r.get_by_role("button", name="停止")).to_be_visible()

        # 排队中：取消排队按钮；空闲：立即执行按钮
        expect(card(page, "东财补数").get_by_role("button", name="取消排队")).to_be_visible()
        expect(p.get_by_role("button", name="立即执行")).to_be_visible()

    def test_phase_badge_not_overlapping_switch(self, servers, page, api_calls):
        """回归：相位徽章曾复用仪表盘状态卡的 .fbadge（absolute 右上角定位），
        脱离文档流压到卡片右上角的启用开关上（用户过目发现）。现为任务页
        专属 .phase-badge（inline-flex 文档流内）。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)

        p = card(page, "新浪每日增量")
        badge = p.locator(".task-tags .phase-badge")
        expect(badge).to_be_visible()
        expect(badge).to_contain_text("空闲")

        # 根因断言：徽章必须在文档流内（不得 absolute 到卡片角落）
        pos = badge.evaluate("el => getComputedStyle(el).position")
        assert pos == "static", f"任务页相位徽章应为文档流内（static），实际 {pos}"

        # 现象断言：徽章与启用开关边界框不相交
        b1 = badge.bounding_box()
        b2 = p.locator("label.switch").bounding_box()
        assert b1 and b2, "徽章或开关未渲染"
        overlap_x = min(b1["x"] + b1["width"], b2["x"] + b2["width"]) - max(b1["x"], b2["x"])
        overlap_y = min(b1["y"] + b1["height"], b2["y"] + b2["height"]) - max(b1["y"], b2["y"])
        assert not (overlap_x > 0 and overlap_y > 0), \
            f"相位徽章与启用开关重叠: badge={b1} switch={b2}"


class TestCreateAndEdit:
    def test_create_task_posts_payload(self, servers, page, api_calls):
        """E2E-103：新建任务 → POST /api/tasks 携带表单值。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        page.get_by_role("button", name="＋ 新建任务").click()
        expect(page.locator(".dialog")).to_be_visible()
        page.locator(".dialog-field", has_text="名称").locator("input").fill("盘前补数")
        page.locator(".dialog-field", has_text="每日执行时刻").locator("input").fill("07:30")
        page.locator(".dialog-actions").get_by_role("button", name="保存").click()
        expect(page.locator(".dialog")).to_have_count(0)

        posts = [(m, u, b) for m, u, b in api_calls if m == "POST"]
        assert posts, "未发出 POST /api/tasks"
        body = json.loads(posts[-1][2])
        assert body["source"] == "sina"
        assert body["mode"] == "incremental"
        assert body["schedule_time"] == "07:30"
        assert body["name"] == "盘前补数"

    def test_edit_task_refills_and_patches(self, servers, page, api_calls):
        """E2E-107：编辑对话框回填现值，修改时刻 → PATCH 携带新值。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        card(page, "新浪每日增量").get_by_role("button", name="编辑").click()
        expect(page.locator(".dialog")).to_be_visible()
        name_val = page.locator(".dialog-field", has_text="名称").locator("input").input_value()
        assert name_val == "新浪每日增量", "编辑应回填任务名"
        time_val = page.locator(".dialog-field", has_text="每日执行时刻").locator("input").input_value()
        assert time_val == "18:00", "编辑应回填执行时刻"
        page.locator(".dialog-field", has_text="每日执行时刻").locator("input").fill("07:45")
        page.locator(".dialog-actions").get_by_role("button", name="保存").click()

        patches = [(m, u, b) for m, u, b in api_calls if m == "PATCH"]
        assert patches, "未发出 PATCH"
        body = json.loads(patches[-1][2])
        assert body["schedule_time"] == "07:45"

    def test_risk_notes_in_dialog(self, servers, page, api_calls):
        """E2E-109：东财源 / 全量重爬 各自的风险提示文案可见。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        page.get_by_role("button", name="＋ 新建任务").click()
        expect(page.locator(".dialog")).to_be_visible()

        src = page.locator(".dialog-field", has_text="数据源").locator("select")
        mode = page.locator(".dialog-field", has_text="模式").locator("select")
        src.select_option("eastmoney")
        expect(page.locator(".dialog .task-risk")).to_contain_text("东财源 drip 模式")
        src.select_option("sina")
        expect(page.locator(".dialog .task-risk")).not_to_contain_text("东财")
        mode.select_option("full")
        expect(page.locator(".dialog .task-risk")).to_contain_text("全量重爬")
        page.locator(".dialog-actions").get_by_role("button", name="取消").click()


class TestActions:
    def test_enable_switch_patches(self, servers, page, api_calls):
        """E2E-104：点禁用任务的开关 → PATCH {enabled: true}。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        card(page, "新浪每日增量").locator("label.switch").click()
        patches = [(m, u, b) for m, u, b in api_calls if m == "PATCH"]
        assert patches and json.loads(patches[-1][2]) == {"enabled": True}

    def test_phase_actions_post(self, servers, page, api_calls):
        """E2E-105：立即执行/停止/取消排队各发对应 POST。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        card(page, "新浪每日增量").get_by_role("button", name="立即执行").click()
        card(page, "全量重建").get_by_role("button", name="停止").click()
        card(page, "东财补数").get_by_role("button", name="取消排队").click()
        urls = [(m, u) for m, u, _ in api_calls]
        assert any(m == "POST" and u.endswith("/api/tasks/1/run") for m, u in urls)
        assert any(m == "POST" and u.endswith("/api/tasks/2/stop") for m, u in urls)
        assert any(m == "POST" and u.endswith("/api/tasks/3/cancel") for m, u in urls)

    def test_history_expands_with_runs(self, servers, page, api_calls):
        """E2E-106：点「历史」→ GET runs → 表格渲染触发/结果/K线数。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        card(page, "新浪每日增量").get_by_role("button", name="历史").click()
        expect(card(page, "新浪每日增量").locator(".runs-table")).to_be_visible()
        row = card(page, "新浪每日增量").locator(".runs-table tbody tr").first
        expect(row).to_contain_text("定时")
        expect(row).to_contain_text("成 5,560 / 败 2")
        expect(row).to_contain_text("11,120")
        assert any(m == "GET" and u.endswith("/api/tasks/1/runs")
                   for m, u, _ in api_calls)

    def test_delete_with_confirm(self, servers, page, api_calls):
        """E2E-108：删除需 confirm 确认 → DELETE 请求发出。"""
        _, frontend_port = servers
        open_tasks(page, frontend_port)
        page.once("dialog", lambda d: d.accept())
        card(page, "新浪每日增量").get_by_role("button", name="删除").click()
        assert any(m == "DELETE" and u.endswith("/api/tasks/1")
                   for m, u, _ in api_calls)
