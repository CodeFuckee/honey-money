"""金融危机史：历史上数次金融危机期间 A 股的表现（数据页子页数据源）。

口径：上证指数收盘为主口径（index_daily，指数日更任务写入），2005 年
沪深300 发布后的危机叠加双口径；基线=危机起点日收盘（人工锚定的收盘
高点），见底=窗口内最低收盘，恢复=见底后首个收盘≥基线。危机区间与
关键事件为人工知识（scratch/crises_verify.py 已按生产库真实数据核实
锚点：1997-05-12 1500.39 / 2001-06-13 2242.42 / 2007-10-16 6092.06 /
2015-06-12 5166.35 / 2018-01-29 3523.00 / 2020-01-14 3106.82 /
2021-09-13 3715.37，见底与恢复日同步核实）。

端点约定同 /api/market/519-924：始终 200，PG 不可达/无数据/脏数据在
status=down 表达；沪深300 表空不降级（各项 hs300_available=False）。
"""

import psycopg

from . import config
from .index_store import fetch_closes

SYMBOL_MAIN = "sh000001"
SYMBOL_SUB = "sh000300"
SUB_NOTE = "沪深300 基日 2004-12-31、2005-04-08 发布，此前危机仅上证口径"

# 七次危机（2026-11-24 人工知识整理，锚点以 index_daily 收盘数据核实）。
# start=危机起点（收盘高点锚，KPI 基线）、end=观察窗口末（含恢复段或反弹
# 段）；hs300=该危机全程是否有沪深300 数据；events 为关键事件标注
# （date 用 ISO 日期，可早于 start——前端图表按可见范围裁剪、时间线全列）。
CRISES = [
    {"key": "crisis-1997", "name": "亚洲金融危机",
     "start": "1997-05-12", "end": "1999-09-30", "hs300": False,
     "summary": "泰铢弃守汇率引爆亚洲货币连环危机，A股恰逢印花税上调与"
                "发行节奏收紧，五个月最大跌幅三成，直到 1999 年 5·19 "
                "行情才走出底部、两年后收复前高。",
     "events": [
         {"date": "1997-05-10", "label": "印花税上调至 5‰，A股见顶回落"},
         {"date": "1997-07-02", "label": "泰铢放弃联系汇率，危机引爆"},
         {"date": "1997-10-23", "label": "国际炒家狙击港元，港股单日暴跌"},
         {"date": "1998-08-14", "label": "港府入市击退炒家，亚洲市场渐稳"},
     ]},
    {"key": "crisis-2001", "name": "国有股减持熊市",
     "start": "2001-06-13", "end": "2007-03-31", "hs300": False,
     "summary": "国有股市价减持办法出台成为导火索，叠加科网泡沫破灭与"
                "券商治理整顿，A股四年长熊最大腰斩有余，股权分置改革后"
                "才见底回升、五年半后收复前高。",
     "events": [
         {"date": "2001-06-12", "label": "《减持国有股筹集社会保障资金"
                                         "管理暂行办法》发布"},
         {"date": "2001-07-26", "label": "减持办法进入新股发行，市场恐慌下跌"},
         {"date": "2001-10-22", "label": "证监会叫停首发增发中的国有股出售"},
         {"date": "2002-06-23", "label": "国有股减持全面叫停，6·24 暴涨"},
         {"date": "2005-04-29", "label": "股权分置改革试点启动"},
         {"date": "2005-06-06", "label": "上证盘中跌破千点（998.23）"},
     ]},
    {"key": "crisis-2008", "name": "全球金融危机",
     "start": "2007-10-16", "end": "2009-12-31", "hs300": True,
     "summary": "美国次贷危机演变为全球金融海啸，叠加 6124 高点的估值"
                "回归，A股一年最大跌幅七成、为 A 股史上最深危机；四万亿"
                "与连续救市后反弹，但 6124 至今未收复。",
     "events": [
         {"date": "2008-04-24", "label": "印花税下调至 1‰，单日大涨"},
         {"date": "2008-09-15", "label": "雷曼兄弟破产，全球金融海啸"},
         {"date": "2008-09-19", "label": "汇金增持+印花税单边+央企回购，"
                                         "三大利好全线涨停"},
         {"date": "2008-11-09", "label": "「四万亿」投资计划公布"},
     ]},
    {"key": "crisis-2015", "name": "股灾（杠杆牛崩盘）",
     "start": "2015-06-12", "end": "2018-02-28", "hs300": True,
     "summary": "场外配资清查引爆杠杆牛崩盘，千股跌停反复出现，叠加 8·11 "
                "汇改与年初熔断连续触发，八个月最大跌幅近五成；5178 "
                "高点至今未收复。",
     "events": [
         {"date": "2015-06-15", "label": "场外配资清查开始，股灾第一波"},
         {"date": "2015-07-08", "label": "千股停牌，「国家队」入场救市"},
         {"date": "2015-08-11", "label": "8·11 汇改，汇率联动冲击"},
         {"date": "2016-01-04", "label": "熔断机制首日两度熔断提前收市"},
         {"date": "2016-01-08", "label": "熔断机制叫停（实施四天）"},
     ]},
    {"key": "crisis-2018", "name": "中美贸易战",
     "start": "2018-01-29", "end": "2021-06-30", "hs300": True,
     "summary": "中美贸易摩擦反复升级压制风险偏好，A股十一个月单边阴跌"
                "近三成，政策底喊话与 G20 休战后见底，三年后收复前高。",
     "events": [
         {"date": "2018-03-23", "label": "美方签署对华贸易备忘录，千股跌停"},
         {"date": "2018-06-15", "label": "美方公布 500 亿美元加征清单，"
                                         "摩擦升级"},
         {"date": "2018-10-19", "label": "金稳委与「一行两会」集中喊话，"
                                         "政策底确立"},
         {"date": "2018-12-01", "label": "G20 阿根廷会晤达成休战共识"},
         {"date": "2019-05-06", "label": "美方关税威胁升级，市场再度急跌"},
     ]},
    {"key": "crisis-2020", "name": "新冠疫情",
     "start": "2020-01-14", "end": "2020-12-31", "hs300": True,
     "summary": "春节前后疫情爆发，叠加海外油价战与美股连续熔断，A股两个"
                "月最大跌幅不足一成五；全球宽松与国内疫后修复下见底后"
                "三个多月即收复前高，历次危机中恢复最快。",
     "events": [
         {"date": "2020-01-23", "label": "武汉封城，春节休市前最后交易日"},
         {"date": "2020-02-03", "label": "春节后首日开盘逾三千股跌停"},
         {"date": "2020-03-09", "label": "沙特油价战引爆，美股十天内四次熔断"},
         {"date": "2020-03-27", "label": "政治局会议部署抗疫特别国债"},
     ]},
    {"key": "crisis-2021", "name": "地产中概熊市",
     "start": "2021-09-13", "end": "2025-12-31", "hs300": True,
     "summary": "房企债务危机与中概股监管冲击长期压制市场，A股近两年半"
                "阴跌至 2700 一线；2024 年「9·24」一揽子增量政策引爆"
                "反转，四年后面收复前高并续创新高。",
     "events": [
         {"date": "2021-09-13", "label": "恒大危机全面发酵，A股见顶回落"},
         {"date": "2022-03-16", "label": "金稳委专题会议稳预期，单日反转"},
         {"date": "2024-01-22", "label": "国常会部署增强市场内在稳定性"},
         {"date": "2024-02-05", "label": "汇金扩大增持，流动性危机见底"},
         {"date": "2024-09-24", "label": "「9·24」一揽子增量政策，暴力反转"},
     ]},
]


def _validate(bars: list[tuple[str, float]]) -> None:
    """序列形状校验：非空、升序无重复、收盘全为正——脏数据明确报错。"""
    if not bars:
        raise ValueError("序列为空")
    dates = [b[0] for b in bars]
    if len(set(dates)) != len(dates):
        raise ValueError("日期重复")
    if any(a > b for a, b in zip(dates, dates[1:])):
        raise ValueError("日期必须升序")
    for d, c in bars:
        if c is None or c <= 0:
            raise ValueError(f"收盘价非正: {d} {c}")


def slice_window(bars: list[tuple[str, float]], start: str,
                 end: str) -> list[tuple[str, float]]:
    """按自然日闭区间 [start, end] 切片（start/end 无需为交易日）。

    区间错位或无数据（配置与数据对不上）明确报错，端点据此降级。
    """
    _validate(bars)
    if start > end:
        raise ValueError(f"窗口起止倒置: {start} > {end}")
    window = [(d, float(c)) for d, c in bars if start <= d <= end]
    if not window:
        raise ValueError(f"窗口 {start}~{end} 内无数据")
    return window


def crisis_stats(window: list[tuple[str, float]]) -> dict:
    """危机区间统计（收盘口径）：基线=首根收盘，见底=最低收盘（并列取
    最早），恢复=见底后首个收盘≥基线；耗时均按交易日计。"""
    _validate(window)
    base_date, base = window[0][0], float(window[0][1])
    trough_i = min(range(len(window)),
                   key=lambda i: (window[i][1], i))   # 并列取最早
    trough_date, trough = window[trough_i][0], float(window[trough_i][1])
    recover = next(((i, d, float(c)) for i, (d, c) in enumerate(window)
                    if i > trough_i and c >= base), None)
    return {"base_date": base_date, "base": base,
            "trough_date": trough_date, "trough": trough,
            "trough_ret": round(trough / base - 1, 4),
            "trough_days": trough_i,
            "recover_date": recover[1] if recover else None,
            "recover_ret": (round(recover[2] / base - 1, 4)
                            if recover else None),
            "recover_days": (recover[0] - trough_i if recover else None)}


def crisis_points(main_window: list[tuple[str, float]],
                  sub_window: list[tuple[str, float]]) -> list[dict]:
    """双线对齐：日期并集升序，各线相对各自窗口首根的累计涨幅（round 4），
    缺席一侧该日 None（沪深300 无数据/窗口缺失均走 None，不降级）。"""
    _validate(main_window)
    if sub_window:
        _validate(sub_window)

    def line(window):
        if not window:
            return {}
        base = float(window[0][1])
        return {d: round(float(c) / base - 1, 4) for d, c in window}

    l_main, l_sub = line(main_window), line(sub_window)
    return [{"date": d, "sse": l_main.get(d), "hs300": l_sub.get(d)}
            for d in sorted(set(l_main) | set(l_sub))]


def _connect():
    return psycopg.connect(config.DATABASE_URL)


def load_bars(conn, symbol: str) -> list[tuple[str, float]]:
    """读收盘序列（模块级薄封装：端点 monkeypatch 测试接缝）。"""
    return fetch_closes(conn, symbol)


def _down(detail: str) -> dict:
    return {"status": "down", "detail": detail, "crises": []}


def register_crises_routes(app) -> None:
    @app.get("/api/market/crises")
    def market_crises():
        """金融危机史：七次危机的区间统计+双口径走势点列。

        始终 200；PG 不可达 / 上证无数据 / 切片脏数据在 status=down
        表达（降级约定，前端渲染占位卡）。沪深300 表空不降级——
        各危机 hs300_available=False、points 内 hs300 全 None。
        """
        try:
            with _connect() as conn:
                main_bars = load_bars(conn, SYMBOL_MAIN)
                sub_bars = load_bars(conn, SYMBOL_SUB)
        except Exception as exc:                    # noqa: BLE001 降级约定
            return _down(f"{type(exc).__name__}: {str(exc)[:120]}")
        if not main_bars:
            return _down("index_daily 暂无上证指数数据（等待首次指数日更）")
        crises = []
        try:
            for c in CRISES:
                window_main = slice_window(main_bars, c["start"], c["end"])
                window_sub = (slice_window(sub_bars, c["start"], c["end"])
                              if c["hs300"] and sub_bars else [])
                crises.append({
                    "key": c["key"], "name": c["name"], "summary": c["summary"],
                    "start": c["start"], "end": c["end"], "events": c["events"],
                    "stats": crisis_stats(window_main),
                    "points": crisis_points(window_main, window_sub),
                    "hs300_available": bool(window_sub)})
        except ValueError as exc:
            return _down(str(exc))
        return {"status": "up",
                "symbols": {"main": {"code": SYMBOL_MAIN, "name": "上证指数",
                                     "source": "eastmoney"},
                            "sub": {"code": SYMBOL_SUB, "name": "沪深300",
                                    "source": "eastmoney", "note": SUB_NOTE}},
                "crises": crises}
