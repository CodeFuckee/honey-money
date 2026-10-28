"""基金数据存储：fund_basic / fund_nav 表 + 批量 upsert + 断点查询。

继承 Store 复用 crawl_run 运行簿记（start_run/finish_run），仅扩展基金
两张表；max_trade_dates 覆写为读 fund_nav（增量断点对齐 runner 既有约定，
实际基金接口单请求全史、upsert 幂等，断点仅省去不了请求量但保持语义一致）。

无状态设计同 crawler/store.py：所有方法接收已打开的 psycopg 连接。
"""

from .store import Store

_DDL_FUND = """
CREATE TABLE IF NOT EXISTS fund_basic (
  code       VARCHAR(10) PRIMARY KEY,
  name       VARCHAR(128) NOT NULL,
  fund_type  VARCHAR(64),
  delisted   BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fund_nav (
  code       VARCHAR(10) NOT NULL,
  nav_date   DATE        NOT NULL,
  unit_nav   DOUBLE PRECISION,
  accum_nav  DOUBLE PRECISION,
  pct_chg    DOUBLE PRECISION,
  unit_money VARCHAR(64),
  source     VARCHAR(16) NOT NULL DEFAULT 'fund_em',
  PRIMARY KEY (code, nav_date, source)
);
CREATE INDEX IF NOT EXISTS idx_fund_nav_date ON fund_nav (nav_date);
CREATE INDEX IF NOT EXISTS idx_fund_nav_code ON fund_nav (code);
"""

_UPSERT_FUND_BASIC = """
INSERT INTO fund_basic (code, name, fund_type, delisted)
VALUES (%s, %s, %s, COALESCE(%s, FALSE))
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  fund_type = COALESCE(EXCLUDED.fund_type, fund_basic.fund_type),
  delisted = COALESCE(EXCLUDED.delisted, fund_basic.delisted),
  updated_at = now()
"""

# 保全型 upsert：EXCLUDED 为 NULL 时保留库内值——快照增量（部分列恒空，
# 如昨日槽无增长率、货基无分红串）不得把全量爬取已入库的数据抹成 NULL
_UPSERT_NAV = """
INSERT INTO fund_nav
  (code, nav_date, unit_nav, accum_nav, pct_chg, unit_money, source)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (code, nav_date, source) DO UPDATE SET
  unit_nav = COALESCE(EXCLUDED.unit_nav, fund_nav.unit_nav),
  accum_nav = COALESCE(EXCLUDED.accum_nav, fund_nav.accum_nav),
  pct_chg = COALESCE(EXCLUDED.pct_chg, fund_nav.pct_chg),
  unit_money = COALESCE(EXCLUDED.unit_money, fund_nav.unit_money)
"""


class FundStore(Store):
    def init_schema(self, conn) -> None:
        super().init_schema(conn)            # crawl_run 等公共表照常建
        with conn.cursor() as cur:
            cur.execute(_DDL_FUND)
        # 老库补列：fund_basic.delisted（在市集合差集标记，2026-10-27）
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE fund_basic "
                        "ADD COLUMN IF NOT EXISTS delisted "
                        "BOOLEAN NOT NULL DEFAULT FALSE")

    def upsert_fund_basic(self, conn, funds: list[dict]) -> None:
        if not funds:
            return
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_FUND_BASIC, [
                (f["code"], f["name"], f.get("fund_type"), f.get("delisted"))
                for f in funds])

    def upsert_fund_navs(self, conn, code: str, rows: list[tuple],
                         source: str = "fund_em") -> int:
        if not rows:
            return 0
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_NAV, [
                (code, row[0], row[1], row[2], row[3], row[4], source)
                for row in rows])
        return len(rows)

    def upsert_fund_nav_rows(self, conn, rows: list[dict],
                             source: str = "fund_em") -> int:
        """快照增量批量落库（rows 为 parse_jjjz_snapshot 输出的 dict 列表）。"""
        if not rows:
            return 0
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_NAV, [
                (r["code"], r["nav_date"], r["unit_nav"], r["accum_nav"],
                 r["pct_chg"], None, source)
                for r in rows])
        return len(rows)

    def max_trade_dates(self, conn, source: str | None = None) -> dict:
        """每只基金库内最新净值日（按源隔离）——runner 增量断点。"""
        with conn.cursor() as cur:
            if source:
                cur.execute(
                    "SELECT code, max(nav_date) FROM fund_nav "
                    "WHERE source = %s GROUP BY code", (source,))
            else:
                cur.execute(
                    "SELECT code, max(nav_date) FROM fund_nav GROUP BY code")
            return dict(cur.fetchall())
