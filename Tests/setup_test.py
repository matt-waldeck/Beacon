"""Server tests for first-run setup. Run: python3 tests/setup_test.py  (needs Flask; no real devices or network)"""
import contextlib, importlib.util, io, os, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
os.environ.pop("UI_PASSWORD", None); os.environ.pop("UI_AUTH", None); os.environ.update(DATA_DIR=tempfile.mkdtemp())
sys.path.append(str(HERE / "stub"))
with contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location("app", HERE.parent / "app.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = [0, 0]
def chk(label, got, want):
    bad[0] += 1
    if got != want: bad[1] += 1; print(f"  FAIL {label}: got {got!r}, wanted {want!r}")
S = lambda r: r.status_code
c = m.app.test_client()
print("== first start ==")
chk("no account is created or printed", m.users(), []); chk("/api/me asks for setup", (c.get("/api/me").get_json()["setup"], c.get("/api/me").get_json()["user"]), (True, None))
chk("data stays closed before setup", S(c.get("/api/data")), 401); chk("sign-in has nobody to sign in", S(c.post("/api/login", json={"name": "admin", "password": "whatever1"})), 401)
print("== creating the admin ==")
chk("bad email refused", S(c.post("/api/setup", json={"name": "me", "email": "nope", "password": "longenough"})), 400); chk("blank email refused", S(c.post("/api/setup", json={"password": "longenough"})), 400)
chk("short password refused", S(c.post("/api/setup", json={"email": "a@b.co", "password": "short"})), 400); chk("still waiting after refusals", m.users(), [])
chk("setup succeeds", S(c.post("/api/setup", json={"name": "matt", "email": "matt@example.com", "password": "longenough"})), 200)
u = m.users(); chk("one admin with email and a hashed password", (len(u), u[0]["name"], u[0]["role"], u[0]["email"], "longenough" in u[0]["hash"]), (1, "matt", "admin", "matt@example.com", False))
chk("signed in straight away", (c.get("/api/me").get_json()["user"], c.get("/api/me").get_json()["setup"], S(c.get("/api/data"))), ({"name": "matt", "role": "admin"}, False, 200))
chk("email shown to admins in the user list", c.get("/api/users").get_json()["users"][0]["email"], "matt@example.com")
print("== closed afterwards ==")
other = m.app.test_client(); chk("a second setup is refused", S(other.post("/api/setup", json={"name": "x", "email": "x@y.co", "password": "longenough"})), 403); chk("still one user", len(m.users()), 1)
chk("normal sign-in works", S(other.post("/api/login", json={"name": "matt", "password": "longenough"})), 200)
print(f"  setup suite: {bad[0] - bad[1]}/{bad[0]} checks passed"); sys.exit(1 if bad[1] else 0)
