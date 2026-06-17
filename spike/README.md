# Spike: retrieving World Cup odds from free APIs

Goal: prove we can retrieve usable FIFA World Cup odds (ideally TAB) from a
**free** API before building any web app. Throwaway scripts only — no DB, no
server, no scheduler.

## Why the-odds-api.com

- Free tier: **500 credits/month**, no credit card.
- Exposes the sport key **`soccer_fifa_world_cup`**.
- Its **`au` region** bookmaker list includes **TAB**.
- Credit cost of an `/odds` call = `#regions × #markets`. We use `au` + `h2h`
  = **1 credit/call**. Listing sports is **free**.

> Note: the `au` region returns **Australian** TAB (tab.com.au). There is no
> `nz` region and NZ TAB (tab.co.nz) has no official public API. Per the
> project owner, AU TAB odds are an acceptable proxy for the spike.

## Setup

```bash
cd spike
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # then edit .env and paste your key
# or: export ODDS_API_KEY=...   (get a free key at https://the-odds-api.com/#get-access)
```

## Run

```bash
python 01_list_sports.py     # 0 credits — confirm soccer_fifa_world_cup is active
python 02_worldcup_odds.py   # ~1 credit — fetch au/h2h odds, save raw JSON, show TAB
python 03_inspect.py         # 0 credits — pretty-print TAB markets from saved JSON
```

Each API call prints its credit usage (remaining/used) to stderr. Raw responses
are written to `spike/data/` (gitignored).

## Fallback / alternative free odds APIs

| API | Free tier | World Cup? | Odds? | TAB? | Notes |
|---|---|---|---|---|---|
| **the-odds-api.com** | 500 credits/mo | Yes (`soccer_fifa_world_cup`) | Yes | AU TAB via `au` region | Primary choice |
| OddsPapi (oddspapi.io) | 250 req/mo | Yes | Yes, 140+ books | Possibly | Try if the-odds-api TAB data is thin |
| API-Football (api-sports.io) | 100 req/day | Yes | Yes (some books) | Unlikely | Larger free volume, different book set |
| football-data.org | Free | Yes | **No** (fixtures/scores only) | No | Not useful for odds |
| NZ TAB (tab.co.nz) | Unofficial | Yes | Yes (native NZ) | NZ TAB itself | No public API; would mean reverse-engineering site JSON (ToS/legal caveat) |

If the-odds-api returns empty/no TAB World Cup data on the free tier, repeat the
spike against OddsPapi using the same three-script structure.

## Findings

_To be filled in after running the scripts with a real API key:_

- World Cup key active? __
- Matches returned (au/h2h): __
- TAB present? For how many matches? __
- Markets TAB exposes (stretch run h2h,totals,spreads): __
- Credits spent: __
- Go / no-go on the-odds-api for the real app: __
