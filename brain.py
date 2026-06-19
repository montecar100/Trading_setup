{\rtf1\ansi\ansicpg1252\cocoartf2868
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 import time, os, logging, random\
from datetime import datetime\
\
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")\
log = logging.getLogger("brain")\
\
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "60"))  # \uc0\u31186 \
\
def l1_perceive():\
    """L1 """\
    return \{"symbol": "XAUUSD", "price": 2300 + random.uniform(-5, 5),\
            "equity": 1_000_000, "positions": []\}\
\
def l2_signal(state):\
    """L2 signal\'94\'94\'94\
    z = random.uniform(-3, 3)\
    if z < -2:   return \{"side": "buy", "z": z\}\
    if z > 2:    return \{"side": "sell", "z": z\}\
    return None\
\
def l3_validate(intent, state):\
    """L3"""\
    if not intent: return None\
    return \{"symbol": state["symbol"], "side": intent["side"], "lots": 0.1\}\
\
def l4_risk(order, state):\
    """L4 risk\'94\'94\'94\
    if not order: return None\
    order["sl"] = state["price"] * (0.99 if order["side"] == "buy" else 1.01)\
    return order\
\
def l5_execute_MOCK(order):\
    """L5 exec\'94\'94\'94\
    log.info(f"[MOCK ORDER] would send: \{order\}")\
\
def loop():\
    log.info(f"Brain started. Loop interval=\{LOOP_INTERVAL\}s")\
    tick = 0\
    while True:\
        tick += 1\
        try:\
            state = l1_perceive()\
            intent = l2_signal(state)\
            order = l3_validate(intent, state)\
            order = l4_risk(order, state)\
            if order:\
                l5_execute_MOCK(order)\
            else:\
                log.info(f"tick \{tick\}: no signal (price=\{state['price']:.2f\})")\
        except Exception as e:\
            log.exception(f"loop error: \{e\}")  \
        time.sleep(LOOP_INTERVAL)\
\
if __name__ == "__main__":\
    loop()}