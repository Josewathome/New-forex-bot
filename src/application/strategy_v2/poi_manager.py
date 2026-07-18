# src/application/strategy_v2/poi_manager.py
#
# POI Manager — aggregates FVG/Order Block/Liquidity zones into unified,
# queryable per-symbol storage with active/expired lifecycle tracking,
# including CHoCH-driven cleanup. Pure composition over the already-
# frozen fvg.py/order_blocks.py/liquidity.py primitives -- this module
# never reimplements their detection/mitigation/invalidation/sweep logic,
# it only stores the zones they produce, evolves them forward via their
# own existing update functions, and layers a manager-local "expired via
# CHoCH" flag on top. The frozen zone dataclasses (FVGZone, OrderBlockZone,
# LiquidityLevel) are never extended with new fields here.
#
# DOCUMENTED IMPLEMENTATION DECISIONS (this module's own scope, no
# structure/FVG/OB/liquidity logic touched or re-litigated):
#
#   M1. Storage is per-symbol, three parallel lists (fvg/ob/liquidity),
#       each wrapped in a lightweight POIRecord that adds manager-local
#       bookkeeping (expired) without touching the frozen zone dataclasses.
#   M2. "Active" = the zone's own tradability (fvg.is_tradable /
#       order_blocks.is_tradable / not level.swept) AND not manager-
#       expired. "Expired" zones are excluded from active_*() but kept in
#       storage (not deleted) for audit/diagnostics, same discipline as
#       structure.py's D4 (swing_log preserved, never deleted).
#   M3. Manager-level "expired" applies only to FVG/OB zones (which carry
#       a Direction aligned with trend) -- NOT to liquidity levels, whose
#       validity is independent of trend state (a resting Asian high is
#       still real liquidity regardless of what the structure engine's
#       trend is doing). Liquidity levels' own one-way `swept` flag is
#       their only lifecycle state.
#   M4. CHoCH cleanup: when a CHoCH fires invalidating trend T, every
#       currently-active FVG/OB zone whose direction corresponds to T
#       (BULLISH CHoCH-invalidated -> Direction.BULLISH zones expire;
#       BEARISH mirrors) is marked expired. This reflects standard SMC
#       practice: once the trend a zone was formed under is invalidated,
#       that zone is no longer a valid same-direction continuation POI.
#       Zones of the OPPOSITE direction are left untouched -- they may be
#       the leading edge of the new counter-trend structure forming after
#       the CHoCH.
#   M5. add_fvg/add_ob/add_liquidity store the zone object as given, no
#       dedup-by-value -- two genuinely distinct zones can share identical
#       bounds (e.g. an OB and its origin-candle FVG), so callers are
#       responsible for not adding the same zone object twice.

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List

from src.domain.entities import Candle, Direction
from src.domain.strategy_v2_entities import FVGZone, OrderBlockZone, LiquidityLevel, TrendState
from src.application.strategy_v2 import fvg as fvg_mod
from src.application.strategy_v2 import order_blocks as ob_mod
from src.application.strategy_v2 import liquidity as liq_mod

_TREND_TO_DIRECTION = {
    TrendState.BULLISH: Direction.BULLISH,
    TrendState.BEARISH: Direction.BEARISH,
}


@dataclass
class POIRecord:
    zone: object          # FVGZone | OrderBlockZone
    expired: bool = False


class POIManager:
    """One instance per symbol. Owns no structure/FVG/OB/liquidity
    detection logic -- callers detect zones with the existing frozen
    functions and hand the resulting objects to add_fvg/add_ob/
    add_liquidity; this class stores them, evolves them forward one
    candle at a time via update(), and answers active/expired queries."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.fvg_records: List[POIRecord] = []
        self.ob_records: List[POIRecord] = []
        self.liquidity_levels: List[LiquidityLevel] = []

    def add_fvg(self, zone: FVGZone) -> None:
        self.fvg_records.append(POIRecord(zone=zone))

    def add_ob(self, zone: OrderBlockZone) -> None:
        self.ob_records.append(POIRecord(zone=zone))

    def add_liquidity(self, level: LiquidityLevel) -> None:
        self.liquidity_levels.append(level)

    def update(self, candle: Candle) -> None:
        """Evolves every stored zone forward exactly one candle, via each
        zone type's own existing (frozen) update function. Call once per
        new candle close, after any new zones for that candle have been
        added."""
        for rec in self.fvg_records:
            fvg_mod.update_fvg_state(rec.zone, candle)
        for rec in self.ob_records:
            ob_mod.update_ob_state(rec.zone, candle)
        min_pen = liq_mod.min_sweep_penetration(self.symbol)
        for level in self.liquidity_levels:
            liq_mod.update_sweep_state(level, candle, min_pen)

    def on_choch(self, invalidated_trend: TrendState) -> None:
        """M4 -- marks active same-direction FVG/OB zones expired.
        invalidated_trend is the trend the CHoCH just ended (StructureEvent.
        invalidated_trend), not the new trend (always NEUTRAL after CHoCH)."""
        direction = _TREND_TO_DIRECTION.get(invalidated_trend)
        if direction is None:
            return  # invalidated_trend should always be BULLISH or BEARISH; NEUTRAL is a no-op, not an error
        for rec in self.fvg_records:
            if not rec.expired and rec.zone.direction == direction and fvg_mod.is_tradable(rec.zone):
                rec.expired = True
        for rec in self.ob_records:
            if not rec.expired and rec.zone.direction == direction and ob_mod.is_tradable(rec.zone):
                rec.expired = True

    def active_fvgs(self) -> List[FVGZone]:
        return [r.zone for r in self.fvg_records if not r.expired and fvg_mod.is_tradable(r.zone)]

    def expired_fvgs(self) -> List[FVGZone]:
        return [r.zone for r in self.fvg_records if r.expired or not fvg_mod.is_tradable(r.zone)]

    def active_obs(self) -> List[OrderBlockZone]:
        return [r.zone for r in self.ob_records if not r.expired and ob_mod.is_tradable(r.zone)]

    def expired_obs(self) -> List[OrderBlockZone]:
        return [r.zone for r in self.ob_records if r.expired or not ob_mod.is_tradable(r.zone)]

    def active_liquidity(self, as_of_time) -> List[LiquidityLevel]:
        return liq_mod.active_unswept_levels(self.liquidity_levels, as_of_time)

    def expired_liquidity(self) -> List[LiquidityLevel]:
        return [lv for lv in self.liquidity_levels if lv.swept]
