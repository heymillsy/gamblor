"""Re-read the saved odds JSON (0 credits) and pretty-print TAB's markets.

Confirms the exact data shape we'd later persist in the real app.

Run: python spike/03_inspect.py [path-to-raw.json]
"""

import json
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DEFAULT_FILE = os.path.join(DATA_DIR, "raw_worldcup_au_h2h.json")
TAB_KEY = "tab"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE
    if not os.path.exists(path):
        print(f"No saved data at {path}. Run 02_worldcup_odds.py first.",
              file=sys.stderr)
        return 1

    with open(path) as f:
        matches = json.load(f)

    print(f"Inspecting {len(matches)} matches from {path}\n")
    for m in matches:
        print(f"=== {m.get('home_team')} vs {m.get('away_team')} "
              f"({m.get('commence_time')}) ===")
        tab = next((b for b in (m.get("bookmakers") or [])
                    if b.get("key") == TAB_KEY), None)
        if not tab:
            print("  TAB: not offered for this match\n")
            continue
        print(f"  TAB (last_update={tab.get('last_update')}):")
        for market in (tab.get("markets") or []):
            print(f"    market '{market.get('key')}':")
            for o in market.get("outcomes", []):
                point = f" point={o['point']}" if "point" in o else ""
                print(f"      {o.get('name')}: {o.get('price')}{point}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
