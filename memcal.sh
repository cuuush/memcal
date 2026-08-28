#!/bin/sh
# Run memcal from anywhere without installing it.
#
# `python3 -m memcal` alone only works from this directory — from anywhere else the
# package is not on sys.path, and from the parent directory it half-resolves and fails
# with "No module named memcal.__main__". Putting the script's own directory on
# PYTHONPATH is the whole fix.
#
# For a `memcal` on your PATH, run ./install.sh instead.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m memcal "$@"
