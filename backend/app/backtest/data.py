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


def load_history(dsn: str, codes: list[str], *, start: str | None = None,
                 end: str | None = None) -> dict[str, pd.DataFrame]:
    """多标的日线宽表：{"open"/"high"/"low"/"close"/"volume": DataFrame(列=代码)}。

    各标的交易日不齐（新股/停牌）→ 透视后 NaN，由引擎容忍（按列独立成交）。
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
        return {col: df.pivot(index="date", columns="code", values=col)
                for col in ("open", "high", "low", "close", "volume")}

    return run()
