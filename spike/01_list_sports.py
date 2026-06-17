"""List available sports (0 credits) and confirm the World Cup key exists.

Run: python spike/01_list_sports.py
"""

import json
import os
import sys

from odds_client import OddsApiError, get

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WORLD_CUP_KEY = "soccer_fifa_world_cup"


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    sports = get("/sports")

    out_path = os.path.join(DATA_DIR, "sports.json")
    with open(out_path, "w") as f:
        json.dump(sports, f, indent=2)
    print(f"Saved {len(sports)} sports to {out_path}\n")

    soccer = [s for s in sports if s.get("key", "").startswith("soccer_")]
    print(f"Soccer competitions available ({len(soccer)}):")
    for s in soccer:
        active = "active" if s.get("active") else "INACTIVE"
        marker = "  <-- World Cup" if s["key"] == WORLD_CUP_KEY else ""
        print(f"  [{active:8}] {s['key']:32} {s.get('title', '')}{marker}")

    wc = next((s for s in sports if s.get("key") == WORLD_CUP_KEY), None)
    print()
    if wc is None:
        print(f"WARNING: '{WORLD_CUP_KEY}' not found in the sports list.")
        return 1
    if not wc.get("active"):
        print(f"WARNING: '{WORLD_CUP_KEY}' exists but is currently INACTIVE "
              "(no live/upcoming matches). Odds calls may return [].")
        return 0

    print(f"OK: '{WORLD_CUP_KEY}' is present and active.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OddsApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
