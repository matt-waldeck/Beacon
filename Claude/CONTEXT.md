# Homelab monitor — project context (v0.1.0)

Purpose: a dead-simple, Grafana-style monitoring dashboard for homelabs. Drag-and-drop grid, every tile configurable in place, built on data already collected from the infrastructure. Reference points the author found too heavy or too light: CheckMK, Beszel, Uptime Kuma, Netdata.

## Stack and layout (flat folder)
- `app.py` — Flask + paramiko + SQLite. One background thread polls devices over SSH (reads /proc, plus `mca-dump` for UniFi extras). Serves the JSON API and `index.html`.
- `index.html` — whole UI: vanilla JS, Chart.js from a CDN, no build step.
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `config.example.json`, `requirements.txt`.
- Runtime data in `/data` (volume): `stats.db` (samples), `devices.json`, `dash.json` (layout + apps), `settings.json` (interval), optional `config.json`.

## Model
- `dash.json`: `{apps: {id: {name, url, icon}}, tabs: {home|network|servers|apps: [tile]}}`.
- Tile: `{id, type, ref, x, y, w, h}` on a 12-column grid. Types: `summary` (ref network|servers|apps), `device` (ref = device name), `app` (ref = app id). Tiles with `new: true` are unsaved and never persisted.
- Layout rule: tiles fall upward until they rest on another tile; the tile being dragged is pinned.
- The Apps tab is the app catalog (removing there removes everywhere); Home can hold extra references to apps.
- Network tab: one device tile per polled device, auto-created.

## Config precedence
SSH login: `config.json` "ssh" block < `SSH_USER`/`SSH_PASSWORD`/`SSH_KEY_FILE`/`SSH_PORT` env < per-device keys. `UI_PASSWORD` (+ optional `UI_USER`) turns on HTTP Basic auth.

## API
`GET /api/data`, `GET|PUT /api/dash`, `POST /api/settings`, `POST /api/devices`, `PUT|DELETE /api/devices/<name>`.

## Conventions
No auth by default, so keep it on a trusted LAN. Credentials never leave the server. Show a field only if the device reports it. Keep the code small; prefer patches to rewrites.

## Roadmap
0.1.x: finish the framework. Servers tab (agentless SSH first), richer Apps tab, more tile types. 0.2: bare-metal Linux install and update. Later: other manufacturers on the Network tab.

## Continuing in a new chat
Upload this file plus the files being changed; `MANIFEST.sha256` lists hashes of the 0.1.0 files so a copy can be verified.
