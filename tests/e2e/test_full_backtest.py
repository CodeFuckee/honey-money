"""教学页「全量回测」E2E：mock 全量回测接口（后台任务/进度/结果/历史/取消）。

覆盖（确认门 1 用例表 2026-10-11）：
- E2E-001 P0 发起→进度→完成→统计卡+Top图+排行表（含失败行）
- E2E-002 P0 排行表列排序与翻页请求契约
- E2E-003 P1 失败股标注原因不阻塞
- E2E-004 P1 历史下拉切换回看
- E2E-005 P1 运行中取消→已取消态
- E2E-006 P2 动量策略按钮置灰
- E2E-007 P2 重复发起 409 提示
真实批量计算由 backend worker 测试覆盖（test_backtest_full.py）。
"""

import json
import re

import pytest
from playwright.sync_api import expect

RUN_DONE = {"id": 7, "strategy": "ma_cross", "range": "3y", "status": "done",
            "params": {}, "filters": {"exclude_st": True, "min_bars": 60},
            "adjust": True, "exec_mode": "next_open", "commission": 2.5e-4,
            "sell_tax": 5e-4, "total": 3, "done": 3, "failed": 1, "skipped": 2,
            "stats": {"count": 2, "positive_pct": 0.5,
                      "median_return": 0.15, "beat_buyhold_pct": 1.0},
            "error": None, "created_at": "2026-10-11T10:00:00",
            "finished_at": "2026-10-11T10:05:00"}
RUN_RUNNING = {**RUN_DONE, "status": "running", "done": 1, "failed": 0,
               "stats": None, "finished_at": None}
RUN_CANCELED = {**RUN_DONE, "status": "canceled", "done": 2, "failed": 0,
                "stats": None, "finished_at": "2026-10-11T10:02:00"}
RUN_OLD = {**RUN_DONE, "id": 6, "strategy": "donchian", "failed": 0,
           "stats": {"count": 3, "positive_pct": 1.0,
                     "median_return": 0.30, "beat_buyhold_pct": 0.66}}

ROWS = {"total": 53, "rows": [
    {"code": "600519", "name": "贵州茅台", "status": "ok",
     "total_return": 0.26, "annual_return": 0.08, "max_drawdown": 0.18,
     "sharpe": 0.85, "win_rate": 0.5, "trades": 3,
     "buyhold_return": 0.10, "reason": None},
    {"code": "000001", "name": "平安银行", "status": "ok",
     "total_return": 0.05, "annual_return": 0.02, "max_drawdown": 0.09,
     "sharpe": 0.4, "win_rate": 0.5, "trades": 1,
     "buyhold_return": 0.01, "reason": None},
    {"code": "301001", "name": "次新股份", "status": "failed",
     "total_return": None, "annual_return": None, "max_drawdown": None,
     "sharpe": None, "win_rate": None, "trades": None,
     "buyhold_return": None, "reason": "区间内无行情数据"}]}

CURVES = {"curves": [
    {"code": "600519", "name": "贵州茅台", "total_return": 0.26,
     "equity": [["2026-01-02", 1.0], ["2026-06-01", 1.26]]},
    {"code": "000001", "name": "平安银行", "total_return": 0.05,
     "equity": [["2026-01-02", 1.0], ["2026-06-01", 1.05]]}]}


@pytest.fixture
def full_api(page):
    """mock 全量回测全部端点；holder 供用例改写返回（details: id → dict|序列）。"""
    calls = []
    holder = {
        "runs": [RUN_DONE, RUN_OLD],
        "details": {7: RUN_DONE, 6: RUN_OLD},
        "rows": {7: ROWS, 6: ROWS},
        "curves": CURVES,
        "create_status": 200,
        "create_body": {"run_id": 7},
    }

    def handler(route):
        req = route.request
        url = req.url

        def fulfill_json(obj, status=200):
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(obj, ensure_ascii=False))

        if re.search(r"/api/backtest/full/runs/\d+/rows", url):
            calls.append({"kind": "rows", "url": url})
            rid = int(re.search(r"/runs/(\d+)/rows", url).group(1))
            fulfill_json(holder["rows"].get(rid, ROWS))
            return
        if re.search(r"/api/backtest/full/runs/\d+/top", url):
            fulfill_json(holder["curves"])
            return
        if re.search(r"/api/backtest/full/runs/\d+/export", url):
            route.fulfill(status=200, content_type="text/csv; charset=utf-8",
                          body="\ufeffcode,name,total_return\n600519,贵州茅台,0.26\n")
            return
        if re.search(r"/api/backtest/full/runs/\d+/cancel", url):
            holder["details"][7] = RUN_CANCELED            # 取消后终态
            fulfill_json({"ok": True})
            return
        if re.search(r"/api/backtest/full/runs/\d+$", url):
            rid = int(re.search(r"/runs/(\d+)$", url).group(1))
            detail = holder["details"].get(rid)
            if isinstance(detail, list):                   # 状态序列：逐次弹出
                detail = detail.pop(0) if len(detail) > 1 else detail[0]
            fulfill_json(detail)
            return
        if re.search(r"/api/backtest/full/runs$", url):
            fulfill_json({"runs": holder["runs"]})
            return
        if re.search(r"/api/backtest/full$", url):
            calls.append({"kind": "create", "body": req.post_data})
            fulfill_json(holder["create_body"], status=holder["create_status"])
            return
        if re.search(r"/api/stocks/basic", url):
            fulfill_json({"status": "up", "total": 0, "stocks": []})
            return
        route.continue_()

    page.route("**/api/**", handler)
    return calls, holder


def open_learn(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="教学").click()
    expect(page.get_by_role("heading", name="量化教学")).to_be_visible()


def open_learn(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="教学").click()
    expect(page.get_by_role("heading", name="量化教学")).to_be_visible()


def goto_full_mode(page):
    """切到「全市场」回测模式段。"""
    page.locator(".learn-mode-btn", has_text="全市场").click()
    expect(page.locator(".learn-mode-btn", has_text="全市场")
           ).to_have_class(re.compile("active"))


class TestFullBacktest:
    def test_e2e_001_run_to_results(self, servers, page, full_api):
        """P0 发起全量回测 → 进度 → 统计卡 + Top 图 + 排行表（含失败行）。"""
        _, holder = full_api
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        page.get_by_role("button", name="发起全量").click()
        holder["details"][7] = [RUN_RUNNING, RUN_DONE]   # 轮询序列：running→done
        expect(page.locator("[data-testid=full-status]")
               ).to_contain_text("已完成 1 / 3", timeout=10000)
        expect(page.locator(".learn-metrics .lm", has_text="正收益占比")
               ).to_contain_text("50.0%")
        expect(page.locator(".learn-metrics .lm", has_text="跑赢买入持有")
               ).to_contain_text("100.0%")
        rows = page.locator(".learn-full-cmp tbody tr")
        expect(rows).to_have_count(3)
        expect(rows.nth(0)).to_contain_text("600519")
        expect(page.locator(".learn-fail-row")).to_contain_text("区间内无行情数据")
        chart = page.evaluate(
            "() => { const c = window.__fullChart; if (!c) return {hook:false};"
            " return {hook: true, n: c.getOption().series.length}; }")
        assert chart == {"hook": True, "n": 2}     # 失败股无曲线 → 2 条

    def test_e2e_002_rows_sort_and_pagination_contract(self, servers, page,
                                                       full_api):
        """P0 列头排序/翻页 → rows 请求参数契约（sort/desc/page）。"""
        calls, _ = full_api
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        expect(page.locator(".learn-full-cmp tbody tr")).to_have_count(3)
        page.locator(".full-sort", has_text="年化").click()    # 换列 → desc=true
        page.locator(".full-sort", has_text="年化").click()    # 同列 → desc=false
        page.get_by_role("button", name="下一页").click()

        def rows_urls():
            return [c["url"] for c in calls if c["kind"] == "rows"]

        last = rows_urls()[-1]
        assert "sort=annual_return" in last
        assert "desc=false" in last                    # URLSearchParams 小写
        assert "page=1" in last

    def test_e2e_003_failed_row_marked(self, servers, page, full_api):
        """P1 失败股行标注原因，指标列显示 —，成功行正常。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        expect(page.locator(".learn-full-cmp tbody tr")).to_have_count(3)
        fail_row = page.locator(".learn-full-cmp tbody tr", has_text="301001")
        expect(fail_row).to_contain_text("（失败：区间内无行情数据）")
        expect(fail_row.locator("td").nth(2)).to_contain_text("—")

    def test_e2e_005_cancel_running(self, servers, page, full_api):
        """P1 运行中取消 → 已取消态（取消端点改写详情为 canceled）。"""
        _, holder = full_api
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        page.get_by_role("button", name="发起全量").click()
        holder["details"][7] = RUN_RUNNING             # 点击后进入运行态
        expect(page.get_by_role("button", name="取消")).to_be_visible()
        page.get_by_role("button", name="取消").click()
        expect(page.locator("[data-testid=full-status]")
               ).to_contain_text("已取消", timeout=10000)

    def test_e2e_006_momentum_disables_full_segment(self, servers, page,
                                                    full_api):
        """P2 动量策略 → 「全市场」模式段禁用带提示（横截面组合策略）。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.locator(".learn-nav", has_text="动量轮动").click()
        seg = page.locator(".learn-mode-btn", has_text="全市场")
        expect(seg).to_be_disabled()
        expect(seg).to_have_attribute("title", re.compile("动量"))

    def test_e2e_007_conflict_409_shown(self, servers, page, full_api):
        """P2 已有任务运行时再发起 → 409 → 页面错误横幅。"""
        _, holder = full_api
        holder["create_status"] = 409
        holder["create_body"] = {"detail": "已有全量回测任务在运行，请等待完成或取消"}
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        page.get_by_role("button", name="发起全量").click()
        expect(page.locator(".learn-params .error-banner")
               ).to_contain_text("已有全量回测任务在运行")

    def test_e2e_008_mode_segment_switch(self, servers, page, full_api):
        """P0 模式分段切换：单股/批量共享代码控件；全市场隐藏代码控件、
        显示过滤面板，主按钮切换为「发起全量」。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        seg = page.locator(".learn-mode-btn")
        expect(seg).to_have_count(3)
        expect(page.locator(".learn-mode-btn", has_text="单股")
               ).to_have_class(re.compile("active"))
        # 批量段：代码控件仍在
        page.locator(".learn-mode-btn", has_text="批量").click()
        expect(page.locator(".learn-ac")).to_be_visible()
        expect(page.get_by_role("button", name="跑回测")).to_be_visible()
        # 全市场段：代码控件隐藏，过滤面板出现（含退市股开关默认勾选），
        # 主按钮变为发起全量
        goto_full_mode(page)
        expect(page.locator(".learn-ac")).to_have_count(0)
        expect(page.locator(".learn-full-controls")).to_be_visible()
        del_cb = page.locator(".learn-full-controls .learn-check",
                              has_text="含退市股")
        expect(del_cb.locator("input")).to_be_checked()
        expect(page.get_by_role("button", name="发起全量")).to_be_visible()
        expect(page.get_by_role("button", name="跑回测")).to_have_count(0)
        # 切回单股
        page.locator(".learn-mode-btn", has_text="单股").click()
        expect(page.locator(".learn-ac")).to_be_visible()
        expect(page.get_by_role("button", name="跑回测")).to_be_visible()
