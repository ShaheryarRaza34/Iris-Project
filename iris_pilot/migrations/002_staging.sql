-- Staging receives source rows as delivered, plus the review decision.
-- geom is deliberately untyped: CRS and validity are checked at promotion,
-- never fixed up silently.
CREATE TABLE staging.bess_site (
    country_code    char(2)     NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    source_id       text        NOT NULL,
    name            text        NOT NULL,
    capacity_mw     numeric(10, 2),
    grid_distance_m numeric(10, 1),
    source_date     date,
    review_status   text        NOT NULL CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    geom            geometry,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, source_id)
);

CREATE TABLE staging.peatland_parcel (
    country_code      char(2)     NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    source_id         text        NOT NULL,
    name              text        NOT NULL,
    degradation_class text,
    peat_depth_m      numeric(6, 2),
    source_date       date,
    review_status     text        NOT NULL CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    geom              geometry,
    loaded_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, source_id)
);
