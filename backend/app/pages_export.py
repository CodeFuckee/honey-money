"""GitHub Pages 静态快照导出（供定投回测日更顺带发布公开页）。

结构与后端 API 响应一致（前端 VITE_STATIC=1 构建时改读这些文件）：
  site-data/funds/dca.json                    排行全量行 + stats + meta
  site-data/funds/dca/scatter.{fee}.json      散点+分箱（fee ∈ gross/disc/nom）
  site-data/funds/dca/rounds/{code}.json      单基金三口径轮次 + totals

`export_all()` 被 pages_publish.publish()（回测日更后的自动发布）与
scripts/export_static_api.py（手动导出驱动）共用。
"""

import json
from pathlib import Path

import psycopg  # noqa: F401  （占位：调用方负责连接；本模块只做导出编排）

from .dca_api import fetch_dca_list, fetch_dca_scatter

PAGE = 200
FEES = ("gross", "disc", "nom")


def _write_json(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


def export_list(dsn: str, out_dir: Path, *, log=print) -> dict:
    """排行全量行（翻页拉全）+ 首页的 stats/meta（全局口径）。"""
    rows, stats, meta, total = [], None, None, 0
    offset = 0
    while True:
        body = fetch_dca_list(dsn, limit=PAGE, offset=offset)
        if body.get("status") != "up":
            raise RuntimeError(f"DB 不可达：{body.get('detail')}")
        rows.extend(body["rows"])
        total = body["total"]
        stats, meta = body["stats"], body["meta"]
        offset += PAGE
        if offset >= total or not body["rows"]:
            break
    _write_json(out_dir / "funds" / "dca.json",
                {"status": "up", "total": total, "rows": rows,
                 "stats": stats, "meta": meta})
    log(f"  排行行：{len(rows):,}（total={total:,}）")
    return {"rows": len(rows)}


def export_scatter(dsn: str, out_dir: Path, *, log=print) -> None:
    for fee in FEES:
        body = fetch_dca_scatter(dsn, fee=fee)
        if body.get("status") != "up":
            raise RuntimeError(f"scatter {fee} 失败：{body.get('detail')}")
        _write_json(out_dir / "funds" / "dca" / f"scatter.{fee}.json", body)


def export_rounds(dsn: str, out_dir: Path, *, log=print) -> int:
    """三口径轮次按基金聚合为单文件；无轮次基金不落文件（前端 404=无止盈）。"""
    n = 0
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT code, fee, seq, tp_date, round_days, gap_days, profit,
                       tp_nav, invested, sub_fee, redeem_fee, net_profit
                FROM fund_dca_rounds ORDER BY code, fee, seq""")
            cur_code, rounds, totals = None, {}, {}

            def flush(code):
                if code is None:
                    return
                _write_json(out_dir / "funds" / "dca" / "rounds" / f"{code}.json",
                            {"status": "up", "code": code,
                             "rounds": rounds, "totals": totals})

            for (code, fee, seq, tp_date, rd, gap, profit, tp_nav, inv,
                 sf, rf, np_) in cur:
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
    return n


def export_all(dsn: str, out_dir: Path, *, log=print) -> dict:
    """全量导出静态快照到 out_dir（site-data 结构）。返回统计。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    list_stats = export_list(dsn, out_dir, log=log)
    export_scatter(dsn, out_dir, log=log)
    fund_count = export_rounds(dsn, out_dir, log=log)
    stats = {"rows": list_stats["rows"], "round_funds": fund_count}
    log(f"导出完成：排行 {stats['rows']:,} 行 / 轮次基金 {fund_count:,} 只")
    return stats
