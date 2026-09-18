"""
Build a self-contained interactive water map of Bengaluru.

Reads the GeoJSON files in data/, simplifies and cleans them with GeoPandas,
then injects the result into template.html to produce a single index.html that
can be opened by double-clicking -- no web server, no Mapbox account.

Run:  python build_map.py
"""

import json
import re
from pathlib import Path

import geopandas as gpd

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
TEMPLATE = BASE / "template.html"
OUTPUT = BASE / "index.html"

# UTM 43N. Bengaluru sits in this zone, so simplification tolerances and any
# measurements are in real metres rather than degrees.
METRIC_CRS = 32643

# 5 m is below one screen pixel at city zoom and imperceptible at street zoom.
SIMPLIFY_M = 5

# 6 decimal places of longitude/latitude is roughly 0.1 m. The source files
# store 15, which is most of the file size.
COORD_DECIMALS = 6

# Strings the source data uses to mean "no value".
EMPTY_VALUES = {"", "None", "nan", "NaN", "<NA>", "null"}

# 995 of the 2224 secondary drains have a "Drain num" of "unknown0", "unknown1"
# and so on. That is an auto-generated placeholder, not a name, so treat it as
# missing. The trailing digits are required: a bare "Unknown" is the recorded
# name of 21 lost lakes and must survive.
PLACEHOLDER_PATTERN = re.compile(r"^unknown\d+$", re.IGNORECASE)

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


def main():
    print("Preparing layers")
    wards = load_wards()
    layers = {
        name: prepare_layer(name, filename, keep, is_polygon, wards)
        for name, filename, keep, is_polygon in LAYERS
    }

    payload = json.dumps(layers, separators=(",", ":"), ensure_ascii=False)
    # Stop any stray "</script>" in the data from closing the script tag early.
    payload = payload.replace("</", r"<\/")

    template = TEMPLATE.read_text(encoding="utf-8")
    if "/*__DATA__*/" not in template:
        raise SystemExit("template.html is missing the /*__DATA__*/ placeholder")
    html = template.replace("/*__DATA__*/", f"const DATA = {payload};")
    OUTPUT.write_text(html, encoding="utf-8")

    size_mb = OUTPUT.stat().st_size / 1048576
    print(f"\nWrote {OUTPUT.name} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
