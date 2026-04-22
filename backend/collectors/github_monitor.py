"""GitHub Collector：监控 6 个中国 AI 头部组织的 repos + releases。

使用 GitHub REST API v3（不含 GraphQL）。认证用 Personal Access Token。
免费额度 5000 req/hr，本模块每轮 ~50 请求，绰绰有余。
"""
import logging

import requests

from backend.db import get_conn, record_status
from backend.utils import config
from backend.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

ORGS = [
    "deepseek-ai",
    "THUDM",
    "MoonshotAI",
    "MiniMax-AI",
    "stepfun-ai",
    "QwenLM",
]

GITHUB_API = "https://api.github.com"
MAX_REPOS_PER_ORG = 30        # 每 org 只看最近 push 的前 30 个 repo
MAX_RELEASES_PER_REPO = 5     # 每 repo 只看最近 5 个 release


def _auth_headers() -> dict:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN 未配置，无法调用 GitHub API")
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ModelRadar/1.0",
    }


@retry_with_backoff(max_retries=2, base_delay=5.0)
def _get(url: str, params: dict | None = None) -> list | dict:
    resp = requests.get(url, headers=_auth_headers(), params=params, timeout=20)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError(f"GitHub API rate limited: {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def _list_org_repos(org: str) -> list[dict]:
    url = f"{GITHUB_API}/orgs/{org}/repos"
    params = {"sort": "pushed", "direction": "desc", "per_page": MAX_REPOS_PER_ORG}
    return _get(url, params=params)


def _list_repo_releases(org: str, repo: str) -> list[dict]:
    url = f"{GITHUB_API}/repos/{org}/{repo}/releases"
    params = {"per_page": MAX_RELEASES_PER_REPO}
    try:
        return _get(url, params=params)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise


def _persist_repo(conn, org: str, repo: dict) -> None:
    import json as _json
    topics = repo.get("topics") or []
    conn.execute(
        """
        INSERT INTO github_snapshots
          (org, repo_name, stars, forks, open_issues, pushed_at, description, topics)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            org,
            repo["name"],
            repo.get("stargazers_count"),
            repo.get("forks_count"),
            repo.get("open_issues_count"),
            repo.get("pushed_at"),
            (repo.get("description") or "")[:500],
            _json.dumps(topics, ensure_ascii=False),
        ),
    )


def _persist_release(conn, org: str, repo_name: str, rel: dict) -> bool:
    """插入 release，使用 UNIQUE(org,repo_name,tag_name) 防重。返回是否新插入。"""
    body = rel.get("body") or ""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO github_releases
          (org, repo_name, tag_name, release_name, published_at, body_preview, html_url, is_prerelease)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            org,
            repo_name,
            rel.get("tag_name") or "",
            rel.get("name"),
            rel.get("published_at"),
            body[:500],
            rel.get("html_url"),
            1 if rel.get("prerelease") else 0,
        ),
    )
    return cur.rowcount > 0


def collect() -> dict:
    """一次完整的 GitHub 采集。返回 {org: {repos_seen, new_releases}}。"""
    summary = {}
    try:
        with get_conn() as conn:
            for org in ORGS:
                try:
                    repos = _list_org_repos(org)
                except Exception as e:
                    logger.error("[GH] 列出 %s 仓库失败: %s", org, e)
                    summary[org] = {"error": str(e)[:100]}
                    continue

                org_new_releases = 0
                for repo in repos:
                    if repo.get("archived") or repo.get("fork"):
                        continue
                    _persist_repo(conn, org, repo)
                    try:
                        releases = _list_repo_releases(org, repo["name"])
                    except Exception as e:
                        logger.warning("[GH] releases %s/%s 失败: %s", org, repo["name"], e)
                        continue
                    for rel in releases:
                        if _persist_release(conn, org, repo["name"], rel):
                            org_new_releases += 1

                summary[org] = {
                    "repos_seen": len(repos),
                    "new_releases": org_new_releases,
                }
                logger.info("[GH] %s: %d repos, %d new releases",
                            org, len(repos), org_new_releases)

        record_status("github", success=True)
        return summary
    except Exception as e:
        logger.exception("GitHub 采集整体失败: %s", e)
        record_status("github", success=False, error=str(e))
        raise


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(collect())
