"""Startup check for pay-per-event charging with the apify Python SDK 4.x.

apify SDK 4.0.0-4.2.1 (src/apify/_charging.py) does not call the platform charge endpoint for an event whose
pricing info has only `eventTieredPricingUsd` and no flat `eventPriceUsd` (a "tier-priced" event): it logs
"... is tier-priced and is not chargeable via the pay-per-event API" and only counts the event locally.

`check_charge_events()` logs a loud WARNING at startup for every event this Actor charges that the SDK
would not bill. With `ENABLE_TIER_CHARGING = True` it also registers tier-priced events with the SDK so
the normal charge path calls the platform charge endpoint; the platform applies the caller's tier price.
The SDK only uses the registered price for its local budget arithmetic, so the highest tier price is
used (the budget can never be overrun; discounted users may stop a little before their limit).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from apify import Actor

ENABLE_TIER_CHARGING = False


def _raw_events(info) -> dict:
    ppe = getattr(info, "pricing_per_event", None)
    return dict(getattr(ppe, "actor_charge_events", None) or {})


def _register_tier_event(cm, event: str, raw) -> bool:
    try:
        from apify._charging import PricingInfoItem
    except ImportError:
        return False
    tiers = getattr(raw, "event_tiered_pricing_usd", None) or {}
    prices = [t.tiered_event_price_usd for t in tiers.values() if getattr(t, "tiered_event_price_usd", None)]
    store = getattr(cm, "_pricing_info", None)
    if not prices or not isinstance(store, dict):
        return False
    store[event] = PricingInfoItem(price=Decimal(str(max(prices))), title=raw.event_title)
    getattr(cm, "_tier_priced_events", set()).discard(event)
    return True


def check_charge_events(events: Iterable[str], *, enable_tier_charging: bool | None = None,
                        cm=None, pricing_info=None) -> dict[str, str]:
    """Return {event: status}; status is ok / tier-enabled / tier-priced-not-charged / unknown."""
    enable = ENABLE_TIER_CHARGING if enable_tier_charging is None else enable_tier_charging
    if cm is None:
        cm = Actor.get_charging_manager()
        pricing_info = Actor.configuration.actor_pricing_info
    info = cm.get_pricing_info()
    if not info.is_pay_per_event:
        return {}
    tiered = getattr(cm, "_tier_priced_events", set()) or set()
    raw = _raw_events(pricing_info)
    status: dict[str, str] = {}
    for ev in events:
        if ev in info.per_event_prices:
            status[ev] = "ok"
        elif ev in tiered and enable and _register_tier_event(cm, ev, raw.get(ev)):
            status[ev] = "tier-enabled"
            Actor.log.info(f"PPE: tier-priced event '{ev}' registered for charging (platform applies tier price).")
        elif ev in tiered:
            status[ev] = "tier-priced-not-charged"
            Actor.log.warning(
                f"!!! PPE WARNING: event '{ev}' has only tiered prices (no eventPriceUsd). "
                f"This apify SDK version will NOT charge it - results are delivered unbilled. !!!")
        else:
            status[ev] = "unknown"
            Actor.log.warning(
                f"!!! PPE WARNING: event '{ev}' is not in this Actor's pay-per-event pricing; "
                f"it will NOT be charged. !!!")
    return status
