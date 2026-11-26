"""Server tests for the UniFi API source, the SSH extras, the saved sites and the device check. Run: python3 Tests/unifi_test.py  (needs Flask; no devices or network).
Starts two small fake controllers on localhost and runs Beacon's own code against them, with the paramiko stand-in."""
import contextlib, importlib.util, io, json, os, stat, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
HERE = Path(__file__).resolve().parent
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, UI_USER="boss", UI_PASSWORD="secret123", TZ="UTC")
time.tzset()
for p in (HERE / "stub", HERE):  # the stand-in wins even when the real paramiko is installed
    if (p / "paramiko.py").exists(): sys.path.insert(0, str(p)); break
with contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location("app", HERE.parent / "app.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = [0, 0]
def chk(label, got, want):
    bad[0] += 1
    if got != want: bad[1] += 1; print(f"  FAIL {label}: got {got!r}, wanted {want!r}")

SITE = "11111111-1111-1111-1111-111111111111"
STATS = {"uptimeSec": 4272049, "loadAverage1Min": 2.456, "cpuUtilizationPct": 4.84, "memoryUtilizationPct": 33.12, "uplink": {"rxRateBps": 8000, "txRateBps": 4000}}
def dev(i, name, ip, feat, state="ONLINE"): return {"id": i, "name": name, "ipAddress": ip, "model": "M", "firmwareVersion": "1.0", "state": state, "features": feat}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        c, u = self.server.cfg, self.path.split("?")[0]; c["seen"].append(self.path)
        if self.headers.get("X-API-KEY") != c["key"]: return self.send(401, {})
        if not u.startswith(c["base"]): return self.send(404, {})
        u = u[len(c["base"]):]
        pages = {"/v1/info": {"applicationVersion": c["ver"]}, "/v1/sites": {"data": [{"id": SITE, "internalReference": "default", "name": "Default"}], "totalCount": 1},
                 f"/v1/sites/{SITE}/devices": {"data": c["devs"], "totalCount": len(c["devs"])}, f"/v1/sites/{SITE}/clients": {"data": c["clients"], "totalCount": len(c["clients"])}}
        if u in pages: return self.send(200, pages[u])
        if u.endswith("/statistics/latest"): return self.send(200, STATS)
        self.send(404, {})
    def send(self, code, body):
        b = json.dumps(body).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
def serve(devs, key, base="/proxy/network/integration", ver="10.0.1", clients=()):
    s = HTTPServer(("127.0.0.1", 0), H); s.cfg = {"devs": devs, "key": key, "base": base, "ver": ver, "clients": list(clients), "seen": []}
    threading.Thread(target=s.serve_forever, daemon=True).start(); return s
S1 = serve([dev("ap-id", "Office AP", "10.0.0.2", ["accessPoint"]), dev("sw-id", "Switch", "10.0.0.3", ["switching"]), dev("off-id", "Garage AP", "10.0.0.4", ["accessPoint"], "OFFLINE")], "good-key",
           clients=[{"type": "WIRELESS", "uplinkDeviceId": "ap-id"}] * 3 + [{"type": "WIRELESS", "uplinkDeviceId": "other"}])
S2 = serve([dev("b-ap", "Shed AP", "10.0.1.2", ["accessPoint"]), dev("b-dup", "Office AP again", "10.0.0.2", ["accessPoint"])], "key-two", base="/integration", ver="9.4.2")
url = lambda s: f"http://127.0.0.1:{s.server_port}"
seen, mode = S1.cfg["seen"], S1.cfg

from concurrent.futures import ThreadPoolExecutor
a = m.app.test_client(); a.post("/api/login", json={"name": "boss", "password": "secret123"})
m.ssh_run = lambda d, cmd=m.CMD: ""  # adding devices wakes Beacon's own polling thread; let it finish its SSH-only round, then park it so only this test polls
for n, h in (("Office AP", "10.0.0.2"), ("Switch", "10.0.0.3"), ("Garage AP", "10.0.0.4"), ("NAS", "10.0.0.9")): a.post("/api/devices", json={"name": n, "host": h})
time.sleep(1.5); m.wake = threading.Event(); m.interval = lambda: 3600; m.TABLES = {k: (v[0], m.wake) for k, v in m.TABLES.items()}
os.environ.update(UNIFI_URL=url(S1), UNIFI_API_KEY="good-key")
ssh_calls = []
MCA = json.dumps({"model": "USW", "version": "7.0", "port_table": [{"poe_power": "10.5"}, {"poe_power": "4.5"}], "vap_table": [{"radio": "ng", "num_sta": 1}, {"radio": "na", "num_sta": 2}]})
def fake_ssh(d, cmd=m.CMD): ssh_calls.append((d["name"], cmd)); return MCA if "mca-dump" in cmd and "@@" not in cmd else ""
m.ssh_run = fake_ssh
def cycle():
    ssh_calls.clear(); devs = [{**m.ssh_login(), **d} for d in m.devices()]
    with ThreadPoolExecutor(4) as ex: rest = m.poll_unifi(devs, ex)
    return [d["name"] for d in rest]
last = lambda n: json.loads(m.sql("SELECT data FROM samples WHERE dev=? ORDER BY ts DESC LIMIT 1", (n,))[0][0])
st = lambda i="env": m.UF["state"].get(i, {})

print("== the API is the source ==")
chk("only the device the controller doesn't know is left for SSH", cycle(), ["NAS"])
ap, sw = last("Office AP"), last("Switch")
chk("AP numbers come from the API", (ap["cpu"], ap["mem"], ap["load"], ap["uptime"], ap["model"], ap["version"]), (4.8, 33.1, 2.46, 4272049, "M", "1.0"))
chk("throughput is the uplink rate", (ap["rx"], ap["tx"]), (8000, 4000))
chk("wireless clients are counted per AP, from one list", (ap["clients"], "clients" in sw), (3, False))
chk("an offline device shows the controller's word for it", last("Garage AP")["error"], "The controller reports this device as offline.")
chk("no SSH at all with the extras off", ssh_calls, [])
chk("PoE and bands are not invented", ("poe" in sw, "bands" in ap), (False, False))
chk("the controller's base path was found by probing", m.UF["conn"]["env"][1].endswith("/proxy/network/integration"), True)
chk("status says how many devices matched, and what kind of controller", (st()["ok"], st()["devices"], st()["matched"], st()["version"], st()["kind"]), (True, 3, 3, "10.0.1", "UniFi OS console"))
chk("the wireless filter was used", any("filter=type.eq" in p and "WIRELESS" in p for p in seen), True)
d = a.get("/api/data").get_json()
chk("the page is told PoE and bands are not collected", (d["has_poe"], d["has_bands"]), (False, False))

print("== the SSH extras ==")
mon = lambda: a.get("/api/monitoring").get_json()
chk("they are off by default", (mon()["poe_ssh"], mon()["bands_ssh"], mon()["configured"]), (False, False, True))
a.post("/api/users", json={"name": "v", "password": "password1", "role": "viewer"}); v = m.app.test_client(); v.post("/api/login", json={"name": "v", "password": "password1"})
chk("a viewer cannot switch them on or read them", (v.post("/api/settings", json={"poe_ssh": True}).status_code, v.get("/api/monitoring").status_code), (403, 403))
a.post("/api/settings", json={"poe_ssh": True}); cycle()
chk("PoE on: only the switch is asked, with mca-dump alone", ssh_calls, [("Switch", "mca-dump 2>/dev/null")])
chk("and its PoE draw is stored", last("Switch").get("poe"), 15.0)
chk("the AP's bands stay out", "bands" in last("Office AP"), False)
a.post("/api/settings", json={"poe_ssh": False, "bands_ssh": True}); cycle()
chk("bands on: only the AP is asked", [c[0] for c in ssh_calls], ["Office AP"])
chk("and its bands are stored, without the PoE", (last("Office AP").get("bands"), "poe" in last("Office AP")), ({"2.4": 1, "5": 2}, False))
chk("the page is told bands are collected", (a.get("/api/data").get_json()["has_bands"], a.get("/api/data").get_json()["has_poe"]), (True, False))
def fail(d, cmd=m.CMD): raise OSError("Authentication failed.")
m.ssh_run = fail; cycle()
chk("a failing SSH extra keeps the API numbers", (last("Office AP")["cpu"], "bands" in last("Office AP")), (4.8, False))
chk("and the problem is reported by device", mon()["errors"], {"Office AP": "Authentication failed."})
m.ssh_run = fake_ssh; a.post("/api/settings", json={"bands_ssh": False, "poe_ssh": False})

print("== the SSH login saved in Settings ==")
chk("none is set at first", mon()["ssh"]["user"], "")
chk("a bad user name is refused", a.post("/api/ssh-login", json={"user": "a b; rm"}).status_code, 400)
chk("a login can be saved", a.post("/api/ssh-login", json={"user": "ubnt", "password": "hunter2", "port": 2222}).status_code, 200)
chk("it is reported without the password", (mon()["ssh"], "hunter2" in json.dumps(mon())), ({"user": "ubnt", "port": 2222, "password": True, "key_file": False, "env": []}, False))
chk("the file is private", (stat.S_IMODE(os.stat(m.SSHF).st_mode), stat.S_IMODE(os.stat(m.SSHF + ".bak").st_mode)), (0o600, 0o600))
a.post("/api/ssh-login", json={"user": "ubnt", "password": "", "port": 22})
chk("a blank password keeps the saved one", (m.ssh_login().get("password"), m.ssh_login().get("port")), ("hunter2", 22))
os.environ["SSH_USER"] = "fromenv"
chk("environment variables win", m.ssh_login()["user"], "fromenv")
chk("and the page is told which ones", mon()["ssh"]["env"], ["user"])
del os.environ["SSH_USER"]
a.post("/api/ssh-login", json={"clear": True})
chk("it can be removed", (os.path.exists(m.SSHF), m.ssh_login().get("user")), (False, None))

print("== when the controller can't be used ==")
mode["key"] = "other"; cycle()
chk("a refused key is said plainly", (st()["ok"], st()["error"]), (False, "The controller refused the API key."))
chk("the last good numbers about the site are kept", (st()["devices"], st()["version"]), (3, "10.0.1"))
chk("the devices it knew show the problem, with the site's name", last("Office AP")["error"], "UniFi API (Environment): The controller refused the API key.")
chk("the API key is not in any message", "good-key" in json.dumps(st()), False)
chk("devices it never knew carry on over SSH", cycle(), ["NAS"])
mode["key"] = "good-key"; m.UF["conn"].clear(); os.environ["UNIFI_SITE"] = "Nowhere"; cycle()
chk("a wrong site lists the real ones", "default" in st()["error"], True)
del os.environ["UNIFI_SITE"]; os.environ["UNIFI_URL"] = url(S1) + "/nothing"; m.UF["conn"].clear(); cycle()
chk("a wrong address is an error, not a crash", st()["ok"], False)
os.environ["UNIFI_URL"] = url(S1); m.UF["conn"].clear(); cycle()
import socket as _s
z = _s.socket(); z.bind(("127.0.0.1", 0)); closed = z.getsockname()[1]; z.close()
def tried(u):
    try: m.unifi_get({"key": "k", "verify": False}, u, "/v1/info")
    except m.UnifiError as e: return str(e)
chk("a closed port says where it looked and what to check", tried(f"http://127.0.0.1:{closed}/x"), f"Could not reach the controller at 127.0.0.1:{closed}: connection refused. Nothing is listening on port {closed} there. Check the port in the address (a self-hosted Network application is usually on 8443); inside Docker, localhost is Beacon's own container, so use the controller's address on your network.")
chk("another host gets the port hint without the Docker one", "Docker" in tried(f"http://127.0.0.2:{closed}/x"), False)
chk("https against a plain-http controller suggests http", tried(f"https://127.0.0.1:{S1.server_port}/x"), f"The controller at 127.0.0.1:{S1.server_port} answered in plain HTTP, but the address starts with https. Try http://127.0.0.1:{S1.server_port}.")
r = a.post("/api/unifi/test", json={"url": f"127.0.0.1:{closed}", "key": "k"}).get_json()
chk("the Test button shows it, with the default https port when none is given", (r["ok"], "127.0.0.1:" + str(closed) in r["error"]), (False, True))

print("== sites set up in Settings ==")
os.environ.pop("UNIFI_API_KEY"); os.environ.pop("UNIFI_URL")
chk("without the env vars and with no saved site, Beacon is back to SSH for everything", (m.unifi_sites(), a.get("/api/data").get_json()["has_poe"], a.get("/api/unifi").get_json()["sites"]), ([], True, []))
put = lambda sites, **kw: a.put("/api/unifi", json={"sites": sites, **kw})
one = {"name": "Home", "url": url(S1), "key": "good-key", "site": "default", "verify": False}
two = {"name": "Cabin", "url": url(S2), "key": "key-two", "site": "Default", "verify": False}
chk("a viewer cannot read or change them", (v.get("/api/unifi").status_code, v.put("/api/unifi", json={"sites": []}).status_code), (403, 403))
chk("a site needs a name", put([{**one, "name": " "}]).status_code, 400)
chk("names must differ", put([one, {**two, "name": "home"}]).status_code, 400)
chk("an address must look like one", put([{**one, "url": "ftp://x"}]).status_code, 400)
chk("a new site needs a key", put([{**one, "key": ""}]).status_code, 400)
r = put([one, two]); chk("two sites can be saved", r.status_code, 200)
chk("by default the device check is on, daily at 1am", {k: m.unifi_saved()["discover"][k] for k in ("enabled", "mode", "at")}, {"enabled": True, "mode": "daily", "at": "01:00"})
chk("the file is private", stat.S_IMODE(os.stat(m.UNIF).st_mode), 0o600)
ids = [x["id"] for x in a.get("/api/unifi").get_json()["sites"]]
chk("they are listed without their keys", ("good-key" in json.dumps(a.get("/api/unifi").get_json()), [x["name"] for x in a.get("/api/unifi").get_json()["sites"]]), (False, ["Home", "Cabin"]))
chk("both start out waiting", [x["status"] for x in a.get("/api/unifi").get_json()["sites"]], ["waiting", "waiting"])
chk("the first site that knows a device wins; the second only gets what's left", cycle(), ["NAS"])
info = {x["name"]: x for x in a.get("/api/unifi").get_json()["sites"]}
chk("Home: connected, UniFi OS console, 3 devices", (info["Home"]["status"], info["Home"]["kind"], info["Home"]["version"], info["Home"]["devices"], info["Home"]["matched"]), ("connected", "UniFi OS console", "10.0.1", 3, 3))
chk("Cabin: connected, standalone path found, nothing of the list matched", (info["Cabin"]["status"], info["Cabin"]["kind"], info["Cabin"]["version"], info["Cabin"]["devices"], info["Cabin"]["matched"]), ("connected", "Network application", "9.4.2", 2, 0))
chk("both report when they last answered", all(isinstance(x["last_ok"], float) for x in info.values()), True)
chk("a blank key keeps the saved one", (put([{**one, "id": ids[0], "key": ""}, {**two, "id": ids[1], "key": ""}]).status_code, [x["id"] for x in a.get("/api/unifi").get_json()["sites"]]), (200, ids))
chk("and the sites still connect", (cycle(), st(ids[0])["ok"]), (["NAS"], True))
t = a.post("/api/unifi/test", json={"id": ids[1], "url": url(S2), "key": "", "site": "default"}).get_json()
chk("a site can be tried without saving, using the saved key", (t["ok"], t["version"], t["devices"], t["kind"]), (True, "9.4.2", 2, "Network application"))
chk("a wrong key is reported by the test", a.post("/api/unifi/test", json={"url": url(S2), "key": "nope", "site": "default"}).get_json(), {"ok": False, "error": "The controller refused the API key."})
chk("the test changes nothing", [x["name"] for x in a.get("/api/unifi").get_json()["sites"]], ["Home", "Cabin"])
S2.cfg["key"] = "changed"; cycle()
chk("one site failing leaves the other working", (st(ids[0])["ok"], st(ids[1])["ok"], last("Office AP")["cpu"]), (True, False, 4.8))
S2.cfg["key"] = "key-two"
ids = [x["id"] for x in a.get("/api/unifi").get_json()["sites"]]; a.put("/api/unifi", json={"sites": [{**one, "id": ids[0], "key": ""}]})
chk("a removed site is forgotten", (list(m.UF["state"]), [x["name"] for x in a.get("/api/unifi").get_json()["sites"]]), ([ids[0]], ["Home"]))

print("== finding new devices ==")
S1.cfg["devs"] += [dev("n1", "Lounge AP", "10.0.0.7", ["accessPoint"]), dev("n2", "Switch", "10.0.0.8", ["switching"]), dev("n3", "No address", "", []), dev("n4", "Bad", "not an ip!", [])]
a.put("/api/unifi", json={"sites": [{**one, "id": ids[0], "key": ""}, two]})
chk("a check across both sites finds what is new, names clashes by address, skips the unusable", (lambda r: (sorted(r.get_json()["added"]), r.get_json()["errors"]))(a.post("/api/unifi/discover")), (["Lounge AP", "Shed AP", "Switch (10.0.0.8)"], {}))
chk("they are in the device list", {d["name"]: d["host"] for d in m.devices()}["Lounge AP"], "10.0.0.7")
chk("a second check finds nothing new", a.post("/api/unifi/discover").get_json()["added"], [])
a.delete("/api/devices/Lounge AP")
chk("a device removed by hand is not brought back", (a.post("/api/unifi/discover").get_json()["added"], a.get("/api/unifi").get_json()["ignored"]), ([], 1))
a.post("/api/devices", json={"name": "Lounge AP", "host": "10.0.0.7"})
chk("adding it again lifts that", a.get("/api/unifi").get_json()["ignored"], 0)
a.delete("/api/devices/Lounge AP"); a.post("/api/unifi/forget")
chk("forgetting the removed devices brings them back on the next check", (a.get("/api/unifi").get_json()["ignored"], a.post("/api/unifi/discover").get_json()["added"]), (0, ["Lounge AP"]))
S1.cfg["devs"].append(dev("n5", "Late AP", "10.0.0.9", ["accessPoint"]))
chk("an address already in the list under another name is left alone", a.post("/api/unifi/discover").get_json()["added"], [])
S2.cfg["key"] = "nope"
chk("a site that can't be read is reported, the others still checked", a.post("/api/unifi/discover").get_json()["errors"], {"Cabin": "The controller refused the API key."})
S2.cfg["key"] = "key-two"
m.dlock.acquire(); chk("a check already running is not started twice", a.post("/api/unifi/discover").status_code, 409); m.dlock.release()
chk("the last check is remembered", (a.get("/api/unifi").get_json()["discover"]["last_added"], isinstance(a.get("/api/unifi").get_json()["discover"]["last_run"], float)), ([], True))

print("== the schedule ==")
T = lambda *x: time.mktime((2026, 11, *x, 0, 0, -1)[:6] + (0, 0, -1))
dd = lambda **k: {"mode": "daily", "at": "01:00", "hours": 24, "last_run": None, **k}
chk("no clock, nothing due", m.next_due(dd(), T(25, 12)), None)
chk("daily: after today's run, the next is tomorrow at 1", m.next_due(dd(last_run=T(25, 1, 2)), T(25, 12)), T(26, 1))
chk("daily: before 1am, the next is today's 1am", m.next_due(dd(last_run=T(24, 1, 2)), T(25, 0, 30)), T(25, 1))
chk("daily: a missed run is due now", m.next_due(dd(last_run=T(23, 1, 2)), T(25, 12)) <= T(25, 12), True)
chk("every N hours counts from the last check", m.next_due(dd(mode="hours", hours=6, last_run=T(25, 8)), T(25, 9)), T(25, 14))
now = time.time(); chk("a different time of day works", m.next_due(dd(at="23:30", last_run=now), now) > now, True)
chk("a bad schedule is refused", (put([one], discover={"at": "25:00"}).status_code, put([one], discover={"hours": 0}).status_code, put([one], discover={"hours": "x"}).status_code), (400, 400, 400))
put([one], discover={"enabled": True, "mode": "hours", "hours": 6, "at": "03:15"})
dsc = a.get("/api/unifi").get_json()["discover"]
chk("a schedule can be saved", (dsc["enabled"], dsc["mode"], dsc["hours"], dsc["at"]), (True, "hours", 6, "03:15"))
chk("changing it restarts its clock", dsc["last_run"], None)
chk("with no clock the next time is not shown", dsc["next"], None)
S1.cfg["devs"].append(dev("n6", "Night AP", "10.0.0.10", ["accessPoint"]))
t0 = time.time(); m.dtick(t0)
chk("the first look only starts the clock", ([d["name"] for d in m.devices() if d["name"] == "Night AP"], abs(a.get("/api/unifi").get_json()["discover"]["last_run"] - t0) < 1), ([], True))
chk("not due yet", ([d["name"] for d in (m.dtick(t0 + 3600), m.devices())[1] if d["name"] == "Night AP"]), [])
m.dtick(t0 + 6 * 3600 + 5)
chk("when due, it adds the new devices", [d["name"] for d in m.devices() if d["name"] == "Night AP"], ["Night AP"])
chk("and the next time is six hours on", abs(a.get("/api/unifi").get_json()["discover"]["next"] - (a.get("/api/unifi").get_json()["discover"]["last_run"] + 6 * 3600)) < 1, True)
put([one], discover={"enabled": False}); S1.cfg["devs"].append(dev("n7", "Off AP", "10.0.0.11", ["accessPoint"])); m.dtick(time.time() + 99 * 3600)
chk("switched off, nothing is added on a schedule", [d["name"] for d in m.devices() if d["name"] == "Off AP"], [])
chk("but a manual check still works", a.post("/api/unifi/discover").get_json()["added"], ["Off AP"])

print("== the old way still works alongside ==")
os.environ.update(UNIFI_URL=url(S2), UNIFI_API_KEY="key-two")
chk("env vars are one more site, listed as such, last", [(x["name"], x["env"]) for x in a.get("/api/unifi").get_json()["sites"]], [("Home", False), ("Environment", True)])
chk("a saved site can't take the environment's name", put([{**one, "name": "environment"}]).status_code, 400)
print(f"{bad[0] - bad[1]}/{bad[0]} passed"); sys.exit(1 if bad[1] else 0)
