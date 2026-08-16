from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("rollcalltrack_ingest.jcs_scores")

DEFAULT_SNAPSHOT = Path(__file__).resolve().parent / "data" / "jcs.json"


def rescale_jcs(value: float) -> int:
    n = max(-1.0, min(1.0, float(value)))
    return int(round(n * 100))


def lookup_ideology(snapshot: dict[str, Any], judge_id: str) -> dict[str, Any]:
    row = (snapshot.get("scores") or {}).get(judge_id)
    if not row:
        return {"ideology_score": None, "ideology_prior_only": False}
    raw = row.get("jcs")
    if raw is None:
        return {"ideology_score": None, "ideology_prior_only": False}
    return {"ideology_score": rescale_jcs(raw), "ideology_prior_only": False}


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_SNAPSHOT
    return json.loads(p.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="JCS / Martin-Quinn snapshot")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    snap = load_snapshot()
    n = len(snap.get("scores") or {})
    logger.info(
        "jcs snapshot updated=%s scores=%d dry_run=%s",
        snap.get("updated"),
        n,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
