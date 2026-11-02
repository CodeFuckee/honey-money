"""Pages 自动发布链路单测：导出编排（fake 连接）+ git 发布流程（本地裸仓）。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pages_publish  # noqa: E402
from app.pages_export import export_all  # noqa: E402


@pytest.fixture
def fake_dsn(monkeypatch):
    """export_list/scatter/rounds 全部走内存假数据（不连真库）。"""
    import app.pages_export as pe

    class _Cur:
        rows = [
            ("000001", "gross", 1, None, 10, 10, 0.21, 1.2, 1000.0, 0.0, 0.0, 210.0),
            ("000001", "nom", 1, None, 10, 10, 0.21, 1.2, 1000.0, 1.5, 3.2, 205.3),
        ]

        def execute(self, *_a, **_k):
            return self

        def __iter__(self):
            return iter(self.rows)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(pe.psycopg, "connect", lambda *a, **k: _Conn())
    return "postgresql://fake"


def _fake_list(dsn, *, limit=0, offset=0):
    if offset:
        return {"status": "up", "total": 1, "rows": [], "stats": None,
                "meta": {"generated_at": "2026-11-01T00:00:00+00:00"}}
    return {"status": "up", "total": 1, "limit": limit, "offset": offset,
            "rows": [{"code": "000001", "name": "测试", "alive": True,
                      "w10_xirr": 0.2}],
            "stats": {"count": 1, "p25": 0.2, "median": 0.2, "p75": 0.2,
                      "p90": 0.2, "positive_share": 1.0,
                      "take_profit_share": 1.0},
            "meta": {"generated_at": "2026-11-01T00:00:00+00:00"}}


def _fake_scatter(dsn, *, fee="disc"):
    return {"status": "up", "fee": fee, "points": [[1.0, 1.0, 0.2, "000001"]],
            "age_bands": [], "scale_bands": [], "matrix": []}


class TestExportAll:
    def test_export_shapes(self, tmp_path, monkeypatch, fake_dsn):
        import app.pages_export as pe
        monkeypatch.setattr(pe, "fetch_dca_list", _fake_list)
        monkeypatch.setattr(pe, "fetch_dca_scatter", _fake_scatter)
        stats = export_all(fake_dsn, tmp_path, log=lambda *a: None)
        assert stats["rows"] == 1
        dca = json.loads((tmp_path / "funds" / "dca.json").read_text())
        assert dca["total"] == 1 and dca["rows"][0]["code"] == "000001"
        for fee in ("gross", "disc", "nom"):
            body = json.loads(
                (tmp_path / "funds" / "dca" / f"scatter.{fee}.json").read_text())
            assert body["fee"] == fee
        rounds = json.loads(
            (tmp_path / "funds" / "dca" / "rounds" / "000001.json").read_text())
        assert set(rounds["rounds"]) == {"gross", "nom"}
        assert rounds["totals"]["nom"]["rounds"] == 1


class TestPublish:
    def _git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, check=True).stdout

    def test_publish_commit_and_push(self, tmp_path, monkeypatch, fake_dsn):
        import app.pages_publish as pp
        monkeypatch.setattr(pp, "export_all", _fake_list and export_all)
        monkeypatch.setattr("app.pages_export.fetch_dca_list", _fake_list)
        monkeypatch.setattr("app.pages_export.fetch_dca_scatter", _fake_scatter)
        repo_url = f"file://{tmp_path / 'origin.git'}"

        # 本地裸仓当 origin
        origin = tmp_path / "origin.git"
        origin.mkdir()
        self._git(origin, "init", "--bare", "--initial-branch=main")
        # 种子 main 分支（bare 仓无工作树，用临时 worktree 提交一个初始提交）
        seed = tmp_path / "seed"
        seed.mkdir()
        self._git(seed, "init", "--initial-branch=main")
        (seed / "README.md").write_text("seed")
        self._git(seed, "add", ".")
        self._git(seed, "-c", "user.name=t", "-c", "user.email=t@t",
                  "commit", "-m", "seed")
        self._git(seed, "push", str(origin), "main")

        work = tmp_path / "work"
        pub = pages_publish.publish(fake_dsn, repo_url, work,
                                    log=lambda *a: None)
        assert pub["changed"] and pub["pushed"]
        # 远端确实收到提交
        self._git(work, "fetch", "origin", "main")
        log = self._git(work, "log", "origin/main", "-1", "--format=%s")
        assert "快照日更" in log
        assert (work / "site-data" / "funds" / "dca.json").exists()

        # 第二次发布：数据无变化 → 不产生新提交
        pub2 = pages_publish.publish(fake_dsn, repo_url, work,
                                     log=lambda *a: None)
        assert not pub2["changed"] and not pub2["pushed"]

    def test_safe_url_masks_token(self):
        assert pages_publish._safe_url(
            "https://x-access-token:ghp_secret@github.com/a/b.git") \
            == "https://***@github.com/a/b.git"
        assert pages_publish._safe_url("https://github.com/a/b.git") \
            == "https://github.com/a/b.git"
