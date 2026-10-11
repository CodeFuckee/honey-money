"""PostgreSQL 存储：表结构、批量 upsert、断点查询、run 汇总。

无状态设计：所有方法接收已打开的 psycopg 连接，事务边界由调用方（runner
的写入协程）控制——既便于测试注入假连接，也保证单协程写库无并发竞争。

表说明：
- stock_basic  代码→市场/名称 快照（清单接口刷新，不分源）
- daily_bar   不复权日线，**主键 (code, trade_date, source)**：不同来源并存
  不覆盖，方便多源数据对比（如东财解封后重爬一遍即可 SQL diff）；
- crawl_run   每次运行汇总（含失败清单与来源），运维排查用；task_id 关联
  触发它的调度任务（NULL=CLI 直跑），trigger_kind 区分触发方式
  （manual=手动/CLI、schedule=到点、catch_up=启动补跑）——调度器的
  「今日已计划执行」判定只认后两者，手动触发不抑制当晚调度。

schema 升级：daily_bar 旧主键 (code,trade_date) 表会被 DROP 重建为三列主键
——仅限尚无业务数据的初始化阶段安全（写入数据后升级需另行迁移）。
"""

import json

import psycopg

DDL = """
CREATE TABLE IF NOT EXISTS stock_basic (
  code       VARCHAR(10) PRIMARY KEY,
  market     SMALLINT NOT NULL,
  name       VARCHAR(64) NOT NULL,
  delisted   BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_bar (
  code              VARCHAR(10) NOT NULL,
  trade_date        DATE        NOT NULL,
  open              DOUBLE PRECISION,
  close             DOUBLE PRECISION,
  high              DOUBLE PRECISION,
  low               DOUBLE PRECISION,
  volume            BIGINT,
  amount            DOUBLE PRECISION,
  amplitude         DOUBLE PRECISION,
  pct_chg           DOUBLE PRECISION,
  change            DOUBLE PRECISION,
  turnover          DOUBLE PRECISION,
  outstanding_share DOUBLE PRECISION,
  source            VARCHAR(16) NOT NULL DEFAULT 'eastmoney',
  PRIMARY KEY (code, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar (trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_bar_code ON daily_bar (code);

CREATE TABLE IF NOT EXISTS crawl_run (
  id             BIGSERIAL PRIMARY KEY,
  mode           VARCHAR(16) NOT NULL,
  source         VARCHAR(16),
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  total_stocks   INTEGER,
  ok_stocks      INTEGER,
  failed_stocks  INTEGER,
  bars_upserted  BIGINT,
  aborted        BOOLEAN NOT NULL DEFAULT false,
  detail         JSONB
);
"""

# 查询 daily_bar 现有主键列（表不存在返回 None 行）
_PK_CHECK = """
SELECT string_agg(a.attname, ',' ORDER BY a.attnum)
FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
WHERE i.indrelid = to_regclass('daily_bar') AND i.indisprimary
"""

_PK_EXPECTED = "code,trade_date,source"

_UPSERT_BAR = """
INSERT INTO daily_bar
  (code, trade_date, open, close, high, low, volume, amount,
   amplitude, pct_chg, change, turnover, outstanding_share, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (code, trade_date, source) DO UPDATE SET
  open = EXCLUDED.open, close = EXCLUDED.close, high = EXCLUDED.high,
  low = EXCLUDED.low, volume = EXCLUDED.volume, amount = EXCLUDED.amount,
  amplitude = EXCLUDED.amplitude, pct_chg = EXCLUDED.pct_chg,
  change = EXCLUDED.change, turnover = EXCLUDED.turnover,
  outstanding_share = EXCLUDED.outstanding_share
"""

_UPSERT_BASIC = """
INSERT INTO stock_basic (code, market, name, delisted)
VALUES (%s, %s, %s, COALESCE(%s, FALSE))
ON CONFLICT (code) DO UPDATE SET
  market = EXCLUDED.market, name = EXCLUDED.name,
  delisted = EXCLUDED.delisted, updated_at = now()
"""


class Store:
    def init_schema(self, conn) -> None:
        # 老库补列：stock_basic.delisted（退市股补爬标记，2026-10-11）
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE stock_basic "
                        "ADD COLUMN IF NOT EXISTS delisted "
                        "BOOLEAN NOT NULL DEFAULT FALSE")
        with conn.cursor() as cur:
            cur.execute(_PK_CHECK)
            row = cur.fetchone()
            if row and row[0] and row[0] != _PK_EXPECTED:
                # 旧主键结构：无数据阶段直接重建（见模块 docstring 的升级约定）
                cur.execute("DROP TABLE daily_bar")
            cur.execute(DDL)
            # crawl_run 旧表补列（CREATE IF NOT EXISTS 不会加新列）
            cur.execute("ALTER TABLE crawl_run ADD COLUMN IF NOT EXISTS source VARCHAR(16)")
            cur.execute("ALTER TABLE crawl_run ADD COLUMN IF NOT EXISTS aborted BOOLEAN "
                        "NOT NULL DEFAULT false")
            # 调度任务关联：task_id（NULL=CLI 直跑）与触发方式
            cur.execute("ALTER TABLE crawl_run ADD COLUMN IF NOT EXISTS task_id BIGINT")
            cur.execute("ALTER TABLE crawl_run ADD COLUMN IF NOT EXISTS trigger_kind "
                        "VARCHAR(16) NOT NULL DEFAULT 'manual'")

    def upsert_stock_basic(self, conn, stocks: list[dict]) -> None:
        if not stocks:
            return
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_BASIC, [
                (s["code"], s["market"], s["name"], s.get("delisted"))
                for s in stocks
            ])

    def max_trade_dates(self, conn, source: str | None = None) -> dict:
        """每只股票库内最新交易日（可按源隔离）——增量爬取的断点。"""
        with conn.cursor() as cur:
            if source:
                cur.execute(
                    "SELECT code, max(trade_date) FROM daily_bar "
                    "WHERE source = %s GROUP BY code", (source,))
            else:
                cur.execute(
                    "SELECT code, max(trade_date) FROM daily_bar GROUP BY code")
            return dict(cur.fetchall())

    def upsert_daily_bars(self, conn, code: str, rows: list[tuple],
                          source: str = "eastmoney") -> int:
        if not rows:
            return 0
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_BAR, [
                (code, row[0], row[1], row[2], row[3], row[4],
                 row[5], row[6], row[7], row[8], row[9], row[10],
                 row[11], source)
                for row in rows
            ])
        return len(rows)

    def start_run(self, conn, mode: str, source: str | None = None,
                  task_id: int | None = None, trigger: str = "manual") -> int:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crawl_run (mode, source, task_id, trigger_kind) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (mode, source, task_id, trigger))
            return cur.fetchone()[0]

    def finish_run(self, conn, run_id: int, *, total_stocks: int, ok_stocks: int,
                   failed_stocks: int, bars_upserted: int, failures: list[str],
                   aborted: bool = False) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crawl_run SET
                  finished_at = now(), total_stocks = %s, ok_stocks = %s,
                  failed_stocks = %s, bars_upserted = %s, aborted = %s, detail = %s
                WHERE id = %s
                """,
                (total_stocks, ok_stocks, failed_stocks, bars_upserted, aborted,
                 json.dumps({"failures": failures}), run_id),
            )
