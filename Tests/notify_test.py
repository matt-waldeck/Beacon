"""Server tests for notifications: the settings, the test button, when a message goes out, and what it says. Run: python3 Tests/notify_test.py  (needs Flask; no devices or network).
Starts a tiny fake webhook receiver and a fake mail server on localhost and runs Beacon's own code against them, with the paramiko stand-in."""
import contextlib, email, email.policy, importlib.util, io, json, os, socket, stat, sys, tempfile, threading, time
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
def body(raw): return email.message_from_string(raw, policy=email.policy.default).get_content()  # the mail as it reads, not as it travels
def until(f, t=5):
    end = time.time() + t
    while time.time() < end:
        if f(): return True
        time.sleep(0.05)
    return bool(f())

HOOKS = []  # what the fake webhook receiver was sent: (path, headers, body)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        HOOKS.append((self.path, dict(self.headers), self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())); self.send_response(200); self.end_headers()
web = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=web.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{web.server_port}/hook/secret-token"

MAIL = []  # (MAIL FROM, [RCPT TO], message text)
mail = socket.socket(); mail.bind(("127.0.0.1", 0)); mail.listen(5)
def serve(c):
    f = c.makefile("rwb"); w = lambda t: (f.write(t.encode() + b"\r\n"), f.flush())
    w("220 fake ready"); rcpt, frm = [], ""
    while True:
        line = f.readline()
        if not line: break
        t = line.decode().strip(); u = t.upper()
        if u.startswith("MAIL FROM"): frm = t; w("250 ok")
        elif u.startswith("RCPT TO"): rcpt.append(t); w("250 ok")
        elif u == "DATA":
            w("354 go"); lines = []
            while (l := f.readline().decode().rstrip("\r\n")) != ".": lines.append(l)
            MAIL.append((frm, rcpt[:], "\n".join(lines))); rcpt.clear(); w("250 queued")
        elif u == "QUIT": w("221 bye"); break
        else: w("250 fake")
    c.close()
def accept():
    while True: threading.Thread(target=serve, args=(mail.accept()[0],), daemon=True).start()
threading.Thread(target=accept, daemon=True).start()
MPORT = mail.getsockname()[1]

a = m.app.test_client(); a.post("/api/login", json={"name": "boss", "password": "secret123"})
a.post("/api/users", json={"name": "v", "password": "password1", "role": "viewer"}); v = m.app.test_client(); v.post("/api/login", json={"name": "v", "password": "password1"})
put = lambda **k: a.put("/api/notify", json=k)
get = lambda: a.get("/api/notify").get_json()
WH = {"type": "webhook", "name": "Hook", "enabled": True, "url": HOOK, "format": "generic", "verify": False}
EM = {"type": "email", "name": "Mail", "enabled": True, "host": "127.0.0.1", "port": MPORT, "security": "none", "sender": "beacon@home.lan", "to": "me@home.lan, you@home.lan"}
def dests(): return {d["name"]: d for d in get()["destinations"]}
def keep(*names, **over):  # the saved destinations as the page sends them back, with changes
    return [{**{k: x for k, x in d.items() if k not in ("password", "set", "hint")}, **over.get(d["name"], {})} for d in get()["destinations"] if d["name"] in names]  # the page never has the secrets to send back

print("== the settings ==")
d = get()
chk("nothing is set up to begin with", (d["destinations"], d["events"], d["after"], d["batch"]), ([], {"device": True, "server": True, "unifi": True}, 3, 0))
chk("a viewer can neither read nor change them", (v.get("/api/notify").status_code, v.put("/api/notify", json={}).status_code, v.post("/api/notify/test", json={"type": "webhook"}).status_code), (403, 403, 403))
bad_rows = {"a webhook needs an address": [{**WH, "url": ""}], "which has to be http or https": [{**WH, "url": "ftp://x/y"}], "mail needs a server, a From and a To": [{**EM, "host": "", "sender": ""}],
            "the port is checked": [{**EM, "port": 0}], "so are the addresses": [{**EM, "to": "nobody"}], "and the From": [{**EM, "sender": "a b@c"}], "a login needs its password": [{**EM, "user": "bob"}],
            "every destination needs a name": [{**WH, "name": " "}], "and its own": [WH, {**EM, "name": "hook"}], "an unknown kind is refused": [{**WH, "type": "sms"}]}
for label, rows in bad_rows.items(): chk(label, put(destinations=rows).status_code, 400)
chk("there is a limit", put(destinations=[{**WH, "name": f"H{i}"} for i in range(21)]).status_code, 400)
chk("the error names the destination", "Hook:" in put(destinations=[{**WH, "url": ""}]).get_json()["error"], True)
chk("the number of checks and the batch size are limited", (put(after=0).status_code, put(after=21).status_code, put(batch=1).status_code, put(batch=51).status_code, put(batch="x").status_code), (400,) * 5)
chk("nothing was saved by the failures", os.path.exists(os.path.join(DATA, "notify.json")), False)
with open(os.path.join(DATA, "notify.json"), "w") as f: json.dump({"after": 4, "webhook": {"enabled": True, "url": HOOK, "format": "slack"}, "email": {**{k: v_ for k, v_ in EM.items() if k not in ("type", "name")}}}, f)
old = m.notify_saved()
chk("the first version's single webhook and email become destinations", ([(x["id"], x["type"], x["name"], x["enabled"]) for x in old["destinations"]], old["after"], old["destinations"][0]["format"]), ([("webhook", "webhook", "Webhook", True), ("email", "email", "Email", True)], 4, "slack"))
os.remove(os.path.join(DATA, "notify.json"))
r = put(destinations=[WH, EM], after=2); j = r.get_json()
chk("saving works, and each destination gets an id", (r.status_code, [(x["name"], x["type"], len(x["id"])) for x in j["destinations"]]), (200, [("Hook", "webhook", 8), ("Mail", "email", 8)]))
chk("the webhook address and mail password never come back", ("secret-token" in json.dumps(j), j["destinations"][0]["set"], j["destinations"][0]["hint"], j["destinations"][1]["password"]), (False, True, "127.0.0.1", False))
chk("the file is private", stat.S_IMODE(os.stat(os.path.join(DATA, "notify.json")).st_mode), 0o600)
ids = {n: x["id"] for n, x in dests().items()}
chk("a row sent back with its id is the same destination, and a blank address keeps the saved one", (put(destinations=keep("Hook", "Mail", Hook={"url": "", "format": "discord"})).status_code, dests()["Hook"]["id"] == ids["Hook"], dests()["Hook"]["format"], m.notify_saved()["destinations"][0]["url"]), (200, True, "discord", HOOK))
put(destinations=keep("Hook", "Mail", Hook={"format": "generic"}))
chk("a blank password keeps the saved one", (put(destinations=keep("Hook", "Mail", Mail={"user": "bob", "password": "pw1"})).status_code, put(destinations=keep("Hook", "Mail")).status_code, m.notify_saved()["destinations"][1]["password"]), (200, 200, "pw1"))
put(destinations=keep("Hook", "Mail", Mail={"user": ""})); chk("removing the user name removes the password", (m.notify_saved()["destinations"][1]["user"], m.notify_saved()["destinations"][1]["password"]), ("", ""))
r = put(destinations=keep("Hook", "Mail") + [{**WH, "name": "Second", "url": HOOK.replace("secret-token", "second")}])
chk("a row without an id is a new destination; the others are untouched", ([x["name"] for x in r.get_json()["destinations"]], {n: x["id"] for n, x in dests().items() if n in ids} == ids, dests()["Second"]["id"] not in ids.values()), (["Hook", "Mail", "Second"], True, True))
chk("changing a destination's kind starts it afresh, so the old address is gone", (put(destinations=keep("Hook", "Mail", "Second", Hook={"type": "email", "enabled": False})).status_code, put(destinations=keep("Hook", "Mail", "Second", Hook={"type": "webhook", "url": "", "enabled": True})).status_code), (200, 400))
put(destinations=[WH] + keep("Mail", "Second", Second={"enabled": False})); ids = {n: x["id"] for n, x in dests().items()}
chk("leaving something out leaves it as it is", (put(after=2).status_code, [x["name"] for x in get()["destinations"]], get()["batch"]), (200, ["Hook", "Mail", "Second"], 0))

print("== the test button ==")
n = len(HOOKS); T = lambda **k: a.post("/api/notify/test", json=k).get_json()
r = T(type="webhook", id=ids["Hook"], format="generic")
chk("a test goes to the saved address, in the format on the page", (r, HOOKS[-1][0], json.loads(HOOKS[-1][2])["event"], json.loads(HOOKS[-1][2])["source"]), ({"ok": True}, "/hook/secret-token", "test", "beacon"))
T(type="webhook", id=ids["Hook"], format="discord"); chk("discord gets a content field", sorted(json.loads(HOOKS[-1][2])), ["content"])
T(type="webhook", id=ids["Hook"], format="slack"); chk("slack gets a text field", sorted(json.loads(HOOKS[-1][2])), ["text"])
T(type="webhook", id=ids["Hook"], format="ntfy"); h = HOOKS[-1]
chk("ntfy gets plain text with a title", (h[2].startswith("If you can read this"), h[1].get("Title"), h[1].get("Content-Type", "").startswith("text/plain")), (True, "Beacon: test message", True))
chk("a new, unsaved one can be tried", (T(**{**WH, "url": HOOK.replace("secret-token", "unsaved")}), HOOKS[-1][0]), ({"ok": True}, "/hook/unsaved"))
chk("the webhook was tried five times", len(HOOKS) - n, 5)
r = T(**{**EM, "id": ids["Mail"]})
chk("a test email goes out", (r, [x.upper() for x in MAIL[-1][1]], "Subject: Beacon: test message" in MAIL[-1][2]), ({"ok": True}, ["RCPT TO:<ME@HOME.LAN>", "RCPT TO:<YOU@HOME.LAN>"], True))
r = T(type="webhook", url="http://127.0.0.1:1/another-secret")
chk("a refused connection is explained without the address", (r["ok"], r["error"].startswith("Couldn't connect"), "another-secret" in r["error"]), (False, True, False))
chk("a test with nothing filled in says what is missing", (T(type="email")["ok"], a.post("/api/notify/test", json={"type": "sms"}).status_code), (False, 400))
chk("the test saves nothing", (dests()["Hook"]["format"], len(dests())), ("generic", 3))

print("== when messages go out ==")
put(destinations=keep("Hook", "Mail", "Second", Second={"enabled": True}))
n, k = len(HOOKS), len(MAIL)
m.watch("device", "Switch", False, "SSH: timed out"); time.sleep(.3)
chk("one failed check says nothing yet", (len(HOOKS) - n, len(MAIL) - k), (0, 0))
m.watch("device", "Switch", False, "SSH: timed out")
chk("a second in a row (the setting) sends to every destination that is on", (until(lambda: len(HOOKS) - n == 2 and len(MAIL) - k == 1), sorted(x[0] for x in HOOKS[n:]), len(MAIL) - k), (True, ["/hook/second", "/hook/secret-token"], 1))
p = json.loads(HOOKS[-1][2])
chk("the webhook says what and why", (p["event"], p["state"], p["kind"], p["name"], p["reason"], p["title"], p["since"] <= time.time()), ("device_down", "down", "device", "Switch", "SSH: timed out", "Beacon: Switch is offline", True))
chk("so does the email", ("Subject: Beacon: Switch is offline" in MAIL[-1][2], "failed 2 checks in a row" in body(MAIL[-1][2]), "SSH: timed out" in body(MAIL[-1][2])), (True, True, True))
m.watch("device", "Switch", False, "again"); m.watch("device", "Switch", False, "again"); time.sleep(.3)
chk("it is only said once while it stays down", len(HOOKS) - n, 2)
m.watch("device", "Switch", True)
chk("and again when it answers", (until(lambda: len(HOOKS) - n == 4), json.loads(HOOKS[-1][2])["event"], "back online" in HOOKS[-1][2]), (True, "device_up", True))
m.watch("device", "Switch", True); time.sleep(.3)
chk("a healthy check says nothing", len(HOOKS) - n, 4)
m.watch("device", "Flaky", False); m.watch("device", "Flaky", True); m.watch("device", "Flaky", False); time.sleep(.3)
chk("a failure that doesn't repeat is never reported", len(HOOKS) - n, 4)
put(destinations=keep("Hook", "Mail", "Second", Second={"enabled": False}))
n = len(HOOKS)
for _ in range(2): m.watch("unifi", "Home", False, "Connection refused")
chk("a controller outage reads differently", (until(lambda: len(HOOKS) > n), json.loads(HOOKS[-1][2])["title"]), (True, "Beacon: Home is unreachable"))
n = len(HOOKS); m.watch("unifi", "Home", True)
chk("and so does the recovery", (until(lambda: len(HOOKS) > n), json.loads(HOOKS[-1][2])["title"]), (True, "Beacon: Home is reachable again"))
m.save_items("servers", [{"name": "NAS", "host": "10.0.0.5", "type": "vm"}]); n = len(HOOKS)
for _ in range(2): m.watch("server", "NAS", False, "It doesn't answer ping.")
chk("a server or VM says which", (until(lambda: len(HOOKS) > n), json.loads(HOOKS[-1][2])["host"], HOOKS[-1][2].count("VM NAS (10.0.0.5)")), (True, "10.0.0.5", 1))
n = len(HOOKS); m.watch("server", "NAS", True); until(lambda: len(HOOKS) > n)

print("== choosing what is sent ==")
put(events={"server": False}); n = len(HOOKS)
for _ in range(2): m.watch("server", "NAS", False)
m.watch("device", "Router", False); m.watch("device", "Router", False)
chk("a switched-off kind is skipped, the others still go", (until(lambda: len(HOOKS) > n), time.sleep(.4), len(HOOKS) - n, json.loads(HOOKS[-1][2])["name"]), (True, None, 1, "Router"))
put(events={"server": True}); n = len(HOOKS); m.watch("server", "NAS", True); time.sleep(.4)
chk("an outage that was muted is not reported as recovered either", len(HOOKS) - n, 0)
put(destinations=keep("Hook", "Mail", "Second", Hook={"enabled": False})); n, k = len(HOOKS), len(MAIL); m.watch("device", "Router", True)
chk("a destination that is switched off gets nothing", (until(lambda: len(MAIL) > k), time.sleep(.3), len(HOOKS) - n, len(MAIL) - k), (True, None, 0, 1))
put(destinations=keep("Hook", "Mail", "Second", Mail={"enabled": False})); n, k = len(HOOKS), len(MAIL); m.watch("device", "Router", False); m.watch("device", "Router", False); time.sleep(.4)
chk("with every one off nothing is sent", (len(HOOKS) - n, len(MAIL) - k), (0, 0))
put(destinations=keep("Hook", "Mail", "Second", Hook={"enabled": True}, Mail={"enabled": True})); m.watch("device", "Router", True); time.sleep(.4)

print("== combining messages ==")
m.BATCH_WAIT = .6
chk("the setting is saved and shown", (put(batch=3).status_code, get()["batch"], get()["batch_wait"]), (200, 3, .6))
n, k = len(HOOKS), len(MAIL); t0 = time.time()
for x in "AB":
    for _ in range(2): m.watch("device", "Dev" + x, False, "down")
chk("fewer than that still go out one by one, after a short wait", (until(lambda: len(HOOKS) - n == 2 and len(MAIL) - k == 2), time.time() - t0 >= .6, [json.loads(h_[2])["event"] for h_ in HOOKS[n:]]), (True, True, ["device_down", "device_down"]))
n, k = len(HOOKS), len(MAIL)
for x in "CDE":
    for _ in range(2): m.watch("device", "Dev" + x, False, "no route")
chk("that many together go out as one message to each destination", (until(lambda: len(HOOKS) - n >= 1 and len(MAIL) - k >= 1), time.sleep(1), len(HOOKS) - n, len(MAIL) - k), (True, None, 1, 1))
p = json.loads(HOOKS[-1][2])
chk("the webhook gets a digest with every item", (p["event"], p["state"], p["title"], [i["name"] for i in p["items"]], p["items"][0]["event"]), ("digest", "down", "Beacon: 3 offline", ["DevC", "DevD", "DevE"], "device_down"))
chk("the email lists them", ("Subject: Beacon: 3 offline" in MAIL[-1][2], "Offline (3):" in body(MAIL[-1][2]), body(MAIL[-1][2]).count("- Device Dev"), "no route" in body(MAIL[-1][2])), (True, True, 3, True))
n = len(HOOKS)
for x in "CDE": m.watch("device", "Dev" + x, True)
for _ in range(2): m.watch("device", "DevF", False, "gone")
until(lambda: len(HOOKS) > n); time.sleep(1); p = json.loads(HOOKS[-1][2])
chk("recoveries and new failures can share one message", (len(HOOKS) - n, p["title"], p["state"], [i["state"] for i in p["items"]]), (1, "Beacon: 1 offline, 3 back online", "down", ["up", "up", "up", "down"]))
put(events={"device": False}); n = len(HOOKS)
for x in "GHIJ":
    for _ in range(2): m.watch("device", "Dev" + x, False)
time.sleep(1.5); chk("messages for a switched-off kind don't count towards it", len(HOOKS) - n, 0)
put(events={"device": True}, batch=0); n = len(HOOKS); t0 = time.time()
for x in "KLM":
    for _ in range(2): m.watch("device", "Dev" + x, False)
chk("with batching off every message goes out by itself, straight away", (until(lambda: len(HOOKS) - n == 3), time.time() - t0 < .6, {json.loads(h_[2])["event"] for h_ in HOOKS[n:]}), (True, True, {"device_down"}))
m.BATCH_WAIT = 15

print("== which checks count ==")
put(batch=0); n = len(HOOKS)
for _ in range(3): m.save_sample("Lost", {"ts": time.time(), "error": "UniFi API (Home): refused"}, notify=False)
time.sleep(.3); chk("devices on a controller that is down are not reported one by one", len(HOOKS) - n, 0)
for _ in range(2): m.save_sample("Lost", {"ts": time.time(), "error": "SSH: no route"})
chk("a normal failed sample counts", (until(lambda: len(HOOKS) > n), json.loads(HOOKS[-1][2])["name"]), (True, "Lost"))
m.save_sample("Lost", {"ts": time.time(), "cpu": 5}); until(lambda: len(HOOKS) > n + 1)
chk("and a good one clears it", json.loads(HOOKS[-1][2])["event"], "device_up")
m.watch("device", "Gone", False); m.notify_prune("device", {"Lost"})
chk("removed devices are forgotten", sorted(k[1] for k in m.NW if k[0] == "device"), ["Lost"])

print("== when sending fails ==")
hid = dests()["Hook"]["id"]; put(destinations=keep("Hook", "Mail", "Second", Hook={"url": "http://127.0.0.1:1/x"}))
for _ in range(2): m.watch("device", "Dead", False)
chk("the reason is kept for the destination that failed, and not for the others", (until(lambda: not m.NLAST.get(hid, {}).get("ok", True)), m.NLAST[hid]["error"].startswith("Couldn't connect"), get()["last"][hid]["ok"], get()["last"][dests()["Mail"]["id"]]["ok"]), (True, True, False, True))
chk("removing a destination forgets how it went", (put(destinations=keep("Mail", "Second")).status_code, hid in get()["last"]), (200, False))
print(f"{bad[0] - bad[1]}/{bad[0]} passed"); sys.exit(1 if bad[1] else 0)
