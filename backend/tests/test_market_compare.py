"""519 vs 924 行情对比测试（离线：对齐纯函数 + 路由契约 + 指数日更任务）。

对齐口径：T 日收盘为基线，r(n)=close(T+n)/close(T)-1，n 为各自序列内
交易日序偏移；两线窗口 T-280 ~ T+1040 固定复刻中信研报图（原图为
wind全A，本项目口径为上证指数同源，页面注明）。对齐函数收两条独立
序列（端点侧从同一连续序列按 T 定位切片），窗口越界处该线为 None。

路由约定同 /api/funds/dca：始终 200，PG 不可达在 status=down 表达。
指数日更任务用假件直驱 _run_index_job（client/store/connect 全注入，
同 test_tasks_api 假连接风格，不碰真库真网）。
"""

import asyncio
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import index_store  # noqa: E402
from app import market_compare as mc  # noqa: E402
from app.main import create_app  # noqa: E402
from app.scheduler import _run_index_job  # noqa: E402

DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"
DEAD_POOL = "http://127.0.0.1:59998"
T1, T2 = "1999-05-19", "2024-09-24"
G = 0.01                                     # 每日固定涨幅，r(n)=(1+G)^n-1


def _series(t_day: str, n_back: int, n_fwd: int, base=1000.0, g=G):
    """合成 T±n 序列：第 n 偏移日期=T±n 自然日、收盘=base*(1+g)^n。

    对齐只依赖顺序与 T 定位、不检验真实交易日历——自然日串接即可精确手算。
    """
    c = dt.date.fromisoformat(t_day)
    return [((c + dt.timedelta(days=n)).isoformat(), base * (1 + g) ** n)
            for n in range(-n_back, n_fwd + 1)]


def _r(n: int, g=G) -> float:
    """未取整期望；实现侧 r 统一 round 4 位，断言一律用 _r4。"""
    return (1 + g) ** n - 1


def _r4(n: int, g=G) -> float:
    return round(_r(n, g), 4)


# ---------- 对齐纯函数 align_lines ----------

class TestAlignLines:
    def test_normal_overlap_exact_returns(self):
        """两线窗口重叠：r 为相对各自 T 收盘的累计涨幅，n=0 双线归零。"""
        points = mc.align_lines(_series(T1, 2, 2), _series(T2, 2, 2),
                                T1, T2, back=2, fwd=2)
        assert [p["n"] for p in points] == [-2, -1, 0, 1, 2]
        p0 = points[2]
        assert p0["r1"] == p0["r2"] == 0.0
        assert p0["d1"] == T1 and p0["d2"] == T2
        assert points[0]["r1"] == pytest.approx(_r4(-2), abs=1e-9)
        assert points[0]["r2"] == pytest.approx(_r4(-2), abs=1e-9)
        assert points[-1]["r2"] == pytest.approx(_r4(2), abs=1e-9)

    def test_line2_shorter_tail_null(self):
        """924 线短于窗口右端：尾段 r2/d2 为 None，519 线照常延展。"""
        points = mc.align_lines(_series(T1, 1, 4), _series(T2, 1, 1),
                                T1, T2, back=1, fwd=4)
        assert points[-1]["r1"] is not None
        assert points[-1]["r2"] is None and points[-1]["d2"] is None
        assert points[2]["r2"] is not None           # n=1 处 924 线仍在

    def test_window_left_clip(self):
        """某线 T 前数据不足 back：其头部 r 为 None，n 轴仍从另一线窗口起。"""
        points = mc.align_lines(_series(T1, 2, 2), _series(T2, 0, 2),
                                T1, T2, back=2, fwd=2)
        assert points[0]["n"] == -2
        assert points[0]["r1"] == pytest.approx(_r4(-2), abs=1e-9)
        assert points[0]["r2"] is None and points[1]["r2"] is None
        assert points[2]["r2"] == 0.0

    def test_fwd_cap(self):
        """窗口右端超过 fwd：n 截断到 T+fwd。"""
        points = mc.align_lines(_series(T1, 1, 6), _series(T2, 1, 6),
                                T1, T2, back=1, fwd=3)
        assert [p["n"] for p in points] == [-1, 0, 1, 2, 3]

    def test_t1_missing_raises(self):
        """T 日不在序列（停牌/日历口径错）：明确报错，端点据此降级。"""
        with pytest.raises(ValueError, match=T1):
            mc.align_lines(_series("1999-05-25", 2, 2), _series(T2, 2, 2),
                           T1, T2, back=2, fwd=2)

    def test_t2_missing_raises(self):
        """T2 缺失：无法出图（T1 单独存在不算正常路径）。"""
        with pytest.raises(ValueError, match=T2):
            mc.align_lines(_series(T1, 1, 1),
                           [( "2024-09-25", 100.0)], T1, T2, back=1, fwd=1)

    def test_empty_series_raises(self):
        with pytest.raises(ValueError, match="空"):
            mc.align_lines([], _series(T2, 1, 1), T1, T2, back=1, fwd=1)

    def test_duplicate_dates_raises(self):
        bars = _series(T1, 1, 1)
        bars.append(bars[0])
        with pytest.raises(ValueError, match="重复"):
            mc.align_lines(bars, _series(T2, 1, 1), T1, T2, back=1, fwd=1)

    def test_unsorted_dates_raises(self):
        bars = list(reversed(_series(T1, 2, 2)))
        with pytest.raises(ValueError, match="升序"):
            mc.align_lines(bars, _series(T2, 2, 2), T1, T2, back=2, fwd=2)

    def test_bad_close_raises(self):
        bars = _series(T1, 1, 1)
        bars[0] = (bars[0][0], 0.0)                  # 非正收盘=脏数据
        with pytest.raises(ValueError, match="收盘"):
            mc.align_lines(bars, _series(T2, 1, 1), T1, T2, back=1, fwd=1)

    def test_constants_match_citic_chart(self):
        """契约：T 日与窗口和研报图一致（防常量被悄悄改坏）。"""
        assert (mc.T1_DATE, mc.T2_DATE, mc.BACK_N, mc.FWD_N) == \
            (T1, T2, 280, 1040)


# ---------- 路由 /api/market/519-924 ----------

class _NullConn:
    """连接假件：with 语义成立，游标不会被用到（load_bars 已被替换）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client():
    return TestClient(create_app())


class TestMarketEndpoint:
    def test_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 且 points 空形状（不允许 5xx）。"""
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/market/519-924")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["points"] == []

    def test_up_shape_and_progress(self, client, monkeypatch):
        """正常路径：meta 齐全、points 覆盖窗口、t2_progress 为 924 线最新点。"""
        bars = _series(T1, 280, 1040) + _series(T2, 280, 500)
        monkeypatch.setattr(mc, "_connect", lambda: _NullConn())
        monkeypatch.setattr(mc, "load_bars", lambda conn, sym: bars)
        resp = client.get("/api/market/519-924")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        assert body["t1"]["date"] == T1 and body["t2"]["date"] == T2
        assert body["back"] == 280 and body["fwd"] == 1040
        assert body["points"][0]["n"] == -280
        assert body["points"][-1]["n"] == 1040
        # 924 线只到 T+500：末段 r2 为 None；t2_progress 指向其最后有效点
        assert body["points"][-1]["r2"] is None
        assert body["t2_progress"]["n"] == 500
        assert body["t2_progress"]["date"] == bars[-1][0]
        assert body["t2_progress"]["ret"] == pytest.approx(_r4(500), abs=1e-9)

    def test_down_when_symbol_data_missing(self, client, monkeypatch):
        """表在但无数据（首次部署未日更）：降级而非 5xx。"""
        monkeypatch.setattr(mc, "_connect", lambda: _NullConn())
        monkeypatch.setattr(mc, "load_bars", lambda conn, sym: [])
        body = client.get("/api/market/519-924").json()
        assert body["status"] == "down"
        assert body["points"] == []


# ---------- 指数日更任务 _run_index_job ----------

class _JobCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, q, p=None):
        self.conn.executed.append((q, p))

    def fetchone(self):
        return self.conn.fetchone_row

    def executemany(self, q, seq):
        self.conn.executed.append((q, list(seq)))


class _JobConn:
    def __init__(self, last_date_row=(None,)):
        self.executed = []
        self.fetchone_row = last_date_row

    def cursor(self):
        return _JobCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
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
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.calls = (rows or []), exc, []

    async def fetch_daily_bars(self, market, code, *, beg="0", end="20500101"):
        self.calls.append({"market": market, "code": code, "beg": beg})
        if self.exc:
            raise self.exc
        from app.crawler.eastmoney import DailyBars
        return DailyBars(code=code, name="上证指数", dktotal=len(self.rows),
                         rows=self.rows)

    async def aclose(self):
        pass


def _krow(date_str: str, close: float):
    """kline 12 元组（布局同 eastmoney.parse_kline_row：close 在第 3 位）。"""
    return (date_str, 0.0, close, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None)


def _run_job(fake_client, fake_store, last_date_row=(None,)):
    """公共驱动：client/store/connect 全注入，asyncio 跑完返回 (summary, conn)。"""
    conn = _JobConn(last_date_row=last_date_row)
    task = {"id": 9, "source": "index", "mode": "daily", "name": "指数日更",
            "schedule_time": "17:30", "enabled": True, "_trigger": "manual"}
    stop_event = type("E", (), {"is_set": lambda self: False})()
    summary = asyncio.run(_run_index_job(
        DEAD_DSN, DEAD_POOL, task, progress=lambda done, total: None,
        stop_event=stop_event, client=fake_client, store=fake_store,
        connect=lambda d: conn))
    return summary, conn


class TestIndexJob:
    def test_incremental_beg_from_last_date(self):
        """有历史：增量 beg=最近日期-10 自然日（重叠容错，YYYYMMDD）。"""
        client = _FakeClient(rows=[_krow("2026-11-19", 3800.0),
                                   _krow("2026-11-20", 3810.0)])
        summary, conn = _run_job(client, _FakeStore(),
                                 last_date_row=(dt.date(2026, 11, 20),))
        assert summary["aborted"] is False and summary["bars"] == 2
        assert client.calls[0]["beg"] == "20261110"
        assert any(q.lstrip().startswith("INSERT INTO index_daily")
                   for q, _ in conn.executed)

    def test_empty_table_fetches_full_history(self):
        """空表（首次部署）：beg=0 拉全史。"""
        client = _FakeClient(rows=[])
        summary, _ = _run_job(client, _FakeStore())
        assert summary["aborted"] is False
        assert client.calls[0]["beg"] == "0"

    def test_fetch_failure_records_and_aborts(self):
        """接口失败：failures 记录、aborted=True，run 正常收尾不抛出。"""
        client = _FakeClient(exc=RuntimeError("K线接口 rc=403"))
        summary, _ = _run_job(client, _FakeStore())
        assert summary["aborted"] is True
        assert summary["failures"] and "rc=403" in summary["failures"][0]

    def test_stop_event_before_fetch(self):
        """stop_event 已置位：不发起请求，aborted=True 正常收尾。"""
        client = _FakeClient(rows=[])
        store = _FakeStore()
        task = {"id": 9, "source": "index", "mode": "daily", "name": "指数日更",
                "_trigger": "manual"}
        stop_event = type("E", (), {"is_set": lambda self: True})()
        summary = asyncio.run(_run_index_job(
            DEAD_DSN, DEAD_POOL, task, progress=lambda done, total: None,
            stop_event=stop_event, client=client, store=store,
            connect=lambda d: _JobConn()))
        assert summary["aborted"] is True and client.calls == []


# ---------- index_store（建表/写入/读取的 SQL 契约） ----------

class _StoreConn:
    """记录型假连接：execute 记录，fetchone/fetchall 可配置。"""

    def __init__(self, fetchone_row=None, fetchall_rows=None):
        self.executed = []
        self.fetchone_row = fetchone_row
        self.fetchall_rows = fetchall_rows or []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, q, p=None):
        self.executed.append((q, p))

    def executemany(self, q, seq):
        self.executed.append((q, list(seq)))

    def fetchone(self):
        return self.fetchone_row

    def fetchall(self):
        return self.fetchall_rows


class TestIndexStore:
    def test_ensure_schema_idempotent(self):
        conn = _StoreConn()
        index_store.ensure_schema(conn)
        index_store.ensure_schema(conn)
        assert all(q.lstrip().startswith("CREATE TABLE IF NOT EXISTS index_daily")
                   for q, _ in conn.executed)

    def test_upsert_sql_shape_and_rows(self):
        conn = _StoreConn()
        rows = [_krow("2026-11-19", 3800.0), _krow("2026-11-20", 3810.0)]
        index_store.upsert_bars(conn, "sh000001", rows)
        assert len(conn.executed) == 1
        q, params = conn.executed[0]
        assert "INSERT INTO index_daily" in q
        assert "ON CONFLICT (symbol, trade_date)" in q and "DO UPDATE" in q
        assert len(params) == 2
        assert params[0][0] == "sh000001" and params[0][1] == "2026-11-19"
        assert float(params[0][5]) == 3800.0         # close 在参数第 6 位

    def test_last_date_none_and_value(self):
        assert index_store.last_date(_StoreConn(fetchone_row=(None,)),
                                     "sh000001") is None
        got = index_store.last_date(
            _StoreConn(fetchone_row=(dt.date(2026, 11, 20),)), "sh000001")
        assert got == dt.date(2026, 11, 20)

    def test_fetch_closes_maps_to_float_and_orders(self):
        conn = _StoreConn(fetchall_rows=[("2026-11-19", Decimal("3800.5")),
                                         ("2026-11-20", Decimal("3810"))])
        bars = index_store.fetch_closes(conn, "sh000001")
        assert bars == [("2026-11-19", 3800.5), ("2026-11-20", 3810.0)]
        q, _ = conn.executed[0]
        assert "ORDER BY trade_date" in q

    def test_fetch_closes_empty(self):
        assert index_store.fetch_closes(_StoreConn(), "sh000001") == []
