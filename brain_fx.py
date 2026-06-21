"""Live trading brain — MULTI-INSTRUMENT, single-decision loop + watchdog.

Architecture (maps to the L1-L6 design):
  - MAIN THREAD : per-loop, per-instrument decision cycle (L1 perceive ->
                  L2 signal -> L3 legalize -> L4 pre-trade risk -> L5 execute).
  - WATCHDOG THREAD : independent, runs faster than the main loop. Its ONLY job
                  is survival: monitor account drawdown / margin and FORCE-
                  LIQUIDATE everything if a hard line is breached, bypassing the
                  main decision chain. This is L4's "circuit breaker" as an
                  independent process, per the design principle that risk control
                  must not depend on the signal chain being healthy.

Concurrency:
  Both threads can place orders (main opens, watchdog flattens). ALL order
  placement goes through ORDER_LOCK so the two threads never send simultaneously.
  The watchdog always wins: once it trips, a global HALT flag stops the main
  thread from opening anything new until manually/After reset.

Truth model (unchanged, critical):
  - Broker /positions is the SINGLE SOURCE OF TRUTH for what we hold.
  - Strategy metadata (entry_z per symbol) persisted to /data, fail-safe on loss.

Risk layer (the full five, now meaningful because we run MULTIPLE instruments):
  1. leverage cap (portfolio gross notional / equity, with safety buffer)
  2. single-instrument concentration (soft cap on any one symbol's share)
  3. net-direction exposure (soft cap on net directional / gross)
  4. liquidation buffer (survive N bars of k-sigma adverse move)
  5. order legalization (lots step/min, stop side + stops_level distance)
  PLUS portfolio total-notional cap and account circuit breaker (watchdog).

Env vars (key ones):
  SYMBOLS            comma list, default "EURUSD,GBPUSD,USDJPY"
  PER_SYMBOL_TF      default M5
  LOOP_INTERVAL      default 60   (main decision cadence, seconds)
  WATCHDOG_INTERVAL  default 10   (watchdog cadence, seconds — faster)
  CB_DD_HALT         default 0.08 (account drawdown that trips circuit breaker)
  PORT_NOTIONAL_CAP  default 10.0 (max gross notional / equity, hard)
  (full list in code below)
"""
import os
import sys
import json
import time
import threading
import logging
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")          # 自动处理 BST/GMT 夏令时
except Exception:
    _LONDON = None

import requests

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:
    HAVE_NUMPY = False

# ── 合并进来的四个模块 (原 brain.py 路线, 现接入唯一入口 brain_fx) ──
from allocator import check_budget, RISK_BUDGET, FX_SYMBOLS
from risk_account_gate import account_gate, _PosView, ACCOUNT_MARGIN_GATE, ACCOUNT_LEVERAGE_GATE
from risk_sharpe_layer import (
    EquityWindow, vol_target_scale, check_spike, in_cooldown, sharpe_obs_ok,
    COOLDOWN_AFTER_STOP_BARS, VOL_TARGET_15MIN,
)

# ── L6: Logfire (失败则降级到标准 logging, 不阻断交易) ──
try:
    import logfire
    logfire.configure()          # 读 LOGFIRE_TOKEN 环境变量
    _HAS_LOGFIRE = True
except Exception:
    _HAS_LOGFIRE = False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ADAPTER_URL = os.environ.get("ADAPTER_URL", "http://173.212.223.200:8000").rstrip("/")
ADAPTER_SECRET = os.environ.get("ADAPTER_SECRET", "my-secret-key-12345")

SYMBOLS = [s.strip().upper() for s in
           os.environ.get("SYMBOLS", "EURUSD,GBPUSD,USDJPY,XAGUSD").split(",") if s.strip()]
PER_SYMBOL_TF = os.environ.get("PER_SYMBOL_TF", "M5")
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "60"))
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "10"))
STATE_DIR = os.environ.get("STATE_DIR", "/data")

# strategy (alpha) params — DEFAULTS (used as the FX baseline).
# NOTE: XAG is a DIFFERENT strategy with different params (wider window, lower
# entry threshold, non-zero exit). Per-symbol overrides live in Z_PARAMS below;
# these globals remain the FX default so existing FX behavior is byte-identical.
Z_WINDOW = int(os.environ.get("Z_WINDOW", "50"))
Z_ENTRY = float(os.environ.get("Z_ENTRY", "2.5"))
Z_EXIT = float(os.environ.get("Z_EXIT", "0.0"))
MAX_HOLDING_BARS = int(os.environ.get("MAX_HOLDING_BARS", "20"))
STOP_LOSS_SIGMA = float(os.environ.get("STOP_LOSS_SIGMA", "5.0"))
USE_LOG_PRICE = os.environ.get("USE_LOG_PRICE", "1") == "1"

# ── PER-SYMBOL strategy params (新增) ──
# FX legs use the validated strategy_fx params (zw=50, ze=2.5, exit=0, log px).
# XAG uses its OWN validated params (zw=60, ze=2.0, exit=0.6) — a SEPARATE
# strategy, NOT the FX one with a different number. Do not "harmonize" them:
# each was tuned/validated on its own series.
#
# entry/exit semantics PER STRATEGY:
#   FX  : exit when z crosses back through 0 (z_exit=0.0)
#   XAG : exit when |z| < z_exit (mean-revert close band, z_exit=0.6)
# This |z|-band vs zero-cross difference is handled in _decide_one (见下).
class ZP:
    __slots__ = ("zw", "ze", "z_exit", "max_hold", "stop_sigma",
                 "use_log", "exit_mode", "tf")

    def __init__(self, zw, ze, z_exit, max_hold, stop_sigma, use_log, exit_mode,
                 tf):
        self.zw = zw; self.ze = ze; self.z_exit = z_exit
        self.max_hold = max_hold; self.stop_sigma = stop_sigma
        self.use_log = use_log; self.exit_mode = exit_mode
        self.tf = tf                                  # per-symbol timeframe (M1/M5/...)

# exit_mode: "zero_cross" (FX) | "abs_band" (XAG)
# tf: FX 用 M5 (strategy_fx 验证周期); XAG 用 M1 (zw=60 根1min ≈ 1小时窗口).
_FX_ZP = ZP(Z_WINDOW, Z_ENTRY, Z_EXIT, MAX_HOLDING_BARS, STOP_LOSS_SIGMA,
            USE_LOG_PRICE, "zero_cross", tf=os.environ.get("PER_SYMBOL_TF", "M5"))
Z_PARAMS = {
    "EURUSD": _FX_ZP,
    "GBPUSD": _FX_ZP,
    "USDJPY": _FX_ZP,
    # XAG: 独立策略, 独立参数 + 独立周期. zw=60 根 M1 ≈ 1h, ze=2.0, |z|<0.6 平仓.
    "XAGUSD": ZP(zw=60, ze=2.0, z_exit=0.6, max_hold=20, stop_sigma=5.0,
                 use_log=True, exit_mode="abs_band",
                 tf=os.environ.get("XAG_TF", "M1")),
}

def zp(symbol):
    return Z_PARAMS.get(symbol, _FX_ZP)

# risk (survival) params
RISK_PER_TRADE = float(os.environ.get("RISK_PER_TRADE", "0.01"))
MAX_LEVERAGE = float(os.environ.get("MAX_LEVERAGE", "5.0"))
MAX_LOTS = float(os.environ.get("MAX_LOTS", "5.0"))

# competition hard limits
LEVERAGE_HARD_CAP = float(os.environ.get("LEVERAGE_HARD_CAP", "30.0"))
LEVERAGE_SAFETY_BUFFER = float(os.environ.get("LEVERAGE_SAFETY_BUFFER", "0.20"))
SINGLE_INST_SOFT = float(os.environ.get("SINGLE_INST_SOFT", "0.80"))
NET_DIR_SOFT = float(os.environ.get("NET_DIR_SOFT", "0.85"))
LIQ_BUFFER_BARS = int(os.environ.get("LIQ_BUFFER_BARS", "5"))
LIQ_SIGMA_BAR = float(os.environ.get("LIQ_SIGMA_BAR", "0.001"))

# portfolio-level caps + circuit breaker
PORT_NOTIONAL_CAP = float(os.environ.get("PORT_NOTIONAL_CAP", "10.0"))  # gross/equity hard
CB_DD_HALT = float(os.environ.get("CB_DD_HALT", "0.08"))   # account DD that trips CB
CB_DD_SCALE = float(os.environ.get("CB_DD_SCALE", "0.05"))  # DD that halves new size

# ── 轮次边界 (新增): 每日伦敦时间此小时为主办方 review / 轮次重置点 ──
# 10pm 伦敦 = 22. 用 Europe/London 时区, 自动处理 BST(夏令时 UTC+1)/GMT.
ROUND_RESET_HOUR_LONDON = int(os.environ.get("ROUND_RESET_HOUR_LONDON", "22"))

# per-symbol instrument registry (contract size, point, stops level, vol min/step)
# JSON env override: INSTRUMENT_SPECS='{"EURUSD":{"contract":100000,...}}'
DEFAULT_SPECS = {
    "EURUSD": {"contract": 100000, "point": 0.00001, "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
    "GBPUSD": {"contract": 100000, "point": 0.00001, "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
    "USDJPY": {"contract": 100000, "point": 0.001,   "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
    "USDCHF": {"contract": 100000, "point": 0.00001, "stops_level": 40, "vmin": 0.01, "vstep": 0.01},
    "USDCAD": {"contract": 100000, "point": 0.00001, "stops_level": 40, "vmin": 0.01, "vstep": 0.01},
    "EURCHF": {"contract": 100000, "point": 0.00001, "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
    "EURGBP": {"contract": 100000, "point": 0.00001, "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
    "XAUUSD": {"contract": 100,    "point": 0.01,    "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
    "XAGUSD": {"contract": 5000,   "point": 0.001,   "stops_level": 30, "vmin": 0.01, "vstep": 0.01},
}
try:
    _override = os.environ.get("INSTRUMENT_SPECS")
    SPECS = dict(DEFAULT_SPECS)
    if _override:
        SPECS.update(json.loads(_override))
except Exception:
    SPECS = dict(DEFAULT_SPECS)

STATE_FILE = os.path.join(STATE_DIR, "brain_state_fx.json")

# ── USD 方向去重 (新增) ──
# 三个 FX 腿 (EURUSD/GBPUSD/USDJPY) 都含 USD. 单品种集中度 + 净方向看不到
# "三腿合起来是同一个 USD 单边赌注". 这里按 USD 方向归并: 同向过度 → 缩这一单.
# role: 买该 symbol 时对 USD 的方向. EURUSD/GBPUSD 买=空USD(-1); USDJPY 买=多USD(+1).
USD_ROLE = {"EURUSD": -1, "GBPUSD": -1, "USDJPY": +1}
MAX_FX_USD_CONCENTRATION = float(os.environ.get("MAX_FX_USD_CONCENTRATION", "0.70"))


def fx_usd_dedup_shrink(symbol, side, new_notional, positions_now):
    """这单是 FX 腿时, 把它和已持有的 FX 腿合起来算 USD 同向集中度.
    若加上这单后 |净USD|/总USD > 阈值 → 把这单缩到刚好落回阈值.
    返回 (allowed_notional, reason). 非 FX 腿原样放行.
    """
    if symbol not in USD_ROLE:
        return new_notional, None
    # 已有 FX 腿的 USD 方向暴露
    net = 0.0
    gross = 0.0
    for s, (sd, l, pr) in positions_now.items():
        if s not in USD_ROLE:
            continue
        usd_dir = USD_ROLE[s] * sd            # sd: +1多/-1空 该 symbol
        nv = notional_of(s, l, pr)
        net += usd_dir * nv
        gross += nv
    # ── 首腿豁免 (与 _check_exposure_after 同源的修复) ──
    # 组合此前没有任何 FX 腿 (gross==0) 时, 这单是当前美元方向的第一笔.
    # 单腿自己的美元集中度数学上必然 100% (它就是全部 FX 敞口), 那不是"过度
    # 单边", 是建仓必然. 不豁免的话每个美元方向的第一个 FX 单永远被缩到 0,
    # 静默失败 → FX 三条腿全废. 只有已有 FX 持仓时才校验同向集中度.
    if gross <= 1e-9:
        return new_notional, None

    # 叠加这单
    this_usd_dir = USD_ROLE[symbol] * side
    net_after = net + this_usd_dir * new_notional
    gross_after = gross + new_notional
    if gross_after <= 1e-9:
        return new_notional, None
    conc = abs(net_after) / gross_after
    if conc <= MAX_FX_USD_CONCENTRATION:
        return new_notional, None
    # 同向过度: 解出这单最大可加名义 x, 使 |net + dir*x| / (gross + x) = thresh.
    # 仅当这单与现有净同向才需要缩 (反向单反而降集中度, 不缩).
    if (net >= 0 and this_usd_dir > 0) or (net <= 0 and this_usd_dir < 0):
        thr = MAX_FX_USD_CONCENTRATION
        # (|net| + x) / (gross + x) = thr  →  x = (thr*gross - |net|) / (1 - thr)
        denom = (1.0 - thr)
        if denom <= 1e-9:
            return 0.0, "fx_usd_dedup: 阈值=1 退化"
        x = (thr * gross - abs(net)) / denom
        x = max(0.0, x)
        return x, ("fx_usd_dedup: USD同向集中度 " + format(conc, ".0%")
                   + " > " + format(thr, ".0%") + " → 缩这单名义至 " + format(x, ".0f"))
    return new_notional, None

MT5_TYPE_BUY = 0
MT5_TYPE_SELL = 1

# new-bar detection: last processed bar timestamp per symbol.
# Decouples POLLING cadence (LOOP_INTERVAL, fast, for responsiveness) from
# DECISION cadence (once per fresh bar, for signal consistency with research,
# which decided on bar closes). The signal only changes when a new bar closes;
# evaluating mid-bar would use an unclosed price and diverge from the backtest.
LAST_BAR_TIME = {}

# ── 新增状态轨道 (sharpe_layer + cooldown 需要, 原 brain_fx 没有) ──
# 1) 15min 权益采样环: sharpe_layer 的 vol_target / spike 都基于它.
#    main loop 每 SHARPE_SAMPLE_SEC 秒采一次 equity, 只保留最近 N 个.
EQUITY_15MIN = []                  # 升序, [-1]=最新; check_spike / sharpe_obs_ok 用
LAST_EQ_SAMPLE_TS = [0.0]         # list 以便闭包内可改 (上次采样的 epoch 秒)
SHARPE_SAMPLE_SEC = int(os.environ.get("SHARPE_SAMPLE_SEC", "900"))  # 900s=15min
EQUITY_RING_MAX = int(os.environ.get("EQUITY_RING_MAX", "96"))       # 96*15min=24h
# 2) cooldown 用: 每 symbol 上次止损发生在第几根 bar + 全局 bar 计数器.
LAST_STOP_BAR = {}                # {symbol: bar_index}
GLOBAL_BAR_COUNTER = [0]          # list 以便闭包内可改; 每个新 bar +1

# vol_target 需要"已实现 15min 波动" → 从 EQUITY_15MIN 现算
def _realized_15min_vol():
    if len(EQUITY_15MIN) < 2:
        return 0.0
    rets = [(EQUITY_15MIN[i] - EQUITY_15MIN[i-1]) / EQUITY_15MIN[i-1]
            for i in range(1, len(EQUITY_15MIN)) if EQUITY_15MIN[i-1] > 0]
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return var ** 0.5

def _eq_window():
    """打包成 sharpe_layer 要的 EquityWindow。"""
    return EquityWindow(
        equity_15min=list(EQUITY_15MIN),
        last_stop_bar=dict(LAST_STOP_BAR),
        current_bar=GLOBAL_BAR_COUNTER[0],
    )

def maybe_sample_equity(equity):
    """main loop 调: 到点就把当前 equity 压进 15min 环。"""
    now = time.time()
    if now - LAST_EQ_SAMPLE_TS[0] >= SHARPE_SAMPLE_SEC:
        EQUITY_15MIN.append(equity)
        if len(EQUITY_15MIN) > EQUITY_RING_MAX:
            del EQUITY_15MIN[0]
        LAST_EQ_SAMPLE_TS[0] = now

def _llog(event, **kw):
    """L6 统一日志: 有 logfire 走 logfire, 否则降级 log.info。Tech 奖审计链。"""
    if _HAS_LOGFIRE:
        logfire.info(event, **kw)
    else:
        log.info(event + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

# --- concurrency primitives --- #
ORDER_LOCK = threading.Lock()     # serialize all order placement
HALT = threading.Event()          # set by watchdog -> main stops opening new
STATE_LOCK = threading.Lock()     # protect state file read/write


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("brain")


# --------------------------------------------------------------------------- #
# Adapter HTTP
# --------------------------------------------------------------------------- #
def _headers():
    return {"x-token": ADAPTER_SECRET}


def get_account():
    r = requests.get(ADAPTER_URL + "/account", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def order_calc_margin(symbol, side, lots, price):
    """本 server margin_initial 返回 0.0 不可信 → 必须用 adapter 的 calc_margin.
    side: 'buy'/'sell'. 失败返回 None → account_gate 据此保守拒单 (算不出就不放行)。"""
    try:
        r = requests.get(ADAPTER_URL + "/calc_margin", headers=_headers(),
                         params={"symbol": symbol, "side": side,
                                 "lots": round(lots, 2), "price": price}, timeout=10)
        r.raise_for_status()
        return float(r.json()["margin"])
    except Exception as e:
        log.error("order_calc_margin " + symbol + " failed: " + str(e))
        return None


def get_rates(symbol, timeframe, count):
    r = requests.get(ADAPTER_URL + "/rates/" + symbol, headers=_headers(),
                     params={"timeframe": timeframe, "count": count}, timeout=15)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(ADAPTER_URL + "/positions", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def send_order(symbol, side, lots, sl, tp=0.0):
    body = {"symbol": symbol, "side": side, "lots": round(lots, 2),
            "sl": float(sl), "tp": float(tp)}
    with ORDER_LOCK:                       # never two orders at once
        r = requests.post(ADAPTER_URL + "/order", headers=_headers(),
                          json=body, timeout=20)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# State persistence (per-symbol strategy metadata)
# --------------------------------------------------------------------------- #
def load_state():
    with STATE_LOCK:
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}


def save_state(state):
    with STATE_LOCK:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.error("persist failed: " + str(e))


# --------------------------------------------------------------------------- #
# Instrument helpers
# --------------------------------------------------------------------------- #
def spec(symbol):
    return SPECS.get(symbol, {"contract": 100000, "point": 0.00001,
                              "stops_level": 30, "vmin": 0.01, "vstep": 0.01})


def is_usd_base(symbol):
    """True if USD is the BASE currency of the pair (USDJPY, USDCHF, USDCAD).
    For these, 1 lot = `contract` USD and the price is in the quote currency
    (JPY/CHF/CAD per USD). PnL of `state * lots * contract * Δprice` is then
    in QUOTE currency, not USD — must divide by price to get USD.
    Cross rates with no USD leg (EURJPY, EURGBP) are not supported here and
    return False (legacy USD-quote behavior)."""
    s = symbol.upper()
    return len(s) == 6 and s[:3] == "USD"


def notional_of(symbol, lots, price):
    """USD notional of a position — common currency for cross-instrument
    concentration / net-direction math.

    - USD-quote pairs (EURUSD, GBPUSD, XAUUSD, ...): notional_USD = lots * contract * price
      (price is USD per unit of base, so contract[base] * price = USD)
    - USD-base pairs (USDJPY, USDCHF, USDCAD): 1 lot = contract USD regardless of
      price; notional_USD = lots * contract (price is in the quote currency, e.g.
      JPY per USD, and is irrelevant for USD notional)
    - Cross rates with no USD leg (EURJPY, EURGBP, EURCHF): need external USD
      conversion, currently unsupported -> falls back to USD-quote formula
      (legacy behavior). Do not rely on cross-rate notional math.
    """
    sp = spec(symbol)
    if is_usd_base(symbol):
        return abs(lots) * sp["contract"]
    return abs(lots) * sp["contract"] * price


# --------------------------------------------------------------------------- #
# Signal: rolling z-score for the latest bar (look-ahead-safe)
# --------------------------------------------------------------------------- #
def compute_current_z(closes, p=None):
    """Look-ahead-safe rolling z for the latest bar.
    p: a ZP per-symbol param object; falls back to FX default if None."""
    if p is None:
        p = _FX_ZP
    if len(closes) < p.zw + 1:
        return None, None
    arr = np.asarray(closes, dtype=float)
    if p.use_log:
        arr = np.log(arr)
    current = arr[-1]
    window = arr[-(p.zw + 1):-1]
    mean_prev = window.mean()
    std_prev = window.std(ddof=0)
    if std_prev <= 0 or np.isnan(std_prev):
        return None, None
    return float((current - mean_prev) / std_prev), float(std_prev)


# --------------------------------------------------------------------------- #
# Order legalization
# --------------------------------------------------------------------------- #
def round_to_step(symbol, lots):
    sp = spec(symbol)
    vmin, vstep = sp["vmin"], sp["vstep"]
    if lots < vmin:
        return 0.0
    steps = int(lots / vstep)
    out = round(steps * vstep, 2)
    return out if out >= vmin else 0.0


def legalize_stop(symbol, entry_price, raw_stop_price, side):
    min_dist = spec(symbol)["stops_level"] * spec(symbol)["point"]
    if side == 1:
        sl = min(raw_stop_price, entry_price - min_dist)
        if sl >= entry_price:
            sl = entry_price - min_dist
    else:
        sl = max(raw_stop_price, entry_price + min_dist)
        if sl <= entry_price:
            sl = entry_price + min_dist
    return float(sl)


# --------------------------------------------------------------------------- #
# Sizing (per-symbol fixed_risk + per-symbol leverage cap)
# --------------------------------------------------------------------------- #
def size_order(symbol, equity, entry_price, stop_price):
    sp = spec(symbol)
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0:
        return 0.0
    usd_is_base = is_usd_base(symbol)
    # notional per lot, USD (see notional_of for the USD-base/USD-quote split)
    notional_per_lot = sp["contract"] if usd_is_base else sp["contract"] * entry_price
    # loss per lot if stop is hit, USD. Loss is in QUOTE currency by default;
    # for USD-base pairs we must convert quote->USD by dividing by entry_price.
    if usd_is_base:
        loss_per_lot = sp["contract"] * stop_dist / entry_price
    else:
        loss_per_lot = sp["contract"] * stop_dist
    if loss_per_lot <= 0:
        return 0.0
    lots_risk = (RISK_PER_TRADE * equity) / loss_per_lot
    use_cap = min(MAX_LEVERAGE, LEVERAGE_HARD_CAP * (1.0 - LEVERAGE_SAFETY_BUFFER))
    lots_lev = (use_cap * equity) / notional_per_lot if notional_per_lot > 0 else 0.0
    return round_to_step(symbol, min(lots_risk, lots_lev, MAX_LOTS))


# --------------------------------------------------------------------------- #
# Pre-trade risk check (the full five, portfolio-aware, in NOTIONAL space)
#   positions_now: dict symbol -> (side, lots, price)  [broker truth]
# Returns approved_lots (<= requested), shrinking to fit every constraint.
# --------------------------------------------------------------------------- #
def pre_trade_risk_check(symbol, side, requested_lots, entry_price, positions_now, equity):
    if requested_lots <= 0 or entry_price <= 0 or equity <= 0:
        return 0.0, ["invalid inputs"]
    rej = []
    sp = spec(symbol)
    new_notional = requested_lots * sp["contract"] * entry_price

    def gross_existing():
        return sum(notional_of(s, l, p) for (s, (sd, l, p)) in positions_now.items())

    # ---- 1. portfolio leverage cap (with buffer) ---- #
    existing_gross = gross_existing()
    lev_cap = LEVERAGE_HARD_CAP * (1.0 - LEVERAGE_SAFETY_BUFFER)
    if (existing_gross + new_notional) / equity > lev_cap:
        max_new = max(0.0, lev_cap * equity - existing_gross)
        requested_lots = max(0.0, max_new / (sp["contract"] * entry_price))
        rej.append("leverage cap -> shrink")
        if requested_lots <= 0:
            return 0.0, rej + ["zero after leverage"]
        new_notional = requested_lots * sp["contract"] * entry_price

    # ---- portfolio TOTAL notional cap (hard) ---- #
    if (existing_gross + new_notional) / equity > PORT_NOTIONAL_CAP:
        max_new = max(0.0, PORT_NOTIONAL_CAP * equity - existing_gross)
        requested_lots = max(0.0, max_new / (sp["contract"] * entry_price))
        rej.append("portfolio notional cap -> shrink")
        if requested_lots <= 0:
            return 0.0, rej + ["zero after port cap"]
        new_notional = requested_lots * sp["contract"] * entry_price

    # ---- 2. single-instrument concentration (only with >=1 existing pos) ---- #
    if len(positions_now) >= 1:
        existing_this = notional_of(symbol, *( (positions_now[symbol][1], positions_now[symbol][2])
                                               if symbol in positions_now else (0, 0) ))
        existing_gross = gross_existing()
        prop_this = existing_this + new_notional
        prop_gross = existing_gross + new_notional
        frac = prop_this / prop_gross if prop_gross > 0 else 0.0
        if frac > SINGLE_INST_SOFT and (1.0 - SINGLE_INST_SOFT) > 0:
            max_new = (SINGLE_INST_SOFT * existing_gross - existing_this) / (1.0 - SINGLE_INST_SOFT)
            max_new = max(0.0, max_new)
            requested_lots = min(requested_lots, max_new / (sp["contract"] * entry_price))
            rej.append("concentration " + format(frac, ".0%") + " -> shrink")
            if requested_lots <= 0:
                return 0.0, rej + ["zero after concentration"]
            new_notional = requested_lots * sp["contract"] * entry_price

    # ---- 3. net-direction exposure ---- #
    if len(positions_now) >= 1:
        net_signed = sum(sd * notional_of(s, l, p) for (s, (sd, l, p)) in positions_now.items())
        net_signed_after = net_signed + side * new_notional
        gross_after = gross_existing() + new_notional
        frac = abs(net_signed_after) / gross_after if gross_after > 0 else 0.0
        if frac > NET_DIR_SOFT:
            sign_existing = 1 if net_signed >= 0 else -1
            if sign_existing == side and (1.0 - NET_DIR_SOFT) > 0:
                existing_gr = gross_existing()
                max_new = (NET_DIR_SOFT * existing_gr - abs(net_signed)) / (1.0 - NET_DIR_SOFT)
                max_new = max(0.0, max_new)
                requested_lots = min(requested_lots, max_new / (sp["contract"] * entry_price))
                rej.append("net-dir " + format(frac, ".0%") + " -> shrink")
                if requested_lots <= 0:
                    return 0.0, rej + ["zero after net-dir"]
                new_notional = requested_lots * sp["contract"] * entry_price

    # ---- 4. liquidation buffer ---- #
    total_adverse = LIQ_BUFFER_BARS * STOP_LOSS_SIGMA * LIQ_SIGMA_BAR
    worst_loss = sum(notional_of(s, l, p) for (s, (sd, l, p)) in positions_now.items()) * total_adverse
    worst_loss += new_notional * total_adverse
    if equity - worst_loss <= 0:
        budget = equity - 0.05 * equity
        ratio = budget / max(worst_loss, 1e-9)
        requested_lots = round_to_step(symbol, requested_lots * max(0.0, ratio))
        rej.append("liq buffer -> shrink " + format(ratio, ".2f"))
        if requested_lots <= 0:
            return 0.0, rej + ["zero after liq buffer"]
        new_notional = requested_lots * sp["contract"] * entry_price

    # ═══════════════════════════════════════════════════════════════════
    # 合并模块: cooldown(拦) → allocator(拦) → vol_target(缩) → spike(缩)
    #          → account_gate(最后拦, 用终size算margin)
    # 顺序铁律: account_gate 必须在所有 shrink 之后 (要用缩完的 size 算 margin)。
    # ═══════════════════════════════════════════════════════════════════
    eqw = _eq_window()

    # ── 5a. cooldown (止损后冷静期, 拦截; 仅开仓方向有意义) ──
    cd, cd_why = in_cooldown(symbol, eqw)
    if cd:
        return 0.0, rej + [cd_why]

    # ── 5b. allocator 预算硬切 (60/40, 拦截) ──
    # 该腿当前 risk ≈ 名义 * 单bar不利 (粗口径, 与 sharpe_layer 同源).
    leg_current_notional = notional_of(symbol, *(positions_now[symbol][1:] if symbol in positions_now else (0, 0)))
    leg_current_risk = leg_current_notional * STOP_LOSS_SIGMA * LIQ_SIGMA_BAR
    new_trade_risk = new_notional * STOP_LOSS_SIGMA * LIQ_SIGMA_BAR
    bc = check_budget(symbol, leg_current_risk, new_trade_risk, equity)
    if not bc.allowed:
        return 0.0, rej + [bc.reason]

    # ── 5c. vol_target (按已实现15min波动缩, 服务 Best Sharpe) ──
    vscale, v_why = vol_target_scale(new_notional, equity, _realized_15min_vol())
    if vscale < 1.0:
        requested_lots = round_to_step(symbol, requested_lots * vscale)
        rej.append(v_why)
        if requested_lots <= 0:
            return 0.0, rej + ["zero after vol_target"]
        new_notional = requested_lots * sp["contract"] * entry_price

    # ── 5d. spike 降险 (上个15min权益剧烈变动 → 本轮所有腿减半) ──
    spike, sp_why = check_spike(eqw)
    if spike:
        requested_lots = round_to_step(symbol, requested_lots * 0.5)
        rej.append(sp_why + " -> halve")
        if requested_lots <= 0:
            return 0.0, rej + ["zero after spike"]
        new_notional = requested_lots * sp["contract"] * entry_price

    # ── 6. account_gate (账户级总保证金/总杠杆, 最后一道, 用终size算margin) ──
    side_str = "buy" if side == 1 else "sell"
    new_margin = order_calc_margin(symbol, side_str, requested_lots, entry_price)
    if new_margin is None:
        return 0.0, rej + ["account_gate: calc_margin 失败 → 保守拒"]
    open_views = []
    for (s, (sd, l, p)) in positions_now.items():
        m = order_calc_margin(s, "buy" if sd == 1 else "sell", l, p)
        if m is None:
            return 0.0, rej + ["account_gate: 持仓 " + s + " margin 算不出 → 保守拒"]
        open_views.append(_PosView(s, notional_of(s, l, p), m))
    ok, ag_why = account_gate(new_notional, new_margin, equity, open_views)
    if not ok:
        return 0.0, rej + [ag_why]
    rej.append(ag_why)

    return round_to_step(symbol, requested_lots), rej


# --------------------------------------------------------------------------- #
# Reconcile broker positions -> dict symbol -> (side, lots, price)
# --------------------------------------------------------------------------- #
def reconcile_positions(positions):
    out = {}
    for p in positions:
        sym = p.get("symbol")
        if sym not in SYMBOLS:
            continue
        side = 1 if int(p.get("type", 0)) == MT5_TYPE_BUY else -1
        out[sym] = (side, float(p.get("volume", 0.0)), float(p.get("price_open", 0.0)),
                    p.get("ticket"))
    # normalize to (side, lots, price) for risk math; keep ticket separately
    pos_math = {s: (v[0], v[1], v[2]) for s, v in out.items()}
    tickets = {s: v[3] for s, v in out.items()}
    return pos_math, tickets


# --------------------------------------------------------------------------- #
# Flatten a single symbol (used by both main exits and watchdog)
# --------------------------------------------------------------------------- #
def flatten_symbol(symbol, side, lots):
    close_side = "sell" if side == 1 else "buy"
    try:
        res = send_order(symbol, close_side, lots, 0.0, 0.0)
        log.info("flatten " + symbol + " " + close_side + " " + format(lots, ".2f")
                 + " retcode=" + str(res.get("retcode")))
        return True
    except Exception as e:
        log.error("flatten " + symbol + " failed: " + str(e))
        return False


# --------------------------------------------------------------------------- #
# WATCHDOG THREAD — independent circuit breaker
# --------------------------------------------------------------------------- #
def _london_now():
    """当前伦敦时间. zoneinfo 不可用时退回 UTC (并告警, 因为 10pm 判定会偏)."""
    if _LONDON is not None:
        return datetime.now(_LONDON)
    return datetime.utcnow()


def _crossed_reset(prev_dt, now_dt):
    """从 prev_dt 到 now_dt 之间是否跨过了伦敦 ROUND_RESET_HOUR 那个整点.
    判定: 日期变了, 或同日内小时从 <reset 跨到 >=reset."""
    if prev_dt is None:
        return False
    if now_dt.date() != prev_dt.date():
        return True
    return prev_dt.hour < ROUND_RESET_HOUR_LONDON <= now_dt.hour


def watchdog():
    log.info("watchdog started, interval=" + str(WATCHDOG_INTERVAL) + "s, CB_DD_HALT="
             + str(CB_DD_HALT) + ", round_reset=" + str(ROUND_RESET_HOUR_LONDON)
             + "h London")
    peak_equity = None
    prev_dt = _london_now()
    while True:
        try:
            now_dt = _london_now()
            acct = get_account()
            equity = float(acct.get("equity", acct.get("balance", 0.0)))

            # ── 轮次边界: 跨伦敦 22:00 → 重置峰值为【本轮起始权益】 ──
            # 否则上一轮的高点会让新一轮开盘正常波动误触 CB_DD_HALT.
            if _crossed_reset(prev_dt, now_dt):
                log.warning("=== ROUND RESET (London " + str(ROUND_RESET_HOUR_LONDON)
                            + ":00) -> peak_equity reset " + format(equity, ".0f")
                            + " ; clearing HALT ===")
                peak_equity = equity
                HALT.clear()        # 新轮解除上一轮的熔断锁 (轮次独立重置)
            prev_dt = now_dt

            if peak_equity is None or equity > peak_equity:
                peak_equity = equity
            dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0

            margin_level = acct.get("margin_level")  # percent, may be None
            danger_margin = margin_level is not None and 0 < float(margin_level) < 150.0

            # ── margin-None 修正 (新增) ──
            # margin_level 查不到 ≠ 安全. 可能 adapter 断网 → watchdog 盲了.
            # 单次容忍 (字段偶发缺失); 连续多次查不到 → 保守 HALT 新开仓 + 告警.
            # ⚠ 这只是"察觉自己盲了"的软兜底. 真正断网强平必须靠 VPS adapter
            #   进程内的本地 watchdog (见 mt5_client 注释), brain 跨网络做不到.
            if margin_level is None:
                watchdog._none_streak = getattr(watchdog, "_none_streak", 0) + 1
                if watchdog._none_streak >= 3:
                    log.error("margin_level 连续 " + str(watchdog._none_streak)
                              + " 次查不到 → watchdog 可能盲了, 保守 HALT 新开仓 "
                              + "(真兜底需 VPS 本地 watchdog)")
                    HALT.set()
            else:
                watchdog._none_streak = 0

            if dd < -CB_DD_HALT or danger_margin:
                reason = ("account DD " + format(dd, ".1%") if dd < -CB_DD_HALT
                          else "margin level " + str(margin_level) + "%")
                log.warning("CIRCUIT BREAKER TRIPPED (" + reason + ") -> FORCE LIQUIDATE ALL")
                HALT.set()
                positions = get_positions()
                pos_math, _ = reconcile_positions(positions)
                for sym, (side, lots, price) in pos_math.items():
                    flatten_symbol(sym, side, lots)
                save_state({})  # clear strategy metadata after liquidation
            # else: leave HALT as-is (a tripped CB stays tripped until reset/restart)
        except requests.exceptions.RequestException as e:
            log.error("watchdog adapter error: " + str(e))
        except Exception as e:
            log.error("watchdog error: " + str(e))
        time.sleep(WATCHDOG_INTERVAL)


# --------------------------------------------------------------------------- #
# MAIN decision cycle — over all symbols
# --------------------------------------------------------------------------- #
def decide_and_act():
    acct = get_account()
    equity = float(acct.get("equity", acct.get("balance", 0.0)))
    trade_allowed = bool(acct.get("trade_allowed", False))

    maybe_sample_equity(equity)        # 喂 15min 权益环 (sharpe_layer 数据源)
    obs_ok, obs_why = sharpe_obs_ok(_eq_window())
    if not obs_ok:
        log.info(obs_why)              # 反向提醒: 观测不足 Sharpe Rank 封顶

    positions = get_positions()
    pos_math, tickets = reconcile_positions(positions)
    # 每 symbol 当前浮动盈亏 (平仓前快照, 用于 exit 事件记 realized_pnl 近似)
    profit_by_symbol = {}
    for _p in positions:
        _s = _p.get("symbol")
        if _s in SYMBOLS:
            profit_by_symbol[_s] = float(_p.get("profit", 0.0))
    state = load_state()

    if HALT.is_set():
        log.warning("HALT set by watchdog -> no new entries this cycle")

    for symbol in SYMBOLS:
        try:
            _decide_one(symbol, equity, trade_allowed, pos_math, tickets, state,
                        profit_by_symbol)
        except Exception as e:
            log.error("decide " + symbol + " error: " + str(e))


def _decide_one(symbol, equity, trade_allowed, pos_math, tickets, state,
                profit_by_symbol=None):
    profit_by_symbol = profit_by_symbol or {}
    p = zp(symbol)                                    # per-symbol z params + tf
    rates = get_rates(symbol, p.tf, max(p.zw + 5, 200))
    if not rates or len(rates) < p.zw + 1:
        return

    # ---- NEW-BAR DETECTION (decouple polling from decision) ---- #
    # Only act on signals when a fresh bar has closed. The latest element is the
    # most recent (possibly still-forming) bar; we key off its timestamp. If it
    # hasn't advanced since we last processed this symbol, the signal is
    # unchanged — skip the signal logic. (Watchdog still runs at its own cadence,
    # and the server-side stop-loss protects intra-bar, so survival is unaffected.)
    latest_bar_time = rates[-1].get("time")
    is_new_bar = LAST_BAR_TIME.get(symbol) != latest_bar_time

    closes = [float(b["close"]) for b in rates]
    last_close = closes[-1]
    z, std_prev = compute_current_z(closes, p)
    if z is None:
        return

    held = symbol in pos_math
    side = pos_math[symbol][0] if held else 0
    lots = pos_math[symbol][1] if held else 0.0
    meta = state.get(symbol)
    if meta and meta.get("ticket") not in (None, tickets.get(symbol)):
        meta = None  # stale

    log.info(symbol + " z=" + format(z, ".2f") + " px=" + format(last_close, ".5f")
             + " pos=" + ("none" if not held else
                          (("long" if side == 1 else "short") + " " + format(lots, ".2f")))
             + ("" if is_new_bar else " [same bar, observe only]"))

    if not trade_allowed:
        return  # pre-open: observe only

    # gate ALL signal-driven actions on a fresh bar
    if not is_new_bar:
        return

    # mark this bar as processed for this symbol (do it now so any early return
    # below still counts the bar as seen — we only decide once per bar)
    LAST_BAR_TIME[symbol] = latest_bar_time
    GLOBAL_BAR_COUNTER[0] += 1          # cooldown 用的全局 bar 计数

    # ---- manage existing ---- #
    if held:
        if meta is None:
            log.warning(symbol + " position w/o metadata -> FAIL-SAFE flatten")
            if flatten_symbol(symbol, side, lots):
                state.pop(symbol, None)
                save_state(state)
            return
        bars_held = meta.get("bars_held", 0) + 1
        entry_z = float(meta.get("entry_z", 0.0))
        reason = None
        # ── EXIT: per-strategy semantics ──
        #   FX  (zero_cross): close when z crosses back through 0
        #   XAG (abs_band)  : close when |z| < z_exit (reverted into the band)
        if p.exit_mode == "abs_band":
            if abs(z) < p.z_exit:
                reason = "revert"
        else:  # zero_cross
            if side == 1 and z >= p.z_exit:
                reason = "revert"
            elif side == -1 and z <= p.z_exit:
                reason = "revert"
        # stop (z moved stop_sigma against entry) — same shape both strategies
        if reason is None:
            if side == 1 and z <= entry_z - p.stop_sigma:
                reason = "stop"
            elif side == -1 and z >= entry_z + p.stop_sigma:
                reason = "stop"
            elif bars_held >= p.max_hold:
                reason = "max_hold"
        if reason is not None:
            log.info(symbol + " exit (" + reason + ")")
            if flatten_symbol(symbol, side, lots):
                if reason == "stop":
                    LAST_STOP_BAR[symbol] = GLOBAL_BAR_COUNTER[0]   # cooldown 起算
                _llog("exit", symbol=symbol, reason=reason,
                      entry_z=round(entry_z, 2), exit_z=round(z, 2),
                      bars_held=bars_held, pnl=round(profit_by_symbol.get(symbol, 0.0), 2),
                      bar=GLOBAL_BAR_COUNTER[0])
                state.pop(symbol, None)
                save_state(state)
        else:
            meta["bars_held"] = bars_held
            state[symbol] = meta
            save_state(state)
        return

    # ---- flat: look for entry (blocked if HALT) ---- #
    if HALT.is_set():
        return
    entry_side = 0
    if z <= -p.ze:
        entry_side = 1
    elif z >= p.ze:
        entry_side = -1
    if entry_side == 0:
        return

    if p.use_log:
        log_stop = np.log(last_close) - entry_side * p.stop_sigma * std_prev
        raw_stop = float(np.exp(log_stop))
    else:
        raw_stop = last_close - entry_side * p.stop_sigma * std_prev
    sl_price = legalize_stop(symbol, last_close, raw_stop, entry_side)

    lots_want = size_order(symbol, equity, last_close, sl_price)
    if lots_want <= 0:
        return
    # circuit-breaker soft scale (account in moderate DD -> halve)
    # (peak tracked in watchdog; here we use a cheap proxy via account margin if present)

    lots_ok, rej = pre_trade_risk_check(symbol, entry_side, lots_want, last_close,
                                        pos_math, equity)
    if lots_ok <= 0:
        log.info(symbol + " entry rejected: " + ("; ".join(rej) if rej else "size 0"))
        _llog("reject", symbol=symbol, z=round(z, 2),
              side=("buy" if entry_side == 1 else "sell"),
              gate="risk_check", reason=("; ".join(rej) if rej else "size 0"),
              bar=GLOBAL_BAR_COUNTER[0])
        return

    # ── USD 方向去重 (FX 腿才生效; XAG 不在 USD_ROLE, 原样通过) ──
    sp_d = spec(symbol)
    want_notional = lots_ok * sp_d["contract"] * last_close
    allowed_notional, dedup_reason = fx_usd_dedup_shrink(
        symbol, entry_side, want_notional, pos_math)
    if dedup_reason is not None:
        lots_ok = round_to_step(symbol, allowed_notional / (sp_d["contract"] * last_close))
        rej.append(dedup_reason)
        if lots_ok <= 0:
            log.info(symbol + " entry rejected after USD dedup")
            _llog("reject", symbol=symbol, z=round(z, 2),
                  side=("buy" if entry_side == 1 else "sell"),
                  gate="usd_dedup", reason=dedup_reason,
                  bar=GLOBAL_BAR_COUNTER[0])
            return

    side_str = "buy" if entry_side == 1 else "sell"
    log.info("ENTRY " + symbol + " " + side_str + " " + format(lots_ok, ".2f")
             + " sl=" + format(sl_price, ".5f") + " z=" + format(z, ".2f")
             + (" [" + "; ".join(rej) + "]" if rej else ""))
    try:
        res = send_order(symbol, side_str, lots_ok, sl_price, 0.0)
        log.info(symbol + " order retcode=" + str(res.get("retcode")))
        new_ticket = res.get("order") or res.get("deal")
        _llog("entry", symbol=symbol, side=side_str, lots=round(lots_ok, 2),
              sl=round(sl_price, 5), z=round(z, 2), retcode=res.get("retcode"),
              shrinks="; ".join(rej) if rej else "none")
        # reflect into local maps so subsequent symbols in this loop see the exposure
        pos_math[symbol] = (entry_side, lots_ok, last_close)
        tickets[symbol] = new_ticket
        state[symbol] = {"ticket": new_ticket, "entry_z": z, "entry_price": last_close,
                         "side": entry_side, "bars_held": 0, "sl": sl_price}
        save_state(state)
    except Exception as e:
        log.error(symbol + " order failed: " + str(e))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def loop():
    log.info("Brain started. symbols=" + ",".join(SYMBOLS) + " tf=" + PER_SYMBOL_TF
             + " loop=" + str(LOOP_INTERVAL) + "s wd=" + str(WATCHDOG_INTERVAL) + "s"
             + " max_lev=" + str(MAX_LEVERAGE) + " CB_DD_HALT=" + str(CB_DD_HALT))
    if not HAVE_NUMPY:
        log.error("numpy missing — cannot run")
        return
    t = threading.Thread(target=watchdog, name="watchdog", daemon=True)
    t.start()
    while True:
        try:
            decide_and_act()
        except requests.exceptions.RequestException as e:
            log.error("adapter error: " + str(e))
        except Exception as e:
            log.error("loop error: " + str(e))
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    loop()
