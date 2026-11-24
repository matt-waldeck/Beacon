"""Server tests for SSH host-key pinning (trust on first use). Run: python3 tests/hostkey_test.py  (needs Flask; no devices or network).
Always runs against tests/stub/paramiko.py, which copies the parts of paramiko's interface that Beacon uses."""
import contextlib, importlib.util, io, os, stat, sys, tempfile, threading, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
os.environ.update(DATA_DIR=tempfile.mkdtemp(), UI_USER="boss", UI_PASSWORD="secret123")
sys.path.insert(0, str(HERE / "stub"))  # the stand-in wins even when the real paramiko is installed
with contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location("app", HERE.parent / "app.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from paramiko import BadHostKeyException, FakeKey
bad = [0, 0]
def chk(label, got, want):
    bad[0] += 1
    if got != want: bad[1] += 1; print(f"  FAIL {label}: got {got!r}, wanted {want!r}")
lines = lambda: [l for l in open(m.KNOWN_HOSTS).read().splitlines() if l.strip()] if os.path.exists(m.KNOWN_HOSTS) else []
def seen(host, key):
    c, p = m.paramiko.SSHClient(), m.TrustFirst()
    try: p.missing_host_key(c, host, key); return "trusted"
    except BadHostKeyException: return "refused"
a = m.app.test_client(); a.post("/api/login", json={"name": "boss", "password": "secret123"})
print("== first sight ==")
chk("nothing is stored at the start", lines(), [])
chk("a new device is trusted and remembered", (seen("10.0.0.1", FakeKey("ssh-ed25519", "key-one")), len(lines())), ("trusted", 1))
chk("the file is private", stat.S_IMODE(os.stat(m.KNOWN_HOSTS).st_mode), 0o600)
chk("another device is added beside it", (seen("10.0.0.2", FakeKey("ssh-ed25519", "key-two")), len(lines())), ("trusted", 2))
print("== a changed key ==")
before = open(m.KNOWN_HOSTS).read()
chk("a different key for a known device is refused", seen("10.0.0.1", FakeKey("ssh-ed25519", "someone-else")), "refused")
chk("so is a different key type for it", seen("10.0.0.1", FakeKey("ssh-rsa", "rsa-key")), "refused")
chk("and nothing was overwritten", open(m.KNOWN_HOSTS).read(), before)
def boom(d): raise BadHostKeyException("10.0.0.1", FakeKey("ssh-ed25519", "x"), FakeKey("ssh-ed25519", "y"))
m.ssh_run = boom; m.poll({"name": "AP", "host": "10.0.0.1"}); last = m.sql("SELECT data FROM samples WHERE dev='AP'")[0][0]
chk("the device shows a clear message", ("host key has changed" in last, "Forget SSH host keys" in last), (True, True))
print("== several at once ==")
ts = [threading.Thread(target=seen, args=(f"10.1.0.{i}", FakeKey("ssh-ed25519", f"k{i}"))) for i in range(12)]; [t.start() for t in ts]; [t.join() for t in ts]
chk("no device is lost when many are first seen together", len(lines()), 2 + 12)
print("== listing ==")
v = m.app.test_client(); a.post("/api/users", json={"name": "v", "password": "password1", "role": "viewer"}); v.post("/api/login", json={"name": "v", "password": "password1"})
a.post("/api/devices", json={"name": "Office AP", "host": "10.0.0.1"})
ks = a.get("/api/hostkeys").get_json()["keys"]
chk("a known device is named, the others are not", (ks[0]["name"], sum(1 for k in ks if k["name"]), len(ks)), ("Office AP", 1, 14))
chk("each shows its key type and a fingerprint", (ks[0]["type"], ks[0]["fingerprint"].startswith("SHA256:"), "=" in ks[0]["fingerprint"]), ("ssh-ed25519", True, False))
chk("viewers can't list or forget", (v.get("/api/hostkeys").status_code, v.post("/api/hostkeys/forget", json={"all": True}).status_code, len(lines())), (403, 403, 14))
print("== forgetting ==")
chk("nothing chosen is refused", (a.post("/api/hostkeys/forget", json={"hosts": []}).status_code, a.post("/api/hostkeys/forget", json={}).status_code, a.post("/api/hostkeys/forget", json={"hosts": [1]}).status_code), (400, 400, 400))
r = a.post("/api/hostkeys/forget", json={"hosts": ["10.0.0.1", "10.1.0.3"]}); chk("chosen devices are forgotten", (r.status_code, r.get_json()["removed"], len(lines())), (200, 2, 12))
chk("the others are kept and still protected", (seen("10.0.0.2", FakeKey("ssh-ed25519", "impostor")), seen("10.0.0.1", FakeKey("ssh-ed25519", "new-key")), len(lines())), ("refused", "trusted", 13))
chk("an unknown address removes nothing", a.post("/api/hostkeys/forget", json={"hosts": ["192.0.2.9"]}).get_json()["removed"], 0)
r = a.post("/api/hostkeys/forget", json={"all": True}); chk("all can be forgotten at once", (r.get_json()["removed"], os.path.exists(m.KNOWN_HOSTS)), (13, False))
chk("nothing to forget is fine", a.post("/api/hostkeys/forget", json={"all": True}).get_json()["removed"], 0)
chk("the old purge action is gone", a.post("/api/storage/purge", json={"what": "hostkeys"}).status_code, 400)
chk("a device is trusted again afterwards", (seen("10.0.0.1", FakeKey("ssh-ed25519", "someone-else")), len(lines())), ("trusted", 1))
print("== password guessing ==")
chk("the default allows five", a.get("/api/security").get_json()["max_attempts"], 5)
w = m.app.test_client(); w.post("/api/login", json={"name": "v", "password": "password1"})
codes = [w.post("/api/me/password", json={"current": "wrong", "new": "newpassword1"}).status_code for _ in range(6)]
chk("five wrong guesses, then locked", codes, [400] * 5 + [429])
chk("even the right password waits", w.post("/api/me/password", json={"current": "password1", "new": "newpassword1"}).status_code, 429)
chk("other people are not affected", a.post("/api/me/password", json={"current": "secret123", "new": "secret1234"}).status_code, 200)
chk("the limit can be changed", (a.post("/api/settings", json={"max_attempts": 2}).get_json()["max_attempts"], a.get("/api/security").get_json()["max_attempts"]), (2, 2))
m.fails.clear(); w2 = m.app.test_client(); w2.post("/api/login", json={"name": "v", "password": "password1"})
chk("a lower limit locks sooner", [w2.post("/api/me/password", json={"current": "wrong", "new": "newpassword1"}).status_code for _ in range(3)], [400, 400, 429])
m.fails.clear(); c3 = m.app.test_client(); chk("signing in follows it too", [c3.post("/api/login", json={"name": "v", "password": "bad"}).status_code for _ in range(3)], [401, 401, 429])
chk("kept between 1 and 20", (a.post("/api/settings", json={"max_attempts": 0}).get_json()["max_attempts"], a.post("/api/settings", json={"max_attempts": 99}).get_json()["max_attempts"]), (1, 20))
chk("text refused, viewers can't change it", (a.post("/api/settings", json={"max_attempts": "many"}).status_code, v.post("/api/settings", json={"max_attempts": 9}).status_code, v.get("/api/security").status_code), (400, 403, 403))
print(f"  host key suite: {bad[0] - bad[1]}/{bad[0]} checks passed"); sys.exit(1 if bad[1] else 0)
