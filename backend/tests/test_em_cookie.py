"""东财 Cookie 模块测试：存取/清洗/验证降级路径（网络请求 mock，离线）。"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import em_cookie  # noqa: E402


class TestStoreLoad:
    def test_save_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "em_cookie.txt")
        em_cookie.save_cookie("  qgqp_id=abc; HAList=a-sh-600519  ", path=path)
        assert em_cookie.load_cookie(path) == "qgqp_id=abc; HAList=a-sh-600519"

    def test_load_missing_returns_empty(self, tmp_path):
        assert em_cookie.load_cookie(str(tmp_path / "nope.txt")) == ""

    def test_clear_removes_file(self, tmp_path):
        path = str(tmp_path / "em_cookie.txt")
        em_cookie.save_cookie("a=1", path=path)
        em_cookie.clear_cookie(path)
        assert em_cookie.load_cookie(path) == ""


class TestVerify:
    def test_empty_cookie_invalid(self):
        r = em_cookie.verify_cookie("")
        assert r["valid"] is False and "为空" in r["reason"]

    def test_valid_response(self, monkeypatch):
        class R:
            status_code = 200
            text = '{"rc":0,"data":{"diff":[{"f12":"600000"}]}}'
            def json(self): return {"rc": 0, "data": {"diff": [{"f12": "x"}]}}
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: R())
        r = em_cookie.verify_cookie("a=1")
        assert r["valid"] is True

    def test_connection_refused_reports_ip_risk(self, monkeypatch):
        def boom(*a, **kw):
            raise httpx.RemoteDisconnected("remote")
        monkeypatch.setattr(httpx, "get", boom)
        r = em_cookie.verify_cookie("a=1")
        assert r["valid"] is False
        assert "风控" in r["reason"]

    def test_bad_payload_invalid(self, monkeypatch):
        class R:
            status_code = 200
            text = "{}"
            def json(self): return {"rc": 3, "data": None}
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: R())
        assert em_cookie.verify_cookie("a=1")["valid"] is False


class TestSanitize:
    """用户粘贴的 Cookie 常带换行/前导空格——HTTP 头非法值必须清洗
    （实测：粘贴值首字符为空格 → httpx LocalProtocolError 拒发）。"""

    def test_leading_space_and_newlines(self):
        dirty = " qgqp_b_id=abc;\n st_nvi=xyz;\n  HAList=a-sh "
        clean = em_cookie.sanitize_cookie(dirty)
        assert clean == "qgqp_b_id=abc; st_nvi=xyz; HAList=a-sh"
        assert "\n" not in clean and not clean.startswith(" ")

    def test_quoted_multiline(self):
        clean = em_cookie.sanitize_cookie('"a=1;\nb=2"')
        assert clean == "a=1; b=2"
