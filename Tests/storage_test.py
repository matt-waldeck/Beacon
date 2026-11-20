"""Server tests for history, retention, storage and purge. Run: python3 tests/storage_test.py  (needs Flask; no real devices or network)"""
import contextlib, importlib.util, io, json, os, sqlite3, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
os.environ.update(DATA_DIR=tempfile.mkdtemp(), UI_USER="boss", UI_PASSWORD="secret123"); data_dir = os.environ["DATA_DIR"]; now = time.time()
sys.path.append(str(HERE / "stub"))
# an old-style database (before the numbers had their own columns) to be migrated
db = sqlite3.connect(os.path.join(data_dir, "stats.db")); db.execute("CREATE TABLE samples (dev TEXT, ts REAL, data TEXT)")
db.execute("INSERT INTO samples VALUES ('old', ?, ?)", (now - 100, json.dumps({"ts": now - 100, "cpu": 12.5, "mem": 40.0, "load": 0.3, "rx": 1000, "tx": 500, "clients": 7, "poe": 12.5})))
db.execute("INSERT INTO samples VALUES ('old', ?, ?)", (now - 50, json.dumps({"ts": now - 50, "error": "unreachable"}))); db.commit(); db.close()
with contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location("app", HERE.parent / "app.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = [0, 0]
def chk(label, got, want):
    bad[0] += 1
    if got != want: bad[1] += 1; print(f"  FAIL {label}: got {got!r}, wanted {want!r}")
S = lambda r: r.status_code
a = m.app.test_client(); a.post("/api/login", json={"name": "boss", "password": "secret123"}); a.post("/api/users", json={"name": "v", "password": "password1", "role": "viewer"})
v = m.app.test_client(); v.post("/api/login", json={"name": "v", "password": "password1"}); g = m.app.test_client(); a.post("/api/settings", json={"guest": True}); g.post("/api/login", json={"guest": True})
print("== migration ==")
cols = {r[1] for r in m.sql("PRAGMA table_info(samples)")}; chk("new columns added", all("m_" + k in cols for k in m.METRICS), True)
row = m.sql("SELECT m_cpu, m_mem, m_load, m_rx, m_tx, m_clients, m_poe FROM samples WHERE ts=?", (now - 100,))[0]; chk("old rows copied into the columns", row, (12.5, 40.0, 0.3, 1000.0, 500.0, 7.0, 12.5))
chk("error rows stay empty", m.sql("SELECT m_cpu FROM samples WHERE ts=?", (now - 50,))[0][0], None); chk("marked done", m.sql("PRAGMA user_version")[0][0], 1)
m.save_sample("new", {"ts": now, "cpu": 5, "mem": 6, "rx": 7}); chk("new samples fill the columns", m.sql("SELECT m_cpu, m_mem, m_rx, m_load FROM samples WHERE dev='new'")[0], (5.0, 6.0, 7.0, None))
m.migrate(); chk("running it again changes nothing", m.sql("SELECT COUNT(*) FROM samples")[0][0], 3); m.sql("DELETE FROM samples")
print("== retention setting ==")
chk("default is 7 days", a.get("/api/data").get_json()["retention_days"], 7)
chk("admin sets 30", (S(a.post("/api/settings", json={"retention_days": 30})), a.get("/api/data").get_json()["retention_days"]), (200, 30))
chk("clamped to 1..365", (a.post("/api/settings", json={"retention_days": 0}).get_json()["retention_days"], a.post("/api/settings", json={"retention_days": 9999}).get_json()["retention_days"]), (1, 365))
chk("text refused", S(a.post("/api/settings", json={"retention_days": "lots"})), 400); chk("viewers can't change it", S(v.post("/api/settings", json={"retention_days": 5})), 403); a.post("/api/settings", json={"retention_days": 30})
print("== long ranges ==")
db = sqlite3.connect(m.DB); ten = 10 * 86400
for dev, cpu, rx in (("A", 10.0, 1000), ("B", 30.0, 2000)):
    db.executemany("INSERT INTO samples (dev, ts, data, m_cpu, m_rx) VALUES (?,?,?,?,?)", [(dev, now - i * 120, json.dumps({"ts": now - i * 120, "cpu": cpu, "rx": rx, "model": "M", "clients": 3}), cpu, rx) for i in range(ten // 120)])
db.executemany("INSERT INTO pings VALUES ('P', ?, ?)", [(now - i * 60, None if i % 10 == 0 else 5.0 + (i % 3)) for i in range(ten // 60)]); db.commit(); db.close()
for n, h in (("A", "10.0.0.1"), ("B", "10.0.0.2")): a.post("/api/devices", json={"name": n, "host": h})
a.post("/api/servers", json={"name": "P", "host": "10.0.0.9"})
d = {x["name"]: x["series"] for x in a.get("/api/data?minutes=14400").get_json()["devices"]}
chk("10 days: about 300 steps (+ newest sample)", 280 <= len(d["A"]) <= 302, True)
chk("each step is the true average", {round(x["cpu"], 1) for x in d["A"][:-1]} | {round(x["cpu"], 1) for x in d["B"][:-1]}, {10.0, 30.0})
chk("steps line up across devices", [x["ts"] for x in d["A"][:-1]] == [x["ts"] for x in d["B"][:-1]], True)
chk("the last entry is a real stored sample, not an average", m.sql("SELECT COUNT(*) FROM samples WHERE dev='A' AND ts=?", (d["A"][-1]["ts"],))[0][0], 1)
raw = {x["name"]: x["series"] for x in a.get("/api/data?minutes=1440").get_json()["devices"]}["A"]; chk("24 hours stays raw", (len(raw) <= 600, sum("model" in x for x in raw) > 300), (True, True))
a.post("/api/settings", json={"retention_days": 7}); far = {x["name"]: x["series"] for x in a.get("/api/data?minutes=43200").get_json()["devices"]}["A"]
chk("asking for more than the retention covers is limited to it", far[0]["ts"] > now - 8 * 86400, True); a.post("/api/settings", json={"retention_days": 30})
p = {x["name"]: x["series"] for x in a.get("/api/servers?minutes=14400").get_json()["servers"]}["P"]; mid = p[:-1]
chk("pings: counts, worst and drops per step", (sum(x["n"] for x in mid) > 9500, all(x["max"] is None or x["max"] >= x["ms"] for x in mid), sum(x["d"] for x in mid) > 900), (True, True, True))
chk("pings: newest real result at the end", abs(p[-1]["ts"] - now) < 65, True)
print("== pruning ==")
a.post("/api/settings", json={"retention_days": 1}); m.prune()
chk("older than the retention is deleted, newer kept", (m.sql("SELECT COUNT(*) FROM samples WHERE ts < ?", (now - 86400 - 5,))[0][0], m.sql("SELECT COUNT(*) FROM samples")[0][0] > 1000), (0, True))
chk("pings pruned too", m.sql("SELECT COUNT(*) FROM pings WHERE ts < ?", (now - 86400 - 5,))[0][0], 0)
print("== storage and purge ==")
chk("admins only", (S(a.get("/api/storage")), S(v.get("/api/storage")), S(g.get("/api/storage")), S(m.app.test_client().get("/api/storage"))), (200, 403, 403, 401))
os.makedirs(m.ICON_DIR, exist_ok=True)
for f, body in (("x.png", b"\x89PNG" + b"0" * 100), ("x.src", b"{}"), ("y.none", b"{}")): open(os.path.join(m.ICON_DIR, f), "wb").write(body)
st = a.get("/api/storage").get_json()
chk("sizes and counts", (st["db"]["bytes"] > 10000, st["db"]["samples"] > 1000, st["db"]["pings"] > 100, st["icons"]["count"], st["icons"]["bytes"] > 100, st["data"]["bytes"] > st["db"]["bytes"], st["disk"]["free"] > 0, st["retention_days"], st["db"]["since"] is not None), (True, True, True, 1, True, True, True, 1, True))
chk("viewers can't purge", (S(v.post("/api/storage/purge", json={"what": "history"})), S(m.app.test_client().post("/api/storage/purge", json={"what": "icons"}))), (403, 401)); chk("unknown action refused", S(a.post("/api/storage/purge", json={"what": "everything"})), 400)
r = a.post("/api/storage/purge", json={"what": "icons"}).get_json(); chk("icon cache cleared", (r["removed"], os.listdir(m.ICON_DIR)), (1, []))
before = os.path.getsize(m.DB); r = a.post("/api/storage/purge", json={"what": "history"}).get_json()
chk("history deleted, devices and servers kept", (r["removed"] > 1000, m.sql("SELECT COUNT(*) FROM samples")[0][0], m.sql("SELECT COUNT(*) FROM pings")[0][0], len(a.get("/api/data").get_json()["devices"]), len(a.get("/api/servers").get_json()["servers"])), (True, 0, 0, 2, 1))
chk("file shrank after the purge", os.path.getsize(m.DB) < before, True); chk("compact works", S(a.post("/api/storage/purge", json={"what": "compact"})), 200)
chk("polling still works after a purge", (m.save_sample("A", {"ts": time.time(), "cpu": 1}), len(a.get("/api/data").get_json()["devices"][0]["series"]))[1], 1)
print("== icon source ==")
chk("selfh.st is the default", (a.get("/api/me").get_json()["icons"], m.icon_candidates("Home Assistant")[0]), ("selfhst", ("selfh.st", "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/home-assistant.svg")))
os.makedirs(m.ICON_DIR, exist_ok=True); open(os.path.join(m.ICON_DIR, "x.png"), "wb").write(b"\x89PNG" + b"0" * 50)
chk("choosing the current source keeps the cache", (S(a.post("/api/settings", json={"icon_source": "selfhst"})), os.listdir(m.ICON_DIR)), (200, ["x.png"]))
chk("switching to Dashboard Icons works and clears the cache", (S(a.post("/api/settings", json={"icon_source": "dashboard"})), os.listdir(m.ICON_DIR), a.get("/api/me").get_json()["icons"]), (200, [], "dashboard"))
chk("lookups follow the choice", {src for src, _ in m.icon_candidates("Home Assistant")} | {u.split("/gh/")[1].split("/")[0] for _, u in m.icon_candidates("Home Assistant")}, {"Dashboard Icons", "homarr-labs"})
chk("unknown source refused", S(a.post("/api/settings", json={"icon_source": "nope"})), 400); chk("viewers can't change it", S(v.post("/api/settings", json={"icon_source": "selfhst"})), 403)
a.post("/api/settings", json={"icon_source": "selfhst"}); chk("and back", a.get("/api/me").get_json()["icons"], "selfhst")
print(f"  storage suite: {bad[0] - bad[1]}/{bad[0]} checks passed"); sys.exit(1 if bad[1] else 0)
