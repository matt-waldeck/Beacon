"""Runs Beacon on a port with made-up device samples and pings, for the browser tests. Usage: python3 tests/serve_sim.py 5056"""
import json, random, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "stub"))  # a stand-in for paramiko is used only if the real one isn't installed
import app
rnd = random.Random(7)
def fake_poll(d):
    now = time.time(); ap = "AP" in d["name"]
    r = {"ts": now, "uptime": 86400 * 12 + now % 5000, "load": round(rnd.uniform(.05, .9), 2), "mem": round(rnd.uniform(40, 62), 1), "cpu": round(rnd.uniform(3, 35), 1),
         "rx": int(rnd.uniform(2e6, 4e7)), "tx": int(rnd.uniform(1e6, 2e7)), "model": "U6-Enterprise" if ap else "USW-Pro-24-PoE", "version": "7.1.26"}
    if ap: r.update(clients=rnd.randint(14, 30), bands={"2.4": rnd.randint(3, 9), "5": rnd.randint(8, 16), "6": rnd.randint(1, 5)})
    else: r.update(poe=round(rnd.uniform(35, 60), 1))
    app.save_sample(d["name"], r)
app.poll = fake_poll
app.ping = lambda host: None if host.endswith(".3") else round(rnd.uniform(.3, 24), 1)
app.app.run(port=int(sys.argv[1]), threaded=True)
