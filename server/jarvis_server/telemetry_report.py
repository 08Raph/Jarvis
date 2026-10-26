"""Aggregate view of the central telemetry database (counts only). Usage: python -m jarvis_server.telemetry_report --db FILE"""
from __future__ import annotations

import argparse
import json
import sys

from jarvis_server.db import DB


def report(db: DB) -> dict:
    one = lambda sql, a=(): [dict(r) for r in db.all(sql, a)]  # noqa: E731
    out = {
        "installs_seen": db.one("SELECT COUNT(DISTINCT install_id) c FROM telemetry")["c"],
        "events_total": db.one("SELECT COUNT(*) c FROM telemetry")["c"],
        "events_by_type": one("SELECT event, COUNT(*) n FROM telemetry GROUP BY event ORDER BY n DESC"),
        "app_versions": one("SELECT app_version, COUNT(DISTINCT install_id) installs FROM telemetry WHERE app_version IS NOT NULL GROUP BY app_version"),
    }
    st = one("SELECT json_extract(data,'$.passed') passed, COUNT(*) n FROM telemetry WHERE event='selftest_result' GROUP BY passed")
    out["selftest_pass_fail"] = st
    out["ram_buckets"] = one("SELECT json_extract(data,'$.ram_gb_bucket') ram_gb, COUNT(*) n FROM telemetry WHERE event='app_start' GROUP BY ram_gb ORDER BY ram_gb")
    out["error_codes"] = one("SELECT json_extract(data,'$.component') component, json_extract(data,'$.code') code, COUNT(*) n FROM telemetry WHERE event='error' GROUP BY component, code ORDER BY n DESC")
    out["avg_tokens_per_second_by_model"] = one(
        "SELECT json_extract(data,'$.model') model, ROUND(AVG(json_extract(data,'$.tokens_per_second')),2) avg_tps, COUNT(*) n "
        "FROM telemetry WHERE event='selftest_result' AND json_extract(data,'$.tokens_per_second') IS NOT NULL GROUP BY model")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    print(json.dumps(report(DB(a.db)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
