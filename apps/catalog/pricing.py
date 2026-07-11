"""
catalog/pricing.py
==================
The Optimal Unit Price (OUP) engine.

"Optimal Unit Price" = the *lowest achievable per-unit cost* for a product at a
given store, once the best promotion is applied at the quantity that maximizes
the saving. It is the single number the comparison feature sorts on.

Why a dedicated, Django-free module?
    * The math is subtle (non-linear promos) and deserves isolated unit tests.
    * It is called in a tight loop by the scraping pipeline (thousands of
      listings/run) — no ORM, no query overhead, just numbers.

Two figures are produced:
    * ``oup``             — best price using only UNCONDITIONAL promos (available
                            to everyone: 2x1, 70%-off-2nd, flat %). This is what
                            cross-store comparison sorts on (apples-to-apples).
    * ``oup_with_bank``   — best price if the shopper also uses a stackable
                            bank/payment discount. Surfaced separately because it
                            is shopper-specific, not a shelf reality.

Promotion mechanics supported
-----------------------------
    NXM            "2x1" / "3x2"        -> pay `pay_quantity` for every `get_quantity`
    NTH_UNIT_PCT   "70% off 2nd unit"   -> block of `min_quantity`; the Nth unit gets `discount_percent` off
    PERCENT_OFF    "25% off"            -> flat percentage on every unit
    BULK_PRICE     "3 units for $1000"  -> fixed total for `min_quantity` units
    BANK           "-15% with Banco X"  -> conditional, stackable multiplier
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

# How many units we are willing to simulate when searching for the optimal
# quantity. Real supermarket promos saturate quickly (2x1, 3x2, 2nd unit), so a
# small window finds the optimum while keeping the loop O(constant).
DEFAULT_MAX_QTY = 6


@dataclass(frozen=True)
class PromoSpec:
    """Plain, ORM-free description of one promotion (see Promotion.to_spec)."""

    promo_type: str
    min_quantity: int = 1
    get_quantity: Optional[int] = None
    pay_quantity: Optional[int] = None
    nth_unit: Optional[int] = None
    discount_percent: Optional[Decimal | float | int] = None
    bulk_total: Optional[Decimal | float | int] = None
    max_units: Optional[int] = None
    is_conditional: bool = False   # requires a specific payment method (bank promo)
    is_stackable: bool = True
    priority: int = 0


@dataclass(frozen=True)
class OUPResult:
    """Outcome of the OUP search."""

    unit_price: Decimal          # unconditional optimal per-unit price
    best_quantity: int           # quantity that achieves `unit_price`
    applied_promo_type: Optional[str]
    unit_price_with_bank: Decimal  # includes stackable conditional discounts
    savings_pct: Decimal         # vs. shelf price, unconditional


@dataclass(frozen=True)
class LineCost:
    """Cost of buying an EXACT quantity of one listing (used by the cart)."""

    total: Decimal               # what the shopper actually pays for `quantity` units
    unit_price: Decimal          # effective per-unit = total / quantity
    applied_promo_type: Optional[str]
    saved: Decimal               # vs. buying `quantity` units at shelf price


def _decimal(value) -> Decimal:
    """Convert external numeric values without introducing binary-float error."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _q2(value) -> Decimal:
    """Round to 2 decimals (ARS cents) with commercial rounding."""
    return _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Per-promo cost model: total cost of buying `qty` units under a single promo.
# Returns None when the promo does not apply to that quantity.
# ---------------------------------------------------------------------------
def _cost_for_quantity(shelf: Decimal, promo: PromoSpec, qty: int) -> Optional[Decimal]:
    p = promo
    # Respect a per-basket cap: units beyond `max_units` pay full shelf price.
    def capped(promo_units: int) -> tuple[int, int]:
        if p.max_units is None:
            return promo_units, 0
        eligible = min(promo_units, p.max_units)
        return eligible, promo_units - eligible

    if p.promo_type == "nxm":
        # Pay `pay_quantity` for each block of `get_quantity`; remainder at shelf.
        get, pay = p.get_quantity or 1, p.pay_quantity or 1
        if qty < get or get <= 0:
            return None
        blocks, remainder = divmod(qty, get)
        promo_units = blocks * get
        eligible_blocks_units, overflow = capped(promo_units)
        eligible_blocks = eligible_blocks_units // get
        overflow_units = qty - eligible_blocks * get
        return eligible_blocks * pay * shelf + overflow_units * shelf

    if p.promo_type == "nth_unit_pct":
        # Block of `min_quantity`; within a block the `nth_unit` gets a discount.
        block = p.min_quantity or 2
        disc = _decimal(p.discount_percent or 0) / Decimal("100")
        if qty < block or block <= 0:
            return None
        blocks = qty // block
        if p.max_units is not None:
            blocks = min(blocks, p.max_units // block)
        if blocks <= 0:
            return None
        # Each full block: (block-1) full units + 1 discounted unit.
        block_cost = ((block - 1) * shelf) + (shelf * (1 - disc))
        return blocks * block_cost + (qty - blocks * block) * shelf

    if p.promo_type == "percent_off":
        disc = _decimal(p.discount_percent or 0) / Decimal("100")
        eligible, overflow = capped(qty)
        return eligible * shelf * (1 - disc) + overflow * shelf

    if p.promo_type == "bulk_price":
        block = p.min_quantity or 1
        total = _decimal(p.bulk_total) if p.bulk_total is not None else None
        if total is None or qty < block:
            return None
        blocks = qty // block
        if p.max_units is not None:
            blocks = min(blocks, p.max_units // block)
        if blocks <= 0:
            return None
        return blocks * total + (qty - blocks * block) * shelf

    # BANK / unknown types are handled as conditional multipliers, not here.
    return None


def _best_unconditional(shelf: Decimal, promos: list[PromoSpec], max_qty: int):
    """Search qty in [1, max_qty] x promos for the minimum per-unit price."""
    best_unit = shelf          # fallback: buy 1 at shelf price
    best_qty = 1
    best_type = None

    for qty in range(1, max_qty + 1):
        for promo in promos:
            total = _cost_for_quantity(shelf, promo, qty)
            if total is None:
                continue
            unit = total / qty
            # Strictly cheaper, or same price at a smaller basket (prefer qty=1).
            if unit < best_unit:
                best_unit, best_qty, best_type = unit, qty, promo.promo_type
    return best_unit, best_qty, best_type


def optimal_unit_price(
    shelf_price,
    promotions: Iterable[PromoSpec] = (),
    max_qty: int = DEFAULT_MAX_QTY,
) -> OUPResult:
    """
    Compute the Optimal Unit Price for a listing.

    Parameters
    ----------
    shelf_price : Decimal | float
        The single-unit price with NO promotion applied.
    promotions : iterable of PromoSpec
        All currently-valid promotions for the listing.
    max_qty : int
        Upper bound of the quantity simulation.

    Returns
    -------
    OUPResult
    """
    shelf = _decimal(shelf_price)
    promos = list(promotions)

    # 1) Unconditional optimum (what everyone can get, no payment condition).
    unconditional = [p for p in promos if not p.is_conditional]
    best_unit, best_qty, best_type = _best_unconditional(shelf, unconditional, max_qty)

    # 2) Apply the single best conditional discount. Different bank/payment
    #    offers are alternatives, not discounts a shopper can combine. The
    #    flag means the chosen offer may stack with the unconditional promotion.
    conditional_discounts = [
        _decimal(p.discount_percent)
        for p in promos
        if p.is_conditional and p.is_stackable and p.discount_percent
    ]
    best_conditional = _decimal(max(conditional_discounts, default=0))
    bank_multiplier = Decimal("1") - best_conditional / Decimal("100")
    unit_with_bank = best_unit * bank_multiplier

    savings = ((shelf - best_unit) / shelf * Decimal("100")) if shelf > 0 else Decimal("0")

    return OUPResult(
        unit_price=_q2(best_unit),
        best_quantity=best_qty,
        applied_promo_type=best_type,
        unit_price_with_bank=_q2(unit_with_bank),
        savings_pct=_q2(savings),
    )


def line_item_cost(
    shelf_price,
    promotions: Iterable[PromoSpec] = (),
    quantity: int = 1,
    include_conditional: bool = False,
) -> LineCost:
    """
    Cost of buying **exactly** ``quantity`` units of a single listing.

    This is NOT ``oup * quantity``. OUP is the best price at the *optimal*
    quantity; a shopper who asks for 3 units of a 2x1 item pays for 2 full units
    (one 2x1 pair + one single), which ``oup * 3`` would badly understate. The
    cart MUST use this quantity-aware figure to be correct.

    ``include_conditional`` layers stackable bank/payment discounts on top (the
    "if you pay with card X" total); off by default so basket comparison stays
    apples-to-apples across stores.
    """
    shelf = _decimal(shelf_price)
    if quantity <= 0:
        return LineCost(_q2(0), _q2(0), None, _q2(0))

    base_total = shelf * quantity            # no promo at all
    best_total, applied = base_total, None

    # Best single UNCONDITIONAL promo for this exact quantity.
    for promo in (p for p in promotions if not p.is_conditional):
        total = _cost_for_quantity(shelf, promo, quantity)
        if total is not None and total < best_total:
            best_total, applied = total, promo.promo_type

    # Conditional bank/payment offers are mutually exclusive. Apply only the
    # best one; multiplying every advertised bank offer would be impossible for
    # a real shopper to obtain.
    if include_conditional:
        conditional_discounts = [
            _decimal(promo.discount_percent)
            for promo in promotions
            if promo.is_conditional and promo.is_stackable and promo.discount_percent
        ]
        best_conditional = _decimal(max(conditional_discounts, default=0))
        best_total *= Decimal("1") - best_conditional / Decimal("100")

    return LineCost(
        total=_q2(best_total),
        unit_price=_q2(best_total / quantity),
        applied_promo_type=applied,
        saved=_q2(base_total - best_total),
    )


def price_per_measure(oup, base_measure) -> Optional[Decimal]:
    """
    Normalize OUP to ARS per canonical measure (per L / per kg).

    ``base_measure`` is the ``(unit, quantity)`` tuple from
    ``Product.base_measure`` (e.g. ('l', 0.9) for a 900 ml item). Returns ARS/L.
    """
    if not base_measure:
        return None
    _unit, quantity = base_measure
    if not quantity:
        return None
    return _q2(_decimal(oup) / _decimal(quantity))


# ---------------------------------------------------------------------------
# Quick self-check (illustrative; real tests live in tests/test_pricing.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    shelf = Decimal("1000")

    two_for_one = PromoSpec(promo_type="nxm", get_quantity=2, pay_quantity=1)
    r = optimal_unit_price(shelf, [two_for_one])
    assert r.unit_price == Decimal("500.00"), r          # 2x1 -> 500/unit

    second_70 = PromoSpec(promo_type="nth_unit_pct", min_quantity=2, nth_unit=2, discount_percent=70)
    r = optimal_unit_price(shelf, [second_70])
    assert r.unit_price == Decimal("650.00"), r          # (1000 + 300)/2 = 650

    bank = PromoSpec(promo_type="bank", discount_percent=15, is_conditional=True)
    r = optimal_unit_price(shelf, [two_for_one, bank])
    assert r.unit_price == Decimal("500.00"), r          # unconditional unchanged
    assert r.unit_price_with_bank == Decimal("425.00"), r  # 500 * 0.85
    print("OUP engine self-check passed:", r)

    # --- quantity-aware line cost (the cart primitive) ---
    # 2x1: buying 3 pays for 2 (one pair + one single), NOT oup*3 = 1500.
    lc = line_item_cost(shelf, [two_for_one], quantity=3)
    assert lc.total == Decimal("2000.00"), lc            # pay 1 (pair) + 1 (single)
    assert lc.unit_price == Decimal("666.67"), lc
    # 2x1 at an even quantity fully benefits.
    assert line_item_cost(shelf, [two_for_one], 4).total == Decimal("2000.00")
    # Buying 1 of a 2x1 = full price (promo doesn't trigger).
    assert line_item_cost(shelf, [two_for_one], 1).total == Decimal("1000.00")
    # 70%-off-2nd, qty 3 = one discounted block (1300) + one single (1000).
    assert line_item_cost(shelf, [second_70], 3).total == Decimal("2300.00")
    # Flat 25% off, qty 3.
    flat = PromoSpec(promo_type="percent_off", discount_percent=25)
    assert line_item_cost(shelf, [flat], 3).total == Decimal("2250.00")
    print("Line-cost (cart) self-check passed:", lc)
