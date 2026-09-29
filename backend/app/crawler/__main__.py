"""爬虫 CLI：python -m app.crawler

用法示例（backend 容器内 / PYTHONPATH=backend）：
  python -m app.crawler                       # 增量：按库内断点续爬/补新
  python -m app.crawler --mode full           # 全量重爬（beg=0 覆盖更新）
  python -m app.crawler --limit 20            # 试跑前 20 只
  python -m app.crawler --only 600000,000001  # 指定股票
退出码：0 全部成功；1 有失败股票（便于计划任务告警）；2 参数/环境错误。
"""

import argparse
import asyncio
import sys

import httpx
import psycopg

from .. import config
from .eastmoney import EastmoneyClient
from .proxies import ProxyBroker
from .rate import RateLimiter
from .runner import CrawlRunner
from .store import Store

PROGRESS_EVERY = 200   # 每完成 N 只打一行进度


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m app.crawler",
        description="东方财富 A 股历史日线爬虫（代理池 + 按域名动态限速）",
    )
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental",
                        help="incremental=按库内断点续爬（默认）；full=全量重爬")
    parser.add_argument("--limit", type=int, default=None, help="只处理清单前 N 只（试跑）")
    parser.add_argument("--only", default=None, help="只爬指定代码，逗号分隔：600000,000001")
    parser.add_argument("--concurrency", type=int, default=8, help="并发抓取协程数（默认 8）")
    parser.add_argument("--initial-qps", type=float, default=2.0, help="每通道初始 QPS")
    parser.add_argument("--max-qps", type=float, default=20.0, help="每通道 QPS 上限")
    parser.add_argument("--proxy-mode", choices=["pool", "direct"], default="pool",
                        help="pool=走代理池(空池自动回退直连，默认)；direct=全部直连"
                             "（实测东财断连免费代理的机房 IP，家宽直连+低QPS更稳）")
    parser.add_argument("--proxy-retries", type=int, default=2,
                        help="传输层失败换代理重试次数（默认 2）")
    return parser.parse_args(argv)


async def amain(args) -> int:
    direct = httpx.AsyncClient(timeout=15.0)
    pool = httpx.AsyncClient(base_url=config.PROXY_POOL_URL, timeout=5.0)
    rate = RateLimiter(initial_qps=args.initial_qps, max_qps=args.max_qps)
    broker = ProxyBroker(http=pool)
    client = EastmoneyClient(rate=rate, proxy_broker=broker, http=direct,
                             max_proxy_retries=args.proxy_retries,
                             proxy_mode=args.proxy_mode)
    store = Store()
    try:
        stocks = await client.fetch_stock_list()
        if args.only:
            wanted = {c.strip() for c in args.only.split(",") if c.strip()}
            stocks = [s for s in stocks if s["code"] in wanted]
            missing = wanted - {s["code"] for s in stocks}
            if missing:
                print(f"[error] 清单中不存在: {sorted(missing)}", file=sys.stderr)
                return 2
        print(f"清单 {len(stocks)} 只（mode={args.mode}"
              + (f", limit={args.limit}" if args.limit else "") + "）")

        def progress(done: int, total: int) -> None:
            if done % PROGRESS_EVERY == 0 or done == total:
                print(f"[progress] {done}/{total}")

        with psycopg.connect(config.DATABASE_URL) as conn:
            store.init_schema(conn)
            store.upsert_stock_basic(conn, stocks)
            conn.commit()
            runner = CrawlRunner(client=client, store=store, conn=conn,
                                 concurrency=args.concurrency, progress=progress)
            summary = await runner.run(stocks, mode=args.mode, limit=args.limit)

        print(f"[done] 总数={summary.total_stocks} 成功={summary.ok_stocks} "
              f"失败={summary.failed_stocks} 入库K线={summary.bars_upserted} "
              f"耗时={summary.elapsed_sec}s")
        if summary.failures:
            print(f"[failures] {summary.failures}")
        print(f"[rate] {rate.stats()}")
        return 1 if summary.failed_stocks else 0
    finally:
        await client.aclose()
        await direct.aclose()
        await pool.aclose()


def main() -> None:
    args = parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
