-- Candidate screening views. Created WITH NO DATA so a fresh database shows
-- them as never built until the orchestrator refreshes them. Screening is
-- deliberately simple and explainable: each row carries a band and the rule
-- that put it there. Missing source attributes are reported, not assumed.

CREATE MATERIALIZED VIEW mart.mv_bess_candidates AS
SELECT
    s.country_code,
    s.source_id,
    s.name,
    s.geom,
    round(ST_Area(s.geom))::bigint AS area_m2,
    s.capacity_mw,
    s.grid_distance_m,
    s.source_date,
    s.source_srid,
    CASE
        WHEN s.grid_distance_m IS NULL THEN 'review'
        WHEN s.grid_distance_m <= 2000 AND ST_Area(s.geom) >= 10000 THEN 'shortlist'
        WHEN s.grid_distance_m <= 5000 THEN 'review'
        ELSE 'deprioritise'
    END AS screening_band,
    CASE
        WHEN s.grid_distance_m IS NULL THEN 'grid distance not in source - not assumed'
        WHEN s.grid_distance_m <= 2000 AND ST_Area(s.geom) >= 10000 THEN 'grid <= 2 km and area >= 1 ha'
        WHEN s.grid_distance_m <= 2000 THEN 'grid <= 2 km but area < 1 ha'
        WHEN s.grid_distance_m <= 5000 THEN 'grid 2-5 km'
        ELSE 'grid > 5 km'
    END AS screening_reason
FROM core.bess_site s
WITH NO DATA;

CREATE UNIQUE INDEX mv_bess_candidates_pk ON mart.mv_bess_candidates (country_code, source_id);

CREATE MATERIALIZED VIEW mart.mv_peatland_candidates AS
SELECT
    p.country_code,
    p.source_id,
    p.name,
    p.geom,
    round(ST_Area(p.geom))::bigint AS area_m2,
    p.degradation_class,
    p.peat_depth_m,
    p.source_date,
    p.source_srid,
    8 AS eco_point_factor_per_m2,
    round(ST_Area(p.geom))::bigint * 8 AS eco_points_indicative,
    CASE
        WHEN p.degradation_class = 'near_natural' THEN 'deprioritise'
        WHEN p.peat_depth_m IS NULL THEN 'review'
        WHEN ST_Area(p.geom) >= 50000 AND p.peat_depth_m >= 0.3 THEN 'shortlist'
        WHEN ST_Area(p.geom) >= 10000 THEN 'review'
        ELSE 'deprioritise'
    END AS screening_band,
    CASE
        WHEN p.degradation_class = 'near_natural' THEN 'near-natural peat - limited rewetting uplift'
        WHEN p.peat_depth_m IS NULL THEN 'peat depth not in source - not assumed'
        WHEN ST_Area(p.geom) >= 50000 AND p.peat_depth_m >= 0.3 THEN 'drained/degraded, area >= 5 ha, depth >= 0.3 m'
        WHEN ST_Area(p.geom) >= 10000 THEN 'drained/degraded, area 1-5 ha or depth < 0.3 m'
        ELSE 'area < 1 ha'
    END AS screening_reason
FROM core.peatland_parcel p
WITH NO DATA;

CREATE UNIQUE INDEX mv_peatland_candidates_pk ON mart.mv_peatland_candidates (country_code, source_id);
