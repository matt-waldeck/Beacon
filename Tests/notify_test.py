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
MAILCFG = {"enabled": True, "host": "127.0.0.1", "port": MPORT, "security": "none", "sender": "beacon@home.lan", "to": "me@home.lan, you@home.lan"}

print("== the settings ==")
d = get()
chk("nothing is sent until a destination is switched on", (d["webhook"]["enabled"], d["email"]["enabled"]), (False, False))
chk("every kind of event is on, after three failed checks", (d["events"], d["after"]), ({"device": True, "server": True, "unifi": True}, 3))
chk("a viewer can neither read nor change them", (v.get("/api/notify").status_code, v.put("/api/notify", json={}).status_code, v.post("/api/notify/test", json={"which": "webhook"}).status_code), (403, 403, 403))
chk("a webhook needs an address", put(webhook={"enabled": True}).status_code, 400)
chk("which has to be http or https", put(webhook={"url": "ftp://x/y"}).status_code, 400)
chk("mail needs a server, a From and a To", put(email={"enabled": True, "host": "x"}).status_code, 400)
chk("the port is checked", put(email={**MAILCFG, "port": 0}).status_code, 400)
chk("so are the addresses", (put(email={**MAILCFG, "to": "nobody"}).status_code, put(email={**MAILCFG, "sender": "a b@c"}).status_code), (400, 400))
chk("a login needs its password", put(email={**MAILCFG, "user": "bob"}).status_code, 400)
chk("the number of checks is limited", (put(after=0).status_code, put(after=21).status_code), (400, 400))
chk("nothing was saved by the failures", os.path.exists(os.path.join(DATA, "notify.json")), False)
r = put(webhook={"enabled": True, "url": HOOK, "format": "generic", "verify": False}, email=MAILCFG, after=2)
j = r.get_json()
chk("saving works", r.status_code, 200)
chk("the webhook address and mail password never come back", ("secret-token" in json.dumps(j), j["webhook"]["set"], j["webhook"]["hint"], j["email"]["password"]), (False, True, "127.0.0.1", False))
chk("the file is private", stat.S_IMODE(os.stat(os.path.join(DATA, "notify.json")).st_mode), 0o600)
chk("a partial save keeps the rest", (put(webhook={"format": "discord"}).get_json()["email"]["to"], get()["webhook"]["set"], get()["after"]), ("me@home.lan, you@home.lan", True, 2))
chk("a blank password keeps the saved one", (put(email={"user": "bob", "password": "pw1"}).status_code, put(email={"user": "bob"}).status_code, m.notify_saved()["email"]["password"]), (200, 200, "pw1"))
put(email={"user": ""}); chk("removing the user name removes the password", (m.notify_saved()["email"]["user"], m.notify_saved()["email"]["password"]), ("", ""))

print("== the test button ==")
n = len(HOOKS); r = a.post("/api/notify/test", json={"which": "webhook", "format": "generic"}).get_json()
chk("a test goes to the saved address, in the format on the page", (r, HOOKS[-1][0], json.loads(HOOKS[-1][2])["event"], json.loads(HOOKS[-1][2])["source"]), ({"ok": True}, "/hook/secret-token", "test", "beacon"))
a.post("/api/notify/test", json={"which": "webhook", "format": "discord"}); chk("discord gets a content field", sorted(json.loads(HOOKS[-1][2])), ["content"])
a.post("/api/notify/test", json={"which": "webhook", "format": "slack"}); chk("slack gets a text field", sorted(json.loads(HOOKS[-1][2])), ["text"])
a.post("/api/notify/test", json={"which": "webhook", "format": "ntfy"}); h = HOOKS[-1]
chk("ntfy gets plain text with a title", (h[2].startswith("If you can read this"), h[1].get("Title"), h[1].get("Content-Type", "").startswith("text/plain")), (True, "Beacon: test message", True))
chk("the webhook was tried four times", len(HOOKS) - n, 4)
r = a.post("/api/notify/test", json={"which": "email"}).get_json()
chk("a test email goes out", (r, [x.upper() for x in MAIL[-1][1]], "Subject: Beacon: test message" in MAIL[-1][2]), ({"ok": True}, ["RCPT TO:<ME@HOME.LAN>", "RCPT TO:<YOU@HOME.LAN>"], True))
r = a.post("/api/notify/test", json={"which": "webhook", "url": "http://127.0.0.1:1/another-secret"}).get_json()
chk("a refused connection is explained without the address", (r["ok"], r["error"].startswith("Couldn't connect"), "another-secret" in r["error"]), (False, True, False))
chk("a test with nothing filled in says what is missing", a.post("/api/notify/test", json={"which": "email", "host": ""}).get_json()["ok"], False)
chk("the test saves nothing", get()["webhook"]["format"], "discord")
put(webhook={"format": "generic"})

print("== when messages go out ==")
now = time.time(); n, k = len(HOOKS), len(MAIL)
m.watch("device", "Switch", False, "SSH: timed out"); time.sleep(.3)
chk("one failed check says nothing yet", (len(HOOKS) - n, len(MAIL) - k), (0, 0))
m.watch("device", "Switch", False, "SSH: timed out")
chk("a second in a row (the setting) sends one webhook and one email", (until(lambda: len(HOOKS) - n == 1 and len(MAIL) - k == 1), len(HOOKS) - n, len(MAIL) - k), (True, 1, 1))
p = json.loads(HOOKS[-1][2])
chk("the webhook says what and why", (p["event"], p["state"], p["kind"], p["name"], p["reason"], p["title"], p["since"] <= time.time()), ("device_down", "down", "device", "Switch", "SSH: timed out", "Beacon: Switch is offline", True))
chk("so does the email", ("Subject: Beacon: Switch is offline" in MAIL[-1][2], "failed 2 checks in a row" in body(MAIL[-1][2]), "SSH: timed out" in body(MAIL[-1][2])), (True, True, True))
m.watch("device", "Switch", False, "again"); m.watch("device", "Switch", False, "again"); time.sleep(.3)
chk("it is only said once while it stays down", len(HOOKS) - n, 1)
m.watch("device", "Switch", True)
chk("and again when it answers", (until(lambda: len(HOOKS) - n == 2), json.loads(HOOKS[-1][2])["event"], "back online" in HOOKS[-1][2]), (True, "device_up", True))
m.watch("device", "Switch", True); time.sleep(.3)
chk("a healthy check says nothing", len(HOOKS) - n, 2)
m.watch("device", "Flaky", False); m.watch("device", "Flaky", True); m.watch("device", "Flaky", False); time.sleep(.3)
chk("a failure that doesn't repeat is never reported", len(HOOKS) - n, 2)

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
put(events={"server": True}, webhook={"enabled": False}); n, k = len(HOOKS), len(MAIL); m.watch("device", "Router", True)
chk("a destination that is switched off gets nothing", (until(lambda: len(MAIL) > k), time.sleep(.3), len(HOOKS) - n, len(MAIL) - k), (True, None, 0, 1))
put(email={"enabled": False}); n, k = len(HOOKS), len(MAIL); m.watch("device", "Router", False); m.watch("device", "Router", False); time.sleep(.4)
chk("with both off nothing is sent", (len(HOOKS) - n, len(MAIL) - k), (0, 0))

print("== which checks count ==")
put(webhook={"enabled": True}); n = len(HOOKS)
for _ in range(3): m.save_sample("Lost", {"ts": time.time(), "error": "UniFi API (Home): refused"}, notify=False)
time.sleep(.3); chk("devices on a controller that is down are not reported one by one", len(HOOKS) - n, 0)
for _ in range(2): m.save_sample("Lost", {"ts": time.time(), "error": "SSH: no route"})
chk("a normal failed sample counts", (until(lambda: len(HOOKS) > n), json.loads(HOOKS[-1][2])["name"]), (True, "Lost"))
m.save_sample("Lost", {"ts": time.time(), "cpu": 5}); until(lambda: len(HOOKS) > n + 1)
chk("and a good one clears it", json.loads(HOOKS[-1][2])["event"], "device_up")
m.watch("device", "Gone", False); m.notify_prune("device", {"Lost"})
chk("removed devices are forgotten", sorted(k[1] for k in m.NW if k[0] == "device"), ["Lost"])

print("== when sending fails ==")
put(webhook={"url": "http://127.0.0.1:1/x"}); n = len(HOOKS)
for _ in range(2): m.watch("device", "Dead", False)
chk("the reason is kept for the page", (until(lambda: not m.NLAST.get("webhook", {}).get("ok", True)), m.NLAST["webhook"]["error"].startswith("Couldn't connect"), "last" in get()), (True, True, True))
print(f"{bad[0] - bad[1]}/{bad[0]} passed"); sys.exit(1 if bad[1] else 0)
