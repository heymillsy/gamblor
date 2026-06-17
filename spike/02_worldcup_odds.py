"""Fetch World Cup odds for the AU region, save raw JSON, summarise TAB.

Cost = #regions x #markets credits. Default au/h2h = 1 credit.

Run: python spike/02_worldcup_odds.py
"""

import json
import os
import sys

from odds_client import OddsApiError, get

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WORLD_CUP_KEY = "soccer_fifa_world_cup"
REGIONS = "au"
MARKETS = "h2h"  # set to "h2h,totals,spreads" for the stretch run (3 credits)
TAB_KEY = "tab"


def tab_h2h(bookmakers: list) -> list | None:
    """Return TAB's h2h outcomes [{name, price}, ...] if present, else None."""
    tab = next((b for b in bookmakers if b.get("key") == TAB_KEY), None)
    if not tab:
        return None
    h2h = next((m for m in (tab.get("markets") or []) if m.get("key") == "h2h"), None)
    return h2h.get("outcomes") if h2h else None


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    matches = get(
        f"/sports/{WORLD_CUP_KEY}/odds",
        {
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )

    out_path = os.path.join(
        DATA_DIR, f"raw_worldcup_{REGIONS}_{MARKETS.replace(',', '-')}.json"
    )
    with open(out_path, "w") as f:
        json.dump(matches, f, indent=2)
    print(f"Saved {len(matches)} matches to {out_path}\n")

    if not matches:
        print("No matches returned. The World Cup may be between fixtures, or "
              "the free tier may not expose this competition. See README "
              "fallback options.")
        return 0

    tab_count = 0
    for m in matches:
        books = m.get("bookmakers") or []
        outcomes = tab_h2h(books)
        if outcomes:
            tab_count += 1
        print(f"{m.get('home_team')} vs {m.get('away_team')}  "
              f"@ {m.get('commence_time')}")
        keys = [b.get("key") for b in books if b.get("key")]
        print(f"  bookmakers returned: {len(books)} "
              f"({', '.join(keys) or 'none'})")
        if outcomes:
            prices = "  ".join(f"{o['name']}={o['price']}" for o in outcomes)
            print(f"  TAB h2h: {prices}")
        else:
            print("  TAB h2h: (TAB not in this match's bookmakers)")
        print()

    print(f"TAB h2h odds present for {tab_count}/{len(matches)} matches.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OddsApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
