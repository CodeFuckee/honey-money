"""复权因子：懒加载入库 + 后/前复权合成。

数据策略（教学回测与详情页复权共用）：
- 因子历史一经除权即固定、极少变化——回测/详情页需要时**懒加载**：
  库内因子末日 ≥ 该股最新行情交易日 → 直接用库；否则重拉全史入库
  （捕捉新除权日）；拉取失败标记 missing，调用方降级不复权（可用性优先）；
- 因子口径：新浪后复权累计因子（hfq-factor，首日=1）——
  后复权价 = 真实价 × 因子（回测收益 = 真实持有收益）；
  前复权价 = 真实价 × 因子 / 最新因子（最新价 = 真实价，历史价下调）。
"""

import pandas as pd
import psycopg

_DDL = """
CREATE TABLE IF NOT EXISTS adjust_factor (
  code       VARCHAR(10)       NOT NULL,
  trade_date DATE              NOT NULL,
  factor     DOUBLE PRECISION  NOT NULL,
  PRIMARY KEY (code, trade_date)
);
"""

_UPSERT = """
INSERT INTO adjust_factor (code, trade_date, factor) VALUES (%s, %s, %s)
ON CONFLICT (code, trade_date) DO UPDATE SET factor = EXCLUDED.factor
"""


def _symbol(code: str) -> str:
    """A股代码 → 新浪 symbol：6 开头沪市 sh，0/3 开头深市 sz。"""
    return ("sh" if code.startswith("6") else "sz") + code


def _default_fetch(symbol: str) -> pd.Series:
    """新浪后复权因子（akshare，与日线爬虫同源直连）。

    返回为**稀疏序列**：仅在除权除息日有行（另有 1900-01-01 哨兵行，丢弃），
    因子为累计口径——合成时靠 reindex+ffill 展开到逐日。"""
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=symbol, adjust="hfq-factor")
    col = "hfq_factor" if "hfq_factor" in df.columns else "factor"
    if col not in df.columns:
        return pd.Series(dtype=float)
    s = pd.Series(df[col].values, index=pd.to_datetime(df["date"]))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s.index > pd.Timestamp("1901-01-01")]          # 丢哨兵行


def load_factors(conn, code: str) -> pd.Series:
    with conn.cursor() as cur:
        cur.execute("SELECT trade_date, factor FROM adjust_factor "
                    "WHERE code = %s ORDER BY trade_date", (code,))
        rows = cur.fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series([r[1] for r in rows],
                     index=pd.to_datetime([r[0] for r in rows]))


def ensure_factors(dsn: str, codes: list[str], *, latest_trade_dates: dict,
                   gap_dates: dict | None = None,
                   connect=None, fetch=None) -> dict[str, str]:
    """确保每只股票的复权因子覆盖到最新除权日，返回 {code: ok|missing}。

    latest_trade_dates: {code: 该股最新行情交易日}（来自 daily_bar）。
    gap_dates: {code: 疑似除权跳空日列表}（来自 daily_bar 的隔夜跳空检测）。

    新鲜判定：因子接口返回**稀疏序列**（仅除权日有行，末日=最近除权日），
    永远追不上最新行情日——因此判定为：
    因子末日 ≥ 最新行情日（近期除权过）
    或 因子末日 ≥ 最后一个疑似跳空日（末日之后 daily_bar 无新除权迹象）
    两者满足其一即视为缓存新鲜，否则重拉。
    """
    connect = connect or psycopg.connect
    fetch = fetch or _default_fetch
    status: dict[str, str] = {}
    with connect(dsn) as conn:
        conn.cursor().execute(_DDL)                    # 幂等建表
        for code in codes:
            latest = latest_trade_dates.get(code)
            if latest is None:                         # 无行情数据：因子无意义
                status[code] = "missing"
                continue
            try:
                cached = load_factors(conn, code)
                fresh = False
                if not cached.empty:
                    last_factor = cached.index.max().date()
                    gaps = [d for d in ((gap_dates or {}).get(code) or [])
                            if d <= latest]
                    if last_factor >= latest:
                        fresh = True                   # 近期除权过，因子到今日
                    elif gaps and last_factor >= max(gaps):
                        fresh = True                   # 末日之后无新除权迹象
                if not fresh:                          # 无缓存 / 因子过期 → 重拉
                    f = fetch(_symbol(code))
                    if f.empty:
                        status[code] = "missing"
                        continue
                    with conn.cursor() as cur:
                        cur.executemany(_UPSERT, [
                            (code, d.date() if hasattr(d, "date") else d,
                             float(v)) for d, v in f.items()])
                    conn.commit()
                status[code] = "ok"
            except Exception:                          # 新浪不可达等：降级标记
                status[code] = "missing"
    return status


def aligned_factor(factors: dict, col: str, index) -> pd.Series | None:
    f = factors.get(col)
    if f is None or f.empty:
        return None
    if not isinstance(f.index, pd.DatetimeIndex):
        f.index = pd.to_datetime(f.index)
    return (f.sort_index().reindex(index.union(f.index))
            .ffill().reindex(index))


def apply_hfq(bars: dict[str, pd.DataFrame], factors: dict) -> dict[str, pd.DataFrame]:
    """后复权：真实价 × 累计因子（回测收益 = 真实持有收益）。"""
    out = {}
    for k, df in bars.items():
        out[k] = df.copy()
        for col in df.columns:
            f = aligned_factor(factors, col, df.index)
            if f is not None:
                out[k][col] = df[col] * f
    return out


def apply_qfq(bars: dict[str, pd.DataFrame], factors: dict) -> dict[str, pd.DataFrame]:
    """前复权：真实价 × 因子 / 最新因子（最新价 = 真实价，历史价下调）。"""
    out = {}
    for k, df in bars.items():
        out[k] = df.copy()
        for col in df.columns:
            f = aligned_factor(factors, col, df.index)
            if f is not None:
                out[k][col] = df[col] * f / f.iloc[-1]
    return out


def ensure_and_apply(bars: dict[str, pd.DataFrame], codes: list[str],
                     dsn: str) -> tuple[dict, dict | None]:
    """回测用的复权懒加载一步式封装：确保因子 → 合成后复权价。

    因子不可用（新浪断连等）→ 返回原 bars + None（调用方降级不复权）。
    提取自 backtest_api 的内联 _with_factors，单股/全量回测共用。
    """
    from .backtest.engine import detect_gap_dates
    latest, gaps = {}, {}
    cl = bars["close"]
    for code in codes:
        if code in cl.columns and cl[code].notna().any():
            sub = cl[[code]].dropna()
            latest[code] = sub.index.max().date()
            prev = sub[code].shift(1)
            gap_mask = ((sub[code] / prev - 1).abs() > 0.098) & prev.notna()
            if gap_mask.any():
                gaps[code] = [d.date() for d in sub.index[gap_mask]]
    status = ensure_factors(dsn, codes,
                            latest_trade_dates=latest, gap_dates=gaps)
    if not all(v == "ok" for v in status.values()):
        return bars, None
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        factors = {code: load_factors(conn, code) for code in codes}
    return apply_hfq(bars, factors), factors
