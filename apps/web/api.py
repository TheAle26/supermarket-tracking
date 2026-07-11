"""
web/api.py
==========
JSON API for the front-end. Deliberately plain Django ``JsonResponse`` views so
the whole thing runs with only Django installed (no DRF). Each maps 1:1 to a
front-end view:

    GET  /api/products/            ?q=...        -> search (typeahead + basket add)
    GET  /api/products/<ean>/compare/            -> COMPARE view (stores ranked)
    GET  /api/products/<ean>/history/?days=90    -> TIME view (price-over-time)
    POST /api/cart/optimize/                     -> CART view (per-store + optimal split)

Auth/throttling are intentionally omitted here; in production wrap these behind
token auth + a rate limiter (or port to DRF ViewSets — the query shapes are the
same). Read views are cache-friendly (the data changes at most once per day).
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from catalog.cart import optimize_cart
from catalog.models import PriceAggregate, PriceHistory, Product, Promotion, StoreProduct


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _money(v):
    return None if v is None else f"{Decimal(v):.2f}"


def _valid_promos_prefetch():
    now = timezone.now()
    qs = (
        Promotion.objects.filter(is_active=True, valid_from__lte=now)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=now))
    )
    return Prefetch("promotions", queryset=qs, to_attr="valid_promos")


def _product_dict(p: Product) -> dict:
    return {
        "ean": p.ean,
        "name": p.name,
        "brand": p.brand,
        "image_url": p.image_url,
        "net_content": _money(p.net_content) if p.net_content is not None else None,
        "unit_of_measure": p.unit_of_measure,
    }


def _bounded_query_int(request, name, default, minimum, maximum):
    """Return (value, error_response) for a bounded integer query parameter."""
    raw = request.GET.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, JsonResponse(
            {"error": f"'{name}' must be an integer"}, status=400
        )
    if not minimum <= value <= maximum:
        return None, JsonResponse(
            {"error": f"'{name}' must be between {minimum} and {maximum}"},
            status=400,
        )
    return value, None


# ---------------------------------------------------------------------------
# search — powers typeahead and "add to cart"
# ---------------------------------------------------------------------------
@require_GET
def search(request):
    q = (request.GET.get("q") or "").strip()
    limit, error = _bounded_query_int(request, "limit", 20, 1, 50)
    if error:
        return error
    if not q:
        return JsonResponse({"results": []})

    qs = Product.objects.search(q).with_price_summary()[:limit]
    results = [
        {**_product_dict(p), "min_oup": _money(getattr(p, "min_oup", None))}
        for p in qs
    ]
    return JsonResponse({"results": results})


# ---------------------------------------------------------------------------
# COMPARE — every store's current offer for one product, cheapest first
# ---------------------------------------------------------------------------
@require_GET
def compare(request, ean):
    product = get_object_or_404(Product, ean=ean)
    offers_qs = (
        product.listings.available()
        .select_related("store")
        .prefetch_related(_valid_promos_prefetch())
        .order_by("current_oup")               # cheapest first (indexed)
    )

    offers = []
    for sp in offers_qs:
        promos = [
            {"type": pr.promo_type, "label": pr.label or pr.get_promo_type_display()}
            for pr in getattr(sp, "valid_promos", [])
        ]
        # Savings of the OUP vs. the struck-through list price.
        disc = None
        if sp.current_list_price and sp.current_oup and sp.current_list_price > 0:
            disc = round(float(1 - (sp.current_oup / sp.current_list_price)) * 100, 1)
        offers.append({
            "store": sp.store.name,
            "slug": sp.store.slug,
            "list_price": _money(sp.current_list_price),
            "price": _money(sp.current_price),
            "oup": _money(sp.current_oup),
            "price_per_measure": _money(sp.current_price_per_measure),
            "discount_pct": disc,
            "promos": promos,
            "url": sp.url,
            "last_seen": sp.last_seen_at.date().isoformat(),
        })

    return JsonResponse({
        "product": _product_dict(product),
        "offers": offers,
        "cheapest": offers[0] if offers else None,
        "store_count": len(offers),
    })


# ---------------------------------------------------------------------------
# TIME — price-over-time series per store (+ inflation stats)
# ---------------------------------------------------------------------------
@require_GET
def history(request, ean):
    days, error = _bounded_query_int(request, "days", 90, 1, 365)
    if error:
        return error
    product = get_object_or_404(Product, ean=ean)
    today = timezone.localdate()
    cutoff = today - timedelta(days=days)

    # Daily granularity is retained for 90 days (see retention policy). For a
    # longer window we prepend monthly aggregates so the chart still spans it.
    daily_cutoff = max(cutoff, today - timedelta(days=90))

    series: dict[str, dict] = {}   # slug -> {store, points: {date: oup}}

    daily = (
        PriceHistory.objects.filter(
            store_product__product=product, captured_at__gte=daily_cutoff
        )
        .select_related("store_product__store")
        .order_by("captured_at")
        .values("store_product__store__slug", "store_product__store__name",
                "captured_at", "oup")
    )
    for row in daily:
        slug = row["store_product__store__slug"]
        s = series.setdefault(slug, {"store": row["store_product__store__name"], "points": {}})
        s["points"][row["captured_at"].isoformat()] = _money(row["oup"])

    # Older-than-90-days months come from the archive (monthly avg).
    if cutoff < daily_cutoff:
        monthly = (
            PriceAggregate.objects.filter(
                store_product__product=product,
                period__gte=cutoff, period__lt=daily_cutoff,
            )
            .select_related("store_product__store")
            .order_by("period")
            .values("store_product__store__slug", "store_product__store__name",
                    "period", "avg_oup")
        )
        for row in monthly:
            slug = row["store_product__store__slug"]
            s = series.setdefault(slug, {"store": row["store_product__store__name"], "points": {}})
            s["points"].setdefault(row["period"].isoformat(), _money(row["avg_oup"]))

    # Shape into sorted arrays + a per-store inflation figure (first vs. last).
    out_series = []
    for slug, s in series.items():
        pts = [{"date": d, "oup": v} for d, v in sorted(s["points"].items()) if v is not None]
        change_pct = None
        if len(pts) >= 2:
            first, last = float(pts[0]["oup"]), float(pts[-1]["oup"])
            if first > 0:
                change_pct = round((last - first) / first * 100, 1)
        out_series.append({
            "slug": slug, "store": s["store"], "points": pts, "change_pct": change_pct,
        })

    out_series.sort(key=lambda x: x["store"])
    return JsonResponse({
        "product": _product_dict(product),
        "days": days,
        "series": out_series,
    })


# ---------------------------------------------------------------------------
# CART — optimize a shopping list across stores
# ---------------------------------------------------------------------------
@csrf_exempt          # token-authenticated JSON API in production; CSRF n/a
@require_POST
def cart_optimize(request):
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "JSON body must be an object"}, status=400)

    items = payload.get("items", [])
    if not isinstance(items, list):
        return JsonResponse({"error": "'items' must be a list"}, status=400)

    normalized_items = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return JsonResponse(
                {"error": f"items[{index}] must be an object"}, status=400
            )
        ean = str(item.get("ean", "")).strip()
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            return JsonResponse(
                {"error": f"items[{index}].quantity must be an integer"}, status=400
            )
        if not ean:
            return JsonResponse(
                {"error": f"items[{index}].ean is required"}, status=400
            )
        if quantity < 1:
            return JsonResponse(
                {"error": f"items[{index}].quantity must be at least 1"}, status=400
            )
        normalized_items.append({"ean": ean, "quantity": quantity})

    include_conditional = payload.get("include_conditional", False)
    if not isinstance(include_conditional, bool):
        return JsonResponse(
            {"error": "'include_conditional' must be a boolean"}, status=400
        )
    result = optimize_cart(normalized_items, include_conditional=include_conditional)
    return JsonResponse(result)
