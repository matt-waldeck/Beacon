import base64, functools, hashlib, json, os, queue, re, secrets, shutil, smtplib, socket, sqlite3, ssl, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import paramiko
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

DATA = os.environ.get("DATA_DIR", "/data")
VERSION = "1.0.0"
DEFAULT_REPO = os.environ.get("GITHUB_REPO") or "matt-waldeck/beacon"  # where the Feedback page sends people
CFG, SET, DB, DEV, SRV, UNIF = (os.path.join(DATA, n) for n in ("config.json", "settings.json", "stats.db", "devices.json", "servers.json", "unifi.json"))
# Read-only /proc reads work on every UniFi device; mca-dump adds model/firmware/client count where present.
CMD = "for f in uptime loadavg meminfo stat net/dev; do echo @@$f; cat /proc/$f; done; echo @@mca; mca-dump 2>/dev/null"
app, wake, swake, dblock, prev = Flask(__name__), threading.Event(), threading.Event(), threading.Lock(), {}
jlock = threading.RLock()  # one change to the settings files at a time


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with jlock:
            return fn(*a, **k)
    return wrapper


def read_json(path, default):  # a damaged file is restored from its backup instead of stopping the app
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        try:
            with open(path + ".bak") as f:
                good = json.load(f)
        except (OSError, ValueError):
            print(f"WARNING: {path} is damaged and has no usable backup", flush=True)
            return default
        shutil.copyfile(path + ".bak", path)
        print(f"WARNING: {path} was damaged and has been restored from its backup", flush=True)
        return good


def write_json(path, obj, **kw):  # write beside, then swap in, so a crash can't leave half a file; keep a copy as .bak
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    shutil.copyfile(path, path + ".bak")


def cfg():
    c = read_json(CFG, {})  # config.json is optional
    return c if isinstance(c, dict) else {}


def env_ssh():
    m = {"user": "SSH_USER", "password": "SSH_PASSWORD", "key_file": "SSH_KEY_FILE", "port": "SSH_PORT"}
    e = {k: os.environ[v] for k, v in m.items() if os.environ.get(v)}
    if "port" in e:
        try:
            e["port"] = int(e["port"])
        except ValueError:
            print("WARNING: SSH_PORT is not a number, ignoring it", flush=True)
            del e["port"]
    return e


PATHS = {"devices": DEV, "servers": SRV}


def items(kind):
    lst = read_json(PATHS[kind], None)
    if not isinstance(lst, list):
        return cfg().get("devices", []) if kind == "devices" else []  # first run: devices seed from config.json
    return lst


def save_items(kind, lst):
    write_json(PATHS[kind], lst, indent=2)


def devices():
    return items("devices")


def whole(v, default, lo, hi):  # a hand-edited or damaged setting falls back to its default instead of breaking every request
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def conf():
    s = read_json(SET, {})
    s = s if isinstance(s, dict) else {}
    return {"interval": whole(s.get("interval", cfg().get("interval", 30)), 30, 5, 3600), "server_interval": whole(s.get("server_interval", 15), 15, 5, 3600), "guest": bool(s.get("guest", False)), "idle_minutes": whole(s.get("idle_minutes", 10), 10, 0, 1440), "max_attempts": whole(s.get("max_attempts", 5), 5, 1, 20), "repo": str(s.get("repo", "")), "retention_days": s.get("retention_days"), "icon_source": s.get("icon_source") if s.get("icon_source") in ICON_SOURCES else DEFAULT_ICONS}


def interval():
    return conf()["interval"]


def sql(q, a=()):
    with dblock:
        db = sqlite3.connect(DB)
        try:
            rows = db.execute(q, a).fetchall()
            db.commit()
            return rows
        finally:
            db.close()


METRICS = ("cpu", "mem", "load", "rx", "tx", "clients", "poe")  # numbers kept in their own columns so long graphs are quick


def migrate():
    sql("CREATE TABLE IF NOT EXISTS samples (dev TEXT, ts REAL, data TEXT)")
    sql("CREATE INDEX IF NOT EXISTS i ON samples (dev, ts)")
    sql("CREATE TABLE IF NOT EXISTS pings (dev TEXT, ts REAL, ms REAL)")
    sql("CREATE INDEX IF NOT EXISTS pi ON pings (dev, ts)")
    have = {r[1] for r in sql("PRAGMA table_info(samples)")}
    for k in METRICS:
        if "m_" + k not in have:
            sql(f"ALTER TABLE samples ADD COLUMN m_{k} REAL")
    if sql("PRAGMA user_version")[0][0] < 1:  # one time: copy the numbers out of the stored samples
        sql("UPDATE samples SET " + ", ".join(f"m_{k} = json_extract(data, '$.{k}')" for k in METRICS) + " WHERE json_extract(data, '$.error') IS NULL")
        sql("PRAGMA user_version = 1")


def save_sample(name, r, notify=True):  # notify=False: the controller is to blame, and it gets its own message
    sql("INSERT INTO samples (dev, ts, data, " + ", ".join("m_" + k for k in METRICS) + ") VALUES (?,?,?," + ",".join("?" * len(METRICS)) + ")",
        (name, r["ts"], json.dumps(r), *(r.get(k) for k in METRICS)))
    if notify:
        watch("device", name, "error" not in r, r.get("error", ""))


def retention():  # days of history to keep: Settings first, then config.json, then 7
    s = read_json(SET, {})
    for v in (s.get("retention_days") if isinstance(s, dict) else None, cfg().get("retention_days"), 7):
        try:
            return max(1, min(365, int(v)))
        except (TypeError, ValueError):
            continue


def prune():
    cutoff = time.time() - 86400 * retention()
    sql("DELETE FROM samples WHERE ts < ?", (cutoff,))
    sql("DELETE FROM pings WHERE ts < ?", (cutoff,))


def long_samples(name, mins):  # a long range: averaged into about 300 steps, plus the newest real sample so the details stay current
    b = max(60, mins * 60 // 300)
    rows = sql("SELECT CAST(ts / ? AS INTEGER) k, " + ", ".join(f"AVG(m_{k})" for k in METRICS) + " FROM samples WHERE dev=? AND ts>? GROUP BY k ORDER BY k", (b, name, time.time() - mins * 60))
    out = []
    for row in rows:
        r = {"ts": row[0] * b + b // 2}  # steps line up across devices, so combined graphs add up
        r.update({k: round(v, 2) for k, v in zip(METRICS, row[1:]) if v is not None})
        if len(r) > 1:
            out.append(r)
    last = sql("SELECT data FROM samples WHERE dev=? ORDER BY ts DESC LIMIT 1", (name,))
    return out + ([json.loads(last[0][0])] if last else [])


def long_pings(name, mins):
    b = max(60, mins * 60 // 300)
    rows = sql("SELECT CAST(ts / ? AS INTEGER) k, AVG(ms), MAX(ms), COUNT(*), SUM(ms IS NULL) FROM pings WHERE dev=? AND ts>? GROUP BY k ORDER BY k", (b, name, time.time() - mins * 60))
    out = [{"ts": k * b + b // 2, "ms": None if a is None else round(a, 2), "max": mx, "n": n, "d": d} for k, a, mx, n, d in rows]
    last = sql("SELECT ts, ms FROM pings WHERE dev=? ORDER BY ts DESC LIMIT 1", (name,))
    return out + ([{"ts": last[0][0], "ms": last[0][1], "max": last[0][1], "n": 1, "d": int(last[0][1] is None)}] if last else [])


KNOWN_HOSTS, hklock = os.path.join(DATA, "known_hosts"), threading.Lock()


class TrustFirst(paramiko.MissingHostKeyPolicy):
    """Trust a device the first time it is seen and remember its key; after that only that key is accepted."""
    def missing_host_key(self, client, hostname, key):
        with hklock:  # several devices are polled at once; each one re-reads the file so no addition is lost
            hk = paramiko.HostKeys()
            if os.path.exists(KNOWN_HOSTS):
                hk.load(KNOWN_HOSTS)
            old = hk.lookup(hostname)
            if old:  # already known under another key type: a mismatch, not a first visit
                raise paramiko.BadHostKeyException(hostname, key, next(iter(old.values())))
            hk.add(hostname, key.get_name(), key)
            hk.save(KNOWN_HOSTS)
            os.chmod(KNOWN_HOSTS, 0o600)
        client._host_keys.add(hostname, key.get_name(), key)  # lets this connection carry on


def ssh_run(d):
    if "user" not in d:
        raise ValueError("No SSH user set: use SSH_USER or the ssh block in config.json.")
    c = paramiko.SSHClient()
    with hklock:
        if os.path.exists(KNOWN_HOSTS):
            c.load_host_keys(KNOWN_HOSTS)
    c.set_missing_host_key_policy(TrustFirst())
    c.connect(d["host"], port=d.get("port", 22), username=d["user"], password=d.get("password"),
              key_filename=d.get("key_file"), timeout=8, look_for_keys=False, allow_agent=False)
    try:
        return c.exec_command(CMD, timeout=20)[1].read().decode(errors="replace")
    finally:
        c.close()


def mca(raw):  # what mca-dump adds: model, firmware, AP client counts by band, PoE draw. Any odd shape just means fewer extras.
    r = {}
    try:
        m = json.loads(raw)
        r.update(model=m.get("model"), version=m.get("version"))
        if "vap_table" in m:  # APs only
            r["clients"] = sum(v.get("num_sta", 0) for v in m["vap_table"])
            bands = {}
            for v in m["vap_table"]:  # radio codes: ng = 2.4 GHz, na = 5 GHz, 6e = 6 GHz
                b = {"ng": "2.4", "na": "5", "6e": "6"}.get(v.get("radio"))
                if b:
                    bands[b] = bands.get(b, 0) + v.get("num_sta", 0)
            if bands:
                r["bands"] = bands
        ports = [p for p in m.get("port_table", []) if "poe_power" in p]
        if ports:  # only PoE-capable devices report this; otherwise the field is left out
            r["poe"] = round(sum(float(p["poe_power"] or 0) for p in ports), 1)
    except (ValueError, AttributeError, TypeError):
        pass
    return r


def parse(out, d):
    s = dict(re.findall(r"@@(\S+)\n(.*?)(?=\n@@|\Z)", out, re.S))
    mem = {k: int(v) for k, v in re.findall(r"(\w+):\s+(\d+)", s["meminfo"])}
    avail = mem.get("MemAvailable", mem["MemFree"] + mem.get("Buffers", 0) + mem.get("Cached", 0))
    cpu = list(map(int, s["stat"].split("\n")[0].split()[1:]))
    idle, total = cpu[3] + cpu[4], sum(cpu)
    rx = tx = 0
    for line in s["net/dev"].split("\n")[2:]:
        name, _, vals = line.partition(":")
        if re.match(d.get("ifaces", r"eth\d+$"), name.strip()):  # per-device regex; avoids double-counting bridges
            v = vals.split()
            rx, tx = rx + int(v[0]), tx + int(v[8])
    now, p = time.time(), prev.get(d["name"])
    r = {"ts": now, "uptime": float(s["uptime"].split()[0]), "load": float(s["loadavg"].split()[0]),
         "mem": round(100 * (1 - avail / mem["MemTotal"]), 1), "cpu": None, "rx": None, "tx": None}
    if p and now > p["ts"] and total > p["total"]:
        dt = now - p["ts"]
        r["cpu"] = round(100 * (1 - (idle - p["idle"]) / (total - p["total"])), 1)
        r["rx"] = max(0, round(8 * (rx - p["rx"]) / dt))
        r["tx"] = max(0, round(8 * (tx - p["tx"]) / dt))
    prev[d["name"]] = {"ts": now, "idle": idle, "total": total, "rx": rx, "tx": tx}
    r.update(mca(s.get("mca", "")))
    return r


HOSTKEY_MSG = "The device's SSH host key has changed, so Beacon did not connect. If you reset or updated the device, use Settings > Security > Forget SSH host keys."


def poll(d):
    try:
        r = parse(ssh_run(d), d)
    except (KeyError, IndexError):
        r = {"ts": time.time(), "error": "Unexpected reply from the device. Is it a UniFi (Linux) device?"}
    except paramiko.BadHostKeyException:  # nothing was sent: the password is only offered once the key is accepted
        r = {"ts": time.time(), "error": HOSTKEY_MSG}
    except Exception as e:
        r = {"ts": time.time(), "error": str(e)[:200]}
    save_sample(d["name"], r)


# ---- UniFi Network API: the official Integration API with an API key, for as many controllers ("sites") as you like, set up in Settings > UniFi
# (or one more from UNIFI_URL and UNIFI_API_KEY). With none set up everything is polled over SSH as before.
RATE_MUL = 1  # the API's uplink rates are read as bits per second (a third-party client documents them so; UniFi's own spec doesn't say). Use 8 if they turn out to be bytes.
API_NAMES = {}  # device name -> id of the site that answered for it last cycle
UF = {"conn": {}, "state": {}}  # per site id: where its API lives and how the last check went
dlock = threading.Lock()  # one device check at a time


class UnifiError(Exception):
    pass


def norm_url(u):
    u = str(u).strip().rstrip("/")
    if u and "://" not in u:
        u = "https://" + u
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("Enter the controller's address, like https://192.168.1.1.")
    return u


def unifi_raw():
    j = read_json(UNIF, {})
    return j if isinstance(j, dict) else {}


def unifi_saved():  # the saved sites and device-check schedule, tidied so a hand-edited file can't break anything
    j = unifi_raw()
    sites = [{"id": str(x["id"]), "name": str(x.get("name") or x["url"])[:40], "url": str(x["url"]), "key": str(x["key"]), "site": str(x.get("site") or "default"), "verify": x.get("verify") is True}
             for x in (j.get("sites") if isinstance(j.get("sites"), list) else []) if isinstance(x, dict) and x.get("id") and x.get("url") and x.get("key")]
    d = j.get("discover") if isinstance(j.get("discover"), dict) else {}
    at, last = str(d.get("at", "")), d.get("last_run")
    disc = {"enabled": d.get("enabled") is not False, "mode": "hours" if d.get("mode") == "hours" else "daily", "hours": whole(d.get("hours", 24), 24, 1, 168),
            "at": at if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at) else "01:00", "last_run": last if isinstance(last, (int, float)) else None,
            "last_added": [str(n) for n in d["last_added"]][:50] if isinstance(d.get("last_added"), list) else [], "last_errors": d["last_errors"] if isinstance(d.get("last_errors"), dict) else {}}
    return {"sites": sites, "discover": disc, "ignored": [str(i) for i in j["ignored"]] if isinstance(j.get("ignored"), list) else []}


def unifi_env():  # the first way of setting this up: UNIFI_URL and UNIFI_API_KEY in the environment count as one more site
    url, key = os.environ.get("UNIFI_URL", "").strip(), os.environ.get("UNIFI_API_KEY", "").strip()
    try:
        url = norm_url(url) if url and key else ""
    except ValueError:
        url = ""
    return {"id": "env", "name": "Environment", "url": url, "key": key, "site": os.environ.get("UNIFI_SITE", "").strip() or "default", "verify": flag("UNIFI_VERIFY_TLS"), "env": True} if url else None


def unifi_sites():
    env = unifi_env()
    return [{**x, "env": False} for x in unifi_saved()["sites"]] + ([env] if env else [])


def addr(host):
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def unreachable(url, e):  # says where Beacon looked and, for the usual mistakes, what to change
    u = urllib.parse.urlparse(url)
    host, port = u.hostname or "?", u.port or (443 if u.scheme == "https" else 80)
    why, at = getattr(e, "reason", e), f"{host}:{port}"
    if isinstance(why, ConnectionRefusedError):
        return f"Connection refused at {at}. Use the port of your UniFi OS Server's local web UI (usually 11443)." + (" In Docker, localhost is Beacon's own container." if host in ("localhost", "127.0.0.1", "::1") else "")
    if "WRONG_VERSION_NUMBER" in str(why):
        return f"{at} answered in plain HTTP. Try http://{at}."
    if isinstance(why, (TimeoutError, socket.timeout)):
        return f"No answer from {at}. Check the address and that Beacon can reach it."
    return f"Could not reach {at}: {str(why)[:150]}"


def unifi_get(c, base, path, params=None):
    ctx = ssl.create_default_context()
    if not c["verify"]:  # most controllers use a self-signed certificate
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(base + path + ("?" + urllib.parse.urlencode(params) if params else ""), headers={"X-API-KEY": c["key"], "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:
        raise UnifiError("The controller refused the API key." if e.code in (401, 403) else f"The controller answered HTTP {e.code} for {path}.")
    except ValueError:
        raise UnifiError("The controller's reply was not JSON. Check UNIFI_URL.")
    except OSError as e:
        raise UnifiError(unreachable(req.full_url, e))


def unifi_all(c, base, path, params=None):  # every page of a list
    out = []
    while True:
        page = unifi_get(c, base, path, {**(params or {}), "offset": len(out), "limit": 200})
        out += page.get("data") or []
        if not page.get("data") or len(out) >= page.get("totalCount", 0):
            return out


def unifi_connect(c, cache=True):  # finds the API's base path and the site, then remembers them until the address or site changes -> (base, site id, version, kind)
    key, hit = (c["url"], c["site"]), UF["conn"].get(c["id"])
    if cache and hit and hit[0] == key:
        return hit[1:]
    path = urllib.parse.urlparse(c["url"]).path
    errs = []
    for base in [c["url"]] if "integration" in path else [c["url"] + "/proxy/network/integration", c["url"] + "/integration"]:  # UniFi OS consoles and standalone installs differ
        try:
            ver = unifi_get(c, base, "/v1/info").get("applicationVersion", "")
            break
        except UnifiError as e:
            errs.append(e)
    else:
        raise next((e for e in errs if "HTTP 404" not in str(e)), errs[-1])
    sites, want = unifi_all(c, base, "/v1/sites"), c["site"].lower()
    site = next((x for x in sites if want in (x["id"].lower(), str(x.get("internalReference", "")).lower(), str(x.get("name", "")).lower())), None)
    if not site:
        raise UnifiError(f"Site '{c['site']}' was not found. The controller has: " + ", ".join(str(x.get("internalReference") or x.get("name")) for x in sites) + ".")
    kind = "UniFi OS console" if "/proxy/network" in base else "Network application"  # inferred from which path answered; the API doesn't say
    if cache:
        UF["conn"][c["id"]] = (key, base, site["id"], ver, kind)
    return base, site["id"], ver, kind


def unifi_read(c, devs, ex):  # {device name: (controller's device record, its latest statistics or an error text, wireless client count or None)}
    base, site, ver, kind = unifi_connect(c)
    ctl = {x["ipAddress"]: x for x in unifi_all(c, base, f"/v1/sites/{site}/devices") if x.get("ipAddress")}
    mine = {}
    for d in devs:  # the controller's devices are matched to your list by IP address
        ip = addr(d["host"])
        if ip in ctl:
            mine[d["name"]] = ctl[ip]

    def stats(name):
        try:
            return unifi_get(c, base, f"/v1/sites/{site}/devices/{mine[name]['id']}/statistics/latest")
        except UnifiError as e:
            return str(e)
    got, counts, warn = dict(zip(mine, ex.map(stats, mine))), {}, ""
    ap = lambda x: "accessPoint" in x.get("features", [])
    if any(ap(x) for x in mine.values()):  # one list of wireless clients, counted per access point
        try:
            for cl in unifi_all(c, base, f"/v1/sites/{site}/clients", {"filter": "type.eq('WIRELESS')"}):
                counts[cl.get("uplinkDeviceId")] = counts.get(cl.get("uplinkDeviceId"), 0) + 1
        except UnifiError as e:
            warn = "Client counts are missing: " + str(e)
    now = time.time()
    UF["state"][c["id"]] = {"ok": True, "version": ver, "kind": kind, "devices": len(ctl), "matched": len(mine), "warn": warn, "last_ok": now, "ts": now}
    return {n: (x, got[n], counts.get(x["id"], 0) if ap(x) and not warn else None) for n, x in mine.items()}


def api_sample(x, st):
    now = time.time()
    if x.get("state") != "ONLINE":
        return {"ts": now, "error": "The controller reports this device as " + str(x.get("state", "unknown")).replace("_", " ").lower() + "."}
    if isinstance(st, str):
        return {"ts": now, "error": "UniFi API: " + st}
    up, num = st.get("uplink") or {}, lambda v, n=1: None if v is None else round(v, n)
    return {"ts": now, "uptime": st.get("uptimeSec"), "load": num(st.get("loadAverage1Min"), 2), "mem": num(st.get("memoryUtilizationPct")), "cpu": num(st.get("cpuUtilizationPct")),
            "rx": num(up.get("rxRateBps") and up["rxRateBps"] * RATE_MUL, 0), "tx": num(up.get("txRateBps") and up["txRateBps"] * RATE_MUL, 0), "model": x.get("model"), "version": x.get("firmwareVersion")}


def poll_unifi(devs, ex):  # saves a sample for every device a site knows about and returns the rest, which are polled over SSH
    got, names, rest = {}, {}, devs
    for c in unifi_sites():  # the first site that knows a device wins
        try:
            part = unifi_read(c, rest, ex)
        except (UnifiError, KeyError, TypeError, AttributeError) as e:
            msg = str(e) if isinstance(e, UnifiError) else "Unexpected reply from the controller."
            UF["state"][c["id"]] = {**UF["state"].get(c["id"], {}), "ok": False, "error": msg, "ts": time.time()}
            watch("unifi", c["name"], False, msg)
            for d in rest:  # during an outage the devices it answered for last time show the problem; the others carry on over SSH
                if API_NAMES.get(d["name"]) == c["id"]:
                    names[d["name"]] = c["id"]
                    save_sample(d["name"], {"ts": time.time(), "error": f"UniFi API ({c['name']}): {msg}"}, notify=False)
            rest = [d for d in rest if d["name"] not in names]
            continue
        watch("unifi", c["name"], True)
        got.update(part)
        names.update({n: c["id"] for n in part})
        rest = [d for d in rest if d["name"] not in part]
    API_NAMES.clear()
    API_NAMES.update(names)
    for k in [k for k in UF["state"] if k not in {c["id"] for c in unifi_sites()}]:  # a removed site is forgotten
        UF["state"].pop(k, None)
        UF["conn"].pop(k, None)
    notify_prune("unifi", {c["name"] for c in unifi_sites()})

    def one(name):
        x, st, n = got[name]
        r = api_sample(x, st)
        if n is not None and "error" not in r:
            r["clients"] = n
        save_sample(name, r)
    list(ex.map(one, got))
    return rest


# ---- finding new devices: the sites' device lists are added to the device list by hand or on a schedule
def ignore(host, on):  # hosts removed from the list by hand are skipped by the device check until they are added back
    if not unifi_sites():
        return
    ip = addr(host)
    with jlock:
        j = unifi_raw()
        ig = set(j.get("ignored") if isinstance(j.get("ignored"), list) else [])
        if (ip in ig) != on:
            ig.add(ip) if on else ig.discard(ip)
            j["ignored"] = sorted(ig)
            write_private(UNIF, j)


def discover():  # adds devices the sites know about that aren't in the list yet -> ([{name, host, site} added], {site name: error})
    found, errs = [], {}
    for c in unifi_sites():
        try:
            base, site, ver, kind = unifi_connect(c)
            found += [(c["name"], x) for x in unifi_all(c, base, f"/v1/sites/{site}/devices")]
        except (UnifiError, KeyError, TypeError, AttributeError) as e:
            errs[c["name"]] = str(e) if isinstance(e, UnifiError) else "Unexpected reply from the controller."
    added = []
    with jlock:
        lst, skip = items("devices"), set(unifi_saved()["ignored"])
        known, names = {addr(d["host"]) for d in lst}, {d["name"] for d in lst}
        for site, x in found:
            ip = x.get("ipAddress")
            if not ip or ip in known or ip in skip or not HOST.match(ip):
                continue
            name = (str(x.get("name") or x.get("model") or ip).strip() or ip)[:60]
            if name in names:  # a clash: tell them apart by address
                name = f"{name[:50]} ({ip})"
            lst.append({"name": name, "host": ip})
            known.add(ip)
            names.add(name)
            added.append({"name": name, "host": ip, "site": site})
        if added:
            save_items("devices", lst)
            wake.set()
    return added, errs


def run_discovery():  # one device check, remembered in the settings file; None when another is already running
    if not dlock.acquire(False):
        return None
    try:
        added, errs = discover()
        with jlock:
            j = unifi_raw()
            j["discover"] = {**(j["discover"] if isinstance(j.get("discover"), dict) else {}), "last_run": time.time(), "last_added": [a["name"] for a in added], "last_errors": errs}
            write_private(UNIF, j)
        return added, errs
    finally:
        dlock.release()


def slot(at, now, days=0):  # the daily time HH:MM (server time) on today's date, moved by days
    h, m = map(int, at.split(":"))
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, h, m, 0, 0, 0, -1))


def next_due(d, now):  # when the next automatic check is due (a time in the past means due now); None until the clock has started
    last = d["last_run"]
    if last is None:
        return None
    if d["mode"] == "hours":
        return last + d["hours"] * 3600
    today = slot(d["at"], now)
    prev = today if today <= now else slot(d["at"], now, -1)  # the most recent scheduled time
    if last < prev:
        return prev  # missed (the container was down, say): due now
    return slot(d["at"], now, 1) if today <= now else today


def dtick(now):  # one look at the schedule
    d = unifi_saved()["discover"]
    if not (d["enabled"] and unifi_sites()):
        return
    if d["last_run"] is None:  # starts the clock: the first automatic check is the next scheduled time, not right away
        with jlock:
            j = unifi_raw()
            j["discover"] = {**(j["discover"] if isinstance(j.get("discover"), dict) else {}), "last_run": now}
            write_private(UNIF, j)
    elif next_due(d, now) <= now:
        run_discovery()


def dloop():
    while True:
        time.sleep(30)
        try:
            dtick(time.time())
        except Exception as e:
            print("device check error:", e, flush=True)


def loop():
    sql("CREATE TABLE IF NOT EXISTS samples (dev TEXT, ts REAL, data TEXT)")
    sql("CREATE INDEX IF NOT EXISTS i ON samples (dev, ts)")
    while True:
        t0 = time.time()
        try:
            shared = {**cfg().get("ssh", {}), **env_ssh()}  # config.json "ssh" block, overridden by SSH_* env vars; a device's own keys win
            devs = [{**shared, **d} for d in devices()]
            with ThreadPoolExecutor(8) as ex:
                if unifi_sites():
                    devs = poll_unifi(devs, ex)
                list(ex.map(poll, devs))
            prune()
            notify_prune("device", {d["name"] for d in devices()})
        except Exception as e:
            print("poll error:", e, flush=True)
        wake.wait(max(1, interval() - (time.time() - t0)))
        wake.clear()


@app.get("/")
def index():
    return send_file("index.html")


def minutes_arg():  # ?minutes= from the page, kept to something sensible
    try:
        return max(1, min(max(1440, retention() * 1440), int(request.args.get("minutes", 60))))
    except ValueError:
        return 60


@app.get("/api/data")
def data():
    mins, out, names = minutes_arg(), [], {c["id"]: c["name"] for c in unifi_sites()}
    for d in devices():  # passwords never leave the server
        if mins > 1440:
            series = long_samples(d["name"], mins)
        else:
            rows = sql("SELECT data FROM samples WHERE dev=? AND ts>? ORDER BY ts", (d["name"], time.time() - mins * 60))
            series = [json.loads(r[0]) for r in rows]
            step = max(1, len(series) // 300)  # downsample, always keeping the newest sample
            series = series[::-1][::step][::-1]
        out.append({"name": d["name"], "host": d["host"], "site": names.get(API_NAMES.get(d["name"])), "series": series})  # site: the one that answered for it last, none when it is polled over SSH
    return jsonify(interval=interval(), server_interval=conf()["server_interval"], version=VERSION, retention_days=retention(), sites=list(names.values()) if len(names) > 1 else [], devices=out)


@app.post("/api/settings")
@locked
def set_settings():
    j, s = request.get_json(silent=True) or {}, conf()
    try:
        for k in ("interval", "server_interval"):
            if k in j:
                s[k] = max(5, min(3600, int(j[k])))
        if "idle_minutes" in j:
            s["idle_minutes"] = max(0, min(1440, int(j["idle_minutes"])))  # 0 = never sign out
        if "retention_days" in j:
            s["retention_days"] = max(1, min(365, int(j["retention_days"])))
        if "max_attempts" in j:  # wrong passwords allowed in five minutes before a lock-out
            s["max_attempts"] = max(1, min(20, int(j["max_attempts"])))
    except (TypeError, ValueError):
        return bad("Enter a whole number.")
    if "guest" in j:
        s["guest"] = bool(j["guest"])
    if "icon_source" in j:  # which icon collection names are looked up in; a change clears the cache so icons are found again
        k = str(j["icon_source"])
        if k not in ICON_SOURCES:
            return bad("Unknown icon source.")
        if k != s["icon_source"]:
            s["icon_source"] = k
            clear_icons()
    if "repo" in j:  # the GitHub repository the Feedback page opens issues on; blank = the default
        r = str(j["repo"]).strip()
        if r and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", r):
            return bad("Use the form owner/name, for example matt-waldeck/beacon.")
        s["repo"] = r
    write_json(SET, s)
    wake.set()
    swake.set()
    return jsonify({**s, "repo": s["repo"] or DEFAULT_REPO})


HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,251}$")
USER = os.environ.get("UI_USER", "admin")
AUTH_FILE = os.path.join(DATA, "auth.json")  # 0.2.x generated login, carried over once
USERS_FILE = os.path.join(DATA, "users.json")
BRAND_FILE = os.path.join(DATA, "brand.json")
AUTH_OFF = os.environ.get("UI_AUTH", "").lower() == "off"
LEVEL = {"admin": 3, "user": 2, "viewer": 1, "guest": 1}  # admin: everything, user: edit layouts and apps, viewer/guest: view only
fails = {}  # recent failed sign-ins per client


def flag(name):  # "1", "true", "yes" or "on" turn a switch on; "0" or blank leave it off
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


if flag("TRUST_PROXY"):  # behind a reverse proxy: take the client IP from X-Forwarded-For
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def secret():
    p = os.path.join(DATA, "secret.key")
    try:
        with open(p) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        with open(p, "w") as f:
            f.write(key)
        os.chmod(p, 0o600)
        return key


app.secret_key = secret()
app.config.update(SESSION_COOKIE_NAME="beacon_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=flag("COOKIE_SECURE"), PERMANENT_SESSION_LIFETIME=timedelta(days=30),
                  MAX_CONTENT_LENGTH=3_000_000)


def users():
    lst = read_json(USERS_FILE, [])
    return lst if isinstance(lst, list) else []


def save_users(lst):
    write_json(USERS_FILE, lst, indent=2)


def setup_users():
    lst, pw = users(), os.environ.get("UI_PASSWORD")
    before = json.dumps(lst)
    if not lst and os.path.exists(AUTH_FILE):  # 0.2.x: carry the generated password over
        try:
            with open(AUTH_FILE) as f:
                lst = [{"name": USER, "hash": json.load(f)["hash"], "role": "admin"}]
        except (KeyError, ValueError):
            pass
    if pw:  # UI_PASSWORD always sets that user's password: also the way back in if it is forgotten
        u = next((x for x in lst if x["name"] == USER), None)
        if not u:
            lst.append({"name": USER, "hash": generate_password_hash(pw), "role": "admin"})
        elif not check_password_hash(u["hash"], pw):
            u["hash"], u["role"] = generate_password_hash(pw), "admin"
    elif not lst:  # nobody yet: the first person to open the web UI creates the admin (POST /api/setup)
        print("First start: open Beacon in your browser to create the admin account (or set UI_PASSWORD to skip that step).", flush=True)
    if json.dumps(lst) != before:
        save_users(lst)


def needs_setup():
    return not AUTH_OFF and not users()


EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
DUMMY = generate_password_hash("not-a-real-password")  # keeps timing similar when the name is unknown
if AUTH_OFF:
    print("WARNING: sign-in disabled (UI_AUTH=off). Protect this app another way.", flush=True)
else:
    setup_users()


def who():
    if AUTH_OFF:
        return {"name": "admin", "role": "admin"}
    n = session.get("u")
    if n:
        u = next((x for x in users() if x["name"] == n), None)
        if u and session.get("h") == u["hash"][-12:]:
            now, idle = int(time.time()), conf()["idle_minutes"] * 60
            if idle and not session.get("r") and now - session.get("t", now) > idle:  # no "keep me signed in", and idle too long
                session.clear()
                g.expired = True
                return None
            return {"name": n, "role": u["role"]}
        session.clear()
    if session.get("g") and conf()["guest"]:
        return {"name": "Guest", "role": "guest"}
    return None


ADMIN_GET = ("/api/users", "/api/storage", "/api/hostkeys", "/api/security", "/api/unifi", "/api/notify")  # reading these needs an admin
PASSIVE = {"/api/data", "/api/servers"}  # background polling: does not count as someone using the page
OPEN = {("GET", "/vendor/chart.umd.min.js"), ("GET", "/"), ("GET", "/healthz"), ("GET", "/api/me"), ("GET", "/api/brand"), ("POST", "/api/login"), ("POST", "/api/setup")}


@app.before_request
def guard():
    m, p = ("GET" if request.method == "HEAD" else request.method), request.path
    if (m, p) in OPEN:
        return None
    me = who()
    if not me:
        exp = bool(g.get("expired"))
        return jsonify(error="You were signed out after a period of inactivity." if exp else "Please sign in.", expired=exp), 401
    need = 1 if p in ("/api/logout", "/api/me/password", "/api/activity") or (m == "GET" and p not in ADMIN_GET) else 2 if (m, p) in (("PUT", "/api/dash"), ("POST", "/api/icon/resolve")) else 3
    if LEVEL[me["role"]] < need:
        return jsonify(error="You don't have permission to do that."), 403
    if session.get("u") and not (m == "GET" and p in PASSIVE) and time.time() - session.get("t", 0) > 15:
        session["t"] = int(time.time())  # real use keeps a session alive


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "same-origin"
    r.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src * data:; frame-ancestors 'none'")
    r.headers.setdefault("Cache-Control", "no-store" if request.path.startswith("/api/") else "no-cache")
    return r


def write_private(path, obj):  # like write_json, but never readable by other users on the host
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    shutil.copyfile(path, path + ".bak")
    os.chmod(path + ".bak", 0o600)


@app.get("/api/unifi")
def unifi_info():  # admin: each site's status, and the device-check schedule (API keys are never sent)
    u, now, out = unifi_saved(), time.time(), []
    for c in unifi_sites():
        st = UF["state"].get(c["id"], {})
        out.append({"id": c["id"], "name": c["name"], "url": c["url"], "site": c["site"], "verify": c["verify"], "env": c["env"], "key": True,
                    "status": "waiting" if "ok" not in st else "connected" if st["ok"] else "error", "error": "" if st.get("ok", True) else st.get("error", ""), "warn": st.get("warn", ""),
                    **{k: st.get(k) for k in ("kind", "version", "devices", "matched", "last_ok")}})
    d = u["discover"]
    return jsonify(sites=out, discover={**d, "next": next_due(d, now) if d["enabled"] and out else None}, ignored=len(u["ignored"]), now=now, server_time=time.strftime("%H:%M %Z"))


@app.put("/api/unifi")
@locked
def set_unifi():  # replaces the list of sites and the schedule; a blank API key keeps the saved one
    j, cur, raw = request.get_json(silent=True) or {}, unifi_saved(), unifi_raw()
    old, rows, out, ids, names = {x["id"]: x for x in cur["sites"]}, j.get("sites", []), [], set(), {"environment"} if unifi_env() else set()
    if not isinstance(rows, list) or len(rows) > 20:
        return bad("Add up to 20 sites.")
    for r in rows:
        r = r if isinstance(r, dict) else {}
        name = str(r.get("name", "")).strip()[:40]
        if not name or name.lower() in names:
            return bad("Give every site its own name." if name else "Give every site a name.")
        names.add(name.lower())
        try:
            url = norm_url(r.get("url", ""))
        except ValueError as e:
            return bad(f"{name}: {e}")
        rid = str(r.get("id") or "")
        key = str(r.get("key") or "").strip() or old.get(rid, {}).get("key", "")
        if not re.fullmatch(r"\S{1,200}", key):
            return bad(f"Enter the API key for {name}.")
        rid = rid if rid in old and rid not in ids else secrets.token_hex(4)
        ids.add(rid)
        out.append({"id": rid, "name": name, "url": url, "key": key, "site": str(r.get("site") or "default").strip()[:80] or "default", "verify": r.get("verify") is True})
    d, new = j.get("discover") if isinstance(j.get("discover"), dict) else {}, dict(cur["discover"])
    if "enabled" in d:
        new["enabled"] = d["enabled"] is True
    if d.get("mode") in ("daily", "hours"):
        new["mode"] = d["mode"]
    if "hours" in d:
        try:
            new["hours"] = int(d["hours"])
        except (TypeError, ValueError):
            new["hours"] = 0
        if not 1 <= new["hours"] <= 168:
            return bad("Check every 1 to 168 hours.")
    if "at" in d:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(d["at"])):
            return bad("Enter the time as HH:MM.")
        new["at"] = d["at"]
    if any(new[k] != cur["discover"][k] for k in ("enabled", "mode", "hours", "at")):
        new["last_run"] = None  # a changed schedule starts its clock again
    raw["sites"] = out
    raw["discover"] = {**(raw["discover"] if isinstance(raw.get("discover"), dict) else {}), **{k: new[k] for k in ("enabled", "mode", "hours", "at", "last_run")}}
    write_private(UNIF, raw)
    for k in [k for k in UF["conn"] if k not in ids | {"env"}]:
        UF["conn"].pop(k, None)
        UF["state"].pop(k, None)
    wake.set()
    return jsonify(ok=True)


@app.post("/api/unifi/test")
def test_unifi():  # tries one site's settings without saving anything
    j = request.get_json(silent=True) or {}
    try:
        url = norm_url(j.get("url", ""))
    except ValueError as e:
        return bad(str(e))
    key = str(j.get("key") or "").strip() or next((x["key"] for x in unifi_saved()["sites"] if x["id"] == j.get("id")), "")
    if not key:
        return bad("Enter the API key.")
    c = {"id": "test", "name": "test", "url": url, "key": key, "site": str(j.get("site") or "").strip() or "default", "verify": j.get("verify") is True}
    try:
        base, site, ver, kind = unifi_connect(c, cache=False)
        n = len(unifi_all(c, base, f"/v1/sites/{site}/devices"))
    except (UnifiError, KeyError, TypeError, AttributeError) as e:
        return jsonify(ok=False, error=str(e) if isinstance(e, UnifiError) else "Unexpected reply from the controller.")
    return jsonify(ok=True, version=ver, kind=kind, devices=n)


@app.post("/api/unifi/discover")
def unifi_discover():  # look for new devices once, now
    if not unifi_sites():
        return bad("Set up a site first.")
    r = run_discovery()
    if r is None:
        return bad("A device check is already running.", 409)
    return jsonify(added=r[0], errors=r[1])


@app.post("/api/unifi/forget")
@locked
def unifi_forget():  # devices removed by hand can be found again
    j = unifi_raw()
    j["ignored"] = []
    write_private(UNIF, j)
    return jsonify(ok=True)


# ---- Notifications: a message to a webhook and/or an email (through your own mail server) when something stops answering and when it comes back.
# Set up in Settings > Notifications (notify.json, 0600: the webhook address and mail password never leave the server).
NOTIFY = os.path.join(DATA, "notify.json")
NEVENTS = ("device", "server", "unifi")  # network devices, servers and VMs, UniFi controllers
NFORMATS = ("generic", "discord", "slack", "ntfy")
NW = {}  # (kind, name) -> {"fails": checks failed in a row, "first": when the run began, "sent": whether it was reported}
NQ = queue.Queue()  # messages waiting to go out, so a slow mail server never holds up the checks
NLAST = {}  # per destination: how the last message went
nlock = threading.Lock()
ADDR = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+")


def notify_saved():  # the saved settings, tidied so a hand-edited file can't break anything
    j = read_json(NOTIFY, {})
    j = j if isinstance(j, dict) else {}
    w, m, ev = (j.get(k) if isinstance(j.get(k), dict) else {} for k in ("webhook", "email", "events"))
    return {"events": {k: ev.get(k, True) is not False for k in NEVENTS}, "after": whole(j.get("after", 3), 3, 1, 20),
            "webhook": {"enabled": w.get("enabled") is True, "url": str(w.get("url") or ""), "format": w.get("format") if w.get("format") in NFORMATS else "generic", "verify": w.get("verify") is not False},
            "email": {"enabled": m.get("enabled") is True, "host": str(m.get("host") or ""), "port": whole(m.get("port", 587), 587, 1, 65535), "security": m.get("security") if m.get("security") in ("none", "starttls", "ssl") else "starttls",
                      "user": str(m.get("user") or ""), "password": str(m.get("password") or ""), "verify": m.get("verify") is not False, "sender": str(m.get("sender") or ""), "to": str(m.get("to") or "")}}


def span(s):  # "5 min", "2 h", "3 days"
    return f"{int(s)} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{round(s / 3600)} h" if s < 129600 else f"{round(s / 86400)} days"


def notify_msg(kind, name, state, since, fails, why, now):
    noun, host = {"device": "Device", "server": "Server", "unifi": "UniFi controller"}[kind], ""
    if kind != "unifi":
        e = next((x for x in items("devices" if kind == "device" else "servers") if x.get("name") == name), {})
        host, noun = str(e.get("host", "")), "VM" if e.get("type") == "vm" else noun
    one = lambda t: re.sub(r"\s+", " ", str(t)).strip()  # one line, so a name can't break a header
    name, host, why = one(name), one(host), one(why)
    who, when = f"{name} ({host})" if host else name, time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(since))
    if state == "down":
        title = f"{name} is " + ("unreachable" if kind == "unifi" else "offline")
        text = f"{noun} {who} has failed {fails} check{'' if fails == 1 else 's'} in a row, since {when}." + (f" {why}" if why else "")
    else:
        title = f"{name} is " + ("reachable again" if kind == "unifi" else "back online")
        text, why = f"{noun} {who} is answering again after {span(now - since)} (down since {when}).", ""
    return {"event": f"{kind}_{state}", "state": state, "kind": kind, "name": name, "host": host, "reason": why, "since": since, "ts": now, "title": f"{brand()['name']}: {title}", "text": text}


def watch(kind, name, ok, why=""):  # every check's result comes through here: after `after` failures in a row it is reported, and again when it answers
    try:
        now = time.time()
        with nlock:
            w = NW.setdefault((kind, name), {"fails": 0, "first": None, "sent": False})
            if ok:
                first, sent, w["fails"], w["first"], w["sent"] = w["first"], w["sent"], 0, None, False
                if not sent:
                    return
                state, fails = "up", 0
            else:
                w["fails"] += 1
                w["first"] = w["first"] or now
                if w["sent"] or w["fails"] < notify_saved()["after"]:
                    return
                w["sent"], first, state, fails = True, w["first"], "down", w["fails"]
        NQ.put(notify_msg(kind, name, state, first, fails, why, now))
    except Exception as e:
        print("notification error:", e, flush=True)


def notify_prune(kind, keep):  # a removed device, server or site is forgotten
    with nlock:
        for k in [k for k in NW if k[0] == kind and k[1] not in keep]:
            del NW[k]


def mime_header(t):  # ntfy reads non-ASCII header text in this form
    return t if t.isascii() else "=?UTF-8?B?" + base64.b64encode(t.encode()).decode() + "?="


def tls_context(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return ctx


def post_webhook(w, m):
    h, f, down = {"User-Agent": f"Beacon/{VERSION}"}, w["format"], m["state"] == "down"
    if f == "ntfy":  # plain text, with the title and a priority in headers
        body = m["text"].encode()
        h.update({"Content-Type": "text/plain; charset=utf-8", "Title": mime_header(m["title"]), "Priority": "high" if down else "default", "Tags": "rotating_light" if down else "white_check_mark"})
    else:
        obj = {"discord": {"content": f"**{m['title']}**\n{m['text']}"}, "slack": {"text": f"*{m['title']}*\n{m['text']}"}}.get(f) or {
            "source": "beacon", "version": VERSION, "event": m["event"], "state": m["state"], "kind": m["kind"], "name": m["name"], "host": m["host"], "reason": m["reason"], "since": m["since"], "time": m["ts"], "title": m["title"], "message": m["text"]}
        body, h["Content-Type"] = json.dumps(obj).encode(), "application/json"
    with urllib.request.urlopen(urllib.request.Request(w["url"], data=body, headers=h, method="POST"), timeout=10, context=tls_context(w["verify"])) as r:
        r.read(1000)


def send_mail(c, m):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"], msg["Date"], msg["Message-ID"] = m["title"], c["sender"], c["to"], formatdate(localtime=True), make_msgid()
    msg.set_content(m["text"] + f"\n\nSent by {brand()['name']}.")
    ctx = tls_context(c["verify"])
    with (smtplib.SMTP_SSL(c["host"], c["port"], timeout=15, context=ctx) if c["security"] == "ssl" else smtplib.SMTP(c["host"], c["port"], timeout=15)) as s:
        if c["security"] == "starttls":
            s.starttls(context=ctx)
        if c["user"]:
            s.login(c["user"], c["password"])
        s.send_message(msg)


def notify_error(e):  # a short reason a message didn't go out; never the webhook address or the password
    if isinstance(e, urllib.error.HTTPError):
        return f"The webhook answered with an error ({e.code} {e.reason})."
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, Exception):
        e = e.reason
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "The mail server refused the user name or password."
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the To address."
    if isinstance(e, smtplib.SMTPSenderRefused):
        return "The mail server refused the From address."
    if isinstance(e, ssl.SSLError):
        return f"The secure connection failed ({str(getattr(e, 'reason', None) or e)[:80]}). Check the Security setting and port, or untick Check the certificate."
    if isinstance(e, smtplib.SMTPException):
        return "The mail server said: " + str(e)[:150]
    if isinstance(e, TimeoutError):
        return "It took too long to answer."
    if isinstance(e, OSError):
        return "Couldn't connect: " + str(e.strerror or e)[:100]
    return "Unexpected error: " + type(e).__name__


def notify_worker():
    while True:
        m = NQ.get()
        try:
            c = notify_saved()
            if not c["events"].get(m["kind"]):
                continue
            for which, send in (("webhook", post_webhook), ("email", send_mail)):
                if c[which]["enabled"]:
                    try:
                        send(c[which], m)
                        NLAST[which] = {"ts": time.time(), "ok": True, "error": ""}
                    except Exception as e:
                        NLAST[which] = {"ts": time.time(), "ok": False, "error": notify_error(e)}
                        print(f"notification to the {which} failed: {NLAST[which]['error']}", flush=True)
        except Exception as e:
            print("notification error:", e, flush=True)


def notify_dest(which, j, cur, test=False):  # one destination as the page sends it, checked; a blank secret keeps the saved one -> (settings, error)
    j = j if isinstance(j, dict) else {}
    on = j["enabled"] is True if "enabled" in j else cur["enabled"]
    need = test or on
    ver = j["verify"] is True if "verify" in j else cur["verify"]
    if which == "webhook":
        url = str(j.get("url") or "").strip() or cur["url"]
        try:
            p = urllib.parse.urlsplit(url)
            good = p.scheme in ("http", "https") and bool(p.hostname) and len(url) <= 500
        except ValueError:
            good = False
        if (url and not good) or (need and not url):
            return None, "Enter the webhook address, starting with http:// or https://."
        return {"enabled": on, "url": url, "format": j.get("format") if j.get("format") in NFORMATS else cur["format"], "verify": ver}, None
    host, sec = str(j.get("host", cur["host"])).strip(), j.get("security") if j.get("security") in ("none", "starttls", "ssl") else cur["security"]
    try:
        port = int(j.get("port", cur["port"]))
    except (TypeError, ValueError):
        port = 0
    user = str(j.get("user", cur["user"])).strip()[:200]
    pw = (str(j.get("password") or "") or cur["password"])[:200] if user else ""
    sender, to = str(j.get("sender", cur["sender"])).strip(), [x for x in re.split(r"[,;\s]+", str(j.get("to", cur["to"]))) if x]
    if host and not HOST.match(host):
        return None, "That doesn't look like a mail server name or address."
    if not 1 <= port <= 65535:
        return None, "Enter a port from 1 to 65535."
    if sender and not ADDR.fullmatch(sender):
        return None, "Enter the From address as name@example.com."
    if len(to) > 10 or not all(ADDR.fullmatch(x) for x in to):
        return None, "Check the To addresses: up to 10, separated by commas."
    if need and not (host and sender and to):
        return None, "Fill in the server, the From address and at least one To address."
    if need and user and not pw:
        return None, "Enter the password for that user name."
    return {"enabled": on, "host": host, "port": port, "security": sec, "user": user, "password": pw, "verify": ver, "sender": sender, "to": ", ".join(to)}, None


@app.get("/api/notify")
def notify_info():  # admin: what is sent and where. The webhook address and the mail password are never sent back.
    c = notify_saved()
    w, m = c["webhook"], c["email"]
    try:
        hint = urllib.parse.urlsplit(w["url"]).hostname or ""
    except ValueError:
        hint = ""
    return jsonify(events=c["events"], after=c["after"], now=time.time(), last=NLAST,
                   webhook={"enabled": w["enabled"], "format": w["format"], "verify": w["verify"], "set": bool(w["url"]), "hint": hint},
                   email={**{k: m[k] for k in ("enabled", "host", "port", "security", "user", "verify", "sender", "to")}, "password": bool(m["password"])})


@app.put("/api/notify")
@locked
def set_notify():
    j, cur = request.get_json(silent=True) or {}, notify_saved()
    ev = j.get("events") if isinstance(j.get("events"), dict) else {}
    new = {"events": {k: ev[k] is True if k in ev else cur["events"][k] for k in NEVENTS}, "after": cur["after"]}
    if "after" in j:
        try:
            new["after"] = int(j["after"])
        except (TypeError, ValueError):
            new["after"] = 0
        if not 1 <= new["after"] <= 20:
            return bad("Wait for 1 to 20 failed checks before sending.")
    for which in ("webhook", "email"):
        new[which], err = notify_dest(which, j[which], cur[which]) if which in j else (cur[which], None)
        if err:
            return bad(err)
    write_private(NOTIFY, new)
    return notify_info()


@app.post("/api/notify/test")
def test_notify():  # sends one message using the settings on the page, saving nothing
    j, cur = request.get_json(silent=True) or {}, notify_saved()
    which = j.get("which")
    if which not in ("webhook", "email"):
        return bad("Unknown destination.")
    d, err = notify_dest(which, j, cur[which], test=True)
    if err:
        return jsonify(ok=False, error=err)
    m = {"event": "test", "state": "test", "kind": "test", "name": brand()["name"], "host": "", "reason": "", "since": None, "ts": time.time(),
         "title": f"{brand()['name']}: test message", "text": "If you can read this, notifications from Beacon will reach you here."}
    try:
        (post_webhook if which == "webhook" else send_mail)(d, m)
    except Exception as e:
        return jsonify(ok=False, error=notify_error(e))
    return jsonify(ok=True)


@app.get("/healthz")
def healthz():
    return "ok"


def fields():
    j = request.get_json(silent=True) or {}
    name, host = str(j.get("name", "")).strip()[:60], str(j.get("host", "")).strip()
    if not name or not HOST.match(host):
        raise ValueError("Enter a name and a valid IP address or hostname.")
    return name, host


def extra(kind):  # a server entry can be a physical server or a virtual machine
    if kind != "servers":
        return {}
    return {"type": "vm" if (request.get_json(silent=True) or {}).get("type") == "vm" else "server"}


def bad(m, code=400):
    return jsonify(error=m), code


TABLES = {"devices": ("samples", wake), "servers": ("pings", swake)}  # history table and wake-up event per list


@app.post("/api/<kind>")
@locked
def add_item(kind):
    if kind not in TABLES:
        return bad("Unknown list.", 404)
    try:
        name, host = fields()
    except ValueError as e:
        return bad(str(e))
    lst = items(kind)
    if any(d["name"] == name for d in lst):
        return bad("That name is already in use.")
    save_items(kind, lst + [{"name": name, "host": host, **extra(kind)}])
    if kind == "devices":
        ignore(host, False)
    TABLES[kind][1].set()  # check right away
    return jsonify(ok=True)


@app.put("/api/<kind>/<path:old>")
@locked
def edit_item(kind, old):
    if kind not in TABLES:
        return bad("Unknown list.", 404)
    try:
        name, host = fields()
    except ValueError as e:
        return bad(str(e))
    lst = items(kind)
    cur = next((d for d in lst if d["name"] == old), None)
    if not cur:
        return bad("Not found.", 404)
    if name != old and any(d["name"] == name for d in lst):
        return bad("That name is already in use.")
    cur.update(name=name, host=host, **extra(kind))
    save_items(kind, lst)
    if kind == "devices":
        ignore(host, False)
    if name != old:
        sql(f"UPDATE {TABLES[kind][0]} SET dev=? WHERE dev=?", (name, old))  # history follows the rename
    prev.pop(old, None)
    TABLES[kind][1].set()
    return jsonify(ok=True)


@app.delete("/api/<kind>/<path:name>")
@locked
def delete_item(kind, name):
    if kind not in TABLES:
        return bad("Unknown list.", 404)
    gone = next((d for d in items(kind) if d["name"] == name), None)
    save_items(kind, [d for d in items(kind) if d["name"] != name])
    if kind == "devices" and gone:
        ignore(gone["host"], True)
    sql(f"DELETE FROM {TABLES[kind][0]} WHERE dev=?", (name,))
    prev.pop(name, None)
    return jsonify(ok=True)


TILE_TYPES = ("stat", "chart", "summary", "app", "list")


def clean_tile(x):
    if not isinstance(x, dict) or x.get("type") not in TILE_TYPES:
        return None
    def num(k, lo, hi, d):
        try:
            return max(lo, min(hi, int(x.get(k))))
        except (TypeError, ValueError):
            return d
    w = num("w", 1, 12, 3)
    t = {"id": str(x.get("id") or secrets.token_hex(3))[:16], "type": x["type"], "ref": str(x.get("ref", ""))[:40],
         "x": num("x", 0, 12 - w, 0), "y": num("y", 0, 400, 0), "w": w, "h": num("h", 1, 40, 3)}
    if x.get("mode") in ("total", "rx", "tx"):  # the throughput tile: show total, received or sent
        t["mode"] = x["mode"]
    return t


def clean_dash(d):  # whatever is sent, only well-formed tiles and apps are kept
    apps, tabs = {}, {}
    if isinstance(d.get("apps"), dict):
        for k, a in list(d["apps"].items())[:500]:
            if isinstance(a, dict) and re.fullmatch(r"[\w-]{1,32}", str(k)):
                apps[str(k)] = {f: str(a.get(f, ""))[:n] for f, n in (("name", 60), ("url", 500), ("icon", 500))}
                apps[str(k)]["name"] = apps[str(k)]["name"].strip() or "App"
    if isinstance(d.get("tabs"), dict):
        for t in ("home", "network", "servers", "apps"):
            if isinstance(d["tabs"].get(t), list):
                tabs[t] = [c for c in (clean_tile(x) for x in d["tabs"][t][:200]) if c]
    return {"v": str(d.get("v", ""))[:8], "apps": apps, "tabs": tabs}


@app.get("/api/dash")
def get_dash():  # tab layouts and the app catalog
    d = read_json(os.path.join(DATA, "dash.json"), {})
    return jsonify(clean_dash(d) if isinstance(d, dict) and d else {})


@app.put("/api/dash")
@locked
def put_dash():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return bad("Expected an object.")
    write_json(os.path.join(DATA, "dash.json"), clean_dash(d))
    for a in clean_dash(d)["apps"].values():  # look icons up in the background so they're ready
        if not a["icon"]:
            queue_icon(a["name"])
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(user=who(), setup=needs_setup(), guest=conf()["guest"], auth=not AUTH_OFF, version=VERSION, idle=conf()["idle_minutes"], icons=conf()["icon_source"], remember=bool(session.get("r")), repo=conf()["repo"] or DEFAULT_REPO)


def recent_fails(key):  # failed attempts in the last five minutes for an address (sign-in) or a name (password change)
    now = time.time()
    if len(fails) > 500:  # forget keys with no recent failures
        for k in [k for k, v in fails.items() if not [t for t in v if now - t < 300]]:
            del fails[k]
    fails[key] = [t for t in fails.get(key, []) if now - t < 300]
    return fails[key]


@app.post("/api/login")
def login():
    j = request.get_json(silent=True) or {}
    if j.get("guest"):
        if not conf()["guest"]:
            return bad("Guest access is turned off.", 403)
        session.clear()
        session["g"], session.permanent = 1, True
        return jsonify(ok=True)
    now = time.time()
    recent = recent_fails(request.remote_addr)
    if len(recent) >= conf()["max_attempts"]:
        return bad("Too many attempts. Try again in a few minutes.", 429)
    name = str(j.get("name", "")).strip().lower()
    u = next((x for x in users() if x["name"].lower() == name), None)
    ok = check_password_hash(u["hash"] if u else DUMMY, str(j.get("password", "")))
    if not (u and ok):
        recent.append(now)
        return bad("Wrong name or password.", 401)
    session.clear()
    session["u"], session["h"], session.permanent = u["name"], u["hash"][-12:], bool(j.get("remember"))
    session["t"], session["r"] = int(time.time()), 1 if j.get("remember") else 0
    return jsonify(ok=True)


@app.post("/api/setup")
@locked
def setup():  # first run only: create the admin, then sign them in. Closed for good once any user exists.
    if not needs_setup():
        return bad("Setup is already complete.", 403)
    j = request.get_json(silent=True) or {}
    name, email, pw = str(j.get("name", "")).strip()[:40] or USER, str(j.get("email", "")).strip()[:320], str(j.get("password", ""))
    if not EMAIL.match(email):
        return bad("Enter a valid email address.")
    if len(pw) < 8:
        return bad("Use at least 8 characters.")
    u = {"name": name, "hash": generate_password_hash(pw), "role": "admin", "email": email}
    save_users([u])
    session.clear()
    session["u"], session["h"], session.permanent = u["name"], u["hash"][-12:], True
    session["t"], session["r"] = int(time.time()), 1
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/me/password")
@locked
def change_password():
    j, lst = request.get_json(silent=True) or {}, users()
    u = next((x for x in lst if x["name"] == who()["name"]), None)
    if not u:
        return bad("Guests can't change a password.", 403)
    recent = recent_fails("pw:" + u["name"])  # guessing the current password is limited like signing in
    if len(recent) >= conf()["max_attempts"]:
        return bad("Too many attempts. Try again in a few minutes.", 429)
    if not check_password_hash(u["hash"], str(j.get("current", ""))):
        recent.append(time.time())
        return bad("Current password is wrong.")
    if len(str(j.get("new", ""))) < 8:
        return bad("Use at least 8 characters.")
    u["hash"] = generate_password_hash(str(j["new"]))
    save_users(lst)
    session["h"] = u["hash"][-12:]  # this session stays signed in, others are signed out
    return jsonify(ok=True)


@app.get("/api/users")
def list_users():
    return jsonify(users=[{"name": u["name"], "role": u["role"], "email": u.get("email", "")} for u in users()], guest=conf()["guest"])


@app.post("/api/users")
@locked
def add_user():
    j, lst = request.get_json(silent=True) or {}, users()
    name, pw, role = str(j.get("name", "")).strip()[:40], str(j.get("password", "")), j.get("role", "viewer")
    if not name or role not in ("admin", "user", "viewer"):
        return bad("Enter a name and a role.")
    if len(pw) < 8:
        return bad("Use at least 8 characters.")
    if any(u["name"].lower() == name.lower() for u in lst):
        return bad("That name is already in use.")
    save_users(lst + [{"name": name, "hash": generate_password_hash(pw), "role": role}])
    return jsonify(ok=True)


@app.put("/api/users/<path:name>")
@locked
def edit_user(name):
    j, lst = request.get_json(silent=True) or {}, users()
    u = next((x for x in lst if x["name"] == name), None)
    if not u:
        return bad("Not found.", 404)
    if "role" in j:
        if j["role"] not in ("admin", "user", "viewer"):
            return bad("Unknown role.")
        if u["role"] == "admin" and j["role"] != "admin" and sum(x["role"] == "admin" for x in lst) < 2:
            return bad("There has to be at least one admin.")
        u["role"] = j["role"]
    if j.get("password"):
        if len(str(j["password"])) < 8:
            return bad("Use at least 8 characters.")
        u["hash"] = generate_password_hash(str(j["password"]))
        if who()["name"] == name:
            session["h"] = u["hash"][-12:]
    save_users(lst)
    return jsonify(ok=True)


@app.delete("/api/users/<path:name>")
@locked
def delete_user(name):
    lst = users()
    u = next((x for x in lst if x["name"] == name), None)
    if not u:
        return bad("Not found.", 404)
    if u["role"] == "admin" and sum(x["role"] == "admin" for x in lst) < 2:
        return bad("There has to be at least one admin.")
    save_users([x for x in lst if x is not u])
    return jsonify(ok=True)


BRAND_DEF = {"name": "Beacon", "tab": "", "tagline": "", "logo": "", "favicon": ""}
IMG = re.compile(r"^data:image/(png|jpeg|webp|gif|svg\+xml|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/=]+$")


def brand():
    b = read_json(BRAND_FILE, {})
    return {**BRAND_DEF, **(b if isinstance(b, dict) else {})}


@app.get("/api/brand")
def get_brand():  # public: the sign-in screen needs it
    return jsonify(brand())


@app.put("/api/brand")
@locked
def put_brand():
    j, b = request.get_json(silent=True) or {}, brand()
    for k, n in (("name", 40), ("tab", 80), ("tagline", 140)):
        if k in j:
            b[k] = str(j[k]).strip()[:n]
    for k in ("logo", "favicon"):
        if k in j:
            v = j[k] or ""
            if v and (len(v) > 700000 or not IMG.match(v)):
                return bad("Use a PNG, JPG, WebP, GIF, SVG or ICO image under 500 KB.")
            b[k] = v
    b["name"] = b["name"] or "Beacon"
    write_json(BRAND_FILE, b)
    return jsonify(b)


def ping(host):
    try:
        out = subprocess.run(["ping", "-c", "1", "-W", "2", host], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"time[=<]([\d.]+)", out)
        return float(m.group(1)) if m else None  # None = no reply
    except FileNotFoundError:
        print("WARNING: the ping program is not installed, so servers will show as offline", flush=True)
        return None
    except Exception:
        return None


def sloop():
    sql("CREATE TABLE IF NOT EXISTS pings (dev TEXT, ts REAL, ms REAL)")
    sql("CREATE INDEX IF NOT EXISTS pi ON pings (dev, ts)")
    while True:
        t0 = time.time()
        try:
            with ThreadPoolExecutor(16) as ex:
                res = list(ex.map(lambda s: (s["name"], ping(s["host"])), items("servers")))
            for n, ms in res:
                sql("INSERT INTO pings VALUES (?,?,?)", (n, t0, ms))
                watch("server", n, ms is not None, "It doesn't answer ping.")
            notify_prune("server", {n for n, _ in res})
        except Exception as e:
            print("ping error:", e, flush=True)
        swake.wait(max(1, conf()["server_interval"] - (time.time() - t0)))
        swake.clear()


@app.get("/api/servers")
def servers_data():
    mins, out = minutes_arg(), []
    for s in items("servers"):
        if mins > 1440:
            series = long_pings(s["name"], mins)
        else:
            rows = sql("SELECT ts, ms FROM pings WHERE dev=? AND ts>? ORDER BY ts", (s["name"], time.time() - mins * 60))
            series = [{"ts": r[0], "ms": r[1]} for r in rows]
            step = max(1, len(series) // 300)  # downsample, keeping the newest sample
            series = series[::-1][::step][::-1]
        out.append({"name": s["name"], "host": s["host"], "type": s.get("type", "server"), "series": series})
    return jsonify(interval=conf()["server_interval"], servers=out)


ICON_DIR = os.path.join(DATA, "icons")


def source_url(env, default):
    return (os.environ.get(env) or default).rstrip("/") + "/"


ICON_SOURCES = {  # key: (name shown, base address). Both serve /svg/<name>.svg and /png/<name>.png with lowercase-hyphen names
    "selfhst": ("selfh.st", source_url("SELFHST_ICONS_SOURCE", "https://cdn.jsdelivr.net/gh/selfhst/icons")),  # CC BY 4.0: credit is shown in Settings > About and the README
    "dashboard": ("Dashboard Icons", source_url("DASHBOARD_ICONS_SOURCE", "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons")),  # Apache-2.0
}
DEFAULT_ICONS = "selfhst"
MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "ico": "image/x-icon", "webp": "image/webp", "svg": "image/svg+xml"}
SIGNATURES = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif", b"\x00\x00\x01\x00": "ico"}
iconq, pending, icon_sem = queue.Queue(), set(), threading.BoundedSemaphore(3)


def norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())[:60]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:60]


def fetch(url, limit=400_000):  # (bytes, None), or (None, "missing" | "network")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Beacon"}), timeout=4) as r:
            data = r.read(limit + 1)
        return (data, None) if len(data) <= limit else (None, "missing")
    except urllib.error.HTTPError as e:
        return None, "missing" if e.code in (403, 404, 410) else "network"
    except Exception:
        return None, "network"


UNSAFE_SVG = re.compile(rb"<script|<foreignobject|<iframe|<embed|<object|<!entity|javascript:|\bon[a-z]+\s*=")  # scripts, embedded pages and event handlers never belong in an icon


def image_kind(data):  # what the bytes really are, or None when they aren't a safe image
    for sig, kind in SIGNATURES.items():
        if data.startswith(sig):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    low = data.lower()
    if data[:600].lstrip().lower().startswith((b"<svg", b"<?xml")) and b"<svg" in data[:2000].lower() and not UNSAFE_SVG.search(low):
        return "svg"
    return None


def icon_candidates(name):  # the chosen collection only (Settings > About); icon names are lowercase with hyphens
    label, base = ICON_SOURCES[conf()["icon_source"]]
    out, n, sl = [], norm(name), slug(name)
    for k in dict.fromkeys([sl, n]):
        if k:
            out += [(label, f"{base}svg/{k}.svg"), (label, f"{base}png/{k}.png")]
    return out


def clear_icons():  # cached icons and remembered misses; icons are fetched again when needed
    files = os.listdir(ICON_DIR) if os.path.isdir(ICON_DIR) else []
    for f in files:
        os.remove(os.path.join(ICON_DIR, f))
    return len([f for f in files if f.rsplit(".", 1)[-1] in MIME])


def icon_path(key):
    for ext in MIME:
        p = os.path.join(ICON_DIR, f"{key}.{ext}")
        if os.path.exists(p):
            return p


def resolve_icon(name):  # find an icon for an app name and keep a copy; remembers misses so it doesn't keep asking
    key = norm(name)
    if not key:
        return {"found": False, "why": "missing"}
    p = icon_path(key)
    if p:
        return {"found": True, "source": read_json(os.path.join(ICON_DIR, key + ".src"), {}).get("source", "cache")}
    miss_file = os.path.join(ICON_DIR, key + ".none")
    miss = read_json(miss_file, None)
    if isinstance(miss, dict) and time.time() - miss.get("t", 0) < (7 * 86400 if miss.get("why") == "missing" else 600):
        return {"found": False, "why": miss.get("why", "missing")}
    if not icon_sem.acquire(timeout=15):
        return {"found": False, "why": "busy"}
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        net, until = False, time.time() + 10  # give up after about ten seconds in total
        for source, url in icon_candidates(name):
            if time.time() > until:
                net = True
                break
            data, err = fetch(url)
            net = net or err == "network"
            kind = image_kind(data) if data else None
            if kind:
                with open(os.path.join(ICON_DIR, f"{key}.{kind}"), "wb") as f:
                    f.write(data)
                write_json(os.path.join(ICON_DIR, key + ".src"), {"source": source})
                return {"found": True, "source": source}
        why = "network" if net else "missing"
        write_json(miss_file, {"t": time.time(), "why": why})
        return {"found": False, "why": why}
    finally:
        icon_sem.release()


def queue_icon(name):  # fetched in the background, so a slow network never holds up the page
    k = norm(name)
    if k and k not in pending and not icon_path(k):
        pending.add(k)
        iconq.put(name)


def icon_worker():
    while True:
        name = iconq.get()
        try:
            resolve_icon(name)
        except Exception as e:
            print("icon error:", e, flush=True)
        finally:
            pending.discard(norm(name))


@app.get("/api/icon")
def get_icon():
    name = request.args.get("name", "")[:100]
    p = icon_path(norm(name))
    if not p:
        d = read_json(os.path.join(DATA, "dash.json"), {})  # only apps on the Apps page are looked up, so nobody can make the server fetch or store arbitrary names
        if isinstance(d, dict) and norm(name) in {norm(a.get("name", "")) for a in (d.get("apps") or {}).values() if isinstance(a, dict)}:
            queue_icon(name)
        return bad("No icon yet.", 404)
    r = send_file(p, mimetype=MIME[p.rsplit(".", 1)[1]])
    r.headers["Cache-Control"] = "private, max-age=86400"
    r.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return r


@app.post("/api/icon/resolve")
def post_icon():
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:100]
    return jsonify(resolve_icon(name)) if name else bad("Enter a name first.")


@app.get("/vendor/<path:name>")
def vendor(name):  # Chart.js is bundled into the image when it's built, so the dashboard works with no internet
    if name != "chart.umd.min.js":
        return bad("Not found.", 404)
    r = send_from_directory(os.environ.get("VENDOR_DIR") or os.path.join(app.root_path, "vendor"), name, mimetype="application/javascript")
    r.headers["Cache-Control"] = "public, max-age=86400"
    return r


def folder_size(path):
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs if os.path.exists(os.path.join(r, f)))


@app.get("/api/storage")
def storage():  # admin: how much room the history and the icon cache take
    db = sum(os.path.getsize(DB + x) for x in ("", "-wal", "-shm", "-journal") if os.path.exists(DB + x))
    n_s, first_s, n_p, first_p = sql("SELECT (SELECT COUNT(*) FROM samples), (SELECT MIN(ts) FROM samples), (SELECT COUNT(*) FROM pings), (SELECT MIN(ts) FROM pings)")[0]
    reusable = sql("PRAGMA freelist_count")[0][0] * sql("PRAGMA page_size")[0][0]
    files = [os.path.join(ICON_DIR, f) for f in os.listdir(ICON_DIR)] if os.path.isdir(ICON_DIR) else []
    firsts, disk = [t for t in (first_s, first_p) if t], shutil.disk_usage(DATA)
    return jsonify(db={"bytes": db, "reusable": reusable, "samples": n_s, "pings": n_p, "since": min(firsts) if firsts else None},
                   icons={"bytes": sum(os.path.getsize(f) for f in files), "count": len([f for f in files if f.rsplit(".", 1)[-1] in MIME])},
                   data={"bytes": folder_size(DATA)}, disk={"free": disk.free, "total": disk.total}, retention_days=retention())


def known_keys():  # [(hostname, key type, base64 key)] as stored in known_hosts
    if not os.path.exists(KNOWN_HOSTS):
        return []
    with open(KNOWN_HOSTS) as f:
        return [tuple(p[:3]) for p in (line.split() for line in f if not line.startswith("#")) if len(p) >= 3]


def fingerprint(b64):  # the usual SHA256:... form shown by ssh
    try:
        return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("=")
    except ValueError:
        return ""


@app.get("/api/hostkeys")
def list_hostkeys():  # admin: the SSH keys Beacon has pinned, matched to your devices by address
    names = {}
    for d in devices():
        names.setdefault(d["host"], d["name"])
    with hklock:
        rows = known_keys()
    out = [{"host": h, "name": names.get(h[1:].split("]")[0] if h.startswith("[") else h, ""), "type": t, "fingerprint": fingerprint(k)} for h, t, k in rows]
    return jsonify(keys=sorted(out, key=lambda x: (not x["name"], (x["name"] or x["host"]).lower())))  # your devices first, then keys for addresses no longer in the list


@app.post("/api/hostkeys/forget")
@locked
def forget_hostkeys():  # admin: those devices are trusted again the next time they are polled
    j = request.get_json(silent=True) or {}
    everyone, hosts = j.get("all") is True, j.get("hosts")
    if not everyone and not (isinstance(hosts, list) and hosts and all(isinstance(h, str) for h in hosts)):
        return bad("Choose at least one device.")
    with hklock:
        if not os.path.exists(KNOWN_HOSTS):
            return jsonify(ok=True, removed=0)
        with open(KNOWN_HOSTS) as f:
            lines = f.read().splitlines()
        keep, removed = [], 0
        for line in lines:
            p = line.split()
            if len(p) >= 3 and not line.startswith("#") and (everyone or p[0] in hosts):
                removed += 1
            else:
                keep.append(line)
        if any(len(l.split()) >= 3 and not l.startswith("#") for l in keep):
            tmp = KNOWN_HOSTS + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(keep) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, KNOWN_HOSTS)
        else:
            os.remove(KNOWN_HOSTS)
    return jsonify(ok=True, removed=removed)


@app.get("/api/security")
def security():  # admin: the values the Security page shows
    return jsonify(max_attempts=conf()["max_attempts"])


@app.post("/api/storage/purge")
@locked
def purge():
    what = (request.get_json(silent=True) or {}).get("what")
    if what == "icons":  # icons are fetched again when needed
        return jsonify(ok=True, removed=clear_icons())
    if what == "history":  # graphs start again from now; devices, servers and settings stay
        n = sql("SELECT (SELECT COUNT(*) FROM samples) + (SELECT COUNT(*) FROM pings)")[0][0]
        sql("DELETE FROM samples")
        sql("DELETE FROM pings")
        prev.clear()
        sql("VACUUM")
        return jsonify(ok=True, removed=n)
    if what == "compact":  # hand freed space back to the disk
        sql("VACUUM")
        return jsonify(ok=True)
    return bad("Unknown action.")


@app.post("/api/activity")
def activity():  # the page reports that someone is really using it
    return jsonify(ok=True)


for _f in ("heimdall.json", "heimdall.json.bak"):  # left over from versions that also used Heimdall's app list
    if os.path.exists(os.path.join(DATA, _f)):
        os.remove(os.path.join(DATA, _f))
migrate()
threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=icon_worker, daemon=True).start()
for _a in (read_json(os.path.join(DATA, "dash.json"), {}).get("apps", {}) if isinstance(read_json(os.path.join(DATA, "dash.json"), {}), dict) else {}).values():
    if isinstance(_a, dict) and not _a.get("icon"):
        queue_icon(_a.get("name", ""))
threading.Thread(target=sloop, daemon=True).start()
threading.Thread(target=dloop, daemon=True).start()
threading.Thread(target=notify_worker, daemon=True).start()
