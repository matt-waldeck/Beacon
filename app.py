import functools, hashlib, json, os, queue, re, secrets, shutil, sqlite3, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import paramiko
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

DATA = os.environ.get("DATA_DIR", "/data")
VERSION = "1.0.0-rc1"
DEFAULT_REPO = os.environ.get("GITHUB_REPO") or "matt-waldeck/beacon"  # where the Feedback page sends people
CFG, SET, DB, DEV, SRV = (os.path.join(DATA, n) for n in ("config.json", "settings.json", "stats.db", "devices.json", "servers.json"))
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


def conf():
    s = read_json(SET, {})
    s = s if isinstance(s, dict) else {}
    return {"interval": s.get("interval", cfg().get("interval", 30)), "server_interval": s.get("server_interval", 15), "guest": bool(s.get("guest", False)), "idle_minutes": int(s.get("idle_minutes", 10)), "repo": str(s.get("repo", ""))}


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
        m = json.loads(s.get("mca", ""))  # extras only: any odd shape just means no extras
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


def poll(d):
    try:
        r = parse(ssh_run(d), d)
    except (KeyError, IndexError):
        r = {"ts": time.time(), "error": "Unexpected reply from the device. Is it a UniFi (Linux) device?"}
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


def minutes_arg():  # ?minutes= from the page, kept to something sensible
    try:
        return max(1, min(10080, int(request.args.get("minutes", 60))))
    except ValueError:
        return 60


@app.get("/api/data")
def data():
    mins, out = minutes_arg(), []
    for d in devices():  # passwords never leave the server
        rows = sql("SELECT data FROM samples WHERE dev=? AND ts>? ORDER BY ts", (d["name"], time.time() - mins * 60))
        series = [json.loads(r[0]) for r in rows]
        step = max(1, len(series) // 300)  # downsample long ranges, always keeping the newest sample
        out.append({"name": d["name"], "host": d["host"], "series": series[::-1][::step][::-1]})
    return jsonify(interval=interval(), server_interval=conf()["server_interval"], version=VERSION, devices=out)


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
    except (TypeError, ValueError):
        return bad("Enter a whole number.")
    if "guest" in j:
        s["guest"] = bool(j["guest"])
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
OPEN = {("GET", "/vendor/chart.umd.min.js"), ("GET", "/"), ("GET", "/healthz"), ("GET", "/api/me"), ("GET", "/api/brand"), ("POST", "/api/login")}


@app.before_request
def guard():
    m, p = ("GET" if request.method == "HEAD" else request.method), request.path
    if (m, p) in OPEN:
        return None
    me = who()
    if not me:
        exp = bool(g.get("expired"))
        return jsonify(error="You were signed out after a period of inactivity." if exp else "Please sign in.", expired=exp), 401
    need = 1 if p in ("/api/logout", "/api/me/password", "/api/activity") or (m == "GET" and p != "/api/users") else 2 if (m, p) in (("PUT", "/api/dash"), ("POST", "/api/icon/resolve")) else 3
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
    save_items(kind, [d for d in items(kind) if d["name"] != name])
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
    return {"id": str(x.get("id") or secrets.token_hex(3))[:16], "type": x["type"], "ref": str(x.get("ref", ""))[:40],
            "x": num("x", 0, 12 - w, 0), "y": num("y", 0, 400, 0), "w": w, "h": num("h", 1, 40, 3)}


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
    return jsonify(user=who(), guest=conf()["guest"], auth=not AUTH_OFF, version=VERSION, idle=conf()["idle_minutes"], remember=bool(session.get("r")), repo=conf()["repo"] or DEFAULT_REPO)


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
    if len(fails) > 500:  # forget clients with no recent failures
        for k in [k for k, v in fails.items() if not [t for t in v if now - t < 300]]:
            del fails[k]
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
@locked
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
            sql("DELETE FROM pings WHERE ts < ?", (t0 - 86400 * cfg().get("retention_days", 7),))
        except Exception as e:
            print("ping error:", e, flush=True)
        swake.wait(max(1, conf()["server_interval"] - (time.time() - t0)))
        swake.clear()


@app.get("/api/servers")
def servers_data():
    mins, out = minutes_arg(), []
    for s in items("servers"):
        rows = sql("SELECT ts, ms FROM pings WHERE dev=? AND ts>? ORDER BY ts", (s["name"], time.time() - mins * 60))
        series = [{"ts": r[0], "ms": r[1]} for r in rows]
        step = max(1, len(series) // 300)  # downsample long ranges, keeping the newest sample
        out.append({"name": s["name"], "host": s["host"], "type": s.get("type", "server"), "series": series[::-1][::step][::-1]})
    return jsonify(interval=conf()["server_interval"], servers=out)


ICON_DIR = os.path.join(DATA, "icons")
HM_FILE = os.path.join(DATA, "heimdall.json")


def source_url(env, default):
    return (os.environ.get(env) or default).rstrip("/") + "/"


HEIMDALL = source_url("HEIMDALL_SOURCE", "https://appslist.heimdall.site")
DASH_ICONS = source_url("DASHBOARD_ICONS_SOURCE", "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons")
SELFHST = source_url("SELFHST_ICONS_SOURCE", "https://cdn.jsdelivr.net/gh/selfhst/icons")
MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "ico": "image/x-icon", "webp": "image/webp", "svg": "image/svg+xml"}
SIGNATURES = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif", b"\x00\x00\x01\x00": "ico"}
HM = {"map": None, "t": 0, "ok": None}  # Heimdall's app list: normalised name -> icon address
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


def image_kind(data):  # what the bytes really are, or None when they aren't a safe image
    for sig, kind in SIGNATURES.items():
        if data.startswith(sig):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:600].lstrip().lower().startswith((b"<svg", b"<?xml")) and b"<svg" in data[:2000].lower() and b"<script" not in data.lower():
        return "svg"
    return None


def heimdall_map():  # Heimdall's list.json, kept for a week; if it can't be reached, use the old copy and retry in 10 minutes
    week = 7 * 86400
    if HM["map"] is not None and time.time() - HM["t"] < week:
        return HM["map"]
    cached = read_json(HM_FILE, None)
    if isinstance(cached, dict) and time.time() - cached.get("t", 0) < week and isinstance(cached.get("map"), dict):
        HM.update(map=cached["map"], t=cached["t"], ok=True)
        return HM["map"]
    data, _ = fetch(HEIMDALL + "list.json", limit=8_000_000)
    found = {}
    try:
        raw = json.loads(data) if data else None
        apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
        for a in (apps.values() if isinstance(apps, dict) else apps or []):
            if isinstance(a, dict) and a.get("name") and a.get("icon") and re.fullmatch(r"[\w.() -]{1,100}", str(a["icon"])):
                found.setdefault(norm(a["name"]), HEIMDALL + "icons/" + urllib.parse.quote(str(a["icon"])))
    except (ValueError, AttributeError, TypeError):
        found = {}
    if found:
        HM.update(map=found, t=time.time(), ok=True)
        write_json(HM_FILE, {"t": HM["t"], "map": found})
    else:
        HM.update(map=cached.get("map", {}) if isinstance(cached, dict) else {}, t=time.time() - week + 600, ok=False)
    return HM["map"]


def icon_candidates(name):
    out, n, sl = [], norm(name), slug(name)
    if heimdall_map().get(n):
        out.append(("Heimdall", heimdall_map()[n]))
    for k in dict.fromkeys([sl, n]):
        if k:
            out += [("Dashboard Icons", f"{DASH_ICONS}svg/{k}.svg"), ("Dashboard Icons", f"{DASH_ICONS}png/{k}.png"),
                    ("selfh.st", f"{SELFHST}svg/{k}.svg"), ("selfh.st", f"{SELFHST}png/{k}.png")]
    return out


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
        return {"found": False, "why": miss.get("why", "missing"), "heimdall": "ok" if HM["ok"] else "unreachable"}
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
        return {"found": False, "why": why, "heimdall": "ok" if HM["ok"] else "unreachable"}
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


@app.post("/api/activity")
def activity():  # the page reports that someone is really using it
    return jsonify(ok=True)


threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=icon_worker, daemon=True).start()
for _a in (read_json(os.path.join(DATA, "dash.json"), {}).get("apps", {}) if isinstance(read_json(os.path.join(DATA, "dash.json"), {}), dict) else {}).values():
    if isinstance(_a, dict) and not _a.get("icon"):
        queue_icon(_a.get("name", ""))
threading.Thread(target=sloop, daemon=True).start()
