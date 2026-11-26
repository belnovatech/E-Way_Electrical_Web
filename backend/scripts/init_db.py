# Load database/schema.sql (and seed.sql) into an empty database.
#   python scripts/init_db.py "<postgres url>"           schema + seed
#   python scripts/init_db.py "<postgres url>" --no-seed schema only
# Run it once on a fresh db - the schema isn't re-runnable.
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]

if len(sys.argv) < 2:
    sys.exit(__doc__ or "usage: init_db.py <postgres url> [--no-seed]")

url = sys.argv[1]
files = ["schema.sql"] if "--no-seed" in sys.argv else ["schema.sql", "seed.sql"]

with psycopg.connect(url, autocommit=True) as conn:
    for name in files:
        print("loading", name)
        conn.execute((ROOT / "database" / name).read_text(encoding="utf-8"))
    zones = conn.execute("SELECT count(*) FROM zones").fetchone()[0]
    print(f"done, {zones} zones")
