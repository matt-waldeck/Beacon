"""Browser smoke test: sign-in, tabs, styles, lists, apps, tiles, feedback, account menu, roles, inactivity. Needs Playwright with Chromium. Run: python3 tests/ui_smoke.py"""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright
HERE = Path(__file__).resolve().parent; DATA = tempfile.mkdtemp(); PORT = 5064; U = f"http://127.0.0.1:{PORT}/"
env = {**os.environ, "DATA_DIR": DATA, "PYTHONPATH": str(HERE / "stub"), "UI_USER": "boss", "UI_PASSWORD": "secret123", "SSH_USER": "x"}
srv = subprocess.Popen([sys.executable, str(HERE / "serve_sim.py"), str(PORT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(3)
res = [0, 0]
def ok(label, cond, extra=""):
    res[0 if cond else 1] += 1
    if not cond: print(f"  FAIL {label}   {extra}")
def ctxpage(b, **kw):
    ctx = b.new_context(viewport={"width": 1400, "height": 900}, **kw); errs = []
    ctx.route("https://github.com/**", lambda r: r.fulfill(body="<html>github</html>", content_type="text/html")); ctx.route("**/chart.umd.min.js", lambda r: r.abort()); ctx.route("https://cdn.jsdelivr.net/**", lambda r: r.abort())
    pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e))); return ctx, pg, errs
def login(pg, u="boss", p="secret123", remember=False):
    pg.fill("#lu", u); pg.fill("#lp", p); remember and pg.check("#lr"); pg.click("#lf .pri"); pg.wait_for_timeout(1500)
def api(pg, method, url, body=None): return pg.evaluate("async ([m, u, b]) => (await fetch(u, {method: m, headers: {'Content-Type': 'application/json'}, body: b && JSON.stringify(b)})).status", [method, url, body])
names = lambda pg: pg.evaluate("[...document.querySelectorAll('#lpanel .items .li .nm b')].map(e => e.textContent)")
try:
  with sync_playwright() as p:
    b = p.chromium.launch(); ctx, pg, errs = ctxpage(b); pg.goto(U); pg.wait_for_timeout(900)
    ok("sign-in screen first", pg.is_visible("#splash")); pg.fill("#lu", "boss"); pg.fill("#lp", "no"); pg.click("#lf .pri"); pg.wait_for_timeout(500); ok("wrong password is refused", "Wrong" in pg.inner_text("#lerr")); login(pg); ok("signed in", not pg.is_visible("#splash"))
    api(pg, "POST", "/api/settings", {"guest": True}); api(pg, "POST", "/api/users", {"name": "viewer1", "password": "password1", "role": "viewer"})
    for n, h in (("Office AP", "192.168.1.20"), ("Core Switch", "192.168.1.2")): api(pg, "POST", "/api/devices", {"name": n, "host": h})
    api(pg, "POST", "/api/servers", {"name": "NAS", "host": "10.0.0.2", "type": "server"}); pg.wait_for_timeout(3500); pg.reload(); pg.wait_for_timeout(1800)
    for tab in ("home", "network", "servers", "apps"):
        pg.click(f"#tabs button[data-t={tab}]"); pg.wait_for_timeout(700)
        ok(f"{tab} tab shows", pg.is_visible("#lpanel") if tab != "home" else pg.is_visible("#grid"))
    pg.click("#tabs button[data-t=network]"); pg.wait_for_timeout(800)
    ok("devices listed A-Z with live numbers", names(pg) == ["Core Switch", "Office AP"] and "–" not in pg.inner_text("#lpanel .items .li:nth-child(2) .c3"), names(pg))
    pg.click("#lpanel .items .li:first-child .lr"); pg.wait_for_timeout(500); ok("a row opens with a graph", pg.is_visible("#lpanel .items .li:first-child .ld canvas"))
    for st in ("honeycrisp", "pastel", "stark"):
        pg.click("#sb"); pg.select_option("#style", st); pg.click("#sx"); pg.wait_for_timeout(500)
        g = pg.evaluate("(() => { const p = document.querySelector('#pill'), a = document.querySelector('#tabs button[aria-current=page]'); return Math.abs(p.offsetLeft - a.offsetLeft) <= 1 && p.offsetWidth === a.offsetWidth; })()"); ok(f"tab oval is on the selected tab ({st})", g)
    pg.click("#sb"); pg.select_option("#style", "honeycrisp"); pg.click("#sx")
    pg.click("#tabs button[data-t=apps]"); pg.wait_for_timeout(600); pg.click("#lpanel .add"); pg.fill("#lpanel .addfm input[name=name]", "Lab Wiki"); pg.fill("#lpanel .addfm input[name=icon]", "📚"); pg.fill("#lpanel .addfm input[name=url]", "https://wiki.lan"); pg.click("#lpanel .addfm .pri"); pg.wait_for_timeout(900)
    ok("an app with an emoji icon is listed", "Lab Wiki" in names(pg) and "📚" in pg.inner_text("#lpanel .items"))
    pg.click("#tabs button[data-t=home]"); pg.wait_for_timeout(600); pg.click("#edit"); pg.select_option("#add", "app:"); pg.wait_for_timeout(400); pg.select_option("#grid .t-app select", label="Lab Wiki"); pg.wait_for_timeout(500)
    ok("an App tile shows the chosen app", pg.locator("#grid .t-app a.app b").inner_text() == "Lab Wiki"); pg.click("#edit")
    pg.click("#sb"); pg.click("#sn button[data-s=fb]"); pg.fill("#fb-title", "Smoke test"); pg.fill("#fb-body", "hello")
    with ctx.expect_page() as pop: pg.click("#fb-go")
    q = urlparse(pop.value.url); pop.value.close(); ok("feedback opens a prefilled GitHub issue", q.path.endswith("/issues/new") and parse_qs(q.query)["title"] == ["Smoke test"]); pg.click("#sx")
    pg.click("#who"); ok("account menu offers Sign out", pg.is_visible("#umo")); pg.click("#umo"); pg.wait_for_timeout(700); ok("signed out to the splash", pg.is_visible("#splash"))
    login(pg, "viewer1", "password1"); ok("viewer: no add or edit controls", not pg.is_visible("#edit"))
    ok("viewer is refused admin data", (api(pg, "GET", "/api/users"), api(pg, "GET", "/api/storage"), api(pg, "PUT", "/api/dash", {}), api(pg, "POST", "/api/settings", {"interval": 31})) == (403, 403, 403, 403))
    pg.click("#who"); pg.click("#umo"); pg.wait_for_timeout(700); pg.click("#lg"); pg.wait_for_timeout(1500); pg.click("#sb"); ok("guest: no Feedback, Users or Account", pg.is_hidden("#sn button[data-s=fb]") and pg.is_hidden("#sn button[data-s=users]") and pg.is_hidden("#sn button[data-s=account]"))
    print("  page errors:", errs or "none"); ok("no page errors", not errs, errs); ctx.close()
    ctx, pg, errs = ctxpage(b); pg.clock.install(); pg.goto(U); pg.clock.resume(); pg.wait_for_timeout(900); login(pg); api(pg, "POST", "/api/settings", {"idle_minutes": 2})
    pg.click("#who"); pg.click("#umo"); pg.wait_for_timeout(600); login(pg); pg.clock.fast_forward("03:30"); pg.wait_for_timeout(1500); ok("idle sign-out explains itself", pg.is_visible("#splash") and "inactivity" in pg.inner_text("#lerr")); ctx.close()
    ctx, pg, errs = ctxpage(b); pg.clock.install(); pg.goto(U); pg.clock.resume(); pg.wait_for_timeout(900); login(pg, remember=True); pg.clock.fast_forward("10:00"); pg.wait_for_timeout(800); ok("keep-me-signed-in is exempt", not pg.is_visible("#splash")); ctx.close(); b.close()
finally:
    srv.terminate(); shutil.rmtree(DATA, ignore_errors=True)
print(f"\nui smoke: {res[0]} passed, {res[1]} failed"); sys.exit(1 if res[1] else 0)
