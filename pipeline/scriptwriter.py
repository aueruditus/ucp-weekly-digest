"""Podcast script generation via Claude API.

Transforms a structured digest into a two-host conversational
podcast script for Wondercraft's Convo Mode.
"""
from __future__ import annotations

import logging
import os

import anthropic

from pipeline.utils import load_podcast_context, load_podcast_meta, retry_with_backoff

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")


def _build_script_system_prompt(context: dict) -> str:
    """Build the script system prompt with editorial context and recurring segments."""
    editorial = context.get("editorial_angle", "").strip()
    project = context.get("project_context", "").strip()
    audience = context.get("audience", "").strip()
    segments = context.get("recurring_segments", [])

    meta = load_podcast_meta()
    show_name = meta.get("name", "UCP Weekly Digest")

    segment_lines = []
    for seg in segments:
        segment_lines.append(f"- **{seg['name']}**: {seg.get('description', '').strip()}")
    segments_section = "\n".join(segment_lines) if segment_lines else ""

    audience_section = (
        f"AUDIENCE (internal — do not address them as 'the UCP community'):\n{audience}\n\n"
        if audience else ""
    )

    return f"""You are a podcast scriptwriter for "{show_name}", a weekly internal
briefing on activity in the Universal Commerce Protocol (UCP) GitHub
repositories. The show has two hosts:

- HOST A: The lead presenter. Senior platform engineer, Australian-inflected
  tone, comfortable with API design and protocol semantics. Speaks from
  practitioner experience as Circulr Tech's CTO.
- HOST B: The curious co-host. Asks smart follow-up questions, pushes back
  on hand-wavy explanations, and translates jargon for less-deep listeners.

{audience_section}ABOUT UCP (background — do not recite verbatim, audience already knows):
{project}

EDITORIAL PERSPECTIVE:
{editorial}

RECURRING SEGMENTS (include only when the digest has relevant material):
{segments_section}

SCRIPT REQUIREMENTS:
- HARD LENGTH CAP: 3,500 words maximum (approximately 20 minutes of audio).
  This is a strict limit — longer scripts cause audio generation to fail.
  Be selective: cover fewer stories rather than overshoot.
- Open with a brief intro referencing the week and the single most
  EP-impacting development of the week (or, if none, the headline change).
- Mix registers across the show: open strategic (for execs and PMs),
  go technical mid-show (architects and engineers), close with a
  "what's talkable this week" beat for sales.
- Cover 4-6 stories from the digest (not every story — be selective),
  grouped by topic. For each, end with one line of "so what does this
  mean for us" — explicit team impact.
- Close with the "What to Watch" segment (open PRs / draft proposals /
  upcoming council meetings) and a sign-off.
- Tone: a Monday-morning internal stand-up in podcast form. Two senior
  colleagues catching up. Australian references where natural. NEVER
  address the audience as "the UCP community" — they ARE Circulr Tech.
- When citing PRs, releases, or issues, mention the number and repo
  ("PR forty-two in ucp-schema") so listeners can find them.
- If a week is quiet, say so honestly in 30 seconds and use the rest
  of the show for "what to watch next week" — don't pad.

NATURAL SPEECH PATTERNS:
Write the dialogue with natural, human speech patterns. Include:
- Filled pauses: "um", "uh", "hmm", "ah"
- Reactive interjections: "oh wow", "right, right", "that's wild",
  "no way", "exactly"
- Self-corrections: "it was -- well, actually it was more like..."
- Laughter cues: "[laughs]", "[chuckles]"
- Trailing thoughts: "and the thing is..."
- Overlapping agreement: "yeah, yeah, exactly"
- Moments of genuine surprise or curiosity: "wait, really?",
  "hang on, say that again"
- Occasional verbal stumbles that a real host would make

Do NOT overdo it -- roughly 1 in 4 lines should contain a disfluency
or natural speech marker. The rest should flow cleanly. The goal is
to sound human, not to sound like you're stalling for time.

FORMAT:
Format the script as a simple alternating dialogue:
HOST_A: [dialogue]
HOST_B: [dialogue]
...

Do NOT include stage directions, sound effect cues, or music notes --
Wondercraft handles production elements separately."""


MAX_WORDS = 3500
OVERSHOOT_TOLERANCE = 200  # Accept up to MAX_WORDS + this before regenerating


@retry_with_backoff(max_retries=2, initial_delay=2)
def generate_script(digest: dict) -> tuple[str, dict]:
    """Generate a podcast script from a digest.

    Args:
        digest: Structured digest dict from generate_digest().

    Returns:
        (script_text, usage_metadata)
    """
    import json

    context = load_podcast_context()
    system_prompt = _build_script_system_prompt(context)

    client = anthropic.Anthropic()
    digest_json = json.dumps(digest, indent=2)

    logger.info(f"Generating podcast script via Claude ({CLAUDE_MODEL})...")

    user_prompt = (
        f"Here is this week's digest:\n\n{digest_json}\n\n"
        f"Generate a ~20-minute two-host podcast script. "
        f"Hard limit: {MAX_WORDS} words."
    )

    total_in = 0
    total_out = 0
    script_text = ""
    word_count = 0

    for attempt in range(2):
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        script_text = response.content[0].text
        word_count = len(script_text.split())
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens

        if word_count <= MAX_WORDS + OVERSHOOT_TOLERANCE:
            break

        logger.warning(
            f"Script overshoot ({word_count} words > {MAX_WORDS} cap), "
            f"regenerating with tighter instruction..."
        )
        user_prompt = (
            f"Here is this week's digest:\n\n{digest_json}\n\n"
            f"Generate a ~20-minute two-host podcast script. "
            f"CRITICAL: A prior attempt produced {word_count} words — far over "
            f"the {MAX_WORDS}-word cap that audio generation requires. "
            f"Cut aggressively: cover fewer stories, trim banter, tighten transitions. "
            f"Output MUST be at or under {MAX_WORDS} words."
        )

    usage = {
        "model": CLAUDE_MODEL,
        "input_tokens": total_in,
        "output_tokens": total_out,
    }

    logger.info(f"Script generated: {word_count} words")

    if word_count < 2500:
        logger.warning(f"Script may be too short ({word_count} words, target 3000-3500)")
    elif word_count > MAX_WORDS + OVERSHOOT_TOLERANCE:
        logger.warning(
            f"Script still over cap after regeneration ({word_count} words, "
            f"cap {MAX_WORDS}); proceeding anyway"
        )

    return script_text, usage


def parse_script_to_segments(script_text: str, voice_map: dict) -> list[dict]:
    """Parse HOST_A/HOST_B script into Wondercraft segments.

    Args:
        script_text: Raw script with "HOST_A: ..." and "HOST_B: ..." lines.
        voice_map: {"HOST_A": "voice_id_1", "HOST_B": "voice_id_2"}

    Returns:
        List of {"text": str, "voice_id": str} segments.
    """
    segments = []
    current_speaker = None
    current_text = []

    for line in script_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("HOST_A:"):
            if current_speaker and current_text:
                segments.append(
                    {
                        "text": " ".join(current_text),
                        "voice_id": voice_map[current_speaker],
                    }
                )
            current_speaker = "HOST_A"
            current_text = [line[len("HOST_A:") :].strip()]

        elif line.startswith("HOST_B:"):
            if current_speaker and current_text:
                segments.append(
                    {
                        "text": " ".join(current_text),
                        "voice_id": voice_map[current_speaker],
                    }
                )
            current_speaker = "HOST_B"
            current_text = [line[len("HOST_B:") :].strip()]

        else:
            current_text.append(line)

    # Final segment
    if current_speaker and current_text:
        segments.append(
            {
                "text": " ".join(current_text),
                "voice_id": voice_map[current_speaker],
            }
        )

    return segments
