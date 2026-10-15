import hashlib, hmac, json, os, re, secrets, sqlite3, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
import paramiko
from flask import Flask, Response, jsonify, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

DATA = os.environ.get("DATA_DIR", "/data")
VERSION = "0.2.1"
CFG, SET, DB, DEV, SRV = (os.path.join(DATA, n) for n in ("config.json", "settings.json", "stats.db", "devices.json", "servers.json"))
# Read-only /proc reads work on every UniFi device; mca-dump adds model/firmware/client count where present.
CMD = "for f in uptime loadavg meminfo stat net/dev; do echo @@$f; cat /proc/$f; done; echo @@mca; mca-dump 2>/dev/null"
app, wake, swake, dblock, prev = Flask(__name__), threading.Event(), threading.Event(), threading.Lock(), {}


def cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}  # config.json is optional


def env_ssh():
    m = {"user": "SSH_USER", "password": "SSH_PASSWORD", "key_file": "SSH_KEY_FILE", "port": "SSH_PORT"}
    e = {k: os.environ[v] for k, v in m.items() if os.environ.get(v)}
    if "port" in e:
        e["port"] = int(e["port"])
    return e


PATHS = {"devices": DEV, "servers": SRV}


def items(kind):
    try:
        with open(PATHS[kind]) as f:
            return json.load(f)
    except FileNotFoundError:
        return cfg().get("devices", []) if kind == "devices" else []  # first run: devices seed from config.json


def save_items(kind, lst):
    with open(PATHS[kind], "w") as f:
        json.dump(lst, f, indent=2)


def devices():
    return items("devices")


def conf():
    try:
        with open(SET) as f:
            s = json.load(f)
    except Exception:
        s = {}
    return {"interval": s.get("interval", cfg().get("interval", 30)), "server_interval": s.get("server_interval", 15)}


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


def ssh_run(d):
    if "user" not in d:
        raise ValueError("No SSH user set: use SSH_USER or the ssh block in config.json.")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(d["host"], port=d.get("port", 22), username=d["user"], password=d.get("password"),
              key_filename=d.get("key_file"), timeout=8, look_for_keys=False, allow_agent=False)
    try:
        return c.exec_command(CMD, timeout=20)[1].read().decode(errors="replace")
    finally:
        c.close()


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
    try:
        m = json.loads(s.get("mca", ""))
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
    except ValueError:
        pass
    return r


def poll(d):
    try:
        r = parse(ssh_run(d), d)
    except Exception as e:
        r = {"ts": time.time(), "error": str(e)[:200]}
    sql("INSERT INTO samples VALUES (?,?,?)", (d["name"], r["ts"], json.dumps(r)))


def loop():
    sql("CREATE TABLE IF NOT EXISTS samples (dev TEXT, ts REAL, data TEXT)")
    sql("CREATE INDEX IF NOT EXISTS i ON samples (dev, ts)")
    while True:
        t0 = time.time()
        try:
            c = cfg()
            # Shared SSH login: config.json "ssh" block, overridden by SSH_* env vars; a device's own keys win.
            shared = {**c.get("ssh", {}), **env_ssh()}
            devs = [{**shared, **d} for d in devices()]
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(poll, devs))
            sql("DELETE FROM samples WHERE ts < ?", (t0 - 86400 * c.get("retention_days", 7),))
        except Exception as e:
            print("poll error:", e, flush=True)
        wake.wait(max(1, interval() - (time.time() - t0)))
        wake.clear()


@app.get("/")
def index():
    return send_file("index.html")


@app.get("/api/data")
def data():
    mins, out = int(request.args.get("minutes", 60)), []
    for d in devices():  # passwords never leave the server
        rows = sql("SELECT data FROM samples WHERE dev=? AND ts>? ORDER BY ts", (d["name"], time.time() - mins * 60))
        series = [json.loads(r[0]) for r in rows]
        step = max(1, len(series) // 300)  # downsample long ranges, always keeping the newest sample
        out.append({"name": d["name"], "host": d["host"], "series": series[::-1][::step][::-1]})
    return jsonify(interval=interval(), server_interval=conf()["server_interval"], version=VERSION, devices=out)


@app.post("/api/settings")
def set_settings():
    j, s = request.get_json(silent=True) or {}, conf()
    for k in ("interval", "server_interval"):
        if k in j:
            s[k] = max(5, min(3600, int(j[k])))
    with open(SET, "w") as f:
        json.dump(s, f)
    wake.set()
    swake.set()
    return jsonify(s)


HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,251}$")
USER = os.environ.get("UI_USER", "admin")
AUTH_FILE = os.path.join(DATA, "auth.json")
GOOD, fails = set(), {}  # credentials already verified (stored as hashes) and recent failed attempts per client
if os.environ.get("TRUST_PROXY"):  # behind a reverse proxy: take the client IP from X-Forwarded-For
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def setup_auth():
    # UI_AUTH=off: no login. UI_PASSWORD: use it. Otherwise generate a password on first start and keep only its hash.
    if os.environ.get("UI_AUTH", "").lower() == "off":
        print("WARNING: login disabled (UI_AUTH=off). Protect this app another way.", flush=True)
        return None
    if os.environ.get("UI_PASSWORD"):
        return generate_password_hash(os.environ["UI_PASSWORD"])
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)["hash"]
    except (FileNotFoundError, KeyError, ValueError):
        pw = secrets.token_urlsafe(12)
        h = generate_password_hash(pw)
        with open(AUTH_FILE, "w") as f:
            json.dump({"hash": h}, f)
        print(f"First start: sign in as '{USER}' with password {pw} (set UI_PASSWORD to choose your own)", flush=True)
        return h


PW_HASH = setup_auth()


@app.before_request
def guard():
    if not PW_HASH or request.path == "/healthz":
        return None
    a, ip, now = request.authorization, request.remote_addr, time.time()
    key = hashlib.sha256(f"{a.username}:{a.password or ''}".encode()).digest() if a else None
    if key in GOOD:
        return None
    fails[ip] = recent = [t for t in fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        return Response("Too many failed attempts. Try again in a few minutes.", 429, {"Retry-After": "300"})
    if a and hmac.compare_digest((a.username or "").encode(), USER.encode()) and check_password_hash(PW_HASH, a.password or ""):
        if len(GOOD) > 50:
            GOOD.clear()
        GOOD.add(key)
        return None
    if a:
        recent.append(now)
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Homelab"'})


@app.after_request
def headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                      "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src * data:; frame-ancestors 'none'"})
    r.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
    return r


@app.get("/healthz")
def healthz():
    return "ok"


@app.before_request
def guard():  # optional login: set UI_PASSWORD (and UI_USER, default "admin") to switch it on
    a = request.authorization
    want = f"{os.environ.get('UI_USER', 'admin')}:{AUTH}".encode()
    if AUTH and not (a and hmac.compare_digest(f"{a.username}:{a.password or ''}".encode(), want)):
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="UniFi stats"'})


def fields():
    j = request.get_json(silent=True) or {}
    name, host = str(j.get("name", "")).strip()[:60], str(j.get("host", "")).strip()
    if not name or not HOST.match(host):
        raise ValueError("Enter a name and a valid IP address or hostname.")
    return name, host


def bad(m, code=400):
    return jsonify(error=m), code


TABLES = {"devices": ("samples", wake), "servers": ("pings", swake)}  # history table and wake-up event per list


@app.post("/api/<kind>")
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
    save_items(kind, lst + [{"name": name, "host": host}])
    TABLES[kind][1].set()  # check right away
    return jsonify(ok=True)


@app.put("/api/<kind>/<path:old>")
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
    cur.update(name=name, host=host)
    save_items(kind, lst)
    if name != old:
        sql(f"UPDATE {TABLES[kind][0]} SET dev=? WHERE dev=?", (name, old))  # history follows the rename
    prev.pop(old, None)
    TABLES[kind][1].set()
    return jsonify(ok=True)


@app.delete("/api/<kind>/<path:name>")
def delete_item(kind, name):
    if kind not in TABLES:
        return bad("Unknown list.", 404)
    save_items(kind, [d for d in items(kind) if d["name"] != name])
    sql(f"DELETE FROM {TABLES[kind][0]} WHERE dev=?", (name,))
    prev.pop(name, None)
    return jsonify(ok=True)


@app.get("/api/dash")
def get_dash():  # tab layouts and the app catalog
    try:
        with open(os.path.join(DATA, "dash.json")) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({})


@app.put("/api/dash")
def put_dash():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return bad("Expected an object.")
    with open(os.path.join(DATA, "dash.json"), "w") as f:
        json.dump(d, f)
    return jsonify(ok=True)


def ping(host):
    try:
        out = subprocess.run(["ping", "-c", "1", "-W", "2", host], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"time[=<]([\d.]+)", out)
        return float(m.group(1)) if m else None  # None = no reply
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
            sql("DELETE FROM pings WHERE ts < ?", (t0 - 86400 * cfg().get("retention_days", 7),))
        except Exception as e:
            print("ping error:", e, flush=True)
        swake.wait(max(1, conf()["server_interval"] - (time.time() - t0)))
        swake.clear()


@app.get("/api/servers")
def servers_data():
    mins, out = int(request.args.get("minutes", 60)), []
    for s in items("servers"):
        rows = sql("SELECT ts, ms FROM pings WHERE dev=? AND ts>? ORDER BY ts", (s["name"], time.time() - mins * 60))
        series = [{"ts": r[0], "ms": r[1]} for r in rows]
        step = max(1, len(series) // 300)  # downsample long ranges, keeping the newest sample
        out.append({"name": s["name"], "host": s["host"], "series": series[::-1][::step][::-1]})
    return jsonify(interval=conf()["server_interval"], servers=out)


threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=sloop, daemon=True).start()
