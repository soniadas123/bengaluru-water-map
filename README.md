# Bengaluru Water Map

An interactive map of Bengaluru's stormwater drains, its existing water bodies,
and the water bodies the city has lost.

**[Open the map](https://soniadas123.github.io/bengaluru-water-map/)**

## Versions

The link above is version 1, tagged `version1`.

The next version is previewed at
**[/next/](https://soniadas123.github.io/bengaluru-water-map/next/)**. It adds:

- the area of each lake measured inside each ward, splitting lakes that cross ward
  boundaries (pieces under 0.1 acres are left out);
- hover tooltips with the name and area of each water body;
- two layers of citizen audit points from MOD Foundation's 2026 stormwater drain
  audits (validated submissions only), coloured by an indicator you pick;
- a section in ward reports summarising the audits in that ward, with what each
  answer usually means.

Build the preview with `python build_map.py next/index.html`. The audit layers need
the three form CSVs from MOD Foundation in `data/`; they are not in this repository.

Pick a ward or a water body, by search or by clicking it, and the map generates
a report: a map of that ward with every layer switched on, and tables of the
existing and lost water bodies recorded there. Every figure in the report is
read off the same data the map draws, so the summary and the picture cannot
drift apart.

## What it shows

| Layer | Features |
| --- | --- |
| Primary drains | 381 |
| Secondary drains | 2,224 |
| Existing water bodies | 205 |
| Lost water bodies | 111 |
| GBA wards | 369 |
| Corporations | 5 |
| GBA boundary | 1 |

The 111 lost water bodies cover 1,784 acres, against 6,070 acres of existing
ones -- 23 per cent of what this dataset maps. That share is of the mapped
record, not of every water body the city ever had.

The year on a lost water body is the last survey it appears on, not the year it
went. There are only four values in the layer -- 1854, 1870, 1897 and 1969 --
and 79 of the 111 carry 1969. The map labels the field *Last mapped* for that
reason. Nothing here can date a disappearance.

## Building it

`index.html` is self-contained: the geodata is baked into it, so it is the only
file the site needs. It is generated, not edited by hand.

    python build_map.py

That reads the GeoJSONs in `data/`, simplifies and cleans them with GeoPandas,
and injects the result into `template.html` to produce `index.html`. Edit
`template.html`, never `index.html`, or the next build overwrites your changes.

Needs `geopandas`. Geometry work is done in EPSG:32643 (UTM 43N) so the 5 m
simplification tolerance is in real metres, then written back out as EPSG:4326.

| File | |
| --- | --- |
| `index.html` | the site |
| `template.html` | its source, with the `/*__DATA__*/` slot |
| `build_map.py` | the generator |
| `data/` | the seven source layers |

## Data

Water body, drain and ward data by MOD Foundation, from the Building a
Resilient Bengaluru website, accessed 23 May 2026. Basemap tiles are Esri's.

It is a snapshot of that record on one day, not a live feed, and the record has
gaps: 92 of the 205 existing water bodies have no status recorded, 21 lost ones
are named only "Unknown", and 71 of the 111 lost ones have no record of what now
occupies the bed.

## GIS disclaimer

While every effort has been made to ensure the accuracy of this information,
Sonia Das makes no warranty, expressed or implied, as to its absolute accuracy.
This product is for informational purposes and is not suitable for legal,
engineering, or surveying purposes. It does not represent an on-the-ground
survey and represents only approximate relative locations.
