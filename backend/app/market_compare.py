"""519 vs 924 行情对比：同一指数序列按两个政策日 T 对齐的窗口比较。

口径：上证指数同源（中信研报原图为 wind全A，免费渠道不可得，页面注明）；
T 日收盘为基线，r(n)=close(T+n)/close(T)-1，n 为各自序列内交易日序偏移；
窗口 T-280 ~ T+1040 固定复刻研报图。两线出自 index_daily 一张表
（指数日更任务写入），端点按 T 定位切片后交给 align_lines 纯函数对齐。

端点约定同 /api/funds/dca：始终 200，PG 不可达/无数据在 status=down 表达。
"""

import psycopg

from . import config
from .index_store import SYMBOL, fetch_closes

T1_DATE = "1999-05-19"
T2_DATE = "2024-09-24"
BACK_N = 280                              # T 前（政策酝酿期）窗口
FWD_N = 1040                              # T 后（行情演绎期）窗口


def _validate(bars: list[tuple[str, float]]) -> None:
    """序列形状校验：非空、升序无重复、收盘全为正——脏数据明确报错。"""
    if not bars:
        raise ValueError("序列为空")
    dates = [b[0] for b in bars]
    if len(set(dates)) != len(dates):
        raise ValueError("日期重复")
    if any(a > b for a, b in zip(dates, dates[1:])):
        raise ValueError("日期必须升序")
    for d, c in bars:
        if c is None or c <= 0:
            raise ValueError(f"收盘价非正: {d} {c}")


def _locate(dates: list[str], t: str) -> int:
    try:
        return dates.index(t)
    except ValueError:
        raise ValueError(f"T 日 {t} 不在序列中") from None


def align_lines(bars1: list[tuple[str, float]], bars2: list[tuple[str, float]],
                t1: str, t2: str, *, back: int, fwd: int) -> list[dict]:
    """两条独立序列 → 按 n=交易日序偏移对齐的点列。

    每线窗口 [T-back, T+fwd] 在各自序列内裁剪；某线越界处该线
    d/r 为 None。r 保留 4 位小数。
    """
    _validate(bars1)
    _validate(bars2)

    def window(bars, t):
        dates = [b[0] for b in bars]
        p = _locate(dates, t)
        lo, hi = -min(back, p), min(fwd, len(bars) - 1 - p)
        base = float(bars[p][1])
        return {n: (dates[p + n], round(float(bars[p + n][1]) / base - 1, 4))
                for n in range(lo, hi + 1)}

    w1, w2 = window(bars1, t1), window(bars2, t2)
    return [{"n": n,
             "d1": w1[n][0] if n in w1 else None,
             "r1": w1[n][1] if n in w1 else None,
             "d2": w2[n][0] if n in w2 else None,
             "r2": w2[n][1] if n in w2 else None}
            for n in sorted(set(w1) | set(w2))]


def _connect():
    return psycopg.connect(config.DATABASE_URL)


def load_bars(conn, symbol: str) -> list[tuple[str, float]]:
    """读收盘序列（模块级薄封装：端点 monkeypatch 测试接缝）。"""
    return fetch_closes(conn, symbol)


def _down(detail: str) -> dict:
    return {"status": "down", "detail": detail, "points": [],
            "t2_progress": None}


def register_market_routes(app) -> None:
    @app.get("/api/market/519-924")
    def market_519_924():
        """519 vs 924 对比：两 T 日对齐的累计涨幅线（上证指数同源）。

        始终 200；PG 不可达 / 指数表无数据 / 对齐脏数据在 status=down
        表达（降级约定，前端渲染占位卡）。
        """
        try:
            with _connect() as conn:
                bars = load_bars(conn, SYMBOL)
        except Exception as exc:                    # noqa: BLE001 降级约定
            return _down(f"{type(exc).__name__}: {str(exc)[:120]}")
        if not bars:
            return _down("index_daily 暂无数据（等待首次指数日更）")
        try:
            p1 = _locate([b[0] for b in bars], T1_DATE)
            p2 = _locate([b[0] for b in bars], T2_DATE)
            b1 = bars[max(0, p1 - BACK_N):p1 + FWD_N + 1]
            b2 = bars[max(0, p2 - BACK_N):p2 + FWD_N + 1]
            points = align_lines(b1, b2, T1_DATE, T2_DATE,
                                 back=BACK_N, fwd=FWD_N)
        except ValueError as exc:
            return _down(str(exc))
        progress = next(({"n": p["n"], "date": p["d2"], "ret": p["r2"]}
                         for p in reversed(points) if p["r2"] is not None),
                        None)
        return {"status": "up",
                "symbol": {"code": SYMBOL, "name": "上证指数",
                           "source": "eastmoney"},
                "t1": {"date": T1_DATE, "label": "5·19 行情"},
                "t2": {"date": T2_DATE, "label": "9·24 行情"},
                "back": BACK_N, "fwd": FWD_N,
                "points": points, "t2_progress": progress}
