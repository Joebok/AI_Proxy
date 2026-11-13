"""Export locally recorded Ollama job statistics as CSV."""
import argparse
import csv
from pathlib import Path
import sqlite3
import sys

from .ollama_stats_store import FIELDS, OllamaStatsStore


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(FIELDS)
    path = args.runtime_dir / "ollama_stats.sqlite3"
    if not path.is_file():
        return 0
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=0.25) as db:
            rows = db.execute("SELECT " + ",".join(FIELDS) + " FROM ollama_stats ORDER BY dispatch_time, job_key").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    for row in rows:
        writer.writerow([value if value is not None else "" for value in row])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
