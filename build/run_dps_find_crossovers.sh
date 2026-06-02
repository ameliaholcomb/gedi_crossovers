#!/usr/bin/env -S bash --login
set -euo pipefail
# This script is the one that is called by the DPS.
# Use this script to prepare input paths for any files
# that are downloaded by the DPS and outputs that are
# required to be persisted

# Get current location of build script
basedir=$(dirname "$(readlink -f "$0")")

# Create output directory to store outputs.
# The name is output as required by the DPS.
# Note how we dont provide an absolute path
# but instead a relative one as the DPS creates
# a temp working directory for our code.

mkdir -p output

# DPS downloads all files provided as inputs to
# this directory called input.
INPUT_DIR=input

# Read the positional arguments as defined in the algorithm registration here
bucket=$1
prefix=$2
distance_m=$3
columns=${4:-}
filters=${5:-}

# Find the region file from the input directory (shapefile, GeoJSON, GPKG, etc.)
shapefile=$(ls ${INPUT_DIR}/* | head -1)

# Build optional argument arrays so that empty args are cleanly omitted
# columns is a space-separated list of column names (e.g. "agbd_l4a rh_98_l2a")
columns_args=()
if [ -n "${columns}" ]; then
    read -ra col_array <<< "${columns}"
    columns_args=("--columns" "${col_array[@]}")
fi

# filters is an optional SQL WHERE clause (e.g. "l4_quality_flag = 1 AND sensitivity > 0.95")
filters_arg=()
if [ -n "${filters}" ]; then
    filters_arg=("--filters" "${filters}")
fi

# Call the script using the absolute paths.
# Use the updated environment when calling 'conda run'.
# This lets us run the same way in a Terminal as in DPS.
# Any output written to the stdout and stderr streams will be
# automatically captured and placed in the output dir.

conda run --live-stream --name pyduck python ${basedir}/../scripts/find_crossovers.py \
    --shapefile "${shapefile}" \
    --bucket "${bucket}" \
    --prefix "${prefix}" \
    --distance_m "${distance_m}" \
    --outfile output/crossovers.parquet \
    "${columns_args[@]}" \
    "${filters_arg[@]}"
