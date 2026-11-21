# Beacon

A simple, good-looking monitoring dashboard for homelabs. Beacon logs in to your network gear over SSH, pings your servers, and shows it all on tiles you arrange yourself, with graphs, lists and a launcher for your apps. It runs in one small Docker container and needs no database server.

## Features

- **Network:** CPU, memory, load, uptime, throughput, clients (with a 2.4 / 5 / 6 GHz split) and PoE draw from UniFi devices, in a sortable list with graphs for each device.
- **Servers:** ping time and up/down status, marked as a server or a virtual machine.
- **Apps:** a launcher with icons looked up for you from selfh.st (or Dashboard Icons, your choice).
- **Home and tiles:** drag, resize and add tiles for anything Beacon tracks, with a choice of hover effects.
- **Looks:** Honeycrisp (glass), Pastel and Stark themes, your own name, logo and favicon.
- **People:** several users (admin, user, viewer), an optional view-only guest profile, inactivity sign-out.

## Quick start

1. Copy `.env.example` to `.env` and set `SSH_USER` and `SSH_PASSWORD` (the login your devices share).
2. `docker compose up -d --build`
3. Open `http://<server>:8080`. On first start Beacon shows a setup page where you create the admin account with a name, email and password. Do this right after starting it, before anyone else on your network can reach the page; to skip the page, set `UI_PASSWORD` in `.env`.
4. Add devices and servers from the Network and Servers tabs.

Your data lives in `./data`. Back that folder up. History is kept for 7 days by default (Settings > Monitoring changes that, up to a year); Settings > About shows how much space it takes and can clear the icon cache or delete history. Every settings file keeps a `.bak` copy and a damaged one is restored automatically.

## Settings you can put in `.env`

| Setting | What it does |
| --- | --- |
| `SSH_USER`, `SSH_PASSWORD`, `SSH_KEY_FILE`, `SSH_PORT` | Shared SSH login for all devices |
| `UI_USER`, `UI_PASSWORD` | Admin name and password. When set, the setup page is skipped and the password is applied at every start (also how you get back in) |
| `UI_AUTH=off` | No sign-in at all. Only if your reverse proxy handles it |
| `TRUST_PROXY=1`, `COOKIE_SECURE=1`, `BIND_ADDR=127.0.0.1` | Behind a reverse proxy: real client addresses, HTTPS cookies, loopback-only port |
| `GITHUB_REPO` | Where Settings > Feedback opens issues |
| `SELFHST_ICONS_SOURCE`, `DASHBOARD_ICONS_SOURCE` | Icon collections, for your own mirrors |

## Security notes

- Sign-in uses a session cookie; by default five wrong passwords lock that address out for five minutes (Settings > Security changes the number, 1 to 20). Keep Beacon behind HTTPS if it is reachable from outside your network.
- Guest access is off by default and is view-only.
- SSH host keys are pinned on first use: the first time Beacon connects to a device it remembers that device's key (in `./data/known_hosts`), and after that it refuses to connect if the key changes, so the shared password is never sent to an impostor. The first connection is trusted, so add devices on a network you trust. If you reset or replace a device, use Settings > Security > Forget SSH host keys to choose which devices to trust again.
- The container starts as root only to make `./data` writable, then runs Beacon as an unprivileged user (ID 10001).
- Changing a password has the same limit on wrong guesses, counted per person.
- The shared SSH password never leaves the server.

## Updating

Replace the files (including the new `entrypoint.sh`) and run `docker compose up -d --build`. Settings > About shows the version. When upgrading from an earlier version, each device's SSH host key is remembered the first time it is polled after the update, and the files in `./data` are handed to the unprivileged user automatically.

## Tests

`tests/` has the checks used before each release. `python3 tests/storage_test.py`, `tests/setup_test.py` and `tests/hostkey_test.py` need only Flask and runs against a throwaway database with no real devices. `tests/ui_smoke.py` and `tests/ui_storage.py` drive a real browser through a simulated Beacon and need Playwright with Chromium (`pip install playwright && playwright install chromium`).

## Icons

App icons are looked up by name in the [selfh.st icon collection](https://selfh.st/icons) by default, or in [Dashboard Icons](https://github.com/homarr-labs/dashboard-icons) if you pick it under Settings > About. They are fetched on demand and cached in `./data/icons`; Beacon does not bundle or redistribute them, and switching source clears the cache.

- selfh.st icons are licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): credit to selfh.st is given here and in Settings > About.
- Dashboard Icons is Apache-2.0.
- In both, the logos themselves are trademarks of their respective owners and are shown only to identify the apps.

## Feedback

Settings > Feedback opens a ready-filled issue on GitHub. The issue forms live in `.github/ISSUE_TEMPLATE`.
