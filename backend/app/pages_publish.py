"""GitHub Pages 自动发布：快照导出 → git 提交推送（回测日更的收尾步骤）。

工作方式（PAGES_REPO_URL 配置了含 token 的仓库 URL 时才启用）：
  ① work_dir 无仓库 → clone（depth 1）；有 → fetch + reset 到 origin/main
  ② pages_export.export_all() 把快照写进 work_dir/site-data/
  ③ 有变更则 commit + push origin main → Pages workflow 自动重建发布

安全：repo_url 含 token，日志一律走 _safe_url() 打码；git 凭证不落盘
（clone URL 即凭证，仅存于进程环境变量）。push 失败抛异常由调用方兜底
（回测结果是主交付物，Pages 保持上一份快照，次日自愈）。
"""

import subprocess
from pathlib import Path

from .pages_export import export_all

_SITE_DIR = "site-data"
_BRANCH = "main"


def _safe_url(url: str) -> str:
    """打码 URL 中的凭证段（https://user:token@host → https://***@host）。"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.split('@', 1)[-1]}"


def _git(work_dir: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=work_dir, capture_output=True, text=True,
        timeout=600, env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin",
                          **(env or {})})
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败："
                           f"{proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def _redact(text: str, url: str) -> str:
    """git 报错信息可能回显带 token 的 URL，统一打码。"""
    if "@" not in url:
        return text
    token = url.split("://", 1)[-1].split("@", 1)[0]
    return text.replace(token, "***")


def publish(dsn: str, repo_url: str, work_dir: str | Path, *,
            log=print) -> dict:
    """导出快照到仓库的 site-data/ 并推送（无变更则不产生提交）。"""
    work = Path(work_dir)
    safe = _safe_url(repo_url)
    work.mkdir(parents=True, exist_ok=True)
    if (work / ".git").exists():
        _git(work, "fetch", "origin", _BRANCH)
        _git(work, "reset", "--hard", f"origin/{_BRANCH}")
        log("  仓库已更新到 origin/main")
    else:
        try:
            _git(work.parent, "clone", "--depth", "1", "--branch", _BRANCH,
                 repo_url, work.name)
            log("  仓库 clone 完成（depth 1）")
        except RuntimeError as exc:
            raise RuntimeError(f"clone 失败（{safe}）："
                               f"{_redact(str(exc), repo_url)}") from exc

    stats = export_all(dsn, work / _SITE_DIR, log=log)

    _git(work, "add", _SITE_DIR)
    status = _git(work, "status", "--porcelain", _SITE_DIR)
    if not status:
        log("  快照无变更，不产生提交")
        return {"changed": False, "pushed": False, **stats}
    _git(work, "-c", "user.name=codefuckee-pages-bot",
         "-c", "user.email=CodeFuckee@users.noreply.github.com",
         "commit", "-m",
         "chore: 定投回测快照日更（调度器自动发布）")
    try:
        _git(work, "push", "origin", _BRANCH)
    except RuntimeError as exc:
        raise RuntimeError(f"push 失败（GitHub 不可达或凭证过期）："
                           f"{_redact(str(exc), repo_url)}") from exc
    log("  已推送 origin/main → Pages workflow 自动重建")
    return {"changed": True, "pushed": True, **stats}
