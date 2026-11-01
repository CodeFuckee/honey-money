"""导出定投回测静态快照数据（GitHub Pages 用）。

从 fund_dca_result / fund_dca_rounds 导出静态 JSON 到 site-data/，
结构与后端 API 响应一致（前端 VITE_STATIC=1 构建时改为读这些文件）：
  site-data/funds/dca.json                    排行全量行 + stats + meta
  site-data/funds/dca/scatter.{fee}.json      散点+分箱（fee ∈ gross/disc/nom）
  site-data/funds/dca/rounds/{code}.json      单基金三口径轮次 + totals

排行行含服务端注入的 age_years（回测参考日 - full_start），静态端过滤/
排序/分页/统计全部客户端化。费用字段/轮次与 API 同名。

用法：DATABASE_URL=... .venv/bin/python scripts/export_static_api.py
（在有库的环境跑——本机或 NAS；产物提交进仓库供 Pages workflow 打包）
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psycopg  # noqa: E402

from app.dca_api import fetch_dca_list, fetch_dca_scatter  # noqa: E402

DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money")
OUT = Path(__file__).resolve().parents[1] / "site-data"
PAGE = 200
FEES = ("gross", "disc", "nom")


def write_json(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"  {path.relative_to(OUT.parent)} ({path.stat().st_size:,} B)")


def export_list() -> None:
    """排行全量行（翻页拉全）+ 首页的 stats/meta（全局口径）。"""
    rows, stats, meta, total = [], None, None, 0
    offset = 0
    while True:
        body = fetch_dca_list(DSN, limit=PAGE, offset=offset)
        if body.get("status") != "up":
            raise SystemExit(f"DB 不可达：{body.get('detail')}")
        rows.extend(body["rows"])
        total = body["total"]
        stats, meta = body["stats"], body["meta"]
        offset += PAGE
        if offset >= total or not body["rows"]:
            break
    write_json(OUT / "funds" / "dca.json",
               {"status": "up", "total": total, "rows": rows,
                "stats": stats, "meta": meta})
    print(f"  排行行：{len(rows):,}（total={total:,}）")


def export_scatter() -> None:
    for fee in FEES:
        body = fetch_dca_scatter(DSN, fee=fee)
        if body.get("status") != "up":
            raise SystemExit(f"scatter {fee} 失败：{body.get('detail')}")
        write_json(OUT / "funds" / "dca" / f"scatter.{fee}.json", body)


def export_rounds() -> None:
    """三口径轮次按基金聚合为单文件；无轮次基金不落文件（前端 404=无止盈）。"""
    out_dir = OUT / "funds" / "dca" / "rounds"
    n = 0
    with psycopg.connect(DSN, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT code, fee, seq, tp_date, round_days, gap_days, profit,
                       tp_nav, invested, sub_fee, redeem_fee, net_profit
                FROM fund_dca_rounds ORDER BY code, fee, seq""")
            cur_code, rounds, totals = None, {}, {}
            def flush(code):
                if code is None:
                    return
                write_json(out_dir / f"{code}.json",
                           {"status": "up", "code": code,
                            "rounds": rounds, "totals": totals})
            for code, fee, seq, tp_date, rd, gap, profit, tp_nav, inv, sf, rf, np_ in cur:
                if code != cur_code:
                    flush(cur_code)
                    n += 1
                    cur_code, rounds, totals = code, {}, {}
                row = {"seq": seq,
                       "tp_date": tp_date.isoformat() if tp_date else None,
                       "round_days": rd, "gap_days": gap,
                       "profit": round(profit, 4) if profit is not None else None,
                       "tp_nav": tp_nav, "invested": inv,
                       "sub_fee": sf, "redeem_fee": rf, "net_profit": np_}
                rounds.setdefault(fee, []).append(row)
                t = totals.setdefault(fee, {"rounds": 0, "invested": 0.0,
                                            "net_profit": 0.0, "sub_fee": 0.0,
                                            "redeem_fee": 0.0})
                t["rounds"] += 1
                for k, v in (("invested", inv), ("net_profit", np_),
                             ("sub_fee", sf), ("redeem_fee", rf)):
                    t[k] = round(t[k] + (v or 0), 2)
            flush(cur_code)
            n += 1
    print(f"  轮次文件：{n:,} 只")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    print(f"导出静态快照 → {OUT}")
    export_list()
    export_scatter()
    export_rounds()
    print("完成")
