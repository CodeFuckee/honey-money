# E2E 测试报告（个股历史行情详情页，2026-10-09）

- 运行环境：`.venv/bin/python -m pytest tests/e2e/test_stock_detail.py -q`
  （Playwright chromium 无头，fixture 自拉 uvicorn + vite preview，
  `/api/stocks` 与 `/api/stocks/*/bars` 经 page.route mock）
- 结果统计：个股详情 5/5 通过；全套 E2E 21/21（含存量 16 条）两轮稳定，无 flaky
- 失败分析（自测期间的脚本缺陷，均已修复）：
  | 现象 | 归因 | 处置 |
  | --- | --- | --- |
  | 详情页 heading 无文本 | 测试暴露的产品实现缺陷：PrimeVue DataTable `row-click` 参数是 `{originalEvent, data, index}` 包装对象，直接当行数据用导致 code/name undefined | 修 openStock 取 `e.data`（前端修复，非测试改动） |
  | 返回后列表页码回 1 | 产品实现缺陷：数据页 v-else-if 卸载 DataTable，重挂后 PrimeVue 内部分页状态归零 | 列表/明细表改受控分页（:first 绑定页码状态） |
  | 明细首行非最新日期 | 测试断言遗漏：API 为图表返回升序，表格展示应倒序 | 明细表倒序展示（最新在上） |
  | 断言「东财源」≠ UI「东方财富源」 | 测试脚本缺陷 | 断言对齐实际文案 |
- 覆盖评估：确认门 1 用例表 6 条全部实现（E2E-201~206）；未覆盖——真实 PG
  数据下的图表视觉（由用户过目 + NAS 实机验证）。
- 结论：达到通过标准；待用户有头过目（确认门 2，与前端过目合并）。
