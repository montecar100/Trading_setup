import time, os, logging
import urllib.request, json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "60"))
ADAPTER_URL = os.environ.get("ADAPTER_URL", "http://173.212.223.200:8000")
ADAPTER_SECRET = os.environ.get("ADAPTER_SECRET", "my-secret-key-12345")

def adapter_get(path):
    req = urllib.request.Request(ADAPTER_URL + path, headers={"x-token": ADAPTER_SECRET})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def loop():
    log.info("Brain started. Adapter=" + ADAPTER_URL + " interval=" + str(LOOP_INTERVAL) + "s")
    n = 0
    while True:
        n += 1
        try:
            tick = adapter_get("/tick/XAUUSD")
            acct = adapter_get("/account")
            pos = adapter_get("/positions")
            msg = "tick " + str(n) + ": XAUUSD bid=" + str(tick.get("bid")) + " ask=" + str(tick.get("ask")) + " equity=" + str(acct.get("equity")) + " positions=" + str(len(pos))
            log.info(msg)
        except Exception as e:
            log.error("tick " + str(n) + " error: " + type(e).__name__ + ": " + str(e))
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    loop()
