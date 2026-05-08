"""Fetch a week of GitHub activity across the configured repos.

Replaces daily-digest's web_search-based research with deterministic
GitHub REST API queries. The output is a structured payload that
digest.py hands to Claude for synthesis into the weekly digest JSON.

For each repo we collect, within the past 7 days:
- Merged pull requests (highest signal)
- Newly opened pull requests still open at end of week
- New releases
- Issues opened
- Issues closed
- New discussions (when the repo enables Discussions)

Stale label changes, dependabot bumps, and pure CI tweaks are surfaced
as low-signal entries so Claude can decide whether to mention them.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GH_TOKEN = os.environ.get("GH_FETCH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
PER_PAGE = 100


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


def _get(path: str, params: dict | None = None) -> Any:
    """GET a GitHub REST endpoint, with basic rate-limit logging."""
    resp = requests.get(f"{GITHUB_API}{path}", headers=_headers(), params=params, timeout=30)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        logger.error(f"GitHub rate limit hit. Remaining={remaining}, reset_epoch={reset}")
    resp.raise_for_status()
    return resp.json()


def _paginate(path: str, params: dict | None = None, max_pages: int = 5) -> list[dict]:
    """Fetch up to max_pages of a paginated endpoint."""
    items: list[dict] = []
    params = dict(params or {})
    params.setdefault("per_page", PER_PAGE)
    for page in range(1, max_pages + 1):
        params["page"] = page
        batch = _get(path, params)
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return items


def _within_window(ts: str | None, since: datetime) -> bool:
    if not ts:
        return False
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) >= since


def _summarise_pr(pr: dict) -> dict:
    """Project a PR object into the small shape we hand to Claude."""
    body = (pr.get("body") or "").strip()
    if len(body) > 1500:
        body = body[:1500] + "…"
    return {
        "number": pr["number"],
        "title": pr.get("title", ""),
        "url": pr.get("html_url"),
        "author": (pr.get("user") or {}).get("login"),
        "state": pr.get("state"),
        "merged": pr.get("merged_at") is not None,
        "merged_at": pr.get("merged_at"),
        "created_at": pr.get("created_at"),
        "closed_at": pr.get("closed_at"),
        "body": body,
        "labels": [l["name"] for l in pr.get("labels", [])],
        "draft": pr.get("draft", False),
    }


def _summarise_issue(issue: dict) -> dict:
    body = (issue.get("body") or "").strip()
    if len(body) > 1500:
        body = body[:1500] + "…"
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "url": issue.get("html_url"),
        "author": (issue.get("user") or {}).get("login"),
        "state": issue.get("state"),
        "created_at": issue.get("created_at"),
        "closed_at": issue.get("closed_at"),
        "comments": issue.get("comments", 0),
        "body": body,
        "labels": [l["name"] for l in issue.get("labels", [])],
    }


def _summarise_release(rel: dict) -> dict:
    body = (rel.get("body") or "").strip()
    if len(body) > 2000:
        body = body[:2000] + "…"
    return {
        "tag": rel.get("tag_name"),
        "name": rel.get("name"),
        "url": rel.get("html_url"),
        "published_at": rel.get("published_at"),
        "prerelease": rel.get("prerelease", False),
        "draft": rel.get("draft", False),
        "body": body,
        "author": (rel.get("author") or {}).get("login"),
    }


def fetch_repo_activity(owner: str, repo: str, since: datetime) -> dict:
    """Fetch a single repo's activity since the given UTC datetime.

    Returns a dict with merged_prs, open_prs, issues_opened, issues_closed,
    releases. Each list is bounded by the GitHub API page limits we set.
    """
    logger.info(f"Fetching {owner}/{repo} activity since {since.isoformat()}…")

    # Pull requests — fetch recently updated and filter client-side.
    # GitHub doesn't have a "merged_after" search, so we paginate by updated
    # desc and stop once we cross the window.
    all_prs = _paginate(
        f"/repos/{owner}/{repo}/pulls",
        params={"state": "all", "sort": "updated", "direction": "desc"},
        max_pages=3,
    )
    merged_prs: list[dict] = []
    open_prs: list[dict] = []
    for pr in all_prs:
        updated = pr.get("updated_at")
        if not _within_window(updated, since):
            break  # list is desc, so we can stop
        if pr.get("merged_at") and _within_window(pr["merged_at"], since):
            merged_prs.append(_summarise_pr(pr))
        elif pr.get("state") == "open" and _within_window(pr.get("created_at"), since):
            open_prs.append(_summarise_pr(pr))

    # Issues — the issues endpoint also returns PRs, so filter them out.
    issues_raw = _paginate(
        f"/repos/{owner}/{repo}/issues",
        params={"state": "all", "since": since.isoformat(), "sort": "updated", "direction": "desc"},
        max_pages=3,
    )
    issues_opened: list[dict] = []
    issues_closed: list[dict] = []
    for it in issues_raw:
        if "pull_request" in it:
            continue  # skip PRs masquerading as issues
        if _within_window(it.get("created_at"), since):
            issues_opened.append(_summarise_issue(it))
        if it.get("state") == "closed" and _within_window(it.get("closed_at"), since):
            issues_closed.append(_summarise_issue(it))

    # Releases
    rels_raw = _paginate(f"/repos/{owner}/{repo}/releases", max_pages=2)
    releases = [
        _summarise_release(r)
        for r in rels_raw
        if _within_window(r.get("published_at"), since) and not r.get("draft")
    ]

    # Reviewer / commenter activity — for the EP path it matters who's
    # active in spec/governance discussion, not just what landed.
    contributors = _aggregate_contributors(owner, repo, since)

    summary = {
        "owner": owner,
        "repo": repo,
        "merged_prs": merged_prs,
        "open_prs": open_prs,
        "issues_opened": issues_opened,
        "issues_closed": issues_closed,
        "releases": releases,
        "contributors": contributors,
    }
    logger.info(
        f"  {owner}/{repo}: {len(merged_prs)} merged PR(s), {len(open_prs)} new open PR(s), "
        f"{len(issues_opened)} issue(s) opened, {len(issues_closed)} issue(s) closed, "
        f"{len(releases)} release(s), {len(contributors)} active contributor(s)"
    )
    return summary


def _aggregate_contributors(owner: str, repo: str, since: datetime) -> list[dict]:
    """Build a per-author activity summary across PR reviews and issue/PR comments.

    The repo-level "active contributors" view is what lets the digest
    identify who's setting tone in spec/governance discussions — useful
    both for picking engagement targets and for week-over-week stakeholder
    tracking.
    """
    counts: dict[str, dict] = {}

    def bump(login: str | None, key: str):
        if not login:
            return
        rec = counts.setdefault(
            login, {"login": login, "prs_authored": 0, "issues_authored": 0,
                    "reviews": 0, "issue_comments": 0, "pr_comments": 0}
        )
        rec[key] += 1

    # PRs and issues: authors are easy from already-fetched data, but we
    # want comments and reviews too. Re-fetch via dedicated endpoints to
    # pick up commenters who didn't author anything.
    try:
        issue_comments = _paginate(
            f"/repos/{owner}/{repo}/issues/comments",
            params={"sort": "created", "direction": "desc",
                    "since": since.isoformat()},
            max_pages=3,
        )
        for c in issue_comments:
            login = (c.get("user") or {}).get("login")
            # GitHub returns the same endpoint for issue and PR comments;
            # distinguish by issue_url vs pull_request_url in the comment payload.
            key = "pr_comments" if "/pulls/" in (c.get("issue_url") or c.get("html_url") or "") else "issue_comments"
            bump(login, key)
    except requests.HTTPError as e:
        logger.warning(f"  comments fetch failed for {owner}/{repo}: {e}")

    try:
        # Pull request reviews require listing per-PR; do it for the PRs
        # we already know touched the window. Cheap because most repos
        # see < 20 PRs in a week.
        pr_pages = _paginate(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "all", "sort": "updated", "direction": "desc"},
            max_pages=2,
        )
        for pr in pr_pages:
            if not _within_window(pr.get("updated_at"), since):
                break
            number = pr.get("number")
            try:
                reviews = _get(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
            except requests.HTTPError:
                continue
            for rev in reviews or []:
                if not _within_window(rev.get("submitted_at"), since):
                    continue
                login = (rev.get("user") or {}).get("login")
                bump(login, "reviews")
            # Authors of PRs in the window
            if _within_window(pr.get("created_at"), since):
                bump((pr.get("user") or {}).get("login"), "prs_authored")
    except requests.HTTPError as e:
        logger.warning(f"  reviews fetch failed for {owner}/{repo}: {e}")

    # Sort by total activity desc, drop bots so the EP-path narrative stays
    # focused on humans whose review actually carries weight.
    out = sorted(
        (
            r for r in counts.values()
            if not (r["login"].endswith("[bot]") or r["login"] == "github-actions")
        ),
        key=lambda r: (
            r["reviews"] * 3 + r["pr_comments"] * 2
            + r["prs_authored"] * 2 + r["issues_authored"] + r["issue_comments"]
        ),
        reverse=True,
    )
    return out[:20]  # cap — Claude doesn't need the long tail


def fetch_week_activity(repos: list[dict], days: int = 7) -> dict:
    """Fetch activity across all configured repos for the past `days` days.

    Args:
        repos: list of {owner, name, display, group} dicts (group optional).
        days: lookback window in days.

    Returns:
        {
          "since": ISO8601 string,
          "until": ISO8601 string,
          "groups": {
            "Group Name": [
              { "display": str, "owner": str, "repo": str, ...activity... },
              ...
            ],
            ...
          }
        }
    """
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)

    groups: dict[str, list[dict]] = {}
    for entry in repos:
        owner = entry.get("owner") or entry["owner_default"]
        name = entry["name"]
        display = entry.get("display") or name
        group = entry.get("group") or display

        try:
            activity = fetch_repo_activity(owner, name, since)
        except requests.HTTPError as e:
            logger.warning(f"Skipping {owner}/{name}: {e}")
            continue

        activity["display"] = display
        groups.setdefault(group, []).append(activity)

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "groups": groups,
    }
