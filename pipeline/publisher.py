"""Podcast publishing: RSS feed generation + Netlify deployment.

Generates an iTunes-compatible RSS feed from all published episodes,
then deploys the feed and any new MP3 files to Netlify using the
content-addressed Deploy API (only new/changed files are uploaded).
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone

import requests
from feedgen.feed import FeedGenerator

from pipeline.utils import load_config

logger = logging.getLogger(__name__)

NETLIFY_API_BASE = "https://api.netlify.com/api/v1"


def _get_netlify_auth_token() -> str:
    return os.environ["NETLIFY_AUTH_TOKEN"]


def _get_netlify_site_id() -> str:
    return os.environ["NETLIFY_SITE_ID"]


def _get_site_url() -> str:
    site_id = _get_netlify_site_id()
    return os.environ.get("NETLIFY_SITE_URL", f"https://{site_id}.netlify.app")


def _netlify_headers() -> dict:
    return {"Authorization": f"Bearer {_get_netlify_auth_token()}"}


def _sha1_of_file(filepath: str) -> str:
    """Compute SHA1 hex digest of a file."""
    h = hashlib.sha1()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha1_of_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ---------------------------------------------------------------------------
# RSS Feed Generation
# ---------------------------------------------------------------------------

def generate_rss_feed(episodes: list[dict], site_base_url: str) -> bytes:
    """Generate podcast RSS XML from published episodes.

    Returns RSS XML as UTF-8 bytes.
    """
    podcast_config = load_config("podcast.yaml")["podcast"]

    fg = FeedGenerator()
    fg.load_extension("podcast")

    fg.title(podcast_config["name"])
    fg.link(href=podcast_config.get("website", site_base_url), rel="alternate")
    fg.link(href=f"{site_base_url}/feed.xml", rel="self")
    fg.description(podcast_config["description"])
    fg.language(podcast_config.get("language", "en"))

    fg.podcast.itunes_author(podcast_config.get("author", ""))
    fg.podcast.itunes_summary(podcast_config["description"])
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_owner(
        name=podcast_config.get("author", ""),
        email=podcast_config.get("email", ""),
    )
    fg.podcast.itunes_type("episodic")

    artwork = podcast_config.get("artwork_url", "")
    if artwork:
        fg.podcast.itunes_image(artwork)

    for cat in podcast_config.get("categories", []):
        parts = cat.split(" > ")
        if len(parts) == 2:
            fg.podcast.itunes_category(parts[0].strip(), parts[1].strip())
        else:
            fg.podcast.itunes_category(cat.strip())

    # Add episodes (newest first)
    for ep in sorted(episodes, key=lambda e: str(e["episode_date"]), reverse=True):
        ep_date = str(ep["episode_date"])
        audio_url = f"{site_base_url}/episodes/{ep_date}.mp3"

        fe = fg.add_entry()
        fe.id(audio_url)
        fe.title(ep.get("episode_title") or f"{podcast_config['name']} — {ep_date}")
        fe.description(ep.get("episode_description") or "")
        fe.published(
            datetime.combine(ep["episode_date"], datetime.min.time()).replace(
                tzinfo=timezone.utc
            )
        )
        fe.enclosure(
            url=audio_url,
            length=str(ep.get("audio_file_size") or 0),
            type="audio/mpeg",
        )
        fe.podcast.itunes_duration(str(ep.get("audio_duration") or "00:00:00"))
        fe.podcast.itunes_summary(ep.get("episode_description") or "")

    return fg.rss_str(pretty=True)


# ---------------------------------------------------------------------------
# Netlify Deploy API
# ---------------------------------------------------------------------------

def _upload_file_to_deploy(deploy_id: str, file_path: str, local_path: str):
    """Upload a local file to a Netlify deploy."""
    with open(local_path, "rb") as f:
        resp = requests.put(
            f"{NETLIFY_API_BASE}/deploys/{deploy_id}/files{file_path}",
            headers={**_netlify_headers(), "Content-Type": "application/octet-stream"},
            data=f,
            timeout=600,
        )
        resp.raise_for_status()


def _upload_bytes_to_deploy(deploy_id: str, file_path: str, data: bytes):
    """Upload raw bytes to a Netlify deploy."""
    resp = requests.put(
        f"{NETLIFY_API_BASE}/deploys/{deploy_id}/files{file_path}",
        headers={**_netlify_headers(), "Content-Type": "application/octet-stream"},
        data=data,
        timeout=60,
    )
    resp.raise_for_status()


def _wait_for_deploy(deploy_id: str, target_state: str, timeout: int = 300):
    """Poll deploy status until it reaches target state."""
    elapsed = 0
    interval = 5
    while elapsed < timeout:
        resp = requests.get(
            f"{NETLIFY_API_BASE}/deploys/{deploy_id}",
            headers=_netlify_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        state = resp.json().get("state", "")
        if state == target_state:
            return
        if state == "error":
            raise RuntimeError(
                f"Netlify deploy {deploy_id} failed: {resp.json().get('error_message')}"
            )
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Deploy {deploy_id} did not reach '{target_state}' in {timeout}s")


def deploy_to_netlify(
    new_mp3_path: str,
    episode_date_str: str,
    feed_xml: bytes,
    published_episodes: list[dict],
    extra_files: dict[str, bytes] | None = None,
) -> tuple[str, str]:
    """Deploy episode + feed + extras to Netlify. Returns (mp3_sha1, site_url).

    Args:
        extra_files: Optional dict of {netlify_path: file_bytes} to include
                     (e.g. digest JSON, script text).
    """
    site_id = _get_netlify_site_id()

    # Build manifest: old episodes by stored hash + new files
    files_manifest = {}
    for ep in published_episodes:
        ep_date = str(ep["episode_date"])
        cdn_hash = ep.get("audio_cdn_hash")
        if cdn_hash and ep_date != episode_date_str:
            files_manifest[f"/episodes/{ep_date}.mp3"] = cdn_hash

    new_mp3_hash = _sha1_of_file(new_mp3_path)
    files_manifest[f"/episodes/{episode_date_str}.mp3"] = new_mp3_hash

    feed_hash = _sha1_of_bytes(feed_xml)
    files_manifest["/feed.xml"] = feed_hash

    # Add extra files (digests, scripts)
    extra_hashes = {}
    if extra_files:
        for path, data in extra_files.items():
            h = _sha1_of_bytes(data)
            files_manifest[path] = h
            extra_hashes[path] = (h, data)

    logger.info(
        f"Netlify deploy: {len(files_manifest)} files in manifest "
        f"({len(published_episodes)} old + 1 new + feed.xml"
        f"{f' + {len(extra_hashes)} extras' if extra_hashes else ''})"
    )

    # Create deploy
    resp = requests.post(
        f"{NETLIFY_API_BASE}/sites/{site_id}/deploys",
        headers={**_netlify_headers(), "Content-Type": "application/json"},
        json={"files": files_manifest},
        timeout=60,
    )
    resp.raise_for_status()
    deploy = resp.json()
    deploy_id = deploy["id"]
    required = set(deploy.get("required", []))

    logger.info(f"Deploy {deploy_id} created. {len(required)} file(s) to upload.")

    # Wait for prepared state if async
    if deploy.get("state") == "preparing":
        _wait_for_deploy(deploy_id, "prepared", timeout=120)

    # Upload only what Netlify needs
    if new_mp3_hash in required:
        logger.info(f"Uploading {episode_date_str}.mp3 ({os.path.getsize(new_mp3_path) / 1e6:.1f} MB)...")
        _upload_file_to_deploy(deploy_id, f"/episodes/{episode_date_str}.mp3", new_mp3_path)

    if feed_hash in required:
        logger.info("Uploading feed.xml...")
        _upload_bytes_to_deploy(deploy_id, "/feed.xml", feed_xml)

    for path, (h, data) in extra_hashes.items():
        if h in required:
            logger.info(f"Uploading {path}...")
            _upload_bytes_to_deploy(deploy_id, path, data)

    # Wait for deploy to go live
    _wait_for_deploy(deploy_id, "ready", timeout=300)

    site_url = _get_site_url()
    logger.info(f"Deploy complete: {site_url}")
    return new_mp3_hash, site_url


# ---------------------------------------------------------------------------
# Index Page
# ---------------------------------------------------------------------------

def generate_index_html(episodes: list[dict], site_base_url: str) -> bytes:
    """Generate a simple episode listing page."""
    podcast_config = load_config("podcast.yaml")["podcast"]
    show_name = podcast_config["name"]
    show_desc = podcast_config.get("description", "").strip()

    sorted_eps = sorted(episodes, key=lambda e: str(e["episode_date"]), reverse=True)

    rows = []
    for ep in sorted_eps:
        ep_date = str(ep["episode_date"])
        title = ep.get("episode_title") or f"{show_name} — {ep_date}"
        desc = ep.get("episode_description") or ""
        duration = ep.get("audio_duration") or ""
        size_mb = f'{(ep.get("audio_file_size") or 0) / 1e6:.1f} MB'

        rows.append(f"""      <tr>
        <td>{ep_date}</td>
        <td>
          <strong>{title}</strong>
          <br><small>{desc}</small>
        </td>
        <td>{duration}</td>
        <td>{size_mb}</td>
        <td>
          <a href="{site_base_url}/episodes/{ep_date}.mp3">MP3</a>
          &middot; <a href="{site_base_url}/digests/{ep_date}.json">Digest</a>
          &middot; <a href="{site_base_url}/scripts/{ep_date}.txt">Script</a>
        </td>
      </tr>""")

    table_rows = "\n".join(rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{show_name}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           max-width: 900px; margin: 0 auto; padding: 2rem 1rem; color: #1a1a1a; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .subtitle {{ color: #666; margin-bottom: 1.5rem; }}
    .feed-link {{ margin-bottom: 2rem; display: block; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 0.75rem 0.5rem; border-bottom: 1px solid #e0e0e0; }}
    th {{ font-size: 0.85rem; text-transform: uppercase; color: #888; }}
    td small {{ color: #666; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>{show_name}</h1>
  <p class="subtitle">{show_desc}</p>
  <a class="feed-link" href="{site_base_url}/feed.xml">RSS Feed</a>
  <table>
    <thead>
      <tr><th>Date</th><th>Episode</th><th>Duration</th><th>Size</th><th>Downloads</th></tr>
    </thead>
    <tbody>
{table_rows}
    </tbody>
  </table>
</body>
</html>"""
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def publish_episode(
    episode_id: str,
    episode_date_str: str,
    mp3_path: str,
    all_episodes: list[dict],
    published_episodes: list[dict],
    digest_json: dict | None = None,
    script_text: str | None = None,
) -> tuple[str, str]:
    """Publish episode: generate RSS, deploy to Netlify, return (cdn_url, hash).

    Args:
        episode_id: UUID of the current episode.
        episode_date_str: YYYY-MM-DD string.
        mp3_path: Local path to the MP3 file.
        all_episodes: All episodes to include in RSS (including current).
        published_episodes: Previously published episodes (for deploy manifest).
        digest_json: Digest dict to deploy as JSON file.
        script_text: Script text to deploy as text file.

    Returns:
        (audio_cdn_url, mp3_sha1_hash)
    """
    import json as _json

    site_url = _get_site_url()

    # Generate RSS feed and index page
    feed_xml = generate_rss_feed(all_episodes, site_url)
    index_html = generate_index_html(all_episodes, site_url)
    logger.info(f"RSS feed generated: {len(feed_xml)} bytes, {len(all_episodes)} episodes")

    # Build extra files: digests + scripts for ALL episodes (current + published)
    extra_files = {}

    # Current episode
    if digest_json is not None:
        extra_files[f"/digests/{episode_date_str}.json"] = _json.dumps(
            digest_json, indent=2
        ).encode("utf-8")
    if script_text is not None:
        extra_files[f"/scripts/{episode_date_str}.txt"] = script_text.encode("utf-8")

    # Previously published episodes
    for ep in published_episodes:
        ep_date = str(ep["episode_date"])
        if ep_date == episode_date_str:
            continue
        if ep.get("digest_json"):
            extra_files[f"/digests/{ep_date}.json"] = _json.dumps(
                ep["digest_json"], indent=2
            ).encode("utf-8")
        if ep.get("script_text"):
            extra_files[f"/scripts/{ep_date}.txt"] = ep["script_text"].encode("utf-8")

    # Index page
    extra_files["/index.html"] = index_html

    # Deploy to Netlify
    mp3_hash, deploy_url = deploy_to_netlify(
        new_mp3_path=mp3_path,
        episode_date_str=episode_date_str,
        feed_xml=feed_xml,
        published_episodes=published_episodes,
        extra_files=extra_files or None,
    )

    audio_cdn_url = f"{deploy_url}/episodes/{episode_date_str}.mp3"
    return audio_cdn_url, mp3_hash
