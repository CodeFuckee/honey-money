"""GitHub Pages 静态快照导出——手动驱动（日更自动发布见 app/pages_publish.py）。

计算/导出编排已迁入 backend/app/pages_export.py（与调度器日更的自动发布
共用同一实现）。本脚本只负责：连库导出到指定目录，供手动提交 site-data/
或调试。结构与后端 API 响应一致（前端 VITE_STATIC=1 构建时改读这些文件）。

用法：DATABASE_URL=... .venv/bin/python scripts/export_static_api.py [输出目录]
（缺省输出仓库根 site-data/）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.pages_export import export_all  # noqa: E402


def main():
    import os
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money")
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parents[1] / "site-data"
    print(f"导出静态快照 → {out_dir}")
    export_all(dsn, out_dir, log=print)


if __name__ == "__main__":
    main()
