"""定投时机合并表格 E2E：时机列并入排行表（mock /api/funds/dca 行内 dd_current）。

2026-11-17 表格合并后的行为（原独立时机区块已删，回撤桶按钮组随之移除——
与区间筛选的「当前回撤」维度重复，用户确认保留区间筛选）：
- E2E-435 P0 统一表格渲染时机列：当前回撤 -27.1%（深回撤高亮）、
  距前高 340 天；点「当前回撤」列头 → 请求 sort=dd_current（服务端排序
  落 t 表）；回撤桶按钮组不再存在；
- E2E-436 区间筛选作用于统一表格：面板常显，输入年化 min → 应用 →
  /api/funds/dca 请求带 xirr_min；清除筛选后参数消失；
- E2E-437 列显示过滤：取消勾选「类型」→ 表头消失；重新勾选恢复。
运行：python -m pytest tests/e2e/test_fund_dca_timing_page.py
"""

import re

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import mock_dca, open_dca
from test_fund_dca_tp_page import mock_tp


class TestDcaMergedTable:
    def test_timing_columns_render_and_sort(self, servers, page, mock_dca,
                                            mock_tp):
        """E2E-435 P0：统一表格渲染时机列（数据来自排行端点行的
        dd_current/days_since_peak）；时机列可服务端排序。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        row = page.locator(".browse-pv tbody tr", has_text="019387")
        expect(row).to_contain_text("-27.1%")      # 当前回撤
        expect(row).to_contain_text("340 天")       # 距前高
        expect(row).to_contain_text("2.9 年")       # 运作年限
        expect(row).to_contain_text("6")            # 止盈次数（合并前列沿用）
        # 回撤桶按钮组已删（与区间筛选重复，2026-11-17 用户确认）
        expect(page.locator("button[data-timing-band]")).to_have_count(0)
        # 时机列排序：请求带 sort=dd_current（后端白名单映射 t.dd_current）
        page.locator(".browse-pv th", has_text="当前回撤").click()
        expect(page.locator(".browse-pv th", has_text="当前回撤")
              ).to_have_class(re.compile("p-datatable-column-sorted"),
                              timeout=10_000)
        assert any("sort=dd_current" in u for u in mock_tp), mock_tp

    def test_range_filters_apply_to_unified_table(self, servers, page,
                                                  mock_dca, mock_tp):
        """E2E-436：区间筛选面板常显（无需展开），作用于统一排行表——
        输入年化 min → 应用筛选 → 列表请求带 xirr_min；清除后参数消失。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".browse-pv tbody tr",
                            has_text="019387")).to_be_visible()
        xirr_min = page.locator(".timing-filter input[placeholder='最低']").nth(0)
        expect(xirr_min).to_be_visible()
        xirr_min.fill("0.3")
        page.locator(".timing-filter button", has_text="应用筛选").click()
        expect(page.locator(".timing-filter button", has_text="清除筛选")
              ).to_be_visible(timeout=10_000)
        assert any("xirr_min=0.3" in u for u in mock_tp), mock_tp
        page.locator(".timing-filter button", has_text="清除筛选").click()
        assert "xirr_min" not in mock_tp[-1], mock_tp

    def test_column_visibility_toggle(self, servers, page, mock_dca, mock_tp):
        """E2E-437：列显示过滤（MultiSelect）——取消勾选「类型」→ 表头
        消失；重新勾选恢复。默认全选。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".browse-pv th", has_text="类型")).to_be_visible()
        sel = page.locator(".funds-search .dca-col-select")
        sel.click()
        page.locator(".p-multiselect-option", has_text="类型").click()
        expect(page.locator(".browse-pv th", has_text="类型")).to_have_count(0)
        # 其余列不受影响
        expect(page.locator(".browse-pv th", has_text="当前回撤")
              ).to_be_visible()
        # 重开下拉再勾选：选项列表随勾选原地重渲染，旧 overlay 节点直接
        # 复用会撞 detach 竞态——先点空白处收起（标题区，避开表格行点击）
        page.get_by_role("heading", name="定投止盈回测").click()
        sel.click()
        page.locator(".p-multiselect-option", has_text="类型").click()
        expect(page.locator(".browse-pv th", has_text="类型")).to_be_visible()
