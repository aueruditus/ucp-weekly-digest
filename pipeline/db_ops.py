"""Database operations for the UCP Weekly Digest pipeline.

All pipeline database writes go through this module, keeping SQL
isolated from the pipeline logic. Schema: ucpweekly.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.connection import get_db_cursor


def get_episode(episode_date: date) -> Optional[dict]:
    """Fetch an existing episode by date. Returns None if not found."""
    with get_db_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM ucpweekly.episodes WHERE episode_date = %s",
            (episode_date,),
        )
        return cur.fetchone()


def create_episode(episode_date: date) -> str:
    """Create a new episode record. Returns the episode UUID."""
    with get_db_cursor() as cur:
        cur.execute(
            """INSERT INTO ucpweekly.episodes (episode_date, status)
               VALUES (%s, 'pending')
               RETURNING id""",
            (episode_date,),
        )
        return str(cur.fetchone()["id"])


def update_episode_status(episode_id: str, status: str, **kwargs):
    """Update episode status and any additional fields."""
    set_clauses = ["status = %s"]
    values = [status]

    for key, value in kwargs.items():
        if key == "digest_json" and isinstance(value, dict):
            set_clauses.append(f"{key} = %s::jsonb")
            values.append(json.dumps(value))
        else:
            set_clauses.append(f"{key} = %s")
            values.append(value)

    values.append(episode_id)
    sql = f"UPDATE ucpweekly.episodes SET {', '.join(set_clauses)} WHERE id = %s"

    with get_db_cursor() as cur:
        cur.execute(sql, values)


def create_pipeline_run(
    episode_id: str,
    episode_date: date,
    run_trigger: str,
    run_environment: str,
    github_run_id: str = None,
) -> str:
    """Create a new pipeline run record. Returns the run UUID."""
    with get_db_cursor() as cur:
        cur.execute(
            """INSERT INTO ucpweekly.pipeline_runs
               (episode_id, episode_date, run_trigger, run_environment, github_run_id)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING id""",
            (episode_id, episode_date, run_trigger, run_environment, github_run_id),
        )
        return str(cur.fetchone()["id"])


def update_pipeline_run(run_id: str, **kwargs):
    """Update a pipeline run with arbitrary fields."""
    if not kwargs:
        return
    set_clauses = []
    values = []
    for key, value in kwargs.items():
        set_clauses.append(f"{key} = %s")
        values.append(value)
    values.append(run_id)
    sql = f"UPDATE ucpweekly.pipeline_runs SET {', '.join(set_clauses)} WHERE id = %s"
    with get_db_cursor() as cur:
        cur.execute(sql, values)


def complete_pipeline_run(
    run_id: str,
    status: str,
    error_message: str = None,
    error_step: str = None,
    error_traceback: str = None,
    total_input_tokens: int = None,
    total_output_tokens: int = None,
):
    """Mark a pipeline run as completed or failed."""
    with get_db_cursor() as cur:
        cur.execute(
            """UPDATE ucpweekly.pipeline_runs
               SET completed_at = now(), status = %s,
                   error_message = %s, error_step = %s, error_traceback = %s,
                   total_input_tokens = %s, total_output_tokens = %s
               WHERE id = %s""",
            (
                status,
                error_message,
                error_step,
                error_traceback,
                total_input_tokens,
                total_output_tokens,
                run_id,
            ),
        )


def get_published_episodes() -> list[dict]:
    """Fetch all episodes with CDN URLs for RSS and deploy manifest."""
    with get_db_cursor(commit=False) as cur:
        cur.execute(
            """SELECT episode_date, episode_title, episode_description,
                      audio_duration, audio_file_size, audio_cdn_url,
                      audio_cdn_hash, digest_json, script_text
               FROM ucpweekly.episodes
               WHERE status = 'published'
                 AND audio_cdn_url IS NOT NULL
                 AND rss_published = true
               ORDER BY episode_date DESC""",
        )
        return cur.fetchall()


def get_recent_digests(limit: int = 4) -> list[dict]:
    """Fetch digest JSON from the most recent episodes for dedup."""
    with get_db_cursor(commit=False) as cur:
        cur.execute(
            """SELECT episode_date, digest_json
               FROM ucpweekly.episodes
               WHERE digest_json IS NOT NULL
                 AND status NOT IN ('pending', 'failed')
               ORDER BY episode_date DESC
               LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()


def get_active_repos() -> list[dict]:
    """Fetch active repos from DB, ordered by sort_order.

    Returns a list of dicts shaped like the repos.yaml entries:
    {owner, name, display, group}
    """
    with get_db_cursor(commit=False) as cur:
        cur.execute(
            """SELECT owner, name, display_name, repo_group
               FROM ucpweekly.repo_config
               WHERE is_active = true
               ORDER BY sort_order""",
        )
        rows = cur.fetchall()
        return [
            {
                "owner": r["owner"],
                "name": r["name"],
                "display": r["display_name"] or r["name"],
                "group": r["repo_group"] or (r["display_name"] or r["name"]),
            }
            for r in rows
        ]
