# E2E 测试报告（金融危机史页 /data/crises，2026-11-24）

- 运行环境：`.venv/bin/python -m pytest tests/e2e/test_market_crises_page.py`，
  Playwright chromium headless；`servers` fixture 本地拉起
  uvicorn（坏 DSN 59999）+ vite preview（先 `npm run build` 再起服务），
  正常路径 `page.route` mock `/api/market/crises`，降级路径走真实坏 DSN。
- 结果统计：总 7 条 | 通过 7 | 失败 0 | 跳过 0 | 首轮耗时 ~18s
- 失败分析（逐条）：无。首轮曾有 3 条失败，归因与处置：
  | 用例 | 失败现象 | 归因 | 处置 |
  |---|---|---|---|
  | E2E-469 | `.nav-item.sub-active` 不出现 | **产品缺陷**（侧栏 sub-active 条件清单漏了 `crises`，切到金融危机史时「数据」组不高亮） | 已修 `App.vue` 侧栏条件，复测通过 |
  | E2E-466/470 | `AttributeError: 'LocatorAssertions' object has no attribute 'first'` | 测试脚本缺陷（`expect(locator).first` 写法错误，应为 `locator.first`） | 已修脚本，复测通过 |
- 覆盖评估：计划 7 条全部执行；E2E-464（P0 渲染）/465（P0 行切换+URL）/466（P1 双口径+时间线）/467（P1 深链接+刷新）/468（P1 降级占位）/469（P1 路由回归）/470（P2 单口径说明）均有断言落地。后端统计口径（crisis_stats/双线对齐/降级契约/日更双指数）由 `backend/tests/test_market_crises.py` 38 个单测覆盖，不在 E2E 范围。
- 稳定性评估：全量 E2E 套件连跑两轮均 166 passed（159 存量 + 7 新增），无 flaky、无随机失败；mock 数据路径与真实降级路径分离，无环境依赖。
- 结论：达到通过标准。发现并修复产品侧缺陷 1 处（侧栏归属高亮）；套件就绪。
