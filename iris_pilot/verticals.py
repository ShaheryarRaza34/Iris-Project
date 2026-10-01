"""The two pilot verticals. Everything vertical-specific that the generic
promote/refresh/export code needs is declared here."""

from __future__ import annotations

from dataclasses import dataclass

# Canonical storage CRS for the pilot region (Brandenburg / western Poland).
# Metric, so ST_Area returns m2 directly.
CANONICAL_SRID = 25833
CANONICAL_CRS_LABEL = "EPSG:25833 (ETRS89 / UTM zone 33N)"

UNCERTAINTY_WORDING = (
    "Preliminary prospecting material. Figures, eco-point estimates and site suitability are "
    "indicative and based on available source data and commercial screening assumptions. The 8 "
    "eco-points/m2 factor is the current commercial baseline, not certified compensation. "
    "Ownership, planning, grid capacity, environmental eligibility and transferability remain "
    "subject to project-specific verification. No permit, reservation or construction readiness "
    "is represented."
)


@dataclass(frozen=True)
class Vertical:
    key: str
    title: str
    staging_table: str
    core_table: str
    view: str
    fixture_file: str
    # Vertical-specific attributes beyond the common ones, as (column, sql type).
    attributes: tuple[tuple[str, str], ...]
    # Extra promotion rejections, as (reason, SQL predicate over the staging row).
    rejections: tuple[tuple[str, str], ...] = ()

    @property
    def view_artifact(self) -> str:
        return self.view

    @property
    def export_artifact(self) -> str:
        return f"export:{self.key}"


BESS = Vertical(
    key="bess",
    title="BESS",
    staging_table="staging.bess_site",
    core_table="core.bess_site",
    view="mart.mv_bess_candidates",
    fixture_file="bess_site.csv",
    attributes=(("capacity_mw", "numeric"), ("grid_distance_m", "numeric")),
    rejections=(
        ("invalid_capacity", "capacity_mw IS NOT NULL AND capacity_mw <= 0"),
        ("invalid_grid_distance", "grid_distance_m IS NOT NULL AND grid_distance_m < 0"),
    ),
)

PEATLAND = Vertical(
    key="peatland",
    title="Peatland",
    staging_table="staging.peatland_parcel",
    core_table="core.peatland_parcel",
    view="mart.mv_peatland_candidates",
    fixture_file="peatland_parcel.csv",
    attributes=(("degradation_class", "text"), ("peat_depth_m", "numeric")),
    rejections=(
        ("invalid_degradation_class",
         "degradation_class IS NULL OR degradation_class NOT IN ('drained', 'degraded', 'near_natural')"),
        ("invalid_peat_depth", "peat_depth_m IS NOT NULL AND peat_depth_m < 0"),
    ),
)

VERTICALS: tuple[Vertical, ...] = (BESS, PEATLAND)
