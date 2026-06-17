# gamblor

Retrieve FIFA World Cup football odds (Australian TAB and other books) from
[the-odds-api.com](https://the-odds-api.com), served as JSON from Vercel
serverless endpoints. No local tooling needed — deploy and call the URLs.

## One-time setup (all doable in a phone browser)

1. **Get a free API key** at https://the-odds-api.com/#get-access
   (free tier: 500 credits/month, no card).
2. **Create a Turso database** at https://app.turso.tech — create a DB, then
   copy its **Database URL** and an **auth token**.
3. **Add environment variables in Vercel** (Project → **Settings** →
   **Environment Variables**, apply to Production + Preview):
   - `ODDS_API_KEY` — your the-odds-api key
   - `TURSO_DATABASE_URL` — your Turso DB URL (`libsql://…` or `https://…`)
   - `TURSO_AUTH_TOKEN` — your Turso auth token
   - `CRON_SECRET` — any random string; protects `/api/refresh` from random
     hits draining credits. Vercel automatically sends it on cron runs.
4. **Redeploy** so the variables take effect (Vercel → Deployments → ⋯ →
   Redeploy, or just push a commit).

The `odds_snapshots` table is created automatically on the first refresh — no
manual migration.

## Endpoints

Open these in a browser or call them from any app — they return JSON with
permissive CORS.

| Endpoint | What you get | Credit cost |
| --- | --- | --- |
| `GET /api/worldcup` | Live World Cup odds, TAB only, h2h (moneyline) | ~1 |
| `GET /api/sports?soccer=true` | Available soccer competitions | 0 |
| `GET /api/refresh` | Fetch odds and **store** them in Turso (secret-gated) | 3 (featured) |
| `GET /api/latest` | Read the latest **stored** snapshot from Turso | 0 |

### Storing odds over time (`/api/refresh` → Turso)

`/api/refresh` fetches odds and saves the raw JSON as a blob row in Turso. It is
protected by `CRON_SECRET` and runs **daily via Vercel Cron** (`vercel.json`).
Trigger it manually in a browser with `?key=YOUR_CRON_SECRET`.

| Mode | URL | What it does | Cost |
| --- | --- | --- | --- |
| featured (default) | `/api/refresh?key=…` | h2h + handicap + over/under for **all** matches, one row | 3 credits |
| deep | `/api/refresh?mode=deep&key=…` | extra markets (btts, double chance, draw-no-bet, team totals, alternates) for matches kicking off soon, one row per match | capped |

Deep-mode query options: `window_hours` (default 48 — only imminent matches),
`max_credits` (default 50 — hard spend cap; it fetches at most
`max_credits ÷ markets` matches), `markets` (override the extra-market list).

Read it back with:
- `GET /api/latest` — newest featured snapshot (parsed payload + metadata)
- `GET /api/latest?scope=event&event_id=ID` — newest stored blob for one match
- `GET /api/latest?list=true` — recent snapshot metadata (history)

Snapshots are stored as the **raw** upstream JSON (blob-first); a structured
schema can be designed later once the real payloads are known.

**Credit budget:** daily featured cron ≈ 3/day ≈ 90/month; occasional capped
deep sweeps ≈ 50 each — comfortably within the free 500/month.

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

- `api/worldcup.py`, `api/sports.py` — live read endpoints that proxy
  the-odds-api and return trimmed JSON.
- `api/refresh.py` — fetches odds (featured or deep mode) and writes raw blobs
  to Turso via its libSQL HTTP API. Secret-gated; cron-driven.
- `api/latest.py` — reads stored snapshots back from Turso (0 credits).
- `vercel.json` — daily cron schedule for `/api/refresh`.
- All functions are Python **stdlib only, zero dependencies** (using `urllib`),
  for reliable serverless cold starts.
- `index.html` — a small landing page linking to the endpoints.
- `spike/` — the original throwaway CLI scripts used to validate the API
  (kept for reference; not needed for the deployed app).

## Not yet built (next steps)
Persisting odds over time (Vercel KV/Postgres), scheduled polling (Vercel
Cron), and a UI. The current endpoints are read-through proxies.
