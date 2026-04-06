# backend/apps/common/intelligence_engine.py
"""
Option Intelligence Engine
===========================
Calculates a 7-variable scoring model for options-based trading signals.
Outputs are per-underlying (BTCUSDT, ETHUSDT, etc.) with per-strike granularity.

Canonical Data Contracts (from normalizers.py):
    Trade: {symbol, price, qty, side, trade_type, timestamp}
    OI:    {symbol, oi_contracts, oi_usd, timestamp}
    Mark:  {s, mp, i, vo, g, d, t, v, b, a, E, ...}  (WS fields, unchanged)
"""

import datetime
import logging
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

import numpy as np

logger = logging.getLogger("intelligence.engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NEAR_MONEY_PCT = 0.10  # ±10% of spot for wall/skew calculations
NUMPY_EMPTY_FALLBACK = 0.0

# Scoring weights — all are now ACTIVE in run_analysis()
WEIGHTS = {
    "whale": 40,
    "pcr": 20,
    "max_pain": 15,
    "walls": 15,
    "gex": 10,
    "skew": 10,
}

# Thresholds — all configurable in one place
THRESHOLDS = {
    "whale_block_usd": 50000,  # Minimum block trade notional to qualify
    "pcr_bullish": 1.0,  # PCR above this → bullish
    "pcr_bearish": 0.7,  # PCR below this → bearish
    "gex_positive": 0,  # GEX above this → bullish
    "max_pain_above_pct": 0.01,  # Max pain > spot × (1 + this) → bullish
    "max_pain_below_pct": 0.01,  # Max pain < spot × (1 - this) → bearish
    "skew_bearish": -0.15,  # Skew below this → bearish (extreme put premium)
    "skew_bullish": 0.15,  # Skew above this → bullish (extreme call premium)
}


class OptionIntelligenceEngine:
    """
    Analyzes a single underlying asset (e.g., BTCUSDT) and produces:
      - Composite score from -100 to +100
      - Signal label (STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL)
      - Per-strike breakdown with call/put separation
      - Whale activity with direction
      - Data freshness metadata
    """

    def __init__(self):
        self.weights = WEIGHTS.copy()
        self.thresholds = THRESHOLDS.copy()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_analysis(
        self,
        asset: str,
        index_price: float,
        mark_data: List[Dict],
        oi_data: List[Dict],
        trade_data: List[Dict],
        data_timestamps: Optional[Dict[str, float]] = None,
        contract_sizes: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        Main entry point. All inputs must use canonical field names
        (normalized by normalizers.py at write time).

        Parameters
        ----------
        asset : str              e.g. "BTCUSDT"
        index_price : float      Current index price
        mark_data : list[dict]   Mark price events (canonical WS fields: s, mp, vo, g, d, ...)
        oi_data : list[dict]     OI records (canonical: symbol, oi_contracts, oi_usd, timestamp)
        trade_data : list[dict]  Trades (canonical: symbol, price, qty, side, trade_type, timestamp)
        data_timestamps : dict   Optional freshness info: {mark_price_age_ms, oi_age_ms, ...}

        Returns
        -------
        dict  Full intelligence report
        """
        # 1. ISOLATION: ensure all items belong to this asset only
        asset_prefix = asset.split("USDT")[0].upper()

        mark_data = self._filter_by_asset(mark_data, asset_prefix, key="s")
        oi_data = self._filter_by_asset(oi_data, asset_prefix, key="symbol")
        trade_data = self._filter_by_asset(trade_data, asset_prefix, key="symbol")

        # 1b. EXPIRY FILTER: restrict to nearest 2 expirations
        # Far-dated contracts contaminate short-term signals (GEX, Walls,
        # Skew, Max Pain) because their OI/IV profiles differ significantly.
        # A1 already ensures OI is nearest-expiry only; this aligns mark_data.
        mark_data, oi_data = self._filter_by_expiry(
            mark_data, oi_data, max_expiries=2
        )

        if not mark_data or index_price <= 0:
            return {
                "status": "insufficient_data",
                "asset": asset,
                "data_timestamps": data_timestamps,
            }

        spot = float(index_price)

        # 1c. IV FORMAT AUTO-DETECTION
        # Binance 'vo' (implied volatility) may be decimal (0.65) or
        # percentage (65.0). Detect once and normalize all downstream.
        iv_scale = self._detect_iv_format(mark_data)
        # Normalize vo in-place so all sub-methods use consistent decimal IV.
        # mark_data is a fresh list per trigger — safe to mutate.
        for m in mark_data:
            raw_vo = m.get("vo", 0)
            if isinstance(raw_vo, (int, float)) and raw_vo > 0:
                m["vo"] = float(raw_vo) * iv_scale

        # 1d. CONTRACT SIZES from exchangeInfo (default 1.0 if unavailable)
        cs = contract_sizes if contract_sizes is not None else {}

        # 2. VARIABLE CALCULATIONS (filtered data only)
        pcr = self._calculate_pcr(oi_data)
        max_pain = self._calculate_max_pain(oi_data, spot)
        walls = self._calculate_walls(oi_data, spot)
        whale_buy, whale_sell = self._calculate_whale_activity(trade_data, cs)
        total_gex = self._calculate_gex(mark_data, oi_data, spot, cs)
        skew = self._calculate_skew(mark_data, spot)
        # avg_iv output is in PERCENTAGE (e.g., 65.0 for 65% IV)
        avg_iv = self._safe_mean(
            [float(x.get("vo", 0)) * 100 for x in mark_data if float(x.get("vo", 0)) > 0]
        )

        # 3. TWO-DIRECTIONAL SCORING
        score, reasons, max_possible_pos, max_possible_neg = self._compute_score(
            spot=spot,
            pcr=pcr,
            max_pain=max_pain,
            walls=walls,
            whale_buy=whale_buy,
            whale_sell=whale_sell,
            total_gex=total_gex,
            skew=skew,
        )

        # 4. PER-STRIKE GRANULARITY
        strike_analysis = self._build_strike_analysis(mark_data, oi_data, spot, cs)
        top_symbols = self._build_top_symbols(mark_data, oi_data, spot, cs)
        oi_concentration = self._calculate_oi_concentration(oi_data)
        nearest_expiry = self._find_nearest_expiry(mark_data)

        # 5. STALE DETECTION
        stale = []
        if data_timestamps:
            for metric_key, age_ms in data_timestamps.items():
                if age_ms > 300_000:  # 5 minutes
                    stale.append(metric_key)

        # Asymmetric normalization: use direction-appropriate denominator
        # so scores always map cleanly to -100..+100 range.
        # max_pos = best possible positive given available data
        # max_neg = worst possible negative (as positive) given available data
        if score >= 0 and max_possible_pos > 0:
            normalized_score = round((score / max_possible_pos) * 100, 2)
        elif score < 0 and max_possible_neg > 0:
            normalized_score = round((score / max_possible_neg) * 100, 2)
        else:
            normalized_score = 0.0

        # Safety clamp — should rarely trigger
        normalized_score = max(-100.0, min(100.0, normalized_score))

        return {
            "asset": asset,
            "score": normalized_score,
            "raw_score": score,
            "max_possible_positive": round(max_possible_pos, 2),
            "max_possible_negative": round(max_possible_neg, 2),
            "signal": self._get_signal_label(normalized_score),
            "index_price": spot,
            "stale": stale,
            "data_timestamps": data_timestamps,
            "metrics": {
                "pcr": round(pcr, 4),
                "max_pain": max_pain,
                "whale_buy_volume": round(whale_buy, 2),
                "whale_sell_volume": round(whale_sell, 2),
                "whale_net_volume": round(whale_buy - whale_sell, 2),
                "total_gex": round(total_gex, 0),
                "avg_iv": round(avg_iv, 6),
                "skew": round(skew, 6),
                "support": walls["support"],
                "support_secondary": walls["support_secondary"],
                "resistance": walls["resistance"],
                "resistance_secondary": walls["resistance_secondary"],
            },
            "strike_analysis": strike_analysis,
            "top_symbols": top_symbols,
            "oi_concentration": round(oi_concentration, 4),
            "nearest_expiry_days": nearest_expiry,
            "reasons": reasons if reasons else ["Neutral"],
        }

    # ------------------------------------------------------------------
    # Scoring Logic — Two-Directional
    # ------------------------------------------------------------------

    def _compute_score(
        self,
        spot: float,
        pcr: float,
        max_pain: float,
        walls: Dict,
        whale_buy: float,
        whale_sell: float,
        total_gex: float,
        skew: float,
    ) -> Tuple[float, List[str], float, float]:
        """
        Compute raw score based on all 6 active variables.

        Returns
        -------
        (score, reasons, max_possible_pos, max_possible_neg)
        - score   : raw weighted score (may be asymmetric)
        - reasons : list of human-readable signal explanations
        - max_possible_pos : best possible positive score given available data
        - max_possible_neg : worst possible negative score as a positive number

        Dynamic normalization principle
        ---------------------------
        Each variable is only counted toward max_possible if its input data
        actually exists. This prevents scores from being artificially deflated
        when e.g. no block trades exist (whale can't fire → max drops by 40).

        Walls are asymmetric: best positive = +weight (testing support + broken
        resistance = 0.5w + 0.5w), worst negative = −1.5×weight (broken support
        + testing resistance = −w + −0.5w). Both directions are tracked separately.
        """
        score = 0.0
        reasons = []
        t = self.thresholds
        w = self.weights

        max_pos = 0.0   # theoretical best positive
        max_neg = 0.0   # theoretical worst negative (stored as positive)

        # --- PCR (always available — OI is mandatory for intelligence) ---
        max_pos += w["pcr"]
        max_neg += w["pcr"]
        if pcr > t["pcr_bullish"]:
            score += w["pcr"]
            reasons.append(f"Bullish PCR ({pcr:.2f})")
        elif pcr < t["pcr_bearish"]:
            score -= w["pcr"]
            reasons.append(f"Bearish PCR ({pcr:.2f})")

        # --- Whale Activity (directional max tracking) ---
        # Only inflate max_possible in directions where whale actually fires.
        # Previous: any block trade data (buy>0 or sell>0) added ±40 to both
        # max_pos and max_neg, inflating the normalization denominator even
        # when no whale signal triggered (e.g., $10k buy < $50k threshold).
        whale_buy_fires = whale_buy > t["whale_block_usd"]
        whale_sell_fires = whale_sell > t["whale_block_usd"]
        has_whale_data = whale_buy > 0 or whale_sell > 0
        if has_whale_data:
            if whale_buy_fires:
                max_pos += w["whale"]
            if whale_sell_fires:
                max_neg += w["whale"]
        if whale_buy_fires:
            score += w["whale"]
            reasons.append(f"Whale Buy Pressure (${whale_buy:,.0f})")
        if whale_sell_fires:
            score -= w["whale"]
            reasons.append(f"Whale Sell Pressure (${whale_sell:,.0f})")

        # --- GEX (always available — mark_data + OI are mandatory) ---
        max_pos += w["gex"]
        max_neg += w["gex"]
        if total_gex > t["gex_positive"]:
            score += w["gex"]
            reasons.append("Positive GEX (MM Gamma Support)")
        elif total_gex < 0:
            score -= w["gex"]
            reasons.append("Negative GEX (MM Gamma Expiry)")

        # --- Max Pain (always available — OI is mandatory) ---
        max_pos += w["max_pain"]
        max_neg += w["max_pain"]
        if max_pain > 0:
            pain_ratio = (max_pain - spot) / spot
            if pain_ratio > t["max_pain_above_pct"]:
                score += w["max_pain"]
                reasons.append(f"Max Pain Above Spot ({max_pain:,.0f})")
            elif pain_ratio < -t["max_pain_below_pct"]:
                score -= w["max_pain"]
                reasons.append(f"Max Pain Below Spot ({max_pain:,.0f})")

        # --- Walls (Support/Resistance) ---
        # Only counted if near-the-money walls exist in OI data.
        # Asymmetric: best positive = +w (testing support + broken resistance),
        #             worst negative = −1.5w (broken support + testing resistance).
        support = walls.get("support", 0)
        resistance = walls.get("resistance", 0)
        has_walls = support > 0 or resistance > 0
        if has_walls:
            max_pos += w["walls"]          # 0.5w + 0.5w = w
            max_neg += w["walls"] * 1.5    # -w + -0.5w = -1.5w

        if support > 0 and spot < support * 0.98:
            score -= w["walls"]
            reasons.append(f"Support Broken ({support:,.0f})")
        elif support > 0 and spot > support * 0.98 and spot < support * 1.02:
            score += w["walls"] * 0.5
            reasons.append(f"Testing Support ({support:,.0f})")

        if resistance > 0 and spot > resistance * 1.02:
            score += w["walls"] * 0.5
            reasons.append(f"Resistance Broken ({resistance:,.0f})")
        elif resistance > 0 and spot > resistance * 0.98 and spot < resistance * 1.02:
            score -= w["walls"] * 0.5
            reasons.append(f"Testing Resistance ({resistance:,.0f})")

        # --- Skew (always available — mark_data is mandatory) ---
        max_pos += w["skew"]
        max_neg += w["skew"]
        if skew > t["skew_bullish"]:
            score += w["skew"]
            reasons.append(f"Bullish IV Skew ({skew:.4f})")
        elif skew < t["skew_bearish"]:
            score -= w["skew"]
            reasons.append(f"Bearish IV Skew ({skew:.4f})")

        return score, reasons, max_pos, max_neg

    def _get_signal_label(self, normalized_score: float) -> str:
        if normalized_score >= 50:
            return "STRONG BUY"
        if normalized_score >= 15:
            return "BUY"
        if normalized_score <= -50:
            return "STRONG SELL"
        if normalized_score <= -15:
            return "SELL"
        return "NEUTRAL"

    # ------------------------------------------------------------------
    # Variable Calculations
    # ------------------------------------------------------------------

    def _calculate_pcr(self, oi_data: List[Dict]) -> float:
        """
        Put/Call Ratio using OI in contracts.
        PCR > 1 = more put OI = potentially bullish (hedging).
        PCR < 0.7 = more call OI = potentially bearish.
        """
        calls = 0.0
        puts = 0.0
        for x in oi_data:
            sym = x.get("symbol", "")
            val = x.get("oi_contracts", 0)
            if "-C" in sym:
                calls += float(val)
            elif "-P" in sym:
                puts += float(val)
        if calls > 0:
            return puts / calls
        # calls=0 with puts>0 → infinite PCR (extreme bullish hedging).
        # Return 5.0 (well above pcr_bullish=1.0) to correctly signal bullish.
        # calls=0 with puts=0 → no OI data at all, return neutral 1.0.
        return 5.0 if puts > 0 else 1.0

    def _calculate_max_pain(self, oi_data: List[Dict], spot: float) -> float:
        """
        TRUE Max Pain: The strike price at which the total dollar value of
        ALL outstanding options expiring out-of-the-money is MINIMIZED.
        This is the strike where option writers (sellers) lose the least money.

        For each candidate strike K:
          - For every CALL with strike < K: it expires ITM, cost = (K - call_strike) × qty
          - For every PUT with strike > K: it expires ITM, cost = (put_strike - K) × qty
          - Sum all ITM costs = total payout at strike K
        Max Pain = K that minimizes this total payout.
        """
        if not oi_data:
            return 0

        # Build per-strike OI maps using oi_usd-weighted approach
        # But for true Max Pain we need contracts × strike delta
        call_strikes = {}  # {strike_price: total_oi_contracts}
        put_strikes = {}  # {strike_price: total_oi_contracts}

        for x in oi_data:
            sym = x.get("symbol", "")
            parts = sym.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            oi_val = float(x.get("oi_contracts", 0))
            if oi_val <= 0:
                continue

            if "-C" in sym:
                call_strikes[strike] = call_strikes.get(strike, 0) + oi_val
            elif "-P" in sym:
                put_strikes[strike] = put_strikes.get(strike, 0) + oi_val

        if not call_strikes and not put_strikes:
            return 0

        # Collect all unique strikes as candidate Max Pain prices
        all_strikes = sorted(set(list(call_strikes.keys()) + list(put_strikes.keys())))

        if not all_strikes:
            return 0

        min_total_payout = float("inf")
        max_pain_strike = all_strikes[len(all_strikes) // 2]  # default to middle

        for candidate_k in all_strikes:
            total_payout = 0.0

            # Calls: ITM when strike < candidate_K. Payout = (K - strike) × contracts
            for call_strike, call_oi in call_strikes.items():
                if call_strike < candidate_k:
                    total_payout += (candidate_k - call_strike) * call_oi

            # Puts: ITM when strike > candidate_K. Payout = (strike - K) × contracts
            for put_strike, put_oi in put_strikes.items():
                if put_strike > candidate_k:
                    total_payout += (put_strike - candidate_k) * put_oi

            if total_payout < min_total_payout:
                min_total_payout = total_payout
                max_pain_strike = candidate_k

        return max_pain_strike

    def _calculate_walls(self, oi_data: List[Dict], spot: float) -> Dict:
        """
        Find support and resistance from OI concentrations.
        Filters to near-the-money strikes (±10% of spot) for relevance.
        Returns top 2 support (puts) and top 2 resistance (calls).
        """
        lower_bound = spot * (1 - NEAR_MONEY_PCT)
        upper_bound = spot * (1 + NEAR_MONEY_PCT)

        call_strikes = []  # [(strike, oi)]
        put_strikes = []  # [(strike, oi)]

        for x in oi_data:
            sym = x.get("symbol", "")
            parts = sym.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue

            if not (lower_bound <= strike <= upper_bound):
                continue

            oi_val = float(x.get("oi_contracts", 0))

            if "-C" in sym and oi_val > 0:
                call_strikes.append((strike, oi_val))
            elif "-P" in sym and oi_val > 0:
                put_strikes.append((strike, oi_val))

        # DIRECTION FILTER: Support must be BELOW spot, Resistance must be ABOVE spot.
        # Without this, a put wall ABOVE spot gets labeled "support" (wrong —
        # it's a ceiling) and a call wall BELOW spot gets labeled "resistance"
        # (wrong — it's a floor). This directly corrupts the wall scoring signals.
        put_supports = [(s, o) for s, o in put_strikes if s <= spot]
        call_resistances = [(s, o) for s, o in call_strikes if s >= spot]

        # Sort by OI descending
        call_resistances.sort(key=lambda x: x[1], reverse=True)
        put_supports.sort(key=lambda x: x[1], reverse=True)

        return {
            "resistance": call_resistances[0][0] if call_resistances else 0,
            "resistance_secondary": call_resistances[1][0] if len(call_resistances) > 1 else 0,
            "support": put_supports[0][0] if put_supports else 0,
            "support_secondary": put_supports[1][0] if len(put_supports) > 1 else 0,
        }

    def _calculate_whale_activity(
        self, trade_data: List[Dict], contract_sizes: Dict[str, float] = None
    ) -> Tuple[float, float]:
        """
        Separate whale BUY and SELL volume from block trades.
        Returns (buy_volume, sell_volume) in USDT notional.
        Notional = price × qty × contractSize (from exchangeInfo).
        """
        buy_volume = 0.0
        sell_volume = 0.0
        cs = contract_sizes or {}

        for t in trade_data:
            if t.get("trade_type") != "BLOCK":
                continue
            sym = t.get("symbol", "")
            cs_val = cs.get(sym, 1.0)
            notional = float(t.get("price", 0)) * float(t.get("qty", 0)) * cs_val
            side = t.get("side", "").upper()
            if side == "BUY":
                buy_volume += notional
            elif side == "SELL":
                sell_volume += notional

        return buy_volume, sell_volume

    def _calculate_gex(
        self, mark_data: List[Dict], oi_data: List[Dict], spot: float,
        contract_sizes: Dict[str, float] = None,
    ) -> float:
        """
        Gamma Exposure approximation.
        GEX = Σ (gamma × OI_contracts × contractSize × spot² × 0.01) × side
        Positive GEX = market maker is long gamma → suppresses volatility.
        Negative GEX = market maker is short gamma → amplifies volatility.

        contractSize from exchangeInfo (1 for BTC/ETH current market).
        """
        cs_map = contract_sizes or {}

        # Build OI lookup from canonical data
        oi_map = {}
        for x in oi_data:
            sym = x.get("symbol", "")
            oi_map[sym] = float(x.get("oi_contracts", 0))

        gex = 0.0
        for m in mark_data:
            sym = m.get("s", "")
            oi_val = oi_map.get(sym, 0)
            if oi_val <= 0:
                continue

            gamma = float(m.get("g", 0))
            if gamma == 0:
                continue

            cs_val = cs_map.get(sym, 1.0)
            side = 1 if "-C" in sym else -1
            gex += (gamma * oi_val * cs_val * (spot**2) * 0.01) * side

        return gex

    def _calculate_skew(self, mark_data: List[Dict], spot: float) -> float:
        """
        ATM Volatility Skew: difference between call IV and put IV
        at the At-The-Money strike (closest to current spot).

        Positive skew = calls more expensive than puts → bullish sentiment.
        Negative skew = puts more expensive than calls → bearish sentiment / hedging.
        """
        if not mark_data or spot <= 0:
            return 0.0

        # Find ATM strike (closest to spot)
        atm_strike = None
        min_delta = float("inf")
        for m in mark_data:
            sym = m.get("s", "")
            parts = sym.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            delta = abs(strike - spot)
            if delta < min_delta:
                min_delta = delta
                atm_strike = strike

        if atm_strike is None:
            return 0.0

        # Collect call and put IVs within ±2% of ATM for interpolation robustness
        tolerance = spot * 0.02
        call_ivs = []
        put_ivs = []
        for m in mark_data:
            sym = m.get("s", "")
            parts = sym.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            if abs(strike - atm_strike) > tolerance:
                continue

            iv = float(m.get("vo", 0))
            if iv <= 0:
                continue

            if "-C" in sym:
                call_ivs.append(iv)
            elif "-P" in sym:
                put_ivs.append(iv)

        call_avg = self._safe_mean(call_ivs)
        put_avg = self._safe_mean(put_ivs)

        return call_avg - put_avg

    # ------------------------------------------------------------------
    # Per-Strike Granularity
    # ------------------------------------------------------------------

    def _build_strike_analysis(
        self, mark_data: List[Dict], oi_data: List[Dict], spot: float,
        contract_sizes: Dict[str, float] = None,
    ) -> List[Dict]:
        """
        Build per-strike breakdown for trade decisions.
        Each strike shows call vs put separation for OI, IV, and GEX.
        Only includes strikes within ±20% of spot for relevance.
        IV output is in percentage format (e.g., 65.0 for 65%).
        """
        cs_map = contract_sizes or {}
        lower = spot * 0.80
        upper = spot * 1.20

        # Build OI map
        oi_map = {}
        for x in oi_data:
            sym = x.get("symbol", "")
            oi_map[sym] = {
                "oi_contracts": float(x.get("oi_contracts", 0)),
                "oi_usd": float(x.get("oi_usd", 0)),
            }

        # Aggregate per strike
        strikes_data = defaultdict(
            lambda: {
                "call_oi": 0.0,
                "put_oi": 0.0,
                "call_iv": [],
                "put_iv": [],
                "call_gex": 0.0,
                "put_gex": 0.0,
            }
        )

        for m in mark_data:
            sym = m.get("s", "")
            parts = sym.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue

            if not (lower <= strike <= upper):
                continue

            bucket = strikes_data[strike]
            oi_info = oi_map.get(sym, {})
            oi_val = oi_info.get("oi_contracts", 0)

            gamma = float(m.get("g", 0))
            iv = float(m.get("vo", 0))
            # Note: 'v' in mark price = vega (Greek), NOT volume. Volume only exists in ticker stream.
            vega = float(m.get("v", 0))

            if "-C" in sym:
                bucket["call_oi"] += oi_val
                bucket["call_gex"] += (
                    (gamma * oi_val * cs_map.get(sym, 1.0) * (spot**2) * 0.01)
                    if oi_val > 0 else 0
                )
                if iv > 0:
                    bucket["call_iv"].append(iv * 100)  # percentage
            elif "-P" in sym:
                bucket["put_oi"] += oi_val
                bucket["put_gex"] -= (
                    (gamma * oi_val * cs_map.get(sym, 1.0) * (spot**2) * 0.01)
                    if oi_val > 0 else 0
                )
                if iv > 0:
                    bucket["put_iv"].append(iv * 100)  # percentage

        # Build sorted output
        result = []
        for strike in sorted(strikes_data.keys()):
            b = strikes_data[strike]
            call_oi = b["call_oi"]
            put_oi = b["put_oi"]
            result.append(
                {
                    "strike": strike,
                    "distance_pct": round((strike - spot) / spot * 100, 2),
                    "call_oi": round(call_oi, 2),
                    "put_oi": round(put_oi, 2),
                    "net_oi": round(call_oi - put_oi, 2),
                    "call_iv": round(self._safe_mean(b["call_iv"]), 6),
                    "put_iv": round(self._safe_mean(b["put_iv"]), 6),
                    "net_gex": round(b["call_gex"] + b["put_gex"], 2),
                }
            )

        return result

    def _build_top_symbols(
        self, mark_data: List[Dict], oi_data: List[Dict], spot: float,
        contract_sizes: Dict[str, float] = None,
    ) -> List[Dict]:
        """
        Top 5 most active individual contracts by gamma exposure.
        IV output is in percentage format (e.g., 65.0 for 65%).
        """
        cs_map = contract_sizes or {}
        oi_map = {}
        for x in oi_data:
            sym = x.get("symbol", "")
            oi_map[sym] = float(x.get("oi_contracts", 0))

        symbol_data = []
        for m in mark_data:
            sym = m.get("s", "")
            oi_val = oi_map.get(sym, 0)
            if oi_val <= 0:
                continue

            parts = sym.split("-")
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue

            gamma = float(m.get("g", 0))
            delta = float(m.get("d", 0))
            iv = float(m.get("vo", 0))
            side = "CALL" if "-C" in sym else "PUT"

            cs_val = cs_map.get(sym, 1.0)
            symbol_data.append(
                {
                    "symbol": sym,
                    "side": side,
                    "strike": strike,
                    "delta": round(delta, 6),
                    "gamma": round(gamma, 8),
                    "iv": round(iv * 100, 2),  # percentage
                    "oi": round(oi_val, 2),
                    "gex": round(
                        gamma
                        * oi_val
                        * cs_val
                        * (spot**2)
                        * 0.01
                        * (1 if side == "CALL" else -1),
                        2,
                    ),
                    "distance_pct": round((strike - spot) / spot * 100, 2),
                }
            )

        # Sort by absolute GEX contribution (highest impact)
        symbol_data.sort(key=lambda x: abs(x["gex"]), reverse=True)
        return symbol_data[:5]

    def _calculate_oi_concentration(self, oi_data: List[Dict]) -> float:
        """
        Percentage of total OI concentrated in the top 3 strikes (by contracts).
        High concentration (>60%) = heavy institutional positioning at specific levels.
        """
        if not oi_data:
            return 0.0

        strike_oi = defaultdict(float)
        total_oi = 0.0
        for x in oi_data:
            sym = x.get("symbol", "")
            parts = sym.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            val = float(x.get("oi_contracts", 0))
            strike_oi[strike] += val
            total_oi += val

        if total_oi <= 0:
            return 0.0

        top_3 = sorted(strike_oi.values(), reverse=True)[:3]
        return sum(top_3) / total_oi

    def _find_nearest_expiry(self, mark_data: List[Dict]) -> Optional[float]:
        """
        Find days until the nearest expiry from mark data symbol names.
        Symbol format: BTC-251123-126000-C → expiry date is 251123 (YYMMDD).
        Uses timezone-aware UTC (datetime.utcnow() is deprecated in Python 3.12+).
        """
        min_days = None

        for m in mark_data:
            sym = m.get("s", "")
            parts = sym.split("-")
            if len(parts) < 2:
                continue
            try:
                # Parse YYMMDD
                expiry_str = parts[1]
                year = 2000 + int(expiry_str[:2])
                month = int(expiry_str[2:4])
                day = int(expiry_str[4:6])
                expiry_dt = datetime.datetime(
                    year, month, day, tzinfo=datetime.timezone.utc
                )
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                days = (expiry_dt - now_utc).total_seconds() / 86400
                if days > 0 and (min_days is None or days < min_days):
                    min_days = days
            except (ValueError, IndexError):
                continue

        return round(min_days, 1) if min_days is not None else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _detect_iv_format(self, mark_data: List[Dict]) -> float:
        """
        Auto-detect whether Binance 'vo' (implied volatility) is decimal (0.65)
        or percentage (65.0) format. Returns a multiplier to normalize to decimal.

        Heuristic: No crypto option trades below 2% IV or above 200% IV.
          - avg(vo) > 2.0  → percentage format → return 0.01 (divide by 100)
          - avg(vo) <= 2.0 → decimal format   → return 1.0 (no change)

        This runs once per analysis cycle and the result normalizes all vo values
        in mark_data in-place, so every downstream method uses consistent decimal IV.
        """
        ivs = []
        for m in mark_data:
            vo = m.get("vo", 0)
            if isinstance(vo, (int, float)) and vo > 0:
                ivs.append(float(vo))
                if len(ivs) >= 30:
                    break

        if not ivs:
            return 1.0  # default: assume decimal

        avg_iv = sum(ivs) / len(ivs)
        if avg_iv > 2.0:
            logger.debug(f"IV format: percentage (avg={avg_iv:.1f}), scaling ×0.01")
            return 0.01
        else:
            logger.debug(f"IV format: decimal (avg={avg_iv:.4f}), no scaling")
            return 1.0

    def _filter_by_asset(
        self, data: List[Dict], asset_prefix: str, key: str = "s"
    ) -> List[Dict]:
        """Filter data list to only items belonging to the given asset prefix."""
        return [x for x in data if x.get(key, "").startswith(asset_prefix)]

    def _extract_expiry(self, item: Dict, key: str = "s") -> Optional[str]:
        """Extract expiry date string (YYMMDD) from a symbol.

        Examples:
            BTC-251123-126000-C → '251123'
            ETH-260107-4000-P   → '260107'
        """
        sym = item.get(key, "")
        parts = sym.split("-")
        return parts[1] if len(parts) >= 2 else None

    def _filter_by_expiry(
        self,
        mark_data: List[Dict],
        oi_data: List[Dict],
        max_expiries: int = 2,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Filter mark_data and oi_data to only the nearest N expirations.

        Far-dated contracts contaminate short-term signals because their
        OI/IV profiles differ significantly from near-term expirations.
        Since A1 already ensures OI is nearest-expiry only, this primarily
        aligns mark_data to match (mark chunks arrive from WS with all expiries).

        Returns (filtered_mark, filtered_oi).
        """
        # Collect unique expiries from mark data (primary — largest dataset)
        expiries = set()
        for item in mark_data:
            exp = self._extract_expiry(item, key="s")
            if exp:
                expiries.add(exp)

        # Fallback: derive from OI data if mark has no parseable expiries
        if not expiries:
            for item in oi_data:
                exp = self._extract_expiry(item, key="symbol")
                if exp:
                    expiries.add(exp)

        if not expiries:
            return mark_data, oi_data

        # YYMMDD format — string sort matches chronological order
        sorted_expiries = sorted(expiries)
        target = set(sorted_expiries[:max_expiries])

        filtered_mark = [
            x for x in mark_data if self._extract_expiry(x, key="s") in target
        ]
        filtered_oi = [
            x for x in oi_data if self._extract_expiry(x, key="symbol") in target
        ]

        return filtered_mark, filtered_oi

    @staticmethod
    def _safe_mean(values: List[float]) -> float:
        """Compute mean with guard against empty lists and NaN."""
        if not values:
            return NUMPY_EMPTY_FALLBACK
        arr = np.array(values, dtype=float)
        if arr.size == 0:
            return NUMPY_EMPTY_FALLBACK
        result = float(np.nanmean(arr))
        return result if np.isfinite(result) else NUMPY_EMPTY_FALLBACK
