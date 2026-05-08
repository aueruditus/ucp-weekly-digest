#!/usr/bin/env python3
"""UCP Weekly Digest Pipeline — Main Orchestrator.

Runs the full pipeline: digest -> script -> audio -> publish.
Tracks state in the ucpweekly.episodes table for idempotency
and resume-on-failure.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import date, datetime

from dotenv import load_dotenv

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline")

from pipeline.digest import generate_digest
from pipeline.scriptwriter import generate_script, parse_script_to_segments
from pipeline.audio import (
    generate_podcast_audio,
    poll_until_complete,
    download_episode,
    WondercraftJobFailed,
)
from pipeline.db_ops import (
    get_episode,
    create_episode,
    update_episode_status,
    create_pipeline_run,
    complete_pipeline_run,
    update_pipeline_run,
    get_published_episodes,
)
from pipeline.publisher import publish_episode
from pipeline.utils import (
    load_voice_config,
    load_repos,
    load_podcast_meta,
    get_mp3_duration,
    ensure_output_dirs,
    generate_episode_description,
)

# Statuses that indicate a step has already been completed
COMPLETED_DIGEST = {"digest_generated", "script_generating", "script_generated",
                     "audio_submitting", "audio_submitted", "audio_polling",
                     "audio_complete", "publishing", "published"}
COMPLETED_SCRIPT = {"script_generated", "audio_submitting", "audio_submitted",
                     "audio_polling", "audio_complete", "publishing", "published"}
COMPLETED_AUDIO = {"audio_complete", "publishing", "published"}


def _should_run_step(existing: dict | None, completed_statuses: set) -> bool:
    """Check if a step needs to run based on current episode status."""
    if existing is None:
        return True
    return existing["status"] not in completed_statuses


def main():
    today = date.today()
    today_str = today.isoformat()
    run_trigger = os.environ.get("RUN_TRIGGER", "manual")
    run_env = os.environ.get("PIPELINE_ENVIRONMENT", "local")
    github_run_id = os.environ.get("GITHUB_RUN_ID")

    logger.info(f"=== UCP Weekly Digest Pipeline for {today_str} ===")
    ensure_output_dirs()

    # --- Idempotency check ---
    existing = get_episode(today)

    if existing and existing["status"] == "published":
        logger.info(f"Episode already published for {today_str}. Skipping.")
        return

    if existing and existing["status"] == "failed":
        logger.info(f"Retrying failed episode (failed at: {existing['error_step']})")
        # Restore the error_step as current status so resume logic skips completed steps
        error_step = existing.get("error_step", "pending")
        update_episode_status(str(existing["id"]), error_step)
        existing = get_episode(today)
        episode_id = str(existing["id"])
    elif existing:
        logger.info(f"Resuming episode in status: {existing['status']}")
        episode_id = str(existing["id"])
    else:
        episode_id = create_episode(today)
        logger.info(f"Created new episode: {episode_id}")

    run_id = create_pipeline_run(
        episode_id, today, run_trigger, run_env, github_run_id
    )

    total_in_tokens = 0
    total_out_tokens = 0

    try:
        # =============================================
        # Step 1: Generate Digest
        # =============================================
        if _should_run_step(existing, COMPLETED_DIGEST):
            logger.info("Step 1/4: Generating digest via Claude API + web search...")
            update_episode_status(episode_id, "digest_generating")
            update_pipeline_run(run_id, digest_started_at=datetime.utcnow().isoformat())

            repos = load_repos()
            digest, digest_usage = generate_digest(repos)
            total_in_tokens += digest_usage["input_tokens"]
            total_out_tokens += digest_usage["output_tokens"]

            # Save to file (wrapped with status metadata for the audit corpus)
            with open(f"output/digests/{today_str}.json", "w") as f:
                json.dump({
                    "episode_date": today_str,
                    "status": "digest_generated",
                    "digest": digest,
                }, f, indent=2)

            update_episode_status(
                episode_id,
                "digest_generated",
                digest_json=digest,
                digest_model=digest_usage["model"],
                digest_tokens_in=digest_usage["input_tokens"],
                digest_tokens_out=digest_usage["output_tokens"],
                digest_generated_at=datetime.utcnow().isoformat(),
            )
            update_pipeline_run(run_id, digest_completed_at=datetime.utcnow().isoformat())
            # Refresh existing to reflect new state
            existing = get_episode(today)
        else:
            digest = existing["digest_json"]
            logger.info("Step 1/4: Using existing digest from database.")
            # Ensure local file exists
            digest_path = f"output/digests/{today_str}.json"
            if not os.path.exists(digest_path):
                with open(digest_path, "w") as f:
                    json.dump({
                        "episode_date": today_str,
                        "status": existing["status"],
                        "digest": digest,
                    }, f, indent=2)
                logger.info(f"  Wrote missing digest file: {digest_path}")

        # =============================================
        # Step 2: Generate Podcast Script
        # =============================================
        if _should_run_step(existing, COMPLETED_SCRIPT):
            logger.info("Step 2/4: Generating podcast script via Claude API...")
            update_episode_status(episode_id, "script_generating")
            update_pipeline_run(run_id, script_started_at=datetime.utcnow().isoformat())

            script_text, script_usage = generate_script(digest)
            total_in_tokens += script_usage["input_tokens"]
            total_out_tokens += script_usage["output_tokens"]
            word_count = len(script_text.split())

            # Save to file
            with open(f"output/scripts/{today_str}.txt", "w") as f:
                f.write(script_text)

            update_episode_status(
                episode_id,
                "script_generated",
                script_text=script_text,
                script_word_count=word_count,
                script_model=script_usage["model"],
                script_tokens_in=script_usage["input_tokens"],
                script_tokens_out=script_usage["output_tokens"],
                script_generated_at=datetime.utcnow().isoformat(),
            )
            update_pipeline_run(run_id, script_completed_at=datetime.utcnow().isoformat())
            existing = get_episode(today)
        else:
            script_text = existing["script_text"]
            logger.info("Step 2/4: Using existing script from database.")
            # Ensure local file exists
            script_path = f"output/scripts/{today_str}.txt"
            if not os.path.exists(script_path):
                with open(script_path, "w") as f:
                    f.write(script_text)
                logger.info(f"  Wrote missing script file: {script_path}")

        # =============================================
        # Step 3: Generate Audio via Wondercraft
        # =============================================
        if _should_run_step(existing, COMPLETED_AUDIO):
            # Re-enter an in-progress Wondercraft job if one was submitted
            # on a prior run. Without this, a retry would abandon the old
            # job and submit a new one, wasting the first job's processing.
            existing_job_id = existing.get("audio_job_id") if existing else None
            existing_status = existing.get("status") if existing else None
            resuming_job = bool(existing_job_id) and existing_status in (
                "audio_submitted",
                "audio_polling",
            )

            if resuming_job:
                logger.info(
                    f"Step 3/4: Resuming poll of existing Wondercraft job {existing_job_id}"
                )
                job_id = existing_job_id
            else:
                logger.info("Step 3/4: Submitting to Wondercraft Convo Mode API...")
                update_episode_status(episode_id, "audio_submitting")
                update_pipeline_run(run_id, audio_started_at=datetime.utcnow().isoformat())

                voice_config = load_voice_config()
                segments = parse_script_to_segments(script_text, voice_config["voice_map"])

                job_id = generate_podcast_audio(
                    segments,
                    delivery_instructions=voice_config.get("delivery_instructions"),
                )

                update_episode_status(
                    episode_id,
                    "audio_submitted",
                    audio_job_id=job_id,
                    audio_submitted_at=datetime.utcnow().isoformat(),
                )

            logger.info(f"Polling for completion (~20-30 min expected)...")
            update_episode_status(episode_id, "audio_polling")
            try:
                result = poll_until_complete(job_id)
            except WondercraftJobFailed:
                # Clear the dead job_id so the next retry submits a fresh job
                # instead of re-polling the same permanently-failed one.
                logger.warning(
                    f"Clearing dead Wondercraft job_id {job_id} so retry submits fresh"
                )
                update_episode_status(episode_id, "audio_polling", audio_job_id=None)
                raise

            episode_path = f"output/episodes/{today_str}.mp3"
            download_url = result["url"]
            file_size = download_episode(download_url, episode_path)
            duration = get_mp3_duration(episode_path)

            update_episode_status(
                episode_id,
                "audio_complete",
                audio_download_url=download_url,
                audio_completed_at=datetime.utcnow().isoformat(),
                audio_file_path=episode_path,
                audio_duration=duration,
                audio_file_size=file_size,
            )
            update_pipeline_run(run_id, audio_completed_at=datetime.utcnow().isoformat())
            existing = get_episode(today)
        else:
            logger.info("Step 3/4: Audio already generated.")

        # =============================================
        # Step 4: Publish (Netlify + RSS)
        # =============================================
        logger.info("Step 4/4: Publishing to Netlify + generating RSS feed...")
        update_episode_status(episode_id, "publishing")
        update_pipeline_run(run_id, publish_started_at=datetime.utcnow().isoformat())

        show_name = load_podcast_meta().get("name", "UCP Weekly Digest")
        episode_title = f"{show_name} — {today_str}"
        description = generate_episode_description(digest)

        # Set title/description before RSS generation reads them
        update_episode_status(
            episode_id,
            "publishing",
            episode_title=episode_title,
            episode_description=description,
        )

        # Ensure MP3 is on disk (may be missing on ephemeral runners after restart)
        episode_path = existing.get("audio_file_path", f"output/episodes/{today_str}.mp3")
        if not os.path.exists(episode_path):
            logger.info("MP3 not on disk, re-downloading from Wondercraft...")
            dl_url = existing.get("audio_download_url")
            if dl_url:
                download_episode(dl_url, episode_path)
            else:
                raise FileNotFoundError(f"MP3 missing and no download URL: {episode_path}")

        # Refresh current episode data for RSS
        current_ep = get_episode(today)

        # Get previously published episodes for deploy manifest
        published_eps = get_published_episodes()

        # All episodes for RSS = previously published + current
        all_eps = list(published_eps) + [current_ep]

        audio_cdn_url, mp3_hash = publish_episode(
            episode_id=episode_id,
            episode_date_str=today_str,
            mp3_path=episode_path,
            all_episodes=all_eps,
            published_episodes=published_eps,
            digest_json=digest,
            script_text=script_text,
        )

        update_episode_status(
            episode_id,
            "published",
            audio_cdn_url=audio_cdn_url,
            audio_cdn_hash=mp3_hash,
            rss_published=True,
            published_at=datetime.utcnow().isoformat(),
        )

        # Refresh the audit-corpus digest file with final status
        with open(f"output/digests/{today_str}.json", "w") as f:
            json.dump({
                "episode_date": today_str,
                "status": "published",
                "digest": digest,
            }, f, indent=2)
        update_pipeline_run(
            run_id,
            publish_completed_at=datetime.utcnow().isoformat(),
        )

        complete_pipeline_run(
            run_id,
            "completed",
            total_input_tokens=total_in_tokens,
            total_output_tokens=total_out_tokens,
        )

        logger.info("=== Pipeline complete ===")

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)

        # Determine which step failed based on current status
        current = get_episode(today)
        error_step = current["status"] if current else "unknown"

        update_episode_status(
            episode_id,
            "failed",
            error_message=str(e)[:1000],
            error_step=error_step,
            retry_count=(existing["retry_count"] + 1 if existing else 1),
        )
        complete_pipeline_run(
            run_id,
            "failed",
            error_message=str(e)[:1000],
            error_step=error_step,
            error_traceback=traceback.format_exc()[:4000],
            total_input_tokens=total_in_tokens,
            total_output_tokens=total_out_tokens,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
