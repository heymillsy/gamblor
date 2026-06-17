# gamblor

Retrieve FIFA World Cup football odds (Australian TAB and other books) from
[the-odds-api.com](https://the-odds-api.com), served as JSON from Vercel
serverless endpoints. No local tooling needed — deploy and call the URLs.

## One-time setup (all doable in a phone browser)

1. **Get a free API key** at https://the-odds-api.com/#get-access
   (free tier: 500 credits/month, no card).
2. **Add it to Vercel:** Project → **Settings** → **Environment Variables** →
   add `ODDS_API_KEY` = your key (apply to Production + Preview).
3. **Redeploy** so the variable takes effect (Vercel → Deployments → ⋯ →
   Redeploy, or just push a commit).

## Endpoints

Open these in a browser or call them from any app — they return JSON with
permissive CORS.

| Endpoint | What you get | Credit cost |
| --- | --- | --- |
| `GET /api/worldcup` | World Cup odds, TAB only, h2h (moneyline) | ~1 |
| `GET /api/sports?soccer=true` | Available soccer competitions | 0 |

### `/api/worldcup` query options
| Param | Default | Notes |
| --- | --- | --- |
| `regions` | `au` | `au` = Australian books incl. TAB. No NZ region exists. |
| `markets` | `h2h` | e.g. `h2h,totals,spreads`. Cost = `#regions × #markets`. |
| `bookmaker` | `tab` | Filter to one book; `all` returns every book. |
| `format` | `simple` | `simple` (trimmed) or `raw` (full upstream payload). |

Examples:
- `/api/worldcup` — TAB h2h prices for every upcoming World Cup match
- `/api/worldcup?bookmaker=all&markets=h2h,totals`
- `/api/sports?soccer=true` — confirm `soccer_fifa_world_cup` is active

Each response includes a `meta.credits` block showing your remaining monthly
quota.

> Note: the `au` region returns **Australian** TAB (tab.com.au). NZ TAB
> (tab.co.nz) has no public API; AU odds are used as a close proxy.

## How it works

- `api/worldcup.py`, `api/sports.py` — Vercel Python serverless functions
  (Python stdlib only, zero dependencies). Each reads `ODDS_API_KEY` from the
  environment and proxies the-odds-api, returning trimmed JSON.
- `index.html` — a small landing page linking to the endpoints.
- `spike/` — the original throwaway CLI scripts used to validate the API
  (kept for reference; not needed for the deployed app).

## Not yet built (next steps)
Persisting odds over time (Vercel KV/Postgres), scheduled polling (Vercel
Cron), and a UI. The current endpoints are read-through proxies.
