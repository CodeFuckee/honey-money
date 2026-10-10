"""复权因子模块测试：懒加载入库缓存策略、后/前复权合成、降级标记。

缓存契约（ensure_factors）：
- 库内因子末日 ≥ 该股最新行情交易日 → 直接用库（不发请求）；
- 否则（无因子/因子过期）→ 拉取全史入库；
- 拉取失败 → 该股标记 missing（调用方降级不复权），不抛错。

合成契约（apply_hfq / apply_qfq）：
- 后复权价 = 真实价 × 因子（收益 = 真实持有收益）；
- 前复权价 = 真实价 × 因子 / 最新因子（最新价 = 真实价）；
- 除权日无跳空（比亚迪 10 转 3 案例：价格 ÷3、因子 ×3）。
"""

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adjust import (  # noqa: E402
    apply_qfq, apply_hfq, ensure_factors)


def factor_series(dates, values):
    return pd.Series(values, index=pd.to_datetime(dates))


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self.conn.fetchall_rows

    def fetchone(self):
        return self.conn.fetchone_row

    def executemany(self, query, seq):
        self.conn.upserted.extend(seq)


class FakeConn:
    def __init__(self, rows=()):
        self.fetchall_rows = list(rows)
        self.fetchone_row = None
        self.upserted = []
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestApplyAdjust:
    def setup_method(self):
        dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"])
        # 比亚迪式除权：1/2 收盘 337（除权前），1/5 起价格 ÷3（112.33），
        # 因子 ×3（后复权因子累计：1.0 → 3.0）
        close = pd.Series([337.0, 112.33, 113.0], index=dates)
        bars = {"close": close.to_frame("002594"),
                "open": (close * 0.99).to_frame("002594")}
        factors = {"002594": factor_series(dates, [1.0, 3.0, 3.0])}
        return bars, factors

    def test_hfq_removes_fake_gap(self):
        """后复权：1/2 价 337×1=337，1/5 起 112.33×3=336.99——除权跳空消失。"""
        bars, factors = self.setup_method()
        adj = apply_hfq(bars, factors)
        s = adj["close"]["002594"]
        assert s.iloc[0] == pytest.approx(337.0)
        assert s.iloc[1] == pytest.approx(112.33 * 3.0)     # 336.99
        # 除权日隔夜变动 ≈ 0（真实收益口径）
        assert abs(s.iloc[1] / s.iloc[0] - 1) < 0.001

    def test_qfq_latest_price_equals_real(self):
        """前复权：最新因子基准——最新价=真实价，历史价按比例下调。"""
        bars, factors = self.setup_method()
        adj = apply_qfq(bars, factors)
        s = adj["close"]["002594"]
        assert s.iloc[-1] == pytest.approx(113.0)            # 最新价=真实价
        assert s.iloc[0] == pytest.approx(337.0 / 3.0)       # 历史价 ÷3

    def test_factor_gap_ffill(self):
        """因子序列缺末日（罕见）：前向填充，价格合成不产生 NaN。"""
        bars, factors = self.setup_method()
        factors = {"002594": factor_series(
            ["2026-01-01", "2026-01-02"], [1.0, 3.0])}   # 缺 01-05 的因子
        adj = apply_hfq(bars, factors)
        s = adj["close"]["002594"]
        assert s.notna().all()
        assert s.iloc[-1] == pytest.approx(113.0 * 3.0)  # 沿用前值因子

    def test_unknown_code_left_untouched(self):
        """无因子数据的代码（降级）：价格原样返回。"""
        bars, _ = self.setup_method()
        adj = apply_hfq(bars, {})                            # 空因子 → 不复权
        assert adj["close"]["002594"].iloc[0] == pytest.approx(337.0)


class TestEnsureFactors:
    def test_fresh_cache_skips_fetch(self, monkeypatch):
        """库内因子末日 ≥ 最新行情日 → 用库，不发请求。"""
        dates = ["2026-01-05"]
        conn = FakeConn(rows=[(pd.Timestamp("2026-01-05").date(), 3.0)])
        fetched = []

        def fake_fetch(symbol):
            fetched.append(symbol)
            return factor_series(["2026-01-05"], [3.0])

        got = ensure_factors(
            "postgresql://fake", ["002594"],
            latest_trade_dates={"002594": dt.date(2026, 1, 5)},
            connect=lambda *a, **k: conn, fetch=fake_fetch)
        assert got == {"002594": "ok"}
        assert fetched == []                                 # 缓存新鲜，未拉取

    def test_stale_cache_refetches_and_upserts(self, monkeypatch):
        """库内因子末日 < 最新行情日（新除权发生）→ 重拉并入库。"""
        conn = FakeConn(rows=[(pd.Timestamp("2026-01-01").date(), 1.0)])

        def fake_fetch(symbol):
            return factor_series(["2026-01-01", "2026-01-06"], [1.0, 3.0])

        got = ensure_factors(
            "postgresql://fake", ["002594"],
            latest_trade_dates={"002594": dt.date(2026, 1, 6)},
            connect=lambda *a, **k: conn, fetch=fake_fetch)
        assert got == {"002594": "ok"}
        assert len(conn.upserted) == 2                       # 全史入库

    def test_no_cache_fetches(self, monkeypatch):
        conn = FakeConn(rows=[])                             # 库里没有因子

        def fake_fetch(symbol):
            return factor_series(["2026-01-06"], [3.0])

        got = ensure_factors(
            "postgresql://fake", ["002594"],
            latest_trade_dates={"002594": dt.date(2026, 1, 6)},
            connect=lambda *a, **k: conn, fetch=fake_fetch)
        assert got == {"002594": "ok"}
        assert len(conn.upserted) == 1

    def test_fetch_failure_marks_missing_not_raise(self):
        """新浪拉取失败：标记 missing（调用方降级不复权），绝不抛错。"""
        got = ensure_factors(
            "postgresql://fake", ["002594"],
            latest_trade_dates={"002594": dt.date(2026, 1, 6)},
            connect=lambda *a, **k: FakeConn(rows=[]),
            fetch=lambda symbol: (_ for _ in ()).throw(RuntimeError("sina down")))
        assert got == {"002594": "missing"}


    def test_gap_dates_cover_cache_fresh(self):
        """因子末日 < 最新行情日，但末日之后无新跳空 → 缓存新鲜不重拉。"""
        conn = FakeConn(rows=[(pd.Timestamp("2026-01-01").date(), 3.0)])
        fetched = []

        def fake_fetch(symbol):
            fetched.append(symbol)
            return factor_series(["2026-01-01"], [3.0])

        got = ensure_factors(
            "postgresql://fake", ["002594"],
            latest_trade_dates={"002594": dt.date(2026, 1, 6)},
            gap_dates={"002594": [dt.date(2025, 7, 29)]},
            connect=lambda *a, **k: conn, fetch=fake_fetch)
        assert got == {"002594": "ok"}
        assert fetched == []

    def test_gap_after_cache_stale_refetches(self):
        """因子末日之后出现新跳空（新除权发生）→ 重拉。"""
        conn = FakeConn(rows=[(pd.Timestamp("2026-01-01").date(), 3.0)])
        fetched = []

        def fake_fetch(symbol):
            fetched.append(symbol)
            return factor_series(["2026-01-01"], [3.0])

        got = ensure_factors(
            "postgresql://fake", ["002594"],
            latest_trade_dates={"002594": dt.date(2026, 1, 6)},
            gap_dates={"002594": [dt.date(2026, 1, 5)]},
            connect=lambda *a, **k: conn, fetch=fake_fetch)
        assert got == {"002594": "ok"}
        assert fetched == ["002594"]
