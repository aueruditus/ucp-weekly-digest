-- Migration: 001_create_schema.sql
-- Purpose: Create ucpweekly schema for the UCP Weekly Digest pipeline
-- Target: Self-hosted Supabase on wong-home-ubuntu (schema: ucpweekly)
--
-- Note: episode_date is the natural key. For a weekly podcast it represents
-- the date the episode was published (the cron runs Sun 19:00 UTC, so
-- episode_date will be that Sunday in UTC).

CREATE SCHEMA IF NOT EXISTS ucpweekly;

-- =============================================================================
-- Table: ucpweekly.episodes
-- One row per published episode. episode_date is unique.
-- =============================================================================
CREATE TABLE ucpweekly.episodes (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_date        date        NOT NULL UNIQUE,

    status              text        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN (
                                        'pending',
                                        'digest_generating',
                                        'digest_generated',
                                        'script_generating',
                                        'script_generated',
                                        'audio_submitting',
                                        'audio_submitted',
                                        'audio_polling',
                                        'audio_complete',
                                        'publishing',
                                        'published',
                                        'failed'
                                    )),

    digest_json         jsonb,
    digest_model        text,
    digest_tokens_in    integer,
    digest_tokens_out   integer,
    digest_generated_at timestamptz,

    script_text         text,
    script_word_count   integer,
    script_model        text,
    script_tokens_in    integer,
    script_tokens_out   integer,
    script_generated_at timestamptz,

    audio_job_id        text,
    audio_submitted_at  timestamptz,
    audio_download_url  text,
    audio_completed_at  timestamptz,

    audio_file_path     text,
    audio_cdn_url       text,
    audio_cdn_hash      text,
    audio_duration      text,
    audio_file_size     bigint,
    rss_published       boolean     NOT NULL DEFAULT false,
    published_at        timestamptz,

    episode_title       text,
    episode_description text,

    error_message       text,
    error_step          text,
    retry_count         integer     NOT NULL DEFAULT 0,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_ucpweekly_episodes_date ON ucpweekly.episodes (episode_date DESC);
CREATE INDEX idx_ucpweekly_episodes_status ON ucpweekly.episodes (status);

-- =============================================================================
-- Table: ucpweekly.pipeline_runs
-- Append-only execution log.
-- =============================================================================
CREATE TABLE ucpweekly.pipeline_runs (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id              uuid        REFERENCES ucpweekly.episodes(id),
    episode_date            date        NOT NULL,

    run_trigger             text,
    run_environment         text,
    github_run_id           text,

    started_at              timestamptz NOT NULL DEFAULT now(),
    completed_at            timestamptz,
    status                  text        NOT NULL DEFAULT 'running'
                                        CHECK (status IN ('running', 'completed', 'failed', 'skipped')),

    digest_started_at       timestamptz,
    digest_completed_at     timestamptz,
    script_started_at       timestamptz,
    script_completed_at     timestamptz,
    audio_started_at        timestamptz,
    audio_completed_at      timestamptz,
    publish_started_at      timestamptz,
    publish_completed_at    timestamptz,

    error_message           text,
    error_step              text,
    error_traceback         text,

    total_input_tokens      integer,
    total_output_tokens     integer,
    estimated_cost_usd      numeric(8,4),

    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_ucpweekly_runs_episode ON ucpweekly.pipeline_runs (episode_date DESC);
CREATE INDEX idx_ucpweekly_runs_status ON ucpweekly.pipeline_runs (status);

-- =============================================================================
-- Table: ucpweekly.repo_config
-- Configurable repo list (pipeline falls back to repos.yaml).
-- =============================================================================
CREATE TABLE ucpweekly.repo_config (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner           text        NOT NULL,
    name            text        NOT NULL,
    display_name    text,
    repo_group      text,
    is_active       boolean     NOT NULL DEFAULT true,
    sort_order      integer     NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, name)
);

-- =============================================================================
-- Trigger: auto-update updated_at on row changes
-- =============================================================================
CREATE OR REPLACE FUNCTION ucpweekly.update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ucpweekly_episodes_updated_at
    BEFORE UPDATE ON ucpweekly.episodes
    FOR EACH ROW
    EXECUTE FUNCTION ucpweekly.update_updated_at();

CREATE TRIGGER ucpweekly_repo_config_updated_at
    BEFORE UPDATE ON ucpweekly.repo_config
    FOR EACH ROW
    EXECUTE FUNCTION ucpweekly.update_updated_at();

-- =============================================================================
-- Seed: the 8 UCP public repos
-- =============================================================================
INSERT INTO ucpweekly.repo_config (owner, name, display_name, repo_group, sort_order) VALUES
    ('Universal-Commerce-Protocol', 'ucp',             'Spec & Documentation',       'Spec & Schema',           1),
    ('Universal-Commerce-Protocol', 'ucp-schema',      'Schema Validator',           'Spec & Schema',           2),
    ('Universal-Commerce-Protocol', 'python-sdk',      'Python SDK',                 'Client SDKs',             3),
    ('Universal-Commerce-Protocol', 'js-sdk',          'JavaScript SDK',             'Client SDKs',             4),
    ('Universal-Commerce-Protocol', 'conformance',     'Conformance Tests',          'Testing & Samples',       5),
    ('Universal-Commerce-Protocol', 'samples',         'Samples',                    'Testing & Samples',       6),
    ('Universal-Commerce-Protocol', 'meeting-minutes', 'Governance Meeting Minutes', 'Governance & Community',  7),
    ('Universal-Commerce-Protocol', '.github',         'Org Configuration',          'Governance & Community',  8);
