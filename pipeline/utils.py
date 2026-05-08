"""Utility functions for the UCP weekly digest pipeline."""
from __future__ import annotations

import os
import sys
import time
import random
import functools
import logging

import yaml
from mutagen.mp3 import MP3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")


def load_config(filename: str) -> dict:
    """Load a YAML config file from the config/ directory."""
    path = os.path.join(CONFIG_DIR, filename)
    with open(path) as f:
        return yaml.safe_load(f)


def load_voice_config() -> dict:
    """Load voice config and return voice_map + delivery_instructions."""
    config = load_config("voices.yaml")
    voice_map = {
        "HOST_A": config["voices"]["host_a"]["voice_id"],
        "HOST_B": config["voices"]["host_b"]["voice_id"],
    }
    return {
        "voice_map": voice_map,
        "delivery_instructions": config.get("delivery_instructions"),
    }


def load_podcast_meta() -> dict:
    """Load the podcast.yaml metadata block (title, author, description, etc.)."""
    return load_config("podcast.yaml")["podcast"]


def load_repos_from_yaml() -> list[dict]:
    """Load repo list from config/repos.yaml.

    Returns a list of {owner, name, display, group} dicts.
    """
    config = load_config("repos.yaml")
    default_owner = config.get("owner", "")
    out = []
    for repo in config.get("repos", []):
        out.append(
            {
                "owner": repo.get("owner") or default_owner,
                "name": repo["name"],
                "display": repo.get("display") or repo["name"],
                "group": repo.get("group") or repo.get("display") or repo["name"],
            }
        )
    return out


def load_podcast_context() -> dict:
    """Load the editorial context from docs/podcast-context.yaml."""
    path = os.path.join(DOCS_DIR, "podcast-context.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def load_repos() -> list[dict]:
    """Load repos from database first, fall back to YAML."""
    try:
        from pipeline.db_ops import get_active_repos

        repos = get_active_repos()
        if repos:
            return repos
    except Exception as e:
        logger.warning(f"Could not load repos from database: {e}")
    return load_repos_from_yaml()


def get_mp3_duration(filepath: str) -> str:
    """Get MP3 duration as HH:MM:SS string."""
    audio = MP3(filepath)
    total_seconds = int(audio.info.length)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def ensure_output_dirs():
    """Create output directories if they don't exist."""
    for subdir in ("digests", "scripts", "episodes"):
        os.makedirs(os.path.join(OUTPUT_DIR, subdir), exist_ok=True)


def generate_episode_description(digest: dict) -> str:
    """Generate a short episode description from the digest topics."""
    topics = digest.get("topics", [])
    if not topics:
        return "This week across the UCP repos."

    topic_names = [t.get("topic_name", "") for t in topics if t.get("stories")]
    headlines = []
    for topic in topics:
        for story in topic.get("stories", [])[:1]:
            headlines.append(story.get("headline", ""))

    parts = ["This week's UCP highlights: "]
    parts.append("; ".join(h for h in headlines[:4] if h))
    if topic_names:
        parts.append(f". Covering: {', '.join(topic_names[:4])}.")
    return "".join(parts)


def retry_with_backoff(max_retries=3, initial_delay=1, backoff_factor=2, max_delay=60):
    """Decorator for retrying operations with exponential backoff."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries:
                        raise
                    logger.warning(
                        f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                        f"after error: {e}. Waiting {delay:.1f}s..."
                    )
                    time.sleep(delay + random.uniform(0, 1))
                    delay = min(delay * backoff_factor, max_delay)

        return wrapper

    return decorator
