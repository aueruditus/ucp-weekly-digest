"""Wondercraft API integration for audio generation.

Submits podcast scripts to Wondercraft's Convo Mode API,
polls for completion, and downloads the generated MP3.
"""
from __future__ import annotations

import json
import logging
import os
import time

import requests

from pipeline.utils import retry_with_backoff

logger = logging.getLogger(__name__)

WONDERCRAFT_BASE = "https://api.wondercraft.ai/v1"
WONDERCRAFT_KEY = os.environ.get("WONDERCRAFT_API_KEY", "")


class WondercraftJobFailed(RuntimeError):
    """Raised when Wondercraft reports a job as finished-with-error.

    Distinct from TimeoutError/network errors so callers can clear the
    stored job_id and submit a fresh job on retry, rather than re-polling
    a permanently-dead job.
    """


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-API-KEY": WONDERCRAFT_KEY,
    }


@retry_with_backoff(max_retries=2, initial_delay=5)
def generate_podcast_audio(
    script_segments: list[dict],
    delivery_instructions: str = None,
) -> str:
    """Submit script to Wondercraft Convo Mode and return job_id.

    Args:
        script_segments: List of {"text": str, "voice_id": str}.
        delivery_instructions: Optional vocal delivery style instructions.

    Returns:
        Wondercraft job ID string.
    """
    payload = {"script": script_segments}
    if delivery_instructions:
        payload["delivery_instructions"] = delivery_instructions

    logger.info(f"Submitting {len(script_segments)} segments to Wondercraft Convo Mode...")

    # Log unique voices for diagnostics
    unique_voices = {s["voice_id"] for s in script_segments}
    logger.info(f"Unique voice IDs in payload: {unique_voices}")
    logger.info(f"Payload keys: {list(payload.keys())}")

    response = requests.post(
        f"{WONDERCRAFT_BASE}/podcast/convo-mode/user-scripted",
        headers=_headers(),
        json=payload,
        timeout=60,
    )

    if response.status_code == 429:
        logger.warning("Wondercraft concurrent job limit hit (429). Waiting 5 min...")
        time.sleep(300)
        response = requests.post(
            f"{WONDERCRAFT_BASE}/podcast/convo-mode/user-scripted",
            headers=_headers(),
            json=payload,
            timeout=60,
        )

    if not response.ok:
        logger.error(f"Wondercraft API error {response.status_code}")
        logger.error(f"Response headers: {dict(response.headers)}")
        logger.error(f"Response body: {response.text[:2000]}")
        response.raise_for_status()
    job_id = response.json()["job_id"]
    logger.info(f"Wondercraft job submitted: {job_id}")
    return job_id


def poll_until_complete(
    job_id: str,
    poll_interval: int = 30,
    max_wait: int = 3000,
) -> dict:
    """Poll Wondercraft job status until complete or timeout.

    For Convo Mode, expect at least 1 minute of processing per minute
    of audio duration. A 20-minute episode may take 20-30 minutes.

    Returns:
        Job result dict with 'url' key (the download URL).
    """
    elapsed = 0
    logger.info(f"Polling Wondercraft job {job_id} (max {max_wait}s)...")

    network_errors = 0
    while elapsed < max_wait:
        try:
            response = requests.get(
                f"{WONDERCRAFT_BASE}/podcast/{job_id}",
                headers=_headers(),
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
            network_errors = 0  # Reset on success
        except (requests.ConnectionError, requests.Timeout) as e:
            network_errors += 1
            if network_errors > 5:
                raise
            logger.warning(f"Network error polling job (attempt {network_errors}/5): {e}")
            time.sleep(poll_interval)
            elapsed += poll_interval
            continue

        # API uses 'finished' (bool) not 'status'
        if result.get("finished"):
            if result.get("error"):
                logger.error(f"Wondercraft job failed. Full response: {json.dumps(result, indent=2)[:3000]}")
                raise WondercraftJobFailed(
                    f"Wondercraft job {job_id} failed: {result.get('error_details')}"
                )
            logger.info(f"Wondercraft job {job_id} completed after {elapsed}s")
            return result

        if elapsed % 120 == 0:
            logger.info(f"Wondercraft job {job_id}: processing, elapsed={elapsed}s")

        time.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(f"Wondercraft job {job_id} timed out after {max_wait}s")


def download_episode(download_url: str, output_path: str) -> int:
    """Download the generated MP3 from Wondercraft.

    Args:
        download_url: Wondercraft download URL (expires after 24h).
        output_path: Local file path to save the MP3.

    Returns:
        File size in bytes.
    """
    logger.info(f"Downloading episode to {output_path}...")
    response = requests.get(download_url, stream=True, timeout=120)
    response.raise_for_status()

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    file_size = os.path.getsize(output_path)
    logger.info(f"Episode downloaded: {file_size / 1024 / 1024:.1f} MB")
    return file_size
