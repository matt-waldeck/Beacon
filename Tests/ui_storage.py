"""Browser test for retention, long ranges, storage and purge. Needs Playwright with Chromium. Run: python3 tests/ui_storage.py"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
from playwright.sync_api import sync_playwright
DATA = tempfile.mkdtemp(); PORT = 5063; U = f"http://127.0.0.1:{PORT}/"
env = {**os.environ, "DATA_DIR": DATA, "PYTHONPATH": str(HERE / "stub"), "UI_USER": "boss", "UI_PASSWORD": "secret123", "SSH_USER": "x"}
srv = subprocess.Popen([sys.executable, str(HERE / "serve_sim.py"), str(PORT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(3)
res = [0, 0]
def ok(label, cond, extra=""):
    res[0 if cond else 1] += 1
    if not cond: print(f"  FAIL {label}   {extra}")
post = lambda pg, url, body: pg.evaluate("async ([u, b]) => (await fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)})).status", [url, body])
def login(pg, u="boss", p="secret123"): pg.fill("#lu", u); pg.fill("#lp", p); pg.click("#lf .pri"); pg.wait_for_timeout(1500)
def opts(pg): return pg.evaluate("[...document.querySelectorAll('#range option')].filter(o => !o.hidden && !o.disabled).map(o => o.value)")
try:
  with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 1400, "height": 900}); pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    ctx.route("**/chart.umd.min.js", lambda r: r.abort()); ctx.route("https://cdn.jsdelivr.net/**", lambda r: r.abort())
    pg.goto(U); pg.wait_for_timeout(900); login(pg)
    for n, h in (("Office AP", "192.168.1.20"), ("Core Switch", "192.168.1.2")): post(pg, "/api/devices", {"name": n, "host": h})
    post(pg, "/api/servers", {"name": "NAS", "host": "10.0.0.2"}); post(pg, "/api/users", {"name": "viewer1", "password": "password1", "role": "viewer"}); pg.wait_for_timeout(2500)
    now = time.time(); db = sqlite3.connect(DATA + "/stats.db", timeout=20)  # ten days of history, written straight into the database
    for dev, base in (("Office AP", 20), ("Core Switch", 8)):
        db.executemany("INSERT INTO samples (dev, ts, data, m_cpu, m_mem, m_load, m_rx, m_tx, m_clients) VALUES (?,?,?,?,?,?,?,?,?)", [(dev, now - 600 - i * 300, json.dumps({"ts": now - 600 - i * 300, "cpu": base + i % 7, "mem": 50, "load": .3, "rx": 5e6, "tx": 2e6, "clients": 12, "model": "M", "version": "1"}), base + i % 7, 50, .3, 5e6, 2e6, 12) for i in range(2880)])
    db.executemany("INSERT INTO pings VALUES ('NAS', ?, ?)", [(now - 600 - i * 60, None if i % 10 == 0 else 4.0 + i % 5) for i in range(14400)]); db.commit(); db.close()
    print("== retention and ranges ==")
    pg.click("#sb"); pg.click("#sn button[data-s=mon]")
    ok("retention field shows the default 7", pg.input_value("#ret") == "7"); ok("ranges offered at 7 days: up to 7 days", opts(pg) == ["15", "60", "360", "1440", "10080"], opts(pg))
    pg.fill("#ret", "30"); pg.click("#rsave"); pg.wait_for_timeout(1500); ok("saved with a confirmation", "Saved" in pg.inner_text("#mmsg")); ok("30 days now offered", opts(pg)[-1] == "43200", opts(pg))
    pg.click("#sn button[data-s=look]"); pg.select_option("#range", "10080"); pg.click("#sx"); pg.wait_for_timeout(1500); pg.reload(); pg.wait_for_timeout(2000)
    ok("the chosen range is remembered after a reload", pg.input_value("#range") == "10080" if False else pg.evaluate("document.querySelector('#range').value") == "10080")
    pg.click("#tabs button[data-t=network]"); pg.wait_for_timeout(1200); pg.click("#lpanel .items .li:first-child .lr"); pg.wait_for_timeout(800)
    draws = pg.evaluate("(() => { const c = document.querySelector('#lpanel .li .ld canvas'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; })()")
    ok("a 7-day graph draws from averaged data", draws > 500, draws)
    ok("graph labels carry dates for long ranges, times for short", pg.evaluate("RANGE") == 10080 and any(m in pg.evaluate("lab(Date.now() / 1000)") for m in ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]))
    pg.click("#tabs button[data-t=servers]"); pg.wait_for_timeout(1500); pg.click("#lpanel .items .li:first-child .lr"); pg.wait_for_timeout(600)
    facts = pg.inner_text("#lpanel .items .li:first-child .ld .f"); ok("server facts work over averaged steps (average, worst, loss)", "Loss" in facts and "ms" in facts and "–" not in facts.split("Loss")[1][:6], facts.replace("\n", " "))
    ok("loss is about the 10% that was seeded", 6 <= int(facts.split("Loss")[1].strip().split("%")[0]) <= 14, facts.replace("\n", " "))
    pg.click("#sb"); pg.click("#sn button[data-s=mon]"); pg.fill("#ret", "3"); pg.click("#rsave"); pg.wait_for_timeout(2500); pg.click("#sx")
    ok("shortening hides 7- and 30-day ranges and falls back", opts(pg) == ["15", "60", "360", "1440"] and pg.evaluate("document.querySelector('#range').value") == "1440", opts(pg))
    pg.click("#sb"); pg.click("#sn button[data-s=mon]"); pg.fill("#ret", "30"); pg.click("#rsave"); pg.wait_for_timeout(1500); pg.click("#sx")

    print("== storage ==")
    pg.click("#sb"); pg.click("#sn button[data-s=about]"); pg.wait_for_timeout(1200)
    st = {k: pg.inner_text("#st-" + k) for k in ("db", "span", "ic", "all", "disk")}; ok("storage figures show", all(v and v != "–" for v in st.values()) and "device samples" in st["db"] and "pings" in st["db"], st)
    ok("it knows how far back the history goes", "days" in st["span"] and "keeping 30" in st["span"], st["span"])
    ok("icon source dropdown sits with the storage figures and defaults to selfh.st", pg.is_visible("#icsrc") and pg.input_value("#icsrc") == "selfhst")
    pg.select_option("#icsrc", "dashboard"); pg.wait_for_timeout(1000); ok("changing the icon source confirms it", "Icon source changed" in pg.inner_text("#st-msg") and pg.evaluate("fetch('/api/me').then(r => r.json()).then(m => m.icons)") == "dashboard")
    pg.select_option("#icsrc", "selfhst"); pg.wait_for_timeout(800)
    pg.once("dialog", lambda d: d.dismiss()); pg.click("#st-hist"); pg.wait_for_timeout(600); ok("cancelling a delete keeps everything", pg.evaluate("fetch('/api/storage').then(r => r.json()).then(s => s.db.samples)") > 1000)  # shortening to 3 days earlier already pruned the older rows
    pg.once("dialog", lambda d: d.accept()); pg.click("#st-icons"); pg.wait_for_timeout(800); ok("clear icon cache reports back", "Removed" in pg.inner_text("#st-msg"), pg.inner_text("#st-msg"))
    pg.click("#st-compact"); pg.wait_for_timeout(1200); ok("compact reports back", "compacted" in pg.inner_text("#st-msg"))
    pg.once("dialog", lambda d: d.accept()); pg.click("#st-hist"); pg.wait_for_timeout(1800)
    s = pg.evaluate("fetch('/api/storage').then(r => r.json())"); ok("history deleted (a few fresh samples may arrive straight after)", s["db"]["samples"] < 20 and s["db"]["pings"] < 20, s["db"])
    ok("devices and servers are still there", pg.evaluate("fetch('/api/data').then(r => r.json()).then(d => d.devices.length)") == 2)
    pg.click("#sx"); pg.click("#who"); pg.click("#umo"); pg.wait_for_timeout(700); login(pg, "viewer1", "password1"); pg.click("#sb"); pg.click("#sn button[data-s=about]")
    ok("viewers see no storage block or icon source", not pg.is_visible("#st-db") and not pg.is_visible("#st-hist") and not pg.is_visible("#icsrc"))
    pg.click("#sx"); pg.click("#who"); pg.click("#umo"); pg.wait_for_timeout(700); login(pg)
    print("== security ==")
    open(DATA + "/known_hosts", "w").write("192.168.1.20 ssh-ed25519 AAAA\n192.168.1.2 ssh-ed25519 BBBB\n10.9.9.9 ssh-ed25519 CCCC\n")
    pg.click("#sb"); pg.click("#sn button[data-s=sec]"); pg.wait_for_timeout(600)
    ok("the attempt limit shows its default and can be saved", pg.input_value("#att") == "5"); pg.fill("#att", "8"); pg.click("#asave"); pg.wait_for_timeout(600); ok("attempt limit saved", "Saved" in pg.inner_text("#amsg") and pg.evaluate("fetch('/api/security').then(r => r.json()).then(s => s.max_attempts)") == 8)
    pg.click("#hk-open"); pg.wait_for_timeout(700); ok("the popup lists stored keys by device name", pg.is_visible("#kd") and "Office AP" in pg.inner_text("#klist") and "Not in your device list" in pg.inner_text("#klist"), pg.inner_text("#klist"))
    ok("nothing is chosen at first", pg.is_disabled("#kgo"))
    pg.check("#klist label:has(b:text('Office AP')) input"); ok("choosing one enables the button", pg.is_enabled("#kgo") and "Forget 1 selected" in pg.inner_text("#kgo"))
    pg.check("#kall"); ok("the all box ticks every device", pg.evaluate("[...document.querySelectorAll('#klist input')].every(x => x.checked)") and "Forget 3 selected" in pg.inner_text("#kgo"))
    pg.uncheck("#klist label:has(b:text('Office AP')) input"); ok("unticking one clears the all box", not pg.is_checked("#kall"))
    pg.click("#kx"); ok("cancel closes the popup", not pg.is_visible("#kd")); ok("and forgets nothing", len(open(DATA + "/known_hosts").read().split("\n")) == 4)
    pg.click("#hk-open"); pg.wait_for_timeout(500); pg.check("#klist label:has(b:text('Office AP')) input"); pg.click("#kgo"); pg.wait_for_timeout(800)
    ok("forgetting one device reports back and keeps the rest", "Forgot 1 host key" in pg.inner_text("#hkmsg") and "192.168.1.20" not in open(DATA + "/known_hosts").read() and "10.9.9.9" in open(DATA + "/known_hosts").read())
    pg.click("#hk-open"); pg.wait_for_timeout(500); pg.check("#kall"); pg.click("#kgo"); pg.wait_for_timeout(800); ok("forgetting all removes the file", "Forgot 2 host keys" in pg.inner_text("#hkmsg") and not os.path.exists(DATA + "/known_hosts"))
    pg.click("#hk-open"); pg.wait_for_timeout(500); ok("with none stored it says so", "No keys are stored" in pg.inner_text("#klist") and pg.is_disabled("#kall")); pg.click("#kx")
    print("  page errors:", errs or "none"); b.close()
finally:
    srv.terminate(); shutil.rmtree(DATA, ignore_errors=True)
print(f"\nstorage UI: {res[0]} passed, {res[1]} failed")
