"""PPE guard: tier-priced events are reported, and charged through the platform API when enabled."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncio

import pytest
from apify_client._models import PayPerEventActorPricingInfo

from apify._charging import ChargingManagerImplementation
from apify._configuration import Configuration

from src import ppe_guard

TIERS = {"FREE": 0.01, "BRONZE": 0.009, "SILVER": 0.008, "GOLD": 0.007, "PLATINUM": 0.007, "DIAMOND": 0.007}


def _pricing(event, tiered=True):
    ev = {"eventTitle": "Row", "eventDescription": ""}
    if tiered:
        ev["eventTieredPricingUsd"] = {k: {"tieredEventPriceUsd": v} for k, v in TIERS.items()}
    else:
        ev["eventPriceUsd"] = 0.01
    return PayPerEventActorPricingInfo.model_validate({
        "pricingModel": "PAY_PER_EVENT", "apifyMarginPercentage": 0.2,
        "createdAt": "2026-11-05T00:00:00.000Z", "startedAt": "2026-11-05T00:00:00.000Z",
        "pricingPerEvent": {"actorChargeEvents": {event: ev}}})


def _cm(pricing, budget="1"):
    cfg = Configuration(is_at_home=True, actor_run_id="RUN", purge_on_start=False,
                        max_total_charge_usd=Decimal(budget))
    object.__setattr__(cfg, "actor_pricing_info", pricing)
    object.__setattr__(cfg, "charged_event_counts", {"row": 0})
    run = MagicMock(get=AsyncMock(return_value=None), charge=AsyncMock())
    client = MagicMock(run=MagicMock(return_value=run))
    return cfg, ChargingManagerImplementation(cfg, client), run


@pytest.mark.parametrize("enable,charged", [(False, False), (True, True)])
def test_tiered_event(enable, charged):
    asyncio.run(_tiered(enable, charged))


async def _tiered(enable, charged):
    cfg, cm, run = _cm(_pricing("row"))
    async with cm:
        st = ppe_guard.check_charge_events(["row"], enable_tier_charging=enable, cm=cm,
                                           pricing_info=cfg.actor_pricing_info)
        assert st == {"row": "tier-enabled" if enable else "tier-priced-not-charged"}
        await cm.charge("row", count=2)
        assert run.charge.await_count == (1 if charged else 0)
        if charged:
            assert cm.get_pricing_info().per_event_prices["row"] == Decimal("0.01")  # highest tier, safe budget


def test_flat_event_untouched():
    asyncio.run(_flat())


async def _flat():
    cfg, cm, run = _cm(_pricing("row", tiered=False))
    async with cm:
        assert ppe_guard.check_charge_events(["row", "typo"], cm=cm, pricing_info=cfg.actor_pricing_info) == {"row": "ok", "typo": "unknown"}
        await cm.charge("row")
        assert run.charge.await_count == 1
