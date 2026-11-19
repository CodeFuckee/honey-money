## E2E 测试报告（定投回测页「最近一次止盈后」当前轮统计，2026-11-19）
- 运行环境：`python -m pytest tests/e2e/test_dca_current_round_page.py`（Playwright chromium headless，conftest servers 自动拉起后端 uvicorn + 前端 vite preview，mock /api/funds/dca** 全家桶）
- 结果统计：总 6 条 | 通过 6 | 失败 0 | 跳过 0 | 耗时 ~13s；全量 E2E 套件 130 条全部通过（2m15s）；新用例 3 连跑全绿（无 flaky）
- 失败分析（逐条）：无
- 过程记录：首跑 E2E-448/449 红——mock 路由通配 `**/api/funds/dca*` 中 `*` 不跨 `/`，summary 子路径漏拦落到死 DSN 后端（卡片显示 —）；归因测试脚本缺陷，改 `**/api/funds/dca**`（同 mock_tp 的 tp** 写法）后 6 条全绿
- 覆盖评估：计划 6 条（E2E-444~449）全部实现并执行；排序请求参数拼接未做 E2E（确认门 1 用户选 6 条方案；服务端排序白名单由单测 TestApiCurColumns::test_cur_sortable_fee_suffix 覆盖）
- 稳定性评估：3 连跑结果一致，无随机性来源（纯 mock、无真实网络依赖）
- 结论：套件就绪；确认门 2 与前端过目合并进行（add-feature 16b/17 步约定）
