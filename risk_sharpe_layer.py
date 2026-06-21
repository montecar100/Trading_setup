"""
risk_sharpe_layer.py — 第三层风控: Sharpe 优化层 (这是"无 alpha"下的真正策略)

═══════════════════════════════════════════════════════════════════
设计前提 (诚实的战略判断)
═══════════════════════════════════════════════════════════════════
你 memory 两个已证实发现:
  - XAUUSD 合成市场 = random walk (VR≈1.0, 自相关≈0, half-life 446 bars)
  - FX 研究代码 README 自述 edge≈0
加上 XAU-XAG cointegration 测不出 → 手里【没有合成市场验证过的 alpha】.
XAG z-score 的 +22.9% 是【外部真实数据】回测, 合成市场大概率也是 random walk,
而均值回复需要负自相关才成立 → 信号在合成市场大概率失效.

结论: 这场比赛大概率【无真 alpha】. 这不是坏消息——
目标奖项 Best Sharpe + Best Tech 不需要 alpha, 需要【平滑权益曲线 + 工程质量】.

所以风控不再是护栏, 是【主体】. 比赛 Sharpe = mean(r_15min)/std(r_15min) 非年化,
奖励平滑 → 主动【压低权益波动】本身就是策略.

本层三条 rule 都服务一个目标: 让 std(r_15min) 尽量小.
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence, Optional
import math


# ─────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────
SHARPE_MIN_OBS = 8              # 比赛: Sharpe 有效观测 <8 → Rank 封顶 50 分 (须连续交易~2h)
VOL_TARGET_15MIN = 0.003        # 目标单 15min 权益收益率波动 (0.3%); 超了缩 size
MAX_SINGLE_15MIN_MOVE = 0.015   # 单个 15min 权益变动硬上限 (1.5%); 防单点尖刺毁 Sharpe
COOLDOWN_AFTER_STOP_BARS = 5    # 止损后冷静期: N 根 bar 内不重开同 symbol (防连环止损锯齿)


@dataclass(frozen=True)
class EquityWindow:
    """最近权益序列 (brain 每 15min 采样一次)。"""
    equity_15min: Sequence[float]   # 升序, [-1]=最新
    last_stop_bar: dict             # {symbol: 上次止损的 bar index}
    current_bar: int


# ─────────────────────────────────────────────────────────────────
# Rule 7 — 波动率目标 (vol targeting): 让每笔的潜在 15min 冲击 ~ 目标波动
# ─────────────────────────────────────────────────────────────────
def vol_target_scale(
    proposed_notional: float,
    equity: float,
    realized_15min_vol: float,
) -> tuple[float, str]:
    """
    根据当前已实现波动, 缩放仓位使预期权益波动逼近 VOL_TARGET_15MIN.
    realized_15min_vol 高 → 缩 size; 低 → 可放大(但不超其他闸门).
    返回 (缩放系数, 原因). 系数 <=1 表示要缩.
    """
    if realized_15min_vol <= 1e-9:
        return 1.0, "vol_target: 无波动历史, 不缩放"
    scale = VOL_TARGET_15MIN / realized_15min_vol
    scale = min(1.0, scale)   # 只缩不放 (放大交给 allocator 预算, 这里只防过波动)
    if scale < 1.0:
        return scale, f"vol_target: 实测波动{realized_15min_vol:.2%}>目标 → 缩至{scale:.0%}"
    return 1.0, "vol_target: 波动在目标内"


# ─────────────────────────────────────────────────────────────────
# Rule 8 — 单点尖刺熔断 (anti-spike): 任何 15min 权益变动 > 上限 → 强制降险
# ─────────────────────────────────────────────────────────────────
def check_spike(eq_window: EquityWindow) -> tuple[bool, str]:
    """
    单个 15min 内权益剧烈变动 (无论盈亏) 都会毁掉 std(r_15min).
    检测到尖刺 → 信号: 下一轮强制缩所有腿 size (由 brain 编排响应).
    返回 (是否触发降险, 原因).
    """
    eq = eq_window.equity_15min
    if len(eq) < 2:
        return False, "spike: 数据不足"
    r = (eq[-1] - eq[-2]) / eq[-2] if eq[-2] > 0 else 0.0
    if abs(r) > MAX_SINGLE_15MIN_MOVE:
        return True, f"spike DETECT: 上个15min权益变动{r:+.1%} > ±{MAX_SINGLE_15MIN_MOVE:.0%} → 降险"
    return False, f"spike: {r:+.2%} 正常"


# ─────────────────────────────────────────────────────────────────
# Rule 9 — 止损冷静期 (anti-whipsaw): 防连环止损把曲线锯成齿
# ─────────────────────────────────────────────────────────────────
def in_cooldown(symbol: str, eq_window: EquityWindow) -> tuple[bool, str]:
    """
    均值回复在 random walk / 趋势市里会连环止损, 每次止损都是一个负尖刺,
    直接毁 Sharpe. 止损后强制冷静 N 根 bar 不重开同 symbol.
    返回 (是否在冷静期, 原因).
    """
    last = eq_window.last_stop_bar.get(symbol)
    if last is None:
        return False, f"cooldown: {symbol} 无近期止损"
    bars_since = eq_window.current_bar - last
    if bars_since < COOLDOWN_AFTER_STOP_BARS:
        return True, (f"cooldown ACTIVE: {symbol} 距上次止损{bars_since}根 "
                      f"< {COOLDOWN_AFTER_STOP_BARS}, 暂不重开")
    return False, f"cooldown: {symbol} 已过冷静期"


# ─────────────────────────────────────────────────────────────────
# Sharpe 观测保障: 提醒 brain 必须维持最低交易频率
# ─────────────────────────────────────────────────────────────────
def sharpe_obs_ok(eq_window: EquityWindow) -> tuple[bool, str]:
    """
    比赛: Sharpe 有效观测 <8 → Rank 封顶 50 分.
    这【不】是降险 rule, 是反向提醒: 若交易太少, Sharpe 奖直接折半.
    与"压波动"形成张力 → 解法: 维持低波动的【持续】交易, 而非交易停摆.
    """
    n = len(eq_window.equity_15min)
    if n < SHARPE_MIN_OBS:
        return False, f"sharpe_obs WARN: 仅{n}个15min观测 <{SHARPE_MIN_OBS}, Sharpe Rank 将封顶50分"
    return True, f"sharpe_obs OK: {n}个观测"
