"""Browser test for Settings > Notifications: what is sent, batching, adding and configuring destinations like the UniFi sites, the test button, secrets staying off the page, and who can see it. Needs Playwright with Chromium. Run: python3 Tests/ui_notify.py"""
import os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright
HERE = Path(__file__).resolve().parent; DATA = tempfile.mkdtemp(); PORT = 5066; U = f"http://127.0.0.1:{PORT}/"
GOT = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self): GOT.append(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()); self.send_response(204); self.end_headers()
web = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=web.serve_forever, daemon=True).start()
env = {**os.environ, "DATA_DIR": DATA, "PYTHONPATH": str(HERE / "stub"), "UI_USER": "boss", "UI_PASSWORD": "secret123", "SSH_USER": "x"}
srv = subprocess.Popen([sys.executable, str(HERE / "serve_sim.py"), str(PORT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(3)
res = [0, 0]
def ok(label, cond, extra=""):
    res[0 if cond else 1] += 1
    if not cond: print(f"  FAIL {label}   {extra}")
try:
  with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 1400, "height": 900}); ctx.route("https://cdn.jsdelivr.net/**", lambda r: r.abort()); pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(U); pg.wait_for_timeout(900); pg.fill("#lu", "boss"); pg.fill("#lp", "secret123"); pg.click("#lf .pri"); pg.wait_for_timeout(1500)
    pg.click("#sb"); pg.click("#sn button[data-s=notify]"); pg.wait_for_timeout(500)
    rows = lambda: pg.locator("#nd-rows .ds"); cards = lambda: pg.locator("#n-list .us")
    ok("the page opens", pg.is_visible("section[data-s=notify]") and not pg.is_visible("section[data-s=look]"))
    ok("everything is on by default, batching is off, nothing is set up", pg.is_checked("#n-device") and pg.is_checked("#n-server") and pg.is_checked("#n-unifi") and pg.input_value("#n-after") == "3" and not pg.is_checked("#n-batch") and pg.is_disabled("#n-bmin") and "No destinations yet" in pg.inner_text("#n-list"))
    pg.check("#n-batch"); ok("switching batching on enables the box with a sensible number", pg.is_enabled("#n-bmin") and pg.input_value("#n-bmin") == "5")
    pg.fill("#n-bmin", "1"); pg.click("#nsave"); pg.wait_for_timeout(500); ok("one is refused", "2 to 50" in pg.inner_text("#nmsg"), pg.inner_text("#nmsg"))
    pg.fill("#n-bmin", "4"); pg.uncheck("#n-server"); pg.fill("#n-after", "2"); pg.click("#nsave"); pg.wait_for_timeout(500); ok("saving works", pg.inner_text("#nmsg") == "Saved.", pg.inner_text("#nmsg"))
    ok("the page explains the wait", "15 seconds" in pg.inner_text("#n-bhelp"))
    pg.reload(); pg.wait_for_timeout(1500); pg.click("#sb"); pg.click("#sn button[data-s=notify]"); pg.wait_for_timeout(500)
    ok("it is all still there after a reload", pg.is_checked("#n-batch") and pg.input_value("#n-bmin") == "4" and pg.input_value("#n-after") == "2" and not pg.is_checked("#n-server"))
    pg.uncheck("#n-batch"); ok("switching it off greys the box out", pg.is_disabled("#n-bmin")); pg.click("#nsave"); pg.wait_for_timeout(500)
    pg.click("#sn button[data-s=look]"); pg.click("#sn button[data-s=notify]"); pg.wait_for_timeout(500); ok("and off is remembered", not pg.is_checked("#n-batch") and pg.is_disabled("#n-bmin"))

    pg.click("#n-cfg"); pg.wait_for_timeout(500); ok("Configure opens the popup, empty to begin with", pg.is_visible("#nd") and rows().count() == 0)
    pg.click("#nd-addw"); pg.click("#nd-addw"); pg.click("#nd-adde"); pg.click("#nd-adde")
    ok("destinations are added one after another, each with its own name", [rows().nth(i).locator(".f-name").input_value() for i in range(4)] == ["Webhook", "Webhook 2", "Email", "Email 2"], [rows().nth(i).locator(".f-name").input_value() for i in range(4)])
    ok("an email starts on STARTTLS and 587, and a standard port follows the security choice", rows().nth(2).locator(".f-sec").input_value() == "starttls" and rows().nth(2).locator(".f-port").input_value() == "587")
    rows().nth(2).locator(".f-sec").select_option("ssl"); ok("(465 after choosing SSL/TLS)", rows().nth(2).locator(".f-port").input_value() == "465")
    rows().nth(2).locator(".f-port").fill("2525"); rows().nth(2).locator(".f-sec").select_option("none"); ok("a custom port is left alone", rows().nth(2).locator(".f-port").input_value() == "2525")
    rows().nth(2).locator(".f-sec").select_option("starttls"); rows().nth(3).locator(".f-del").click(); rows().nth(1).locator(".f-del").click()
    ok("a row can be removed", rows().count() == 2)
    w, e = rows().nth(0), rows().nth(1)
    w.locator(".f-url").fill(f"http://127.0.0.1:{web.server_port}/hook/top-secret"); w.locator(".f-fmt").select_option("discord"); w.locator(".f-name").fill("Discord")
    w.locator(".f-test").click(); pg.wait_for_timeout(1500)
    ok("Test reports success and the receiver got a Discord message", "Sent" in w.locator(".f-res").inner_text() and len(GOT) == 1 and '"content"' in GOT[0], (w.locator(".f-res").inner_text(), GOT))
    e.locator(".f-name").fill("Mail"); e.locator(".f-host").fill("127.0.0.1"); e.locator(".f-port").fill("1"); e.locator(".f-from").fill("beacon@home.lan"); e.locator(".f-to").fill("me@home.lan"); e.locator(".f-test").click(); pg.wait_for_timeout(1500)
    ok("a failed mail test says why, on that row", "Couldn't connect" in e.locator(".f-res").inner_text() and w.locator(".f-res").inner_text().startswith("Sent"), e.locator(".f-res").inner_text())
    pg.screenshot(path="/tmp/notify_dialog.png")
    pg.click("#nd-addw"); rows().nth(2).locator(".f-name").fill("Empty"); pg.click("#nd-save"); pg.wait_for_timeout(500)
    ok("switching one on without an address is refused, naming it", "Empty:" in pg.inner_text("#nd-err") and "webhook address" in pg.inner_text("#nd-err") and pg.is_visible("#nd"), pg.inner_text("#nd-err"))
    rows().nth(2).locator(".f-name").fill("Discord"); pg.click("#nd-save"); pg.wait_for_timeout(500); ok("and so is a repeated name", "own name" in pg.inner_text("#nd-err"), pg.inner_text("#nd-err"))
    rows().nth(2).locator(".f-del").click(); e.locator(".f-port").fill("587"); pg.click("#nd-save"); pg.wait_for_timeout(700)
    ok("saving closes the popup and shows the destinations", not pg.is_visible("#nd") and pg.inner_text("#ndmsg") == "Saved." and cards().count() == 2, (pg.inner_text("#ndmsg"), cards().count()))
    t = pg.inner_text("#n-list")
    ok("each card says what it is and where it goes, without the address", "Discord" in t and "Webhook" in t and "127.0.0.1" in t and "me@home.lan" in t and "top-secret" not in t and pg.locator("#n-list .neon").count() == 2, t)
    pg.screenshot(path="/tmp/notify_page.png")
    pg.reload(); pg.wait_for_timeout(1500); pg.click("#sb"); pg.click("#sn button[data-s=notify]"); pg.wait_for_timeout(500); ok("they are still there after a reload", cards().count() == 2)
    pg.click("#n-cfg"); pg.wait_for_timeout(500); w, e = rows().nth(0), rows().nth(1)
    ok("reopened, the saved address is only hinted at", w.locator(".f-url").input_value() == "" and "Saved (127.0.0.1)" in w.locator(".f-url").get_attribute("placeholder") and "top-secret" not in w.locator(".f-url").get_attribute("placeholder") and w.locator(".f-fmt").input_value() == "discord" and e.locator(".f-to").input_value() == "me@home.lan")
    w.locator(".f-test").click(); pg.wait_for_timeout(1500); ok("a test with the address left blank uses the saved one", "Sent" in w.locator(".f-res").inner_text() and len(GOT) == 2, (w.locator(".f-res").inner_text(), len(GOT)))
    e.locator(".f-del").click(); w.locator(".f-on").uncheck(); pg.click("#nd-save"); pg.wait_for_timeout(700)
    ok("removing one and switching another off shows in the list", cards().count() == 1 and "Off" in pg.inner_text("#n-list"), pg.inner_text("#n-list"))
    pg.evaluate("fetch('/api/users', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'viewer1', password: 'password1', role: 'viewer'})})"); pg.wait_for_timeout(300)
    v = ctx.new_page(); v.goto(U); v.wait_for_timeout(600); ctx.clear_cookies(); v.reload(); v.wait_for_timeout(900); v.fill("#lu", "viewer1"); v.fill("#lp", "password1"); v.click("#lf .pri"); v.wait_for_timeout(1500)
    v.click("#sb"); ok("a viewer doesn't get the Notifications page", not v.is_visible("#sn button[data-s=notify]"))
    ok("the page ran without script errors", not errs, errs)
    b.close()
finally:
  srv.terminate()
print(f"{res[0]}/{res[0] + res[1]} passed"); sys.exit(1 if res[1] else 0)
