-- Promoted entities. Identity is (country_code, source_id); geometry is typed,
-- valid, and stored in the canonical CRS. source_srid records the delivered CRS.
CREATE TABLE core.bess_site (
    country_code    char(2)     NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    source_id       text        NOT NULL,
    name            text        NOT NULL,
    capacity_mw     numeric(10, 2) CHECK (capacity_mw > 0),
    grid_distance_m numeric(10, 1) CHECK (grid_distance_m >= 0),
    source_date     date        NOT NULL,
    geom            geometry(MultiPolygon, 25833) NOT NULL CHECK (ST_IsValid(geom)),
    source_srid     integer     NOT NULL,
    content_hash    text        NOT NULL,
    promoted_at     timestamptz NOT NULL,
    promoted_by_run text        NOT NULL,
    PRIMARY KEY (country_code, source_id)
);
CREATE INDEX bess_site_geom_idx ON core.bess_site USING gist (geom);

CREATE TABLE core.peatland_parcel (
    country_code      char(2)     NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    source_id         text        NOT NULL,
    name              text        NOT NULL,
    degradation_class text        NOT NULL CHECK (degradation_class IN ('drained', 'degraded', 'near_natural')),
    peat_depth_m      numeric(6, 2) CHECK (peat_depth_m >= 0),
    source_date       date        NOT NULL,
    geom              geometry(MultiPolygon, 25833) NOT NULL CHECK (ST_IsValid(geom)),
    source_srid       integer     NOT NULL,
    content_hash      text        NOT NULL,
    promoted_at       timestamptz NOT NULL,
    promoted_by_run   text        NOT NULL,
    PRIMARY KEY (country_code, source_id)
);
CREATE INDEX peatland_parcel_geom_idx ON core.peatland_parcel USING gist (geom);
