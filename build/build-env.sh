#!/usr/bin/env -S bash --login
set -euo pipefail
# This script is used to install any custom packages required by the algorithm.

# Get current location of build script
basedir=$( cd "$(dirname "$0")" ; pwd -P )

conda env update -f ${basedir}/../environment.yml
conda run -n pyduck python -m pip install git+https://github.com/ameliaholcomb/gedi_tiler.git
