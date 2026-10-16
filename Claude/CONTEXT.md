# Beacon — project context (v0.3.0)

Beacon is a dead-simple, Grafana-style monitoring dashboard for homelabs: drag-and-drop grid, every tile configurable in place, built on data already collected. Reference points the author found too heavy or too light: CheckMK, Beszel, Uptime Kuma, Netdata.

## Stack and layout (flat folder)
- `app.py` — Flask + paramiko + SQLite. Threads: `loop` polls devices over SSH (reads /proc, plus `mca-dump` for UniFi extras); `sloop` pings servers (`ping` binary, own interval).
- `index.html` — whole UI: vanilla JS, Chart.js from a CDN, no build step. Sign-in splash, settings dialog, tabs, grid.
- `Dockerfile` (installs iputils-ping), `docker-compose.yml` (service `beacon`, cap NET_RAW), `.env.example`, `config.example.json`, `requirements.txt`.
- Runtime data in `/data`: `stats.db` (tables `samples`, `pings`), `devices.json`, `servers.json`, `dash.json`, `settings.json` (interval, server_interval, guest), `users.json`, `brand.json`, `secret.key`, optional `config.json`; `auth.json` is the 0.2.x login, migrated once.

## Sign-in and roles
- Session cookie (`beacon_session`, signed with `secret.key`), scrypt password hashes in `users.json`. Roles: admin (everything), user (edit layouts and apps), viewer (view only). Guest = view-only profile with no sign-in, off by default, toggled in Settings > Users.
- Open routes: `/`, `/healthz`, `GET /api/me`, `GET /api/brand`, `POST /api/login`. Everything else needs a session; `PUT /api/dash` needs user+, other writes need admin. 5 failed sign-ins per 5 minutes per client IP locks that IP out.
- Env: `UI_USER` (default admin) and `UI_PASSWORD` (always resets that user's password at start, creates the admin if missing); with neither, a random admin password is printed once in the log. `UI_AUTH=off` removes sign-in entirely (proxy-side auth). `TRUST_PROXY=1` reads the client IP from X-Forwarded-For. `COOKIE_SECURE=1` over HTTPS. Responses carry CSP and nosniff headers.

## Branding
`brand.json`: name, tab title, sign-in message, logo and favicon (stored as data URIs, validated server-side, 500 KB max). Edited in Settings > Branding; public read so the sign-in screen can use it.

## Styles
Honeycrisp (default; Liquid Glass-inspired: wallpaper gradient, frosted tiles, floating capsule header; light/dark/auto), Pastel (same glass, candy colours, light only), Stark (HUD, dark only). All CSS variables on `:root`, chosen in Settings > Appearance (per browser).

## Dashboard model
- `dash.json`: `{v, apps: {id: {name, url, icon}}, tabs: {home|network|servers|apps: [tile]}}`. Tile: `{id, type, ref, x, y, w, h}` on a 12-column grid; `new: true` tiles are unsaved.
- Tile types: `stat`/`chart` (ref = key in `CAT` in index.html), `summary`, `list` (devices|servers), `app`. To add a tracked value: a `CAT` entry plus a `STAT` or `CHART` function.
- Network and Servers tabs = four top tiles + a list tile; rows expand for details and graphs; add/edit/remove inline (admin). Apps tab is the catalog; blank icon = lookup by name (Dashboard Icons, then selfh.st, then first letter).
- Layout rule: tiles fall upward until they rest on another; the dragged tile is pinned.

## API
Open: `GET /healthz`, `GET /api/me`, `GET /api/brand`, `POST /api/login`. Any signed-in role: `GET /api/data|servers|dash`, `POST /api/logout`, `POST /api/me/password`. User+: `PUT /api/dash`. Admin: `PUT /api/brand`, `POST /api/settings`, `GET|POST /api/users`, `PUT|DELETE /api/users/<name>`, `POST /api/<devices|servers>`, `PUT|DELETE /api/<devices|servers>/<name>`.

## Conventions
Credentials never leave the server. Keep the code small; prefer patches to rewrites. Bind to loopback (`BIND_ADDR=127.0.0.1`) when a proxy on the same host fronts it.

## Roadmap
More tile types, agent-based server metrics, richer Apps (descriptions, status checks), per-user layouts. Bare-metal Linux install/update (not yet scheduled). Other manufacturers on the Network tab.

## Continuing in a new chat
Upload this file plus the files being changed; `MANIFEST.sha256` lists hashes of the shipped files so a copy can be verified.
