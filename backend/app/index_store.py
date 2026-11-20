"""指数日线存储：index_daily 表（symbol+trade_date 主键，幂等 upsert）。

当前仅上证指数（519 vs 924 对比页数据源）；东财 K 线接口对指数免 Cookie，
由指数日更任务（scheduler._run_index_job）写入，端点 market_compare 读取。
"""

_DDL = """
CREATE TABLE IF NOT EXISTS index_daily (
  symbol     TEXT    NOT NULL,
  trade_date DATE    NOT NULL,
  open       NUMERIC NOT NULL,
  high       NUMERIC NOT NULL,
  low        NUMERIC NOT NULL,
  close      NUMERIC NOT NULL,
  volume     BIGINT  NOT NULL DEFAULT 0,
  amount     NUMERIC NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, trade_date)
);
"""

# 东财 secid：1=沪。指数免 Cookie（公开 ut 即可，个股清单接口才要）。
INDEX_SYMBOLS = {"sh000001": {"market": 1, "code": "000001", "name": "上证指数"}}
SYMBOL = "sh000001"

_COLS = "(symbol, trade_date, open, high, low, close, volume, amount)"
_UPSERT = (
    f"INSERT INTO index_daily {_COLS} "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (symbol, trade_date) DO UPDATE SET "
    "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
    "close = EXCLUDED.close, volume = EXCLUDED.volume, "
    "amount = EXCLUDED.amount"
)


def ensure_schema(conn) -> None:
    """建表（启动兜底 + 日更前置幂等调用，教训3：新表必须在启动清单）。"""
    with conn.cursor() as cur:
        cur.execute(_DDL)


def upsert_bars(conn, symbol: str, rows: list[tuple]) -> None:
    """kline 12 元组批写（布局同 eastmoney.parse_kline_row：close 在第 3 位，
    volume 第 6 位、amount 第 7 位）。"""
    params = [(symbol, r[0], r[1], r[3], r[4], r[2], r[5], r[6]) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(_UPSERT, params)


def last_date(conn, symbol: str):
    """该指数最近交易日（增量拉取起点）；空表 None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM index_daily WHERE symbol = %s",
                    (symbol,))
        row = cur.fetchone()
    return row[0] if row else None


def fetch_closes(conn, symbol: str) -> list[tuple[str, float]]:
    """全史收盘序列（升序 ISO 日期串，供对比页对齐）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_char(trade_date, 'YYYY-MM-DD') AS d, close "
            "FROM index_daily WHERE symbol = %s ORDER BY trade_date",
            (symbol,))
        rows = cur.fetchall()
    return [(r[0], float(r[1])) for r in rows]
