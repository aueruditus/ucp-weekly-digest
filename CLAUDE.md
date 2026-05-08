# UCP Weekly Digest Project

A weekly podcast pipeline that summarises GitHub activity across the eight
public Universal Commerce Protocol repos and publishes a ~20-minute two-host
audio episode. Forked in spirit from `daily-digest`: same pipeline shape
(digest → script → audio → publish) and same DB conventions, with the
research step swapped from web search to a deterministic GitHub fetcher.

## Database Access

This project uses the same self-hosted Supabase instance as `daily-digest`,
but writes to its own schema: **`ucpweekly`**.

### MCP Connection (Preferred)

Use the `supabase-local` MCP server for all database operations.

**Prerequisites:** SSH tunnel must be active:
```bash
ssh -L 54321:localhost:54321 -L 54322:localhost:54322 -L 54323:localhost:54323 iepwong@wong-home-ubuntu.local
```

The MCP server connects via `postgresql://postgres:postgres@localhost:54322/postgres`.

### Fallback (psql)

```bash
psql "postgresql://postgres:postgres@localhost:54322/postgres" -c "YOUR SQL HERE"
```

### Important Notes

- Database is local/self-hosted. The official `mcp.supabase.com` MCP cannot reach it.
- Check tunnel: `lsof -i :54322`.
- This project's schema is `ucpweekly`. Daily-digest's is `dailydigest`. They
  share the same Postgres instance — keep queries scoped.

## Project Structure

- `pipeline/main.py` — Pipeline orchestrator (digest → script → audio → publish)
- `pipeline/digest.py` — Claude API digest synthesis (no web search; consumes pre-fetched GitHub data)
- `pipeline/github_fetch.py` — GitHub REST API fetcher (PRs, issues, releases per repo)
- `pipeline/scriptwriter.py` — Claude API for podcast script generation
- `pipeline/audio.py` — Wondercraft Convo Mode API integration
- `pipeline/publisher.py` — RSS feed + Netlify deploy
- `pipeline/db_ops.py` — All DB operations (schema: `ucpweekly`)
- `pipeline/utils.py` — Config loading, helpers
- `db/connection.py` — PostgreSQL connection helper
- `db/migrations/001_create_schema.sql` — Schema, tables, seed UCP repo list
- `config/repos.yaml` — Configurable repo list + thematic groups (fallback if DB unavailable)
- `config/voices.yaml` — Wondercraft voice IDs and delivery instructions
- `config/podcast.yaml` — Show metadata (RSS title, author, description, etc.)
- `docs/podcast-context.yaml` — Editorial context injected into Claude prompts

## Database Schema (`ucpweekly`)

- `episodes` — One row per episode date; digest JSON, script text, audio metadata, pipeline status
- `pipeline_runs` — Append-only execution log
- `repo_config` — Configurable repo list (8 UCP repos seeded)

## How the digest step works

1. `pipeline.utils.load_repos()` — list of `{owner, name, display, group}` from
   `repo_config` table (fallback: `config/repos.yaml`).
2. `pipeline.github_fetch.fetch_week_activity(repos)` — for each repo, pulls
   merged PRs, open PRs, issues opened/closed, and releases from the past
   7 days via the GitHub REST API.
3. `pipeline.digest.generate_digest(repos)` — sends the structured activity
   payload + editorial context + last 4 episodes (for dedup) to Claude.
   Claude returns the same digest JSON schema daily-digest produces, so
   the rest of the pipeline is unchanged.

## Running the Pipeline

```bash
source venv/bin/activate
python pipeline/main.py
```

Requires (see `.env.example`):
- `ANTHROPIC_API_KEY`
- `WONDERCRAFT_API_KEY`
- `GH_FETCH_TOKEN` (PAT for higher rate limits; in CI, the workflow uses `${{ secrets.GITHUB_TOKEN }}`)
- `DATABASE_URL` (with SSH tunnel active for local runs)
- Netlify creds for publish step

## Schedule

GitHub Action runs Sunday 19:00 UTC = Monday 06:00 AEDT (`.github/workflows/weekly-podcast.yml`).
