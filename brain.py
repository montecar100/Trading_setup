import time, os, logging
import urllib.request, json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "60"))
ADAPTER_URL = os.environ.get("ADAPTER_URL", "http://173.212.223.200:8000")
ADAPTER_SECRET = os.environ.get("ADAPTER_SECRET", "my-secret-key-12345")

def adapter_get(path):
    """调适配器的 GET 接口"""
    req = urllib.request.Request(ADAPTER_URL + path,
                                 headers={"x-token": ADAPTER_SECRET})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def l1_perceive():
    """L1 感知: 连适配器拉真实数据"""
    tick = adapter_get("/tick/XAUUSD")
    account = adapter_get("/account")
    positions = adapter_get("/positions")
    return {"tick": tick, "account": account, "positions": positions}

def loop():
    log.info(f"Brain started. Adapter={ADAPTER_URL}, interval={LOOP_INTERVAL}s")
    tick = 0
    while True:
        tick += 1
        try:
            state = l1_perceive()
            t = state["tick"]
            acct = state["account"]
            log.info(f"tick

cd /Users/yimiaozhang/Desktop/trading_competition

cat > brain.py << 'PYEOF'
import time, os, logging
import urllib.request, json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "60"))
ADAPTER_URL = os.environ.get("ADAPTER_URL", "http://173.212.223.200:8000")
ADAPTER_SECRET = os.environ.get("ADAPTER_SECRET", "my-secret-key-12345")

def adapter_get(path):
    """调适配器的 GET 接口"""
    req = urllib.request.Request(ADAPTER_URL + path,
                                 headers={"x-token": ADAPTER_SECRET})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def l1_perceive():
    """L1 感知: 连适配器拉真实数据"""
    tick = adapter_get("/tick/XAUUSD")
    account = adapter_get("/account")
    positions = adapter_get("/positions")
    return {"tick": tick, "account": account, "positions": positions}

def loop():
    log.info(f"Brain started. Adapter={ADAPTER_URL}, interval={LOOP_INTERVAL}s")
    tick = 0
    while True:
        tick += 1
        try:
            state = l1_perceive()
            t = state["tick"]
            acct = state["account"]
            log.info(f"tick {tick}: XAUUSD bid={t.get('bid')} ask={t.get('ask')} "
                     f"| equity={acct.get('equity')} | positions={len(state['positions'])}")
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    loop()
