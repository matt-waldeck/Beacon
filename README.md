# Beacon

A simple, good-looking monitoring dashboard for homelabs. Beacon logs in to your network gear over SSH, pings your servers, and shows it all on tiles you arrange yourself, with graphs, lists and a launcher for your apps. It runs in one small Docker container and needs no database server.

## Features

- **Network:** CPU, memory, load, uptime, throughput, clients (with a 2.4 / 5 / 6 GHz split) and PoE draw from UniFi devices, in a sortable list with graphs for each device.
- **Servers:** ping time and up/down status, marked as a server or a virtual machine.
- **Apps:** a launcher with icons looked up for you (Heimdall's app list, Dashboard Icons, selfh.st).
- **Home and tiles:** drag, resize and add tiles for anything Beacon tracks, with a choice of hover effects.
- **Looks:** Honeycrisp (glass), Pastel and Stark themes, your own name, logo and favicon.
- **People:** several users (admin, user, viewer), an optional view-only guest profile, inactivity sign-out.

## Quick start

1. Copy `.env.example` to `.env` and set `SSH_USER` and `SSH_PASSWORD` (the login your devices share).
2. `docker compose up -d --build`
3. Open `http://<server>:8080`. On first start Beacon creates an admin and prints its password once: `docker compose logs beacon`.
4. Add devices and servers from the Network and Servers tabs.

Your data lives in `./data`. Back that folder up. Every settings file keeps a `.bak` copy and a damaged one is restored automatically.

## Settings you can put in `.env`

| Setting | What it does |
| --- | --- |
| `SSH_USER`, `SSH_PASSWORD`, `SSH_KEY_FILE`, `SSH_PORT` | Shared SSH login for all devices |
| `UI_USER`, `UI_PASSWORD` | Admin name and password. When set, the password is applied at every start (also how you get back in) |
| `UI_AUTH=off` | No sign-in at all. Only if your reverse proxy handles it |
| `TRUST_PROXY=1`, `COOKIE_SECURE=1`, `BIND_ADDR=127.0.0.1` | Behind a reverse proxy: real client addresses, HTTPS cookies, loopback-only port |
| `GITHUB_REPO` | Where Settings > Feedback opens issues |
| `HEIMDALL_SOURCE`, `DASHBOARD_ICONS_SOURCE`, `SELFHST_ICONS_SOURCE` | Icon sources, for your own mirrors |

## Security notes

- Sign-in uses a session cookie; five wrong passwords lock that address out for five minutes. Keep Beacon behind HTTPS if it is reachable from outside your network.
- Guest access is off by default and is view-only.
- SSH host keys are not verified yet: Beacon accepts whatever key a device presents. Keep it on a network you trust.
- The shared SSH password never leaves the server.

## Updating

Replace the files and run `docker compose up -d --build`. Settings > About shows the version.

## Feedback

Settings > Feedback opens a ready-filled issue on GitHub. The issue forms live in `.github/ISSUE_TEMPLATE`.
