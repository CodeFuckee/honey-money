"""起点条件回测报告测试（fund_dip_validation 结果表存储 + /api/funds/dca/dip-report）。

数据来源：scratch/dip_validation.py 假设验证落库（grp=SYS/NODIP/DIP；
bucket='all' 三组对照 / 深度桶（DIP）/ 起点年份（SYS）；tp ∈ {0.15, 0.20}）。
"""

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


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnOne:
    def __init__(self, one, rows=None):
        self.one = one
        self.rows = rows or []
        self.executed: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class TestDipValidationStore:
    def test_validation_ddl(self):
        from app.dca_store import ensure_tp_schema
        conn = FakeConn()
        ensure_tp_schema(conn)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert any("CREATE TABLE IF NOT EXISTS fund_dip_validation" in s
                   for s in sqls)
        assert any("PRIMARY KEY (grp, bucket, tp)" in s for s in sqls)

    def test_import_dip_validation_replaces_all(self):
        """整表替换（三维度共用一张表，重跑验证后旧数据不得残留）。"""
        from app.dca_store import import_dip_validation
        rows = [("DIP", "25-30", 0.20, 11511, 582, 297, 1084, 0.632,
                 0.32, 0.20, 0.45)]
        conn = FakeConn()
        import_dip_validation(conn, rows)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0] == "DELETE FROM fund_dip_validation"
        assert len(conn.cursor_obj.batch) == 1
        sql, batch = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dip_validation" in sql and len(batch) == 1


def _patch_connect(monkeypatch, module, conn):
    def fake_connect(*a, **k):
        return conn
    monkeypatch.setattr(module.psycopg, "connect", fake_connect)


class TestDipReportRoutes:
    def test_dip_report_down_degrades(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/dip-report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["groups"] == []

    def test_dip_report_shape(self, client, monkeypatch):
        """读全表并按维度组装：groups/buckets/years 三段 + tp 列表回显。"""
        import app.dca_api as dca_mod
        row = ("DIP", "25-30", 0.20, 11511, 582, 297, 1084, 0.632,
               0.32, 0.20, 0.45)
        conn = FakeConnOne(None, rows=[row])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/dip-report")
        body = resp.json()
        assert body["status"] == "up"
        # DIP 行 bucket='25-30'（非 all）→ 按语义进 buckets 段（深度梯度）
        b = body["buckets"][0]
        assert b["grp"] == "DIP" and b["bucket"] == "25-30"
        assert b["tp"] == 0.2 and b["n"] == 11511
        assert b["days_median"] == 582 and b["success_rate"] == 0.632
        assert b["xirr_median"] == 0.32
        assert body["groups"] == [] and body["years"] == []
        assert body["tp_list"] == [0.2]

    def test_dip_report_dimension_split(self, client, monkeypatch):
        """groups=SYS/NODIP（bucket='all'）；buckets=DIP 深度桶；
        years=SYS 起点年份——按 grp+bucket 形态分拣。"""
        import app.dca_api as dca_mod
        rows = [
            ("SYS", "all", 0.20, 247584, 650, 328, 1128, 0.588, 0.286, 0.1, 0.5),
            ("NODIP", "all", 0.20, 131822, 797, 346, 1355, 0.488, 0.226, 0.0, 0.4),
            ("DIP", "25-30", 0.20, 11511, 582, 297, 1084, 0.632, 0.32, 0.2, 0.45),
            ("SYS", "2019", 0.20, 30000, 500, 300, 900, 0.70, 0.35, 0.1, 0.6),
        ]
        conn = FakeConnOne(None, rows=rows)
        _patch_connect(monkeypatch, dca_mod, conn)
        body = client.get("/api/funds/dca/dip-report").json()
        assert [(g["grp"]) for g in body["groups"]] == ["SYS", "NODIP"]
        assert [b["bucket"] for b in body["buckets"]] == ["25-30"]
        assert [y["bucket"] for y in body["years"]] == ["2019"]
