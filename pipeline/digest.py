"""Weekly digest generation.

Pulls a week of GitHub activity across the configured UCP repos via
github_fetch, then asks Claude to synthesise it into a structured
digest matching the same JSON schema daily-digest uses (so the
downstream scriptwriter, audio, and publisher steps are unchanged).

Editorial context from docs/podcast-context.yaml is injected into the
system prompt to keep narration consistent across episodes.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

import anthropic

from pipeline.db_ops import get_recent_digests
from pipeline.github_fetch import fetch_week_activity
from pipeline.utils import load_repos, load_podcast_context, retry_with_backoff

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")


def _build_system_prompt(
    context: dict,
    recent_summary: str = "",
    last_ep_date: date | None = None,
) -> str:
    """Build the digest system prompt with editorial context."""
    personal = context.get("personal_context", "").strip()
    project = context.get("project_context", "").strip()
    editorial = context.get("editorial_angle", "").strip()
    deprioritise = context.get("deprioritise", [])

    if recent_summary and last_ep_date:
        dedup_block = f"""

PREVIOUSLY COVERED (last 4 episodes — DO NOT repeat unless there is a NEW development):
{recent_summary}

DEDUPLICATION RULES (STRICT):
- Last episode aired {last_ep_date}. Stories must reflect activity AFTER that date.
- A long-running PR previously covered may only reappear if it MERGED, gained new
  commits with substantive changes, or hit a milestone (review, decision) since
  the last episode.
- An ongoing issue or discussion may reappear only if there has been a notable
  comment, label change, or resolution since the last episode.
- If a repo had nothing new since {last_ep_date}, return ZERO stories for it.
  Padding with stale recap is worse than an empty topic — listeners notice."""
    else:
        dedup_block = ""

    return f"""You are an editor compiling a weekly briefing about activity across the
Universal Commerce Protocol (UCP) GitHub repositories.

ABOUT THE HOST:
{personal}

ABOUT UCP:
{project}

EDITORIAL PERSPECTIVE:
{editorial}

DEPRIORITISE:
{chr(10).join('- ' + t for t in deprioritise)}{dedup_block}

INPUT FORMAT:
You will be given a structured JSON payload of GitHub activity grouped by
theme (Spec & Schema, Client SDKs, Testing & Samples, Governance & Community).
Each group contains repos and each repo lists merged PRs, open PRs, issues
opened, issues closed, and releases — all within the past 7 days.

YOUR TASK:
Synthesise this raw activity into a digest with one topic per group. For each
group, surface the most significant stories (a "story" is one PR, release,
issue, or thematic cluster). Aim for 1-4 stories per group, fewer is fine,
zero is fine for a quiet group.

QUALITY BAR FOR A "STORY":
- A merged PR or release with substantive changes — flag the version, link
  the URL, summarise what changed in one or two sentences.
- A cluster of related PRs that together advance one feature.
- A high-engagement issue or discussion (multiple comments, contested
  decisions, design proposals).
- A first-time external contribution — worth a mention even if small.
- NOT a story: dependabot bumps, lint config tweaks, single-line typo fixes,
  unmerged draft PRs with no review activity.

OUTPUT — return ONLY a JSON object matching this schema, no markdown fencing:
{{
  "date": "YYYY-MM-DD",
  "topics": [
    {{
      "topic_name": "string (use the group name verbatim)",
      "stories": [
        {{
          "headline": "string (concise, scannable — include PR/release number where helpful)",
          "published_date": "YYYY-MM-DD (merge date, release date, issue close date, etc.)",
          "summary": "string (2-3 sentences: what changed and why it matters)",
          "source": "string (e.g. 'PR #42 in ucp-schema by @username' or 'Release v0.3.0 in python-sdk')",
          "relevance": "string (1 sentence on why an implementer should care)"
        }}
      ]
    }}
  ],
  "connections": "string (1 short paragraph noting cross-repo themes or coupling — e.g. 'a schema field added in ucp-schema landed in both SDKs this week')"
}}"""


def _summarise_recent_episodes(limit: int = 4) -> tuple[str, date | None]:
    """Build a brief breakdown of recent episodes for dedup."""
    recent = get_recent_digests(limit=limit)
    if not recent:
        return "", None

    last_ep_date = recent[0]["episode_date"]

    blocks = []
    for ep in recent:
        ep_date = ep["episode_date"]
        digest = ep["digest_json"]
        if not digest or "topics" not in digest:
            continue
        topic_lines = []
        for topic in digest["topics"]:
            stories = topic.get("stories", [])
            if not stories:
                continue
            topic_lines.append(f"  Topic: {topic.get('topic_name', '')}")
            for story in stories:
                pub = story.get("published_date", "?")
                src = story.get("source", "?")
                hl = story.get("headline", "")
                topic_lines.append(f"    [{pub}] {hl} ({src})")
        if topic_lines:
            blocks.append(f"Episode {ep_date}:\n" + "\n".join(topic_lines))

    if not blocks:
        return "", last_ep_date
    return "\n\n".join(blocks), last_ep_date


def _build_user_prompt(activity: dict) -> str:
    """Render the GitHub activity payload as the user message."""
    payload = json.dumps(activity, indent=2, default=str)
    if len(payload) > 200_000:
        # Defensive cap — typical weeks should be far smaller, but a busy
        # release week could spike. Truncating here keeps token use bounded.
        payload = payload[:200_000] + "\n…[truncated]…"
    return (
        f"Past 7 days of UCP GitHub activity (window {activity['since']} → {activity['until']}):\n\n"
        f"{payload}\n\n"
        "Synthesise this into the digest JSON per your instructions. Return ONLY the JSON object, "
        "no markdown fencing, no preamble."
    )


def _extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction from a Claude response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


@retry_with_backoff(max_retries=3, initial_delay=2)
def generate_digest(repos: list[dict] = None) -> tuple[dict, dict]:
    """Generate a weekly digest from GitHub activity.

    Args:
        repos: List of repo dicts (owner, name, display, group). If None,
               loaded via load_repos().

    Returns:
        (digest_dict, usage_metadata)
    """
    if repos is None:
        repos = load_repos()

    activity = fetch_week_activity(repos, days=7)

    context = load_podcast_context()
    recent_summary, last_ep_date = _summarise_recent_episodes(limit=4)
    system_prompt = _build_system_prompt(context, recent_summary, last_ep_date)

    client = anthropic.Anthropic()
    user_prompt = _build_user_prompt(activity)

    if last_ep_date:
        logger.info(
            f"Dedup context: {len(recent_summary)} chars from last 4 episodes (last aired {last_ep_date})"
        )
    repo_count = sum(len(rs) for rs in activity["groups"].values())
    logger.info(
        f"Calling Claude ({CLAUDE_MODEL}) to synthesise {repo_count} repos across "
        f"{len(activity['groups'])} groups…"
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text_content = ""
    for block in response.content:
        if block.type == "text":
            text_content += block.text

    digest = _extract_json(text_content)

    if digest is None:
        logger.warning("No JSON in initial response, sending follow-up to compile digest…")
        followup = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=6000,
            system="You are a JSON compiler. Output ONLY a valid JSON object, no other text.",
            messages=[
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": (
                    "Compile the digest JSON now per the schema in the prior system instructions. "
                    "Return ONLY the JSON object."
                )},
            ],
        )
        followup_text = ""
        for block in followup.content:
            if block.type == "text":
                followup_text += block.text
        response.usage.input_tokens += followup.usage.input_tokens
        response.usage.output_tokens += followup.usage.output_tokens
        digest = _extract_json(followup_text)
        if digest is None:
            raise ValueError(f"Failed to extract JSON from follow-up: {followup_text[:500]}")

    usage = {
        "model": CLAUDE_MODEL,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }

    topic_count = len(digest.get("topics", []))
    story_count = sum(len(t.get("stories", [])) for t in digest.get("topics", []))
    logger.info(f"Digest generated: {topic_count} topics, {story_count} stories")

    return digest, usage
