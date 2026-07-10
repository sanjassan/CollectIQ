# CollectIQ — RWA Price Verification Engine for Renaiss

CollectIQ is a competitive pricing-intelligence and hidden-gem hunter for
**Renaiss** — the on-chain (BSC) graded-card gacha for Pokémon / One-Piece
collectibles. It fuses the official Renaiss APIs, an **independent** third-party
sales index, and live BSC on-chain events to compute each pack's expected value
(EV), track live prize pools in real time, and surface underpriced "Renaiss-low,
market-high" cards.

> CollectIQ is a **read-only analytics & monitoring tool** — it never places
> orders, moves funds, or performs any on-chain transaction. Every contract
> address referenced is a public Renaiss card-pool contract, verifiable on
> BscScan.

## Screenshots

The interface ships with a built-in four-language switcher (EN · 中 · 日 · 한,
top-right), defaulting to English.

| Price Verification (home) | Price Intel | CDP Collateral Sim |
|---|---|---|
| ![Home](docs/screenshots/01-home-price-verify.png) | ![Price Intel](docs/screenshots/03-price-intel.png) | ![CDP](docs/screenshots/04-cdp-collateral.png) |

| RWA Index | Oracle | On-chain Holdings |
|---|---|---|
| ![RWA Index](docs/screenshots/05-rwa-index.png) | ![Oracle](docs/screenshots/06-oracle.png) | ![Holdings](docs/screenshots/07-holdings.png) |

| Live Pool | Limited History | API Status |
|---|---|---|
| ![Live Pool](docs/screenshots/08-live-pool.png) | ![Limited History](docs/screenshots/09-limited-history.png) | ![API Status](docs/screenshots/11-api-status.png) |

More screenshots in [`docs/screenshots/`](docs/screenshots/).

## Features

- **EV analytics** — Official EV vs. real opened-pack FMV averages for every
  pack, yielding a value-for-money multiple and the deviation between them.
- **Real-time on-chain tracking** — Scans BSC Transfer logs to follow every card
  as it is pulled, recycled, or burned, with latency down to the minute.
- **Live prize-pool panel** (`/live`) — During a limited pack's open window,
  dynamically reconstructs the remaining pool value and pull progress.
- **Independent price verification** — Cross-checks Renaiss's in-house index
  against PriceCharting (aggregated eBay sales), clearly labeling source
  independence so the two are never conflated.
- **Market-listing arbitrage** — Highlights Renaiss listings priced well below
  prevailing market — the easter eggs.
- **Limited-pack history** (`/limited-history`) — A timeline of every open / add
  / S-card event for each limited drop.
- **Freshness badges + liveness probes** — `/api/freshness` flags whether each
  data source is stale; `/healthz` plus a watchdog raises a Telegram alert if a
  service stalls.
- **Four-language UI** — English / 中文 / 日本語 / 한국어, switchable live, with a
  bilingual translation dictionary and dynamic-content observer.

## Architecture

```
Browser → (optional) reverse proxy → dashboard.py (Flask, DASHBOARD_PORT)
                              │
        ┌─────────────────────┼──────────────────────────┐
        ▼                     ▼                          ▼
  api.renaiss.xyz/v0    api.renaissos.com/v1        BSC on-chain (BNB_RPC)
  packs / market /      card index / pricing /      Transfer-log pull events
  holdings              sales
```

- `renaiss_api.py` — Unified wrapper over both backend APIs, with caching and
  exponential backoff on 429/5xx.
- `dashboard.py` — Flask web UI + JSON API (EV, on-chain, market, independent
  price, freshness).
- `scripts/` — A suite of scheduled jobs (on-chain scanning, pack-catalog
  backfill, price comparison, limited-drop detection, liveness probes…).
- SQLite storage: `data/collectiq_core.db` (card tables / holdings / ledger)
  and `data/onchain_pulls.db` (on-chain pulls + sync state).

## Data model notes

- Price sources are kept in two clearly separated classes: `renaiss_index`
  (Renaiss's own index) and `pricecharting_ebay` (independent, verifiable).
  Only independent sources carry a clickable verification link, so provenance is
  always explicit in the UI.

## Install & Run

```bash
pip install -r requirements.txt

# Configure environment (Telegram alerts, keyed BSC RPC, etc.)
cp .env.example .env
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — alerting (logs only if unset)
#   BNB_RPC="<comma-separated keyed nodes>" — on-chain sync (falls back to public nodes)

# Start the web dashboard
python dashboard.py          # port set by DASHBOARD_PORT
```

On macOS the background jobs are scheduled via launchd; sample plists live in
`deploy/launchagents/` (`BNB_RPC` is redacted — see that directory's README to
restore it).

## Key API Endpoints

| Endpoint | Description |
|------|------|
| `/healthz` | Liveness probe (Flask + DB availability) |
| `/api/freshness` | Per-source data freshness / staleness |
| `/api/new-pack` | Currently open / most recent limited pack |
| `/api/limited-history` | Limited-pack event timeline |
| `/api/pack-ev` | Pack EV analysis |
| `/api/live/*` | Live prize pool / pull events / EV reconstruction |
| `/api/comparison` | Renaiss vs. independent price cross-check |

## Common Commands

```bash
python renaiss_api.py                              # Quick API-wrapper self-test
python scripts/grab_pack_contents.py --daily       # Pack catalog + independent/index backfill
BNB_RPC="<keyed nodes>" python scripts/track_pulls_onchain.py   # Manual on-chain tracking run
```

## License

MIT
