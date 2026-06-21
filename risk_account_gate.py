"""
risk_account_gate.py — risk_engine 增量: 账户级总敞口闸门 (护栏, 新增 rule)

═══════════════════════════════════════════════════════════════════
为什么需要它 (你 memory 里一直标 open 的缺口)
═══════════════════════════════════════════════════════════════════
现有 evaluate_order 是【逐单】检查: 每笔 ≤2% 风险、每笔 ≤12x.
但多品种并行时, XAG 腿 + FX 几条腿【共用同一个 $1M 权益和保证金池】.
每笔单独看都合规, 叠加后账户总保证金占用/总杠杆可能逼近爆仓或扣分线.
symbol 不冲突, 但【保证金池冲突】. 这是"逐单全过、账户却已危险"的阴沟翻船.

本模块 = 一道【事前】闸门: 第 N 条腿进场【前】, 先算"这单成交后全账户
总保证金占用会不会超 GATE", 超了就 REJECT (或交回 allocator 缩).

定位: 纯护栏. 不知道也不关心"XAG 该不该比 EUR 优先"——那是 allocator 的事.
本闸门对所有腿一视同仁, 只回答一个问题: 这单会不会让账户总占用爆.
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence, Optional


# 你拍板的阈值
ACCOUNT_MARGIN_GATE = 0.60     # 账户总保证金占用上限 (扣分线 90%, 留 30% 缓冲)
ACCOUNT_LEVERAGE_GATE = 12.0   # 账户总有效杠杆上限 (与单笔 MAX_EFFECTIVE_LEVERAGE 对齐)


@dataclass(frozen=True)
class _PosView:
    """风控只需要的持仓最小视图 (从 MT5 持仓映射)。"""
    symbol: str
    notional: float            # 名义价值 = |volume| * contract_size * price
    margin: float              # 该仓占用保证金 (来自 mt5.order_calc_margin, 非 margin_initial!)


def account_gate(
    new_notional: float,
    new_margin: float,
    equity: float,
    open_positions: Sequence[_PosView],
) -> tuple[bool, str]:
    """
    事前闸门. 返回 (是否放行, 原因).

    Args:
        new_notional: 待下这单的名义价值
        new_margin:   待下这单的保证金占用 (必须用 mt5.order_calc_margin 算,
                      因为本 server margin_initial 返回 0.0 不可信)
        equity:       当前账户净值
        open_positions: 当前所有持仓 (跨所有 symbol)

    Rule 1 — 总保证金占用: (已用 margin + 这单 margin) / equity <= GATE
    Rule 2 — 总有效杠杆:   (总名义 + 这单名义) / equity <= LEV_GATE
    """
    if equity <= 0:
        return False, "account_gate: equity<=0 (异常, 拒)"

    used_margin = sum(p.margin for p in open_positions)
    total_notional = sum(p.notional for p in open_positions)

    # Rule 1: 总保证金占用
    proj_margin_util = (used_margin + new_margin) / equity
    if proj_margin_util > ACCOUNT_MARGIN_GATE:
        return False, (f"account_gate REJECT: 总保证金占用 {proj_margin_util:.1%} "
                       f"> {ACCOUNT_MARGIN_GATE:.0%} (已用{used_margin:.0f}+本单{new_margin:.0f})")

    # Rule 2: 总有效杠杆
    proj_leverage = (total_notional + new_notional) / equity
    if proj_leverage > ACCOUNT_LEVERAGE_GATE:
        return False, (f"account_gate REJECT: 总有效杠杆 {proj_leverage:.1f}x "
                       f"> {ACCOUNT_LEVERAGE_GATE}x")

    return True, (f"account_gate PASS: 占用→{proj_margin_util:.1%} 杠杆→{proj_leverage:.1f}x")


# ═══════════════════════════════════════════════════════════════════
# 集成点 (写进 evaluate_order, 作为第六道检查, 顺序在敞口之后):
#
#   def evaluate_order(order, state):
#       # ... ① 熔断 ② 预警线 ③ 杠杆 ④ 单笔风险(缩size) ⑤ 敞口 ...
#       # ⑥ 账户级总闸门 (NEW)
#       ok, why = account_gate(
#           new_notional = order.size * contract_value(order.symbol) * order.entry_price,
#           new_margin   = mt5_order_calc_margin(order),   # 不能用 margin_initial
#           equity       = state.equity,
#           open_positions = [_PosView(p.symbol, p.notional, p.margin) for p in state.positions],
#       )
#       if not ok:
#           return Decision(Verdict.REJECTED, None, why)
#       return Decision(Verdict.APPROVED, order, "...")
#
# 注意顺序: 放在【最后】. 因为 ④ 可能已缩小 size, 要用缩后的 size 算 new_margin.
# 熔断仍是优先级最高(独立函数), 本闸门只在前五关都过后才算总账.
# ═══════════════════════════════════════════════════════════════════
