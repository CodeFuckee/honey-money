"""基金定投止盈回测结果接口测试（离线：降级形状 + SQL 形状 + 导入器）。

回测结果是一次性离线产物（scripts/fund_dca_backtest.py 生成 CSV）导入
fund_dca_result 快照表；SQL 正确性由真实数据冒烟（NAS）覆盖，此处验证
路由契约、降级形状、参数白名单与导入器行为——与 test_browse 同款约定。
"""

import csv
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"


class TestDcaRoutes:
    def test_list_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 且 rows/total 空形状（不允许 5xx）。"""
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["rows"] == [] and body["total"] == 0

    def test_param_clamping(self, client):
        """limit/offset 越界值被钳制（limit≤200、offset≥0），不抛错。"""
        resp = client.get("/api/funds/dca", params={"limit": 99999, "offset": -5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 200
        assert body["offset"] == 0

    def test_shape_has_stats_and_meta_keys(self, client):
        """响应固定带 stats/meta 键（未导入/不可达时可为 None），前端可空态渲染。"""
        resp = client.get("/api/funds/dca")
        assert resp.status_code == 200
        body = resp.json()
        assert "stats" in body and "meta" in body and "rows" in body


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list = []
        self.batch: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def executemany(self, query, seq):
        self.batch.append((query, list(seq)))

    def fetchall(self):
        return self.conn.fetchall_rows

    def fetchone(self):
        return None


class FakeConn:
    """捕获 SQL 与参数；fetchall/fetchone 可配置。"""

    def __init__(self, rows=None, one=None):
        self.fetchall_rows = rows or []
        self.fetchone_row = one
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fetch_dca(**kw):
    import app.dca_api as dca_mod
    conn = FakeConn()

    def fake_connect(*a, **k):
        return conn

    orig = dca_mod.psycopg.connect
    dca_mod.psycopg.connect = fake_connect
    try:
        return dca_mod.fetch_dca_list("postgresql://fake", **kw), conn
    finally:
        dca_mod.psycopg.connect = orig


class TestDcaSqlShapes:
    def test_default_sort_and_alive(self):
        """缺省：alive 仅在市谓词 + ORDER BY w10_xirr DESC NULLS LAST。"""
        body, conn = _fetch_dca()
        sql, params = conn.cursor_obj.executed[0]
        assert "ORDER BY w10_xirr DESC NULLS LAST" in sql
        assert "alive" in sql
        assert params[-2:] == (50, 0)

    def test_sort_whitelist_rejects_injection(self):
        """sort 非白名单值回落 w10_xirr——非法值不得进入 SQL（防注入）。"""
        body, conn = _fetch_dca(sort="w10_xirr; DROP TABLE fund_dca_result")
        sql, _ = conn.cursor_obj.executed[0]
        assert "DROP TABLE" not in sql
        assert "ORDER BY w10_xirr DESC" in sql

    def test_sort_and_order_mapping(self):
        """sort=full_xirr & order=asc → ORDER BY full_xirr ASC。"""
        body, conn = _fetch_dca(sort="full_xirr", order="asc")
        sql, _ = conn.cursor_obj.executed[0]
        assert "ORDER BY full_xirr ASC NULLS LAST" in sql

    def test_alive_empty_means_no_filter(self):
        """alive=''：全部（含清盘），WHERE 不含 alive 谓词（列名除外）。"""
        body, conn = _fetch_dca(alive="")
        sql, _ = conn.cursor_obj.executed[0]
        assert "alive = TRUE" not in sql and "alive = FALSE" not in sql

    def test_search_params_include_like(self):
        """q 搜索：参数含两侧 % 通配（参数化 LIKE，无内联 %——psycopg3 坑）。"""
        body, conn = _fetch_dca(q="白酒")
        sql, params = conn.cursor_obj.executed[0]
        assert params[0] == "白酒"
        assert params[1] == "%白酒%"

    def test_offset_clamped_and_limit_capped(self):
        """limit>200 钳到 200、offset<0 钳到 0。"""
        body, conn = _fetch_dca(limit=5000, offset=-9)
        _, params = conn.cursor_obj.executed[0]
        assert params[-2:] == (200, 0)


class TestDcaImporter:
    CSV_ROWS = [
        ["code", "name", "fund_type", "delisted", "scale_yi", "scale_date",
         "full_start", "full_end", "full_months", "full_rounds",
         "full_invested", "full_final_value", "full_profit_rate", "full_xirr",
         "w10_start", "w10_end", "w10_months", "w10_rounds",
         "w10_invested", "w10_final_value", "w10_profit_rate", "w10_xirr"],
        ["510300", "沪深300ETF华泰柏瑞", "指数型-股票", "False", "1069.0",
         "2026-09-30", "2012-05-04", "2026-10-28", "120", "3",
         "120000", "141600", "0.18", "0.107",
         "2016-10-31", "2026-10-28", "121", "3",
         "121000", "143390", "0.185", "0.107"],
        ["161725", "招商中证白酒指数(LOF)A", "指数型-股票", "False", "209.0",
         "2026-09-30", "2015-05-27", "2026-10-28", "138", "7",
         "138000", "117300", "-0.15", "-0.133",
         "2016-10-31", "2026-10-28", "121", "7",
         "121000", "102850", "-0.15", "-0.147"],
        ["000004", "太老无净值", "混合型-偏股", "True", "2.0",
         "2019-12-31", "", "", "", "", "", "", "", "",
         "", "", "", "", "", "", "", ""],
    ]

    def _write_csv(self, tmp_path):
        p = tmp_path / "dca.csv"
        with p.open("w", newline="") as f:
            csv.writer(f).writerows(self.CSV_ROWS)
        return p

    def test_parse_rows_types_and_nan_to_none(self, tmp_path):
        """解析：数值/日期转目标类型，空串→None；alive 按净值新鲜度计算。"""
        from app.dca_store import parse_csv
        p = self._write_csv(tmp_path)
        rows = parse_csv(p)
        assert len(rows) == 3
        by = {r["code"]: r for r in rows}
        new = by["510300"]
        assert new["alive"] is True           # 净值日接近最新
        assert new["w10_xirr"] == pytest.approx(0.107)
        assert new["w10_rounds"] == 3
        assert new["full_start"] == dt.date(2012, 5, 4)
        old = by["000004"]
        assert old["alive"] is False          # 2019 年后无净值
        assert old["w10_xirr"] is None        # 空串→None
        assert old["full_months"] is None

    def test_import_rows_sql_sequence(self, tmp_path):
        """导入：DELETE 全表 → executemany 插入 → meta upsert（单事务由调用方提交）。"""
        from app.dca_store import import_rows, parse_csv
        p = self._write_csv(tmp_path)
        rows = parse_csv(p)
        conn = FakeConn()
        import_rows(conn, rows, params={"amount": 1000})
        kinds = [sql.split()[0] for sql, _ in conn.cursor_obj.executed]
        assert kinds[0] == "DELETE"
        assert kinds[-1][0] != "DELETE"
        # 最后一条 executed 是 meta upsert（参数：fund_count, params json）
        last_sql, last_params = conn.cursor_obj.executed[-1]
        assert "fund_dca_meta" in last_sql
        assert last_params[0] == 3            # fund_count
        assert "amount" in last_params[1]
        # executemany 恰一次，3 行
        assert len(conn.cursor_obj.batch) == 1
        sql, seq = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dca_result" in sql
        assert len(seq) == 3

    def test_import_is_idempotent(self, tmp_path):
        """重复导入同一 CSV：SQL 序列一致（DELETE+INSERT 幂等，不叠加）。"""
        from app.dca_store import import_rows, parse_csv
        p = self._write_csv(tmp_path)
        rows = parse_csv(p)
        seqs = []
        for _ in range(2):
            conn = FakeConn()
            import_rows(conn, rows, params={"amount": 1000})
            seqs.append((len(conn.cursor_obj.executed),
                         len(conn.cursor_obj.batch[0][1])))
        assert seqs[0] == seqs[1]
