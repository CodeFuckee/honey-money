"""PostgreSQL 存储：表结构、批量 upsert、断点查询、run 汇总。

无状态设计：所有方法接收已打开的 psycopg 连接，事务边界由调用方（runner
的写入协程）控制——既便于测试注入假连接，也保证单协程写库无并发竞争。

表说明：
- stock_basic  代码→市场/名称 快照（清单接口刷新）
- daily_bar   不复权日线，(code, trade_date) 主键；ON CONFLICT 覆盖更新——
              当日数据盘后重拉必须能刷新旧值
- crawl_run   每次运行汇总（含失败清单），运维排查用
"""

import json

import psycopg

DDL = """
CREATE TABLE IF NOT EXISTS stock_basic (
  code       VARCHAR(10) PRIMARY KEY,
  market     SMALLINT NOT NULL,
  name       VARCHAR(64) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_bar (
  code       VARCHAR(10) NOT NULL,
  trade_date DATE        NOT NULL,
  open       DOUBLE PRECISION,
  close      DOUBLE PRECISION,
  high       DOUBLE PRECISION,
  low        DOUBLE PRECISION,
  volume     BIGINT,
  amount     DOUBLE PRECISION,
  amplitude  DOUBLE PRECISION,
  pct_chg    DOUBLE PRECISION,
  change     DOUBLE PRECISION,
  turnover   DOUBLE PRECISION,
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar (trade_date);

CREATE TABLE IF NOT EXISTS crawl_run (
  id             BIGSERIAL PRIMARY KEY,
  mode           VARCHAR(16) NOT NULL,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  total_stocks   INTEGER,
  ok_stocks      INTEGER,
  failed_stocks  INTEGER,
  bars_upserted  BIGINT,
  detail         JSONB
);
"""

_UPSERT_BAR = """
INSERT INTO daily_bar
  (code, trade_date, open, close, high, low, volume, amount,
   amplitude, pct_chg, change, turnover)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (code, trade_date) DO UPDATE SET
  open = EXCLUDED.open, close = EXCLUDED.close, high = EXCLUDED.high,
  low = EXCLUDED.low, volume = EXCLUDED.volume, amount = EXCLUDED.amount,
  amplitude = EXCLUDED.amplitude, pct_chg = EXCLUDED.pct_chg,
  change = EXCLUDED.change, turnover = EXCLUDED.turnover
"""

_UPSERT_BASIC = """
INSERT INTO stock_basic (code, market, name)
VALUES (%s, %s, %s)
ON CONFLICT (code) DO UPDATE SET
  market = EXCLUDED.market, name = EXCLUDED.name, updated_at = now()
"""


class Store:
    def init_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(DDL)

    def upsert_stock_basic(self, conn, stocks: list[dict]) -> None:
        if not stocks:
            return
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_BASIC, [
                (s["code"], s["market"], s["name"]) for s in stocks
            ])

    def max_trade_dates(self, conn) -> dict:
        """每只股票库内最新交易日——增量爬取的断点。"""
        with conn.cursor() as cur:
            cur.execute("SELECT code, max(trade_date) FROM daily_bar GROUP BY code")
            return dict(cur.fetchall())

    def upsert_daily_bars(self, conn, code: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_BAR, [
                (code, row[0], row[1], row[2], row[3], row[4],
                 row[5], row[6], row[7], row[8], row[9], row[10])
                for row in rows
            ])
        return len(rows)

    def start_run(self, conn, mode: str) -> int:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crawl_run (mode) VALUES (%s) RETURNING id", (mode,))
            return cur.fetchone()[0]

    def finish_run(self, conn, run_id: int, *, total_stocks: int, ok_stocks: int,
                   failed_stocks: int, bars_upserted: int, failures: list[str]) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crawl_run SET
                  finished_at = now(), total_stocks = %s, ok_stocks = %s,
                  failed_stocks = %s, bars_upserted = %s, detail = %s
                WHERE id = %s
                """,
                (total_stocks, ok_stocks, failed_stocks, bars_upserted,
                 json.dumps({"failures": failures}), run_id),
            )
