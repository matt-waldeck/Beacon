"""Browser test for Settings > Notifications: the page, saving, the test button, secrets staying off the page, and who can see it. Needs Playwright with Chromium. Run: python3 Tests/ui_notify.py"""
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
    ok("the page opens", pg.is_visible("section[data-s=notify]") and not pg.is_visible("section[data-s=look]"))
    ok("everything is on by default, nothing is sent yet", pg.is_checked("#n-device") and pg.is_checked("#n-server") and pg.is_checked("#n-unifi") and not pg.is_checked("#w-on") and not pg.is_checked("#e-on") and pg.input_value("#n-after") == "3")
    ok("mail starts on STARTTLS and 587", pg.input_value("#e-sec") == "starttls" and pg.input_value("#e-port") == "587")
    pg.select_option("#e-sec", "ssl"); ok("a standard port follows the security choice", pg.input_value("#e-port") == "465")
    pg.fill("#e-port", "2525"); pg.select_option("#e-sec", "none"); ok("a custom port is left alone", pg.input_value("#e-port") == "2525")
    pg.select_option("#e-sec", "starttls"); pg.fill("#e-port", "587")
    pg.check("#w-on"); pg.click("#nsave"); pg.wait_for_timeout(500); ok("switching the webhook on without an address is refused", "Enter the webhook address" in pg.inner_text("#nmsg"), pg.inner_text("#nmsg"))
    pg.fill("#w-url", f"http://127.0.0.1:{web.server_port}/hook/top-secret"); pg.select_option("#w-fmt", "discord"); pg.uncheck("#n-server"); pg.fill("#n-after", "2")
    pg.click("#w-test"); pg.wait_for_timeout(1500)
    ok("the test button reports success and the receiver got a Discord message", "Sent" in pg.inner_text("#w-res") and len(GOT) == 1 and '"content"' in GOT[0], (pg.inner_text("#w-res"), GOT))
    pg.fill("#e-host", "127.0.0.1"); pg.fill("#e-port", "1"); pg.fill("#e-from", "beacon@home.lan"); pg.fill("#e-to", "me@home.lan"); pg.click("#e-test"); pg.wait_for_timeout(1500)
    ok("a failed mail test says why", "Couldn't connect" in pg.inner_text("#e-res"), pg.inner_text("#e-res"))
    pg.screenshot(path="/tmp/notify_page.png")
    pg.fill("#e-port", "587"); pg.click("#nsave"); pg.wait_for_timeout(700)
    ok("saving works", pg.inner_text("#nmsg") == "Saved.", pg.inner_text("#nmsg"))
    ok("the address is not shown again, only where it goes", pg.input_value("#w-url") == "" and "127.0.0.1" in pg.get_attribute("#w-url", "placeholder") and "top-secret" not in pg.get_attribute("#w-url", "placeholder"))
    pg.reload(); pg.wait_for_timeout(1500); pg.click("#sb"); pg.click("#sn button[data-s=notify]"); pg.wait_for_timeout(500)
    ok("it is all still there after a reload", pg.is_checked("#w-on") and pg.input_value("#w-fmt") == "discord" and not pg.is_checked("#n-server") and pg.input_value("#n-after") == "2" and pg.input_value("#e-host") == "127.0.0.1" and pg.input_value("#e-to") == "me@home.lan")
    pg.click("#e-test"); pg.wait_for_timeout(1500); ok("a test with the saved server still works the same", "Couldn't connect" in pg.inner_text("#e-res") or "Sent" in pg.inner_text("#e-res"))
    pg.evaluate("fetch('/api/users', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'viewer1', password: 'password1', role: 'viewer'})})"); pg.wait_for_timeout(300)
    v = ctx.new_page(); v.goto(U); v.wait_for_timeout(600); ctx.clear_cookies(); v.reload(); v.wait_for_timeout(900); v.fill("#lu", "viewer1"); v.fill("#lp", "password1"); v.click("#lf .pri"); v.wait_for_timeout(1500)
    v.click("#sb"); ok("a viewer doesn't get the Notifications page", not v.is_visible("#sn button[data-s=notify]"))
    ok("the page ran without script errors", not errs, errs)
    b.close()
finally:
  srv.terminate()
print(f"{res[0]}/{res[0] + res[1]} passed"); sys.exit(1 if res[1] else 0)
