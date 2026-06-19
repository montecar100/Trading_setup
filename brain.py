import time, os, logging, random

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("brain")

LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "60"))

def l1_perceive():
    return {"symbol": "XAUUSD", "price": 2300 + random.uniform(-5, 5),
            "equity": 1_000_000, "positions": []}

def l2_signal(state):
    z = random.uniform(-3, 3)
    if z < -2:
        return {"side": "buy", "z": z}
    if z > 2:
        return {"side": "sell", "z": z}
    return None

def l3_validate(intent, state):
    if not intent:
        return None
    return {"symbol": state["symbol"], "side": intent["side"], "lots": 0.1}

def l4_risk(order, state):
    if not order:
        return None
    order["sl"] = state["price"] * (0.99 if order["side"] == "buy" else 1.01)
    return order

def l5_execute_MOCK(order):
    log.info(f"[MOCK ORDER] would send: {order}")

def loop():
    log.info(f"Brain started. Loop interval={LOOP_INTERVAL}s")
    tick = 0
    while True:
        tick += 1
        try:
            state = l1_perceive()
            intent = l2_signal(state)
            order = l3_validate(intent, state)
            order = l4_risk(order, state)
            if order:
                l5_execute_MOCK(order)
            else:
                log.info(f"tick {tick}: no signal (price={state['price']:.2f})")
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    loop()
