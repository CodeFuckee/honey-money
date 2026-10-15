"""回测数据读取：从 PG 取不复权日线（固定新浪源），透视成 OHLCV 宽表。

无状态设计（同 browse/crawl_stats）：调用方传入 DSN，异常向上抛出，
由 API 层统一降级为 status=down（教学回测是同步接口，秒级返回）。
"""

import pandas as pd
import psycopg

_SQL = """
SELECT trade_date, code, open, high, low, close, volume
FROM daily_bar
WHERE source = 'sina' AND code = ANY(%s)
  {date_clause}
ORDER BY trade_date
"""


def sanitize_prices(wide: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """无效价清洗：OHLC 中 <=0 视为缺失（置 NaN），其余一律不动。

    新浪源曾吐 open=0 脏行（2024-11-06 三只科创板，全库仅此 3 行），而
    vectorbt 撮合对「非 NaN 但非正」的价格直接抛 ValueError；NaN 则走
    引擎既有的缺失容忍路径（各标的交易日不齐的停牌日一直如此），语义
    最小侵入。volume=0（停牌）不在此列。
    """
    return {col: (df.where(df > 0) if col in ("open", "high", "low", "close")
                  else df)
            for col, df in wide.items()}


def load_history(dsn: str, codes: list[str], *, start: str | None = None,
                 end: str | None = None) -> dict[str, pd.DataFrame]:
    """多标的日线宽表：{"open"/"high"/"low"/"close"/"volume": DataFrame(列=代码)}。

    各标的交易日不齐（新股/停牌）→ 透视后 NaN，由引擎容忍（按列独立成交）；
    价格 <=0 的脏行（源数据异常）先经 sanitize_prices 置 NaN 走同一容忍路径。
    """
    if not codes:
        raise ValueError("codes 不能为空")
    date_clause = ""
    params: list = [codes]
    if start:
        date_clause += " AND trade_date >= %s"
        params.append(start)
    if end:
        date_clause += " AND trade_date <= %s"
        params.append(end)

    def run() -> dict:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL.replace("{date_clause}", date_clause), params)
                rows = cur.fetchall()
        if not rows:
            return {}
        df = pd.DataFrame(rows, columns=["date", "code", "open", "high",
                                         "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"])
        wide = {col: df.pivot(index="date", columns="code", values=col)
                for col in ("open", "high", "low", "close", "volume")}
        return sanitize_prices(wide)

    return run()
