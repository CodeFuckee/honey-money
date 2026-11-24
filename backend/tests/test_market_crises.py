"""金融危机史测试（离线：区间统计纯函数 + 双线对齐 + 路由契约 + 指数日更双指数）。

口径：基线=危机起点日收盘（人工锚定的收盘高点），见底=窗口内最低收盘，
恢复=见底后首个收盘≥基线；涨幅统一 round 4 位。危机区间与事件为人工
知识（scratch/crises_verify.py 已用生产库真实数据核实锚点）。

路由约定同 /api/market/519-924：始终 200，PG 不可达/脏数据在 status=down
表达。指数日更任务改遍历 INDEX_SYMBOLS（上证+沪深300），假件直驱同
test_market_compare 风格，不碰真库真网。
"""

import asyncio
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import index_store  # noqa: E402
from app import market_crises as mcr  # noqa: E402
from app.main import create_app  # noqa: E402
from app.scheduler import _run_index_job  # noqa: E402

DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"
DEAD_POOL = "http://127.0.0.1:59998"


def _bars(base_date: str, closes) -> list[tuple[str, float]]:
    """合成序列：第 i 根日期=base+i 自然日、收盘=closes[i]（自然日串接即可
    精确手算，区间统计只依赖顺序与日期范围）。"""
    c = dt.date.fromisoformat(base_date)
    return [((c + dt.timedelta(days=i)).isoformat(), float(v))
            for i, v in enumerate(closes)]


# ---------- 区间切片 slice_window ----------

class TestSliceWindow:
    def test_inclusive_range_clip(self):
        """[start, end] 闭区间切片：边界日期进窗，窗外剔除。"""
        bars = _bars("2020-01-01", [100, 101, 102, 103, 104])
        got = mcr.slice_window(bars, "2020-01-02", "2020-01-04")
        assert [b[0] for b in got] == ["2020-01-02", "2020-01-03", "2020-01-04"]

    def test_start_end_not_trading_day(self):
        """start/end 为自然日（非交易日）：按范围取首根≥start、末根≤end。"""
        bars = [("2020-01-01", 100.0), ("2020-01-03", 101.0),
                ("2020-01-06", 102.0)]                    # 交易日稀疏
        got = mcr.slice_window(bars, "2020-01-02", "2020-01-04")
        assert [b[0] for b in got] == ["2020-01-03"] and got[0][1] == 101.0

    def test_empty_window_raises(self):
        """窗口无数据（配置区间与数据错位）：明确报错，端点据此降级。"""
        bars = _bars("2020-01-01", [100, 101])
        with pytest.raises(ValueError, match="窗口"):
            mcr.slice_window(bars, "2019-01-01", "2019-06-30")

    def test_start_after_end_raises(self):
        bars = _bars("2020-01-01", [100, 101])
        with pytest.raises(ValueError, match="窗口"):
            mcr.slice_window(bars, "2020-01-03", "2020-01-01")

    def test_input_validation_delegated(self):
        """脏数据校验在切片前统一做：重复/乱序/非正收盘均报错。"""
        ok = _bars("2020-01-01", [100, 101])
        dup = ok + [ok[0]]
        with pytest.raises(ValueError, match="重复"):
            mcr.slice_window(dup, "2020-01-01", "2020-01-05")
        with pytest.raises(ValueError, match="升序"):
            mcr.slice_window(list(reversed(ok)), "2020-01-01", "2020-01-05")
        with pytest.raises(ValueError, match="收盘"):
            mcr.slice_window(_bars("2020-01-01", [100, 0]),
                             "2020-01-01", "2020-01-05")


# ---------- 危机统计 crisis_stats ----------

class TestCrisisStats:
    def test_drop_and_recover_exact_values(self):
        """手算锚：100→95→80→90→105，见底 -20%（第 2 交易日），2 交易日后收复。"""
        window = _bars("2020-01-05", [100, 95, 80, 90, 105])
        s = mcr.crisis_stats(window)
        assert s["base_date"] == "2020-01-05" and s["base"] == 100.0
        assert s["trough_date"] == "2020-01-07" and s["trough"] == 80.0
        assert s["trough_ret"] == -0.2 and s["trough_days"] == 2
        assert s["recover_date"] == "2020-01-09"
        assert s["recover_ret"] == 0.05 and s["recover_days"] == 2

    def test_no_recovery_returns_none(self):
        """单调下跌：见底在最后一根，恢复三字段全 None。"""
        s = mcr.crisis_stats(_bars("2007-10-16", [100, 90, 85, 80]))
        assert s["trough_date"] == "2007-10-19" and s["trough_ret"] == -0.2
        assert s["recover_date"] is None
        assert s["recover_ret"] is None and s["recover_days"] is None

    def test_flat_line_immediate_recovery(self):
        """恒净值：见底=起点（并列取最早），下一交易日即算收复。"""
        s = mcr.crisis_stats(_bars("2020-01-05", [100, 100, 100]))
        assert s["trough_date"] == "2020-01-05" and s["trough_ret"] == 0.0
        assert s["trough_days"] == 0
        assert s["recover_date"] == "2020-01-06" and s["recover_days"] == 1

    def test_trough_at_last_bar_no_recovery(self):
        s = mcr.crisis_stats(_bars("2020-01-05", [100, 90]))
        assert s["trough_date"] == "2020-01-06"
        assert s["recover_date"] is None

    def test_single_point_window(self):
        """窗口仅一根：见底=自身、零涨跌、无恢复。"""
        s = mcr.crisis_stats(_bars("2020-01-05", [100]))
        assert s["base"] == 100.0 and s["trough_ret"] == 0.0
        assert s["trough_days"] == 0 and s["recover_date"] is None

    def test_recovery_equal_base_inclusive(self):
        """恢复口径 ≥：收盘恰好回到基线即算收复（recover_ret=0）。"""
        s = mcr.crisis_stats(_bars("2020-01-05", [100, 90, 100]))
        assert s["recover_date"] == "2020-01-07"
        assert s["recover_ret"] == 0.0 and s["recover_days"] == 1

    def test_ret_rounded_to_4(self):
        """涨幅统一 round 4 位：1/3 跌幅 → -0.6667。"""
        s = mcr.crisis_stats(_bars("2020-01-05", [3, 1]))
        assert s["trough_ret"] == -0.6667

    def test_tie_takes_earliest_trough(self):
        """最低收盘并列：见底日取最早（下跌持续天数口径更严）。"""
        s = mcr.crisis_stats(_bars("2020-01-05", [100, 80, 80, 90]))
        assert s["trough_date"] == "2020-01-06" and s["trough_days"] == 1


# ---------- 双线对齐 crisis_points ----------

class TestCrisisPoints:
    def test_aligned_two_lines(self):
        """同日历双线：起点双双归零，各线相对各自基线的累计涨幅。"""
        main = _bars("2020-01-05", [100, 110, 90])
        sub = _bars("2020-01-05", [2000, 2100, 1800])
        pts = mcr.crisis_points(main, sub)
        assert [p["date"] for p in pts] == ["2020-01-05", "2020-01-06",
                                            "2020-01-07"]
        assert pts[0]["sse"] == 0.0 and pts[0]["hs300"] == 0.0
        assert pts[1]["sse"] == 0.1 and pts[1]["hs300"] == 0.05
        assert pts[2]["sse"] == -0.1 and pts[2]["hs300"] == -0.1

    def test_sub_missing_tail(self):
        """沪深300 序列缺尾部：该日期 hs300=None、sse 照常。"""
        main = _bars("2020-01-05", [100, 110, 90])
        sub = _bars("2020-01-05", [2000, 2100])
        pts = mcr.crisis_points(main, sub)
        assert pts[1]["hs300"] == 0.05
        assert pts[2]["sse"] == -0.1 and pts[2]["hs300"] is None

    def test_sub_empty_all_none(self):
        """沪深300 无数据（1997/2001 危机或表未回填）：hs300 全 None。"""
        main = _bars("2020-01-05", [100, 110])
        pts = mcr.crisis_points(main, [])
        assert len(pts) == 2
        assert all(p["hs300"] is None for p in pts)
        assert [p["sse"] for p in pts] == [0.0, 0.1]

    def test_union_of_dates(self):
        """两线日历有差异：取日期并集，缺席一侧该日 None。"""
        main = _bars("2020-01-05", [100, 110])
        sub = [("2020-01-06", 2000.0), ("2020-01-07", 2100.0)]
        pts = mcr.crisis_points(main, sub)
        assert [p["date"] for p in pts] == ["2020-01-05", "2020-01-06",
                                            "2020-01-07"]
        assert pts[0]["hs300"] is None and pts[0]["sse"] == 0.0
        assert pts[1]["sse"] == 0.1 and pts[1]["hs300"] == 0.0
        assert pts[2]["sse"] is None and pts[2]["hs300"] == 0.05

    def test_ret_rounded_to_4(self):
        main = _bars("2020-01-05", [3, 1])
        sub = _bars("2020-01-05", [3, 2])
        pts = mcr.crisis_points(main, sub)
        assert pts[1]["sse"] == -0.6667 and pts[1]["hs300"] == -0.3333


# ---------- 危机配置 CRISES（人工知识契约） ----------

class TestCrisesConfig:
    def test_seven_crises_shape(self):
        """契约：7 次危机、key 唯一、起点升序、要素齐全、事件带日期与标签。"""
        assert len(mcr.CRISES) == 7
        keys = [c["key"] for c in mcr.CRISES]
        assert len(set(keys)) == 7
        starts = [c["start"] for c in mcr.CRISES]
        assert starts == sorted(starts)
        for c in mcr.CRISES:
            assert c["start"] < c["end"]
            assert c["name"] and c["summary"]
            assert isinstance(c["hs300"], bool)
            assert c["events"], c["key"]
            for e in c["events"]:
                assert e["date"] and e["label"]

    def test_pinned_anchors(self):
        """钉值锚：起止日期与生产库核实的收盘高低点一致（防悄悄改坏）。"""
        pinned = {c["key"]: (c["start"], c["end"], c["hs300"])
                  for c in mcr.CRISES}
        assert pinned["crisis-1997"] == ("1997-05-12", "1999-09-30", False)
        assert pinned["crisis-2001"] == ("2001-06-13", "2007-03-31", False)
        assert pinned["crisis-2008"] == ("2007-10-16", "2009-12-31", True)
        assert pinned["crisis-2015"] == ("2015-06-12", "2018-02-28", True)
        assert pinned["crisis-2018"] == ("2018-01-29", "2021-06-30", True)
        assert pinned["crisis-2020"] == ("2020-01-14", "2020-12-31", True)
        assert pinned["crisis-2021"] == ("2021-09-13", "2025-12-31", True)

    def test_hs300_only_for_post_2005_crises(self):
        """沪深300 基日 2005-04-08：仅 2008 及以后的危机配置双口径。"""
        assert [c["key"] for c in mcr.CRISES if c["hs300"]] == \
            ["crisis-2008", "crisis-2015", "crisis-2018", "crisis-2020",
             "crisis-2021"]


# ---------- 路由 /api/market/crises ----------

class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client():
    return TestClient(create_app())


class TestCrisesEndpoint:
    def test_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 且 crises 空（不允许 5xx）。"""
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/market/crises")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["crises"] == []

    def _patch_bars(self, monkeypatch, main, sub):
        monkeypatch.setattr(mcr, "_connect", lambda: _NullConn())
        monkeypatch.setattr(mcr, "load_bars",
                            lambda conn, sym: main if sym == "sh000001" else sub)

    def test_up_shape_seven_crises(self, client, monkeypatch):
        """正常路径：7 条危机齐全，stats/points/events/hs300_available 结构完整。"""
        main = _bars("1990-01-01", [100] * 13000)            # 覆盖全部窗口
        sub = _bars("2005-01-01", [2000] * 8000)
        self._patch_bars(monkeypatch, main, sub)
        body = client.get("/api/market/crises").json()
        assert body["status"] == "up"
        assert len(body["crises"]) == 7
        by_key = {c["key"]: c for c in body["crises"]}
        for c in body["crises"]:
            assert c["name"] and c["summary"] and c["events"]
            stats = c["stats"]
            assert stats["base"] == 100.0 and stats["trough_ret"] == 0.0
            assert stats["base_date"] and stats["trough_date"]
            assert c["points"][0]["sse"] == 0.0
            assert c["points"][-1]["date"] <= c["end"]
        assert by_key["crisis-1997"]["hs300_available"] is False
        assert by_key["crisis-2001"]["hs300_available"] is False
        assert by_key["crisis-2008"]["hs300_available"] is True
        assert by_key["crisis-2008"]["points"][0]["hs300"] == 0.0
        # 1997/2001 配置单口径：即便 sub 有数据也不画（返回 None）
        assert all(p["hs300"] is None for p in by_key["crisis-1997"]["points"])

    def test_up_with_empty_sub_table(self, client, monkeypatch):
        """沪深300 表空（首次部署未回填）：整页仍 up，双口径项全部降为单线。"""
        main = _bars("1990-01-01", [100] * 13000)
        self._patch_bars(monkeypatch, main, [])
        body = client.get("/api/market/crises").json()
        assert body["status"] == "up"
        assert all(c["hs300_available"] is False for c in body["crises"])
        assert all(p["hs300"] is None
                   for c in body["crises"] for p in c["points"])

    def test_down_when_main_missing(self, client, monkeypatch):
        """上证无数据（主口径缺失）：整页降级。"""
        self._patch_bars(monkeypatch, [], [])
        body = client.get("/api/market/crises").json()
        assert body["status"] == "down" and body["crises"] == []

    def test_down_on_dirty_data(self, client, monkeypatch):
        """脏数据（收盘非正）：明确降级而非 5xx 或带病输出。"""
        main = _bars("1990-01-01", [100] * 100)
        main[50] = (main[50][0], -1.0)
        self._patch_bars(monkeypatch, main, [])
        body = client.get("/api/market/crises").json()
        assert body["status"] == "down" and body["crises"] == []


# ---------- index_store 双指数注册 ----------

class TestIndexSymbols:
    def test_hs300_registered(self):
        """契约：沪深300 挂入 INDEX_SYMBOLS（东财 secid 沪市 000300）。"""
        assert index_store.INDEX_SYMBOLS["sh000300"] == {
            "market": 1, "code": "000300", "name": "沪深300"}
        assert index_store.SYMBOL == "sh000001"     # 519-924 页主口径不变


# ---------- 指数日更任务（双指数遍历） ----------

class _DualCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, q, p=None):
        self.conn.executed.append((q, p))

    def fetchone(self):
        return self.conn.fetchone_for(self.conn.executed[-1])

    def executemany(self, q, seq):
        self.conn.executed.append((q, list(seq)))


class _DualConn:
    """last_date 按 symbol 参数返回不同值的假连接。"""

    def __init__(self, last_by_symbol):
        self.executed = []
        self.last_by_symbol = last_by_symbol

    def fetchone_for(self, executed_call):
        q, p = executed_call
        if "max(trade_date)" in q and p:
            return (self.last_by_symbol.get(p[0]),)
        return (None,)

    def cursor(self):
        return _DualCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeStore:
    def __init__(self):
        self.calls = []

    def start_run(self, conn, **kw):
        self.calls.append(("start", kw))
        return 1

    def finish_run(self, conn, run_id, **kw):
        self.calls.append(("finish", kw))


class _FakeClient:
    def __init__(self, rows_by_code=None, fail_code=None, exc=None):
        self.rows_by_code = rows_by_code or {}
        self.fail_code = fail_code
        self.exc = exc
        self.calls = []

    async def fetch_daily_bars(self, market, code, *, beg="0", end="20500101"):
        self.calls.append({"market": market, "code": code, "beg": beg})
        if self.exc:
            raise self.exc
        if code == self.fail_code:
            raise RuntimeError(f"K线接口 rc=403 code={code}")
        from app.crawler.eastmoney import DailyBars
        rows = self.rows_by_code.get(code, [])
        return DailyBars(code=code, name="x", dktotal=len(rows), rows=rows)

    async def aclose(self):
        pass


def _krow(date_str: str, close: float):
    """kline 12 元组（布局同 eastmoney.parse_kline_row：close 在第 3 位）。"""
    return (date_str, 0.0, close, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None)


def _run_job(fake_client, fake_store, conn):
    task = {"id": 9, "source": "index", "mode": "daily", "name": "指数日更",
            "schedule_time": "17:30", "enabled": True, "_trigger": "manual"}
    stop_event = type("E", (), {"is_set": lambda self: False})()
    progress_log = []

    def progress(done, total):
        progress_log.append((done, total))

    summary = asyncio.run(_run_index_job(
        DEAD_DSN, DEAD_POOL, task, progress=progress,
        stop_event=stop_event, client=fake_client, store=fake_store,
        connect=lambda d: conn))
    return summary, progress_log


class TestIndexJobDual:
    def test_fetches_both_symbols_with_own_beg(self):
        """双指数各拉一次、各按自己的 last_date 增量：上证 beg=昨日-10、
        沪深300 空表 beg=0 全史；bars 合计两指数。"""
        conn = _DualConn({"sh000001": dt.date(2026, 11, 20),
                          "sh000300": None})
        client = _FakeClient(rows_by_code={
            "000001": [_krow("2026-11-19", 3800.0), _krow("2026-11-20", 3810.0)],
            "000300": [_krow("2005-04-08", 1000.0)]})
        summary, _ = _run_job(client, _FakeStore(), conn)
        assert summary["aborted"] is False
        assert [c["code"] for c in client.calls] == ["000001", "000300"]
        assert client.calls[0]["beg"] == "20261110"
        assert client.calls[1]["beg"] == "0"
        assert summary["bars"] == 3
        inserts = [q for q, _ in conn.executed
                   if q.lstrip().startswith("INSERT INTO index_daily")]
        assert len(inserts) == 2

    def test_second_symbol_failure_keeps_first_and_records(self):
        """沪深300 拉取失败：上证增量已入库不回滚，failures 记录、aborted=True，
        run 正常收尾不抛出。"""
        conn = _DualConn({"sh000001": dt.date(2026, 11, 20),
                          "sh000300": None})
        client = _FakeClient(rows_by_code={
            "000001": [_krow("2026-11-20", 3810.0)]}, fail_code="000300")
        summary, _ = _run_job(client, _FakeStore(), conn)
        assert summary["aborted"] is True
        assert summary["bars"] == 1
        assert summary["failures"] and "000300" in summary["failures"][0]
        assert any(q.lstrip().startswith("INSERT INTO index_daily")
                   for q, _ in conn.executed)

    def test_progress_covers_all_symbols(self):
        """progress 总数=指数数：0/N 起步，逐指数推进到 N/N。"""
        conn = _DualConn({"sh000001": None, "sh000300": None})
        summary, log = _run_job(_FakeClient(), _FakeStore(), conn)
        assert log == [(0, 2), (1, 2), (2, 2)]
