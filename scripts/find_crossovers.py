"""
Module for finding repeat GEDI footprints (crossovers) using H3 spatial indexing.

Provides functionality to identify pairs of GEDI shots that are within a specified
distance of each other, with support for user-supplied filters and data columns.
"""

import argparse
import h3
import geopandas as gpd
import json
import logging
import numpy as np
import os
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.transform import array_bounds, from_bounds
from rasterio.warp import transform_bounds
import sys
from maap.maap import MAAP
from typing import List, Optional, Union

from gtiler.database import ducky
import crossovers

logger = logging.getLogger(__name__)


def write_diff_cog(df, columns: List[str], outfile: str) -> None:
    """Rasterize per-column differences (t2 - t1) as a multi-band COG.

    Each footprint pair is drawn as a 25 m diameter circle at the t2 location.
    One band per column, named <col>_diff.
    """
    if not columns or df.empty:
        logger.warning("No data for COG output; skipping.")
        return

    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["t2_lon_lowestmode"], df["t2_lat_lowestmode"]),
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")
    gdf["geometry"] = gdf.geometry.buffer(12.5)  # 25 m diameter circles

    minx, miny, maxx, maxy = gdf.total_bounds
    pad = 50.0
    minx -= pad; miny -= pad; maxx += pad; maxy += pad

    res = 12.5  # pixel size in metres (matches footprint radius)
    width = max(1, int(np.ceil((maxx - minx) / res)))
    height = max(1, int(np.ceil((maxy - miny) / res)))
    transform = from_bounds(minx, miny, maxx, maxy, width, height)

    bands = []
    band_names = []
    for col in columns:
        t1_col, t2_col = f"t1_{col}", f"t2_{col}"
        if t1_col not in df.columns or t2_col not in df.columns:
            logger.warning("Columns %s/%s not found in data; skipping.", t1_col, t2_col)
            continue

        diffs = (df[t2_col] - df[t1_col]).astype(np.float32).values
        valid = ~np.isnan(diffs)
        shapes = [
            (geom, float(val))
            for geom, val, ok in zip(gdf.geometry, diffs, valid)
            if ok
        ]

        arr = (
            rasterize(
                shapes,
                out_shape=(height, width),
                transform=transform,
                fill=float("nan"),
                dtype=np.float32,
            )
            if shapes
            else np.full((height, width), float("nan"), dtype=np.float32)
        )
        bands.append(arr)
        band_names.append(f"{col}_diff")

    if not bands:
        logger.warning("No valid columns for COG output; skipping.")
        return

    logger.info("Writing %d-band diff COG to %s", len(bands), outfile)
    with rasterio.open(
        outfile,
        "w",
        driver="COG",
        height=height,
        width=width,
        count=len(bands),
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
        nodata=float("nan"),
        compress="deflate",
    ) as dst:
        for i, (arr, name) in enumerate(zip(bands, band_names), 1):
            dst.write(arr, i)
            dst.update_tags(i, name=name)

    _write_stac_item(outfile, band_names, bands, transform, height, width, df)


def _write_stac_item(
    cog_path: str,
    band_names: List[str],
    bands: List[np.ndarray],
    raster_transform,
    height: int,
    width: int,
    df,
) -> None:
    left, bottom, right, top = array_bounds(height, width, raster_transform)
    minlon, minlat, maxlon, maxlat = transform_bounds(
        "EPSG:3857", "EPSG:4326", left, bottom, right, top
    )

    all_times = pd.to_datetime(
        pd.concat([df["t1_absolute_time"].dropna(), df["t2_absolute_time"].dropna()])
    )
    start_dt = all_times.min().isoformat()
    end_dt = all_times.max().isoformat()

    eo_bands = []
    raster_bands = []
    for name, arr in zip(band_names, bands):
        col = name.removesuffix("_diff")
        valid = arr[~np.isnan(arr)]
        eo_bands.append({
            "name": name,
            "description": f"Difference (t2 - t1) for {col}",
        })
        raster_bands.append({
            "nodata": "nan",
            "data_type": "float32",
            "statistics": {
                "minimum": float(valid.min()) if valid.size else None,
                "maximum": float(valid.max()) if valid.size else None,
                "mean": float(valid.mean()) if valid.size else None,
                "stddev": float(valid.std()) if valid.size else None,
                "valid_percent": float(100 * valid.size / arr.size),
            },
        })

    item = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/eo/v1.0.0/schema.json",
            "https://stac-extensions.github.io/raster/v1.1.0/schema.json",
        ],
        "id": os.path.splitext(os.path.basename(cog_path))[0],
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [minlon, minlat],
                [maxlon, minlat],
                [maxlon, maxlat],
                [minlon, maxlat],
                [minlon, minlat],
            ]],
        },
        "bbox": [minlon, minlat, maxlon, maxlat],
        "properties": {
            "datetime": None,
            "start_datetime": start_dt,
            "end_datetime": end_dt,
        },
        "links": [],
        "assets": {
            "data": {
                "href": os.path.basename(cog_path),
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "title": "GEDI crossover differences (COG)",
                "roles": ["data"],
                "eo:bands": eo_bands,
                "raster:bands": raster_bands,
            },
        },
    }

    stac_path = os.path.splitext(cog_path)[0] + ".json"
    with open(stac_path, "w") as f:
        json.dump(item, f, indent=2)
    logger.info("Wrote STAC item to %s", stac_path)


def main(args):
    shape = gpd.read_file(args.shapefile).to_crs("EPSG:4326")

    temp_dir = ".tmp/duckdb_tmp"
    os.makedirs(temp_dir, exist_ok=True)
    con = ducky.init_duckdb(temp_dir)
    data_spec = ducky.data_spec(args.bucket, args.prefix)
    columns = [col.strip() for col in args.columns.split(",")] if args.columns else []

    res = crossovers.find_repeat_footprints(
        con, data_spec, shape, args.distance_m, args.filters, columns
    )
    logger.info("Found %d repeat footprint pairs.", len(res))

    res.to_parquet(args.outfile, index=False)

    if args.cog_outfile:
        write_diff_cog(res, columns, args.cog_outfile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find repeat GEDI footprints (crossovers) using H3 spatial indexing."
    )
    parser.add_argument(
        "--shapefile",
        type=str,
        required=True,
        help="Path to shapefile defining region to search for crossovers.",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        required=True,
        help="AWS S3 bucket in which the tiled GEDI database is stored.",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        required=True,
        help="AWS S3 prefix in which the tiled GEDI database is stored.",
    )
    parser.add_argument(
        "--distance_m",
        type=int,
        required=True,
        default=40,
        help="Maximum distance in meters for repeat footprints.",
    )
    parser.add_argument(
        "--columns",
        type=str,
        required=False,
        help="Comma-separated list of columns to include in the output data. Shot number, latitude, longitude, and metric distance between footprints are always included.",
    )
    parser.add_argument(
        "--filters",
        type=str,
        required=False,
        help="String of quality filters, e.g.\n'l4_quality_flag = 1 AND sensitivity > 0.95'",
    )
    parser.add_argument(
        "--outfile",
        type=str,
        required=True,
        help="Path in which to store repeat footprints data",
    )
    parser.add_argument(
        "--cog_outfile",
        type=str,
        required=False,
        help=(
            "If provided, write a Cloud Optimized GeoTIFF with one band per column "
            "in --columns, each containing the difference (t2 - t1) rasterized as "
            "25 m diameter circles at the t2 footprint location (EPSG:3857, 12.5 m pixels)."
        ),
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    args = parser.parse_args()
    main(args)
