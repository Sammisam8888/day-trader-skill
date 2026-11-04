"""
All-in cost of an intraday equity round trip on NSE.

The risk gate was using a flat Rs 20 (brokerage only). That understates the
real figure, and on a small account the gap decides whether a trade is worth
taking at all. This computes every statutory charge so the break-even move
can be stated as a percentage rather than guessed at.

Rates as of 2026 for NSE cash intraday (MIS). They change in Union Budgets
and exchange circulars -- check them before trusting a tight margin, and
override via the constants below.

On TDS: there is NO TDS on equity trading gains for a resident individual.
Intraday equity profit is speculative business income, taxed at your slab
rate when you file, and short-term delivery gains fall under section 111A.
Nothing is withheld at the time of the trade, so no part of it belongs in a
per-trade cost model. Nothing here is tax advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

# Per-order brokerage. INDstocks charges a flat Rs 10 per algo order.
BROKERAGE_PER_ORDER = Decimal("10")

# Securities Transaction Tax: intraday equity is charged on the SELL leg only.
STT_SELL_RATE = Decimal("0.00025")          # 0.025%

# NSE exchange transaction charge, both legs.
EXCHANGE_TXN_RATE = Decimal("0.0000297")    # 0.00297%

# SEBI turnover fee, both legs: Rs 10 per crore.
SEBI_RATE = Decimal("0.000001")

# NSE investor protection fund trust, both legs: Rs 10 per crore.
IPFT_RATE = Decimal("0.000001")

# Stamp duty: BUY leg only.
STAMP_DUTY_RATE = Decimal("0.00003")        # 0.003%

# GST on brokerage + exchange charges + SEBI fee.
GST_RATE = Decimal("0.18")

PAISE = Decimal("0.01")


def _r(x: Decimal) -> Decimal:
    return x.quantize(PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Charges:
    brokerage: Decimal
    stt: Decimal
    exchange: Decimal
    sebi: Decimal
    ipft: Decimal
    stamp_duty: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return _r(self.brokerage + self.stt + self.exchange + self.sebi
                  + self.ipft + self.stamp_duty + self.gst)

    def breakdown(self) -> dict:
        return {
            "brokerage": str(self.brokerage), "stt": str(self.stt),
            "exchange": str(self.exchange), "sebi": str(self.sebi),
            "ipft": str(self.ipft), "stamp_duty": str(self.stamp_duty),
            "gst": str(self.gst), "total": str(self.total),
        }


def round_trip(buy_value: Decimal, sell_value: Decimal) -> Charges:
    """Every charge on buying at one value and selling at another, intraday."""
    turnover = buy_value + sell_value

    brokerage = BROKERAGE_PER_ORDER * 2
    stt = _r(sell_value * STT_SELL_RATE)          # sell leg only
    exchange = _r(turnover * EXCHANGE_TXN_RATE)
    sebi = _r(turnover * SEBI_RATE)
    ipft = _r(turnover * IPFT_RATE)
    stamp = _r(buy_value * STAMP_DUTY_RATE)       # buy leg only
    gst = _r((brokerage + exchange + sebi) * GST_RATE)

    return Charges(brokerage=brokerage, stt=stt, exchange=exchange, sebi=sebi,
                   ipft=ipft, stamp_duty=stamp, gst=gst)


def cost_of_plan(entry: Decimal, target: Decimal, qty: int) -> Charges:
    """Charges for the winning case: in at entry, out at target."""
    return round_trip(entry * qty, target * qty)


def breakeven_move(entry: Decimal, qty: int) -> Decimal:
    """The rupee move per share needed just to cover costs.

    Solved rather than iterated: charges are linear in the sell value, so
    with S = entry*qty + qty*m, total(m) = fixed + rate * qty * m, and we
    want gain qty*m = total(m).
    """
    if qty <= 0:
        return Decimal(0)

    buy_value = entry * qty
    fixed = (BROKERAGE_PER_ORDER * 2 * (Decimal(1) + GST_RATE)
             + buy_value * STAMP_DUTY_RATE
             + buy_value * (EXCHANGE_TXN_RATE * (Decimal(1) + GST_RATE)
                            + SEBI_RATE * (Decimal(1) + GST_RATE)
                            + IPFT_RATE))

    # Per rupee of sell value.
    sell_rate = (STT_SELL_RATE
                 + EXCHANGE_TXN_RATE * (Decimal(1) + GST_RATE)
                 + SEBI_RATE * (Decimal(1) + GST_RATE)
                 + IPFT_RATE)

    # gain = fixed + sell_rate * (buy_value + gain)
    gain = (fixed + sell_rate * buy_value) / (Decimal(1) - sell_rate)
    return _r(gain / qty)


def breakeven_pct(entry: Decimal, qty: int) -> Decimal:
    """Break-even move as a percentage of entry. The number that decides
    whether an account is large enough to trade at all."""
    if entry <= 0 or qty <= 0:
        return Decimal(0)
    return _r(breakeven_move(entry, qty) / entry * Decimal(100))
