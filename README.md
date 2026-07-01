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
   - `AUTH_USERNAME` / `AUTH_PASSWORD` — credentials for the web app login
     (`index.html`). The landing page shows a sign-in form; the fixtures list
     appears only after these are entered correctly.
   - `AUTH_SECRET` — *(optional)* random string used to sign login tokens. If
     unset, `CRON_SECRET` is reused.
   - `SGO_API_KEY` — *(optional, for player props)* a free SportsGameOdds key
     from sportsgameodds.com; powers `/api/sgo`.
   - `ODDSPAPI_API_KEY` — *(optional, spike)* a free OddsPapi key from
     oddspapi.io; powers `/api/oddspapi`.
   - `APIFOOTBALL_KEY` — a free API-Football key from
     dashboard.api-football.com; powers the home-page fixtures & results
     (`/api/wc`) and the `/api/apifootball` spike.
4. **Redeploy** so the variables take effect (Vercel → Deployments → ⋯ →
   Redeploy, or just push a commit).

The `odds_snapshots` table is created automatically on the first refresh — no
manual migration.

## Endpoints

Open these in a browser or call them from any app — they return JSON with
permissive CORS.

### Web app: login → fixtures → odds

The site root (`index.html`) opens on a **sign-in form** (username + password).
Credentials are checked server-side against `AUTH_USERNAME` / `AUTH_PASSWORD`;
on success a signed, 7-day token is stored in the browser and the **fixtures &
results** page is revealed. Backed by:

| Endpoint | What it does | Credit cost |
| --- | --- | --- |
| `POST /api/login` | check `{username, password}` → signed token | 0 |
| `GET /api/login?token=…` | verify a token (used on page load) | 0 |

Once signed in, the page lists **every** World Cup 2026 match — group stage and
the full knockout bracket — grouped by round, showing final/live scores for
matches that have kicked off and kickoff times for those that haven't. Knockout
fixtures whose teams aren't decided yet show as **TBD**. Backed by:

| Endpoint | What it does | Cost |
| --- | --- | --- |
| `GET /api/wc` | stored fixtures + results (read-only) | 0 |
| `GET /api/wc?job=refresh&key=…` (or `POST /api/wc?key=…`) | fetch the full WC 2026 schedule + results from API-Football and store | 1 API-Football req |

Fixture data comes from **API-Football** (`league=1`, `season=2026`), which —
unlike the-odds-api — carries the complete tournament schedule (including
undetermined knockout fixtures) and match scores. The refresh runs **daily via
Vercel Cron** (`vercel.json` → `GET /api/wc?job=refresh`, gated by
`CRON_SECRET`; Vercel sends the secret as a Bearer token automatically). Trigger
it manually with `?job=refresh&key=YOUR_CRON_SECRET`. It upserts into the
`wc_matches` table (created automatically). Requires `APIFOOTBALL_KEY`.

> The per-fixture odds view (`odds.html`, `/api/fixtures`, `/api/fixture_odds`)
> and the-odds-api refresh cron are still available as endpoints but are no
> longer linked from the home page.

---

#### Exploratory endpoints

| Endpoint | What you get | Credit cost |
| --- | --- | --- |
| `GET /api/worldcup` | Live World Cup odds, TAB only, h2h (moneyline) | ~1 |
| `GET /api/sports?soccer=true` | Available soccer competitions | 0 |
| `GET /api/refresh` | Fetch odds and **store** them in Turso (secret-gated) | 3 (featured) |
| `GET /api/match` | Deep extra-markets sweep for **one match** by id (secret-gated) | #markets |
| `GET /api/sgo` | **Player props** via SportsGameOdds (secret-gated) | objects |
| `GET /api/oddspapi` | **Spike**: explore OddsPapi (player props, free tier) | 1 req each |
| `GET /api/latest` | Read the latest **stored** snapshot from Turso | 0 |

### OddsPapi spike (`/api/oddspapi`)

the-odds-api has no World Cup props for TAB, and SportsGameOdds gates the World
Cup (`INTERNATIONAL_SOCCER`) behind a paid plan. OddsPapi's **free tier** (250
requests/month, query-param `apiKey`) advertises World Cup odds *with player
props* across 300+ books incl. TAB. This endpoint is a thin **secret-gated
proxy** so we can discover the exact ids before building storage:

| `path=` | Purpose |
| --- | --- |
| `sports` | reference list (soccer = `sportId` 10) |
| `fixtures` | schedule (defaults `sportId=10`, `from`=today, `to`=+10d; ≤10-day span). Add `summary=tournaments` for a short list of `{tournamentId, name, count}`; `tournamentId=16` (World Cup) or `tournament=world cup` returns a compact fixture list (`fixtureId`, teams, start). |
| `odds` | `?fixtureId=…` full prices for one fixture (defaults `oddsFormat=decimal`, `verbosity=3` for props). Add `marketId=10730` (Anytime Goal Scorer) to extract just that market with player names + prices per bookmaker. |
| `odds-by-tournaments` | `?tournamentIds=…&bookmaker=tab` |
| `bookmakers` / `markets` | reference lists (find TAB's key / the goalscorer market id). Add `q=tab` to filter the list to matching entries. |

Any extra query params are forwarded to OddsPapi. Requires `ODDSPAPI_API_KEY`.
No storage yet — spike first, add storage once the shape is confirmed.

### API-Football spike (`/api/apifootball`)

the-odds-api/DraftKings lacks exotic markets (correct score, winning margin).
API-Football (free 100 req/day) covers the 2026 World Cup with those markets
(from its own bookmaker set, pre-match). Secret-gated discovery proxy:

| `path=` | Purpose |
| --- | --- |
| `status` | plan + requests used today |
| `odds/bets` | bet-type catalog (find "Correct Score" / margin markets) |
| `fixtures` | World Cup fixtures (defaults `league=1`, `season=2026`) |
| `odds` | `?fixture=ID` pre-match odds (bookmakers + bets) for one fixture |

Requires `APIFOOTBALL_KEY`. Spike only — integrate exotics once confirmed.

### Player props (`/api/sgo` → SportsGameOdds)

the-odds-api's soccer player props are limited to US bookmakers — **not** the
World Cup or TAB. SportsGameOdds covers World Cup player props (anytime
goalscorer, shots, cards, etc.) and **includes TAB**, on a free Amateur tier
(~1,000 *objects*/month, billed per event; one match = 1 object). Set
`SGO_API_KEY` and use:

| Mode | URL | What it does | Cost |
| --- | --- | --- | --- |
| discovery | `/api/sgo?mode=leagues&key=…` | list soccer leagues to find the World Cup `leagueID` | cheap |
| league | `/api/sgo?leagueID=ID&key=…` | all World Cup events + odds (incl. props), one blob row per event | ~1 object/event |
| event | `/api/sgo?mode=event&event_id=SGO_ID&key=…` | one match, full player-prop detail | 1 object |

SGO event ids differ from the-odds-api ids — get them from the league pull.
Blobs are stored in a separate `sgo_snapshots` table (auto-created).

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

**Single match:** `GET /api/match?event_id=ID&key=…` sweeps all extra markets
for one match and stores it (cost = `#markets`, default 6 credits). Get the
`id` from `/api/worldcup` or `/api/latest`. Accepts `id`/`match_id` as aliases
and a `markets=` override. The response includes the stored odds so you see them
immediately.

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
- `api/match.py` — deep extra-markets sweep for a single match by `event_id`,
  stored as a blob and returned. Secret-gated.
- `api/sgo.py` — SportsGameOdds player props (World Cup, incl. TAB); stores raw
  blobs in `sgo_snapshots`. Secret-gated.
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
