"""
INDstocks Smart Order (GTT) payload builder + validator.

Encodes the rules from https://api-docs.indstocks.com/smart_orders/ so that a
malformed order is caught locally, before it costs a round trip, a rejection,
or -- worse -- a half-placed position.

Prices are Decimal throughout. Tick-size checks on floats are a trap: NSE's
0.05 tick means 358.35 % 0.05 == 0.049999999999994145 in binary floating point,
so a correct price fails a naive modulo test. Never pass floats in here.

No network. No credentials. Pure logic, so it can be unit-tested offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Literal, Optional

TxnType = Literal["BUY", "SELL"]
Segment = Literal["EQUITY", "DERIVATIVE", "COMMODITY"]
OrderType = Literal["LIMIT", "MARKET", "TRIGGER"]

# algo_id defaults, per the docs: "99999" for NSE and MCX,
# "9999999999999999" for BSE.
DEFAULT_ALGO_ID = {"NSE": "99999", "MCX": "99999", "BSE": "9999999999999999"}

MAX_REMARKS = 100


class SmartOrderError(ValueError):
    """Carries the doc's own validation name so failures are greppable."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def to_tick(price: Decimal, tick: Decimal, *, mode: str = "nearest") -> Decimal:
    """Snap a price to a multiple of the instrument tick size.

    mode="down" for a BUY stop-loss leg (further from entry = safer),
    mode="up"   for a BUY target leg,
    mode="nearest" otherwise.
    """
    if tick <= 0:
        raise SmartOrderError("TickSizeMustBeAboveZero", "tick size must be > 0")
    units = price / tick
    if mode == "down":
        units = units.to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        units = units.to_integral_value(rounding=ROUND_UP)
    else:
        units = Decimal(int(units + Decimal("0.5")))
    return (units * tick).quantize(tick)


def on_tick(price: Decimal, tick: Decimal) -> bool:
    return (price / tick) == (price / tick).to_integral_value()


@dataclass
class SmartOrder:
    txn_type: TxnType
    exchange: str
    segment: Segment
    product: str
    order_type: OrderType
    security_id: str
    qty: int
    validity: str = "DAY"
    limit_price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    trigger_limit_price: Optional[Decimal] = None
    sl_trigger_price: Optional[Decimal] = None
    sl_limit_price: Optional[Decimal] = None
    tgt_trigger_price: Optional[Decimal] = None
    tgt_limit_price: Optional[Decimal] = None
    algo_id: Optional[str] = None
    remarks: Optional[str] = None
    slice_order: bool = False

    # Not sent. Needed to validate locally.
    tick_size: Decimal = Decimal("0.05")
    lot_size: int = 1
    cmp: Optional[Decimal] = None  # current market price
    _warnings: list[str] = field(default_factory=list)

    # ---------- entry price resolution ----------

    def entry_price(self) -> Decimal:
        """The price both legs are validated against.

        LIMIT   -> limit_price
        MARKET  -> the live market price
        TRIGGER -> trigger_limit_price, or trigger_price when omitted
        """
        if self.order_type == "LIMIT":
            if self.limit_price is None:
                raise SmartOrderError(
                    "LimitPriceMustBeAboveZero",
                    "limit_price is required for a LIMIT order",
                )
            return self.limit_price
        if self.order_type == "MARKET":
            if self.cmp is None:
                raise SmartOrderError(
                    "MarketEntryNeedsCMP",
                    "a MARKET order's legs are checked against the live price; "
                    "supply cmp so this can be validated before sending",
                )
            return self.cmp
        return self.trigger_limit_price or self.trigger_price  # type: ignore[return-value]

    # ---------- validation ----------

    def validate(self) -> "SmartOrder":
        self._validate_qty()
        self._validate_order_type()
        entry = self.entry_price()
        self._validate_legs(entry)
        self._validate_remarks()
        self._validate_slicing()
        return self

    def _validate_qty(self) -> None:
        if self.qty is None or self.qty <= 0:
            raise SmartOrderError(
                "QtyMustBeAboveZero", "qty must be specified and greater than zero"
            )
        if self.segment in ("DERIVATIVE", "COMMODITY") and self.qty % self.lot_size:
            raise SmartOrderError(
                "QtyMultipleOfLotSize",
                f"qty {self.qty} is not a multiple of lot size {self.lot_size}",
            )

    def _validate_order_type(self) -> None:
        if self.order_type == "LIMIT":
            if self.limit_price is None or self.limit_price <= 0:
                raise SmartOrderError(
                    "LimitPriceMustBeAboveZero",
                    "limit price must be specified and greater than zero",
                )

        elif self.order_type == "MARKET":
            # A COMMODITY MARKET order carrying limit_price is rejected; for the
            # others the field is simply meaningless. Refuse it everywhere so we
            # never depend on which rule the server applies.
            if self.limit_price is not None:
                raise SmartOrderError(
                    "LimitPriceNotAllowedForMarket",
                    "omit limit_price on a MARKET order",
                )
            self._warnings.append(
                "MARKET is converted to LIMIT at the live price before it reaches "
                "the exchange -- this order can fail to fill on a fast move."
            )

        elif self.order_type == "TRIGGER":
            if self.limit_price is not None:
                raise SmartOrderError(
                    "LimitPriceNotAllowedForTrigger",
                    "Limit Price is not allowed for TRIGGER order type; "
                    "use Trigger Limit Price instead",
                )
            if self.trigger_price is None or self.trigger_price <= 0:
                raise SmartOrderError(
                    "TriggerPriceMustBeAboveZero",
                    "trigger_price must be present and greater than zero",
                )
            if not on_tick(self.trigger_price, self.tick_size):
                raise SmartOrderError(
                    "TriggerPriceTickSize",
                    f"trigger_price {self.trigger_price} is not a multiple of "
                    f"tick size {self.tick_size}",
                )
            if self.trigger_limit_price is not None and not on_tick(
                self.trigger_limit_price, self.tick_size
            ):
                raise SmartOrderError(
                    "TriggerLimitPriceTickSize",
                    f"trigger_limit_price {self.trigger_limit_price} is not a "
                    f"multiple of tick size {self.tick_size}",
                )
            if self.cmp is not None:
                if self.txn_type == "BUY" and self.trigger_price <= self.cmp:
                    raise SmartOrderError(
                        "TriggerPriceVsCMP",
                        f"BUY trigger_price {self.trigger_price} must be strictly "
                        f"greater than CMP {self.cmp}",
                    )
                if self.txn_type == "SELL" and self.trigger_price >= self.cmp:
                    raise SmartOrderError(
                        "TriggerPriceVsCMP",
                        f"SELL trigger_price {self.trigger_price} must be strictly "
                        f"less than CMP {self.cmp}",
                    )

    def _validate_legs(self, entry: Decimal) -> None:
        # Each leg needs BOTH its trigger and its limit, or the order is rejected.
        if (self.sl_trigger_price is None) != (self.sl_limit_price is None):
            raise SmartOrderError(
                "StopLossLegIncomplete",
                "sl_trigger_price and sl_limit_price must be supplied together",
            )
        if (self.tgt_trigger_price is None) != (self.tgt_limit_price is None):
            raise SmartOrderError(
                "TargetLegIncomplete",
                "tgt_trigger_price and tgt_limit_price must be supplied together",
            )

        buy = self.txn_type == "BUY"

        if self.sl_trigger_price is not None:
            assert self.sl_limit_price is not None
            if buy:
                if self.sl_trigger_price >= entry:
                    raise SmartOrderError(
                        "MaxSlTriggerPrice",
                        f"BUY stop-loss trigger {self.sl_trigger_price} must sit "
                        f"below entry {entry}",
                    )
                if self.sl_limit_price >= self.sl_trigger_price:
                    raise SmartOrderError(
                        "MaxSlLimitPrice",
                        f"BUY stop-loss limit {self.sl_limit_price} must sit below "
                        f"its trigger {self.sl_trigger_price}",
                    )
            else:
                if self.sl_trigger_price <= entry:
                    raise SmartOrderError(
                        "MinSlTriggerPrice",
                        f"SELL stop-loss trigger {self.sl_trigger_price} must sit "
                        f"above entry {entry}",
                    )
                if self.sl_limit_price <= self.sl_trigger_price:
                    raise SmartOrderError(
                        "MinSlLimitPrice",
                        f"SELL stop-loss limit {self.sl_limit_price} must sit above "
                        f"its trigger {self.sl_trigger_price}",
                    )

        if self.tgt_trigger_price is not None:
            assert self.tgt_limit_price is not None
            if buy:
                if self.tgt_trigger_price <= entry:
                    raise SmartOrderError(
                        "MinTgtTriggerPrice",
                        f"BUY target trigger {self.tgt_trigger_price} must sit above "
                        f"entry {entry}",
                    )
                if self.tgt_limit_price <= self.tgt_trigger_price:
                    raise SmartOrderError(
                        "MinTgtLimitPrice",
                        f"BUY target limit {self.tgt_limit_price} must sit above its "
                        f"trigger {self.tgt_trigger_price}",
                    )
            else:
                if self.tgt_trigger_price >= entry:
                    raise SmartOrderError(
                        "MaxTgtTriggerPrice",
                        f"SELL target trigger {self.tgt_trigger_price} must sit below "
                        f"entry {entry}",
                    )
                if self.tgt_limit_price >= self.tgt_trigger_price:
                    raise SmartOrderError(
                        "MaxTgtLimitPrice",
                        f"SELL target limit {self.tgt_limit_price} must sit below its "
                        f"trigger {self.tgt_trigger_price}",
                    )

        for name in (
            "sl_trigger_price",
            "sl_limit_price",
            "tgt_trigger_price",
            "tgt_limit_price",
        ):
            v = getattr(self, name)
            if v is not None and not on_tick(v, self.tick_size):
                raise SmartOrderError(
                    "LegPriceTickSize",
                    f"{name} {v} is not a multiple of tick size {self.tick_size}",
                )

    def _validate_remarks(self) -> None:
        if self.remarks is not None and len(self.remarks) > MAX_REMARKS:
            # The server truncates silently; truncating here too would break
            # reconciliation without telling anyone.
            raise SmartOrderError(
                "RemarksTooLong",
                f"remarks is {len(self.remarks)} chars; max {MAX_REMARKS} "
                "(the server truncates silently, which breaks reconciliation)",
            )

    def _validate_slicing(self) -> None:
        if self.slice_order and self.segment == "EQUITY":
            raise SmartOrderError(
                "SlicingNotAllowedForEquity",
                "Order slicing is available for segments DERIVATIVE and COMMODITY only",
            )

    # ---------- serialisation ----------

    def payload(self) -> dict:
        """The JSON body for POST /smart/order. Validates first."""
        self.validate()

        def num(d: Optional[Decimal]):
            return None if d is None else float(d)

        body = {
            "txn_type": self.txn_type,
            "exchange": self.exchange,
            "segment": self.segment,
            "product": self.product,
            "order_type": self.order_type,
            "validity": self.validity,
            "security_id": self.security_id,
            "qty": self.qty,
            "algo_id": self.algo_id or DEFAULT_ALGO_ID.get(self.exchange, "99999"),
            "limit_price": num(self.limit_price),
            "trigger_price": num(self.trigger_price),
            "trigger_limit_price": num(self.trigger_limit_price),
            "sl_trigger_price": num(self.sl_trigger_price),
            "sl_limit_price": num(self.sl_limit_price),
            "tgt_trigger_price": num(self.tgt_trigger_price),
            "tgt_limit_price": num(self.tgt_limit_price),
            "remarks": self.remarks,
        }
        if self.slice_order:
            body["slice_order"] = True
        return {k: v for k, v in body.items() if v is not None}

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)


def build_long_day_trade(
    *,
    security_id: str,
    qty: int,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    tick_size: Decimal = Decimal("0.05"),
    slippage_ticks: int = 2,
    exchange: str = "NSE",
    remarks: Optional[str] = None,
) -> SmartOrder:
    """Turn the skill's three numbers (entry/stop/target) into a legal smart order.

    trader-exit speaks in two prices; the API needs four. The limit on each leg
    is offset from its trigger by `slippage_ticks` so the leg can actually fill
    when it fires: a stop-limit placed exactly at its trigger will sit unfilled
    in a gap, which is the failure mode that turns a planned loss into an
    unplanned one.
    """
    band = tick_size * slippage_ticks

    sl_trigger = to_tick(stop, tick_size, mode="down")
    sl_limit = to_tick(sl_trigger - band, tick_size, mode="down")
    tgt_trigger = to_tick(target, tick_size, mode="up")
    tgt_limit = to_tick(tgt_trigger + band, tick_size, mode="up")

    return SmartOrder(
        txn_type="BUY",
        exchange=exchange,
        segment="EQUITY",
        product="INTRADAY",
        order_type="LIMIT",
        security_id=security_id,
        qty=qty,
        limit_price=to_tick(entry, tick_size, mode="up"),
        sl_trigger_price=sl_trigger,
        sl_limit_price=sl_limit,
        tgt_trigger_price=tgt_trigger,
        tgt_limit_price=tgt_limit,
        tick_size=tick_size,
        remarks=remarks,
    ).validate()


def build_for_instrument(
    instrument,
    *,
    qty: int,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    slippage_ticks: int = 2,
    remarks: Optional[str] = None,
) -> SmartOrder:
    """Same as build_long_day_trade, but takes security_id and tick size from
    a resolved Instrument rather than being told.

    This is the form callers should use. The tick-size default of 0.05 in
    build_long_day_trade is a convenience for tests and is wrong for a large
    part of the market: the master puts roughly 18,700 NSE instruments at
    0.01 and RELIANCE at 0.10. An off-tick leg price is rejected outright,
    which on a stop-loss leg means the protection never reaches the book.

    Duck-typed on purpose (.security_id, .tick_size, .exchange) so this module
    stays importable without the instruments master.
    """
    return build_long_day_trade(
        security_id=instrument.security_id,
        qty=qty,
        entry=entry,
        stop=stop,
        target=target,
        tick_size=instrument.tick_size,
        slippage_ticks=slippage_ticks,
        exchange=getattr(instrument, "exchange", "NSE") or "NSE",
        remarks=remarks,
    )


def build_trigger_for_instrument(
    instrument,
    *,
    qty: int,
    range_high: Decimal,
    stop: Decimal,
    target: Decimal,
    cmp: Decimal,
    slippage_ticks: int = 2,
    remarks: Optional[str] = None,
) -> SmartOrder:
    """A resting TRIGGER order at the opening-range high, legs attached.

    This is what removes execution latency: the order sits AT THE EXCHANGE
    with its stop and target, so the fill happens in exchange time rather
    than waiting on a human, a chat round trip or an ssh session.

    The trade-off is honest and worth stating: a TRIGGER fires on a TOUCH of
    the level, where the close-above rule waits for a completed candle. It
    will catch moves the close rule misses and will also be taken out by
    wicks the close rule ignores. Different strategy, not a faster one.

    trigger_price must sit strictly above the current price, so a level that
    has already been passed is refused rather than silently repriced.
    """
    tick = instrument.tick_size
    band = tick * slippage_ticks

    trigger = to_tick(range_high + tick, tick, mode="up")
    if trigger <= cmp:
        raise SmartOrderError(
            "TriggerPriceVsCMP",
            f"range high {range_high} is already below the market at {cmp}; "
            "the breakout has happened and this entry is stale",
        )

    # The entry the legs are measured against is trigger_limit_price.
    trigger_limit = to_tick(trigger + band, tick, mode="up")

    sl_trigger = to_tick(stop, tick, mode="down")
    sl_limit = to_tick(sl_trigger - band, tick, mode="down")
    tgt_trigger = to_tick(target, tick, mode="up")
    tgt_limit = to_tick(tgt_trigger + band, tick, mode="up")

    return SmartOrder(
        txn_type="BUY",
        exchange=getattr(instrument, "exchange", "NSE") or "NSE",
        segment="EQUITY",
        product="INTRADAY",
        order_type="TRIGGER",
        security_id=instrument.security_id,
        qty=qty,
        trigger_price=trigger,
        trigger_limit_price=trigger_limit,
        sl_trigger_price=sl_trigger,
        sl_limit_price=sl_limit,
        tgt_trigger_price=tgt_trigger,
        tgt_limit_price=tgt_limit,
        tick_size=tick,
        cmp=cmp,
        remarks=remarks,
    ).validate()
