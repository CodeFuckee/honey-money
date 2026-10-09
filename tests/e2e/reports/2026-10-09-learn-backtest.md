# E2E 测试报告（量化教学页 · 六大经典回测策略，2026-10-09）

- 运行环境：`.venv/bin/python -m pytest tests/e2e/test_learn_page.py -q`
  （Playwright chromium 无头；/api/backtest 与 /api/stocks 经 page.route mock）
- 结果统计：教学页 5/5 | 全套 E2E 26/26 两轮稳定 | 后端 216 全绿（新增 33：
  信号层 12 + 指标/成本 8 + 数据/API 契约 8 + vectorbt 撮合 5）
- 失败分析（自测期间已修复）：
  | 现象 | 归因 | 处置 |
  | --- | --- | --- |
  | vectorbt 1.1.1 + pandas 3 组合 numba 报 object 数组 | 环境兼容性（产品依赖锁缺失） | requirements 锁 pandas>=2.2,<3 |
  | from_signals 永不成交（净值恒 1） | vectorbt 1.x 的 size 默认 None（0.2x 是 inf） | engine 显式 size=np.inf, size_type="amount" |
  | 信号全 False | 无名 Series to_frame() 列名 0 被 reindex(columns) 整列丢弃 | norm_signals 先对齐列名 |
  | sl_fees 参数不存在 | vectorbt 1.x 移除卖出差分费用 | 印花税按双边均摊近似，教学页注明 |
  | 测试断言文案与 UI 不符等 3 处 | 测试脚本缺陷 | 修正断言 |
- 覆盖评估：策略导航/原理卡、回测结果（图/指标/交易表）、撮合切换入参、
  轮动池输入、详情页联动入口、空数据空态；未覆盖——真实 PG 全史数据的
  图表视觉（由用户过目 + NAS 实机覆盖）。
- 结论：达到通过标准；待用户有头过目（与前端过目合并）。
