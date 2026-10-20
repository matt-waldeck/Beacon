import hashlib, json, os, re, secrets, sqlite3, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import paramiko
from flask import Flask, g, jsonify, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

DATA = os.environ.get("DATA_DIR", "/data")
VERSION = "0.4.0"
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
    return {"interval": s.get("interval", cfg().get("interval", 30)), "server_interval": s.get("server_interval", 15), "guest": bool(s.get("guest", False)), "idle_minutes": int(s.get("idle_minutes", 10))}


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
    if "guest" in j:
        s["guest"] = bool(j["guest"])
    if "idle_minutes" in j:
        s["idle_minutes"] = max(0, min(1440, int(j["idle_minutes"])))  # 0 = never sign out
    with open(SET, "w") as f:
        json.dump(s, f)
    wake.set()
    swake.set()
    return jsonify(s)


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
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_users(lst):
    with open(USERS_FILE, "w") as f:
        json.dump(lst, f, indent=2)


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
    elif not lst:
        p = secrets.token_urlsafe(12)
        lst = [{"name": USER, "hash": generate_password_hash(p), "role": "admin"}]
        print(f"First start: sign in as '{USER}' with password {p} (set UI_PASSWORD to choose your own)", flush=True)
    if json.dumps(lst) != before:
        save_users(lst)


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


PASSIVE = {"/api/data", "/api/servers"}  # background polling: does not count as someone using the page
OPEN = {("GET", "/"), ("GET", "/healthz"), ("GET", "/api/me"), ("GET", "/api/brand"), ("POST", "/api/login")}


@app.before_request
def guard():
    m, p = ("GET" if request.method == "HEAD" else request.method), request.path
    if (m, p) in OPEN:
        return None
    me = who()
    if not me:
        exp = bool(g.get("expired"))
        return jsonify(error="You were signed out after a period of inactivity." if exp else "Please sign in.", expired=exp), 401
    need = 1 if p in ("/api/logout", "/api/me/password", "/api/activity") or (m == "GET" and p != "/api/users") else 2 if (m, p) == ("PUT", "/api/dash") else 3
    if LEVEL[me["role"]] < need:
        return jsonify(error="You don't have permission to do that."), 403
    if session.get("u") and not (m == "GET" and p in PASSIVE) and time.time() - session.get("t", 0) > 15:
        session["t"] = int(time.time())  # real use keeps a session alive


@app.after_request
def headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                      "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src * data:; frame-ancestors 'none'"})
    r.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
    return r


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
    cur.update(name=name, host=host, **extra(kind))
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
        out.append({"name": s["name"], "host": s["host"], "type": s.get("type", "server"), "series": series[::-1][::step][::-1]})
    return jsonify(interval=conf()["server_interval"], servers=out)


@app.get("/api/me")
def me():
    return jsonify(user=who(), guest=conf()["guest"], auth=not AUTH_OFF, version=VERSION, idle=conf()["idle_minutes"], remember=bool(session.get("r")))


@app.post("/api/login")
def login():
    j = request.get_json(silent=True) or {}
    if j.get("guest"):
        if not conf()["guest"]:
            return bad("Guest access is turned off.", 403)
        session.clear()
        session["g"], session.permanent = 1, True
        return jsonify(ok=True)
    ip, now = request.remote_addr, time.time()
    recent = fails[ip] = [t for t in fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
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


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/me/password")
def change_password():
    j, lst = request.get_json(silent=True) or {}, users()
    u = next((x for x in lst if x["name"] == who()["name"]), None)
    if not u:
        return bad("Guests can't change a password.", 403)
    if not check_password_hash(u["hash"], str(j.get("current", ""))):
        return bad("Current password is wrong.")
    if len(str(j.get("new", ""))) < 8:
        return bad("Use at least 8 characters.")
    u["hash"] = generate_password_hash(str(j["new"]))
    save_users(lst)
    session["h"] = u["hash"][-12:]  # this session stays signed in, others are signed out
    return jsonify(ok=True)


@app.get("/api/users")
def list_users():
    return jsonify(users=[{"name": u["name"], "role": u["role"]} for u in users()], guest=conf()["guest"])


@app.post("/api/users")
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
    try:
        with open(BRAND_FILE) as f:
            return {**BRAND_DEF, **json.load(f)}
    except FileNotFoundError:
        return dict(BRAND_DEF)


@app.get("/api/brand")
def get_brand():  # public: the sign-in screen needs it
    return jsonify(brand())


@app.put("/api/brand")
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
    with open(BRAND_FILE, "w") as f:
        json.dump(b, f)
    return jsonify(b)


@app.post("/api/activity")
def activity():  # the page reports that someone is really using it
    return jsonify(ok=True)


threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=sloop, daemon=True).start()
