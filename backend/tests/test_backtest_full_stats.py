"""全量回测统计扩展测试：最好/最差收益 + 退市股单独总结（纯函数，离线）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest_full import compute_stats  # noqa: E402


def _row(ret, *, delisted=False, status="ok", buyhold=None):
    return {"status": status, "total_return": ret, "buyhold_return": buyhold,
            "delisted": delisted}


class TestComputeStatsExtended:
    def test_best_worst_returns(self):
        rows = [_row(0.10), _row(-0.25), _row(0.42), _row(0.05)]
        stats = compute_stats(rows)
        assert stats["best_return"] == 0.42
        assert stats["worst_return"] == -0.25

    def test_delisted_group_summary(self):
        rows = [
            _row(0.10, delisted=False), _row(0.20, delisted=False),
            _row(-0.60, delisted=True), _row(-0.80, delisted=True),
            _row(-0.10, delisted=True),
        ]
        stats = compute_stats(rows)
        d = stats["delisted"]
        assert d["count"] == 3
        assert d["median_return"] == -0.60   # 3 值中位 = 排序后第 2 个
        assert d["best_return"] == -0.10 and d["worst_return"] == -0.80
        assert stats["count"] == 5
        assert stats["median_return"] == -0.10  # 5 值中位 = 排序后第 3 个

    def test_delisted_absent_key_when_none(self):
        """无退市股 → delisted 键为 None（前端显示 '—'）。"""
        stats = compute_stats([_row(0.1), _row(0.2)])
        assert stats["delisted"] is None

    def test_delisted_failed_counted(self):
        """退市股失败行计入 delisted_failed（不参与收益统计）。"""
        rows = [_row(0.1, delisted=False),
                _row(0.2, delisted=True),
                _row(None, delisted=True, status="failed", )]
        stats = compute_stats(rows)
        assert stats["delisted"]["count"] == 1
        assert stats["delisted_failed"] == 1

    def test_legacy_shape_preserved(self):
        """原有四项字段不回归（前端统计卡兼容）。"""
        rows = [_row(0.1, buyhold=0.05), _row(-0.2, buyhold=-0.3)]
        stats = compute_stats(rows)
        assert set(stats) >= {"count", "positive_pct", "median_return",
                              "beat_buyhold_pct", "best_return", "worst_return"}
        assert stats["positive_pct"] == 0.5
        assert stats["beat_buyhold_pct"] == 1.0
