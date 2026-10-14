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
                      "median_return": 0.15, "beat_buyhold_pct": 1.0,
                      "best_return": 0.26, "worst_return": -0.10},
            "error": None, "created_at": "2026-10-11T10:00:00",
            "finished_at": "2026-10-11T10:05:00",
            "reused_from": 6, "reused_count": 2}
RUN_RUNNING = {**RUN_DONE, "status": "running", "done": 1, "failed": 0,
               "stats": None, "finished_at": None,
               "reused_from": None, "reused_count": 0}
RUN_CANCELED = {**RUN_DONE, "status": "canceled", "done": 2, "failed": 0,
                "stats": None, "finished_at": "2026-10-11T10:02:00",
                "reused_from": None, "reused_count": 0}
RUN_RUNNING_LIVE = {**RUN_DONE, "status": "running", "done": 2, "failed": 0,
                    "stats": {"count": 2, "positive_pct": 1.0,
                              "median_return": 0.15, "beat_buyhold_pct": 1.0,
                              "best_return": 0.26, "worst_return": 0.05},
                    "finished_at": None,
                    "reused_from": None, "reused_count": 0}
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

ROWS_1 = {"total": 1, "rows": [ROWS["rows"][0]]}
ALL_CURVES = {"curves": [
    {"code": "600519", "name": "贵州茅台", "total_return": 0.26,
     "equity": [["2026-01-02", 1.0], ["2026-03-01", 1.26]]},
    {"code": "000001", "name": "平安银行", "total_return": 0.05,
     "equity": [["2026-01-02", 1.0], ["2026-03-01", 1.05]]},
    {"code": "000003", "name": "PT金田Ａ", "total_return": -0.62,
     "equity": [["2026-01-02", 1.0], ["2026-03-01", 0.005]]}]}

DELIST_CURVES = {"curves": [
    {"code": "000004", "name": "国华退", "total_return": -0.9681,
     "equity": [["2026-01-02", 1.0], ["2026-06-01", 0.4]]}]}
DELIST_ROWS = {"total": 2, "rows": [
    {"code": "000003", "name": "PT金田Ａ", "status": "ok",
     "total_return": -0.6185, "annual_return": -0.09, "max_drawdown": 0.7,
     "sharpe": -0.5, "win_rate": 0.3, "trades": 1,
     "buyhold_return": -0.6185, "reason": None},
    {"code": "000004", "name": "国华退", "status": "ok",
     "total_return": -0.9681, "annual_return": -0.15, "max_drawdown": 0.95,
     "sharpe": -0.8, "win_rate": 0.1, "trades": 1,
     "buyhold_return": -0.9681, "reason": None}],
    "stats": {"count": 2, "positive_pct": 0.0,
              "median_return": -0.79, "beat_buyhold_pct": 0.5,
              "best_return": -0.6185, "worst_return": -0.9681}}

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
            seq = (holder.get("rows_seq") or {}).get(rid)
            if seq:
                body = seq.pop(0) if len(seq) > 1 else seq[0]
            else:
                body = holder["rows"].get(rid, ROWS)
            if "delisted=only" in url:
                body = DELIST_ROWS
            fulfill_json(body)
            return
        if re.search(r"/api/backtest/full/runs/\d+/curves", url):
            fulfill_json(DELIST_CURVES if "delisted=only" in url
                         else ALL_CURVES)
            return
        if re.search(r"/api/backtest/full/runs/\d+/top", url):
            if "delisted=only" in url:
                fulfill_json(DELIST_CURVES)
            else:
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
        expect(page.locator(".full-total-metrics .lm", has_text="正收益占比")
               ).to_contain_text("50.0%")
        expect(page.locator(".full-total-metrics .lm", has_text="跑赢买入持有")
               ).to_contain_text("100.0%")
        rows = page.locator(".learn-full-cmp tbody tr")
        expect(rows).to_have_count(3)
        expect(rows.nth(0)).to_contain_text("600519")
        expect(page.locator(".learn-fail-row")).to_contain_text("区间内无行情数据")
        chart = page.evaluate(
            "() => { const c = window.__fullChart; if (!c) return {hook:false};"
            " return {hook: true, n: c.getOption().series.length}; }")
        assert chart == {"hook": True, "n": 3}     # 全部股票视图（含退市股曲线）

    def test_e2e_002_rows_sort_and_pagination_contract(self, servers, page,
                                                       full_api):
        """P0 列头排序/翻页 → rows 请求参数契约（sort/desc/page）。"""
        calls, _ = full_api
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        expect(page.locator(".learn-full-cmp tbody tr")).to_have_count(3)
        page.locator(".learn-full-cmp .full-sort", has_text="年化").click()  # 换列 → desc=true
        page.locator(".learn-full-cmp .full-sort", has_text="年化").click()  # 同列 → desc=false
        page.locator(".full-pager").get_by_role("button", name="下一页").click()

        def rows_urls():
            # 仅主表请求（退市股总结表的轮询请求不参与契约断言）
            return [c["url"] for c in calls
                    if c["kind"] == "rows" and "delisted=" not in c["url"]]

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

    def test_e2e_009_live_incremental_display(self, servers, page, full_api):
        """P0 运行中逐股实时上屏：排行表随轮询增长、统计卡实时部分统计、
        结果区标注「实时更新」；完成后切终值（节流 Top20 由前端节流逻辑保证）。"""
        _, holder = full_api
        _, frontend_port = servers
        # 运行列表首项即运行中任务：进页自动接续轮询（运行中实时上屏的真实入口）
        holder["runs"] = [RUN_RUNNING_LIVE, RUN_OLD]
        holder["details"][7] = [RUN_RUNNING_LIVE, RUN_RUNNING_LIVE,
                                RUN_RUNNING_LIVE, RUN_DONE]
        holder["rows_seq"] = {7: [ROWS_1, ROWS_1, ROWS_1, ROWS]}
        open_learn(page, frontend_port)
        goto_full_mode(page)
        # running 阶段：1 行即上屏 + 统计卡部分值 + 实时更新标注
        expect(page.locator(".learn-full-cmp tbody tr")
               ).to_have_count(1, timeout=10000)
        expect(page.locator(".full-total-metrics .lm", has_text="正收益占比")
               ).to_contain_text("100.0%")
        expect(page.locator(".result-frame > .result-head:not(.delist-head) .card-hint")
               ).to_contain_text("实时更新")
        # 轮询后行数增长（1 → 3），完成态统计切终值、hint 回常规
        expect(page.locator(".learn-full-cmp tbody tr")
               ).to_have_count(3, timeout=15000)
        expect(page.locator(".full-total-metrics .lm", has_text="正收益占比")
               ).to_contain_text("50.0%", timeout=15000)
        expect(page.locator(".result-frame > .result-head:not(.delist-head) .card-hint")
               ).to_contain_text("Top 20")
        # 退市股票总结区块：独立统计卡 + 退市排行表（样式与全市场结果一致）
        expect(page.locator(".result-head .card-title", has_text="退市股票总结")
               ).to_be_visible()
        expect(page.locator(".delist-metrics .lm", has_text="退市股参与")
               ).to_contain_text("2")
        expect(page.locator(".delist-metrics .lm", has_text="正收益占比")
               ).to_contain_text("0.0%")
        expect(page.locator(".delist-metrics .lm", has_text="最好收益")
               ).to_contain_text("-61.9%")
        expect(page.locator(".delist-metrics .lm", has_text="最差收益")
               ).to_contain_text("-96.8%")
        expect(page.locator(".learn-delist-cmp tbody tr", has_text="PT金田Ａ")
               ).to_contain_text("-61.9%")
        # 退市股收益折线图（独立 ECharts 实例 + 钩子）
        delist_chart = page.evaluate(
            "() => { const c = window.__delistChart; if (!c) return {hook:false};"
            " return {hook: true, n: c.getOption().series.length}; }")
        assert delist_chart == {"hook": True, "n": 1}

    def test_e2e_010_representative_labels_return_basis(self, servers, page,
                                                        full_api):
        """P1 四代表股标签按「净值末点 - 1」的收益率口径——回归：曾把净值
        直接当收益率（净值 0.005 被标成 "+0%"，与统计卡最差 -99.5% 矛盾）；
        选中态按钮与三视图切换正常。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        page.locator(".ct-btn", has_text="四代表股").click()
        expect(page.locator(".ct-btn", has_text="四代表股")
               ).to_have_class(re.compile("on"))
        page.wait_for_function(
            "() => { const c = window.__fullChart;"
            " return c && c.getOption().series.some("
            "s => String(s.name).startsWith('最低(')); }", timeout=10000)
        names = page.evaluate(
            "() => window.__fullChart.getOption().series.map(s => s.name)")
        assert "最高(+26.0%) 600519 贵州茅台" in names
        # 最低必须是收益率 -99.5%，而非净值 0.005（旧实现标成 "+0%"）
        assert "最低(-99.5%) 000003 PT金田Ａ" in names
        assert "近中位(+5.0%) 000001 平安银行" in names

    def test_e2e_011_reuse_flag_and_hint(self, servers, page, full_api):
        """P1 同参结果复用：开关默认开且随请求体提交（可关）；详情状态行
        显示复用来源（run #N 的 M 只）。"""
        calls, _ = full_api
        _, frontend_port = servers
        open_learn(page, frontend_port)
        goto_full_mode(page)
        # 状态行（viewable 完成态）：复用来源提示
        expect(page.locator("[data-testid=full-status]")
               ).to_contain_text("已复用 run #6 的 2 只结果")
        # 开关默认勾选；取消勾选发起 → 请求体 reuse=false
        controls = page.locator(".learn-full-controls")
        reuse_cb = controls.locator(".learn-check",
                                    has_text="复用历史结果").locator("input")
        expect(reuse_cb).to_be_checked()
        reuse_cb.uncheck()
        page.get_by_role("button", name="发起全量").click()
        expect(page.locator("[data-testid=full-status]")
               ).to_contain_text("已完成", timeout=10000)

        def create_bodies():
            return [json.loads(c["body"]) for c in calls if c["kind"] == "create"]

        assert create_bodies()[-1]["reuse"] is False
        # 重新勾选再发起 → reuse=true
        reuse_cb.check()
        page.get_by_role("button", name="发起全量").click()
        expect(page.locator("[data-testid=full-status]")
               ).to_contain_text("已完成", timeout=10000)
        assert create_bodies()[-1]["reuse"] is True
