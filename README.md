# UCP Weekly Digest

A weekly podcast that summarises activity across the [Universal Commerce
Protocol](https://github.com/Universal-Commerce-Protocol) GitHub
organisation. Every Monday morning AEDT, an automated pipeline pulls a week
of merged PRs, releases, issues, and discussions from the eight UCP repos,
synthesises them into a developer-facing digest with Claude, and publishes
a ~20-minute two-host audio episode to an RSS feed via Wondercraft and
Netlify.

This project is a fork-in-spirit of [`aueruditus/daily-digest`](https://github.com/aueruditus/daily-digest).
Same pipeline (digest → script → audio → publish), same DB conventions —
the research step is swapped from web search to a deterministic GitHub
REST fetcher.

## Architecture

```
┌─────────────┐   ┌──────────────────┐   ┌─────────────┐   ┌──────────────┐
│   Sunday    │──▶│  github_fetch    │──▶│  digest.py  │──▶│ scriptwriter │
│  19:00 UTC  │   │ (GitHub REST,    │   │ (Claude     │   │   (Claude)   │
│ (GH Action) │   │  past 7 days)    │   │  synthesis) │   │              │
└─────────────┘   └──────────────────┘   └─────────────┘   └───────┬──────┘
                                                                    │
                          ┌─────────────────────────────────────────┘
                          ▼
                   ┌──────────────┐   ┌──────────────┐   ┌─────────────┐
                   │  audio.py    │──▶│ publisher.py │──▶│  Listeners  │
                   │ (Wondercraft │   │  (RSS feed,  │   │ (Apple, etc)│
                   │  Convo Mode) │   │   Netlify)   │   │             │
                   └──────────────┘   └──────────────┘   └─────────────┘
```

State is tracked in Postgres (`ucpweekly` schema) so failed runs resume
from the last completed step rather than starting over.

## Repos covered

| Repo | Group |
|------|-------|
| `ucp` | Spec & Schema |
| `ucp-schema` | Spec & Schema |
| `python-sdk` | Client SDKs |
| `js-sdk` | Client SDKs |
| `conformance` | Testing & Samples |
| `samples` | Testing & Samples |
| `meeting-minutes` | Governance & Community |
| `.github` | Governance & Community |

Edit `config/repos.yaml` (or the `ucpweekly.repo_config` table) to change
what's tracked.

## Local development

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in keys

# SSH tunnel to the Supabase instance (one-off, keep running):
ssh -L 54322:localhost:54322 iepwong@wong-home-ubuntu.local

# Apply migrations once:
psql "$DATABASE_URL" -f db/migrations/001_create_schema.sql

# Run the pipeline:
python pipeline/main.py
```

## Deployment checklist

The scheduled GitHub Action (`.github/workflows/weekly-podcast.yml`)
needs the following repo secrets:

| Secret | Purpose |
|--------|---------|
| `ANTHROPIC_API_KEY` | Claude API |
| `WONDERCRAFT_API_KEY` | Audio generation |
| `DATABASE_URL` | Postgres (`ucpweekly` schema) |
| `NETLIFY_AUTH_TOKEN` | Deploy MP3 + RSS |
| `NETLIFY_SITE_ID` | Target Netlify site |
| `NETLIFY_SITE_URL` | Public CDN base URL |
| `TS_AUTH_KEY` | Tailscale ephemeral key (to reach the self-hosted Postgres from the runner) |

`GITHUB_TOKEN` is provided by the runner — the workflow forwards it as
`GH_FETCH_TOKEN` so `github_fetch.py` can hit the REST API at the
authenticated rate limit.

See `CLAUDE.md` for project-internal notes (DB access, tunnel setup, MCP).

## License

TBD.
