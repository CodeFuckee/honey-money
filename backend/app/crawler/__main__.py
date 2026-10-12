"""爬虫 CLI：python -m app.crawler

用法示例（backend 容器内 / PYTHONPATH=backend）：
  python -m app.crawler                          # 新浪源增量（默认，防封参数内置）
  python -m app.crawler --limit 20               # 试跑前 20 只
  python -m app.crawler --source eastmoney       # 东财源（解封后用于校验/补数）
  python -m app.crawler --mode full --only 600000
退出码：0 全部成功；1 有失败股票或触发熔停；2 参数/环境错误。

防封默认值（新浪源）：1 QPS 起步（AIMD 自适应、上限 5）+ 间隔抖动 ±30% +
每 300 只歇 30s + 连续 15 只失败熔停。
东财源默认 drip 节奏：每 30 秒 1 请求（±30% 随机抖动 = 21~39s 实际间隔），
min=max 锁死不提速（出口 IP 曾被封，宁慢勿封）；清单加载同样按此节奏。
"""

import argparse
import asyncio
import sys

import httpx
import psycopg

from .. import config
from .delisted import DelistedClient
from .eastmoney import EastmoneyClient
from .proxies import ProxyBroker
from .rate import RateLimiter
from .runner import CrawlRunner
from .sina import SinaClient
from .store import Store

PROGRESS_EVERY = 200   # 每完成 N 只打一行进度

# 各源默认限速：新浪直连宽松但保守起步；东财出口 IP 曾被封，锁 drip：
# 1/30 QPS（每 30 秒 1 请求）且 min=max——AIMD 不升不降，节奏完全固定，
# 间隔随机化交给 jitter（±30% → 21~39s），消除固定节奏的机器特征。
SOURCE_DEFAULTS = {
    "sina": {"initial_qps": 1.0, "max_qps": 5.0},
    "eastmoney": {"initial_qps": 1 / 30, "max_qps": 1 / 30, "min_qps": 1 / 30},
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m app.crawler",
        description="A 股历史日线爬虫（多源：新浪(默认)/东财；按域名动态限速+断点续爬）",
    )
    parser.add_argument("--source", choices=["sina", "eastmoney", "delisted"],
                        default="sina",
                        help="数据源（默认 sina：直连可用且带换手率字段；"
                             "eastmoney 出口 IP 曾被封禁，慎用；delisted=退市股"
                             "历史补爬，klc_kl 全史无换手率字段）")
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental",
                        help="incremental=按库内断点续爬（默认）；full=全量重爬")
    parser.add_argument("--limit", type=int, default=None, help="只处理清单前 N 只（试跑）")
    parser.add_argument("--only", default=None, help="只爬指定代码，逗号分隔：600000,000001")
    parser.add_argument("--concurrency", type=int, default=4, help="并发抓取协程数（默认 4）")
    parser.add_argument("--initial-qps", type=float, default=None,
                        help="每通道初始 QPS（默认按源：sina=1 eastmoney=1）")
    parser.add_argument("--max-qps", type=float, default=None,
                        help="每通道 QPS 上限（默认按源：sina=5 eastmoney=1/30）")
    parser.add_argument("--min-qps", type=float, default=None,
                        help="每通道 QPS 下限（AIMD 降速托底；默认 0.2，"
                             "东财源默认 1/30 与初始/上限一致即锁死节奏）")
    parser.add_argument("--jitter", type=float, default=0.3,
                        help="请求间隔随机抖动比例（默认 0.3=±30%%，防封措施）")
    parser.add_argument("--batch-size", type=int, default=300,
                        help="每爬 N 只歇一批（默认 300；0=不分批）")
    parser.add_argument("--batch-pause", type=float, default=30.0,
                        help="批间休眠秒数（默认 30）")
    parser.add_argument("--abort-after", type=int, default=15,
                        help="连续 N 只失败自动熔停保存退出（默认 15；0=不熔停）")
    parser.add_argument("--proxy-mode", choices=["pool", "direct"], default="pool",
                        help="（仅东财源）pool=走代理池(空池自动回退直连)；"
                             "direct=全直连")
    parser.add_argument("--proxy-retries", type=int, default=2,
                        help="（仅东财源）传输层失败换代理重试次数（默认 2）")
    return parser.parse_args(argv)


async def amain(args) -> int:
    defaults = SOURCE_DEFAULTS[args.source]
    initial_qps = args.initial_qps if args.initial_qps is not None else defaults["initial_qps"]
    max_qps = args.max_qps if args.max_qps is not None else defaults["max_qps"]
    min_qps = args.min_qps if args.min_qps is not None else defaults.get("min_qps", 0.2)

    rate = RateLimiter(initial_qps=initial_qps, min_qps=min_qps, max_qps=max_qps,
                       jitter=args.jitter)
    store = Store()

    if args.source == "sina":
        direct = httpx.AsyncClient(timeout=30.0)
        client = SinaClient(rate=rate, http=direct)
    elif args.source == "delisted":
        direct = httpx.AsyncClient(timeout=30.0)
        client = DelistedClient(rate=rate, http=direct)
    else:
        direct = httpx.AsyncClient(timeout=15.0)
        pool = httpx.AsyncClient(base_url=config.PROXY_POOL_URL, timeout=5.0)
        broker = ProxyBroker(http=pool)
        client = EastmoneyClient(rate=rate, proxy_broker=broker, http=direct,
                                 max_proxy_retries=args.proxy_retries,
                                 proxy_mode=args.proxy_mode)
    try:
        stocks = await client.fetch_stock_list()
        if args.only:
            wanted = {c.strip() for c in args.only.split(",") if c.strip()}
            stocks = [s for s in stocks if s["code"] in wanted]
            missing = wanted - {s["code"] for s in stocks}
            if missing:
                print(f"[error] 清单中不存在: {sorted(missing)}", file=sys.stderr)
                return 2
        print(f"[{args.source}] 清单 {len(stocks)} 只（mode={args.mode}, "
              f"qps {initial_qps:.4g}->{max_qps:.4g}(min {min_qps:.4g}), jitter ±{args.jitter:.0%}, "
              f"batch {args.batch_size}/{args.batch_pause}s"
              + (f", limit={args.limit}" if args.limit else "") + "）")

        def progress(done: int, total: int) -> None:
            if done % PROGRESS_EVERY == 0 or done == total:
                print(f"[progress] {done}/{total}")

        with psycopg.connect(config.DATABASE_URL) as conn:
            store.init_schema(conn)
            if args.source == "delisted":
                n = store.migrate_delisted_source(conn)
                if n:
                    print(f"[migrate] {n} 行 source delisted → sina")
            store.upsert_stock_basic(conn, stocks)
            conn.commit()
            runner = CrawlRunner(
                client=client, store=store, conn=conn,
                concurrency=args.concurrency, progress=progress,
            )
            summary_total = len(stocks)

            def batch_commit(done: int) -> None:
                conn.commit()
                print(f"[commit] 已提交 {done}/{summary_total} 只")

            summary = await runner.run(
                stocks, mode=args.mode, limit=args.limit, source=args.source,
                batch_size=args.batch_size or None,
                batch_pause=args.batch_pause,
                abort_after_consecutive_failures=args.abort_after or None,
                on_batch=batch_commit,
            )

        print(f"[done] source={summary.source} 总数={summary.total_stocks} "
              f"成功={summary.ok_stocks} 失败={summary.failed_stocks} "
              f"入库K线={summary.bars_upserted} 耗时={summary.elapsed_sec}s"
              + ("（已熔停）" if summary.aborted else ""))
        if summary.failures:
            print(f"[failures] {summary.failures}")
        print(f"[rate] {rate.stats()}")
        return 1 if (summary.failed_stocks or summary.aborted) else 0
    finally:
        if hasattr(client, "aclose"):
            await client.aclose()
        await direct.aclose()


def main() -> None:
    args = parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
