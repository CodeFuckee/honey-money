"""东财 A 股日线爬虫子包。

模块划分：
- rate.py        按域名通道的动态限速器（滑动窗口成功率 AIMD + 熔断冷却）
- proxies.py     代理池经纪（jhao104/proxy_pool 的 /get、/delete 封装）
- eastmoney.py   东财 HTTP 接口客户端（清单 + 日线 K 线）
- store.py       PostgreSQL 存储（建表 / 批量 upsert / 断点查询 / run 汇总）
- runner.py      爬取编排（并发抓取 + 单写入库流水线）
- __main__.py    CLI 入口：python -m app.crawler
"""
