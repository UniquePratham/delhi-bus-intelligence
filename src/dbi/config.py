"""Central paths and tunables for the Delhi bus intelligence project."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
ARTIFACTS = DATA / "artifacts"

GTFS_DIR = RAW / "gtfs" / "bus"
GPS_DIR = RAW / "gps"
TICKETS_CSV = RAW / "October-Delhi.csv"

REFERENCE_DB = PROCESSED / "reference.duckdb"
TRAFFIC_DB = PROCESSED / "traffic.duckdb"
CROWDING_DB = PROCESSED / "crowding.duckdb"

# Delhi bounding box used to reject GPS outliers.
LAT_MIN, LAT_MAX = 28.20, 28.95
LON_MIN, LON_MAX = 76.75, 77.55

# A ping pair is only used as a speed sample when it is plausible.
MIN_DT_S = 5
MAX_DT_S = 300
MAX_SPEED_MS = 25.0  # 90 km/h; above this the pair is treated as a GPS jump

# Radius used when snapping a GPS point to a route stop.
SNAP_RADIUS_M = 120.0

# Walking speed for access/egress legs.
WALK_SPEED_MS = 1.25
MAX_WALK_M = 1200.0

# Crowding thresholds, expressed as load factor (passengers / seated capacity).
LOAD_SEATS_AVAILABLE = 0.85
LOAD_STANDING = 1.30

# Absolute load-index bands, used for the cross-route comparable category.
# Calibrated on the observed distribution of the load index: p50 = 5.6,
# p75 = 14.5, p90 = 29.0. The bands sit at roughly p65 and p85 so that the
# three categories are not dominated by one. The load index is a relative count
# from a ~2% ticket sample, so these are ordinal bands, not passenger numbers.
LOAD_BAND_SEATS = 8.0
LOAD_BAND_STANDING = 22.0

# Assumed seated capacity by bus class. Delhi DTC standard floor buses are
# 12 m; cluster (DIMTS) buses include 9 m midis. These are planning
# assumptions, not measured values, and are documented as such.
SEATED_CAPACITY_DEFAULT = 42
SEATED_CAPACITY_AC = 38
