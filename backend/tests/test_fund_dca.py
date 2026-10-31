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
        return self.conn.fetchone_row


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
    one = kw.pop("one", (None,))     # fetchone 桩：参考日/count/stats/meta 各取一次
    conn = FakeConn(one=one)

    def fake_connect(*a, **k):
        return conn

    orig = dca_mod.psycopg.connect
    dca_mod.psycopg.connect = fake_connect
    try:
        return dca_mod.fetch_dca_list("postgresql://fake", **kw), conn
    finally:
        dca_mod.psycopg.connect = orig


class TestDcaSqlShapes:
    # run() 的执行序：[0]=max(full_end) 参考日，[1]=列表，[2]=count，[3]=stats
    def test_default_sort_and_alive(self):
        """缺省：alive 仅在市谓词 + ORDER BY w10_xirr DESC NULLS LAST。"""
        body, conn = _fetch_dca()
        sql, params = conn.cursor_obj.executed[1]
        assert "ORDER BY w10_xirr DESC NULLS LAST" in sql
        assert "alive = TRUE" in sql
        assert params[-2:] == (50, 0)

    def test_sort_whitelist_rejects_injection(self):
        """sort 非白名单值回落 w10_xirr——非法值不得进入 SQL（防注入）。"""
        body, conn = _fetch_dca(sort="w10_xirr; DROP TABLE fund_dca_result")
        sql, _ = conn.cursor_obj.executed[1]
        assert "DROP TABLE" not in sql
        assert "ORDER BY w10_xirr DESC" in sql

    def test_sort_and_order_mapping(self):
        """sort=full_xirr & order=asc → ORDER BY full_xirr ASC。"""
        body, conn = _fetch_dca(sort="full_xirr", order="asc")
        sql, _ = conn.cursor_obj.executed[1]
        assert "ORDER BY full_xirr ASC NULLS LAST" in sql

    def test_alive_empty_means_no_filter(self):
        """alive=''：全部（含清盘），WHERE 不含 alive 谓词（列名除外）。"""
        body, conn = _fetch_dca(alive="")
        sql, _ = conn.cursor_obj.executed[1]
        assert "alive = TRUE" not in sql and "alive = FALSE" not in sql

    def test_search_params_include_like(self):
        """q 搜索：参数含两侧 % 通配（参数化 LIKE，无内联 %——psycopg3 坑）。"""
        body, conn = _fetch_dca(q="白酒")
        sql, params = conn.cursor_obj.executed[1]
        assert params[0] == "白酒"
        assert params[1] == "%白酒%"

    def test_offset_clamped_and_limit_capped(self):
        """limit>200 钳到 200、offset<0 钳到 0。"""
        body, conn = _fetch_dca(limit=5000, offset=-9)
        _, params = conn.cursor_obj.executed[1]
        assert params[-2:] == (200, 0)

    def test_no_min_age_no_age_clause(self):
        """缺省不限年限：列表 SQL 无年限谓词，参数恰 5 个（q/like/like/limit/offset）。"""
        body, conn = _fetch_dca()
        sql, params = conn.cursor_obj.executed[1]
        assert "full_start <" not in sql
        assert len(params) == 5

    def test_min_age_strictly_greater_and_stats_follows(self):
        """min_age=3：列表与 stats 都加 full_start < 参考日-3年 截止参数（严格大于）。"""
        ref = dt.date(2026, 10, 29)
        body, conn = _fetch_dca(min_age=3, one=(ref,))
        sql, params = conn.cursor_obj.executed[1]
        assert "full_start < %s" in sql
        assert params[3] == ref - dt.timedelta(days=365.25 * 3)  # LIKE 之后、LIMIT 之前
        assert params[-2:] == (50, 0)
        stats_sql, stats_params = conn.cursor_obj.executed[3]
        assert "full_start < %s" in stats_sql
        assert stats_params == [params[3]]

    def test_min_age_zero_means_no_filter(self):
        """min_age=0：等同不限年限（0 即「全部」）。"""
        ref = dt.date(2026, 10, 29)
        body, conn = _fetch_dca(min_age=0, one=(ref,))
        sql, params = conn.cursor_obj.executed[1]
        assert "full_start <" not in sql
        assert len(params) == 5

    def test_min_scale_adds_clause_and_stats_follows(self):
        """min_scale=2：列表与 stats 都加 scale_yi > %s，参数在年龄截止之后。"""
        ref = dt.date(2026, 10, 29)
        body, conn = _fetch_dca(min_age=3, min_scale=2, one=(ref,))
        sql, params = conn.cursor_obj.executed[1]
        assert "scale_yi > %s" in sql
        assert params[3] == ref - dt.timedelta(days=365.25 * 3)   # 年龄截止
        assert params[4] == 2                                     # 规模
        assert params[-2:] == (50, 0)
        stats_sql, stats_params = conn.cursor_obj.executed[3]
        assert "scale_yi > %s" in stats_sql
        assert stats_params == [params[3], 2]

    def test_min_scale_zero_no_clause(self):
        """min_scale=0（默认）：无规模谓词，参数不含规模。"""
        body, conn = _fetch_dca(min_scale=0)
        sql, params = conn.cursor_obj.executed[1]
        assert "scale_yi >" not in sql
        assert len(params) == 5


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


class TestDcaScatter:
    """三维散点 + 年限段 × 规模段分箱：纯函数统计 + 路由降级。"""

    def _rows_and_ref(self):
        ref = dt.date(2026, 10, 29)
        # (scale, first_nav, xirr, rounds)
        rows = [
            (5.0, ref - dt.timedelta(days=365.25 * 11), 0.20, 3),   # ≥10年, 1-10亿
            (0.5, ref - dt.timedelta(days=365.25 * 11), 0.02, 1),   # ≥10年, <1亿
            (2.0, ref - dt.timedelta(days=365.25 * 1), 0.50, 2),    # 0-1年, 1-10亿
            (0.3, ref - dt.timedelta(days=365.25 * 2), -0.10, 0),   # 1-3年, <1亿
            (1.0, ref - dt.timedelta(days=365.25 * 4), 0.05, 1),    # 3-5年, 1-10亿
        ]
        return rows, ref

    def test_each_fund_in_exactly_one_band(self):
        """每基金只计入唯一格子：全矩阵 count 之和 = 基金数。"""
        from app.dca_api import compute_bands
        rows, ref = self._rows_and_ref()
        b = compute_bands(rows, ref)
        total = sum(c["count"] for row in b["matrix"] for c in row)
        assert total == len(rows) == 5
        assert b["age_bands"] == ["0-1 年", "1-3 年", "3-5 年", "5-10 年", "≥10 年"]
        assert b["scale_bands"] == ["<1 亿", "1-10 亿", "10-100 亿", "≥100 亿"]

    def test_band_values(self):
        from app.dca_api import compute_bands
        rows, ref = self._rows_and_ref()
        b = compute_bands(rows, ref)
        # ≥10年 × <1亿：仅 0.5 亿那只
        c = b["matrix"][4][0]
        assert c["count"] == 1 and c["mean_xirr"] == pytest.approx(0.02)
        # 0-1年 × 1-10亿：2 亿新基金，离群 0.50
        c = b["matrix"][0][1]
        assert c["count"] == 1 and c["mean_xirr"] == pytest.approx(0.50)
        # 1-10亿 列合计 = 3 只（5亿/2亿/1亿）
        col = sum(b["matrix"][ai][1]["count"] for ai in range(5))
        assert col == 3
        # 正占比：≥10年 × <1亿 = 100%
        assert b["matrix"][4][0]["positive_share"] == 1.0

    def test_empty_cells_have_none_stats(self):
        from app.dca_api import compute_bands
        rows, ref = self._rows_and_ref()
        b = compute_bands(rows, ref)
        c = b["matrix"][0][3]          # 0-1年 × ≥100亿：空格
        assert c["count"] == 0
        assert c["mean_xirr"] is None and c["positive_share"] is None

    def test_route_down_degrades(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/scatter")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["points"] == []


class TestDcaRounds:
    """每次止盈明细：路由降级 + rounds CSV 解析/导入。"""

    def test_route_down_degrades(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/019387/rounds")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rounds"] == []
        assert body["code"] == "019387"

    def test_list_rows_include_gap_avg(self):
        """列表行携带 gap_avg_days（前端「止盈间隔」列数据源）。"""
        body, conn = _fetch_dca(one=(None,))
        sql, _ = conn.cursor_obj.executed[1]
        assert "gap_avg_days" in sql

    def test_parse_rounds_csv(self, tmp_path):
        from app.dca_store import parse_rounds_csv
        import datetime as dt
        p = tmp_path / "rounds.csv"
        p.write_text(
            "code,seq,tp_date,round_days,gap_days,profit\n"
            "019387,1,2024-02-10,74,74,0.213\n"
            "019387,2,2024-05-20,65,100,0.2001\n"
            "161725,1,2017-03-15,,150,0.2500\n")
        rows = parse_rounds_csv(p)
        assert len(rows) == 3
        assert rows[0] == ("019387", 1, dt.date(2024, 2, 10), 74, 74, 0.213)
        assert rows[2][3] is None and rows[2][2] == dt.date(2017, 3, 15)  # round_days 空串→None

    def test_import_rounds_sequence_and_aggregates(self, tmp_path):
        from app.dca_store import import_rounds, parse_rounds_csv
        p = tmp_path / "rounds.csv"
        p.write_text(
            "code,seq,tp_date,round_days,gap_days,profit\n"
            "019387,1,2024-02-10,74,74,0.213\n"
            "019387,2,2024-05-20,65,100,0.2001\n")
        rows = parse_rounds_csv(p)
        conn = FakeConn()
        import_rounds(conn, rows)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0] == "DELETE FROM fund_dca_rounds"
        assert any("RESET" not in x and "SET gap_avg_days" not in x.upper()
                   or "DELETE" in x for x in sqls) is not None
        # 汇总重置 + 回填两条 UPDATE
        assert sum(1 for sql in sqls if "fund_dca_result" in sql) == 2
        assert len(conn.cursor_obj.batch) == 1            # executemany 一次
        sql, seq_rows = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dca_rounds" in sql and len(seq_rows) == 2


class TestDcaStoreFeeCols:
    def test_parse_csv_fee_columns(self, tmp_path):
        """费用三口径列（*_xirr_disc/nom）按表头导入为浮点。"""
        import csv as _csv
        from app.dca_store import _COLS, parse_csv
        header = [c for c in _COLS if c != "alive"]
        r0 = {c: "" for c in header}
        r0.update({"code": "000001", "name": "测试混合A", "w10_xirr": "0.1",
                   "w10_xirr_disc": "0.09", "w10_xirr_nom": "0.08",
                   "full_start": "2024-01-01", "full_end": "2026-10-30"})
        p = tmp_path / "bt.csv"
        with p.open("w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(header)
            w.writerow([r0[c] for c in header])
        rows = parse_csv(p)
        assert rows[0]["w10_xirr"] == pytest.approx(0.1)
        assert rows[0]["w10_xirr_disc"] == pytest.approx(0.09)
        assert rows[0]["w10_xirr_nom"] == pytest.approx(0.08)

    def test_parse_csv_fee_cols_missing_cell_ok(self, tmp_path):
        """旧 CSV 无费用列 → None，不炸导入。"""
        import csv as _csv
        from app.dca_store import _COLS, parse_csv
        header = ["code", "name", "full_start", "full_end", "w10_xirr"]
        p = tmp_path / "old.csv"
        with p.open("w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(header)
            w.writerow(["000002", "旧格式", "2024-01-01", "2026-10-30", "0.1"])
        rows = parse_csv(p)
        assert rows[0]["w10_xirr_disc"] is None
        assert rows[0]["w10_xirr_nom"] is None


def _fetch_scatter(**kw):
    import app.dca_api as dca_mod
    conn = FakeConn(one=(None,))

    def fake_connect(*a, **k):
        return conn

    orig = dca_mod.psycopg.connect
    dca_mod.psycopg.connect = fake_connect
    try:
        return dca_mod.fetch_dca_scatter("postgresql://fake", **kw), conn
    finally:
        dca_mod.psycopg.connect = orig


class TestDcaScatterFee:
    # run() 的执行序：[0]=max(full_end) 参考日，[1]=散点行
    def test_default_fee_is_disc(self):
        body, conn = _fetch_scatter()
        sql, _ = conn.cursor_obj.executed[1]
        assert "w10_xirr_disc" in sql
        assert body["fee"] == "disc"

    def test_fee_nom_switches_column(self):
        body, conn = _fetch_scatter(fee="nom")
        sql, _ = conn.cursor_obj.executed[1]
        assert "w10_xirr_nom" in sql
        assert "w10_xirr_disc" not in sql
        assert body["fee"] == "nom"

    def test_fee_gross_uses_legacy_column(self):
        body, conn = _fetch_scatter(fee="gross")
        sql, _ = conn.cursor_obj.executed[1]
        assert "w10_xirr IS NOT NULL" in sql
        assert body["fee"] == "gross"

    def test_fee_invalid_falls_back_to_disc(self):
        """非白名单值（含注入串）回落 disc，不得进入 SQL。"""
        body, conn = _fetch_scatter(fee="w10_xirr; DROP TABLE fund_dca_result")
        sql, _ = conn.cursor_obj.executed[1]
        assert "DROP" not in sql
        assert "w10_xirr_disc" in sql
        assert body["fee"] == "disc"
