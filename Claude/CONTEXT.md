# Beacon — project context (v0.4.0)

Beacon is a dead-simple, Grafana-style monitoring dashboard for homelabs: drag-and-drop grid, every tile configurable in place, built on data already collected. Reference points the author found too heavy or too light: CheckMK, Beszel, Uptime Kuma, Netdata.

## Stack and layout (flat folder)
- `app.py` — Flask + paramiko + SQLite. Threads: `loop` polls devices over SSH (reads /proc, plus `mca-dump` for UniFi extras); `sloop` pings servers (`ping` binary, own interval).
- `index.html` — whole UI: vanilla JS, Chart.js from a CDN, no build step. Sign-in splash, settings dialog, tabs, grid.
- `Dockerfile` (installs iputils-ping), `docker-compose.yml` (service `beacon`, cap NET_RAW), `.env.example`, `config.example.json`, `requirements.txt`.
- Runtime data in `/data`: `stats.db` (tables `samples`, `pings`), `devices.json`, `servers.json`, `dash.json`, `settings.json` (interval, server_interval, guest), `users.json`, `brand.json`, `secret.key`, optional `config.json`; `auth.json` is the 0.2.x login, migrated once.

## Sign-in and roles
- Session cookie (`beacon_session`, signed with `secret.key`), scrypt password hashes in `users.json`. Roles: admin (everything), user (edit layouts and apps), viewer (view only). Guest = view-only profile with no sign-in, off by default, toggled in Settings > Users.
- Inactivity: sessions end after `idle_minutes` (default 10, Settings > Security, 0 = never) without real use. The page reports mouse/keyboard/touch with `POST /api/activity`; background polling (`GET /api/data`, `/api/servers`) does not count. Guests and "keep me signed in" sessions (`session["r"]`) are exempt. Expired requests get `401` with `expired: true`.
- Open routes: `/`, `/healthz`, `GET /api/me`, `GET /api/brand`, `POST /api/login`. Everything else needs a session; `PUT /api/dash` needs user+, other writes need admin. 5 failed sign-ins per 5 minutes per client IP locks that IP out.
- Env: `UI_USER` (default admin) and `UI_PASSWORD` (always resets that user's password at start, creates the admin if missing); with neither, a random admin password is printed once in the log. `UI_AUTH=off` removes sign-in entirely (proxy-side auth). `TRUST_PROXY=1` reads the client IP from X-Forwarded-For. `COOKIE_SECURE=1` over HTTPS (switches accept 1/true/yes/on). Responses carry CSP and nosniff headers.

## Branding
`brand.json`: name, tab title, sign-in message, logo and favicon (stored as data URIs, validated server-side, 500 KB max). Edited in Settings > Branding; public read so the sign-in screen can use it.

## Styles
Honeycrisp (default; Liquid Glass-inspired: wallpaper gradient, frosted tiles, floating capsule header; light/dark/auto), Pastel (candy colours, confetti and drifting bubbles, tiles that hop on hover, emoji tabs; light only), Stark (HUD, dark only). All CSS variables on `:root`, chosen in Settings > Appearance (per browser).

## Per-user cookie
`beacon_sort` (1 year) holds each user's list sorting as `{user: {devices|servers|apps: [column, direction]}}`; click a column header to sort, click again to flip. Default is by name, ascending.

## Appearance settings (per browser, localStorage)
`style` (honeycrisp | pastel | stark), `theme` (auto | light | dark; Honeycrisp only), `hover` (none | glow | bounce; unset = glow for Honeycrisp and Stark, bounce for Pastel), `fun` (Pastel only; off hides its emoji, sparkles and little animations), `bands`. They become `data-style`, `data-theme`, `data-hover`, `data-fun` on `<html>`. The selected tab is marked by one oval (`#pill`) that glides between tabs in every style, filled from the `--pill` token.

## Dashboard model
- `dash.json` (v "0.3"): `{v, apps: {id: {name, url, icon}}, tabs: {home|network|servers|apps: [tile]}}`. Tile: `{id, type, ref, x, y, w, h}` on a 12-column grid; `new: true` tiles are unsaved.
- Tile types: `stat`/`chart` (ref = key in `CAT` in index.html), `summary`, `app` (Home only; an empty `ref` shows a menu in the tile to pick which app it shows; the Apps page is where apps are edited). To add a tracked value: a `CAT` entry plus a `STAT` or `CHART` function.
- On phones (under 700px) the grid becomes two stacked columns and layout editing is hidden. Lists are not tiles. Network and Servers = a tile grid on top (default: four tiles) and a scrollable list panel (`#lp`) filling the rest of the screen; rows expand for details and graphs; add/edit/remove inline (admin). Servers have a type (`server` or `vm`, stored in `servers.json`). Apps = a list only, no grid: click a row to open the app, Edit (user+) for the form, Add to/Remove from Home; the catalog is `D.apps`; blank icon = lookup by name (Dashboard Icons, then selfh.st, then first letter).
- Layout rule: tiles fall upward until they rest on another; the dragged tile is pinned.

## API
Open: `GET /healthz`, `GET /api/me`, `GET /api/brand`, `POST /api/login`. Any signed-in role: `GET /api/data|servers|dash`, `POST /api/logout`, `POST /api/me/password`, `POST /api/activity`. User+: `PUT /api/dash`. Admin: `PUT /api/brand`, `POST /api/settings` (intervals, guest, idle_minutes), `GET|POST /api/users`, `PUT|DELETE /api/users/<name>`, `POST /api/<devices|servers>`, `PUT|DELETE /api/<devices|servers>/<name>`.

## Conventions
Credentials never leave the server. Keep the code small; prefer patches to rewrites. Element ids in `index.html` must be unique (a clash between the sign-in password field and the list panel once hid every list). Check the UI in a real browser (headless Chromium via Playwright) rather than only syntax-checking it. Bind to loopback (`BIND_ADDR=127.0.0.1`) when a proxy on the same host fronts it.

## Roadmap
More tile types, agent-based server metrics, richer Apps (descriptions, status checks), per-user layouts. Bare-metal Linux install/update (not yet scheduled). Other manufacturers on the Network tab.

## Continuing in a new chat
Upload this file plus the files being changed; `MANIFEST.sha256` lists hashes of the shipped files so a copy can be verified.
