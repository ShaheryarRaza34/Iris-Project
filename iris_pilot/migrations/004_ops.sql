-- Freshness bookkeeping. dataset_version is bumped by promotion only when
-- promoted content actually changes; every derived artifact records the
-- version it was built from. Stale = built_from_version < current version.
CREATE TABLE ops.dataset_version (
    vertical       text PRIMARY KEY,
    version        bigint      NOT NULL DEFAULT 0,
    changed_at     timestamptz,
    changed_by_run text
);

CREATE TABLE ops.artifact_state (
    artifact            text PRIMARY KEY,
    vertical            text NOT NULL REFERENCES ops.dataset_version (vertical),
    kind                text NOT NULL CHECK (kind IN ('materialized_view', 'export')),
    built_from_version  bigint,
    built_at            timestamptz,
    built_by_run        text,
    row_count           bigint,
    location            text,
    last_attempt_at     timestamptz,
    last_attempt_run    text,
    last_attempt_status text CHECK (last_attempt_status IN ('succeeded', 'failed')),
    last_error          text
);

INSERT INTO ops.dataset_version (vertical) VALUES ('bess'), ('peatland');

INSERT INTO ops.artifact_state (artifact, vertical, kind) VALUES
    ('mart.mv_bess_candidates',     'bess',     'materialized_view'),
    ('mart.mv_peatland_candidates', 'peatland', 'materialized_view'),
    ('export:bess',                 'bess',     'export'),
    ('export:peatland',             'peatland', 'export');
