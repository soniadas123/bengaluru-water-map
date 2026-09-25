"""
Build a self-contained interactive water map of Bengaluru.

Reads the GeoJSON files in data/, simplifies and cleans them with GeoPandas,
then injects the result into template.html to produce a single index.html that
can be opened by double-clicking -- no web server, no Mapbox account.

Run:  python build_map.py                    writes index.html
      python build_map.py next/index.html    writes the page somewhere else
"""

import json
import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
TEMPLATE = BASE / "template.html"
# The page to write; a path on the command line, relative to this folder, overrides it.
OUTPUT = BASE / (sys.argv[1] if len(sys.argv) > 1 else "index.html")

# UTM 43N. Bengaluru sits in this zone, so simplification tolerances and any
# measurements are in real metres rather than degrees.
METRIC_CRS = 32643

# 5 m is below one screen pixel at city zoom and imperceptible at street zoom.
SIMPLIFY_M = 5

# 6 decimal places of longitude/latitude is roughly 0.1 m. The source files
# store 15, which is most of the file size.
COORD_DECIMALS = 6

SQ_M_PER_ACRE = 4046.8564224

# A lake piece inside a ward smaller than this is a sliver where the lake and
# ward outlines disagree slightly, not real lake area, so it is left out.
MIN_ACRES = 0.1

# A drain piece inside a ward shorter than this only touches the ward edge, so
# it does not count as crossing the ward.
MIN_DRAIN_M = 20

# The report's key map shows the whole city in a small box, so the ward
# outlines can be much coarser than on the main map.
KEYMAP_SIMPLIFY_M = 60
KEYMAP_DECIMALS = 4

# Strings the source data uses to mean "no value".
EMPTY_VALUES = {"", "None", "nan", "NaN", "<NA>", "null"}

# 995 of the 2224 secondary drains have a "Drain num" of "unknown0", "unknown1"
# and so on. That is an auto-generated placeholder, not a name, so treat it as
# missing. The trailing digits are required: a bare "Unknown" is the recorded
# name of 21 lost lakes and must survive.
PLACEHOLDER_PATTERN = re.compile(r"^unknown\s*\d+$", re.IGNORECASE)

# The same lake status written two different ways.
STATUS_FIXES = {
    "Developed lake": "Developed Lake",
    "Work in progress": "Work in Progress",
}

# name, filename, columns to keep (after renaming), polygon?
LAYERS = [
    (
        "gba_boundary",
        "mod-foundation_gba_boundary (1).geojson",
        ["Name"],
        True,
    ),
    (
        "gba_corporations",
        "mod-foundation_gba_corporations (2).geojson",
        ["Corporation"],
        True,
    ),
    (
        "gba_wards",
        "mod-foundation_gba_wards (1).geojson",
        ["ward_name", "Corporation", "valley", "subvalley", "Assembly"],
        True,
    ),
    (
        "lakes_existing",
        "mod-foundation_lakes_existing (1).geojson",
        ["name", "area_acres", "status", "custodian", "Category",
         "valley", "subvalley", "ward_name", "Corporation"],
        True,
    ),
    (
        "lakes_lost",
        "mod-foundation_lakes_lost (1).geojson",
        ["name", "area_acres", "year", "current_use",
         "valley", "subvalley", "ward_name", "Corporation"],
        True,
    ),
    (
        "primarydrains",
        "mod-foundation_primarydrains.geojson",
        ["Drain num", "length_m", "valley", "subvalley", "ward_name", "Corporation"],
        False,
    ),
    (
        "secondarydrains",
        "mod-foundation_secondarydrains.geojson",
        ["Drain num", "sec_id", "pri_Drain num", "length_m",
         "valley", "subvalley", "ward_name", "Corporation"],
        False,
    ),
]


# Citizen audit forms exported from KoboToolbox: name, filename, and the
# columns to keep, mapped to the name used on the map. Form 3 (resident
# interviews) has no coordinates, so it is not mapped.
AUDITS = [
    (
        "audit_structure",
        "mod-foundation_form-1 (1).csv",
        {
            "wall_condition": "wall_condition", "wall_height": "wall_height",
            "fence": "fence", "wall_material": "wall_material",
            "bridge_type": "bridge_type", "bridge_condition": "bridge_condition",
            "elec_condition": "elec_condition", "cables_condition": "cables_condition",
            "manholes_condition": "manholes_condition",
            "team_name": "team", "_drain": "drain",
            "_secondarydrain": "secondary_drain", "date_time": "date",
            "rhs_lhs": "side",
        },
    ),
    (
        "audit_water",
        "mod-foundation_form-2.csv",
        {
            "water_stagnant": "water_stagnant", "water_colour": "water_colour",
            "water_turbidity": "water_turbidity", "water_smell": "water_smell",
            "water_contamination": "water_contamination", "inlets": "inlets",
            "unauthorised_inlets": "unauthorised_inlets",
            "sw_inside": "sw_inside", "sw_inside_type": "sw_inside_type",
            "sw_outside": "sw_outside",
            "community_engagement": "community_engagement",
            "_team_name": "team", "_drain": "drain",
            "_drain_secondary": "secondary_drain", "start": "date",
            "rhs_lhs": "side",
        },
    ),
]

# The same audit answer written in different cases across form versions.
AUDIT_FIXES = {
    "Cannot See": "Cannot see",
    "Cannot see or smell": "Cannot see",
    "Froth Or Foam Visible": "Froth or foam visible",
    "Solid Particles Or Oily Film On Water": "Solid particles or oily film on water",
    "Commercial Mostly": "Commercial (mostly)",
    "Strong Smell Not Necessarily Sewage": "Strong smell, not necessarily sewage",
    "yes": "Yes",
    "no": "No",
    "rhs": "Right hand side (RHS)",
    "lhs": "Left hand side (LHS)",
}


def round_coords(obj, decimals=COORD_DECIMALS):
    """Recursively round every float in a nested coordinate list."""
    if isinstance(obj, list):
        return [round_coords(item, decimals) for item in obj]
    if isinstance(obj, float):
        return round(obj, decimals)
    return obj


def clean_properties(props):
    """Strip whitespace, drop placeholder values, shorten long numbers."""
    cleaned = {}
    for key, value in props.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if value in EMPTY_VALUES or PLACEHOLDER_PATTERN.match(value):
                continue
        elif isinstance(value, float):
            if value != value:  # NaN
                continue
            value = round(value, 2)
        cleaned[key] = value
    return cleaned


def load_wards():
    """Ward polygons in the metric CRS, used to fill gaps in other layers."""
    wards = gpd.read_file(DATA_DIR / "mod-foundation_gba_wards (1).geojson")
    return wards[["ward_name", "Corporation", "geometry"]].to_crs(METRIC_CRS)


def fill_from_wards(gdf, wards):
    """Recover a missing ward or corporation by locating the feature."""
    gaps = gdf["ward_name"].isna() | gdf["Corporation"].isna()
    if not gaps.any():
        return gdf

    points = gdf.loc[gaps, ["geometry"]].to_crs(METRIC_CRS)
    points["geometry"] = points.representative_point()
    located = gpd.sjoin(points, wards, how="left", predicate="within")

    for column in ("ward_name", "Corporation"):
        gdf.loc[gaps, column] = gdf.loc[gaps, column].fillna(located[column])

    still_missing = int((gdf["ward_name"].isna() | gdf["Corporation"].isna()).sum())
    print(f"    located {int(gaps.sum()) - still_missing} of {int(gaps.sum())} "
          f"feature(s) with a missing ward or corporation")
    return gdf


def prepare_layer(name, filename, keep, is_polygon, wards):
    gdf = gpd.read_file(DATA_DIR / filename)

    # The corporation column is spelled differently across files.
    if "corporatio" in gdf.columns:
        gdf = gdf.rename(columns={"corporatio": "Corporation"})

    if name == "lakes_lost":
        # This column is not a status -- it describes what now sits on the
        # lakebed, so give it an honest name.
        gdf = gdf.rename(columns={"Status": "current_use"})

    if name == "lakes_existing":
        gdf["status"] = gdf["status"].replace(STATUS_FIXES)

    # A few features have no ward or corporation recorded. Locate those inside
    # the ward polygons so the filters do not silently drop them.
    if "Corporation" in gdf.columns and "ward_name" in gdf.columns:
        gdf = fill_from_wards(gdf, wards)

    columns = [c for c in keep if c in gdf.columns]
    gdf = gdf[columns + ["geometry"]]

    # Simplify in metres, then repair any self-intersection simplification made.
    gdf = gdf.to_crs(METRIC_CRS)
    geometry = gdf.geometry.simplify(SIMPLIFY_M)
    if is_polygon:
        geometry = geometry.buffer(0)
    gdf = gdf.set_geometry(geometry).to_crs(4326)

    collection = json.loads(gdf.to_json(drop_id=True))
    for feature in collection["features"]:
        feature["properties"] = clean_properties(feature["properties"])
        feature["geometry"]["coordinates"] = round_coords(
            feature["geometry"]["coordinates"]
        )

    print(f"  {name:18s} {len(collection['features']):5d} features")
    return collection


def prepare_audit(name, filename, columns, wards):
    """Validated citizen audit submissions as a GeoJSON point collection."""
    # Kobo exports these files in the Windows code page, not UTF-8.
    df = pd.read_csv(DATA_DIR / filename, encoding="cp1252", dtype=str)

    # Only submissions the team marked as checked. A copy of the header row sits
    # inside the data; its lat/long are text, so the number check drops it too.
    lat = pd.to_numeric(df["lat"], errors="coerce")
    lon = pd.to_numeric(df["long"], errors="coerce")
    keep = (df["_validation_status"].str.strip() == "yes") & \
        lat.between(12.6, 13.4) & lon.between(77.2, 78.0)
    df = df[keep]

    df = df[list(columns)].rename(columns=columns)
    for column in df.columns:
        df[column] = (df[column].str.strip()
                      .str.replace(r"\s+", " ", regex=True)
                      .replace(AUDIT_FIXES))
    # Keep just the day: form 1 stores "2026/02/22 11:04:00+0530", form 2 an ISO time.
    df["date"] = df["date"].str[:10].str.replace("/", "-")

    points = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(lon[keep], lat[keep]), crs=4326
    )
    located = gpd.sjoin(points.to_crs(METRIC_CRS), wards[["ward_name", "geometry"]],
                        how="left", predicate="within")
    # A point exactly on a shared ward edge can match two wards; keep the first.
    located = located[~located.index.duplicated()]
    points["ward_name"] = located["ward_name"]

    collection = json.loads(points.to_json(drop_id=True))
    for feature in collection["features"]:
        feature["properties"] = clean_properties(feature["properties"])
        feature["geometry"]["coordinates"] = round_coords(
            feature["geometry"]["coordinates"]
        )

    print(f"  {name:18s} {len(collection['features']):5d} validated points")
    return collection


def lake_area_by_ward(name, filename, wards):
    """Area of each lake that lies inside each ward, in acres.

    Returns {ward_name: [{"i": row, "acres": area}, ...]}, largest first. "i" is
    the lake's position in DATA[name].features, since prepare_layer keeps every
    row in file order.
    """
    lakes = gpd.read_file(DATA_DIR / filename).to_crs(METRIC_CRS)
    lakes["i"] = range(len(lakes))
    lakes["geometry"] = lakes.geometry.make_valid()

    wards = wards[["ward_name", "geometry"]].copy()
    wards["geometry"] = wards.geometry.make_valid()

    pieces = gpd.overlay(lakes[["i", "geometry"]], wards, how="intersection",
                         keep_geom_type=True)
    pieces["acres"] = pieces.geometry.area / SQ_M_PER_ACRE
    kept = pieces[pieces["acres"] >= MIN_ACRES]

    by_ward = {}
    for row in kept.itertuples():
        # A lake cut into several parts by the ward edge adds up per ward.
        entries = by_ward.setdefault(row.ward_name.strip(), {})
        entries[row.i] = entries.get(row.i, 0) + row.acres
    result = {
        ward: [{"i": int(i), "acres": round(acres, 2)}
               for i, acres in sorted(entries.items(), key=lambda e: -e[1])]
        for ward, entries in by_ward.items()
    }

    split = int((kept.groupby("i")["ward_name"].nunique() > 1).sum())
    print(f"  {name:18s} {len(kept):5d} lake-in-ward pieces kept, "
          f"{len(pieces) - len(kept)} under {MIN_ACRES} acres dropped, "
          f"{split} lakes split across wards")
    return result


def is_drain_name(value):
    """False for a missing name or an "unknown12" style placeholder."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    return bool(value) and value not in EMPTY_VALUES and \
        not PLACEHOLDER_PATTERN.match(value)


def drains_by_ward(name, filename, wards):
    """Drains that cross each ward, with the length inside the ward in metres.

    Returns {ward_name: {"named": [{"name", "m", "feeds"?}, ...], "unnamed_m": m}},
    named drains longest first. "feeds" is the primary drain a secondary drain
    flows into, when recorded. The source files store most drains as many short
    pieces (the median secondary piece is 7 m), so pieces are added up per drain
    name before the MIN_DRAIN_M cut-off applies, and drains with no name are only
    given as a total length, since counting their pieces would mean nothing.
    """
    drains = gpd.read_file(DATA_DIR / filename).to_crs(METRIC_CRS)
    if "pri_Drain num" not in drains.columns:
        drains["pri_Drain num"] = None
    drains = drains[["Drain num", "pri_Drain num", "geometry"]]

    pieces = gpd.overlay(drains, wards[["ward_name", "geometry"]],
                         how="intersection", keep_geom_type=True)
    pieces["m"] = pieces.geometry.length
    pieces["ward_name"] = pieces["ward_name"].str.strip()
    is_named = pieces["Drain num"].apply(is_drain_name)
    pieces["Drain num"] = pieces["Drain num"].where(is_named).str.strip()
    pieces["feeds"] = pieces["pri_Drain num"].where(
        pieces["pri_Drain num"].apply(is_drain_name)).str.strip()

    named = pieces[is_named].groupby(["ward_name", "Drain num"], as_index=False).agg(
        m=("m", "sum"), feeds=("feeds", "first")).rename(columns={"Drain num": "drain"})
    named = named[named["m"] >= MIN_DRAIN_M].sort_values("m", ascending=False)
    unnamed = pieces[~is_named].groupby("ward_name")["m"].sum()
    unnamed = unnamed[unnamed >= MIN_DRAIN_M]

    result = {}
    for row in named.itertuples():
        entry = {"name": row.drain, "m": round(row.m)}
        if isinstance(row.feeds, str):
            entry["feeds"] = row.feeds
        ward = result.setdefault(row.ward_name, {"named": [], "unnamed_m": 0})
        ward["named"].append(entry)
    for ward_name, metres in unnamed.items():
        result.setdefault(ward_name, {"named": [], "unnamed_m": 0})
        result[ward_name]["unnamed_m"] = round(metres)

    print(f"  {name:18s} {len(named):5d} named drain-in-ward entries, "
          f"{len(unnamed)} wards with unnamed drains")
    return result


def ward_stats(wards, ward_lakes, ward_drains):
    """Each ward's headline figures and how it compares with the other wards.

    Ranks count only wards that have any of the thing being ranked, so a ward
    is never "ahead" of wards that simply have none. Drains are compared per
    square km, as bigger wards would otherwise always come out on top.
    Citizen audits are not ranked: a ward with no audits was not checked,
    which says nothing about its drains.
    """
    stats = pd.DataFrame({"ward_name": wards["ward_name"].str.strip(),
                          "area_km2": wards.geometry.area / 1e6})

    def lake_acres(layer):
        return stats["ward_name"].map(
            lambda ward: sum(lake["acres"] for lake in ward_lakes[layer].get(ward, [])))

    def drain_km(layer):
        return stats["ward_name"].map(
            lambda ward: (sum(d["m"] for d in ward_drains[layer].get(ward, {}).get("named", [])) +
                          ward_drains[layer].get(ward, {}).get("unnamed_m", 0)) / 1000)

    stats["existing_acres"] = lake_acres("lakes_existing")
    stats["lost_acres"] = lake_acres("lakes_lost")
    stats["primary_km"] = drain_km("primarydrains")
    stats["secondary_km"] = drain_km("secondarydrains")
    stats["drain_density"] = (stats["primary_km"] + stats["secondary_km"]) / stats["area_km2"]

    result = {}
    lost_of = int((stats["lost_acres"] > 0).sum())
    existing_of = int((stats["existing_acres"] > 0).sum())
    for row in stats.itertuples():
        entry = {
            "area_km2": round(row.area_km2, 2),
            "existing_acres": round(row.existing_acres, 2),
            "lost_acres": round(row.lost_acres, 2),
            "primary_km": round(row.primary_km, 2),
            "secondary_km": round(row.secondary_km, 2),
            # Share of wards with less drain per sq km than this one.
            "drain_pct": round(100 * float((stats["drain_density"] < row.drain_density).mean())),
        }
        total_water = row.existing_acres + row.lost_acres
        if total_water > 0:
            entry["lost_share"] = round(100 * row.lost_acres / total_water)
        if row.lost_acres > 0:
            entry["lost_rank"] = int((stats["lost_acres"] > row.lost_acres).sum()) + 1
        if row.existing_acres > 0:
            entry["existing_rank"] = int((stats["existing_acres"] > row.existing_acres).sum()) + 1
        result[row.ward_name] = entry

    result["_city"] = {
        "wards": len(stats),
        "lost_of": lost_of,
        "existing_of": existing_of,
        "lost_acres": round(float(stats["lost_acres"].sum()), 1),
        "existing_acres": round(float(stats["existing_acres"].sum()), 1),
    }
    print(f"  {'ward_stats':18s} {len(stats):5d} wards, {lost_of} lost water bodies, "
          f"{existing_of} have existing ones")
    return result


def keymap_wards(wards):
    """Coarse ward outlines for the small key map in the report."""
    coarse = wards[["ward_name", "geometry"]].copy()
    coarse["ward_name"] = coarse["ward_name"].str.strip()
    coarse["geometry"] = coarse.geometry.simplify(KEYMAP_SIMPLIFY_M).buffer(0)
    coarse = coarse.to_crs(4326)

    collection = json.loads(coarse.to_json(drop_id=True))
    for feature in collection["features"]:
        feature["geometry"]["coordinates"] = round_coords(
            feature["geometry"]["coordinates"], KEYMAP_DECIMALS
        )
    print(f"  {'keymap_wards':18s} {len(collection['features']):5d} coarse outlines")
    return collection


def main():
    print("Preparing layers")
    wards = load_wards()
    layers = {
        name: prepare_layer(name, filename, keep, is_polygon, wards)
        for name, filename, keep, is_polygon in LAYERS
    }

    print("Preparing citizen audits")
    for name, filename, columns in AUDITS:
        layers[name] = prepare_audit(name, filename, columns, wards)

    print("Measuring lake area inside each ward")
    layers["ward_lakes"] = {
        name: lake_area_by_ward(name, filename, wards)
        for name, filename, _, _ in LAYERS
        if name in ("lakes_existing", "lakes_lost")
    }

    print("Finding drains crossing each ward")
    layers["ward_drains"] = {
        name: drains_by_ward(name, filename, wards)
        for name, filename, _, _ in LAYERS
        if name in ("primarydrains", "secondarydrains")
    }

    print("Comparing wards")
    layers["ward_stats"] = ward_stats(wards, layers["ward_lakes"], layers["ward_drains"])

    print("Preparing key map")
    layers["keymap_wards"] = keymap_wards(wards)

    payload = json.dumps(layers, separators=(",", ":"), ensure_ascii=False)
    # Stop any stray "</script>" in the data from closing the script tag early.
    payload = payload.replace("</", r"<\/")

    template = TEMPLATE.read_text(encoding="utf-8")
    if "/*__DATA__*/" not in template:
        raise SystemExit("template.html is missing the /*__DATA__*/ placeholder")
    html = template.replace("/*__DATA__*/", f"const DATA = {payload};")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")

    size_mb = OUTPUT.stat().st_size / 1048576
    print(f"\nWrote {OUTPUT.relative_to(BASE)} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
