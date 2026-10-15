# Homelab monitor — project context (v0.2.0)

Purpose: a dead-simple, Grafana-style monitoring dashboard for homelabs. Drag-and-drop grid, every tile configurable in place, built on data already collected. Reference points the author found too heavy or too light: CheckMK, Beszel, Uptime Kuma, Netdata.

## Stack and layout (flat folder)
- `app.py` — Flask + paramiko + SQLite. Two background threads: `loop` polls devices over SSH (reads /proc, plus `mca-dump` for UniFi extras); `sloop` pings servers (`ping` binary, own interval).
- `index.html` — whole UI: vanilla JS, Chart.js from a CDN, no build step. Two styles (Apple, Stark) x light/dark via CSS variables.
- `Dockerfile` (installs iputils-ping), `docker-compose.yml` (cap NET_RAW), `.env.example`, `config.example.json`, `requirements.txt`.
- Runtime data in `/data`: `stats.db` (tables `samples`, `pings`), `devices.json`, `servers.json`, `dash.json`, `settings.json` (interval, server_interval), optional `config.json`.

## Model
- `dash.json`: `{v, apps: {id: {name, url, icon}}, tabs: {home|network|servers|apps: [tile]}}`. Tile: `{id, type, ref, x, y, w, h}` on a 12-column grid; tiles with `new: true` are unsaved.
- Tile types: `stat` and `chart` (ref = key in the `CAT` table in index.html, e.g. `n.clients`, `n.c.net`, `s.c.ping`), `summary` (network|servers|apps), `list` (devices|servers), `app` (ref = app id).
- Adding a tracked value as a tile: add a `CAT` entry plus a `STAT` or `CHART` function.
- Network and Servers tabs = four top tiles + a list tile. Rows expand in place for facts and charts; add/edit/remove happen inline in the list.
- Apps tab is the catalog (removing there removes everywhere); Home can reference apps. Icons: blank icon = looked up by name from Dashboard Icons (jsDelivr), then selfh.st, then first letter.
- Layout rule: tiles fall upward until they rest on another; the dragged tile is pinned. `D.v` triggers one-time layout migrations.

## Config precedence
SSH login: `config.json` "ssh" < `SSH_*` env < per-device keys. `UI_PASSWORD` (+ `UI_USER`) turns on HTTP Basic auth.

## API
`GET /api/data`, `GET /api/servers`, `GET|PUT /api/dash`, `POST /api/settings` (interval, server_interval), `POST /api/<devices|servers>`, `PUT|DELETE /api/<devices|servers>/<name>`.

## Conventions
No auth by default, keep it on a trusted LAN. Credentials never leave the server. Keep the code small; prefer patches to rewrites.

## Roadmap
Later 0.2.x/0.3: more tile types, agent-based server metrics, richer Apps (descriptions, status checks). Bare-metal Linux install/update (floated for 0.2, not yet scheduled). Other manufacturers on the Network tab.

## Continuing in a new chat
Upload this file plus the files being changed; `MANIFEST.sha256` lists hashes of the shipped files so a copy can be verified.
