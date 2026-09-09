"""Emergency bands derived from the on-chain liquidation threshold.

The shipped config had `ltv_cushion` exactly ON Arbitrum's real liquidation
threshold (0.79) and `ltv_deleverage` two points past it, because both were
calibrated against Ethereum's 0.81. Neither priority could fire before Aave
liquidated. These tests pin the property that replaced them: every threshold
is derived from the threshold actually read on-chain, so it cannot drift into
fiction when governance moves the parameter or when the chain differs.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from delta0.config import Config
from delta0.decision import (
    Bands,
    OperationalContext,
    bands_incoherence,
    decide,
    derive_bands,
)
from delta0.types import Priority, Snapshot

# Read on-chain 2026-09-08, block 503134105. See memory/aave_findings.md §9.
LT_ARBITRUM = 0.79
# The value the config was calibrated on — wstETH on Ethereum Core.
LT_ETHEREUM = 0.81


def test_bands_derive_from_the_on_chain_threshold(config: Config) -> None:
    bands = derive_bands(LT_ARBITRUM, config)
    assert bands.ltv_pump == pytest.approx(0.750)
    assert bands.ltv_cushion == pytest.approx(0.765)
    assert bands.ltv_deleverage == pytest.approx(0.775)


def test_bands_follow_a_governance_cut(config: Config) -> None:
    """A lowered threshold must move the bands, not silently invalidate them."""
    before = derive_bands(LT_ARBITRUM, config)
    after = derive_bands(0.75, config)  # governance cuts the threshold
    assert after.ltv_deleverage < before.ltv_deleverage
    # The distance to liquidation is what the config declares, so it is preserved.
    assert after.lt - after.ltv_deleverage == pytest.approx(before.lt - before.ltv_deleverage)


@pytest.mark.parametrize("lt", [0.75, 0.79, 0.81, 0.83, 0.93])
def test_every_band_leaves_room_before_liquidation(config: Config, lt: float) -> None:
    """Whatever the threshold, each priority triggers while the position lives."""
    bands = derive_bands(lt, config)
    for threshold in (bands.ltv_pump, bands.ltv_cushion, bands.ltv_deleverage):
        assert threshold < lt
    # Stated as health factors: every trigger sits strictly above liquidation.
    for hf_threshold in (bands.hf_pump, bands.hf_cushion, bands.hf_deleverage):
        assert hf_threshold > 1.0


def test_priorities_keep_their_order(config: Config) -> None:
    """Pump fires first, deleverage last — the slow path starts earliest."""
    bands = derive_bands(LT_ARBITRUM, config)
    assert bands.ltv_pump < bands.ltv_cushion < bands.ltv_deleverage
    assert bands.hf_pump > bands.hf_cushion > bands.hf_deleverage


def test_the_shipped_thresholds_were_past_liquidation() -> None:
    """The regression this chantier exists for, stated as arithmetic.

    Nothing to derive here: 0.79 and 0.81 against a liquidation threshold of
    0.79 leave zero and minus two points. Kept as a test so the numbers cannot
    quietly come back.
    """
    old_cushion, old_deleverage = 0.79, 0.81
    assert old_cushion - LT_ARBITRUM == pytest.approx(0.0)
    assert old_deleverage > LT_ARBITRUM
    # They were coherent on the chain they were calibrated for, which is how
    # they survived review.
    assert old_cushion < LT_ETHEREUM


def test_boot_refuses_when_the_pump_would_fire_at_rest(config: Config) -> None:
    """A threshold low enough to sit under the target must stop the boot."""
    # target_ltv 0.70 and a pump margin of 0.04 need LT > 0.74.
    problem = bands_incoherence(0.73, config)
    assert problem is not None
    assert "pump threshold" in problem
    assert bands_incoherence(LT_ARBITRUM, config) is None


def test_boot_refuses_an_unreadable_threshold(config: Config) -> None:
    """LT read as zero means the Aave data is missing, not that all is well."""
    assert bands_incoherence(0.0, config) is not None


def test_price_drop_to_a_band(config: Config) -> None:
    """The bands in the operator's language: how far the price may fall."""
    bands = derive_bands(LT_ARBITRUM, config)
    drop = bands.price_drop_to(bands.ltv_pump, config.target_ltv)
    assert drop == pytest.approx(1 - 0.70 / 0.75, abs=1e-6)  # -6.67 %


# --- The property that matters, end to end -----------------------------------


def _at_ltv(base: Snapshot, ltv: float) -> Snapshot:
    collateral = base.collateral_usd
    debt = ltv * collateral
    return replace(
        base,
        usdc_variable_debt_balance=debt,
        hf=base.aave_lt_wsteth * collateral / debt,
    )


def test_a_falling_price_meets_every_defence_before_liquidation(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    """Walk the LTV up to liquidation and check each priority gets its turn.

    This is the guarantee the old config could not offer: with the cushion on
    the threshold and the deleverage past it, a falling price met P6 and then
    nothing at all.
    """
    bands = derive_bands(stable_snapshot.aave_lt_wsteth, config)
    seen: list[Priority] = []
    ltv = config.target_ltv
    while ltv < stable_snapshot.aave_lt_wsteth:
        action = decide(_at_ltv(stable_snapshot, ltv), config, nominal_ctx)
        if action.priority not in seen and action.priority in (
            Priority.P6_PUMP_DOWN,
            Priority.P3_EMERGENCY_REPAY,
        ):
            seen.append(action.priority)
        ltv += 0.002

    # The pump gets its turn first, then the cushion — both strictly before
    # the health factor reaches 1.
    assert seen == [Priority.P6_PUMP_DOWN, Priority.P3_EMERGENCY_REPAY]
    assert bands.hf_cushion > 1.0


def test_deleverage_fires_before_liquidation_when_the_cushion_is_gone(
    stable_snapshot: Snapshot,
    config: Config,
    nominal_ctx: OperationalContext,
) -> None:
    depleted = replace(stable_snapshot, usdc_atoken_balance=0.0)
    bands = derive_bands(depleted.aave_lt_wsteth, config)
    snap = _at_ltv(depleted, bands.ltv_deleverage + 0.002)
    assert snap.hf > 1.0  # still alive when the defence triggers
    action = decide(snap, config, nominal_ctx)
    assert action.priority is Priority.P4_DELEVERAGE


def test_bands_is_frozen() -> None:
    bands = Bands(lt=0.79, ltv_pump=0.75, ltv_cushion=0.765, ltv_deleverage=0.775)
    with pytest.raises(AttributeError):
        bands.lt = 0.80  # type: ignore[misc]
