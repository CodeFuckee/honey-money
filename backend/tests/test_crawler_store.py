"""存储层测试：用假 psycopg 连接/游标验证 SQL 与参数（离线，无真实 PG）。"""

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import store as store_mod  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[str] = []
        self.executemany_calls: list[tuple[str, list]] = []
        self.fetchall_rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)
        if "RETURNING id" in query:
            self.fetchall_rows = [(self.conn.next_run_id,)]

    def executemany(self, query, params_seq):
        self.executemany_calls.append((query, list(params_seq)))

    def fetchone(self):
        return self.fetchall_rows[0] if self.fetchall_rows else None

    def fetchall(self):
        return self.fetchall_rows


class FakeConn:
    def __init__(self):
        self.next_run_id = 7
        self.commits = 0
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_conn(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(store_mod.psycopg, "connect", lambda *a, **kw: conn)
    return conn


class TestSchema:
    def test_init_schema_creates_tables(self, fake_conn):
        store_mod.Store().init_schema(fake_conn)
        ddl = "\n".join(fake_conn.cursor_obj.executed)
        for table in ("stock_basic", "daily_bar", "crawl_run"):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
        assert "CREATE INDEX IF NOT EXISTS" in ddl

    def test_init_schema_adds_task_link_columns(self, fake_conn):
        """调度关联列（CLI 与调度器共用 crawl_run）：不存在则补建。"""
        store_mod.Store().init_schema(fake_conn)
        ddl = "\n".join(fake_conn.cursor_obj.executed)
        assert "ALTER TABLE crawl_run ADD COLUMN IF NOT EXISTS task_id" in ddl
        assert ("ADD COLUMN IF NOT EXISTS trigger_kind" in ddl)


class TestUpsertStockBasic:
    def test_upsert_rows(self, fake_conn):
        stocks = [{"code": "600000", "market": 1, "name": "浦发银行"}]
        store_mod.Store().upsert_stock_basic(fake_conn, stocks)
        sql, params = fake_conn.cursor_obj.executemany_calls[0]
        assert "INSERT INTO stock_basic" in sql
        assert "ON CONFLICT (code) DO UPDATE" in sql
        assert params == [("600000", 1, "浦发银行", None)]   # 缺省 delisted=NULL→FALSE

    def test_upsert_rows_with_delisted_flag(self, fake_conn):
        stocks = [{"code": "000003", "market": 0, "name": "PT金田Ａ",
                   "delisted": True}]
        store_mod.Store().upsert_stock_basic(fake_conn, stocks)
        sql, params = fake_conn.cursor_obj.executemany_calls[0]
        assert "delisted" in sql
        assert params == [("000003", 0, "PT金田Ａ", True)]

    def test_empty_stocks_noop(self, fake_conn):
        store_mod.Store().upsert_stock_basic(fake_conn, [])
        assert fake_conn.cursor_obj.executemany_calls == []


class TestDailyBars:
    ROWS = [("2026-09-29", 9.15, 9.18, 9.25, 9.09, 805097, 739740988.0, 1.75, 0.22, 0.02, 0.24, 3.33e10)]

    def test_upsert_idempotent_conflict_update(self, fake_conn):
        """同 (code, trade_date, source) 冲突时覆盖更新——同源重跑覆盖，异源并存。"""
        n = store_mod.Store().upsert_daily_bars(fake_conn, "600000", self.ROWS, source="sina")
        assert n == 1
        sql, params = fake_conn.cursor_obj.executemany_calls[0]
        assert "INSERT INTO daily_bar" in sql
        assert "ON CONFLICT (code, trade_date, source) DO UPDATE" in sql
        row = params[0]
        assert row[0] == "600000" and row[1] == "2026-09-29"
        assert row[12] == 3.33e10       # outstanding_share（params[12]）
        assert row[13] == "sina"        # source（params[13]）

    def test_empty_rows_returns_zero(self, fake_conn):
        assert store_mod.Store().upsert_daily_bars(fake_conn, "600000", [], source="sina") == 0
        assert fake_conn.cursor_obj.executemany_calls == []

    def test_max_trade_dates_filters_by_source(self, fake_conn):
        """断点按源隔离：新浪的进度不受东财数据影响。"""
        fake_conn.cursor_obj.fetchall_rows = [
            ("600000", dt.date(2026, 9, 29)), ("000001", dt.date(2026, 9, 28)),
        ]
        got = store_mod.Store().max_trade_dates(fake_conn, source="sina")
        assert got == {"600000": dt.date(2026, 9, 29), "000001": dt.date(2026, 9, 28)}
        sql = fake_conn.cursor_obj.executed[-1]
        assert "source" in sql


class TestSchemaMigration:
    def test_legacy_primary_key_recreated(self, fake_conn):
        """旧主键表（无 source）被 DROP 重建为三列主键（无数据阶段安全）。"""
        fake_conn.cursor_obj.fetchall_rows = [("code,trade_date",)]   # 现存主键
        store_mod.Store().init_schema(fake_conn)
        executed = "\n".join(fake_conn.cursor_obj.executed)
        assert "DROP TABLE" in executed
        assert "PRIMARY KEY (code, trade_date, source)" in executed

    def test_new_schema_kept(self, fake_conn):
        """主键已是新版（含 source）：不 DROP，幂等。"""
        fake_conn.cursor_obj.fetchall_rows = [("code,trade_date,source",)]
        store_mod.Store().init_schema(fake_conn)
        executed = "\n".join(fake_conn.cursor_obj.executed)
        assert "DROP TABLE" not in executed


class TestCrawlRun:
    def test_start_run_returns_id(self, fake_conn):
        run_id = store_mod.Store().start_run(fake_conn, mode="incremental")
        assert run_id == 7
        assert "INSERT INTO crawl_run" in fake_conn.cursor_obj.executed[-1]

    def test_finish_run_updates_stats(self, fake_conn):
        store_mod.Store().finish_run(
            fake_conn, 7, total_stocks=10, ok_stocks=9, failed_stocks=1,
            bars_upserted=1234, failures=["600000"],
        )
        sql = fake_conn.cursor_obj.executed[-1]
        assert "UPDATE crawl_run" in sql
        assert "finished_at" in sql
