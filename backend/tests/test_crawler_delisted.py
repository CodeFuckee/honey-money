"""退市股补爬测试：名单解析 / JS 解码行转换 / 客户端流程 / store 标记。

数据通道：新浪 klc_kl.js 全史（py_mini_racer + hk_js_decode，akshare 的
stock_zh_a_daily 对退市股挂在第二步「流通股本」接口——本模块绕开它）。
"""

import asyncio
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import delisted as dl  # noqa: E402
from app.backtest_full import filter_universe  # noqa: E402


# ------------------------------------------------------------ 名单解析 ----

def _sh_df():
    return pd.DataFrame({
        "公司代码": ["600001", "600003", "600519"],
        "公司简称": ["邯郸钢铁", "ST东北高", "贵州茅台"],
        "上市日期": ["1998-01-22", "1999-08-10", "2001-08-27"],
        "暂停上市日期": ["2009-12-29", "2010-02-26", None],
    })


def _sz_df():
    return pd.DataFrame({
        "证券代码": ["000003", "000004"],
        "证券简称": ["PT金田Ａ", "国华退"],
        "上市日期": ["1991-01-14", "1990-12-01"],
        "终止上市日期": ["2002-06-14", "2026-07-14"],
    })


class TestParseDelistLists:
    def test_merge_and_market_mapping(self):
        stocks = dl.parse_delist_lists(_sh_df(), _sz_df(), live_codes=set())
        by_code = {s["code"]: s for s in stocks}
        assert by_code["600001"]["market"] == 1            # 沪 → 1
        assert by_code["000003"]["market"] == 0            # 深 → 0
        assert by_code["000003"]["delisted"] is True
        assert by_code["600001"]["name"] == "邯郸钢铁"

    def test_live_codes_excluded(self):
        """暂停上市后恢复上市的股票（仍在市清单）不得标退市。"""
        stocks = dl.parse_delist_lists(_sh_df(), _sz_df(),
                                       live_codes={"600519"})
        codes = {s["code"] for s in stocks}
        assert "600519" not in codes
        assert "600001" in codes and "000003" in codes

    def test_empty_frames(self):
        assert dl.parse_delist_lists(pd.DataFrame(), pd.DataFrame(),
                                     live_codes=set()) == []


# -------------------------------------------------------- 行转换 ----

class TestJsRowsToBars:
    def test_convert_with_prev_close_derivation(self):
        decoded = [
            {"date": "1991-07-03T00:00:00.000Z", "open": 7.1, "high": 7.3,
             "low": 7, "close": 7.1, "volume": 26700},
            {"date": "1991-07-04T00:00:00.000Z", "open": 7.1, "high": 7.4,
             "low": 7.05, "close": 7.2, "volume": 100000},
        ]
        rows = dl.js_rows_to_bars(decoded)
        assert len(rows) == 2
        first, second = rows
        # 12 元组：date/open/close/high/low/volume(手)/amount/振幅/涨跌幅/涨跌额/换手/流通股本
        assert first[0] == "1991-07-03" and first[5] == 267        # 股→手
        assert first[6] is None and first[10] is None and first[11] is None
        assert first[7] is None                                    # 首根无前收
        assert second[8] == pytest.approx((7.2 - 7.1) / 7.1 * 100)  # 涨跌幅
        assert second[9] == pytest.approx(0.1)                      # 涨跌额
        assert second[7] == pytest.approx((7.4 - 7.05) / 7.1 * 100)  # 振幅

    def test_empty_decode(self):
        assert dl.js_rows_to_bars([]) == []


# ------------------------------------------------------------ 客户端 ----

class TestDelistedClient:
    def test_fetch_stock_list_merges_and_excludes_live(self, monkeypatch):
        monkeypatch.setattr(
            dl, "_delist_lists",
            lambda: (_sh_df(), _sz_df()))
        monkeypatch.setattr(
            dl, "_live_codes",
            lambda: {"600519"})
        client = dl.DelistedClient(rate=None, http=None)
        stocks = asyncio.run(client.fetch_stock_list())
        codes = {s["code"] for s in stocks}
        assert {"600001", "000003", "000004"} <= codes
        assert "600519" not in codes
        assert all(s["delisted"] for s in stocks)

    def test_fetch_daily_bars_decodes_and_converts(self, monkeypatch):
        captured = {}

        class FakeHttp:
            async def get(self, url, headers=None, timeout=None):
                captured["url"] = url
                class R:
                    text = 'var KLC_KL_sz000003="HXDUMMY";'

                    def raise_for_status(self):
                        pass

                return R()

        def fake_decode(text):
            assert "HXDUMMY" in text
            return [{"date": "2002-04-26T00:00:00.000Z", "open": 2.71,
                     "high": 2.71, "low": 2.71, "close": 2.71,
                     "volume": 4397205}]

        monkeypatch.setattr(dl, "_decode_kl", fake_decode)

        class FakeRate:
            async def acquire(self, key):
                captured["channel"] = key

            def report(self, key, ok):
                captured["ok"] = ok

        client = dl.DelistedClient(rate=FakeRate(), http=FakeHttp())
        bars = asyncio.run(client.fetch_daily_bars(0, "000003"))
        assert "sz000003" in captured["url"]           # klc_kl.js 按码拼 URL
        assert captured["channel"] == "sina"           # 与主爬虫共用新浪限速通道
        assert captured["ok"] is True
        assert bars.code == "000003" and bars.source == "sina"
        assert len(bars.rows) == 1 and bars.rows[0][0] == "2002-04-26"

    def test_fetch_daily_bars_decode_failure_raises(self, monkeypatch):
        class FakeHttp:
            async def get(self, url, headers=None, timeout=None):
                class R:
                    text = 'var KLC_KL_sz000003="BAD";'

                    def raise_for_status(self):
                        pass

                return R()

        def boom(text):
            raise ValueError("解码失败")

        monkeypatch.setattr(dl, "_decode_kl", boom)

        class FakeRate:
            async def acquire(self, key):
                pass

            def report(self, key, ok):
                self.ok = ok

        rate = FakeRate()
        client = dl.DelistedClient(rate=rate, http=FakeHttp())
        with pytest.raises(dl.DelistedError):
            asyncio.run(client.fetch_daily_bars(0, "000003"))
        assert rate.ok is False                        # 失败计入限速通道


# --------------------------------------------------------- 回测过滤 ----

class TestIncludeDelistedFilter:
    STOCKS = [
        {"code": "600519", "name": "贵州茅台", "delisted": False},
        {"code": "000010", "name": "*ST美丽", "delisted": False},
        {"code": "000003", "name": "PT金田Ａ", "delisted": True},
        {"code": "600001", "name": "邯郸钢铁", "delisted": True},
    ]
    COUNTS = {s["code"]: 500 for s in STOCKS}

    def test_delisted_included_by_default(self):
        kept, _ = filter_universe(self.STOCKS, self.COUNTS,
                                  exclude_st=True, min_bars=60,
                                  include_delisted=True)
        codes = {s["code"] for s in kept}
        assert codes == {"600519", "000003", "600001"}   # 在市 ST 剔，退市留

    def test_delisted_excluded_when_opt_out(self):
        kept, _ = filter_universe(self.STOCKS, self.COUNTS,
                                  exclude_st=True, min_bars=60,
                                  include_delisted=False)
        assert [s["code"] for s in kept] == ["600519"]   # 回到纯在市口径

    def test_exclude_st_keeps_delisted_names_with_tui(self):
        stocks = [s for s in self.STOCKS if s["code"] != "000010"]
        kept, _ = filter_universe(stocks, self.COUNTS,
                                  exclude_st=True, min_bars=60,
                                  include_delisted=True)
        # 退市股名称带退/PT 不被「剔除 ST/退」误杀
        assert {s["code"] for s in kept} == {"600519", "000003", "600001"}
