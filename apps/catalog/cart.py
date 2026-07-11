"""
catalog/cart.py
===============
Cart / basket optimization. Given a shopping list ``[{ean, quantity}, ...]`` it
answers the two questions a shopper actually has:

    1. "If I do ALL my shopping at one store, which store is cheapest — and what
        does it cost me at each?"                          -> per-store baskets
    2. "If I'm willing to split my trip, what's the theoretical minimum, and where
        do I buy each item?"                               -> optimal split

Correctness note
----------------
Line totals use :func:`catalog.pricing.line_item_cost` — the *quantity-aware*
cost — NOT ``current_oup * quantity``. OUP is the best price at the optimal
quantity; applying it blindly to an arbitrary requested quantity understates the
cost of promos like 2x1 (buy 3 -> pay 2, not 1.5). See the pricing self-checks.

Efficiency note
---------------
The whole basket is resolved in a handful of queries: one ``Product`` fetch with
the available listings + their (currently valid) promotions prefetched. No N+1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.db.models import Prefetch, Q
from django.utils import timezone

from .models import Product, Promotion, StoreProduct
from .pricing import line_item_cost


@dataclass
class CartLine:
    ean: str
    quantity: int
    name: str = ""
    # per-store cost for THIS line: slug -> {store, total, unit, promo}
    offers: dict = field(default_factory=dict)


def _valid_promotions_prefetch():
    """Prefetch only currently-valid promotions (don't price on expired promos)."""
    now = timezone.now()
    qs = (
        Promotion.objects.filter(is_active=True, valid_from__lte=now)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=now))
    )
    return Prefetch("promotions", queryset=qs, to_attr="valid_promos")


def optimize_cart(items: list[dict], include_conditional: bool = False) -> dict:
    """
    Parameters
    ----------
    items : list of {"ean": str, "quantity": int}
    include_conditional : bool
        If True, factor stackable bank/payment discounts into every line.

    Returns
    -------
    dict  (JSON-serializable) — see the assembled ``result`` at the bottom.
    """
    # 1) Normalize + de-duplicate the requested list (sum repeats of the same EAN).
    qty_by_ean: dict[str, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        ean = str(it.get("ean", "")).strip()
        try:
            qty = int(it.get("quantity", 1) or 0)
        except (TypeError, ValueError):
            continue
        if ean and qty > 0:
            qty_by_ean[ean] = qty_by_ean.get(ean, 0) + qty

    if not qty_by_ean:
        return _empty_result()

    # 2) One bulk fetch: products + available listings + valid promotions.
    products = (
        Product.objects.filter(ean__in=list(qty_by_ean))
        .prefetch_related(
            Prefetch(
                "listings",
                queryset=(
                    StoreProduct.objects.available()
                    .select_related("store")
                    .prefetch_related(_valid_promotions_prefetch())
                ),
            )
        )
    )
    product_by_ean = {p.ean: p for p in products}

    # 3) Build the (ean x store) line-cost matrix once.
    lines: list[CartLine] = []
    stores_seen: dict[str, object] = {}     # slug -> Store
    for ean, qty in qty_by_ean.items():
        product = product_by_ean.get(ean)
        line = CartLine(ean=ean, quantity=qty, name=product.name if product else ean)
        if product:
            for sp in product.listings.all():
                if sp.current_price is None:
                    continue
                specs = [pr.to_spec() for pr in getattr(sp, "valid_promos", [])]
                lc = line_item_cost(sp.current_price, specs, qty, include_conditional)
                line.offers[sp.store.slug] = {
                    "store": sp.store.name,
                    "slug": sp.store.slug,
                    "total": lc.total,
                    "unit_price": lc.unit_price,
                    "promo": lc.applied_promo_type,
                    "saved": lc.saved,
                    "url": sp.url,
                }
                stores_seen[sp.store.slug] = sp.store
        lines.append(line)

    # 4a) Per-store single-basket totals (+ what each store is missing).
    per_store = []
    for slug, store in stores_seen.items():
        subtotal = Decimal("0")
        found = 0
        missing = []
        for line in lines:
            offer = line.offers.get(slug)
            if offer:
                subtotal += offer["total"]
                found += 1
            else:
                missing.append(line.ean)
        per_store.append({
            "slug": slug,
            "store": store.name,
            "subtotal": _money(subtotal),
            "items_found": found,
            "items_total": len(lines),
            "missing": missing,
            "has_all": not missing,
        })
    # Cheapest first; a store carrying everything always ranks above a partial one.
    per_store.sort(key=lambda s: (not s["has_all"], Decimal(s["subtotal"])))

    # 4b) Optimal split — cheapest store per item (the theoretical floor).
    optimal_total = Decimal("0")
    picks = []
    unavailable = []
    for line in lines:
        if not line.offers:
            unavailable.append(line.ean)
            continue
        best_slug = min(line.offers, key=lambda s: line.offers[s]["total"])
        best = line.offers[best_slug]
        optimal_total += best["total"]
        picks.append({
            "ean": line.ean,
            "name": line.name,
            "quantity": line.quantity,
            "store": best["store"],
            "slug": best_slug,
            "total": _money(best["total"]),
            "unit_price": _money(best["unit_price"]),
            "promo": best["promo"],
        })

    # 5) Savings of splitting vs. the best single store that carries everything.
    best_single = next((s for s in per_store if s["has_all"]), None)
    savings_vs_single = None
    if best_single:
        savings_vs_single = _money(Decimal(best_single["subtotal"]) - optimal_total)

    return {
        "lines": [
            {"ean": l.ean, "name": l.name, "quantity": l.quantity,
             "offers": {k: {**v, "total": _money(v["total"]),
                            "unit_price": _money(v["unit_price"]),
                            "saved": _money(v["saved"])}
                        for k, v in l.offers.items()}}
            for l in lines
        ],
        "per_store": per_store,
        "best_single_store": best_single,
        "optimal_split": {
            "total": _money(optimal_total),
            "savings_vs_best_single": savings_vs_single,
            "picks": picks,
            "unavailable": unavailable,
        },
        "include_conditional": include_conditional,
    }


def _money(v: Optional[Decimal]) -> Optional[str]:
    """Serialize Decimals as strings to preserve exact cents over JSON."""
    return None if v is None else f"{Decimal(v):.2f}"


def _empty_result() -> dict:
    return {
        "lines": [], "per_store": [], "best_single_store": None,
        "optimal_split": {"total": "0.00", "savings_vs_best_single": None,
                          "picks": [], "unavailable": []},
        "include_conditional": False,
    }
