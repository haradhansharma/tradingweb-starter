"""
Binance Options Market Analyzer
REDESIGNED: Enhanced 5-variable framework with real-time WebSocket data

Key Improvements:
1. PCR: Real-time volume ratio from tickers
2. Max Pain: Computed from WebSocket OI updates (60s)
3. Liquidity Walls: Highest OI strikes from WebSocket
4. IV Risk: Average IV from mark prices with Greek analysis
5. Whale Delta: BLOCK trade filtering with real delta weighting

Variable Weights:
- PCR (Sentiment):        20 points
- Max Pain Magnet:        15 points
- Liquidity Walls:        15 points
- IV Risk:                10 points (confidence reducer)
- Whale Delta:            40 points (MOST IMPORTANT)
"""

import logging
import time
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

# Import state from connector
from .binance_connector import MarketState, OptionSymbol

log = logging.getLogger(__name__)


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================

class TradingSignal(Enum):
    """Trading signal enumeration."""
    STRONG_BUY = "STRONG BUY"
    SCALP_LONG = "SCALP LONG"
    NEUTRAL = "NEUTRAL"
    SCALP_SHORT = "SCALP SHORT"
    STRONG_SELL = "STRONG SELL"


@dataclass
class IntelligenceMetrics:
    """Data class for calculated metrics."""
    pcr: float = 0.0
    pcr_put_volume: float = 0.0
    pcr_call_volume: float = 0.0
    
    max_pain: float = 0.0
    max_pain_distance_pct: float = 0.0
    
    call_resistance: float = 0.0
    call_wall_oi: float = 0.0
    put_support: float = 0.0
    put_wall_oi: float = 0.0
    
    avg_iv: float = 0.0
    iv_percentile: float = 0.0  # Relative to typical range
    
    whale_net_delta: float = 0.0
    whale_trade_count: int = 0
    whale_buy_pressure: float = 0.0  # -1 to +1
    
    index_price: float = 0.0
    timestamp: int = 0


@dataclass
class TradingDecision:
    """Data class for trading decision."""
    action: str
    score: float
    confidence: float
    reasons: List[str]
    entry_zone: float
    take_profit: float
    stop_loss: float
    max_pain_target: float
    risk_level: str  # LOW, MEDIUM, HIGH


# =============================================================================
# ANALYZER CLASS
# =============================================================================

class BinanceOptionMarketAnalyzer:
    """
    Processes real-time Binance Options WebSocket data into trading intelligence.
    
    This analyzer works with the MarketState from BinanceWebSocketManager,
    providing event-driven analysis for futures trading decisions.
    """
    
    # =========================================================================
    # CONFIGURATION CONSTANTS
    # =========================================================================
    
    # PCR thresholds (Put-Call Ratio)
    PCR_EXTREME_FEAR = 1.2      # High PCR = excessive put buying = bearish sentiment
    PCR_FEAR = 0.9
    PCR_NEUTRAL = 0.7
    PCR_GREED = 0.5
    PCR_EXTREME_GREED = 0.3     # Low PCR = excessive call buying = bullish sentiment
    
    # Max Pain thresholds
    MAX_PAIN_STRONG_MAGNET = 0.03    # 3% distance = strong magnet
    MAX_PAIN_WEAK_MAGNET = 0.015     # 1.5% distance = weak magnet
    
    # IV thresholds
    IV_LOW = 0.4             # 40% IV = low volatility
    IV_NORMAL = 0.6          # 60% IV = normal
    IV_HIGH = 0.8            # 80% IV = high
    IV_EXTREME = 1.0         # 100% IV = extreme
    
    # Whale Delta thresholds
    WHALE_DELTA_STRONG = 1000      # Strong institutional signal
    WHALE_DELTA_MODERATE = 500     # Moderate signal
    WHALE_DELTA_WEAK = 200         # Weak but notable
    
    # Scoring weights (total = 100)
    PCR_WEIGHT = 20
    MAX_PAIN_WEIGHT = 15
    WALLS_WEIGHT = 15
    IV_PENALTY = 0.5          # Multiplier when IV is high
    WHALE_WEIGHT = 40         # Most important!
    
    # Historical IV tracking (for percentile calculation)
    IV_HISTORY_SIZE = 100
    _iv_history: Dict[str, List[float]] = {}
    
    # =========================================================================
    # MAIN ANALYSIS METHOD
    # =========================================================================
    
    def generate_full_report(self, asset: str, state: MarketState) -> Dict[str, Any]:
        """
        Generate a complete intelligence report for an asset.
        
        Args:
            asset: Asset symbol (BTC, ETH, SOL)
            state: MarketState from WebSocket manager
            
        Returns:
            Complete intelligence report with metrics and trading signal
        """
        log.debug(f"Generating intelligence report for {asset}")
        
        # Calculate all 5 metrics
        metrics = self._calculate_all_metrics(asset, state)
        
        # Generate trading decision
        decision = self._generate_trading_decision(metrics)
        
        # Build and return the complete report
        return {
            "asset": asset,
            "timestamp": metrics.timestamp,
            "index_price": metrics.index_price,
            "metrics": {
                "pcr": round(metrics.pcr, 3),
                "pcr_put_volume": round(metrics.pcr_put_volume, 2),
                "pcr_call_volume": round(metrics.pcr_call_volume, 2),
                "max_pain": round(metrics.max_pain, 2),
                "max_pain_distance_pct": round(metrics.max_pain_distance_pct * 100, 2),
                "call_resistance": round(metrics.call_resistance, 2),
                "put_support": round(metrics.put_support, 2),
                "avg_iv": round(metrics.avg_iv, 4),
                "whale_net_delta": round(metrics.whale_net_delta, 2),
                "whale_trade_count": metrics.whale_trade_count,
                "whale_buy_pressure": round(metrics.whale_buy_pressure, 3),
            },
            "analysis": {
                "action": decision.action,
                "score": round(decision.score, 2),
                "confidence": round(decision.confidence, 2),
                "risk_level": decision.risk_level,
                "reasons": decision.reasons,
            },
            "trade_setup": {
                "entry_zone": round(decision.entry_zone, 2),
                "take_profit": round(decision.take_profit, 2),
                "stop_loss": round(decision.stop_loss, 2),
                "max_pain_target": round(decision.max_pain_target, 2),
            },
        }
    
    def _calculate_all_metrics(self, asset: str, state: MarketState) -> IntelligenceMetrics:
        """Calculate all 5 core metrics from MarketState."""
        
        metrics = IntelligenceMetrics(
            index_price=state.index_price,
            timestamp=state.last_update or int(time.time() * 1000)
        )
        
        # 1. PCR (Sentiment) - from tickers
        self._calculate_pcr(state, metrics)
        
        # 2. Max Pain (Target) - from open interest
        self._calculate_max_pain(state, metrics)
        
        # 3. Liquidity Walls (Barriers) - from open interest
        self._calculate_walls(state, metrics)
        
        # 4. IV Risk (Warning) - from mark prices
        self._calculate_iv_risk(asset, state, metrics)
        
        # 5. Whale Delta (Footprints) - from BLOCK trades
        self._calculate_whale_delta(state, metrics)
        
        return metrics
    
    # =========================================================================
    # VARIABLE 1: Put-Call Ratio (Sentiment)
    # =========================================================================
    
    def _calculate_pcr(self, state: MarketState, metrics: IntelligenceMetrics):
        """
        Calculate Put-Call Ratio from ticker volume.
        
        PCR = Total Put Volume / Total Call Volume
        
        Interpretation:
        - PCR > 1.2: Extreme Fear (contrarians: bullish - too many bears)
        - PCR 0.9-1.2: Fear
        - PCR 0.5-0.9: Neutral
        - PCR 0.3-0.5: Greed
        - PCR < 0.3: Extreme Greed (contrarians: bearish - too many bulls)
        
        Note: Using volume from tickers, not open interest.
        Volume PCR is more responsive to recent activity.
        """
        put_volume = 0.0
        call_volume = 0.0
        
        for symbol, ticker in state.tickers.items():
            # Determine if Put or Call from symbol
            if symbol.endswith("-P"):
                put_volume += float(ticker.get("volume", 0))
            elif symbol.endswith("-C"):
                call_volume += float(ticker.get("volume", 0))
        
        # Also check mark_prices for additional coverage
        for symbol, mark in state.mark_prices.items():
            # Use ticker volume if available, otherwise estimate from OI
            if symbol not in state.tickers:
                if symbol.endswith("-P"):
                    put_volume += float(state.open_interest.get(symbol, {}).get("open_interest", 0))
                elif symbol.endswith("-C"):
                    call_volume += float(state.open_interest.get(symbol, {}).get("open_interest", 0))
        
        if call_volume <= 0:
            metrics.pcr = 0.7  # Default neutral
            return
        
        pcr = put_volume / call_volume
        
        metrics.pcr = pcr
        metrics.pcr_put_volume = put_volume
        metrics.pcr_call_volume = call_volume
        
        log.debug(
            f"PCR: {pcr:.3f} (Put Vol: {put_volume:.0f}, Call Vol: {call_volume:.0f})"
        )
    
    # =========================================================================
    # VARIABLE 2: Max Pain (Target Magnet)
    # =========================================================================
    
    def _calculate_max_pain(self, state: MarketState, metrics: IntelligenceMetrics):
        """
        Calculate Max Pain strike price.
        
        Max Pain = Strike where total option payouts are minimized.
        Market makers have incentive to push price toward this level.
        
        Algorithm:
        1. Extract all unique strike prices from OI data
        2. For each possible expiry price, calculate total pain
        3. Max Pain = strike with minimum total pain
        """
        if not state.open_interest:
            return
        
        # Parse OI data to get strikes and OI per strike
        calls_by_strike: Dict[float, float] = {}
        puts_by_strike: Dict[float, float] = {}
        
        for symbol, oi_data in state.open_interest.items():
            parts = symbol.split("-")
            if len(parts) < 4:
                continue
            
            try:
                strike = float(parts[2])
                oi = float(oi_data.get("open_interest", 0))
                option_type = parts[3]
                
                if option_type == "C":
                    calls_by_strike[strike] = calls_by_strike.get(strike, 0) + oi
                elif option_type == "P":
                    puts_by_strike[strike] = puts_by_strike.get(strike, 0) + oi
            except (ValueError, IndexError):
                continue
        
        if not calls_by_strike and not puts_by_strike:
            return
        
        # Get all unique strikes
        all_strikes = sorted(set(list(calls_by_strike.keys()) + list(puts_by_strike.keys())))
        
        if not all_strikes:
            return
        
        # Calculate pain at each strike
        min_pain = float('inf')
        max_pain_strike = all_strikes[0]
        
        for expiry_price in all_strikes:
            total_pain = 0.0
            
            # Call pain: max(0, expiry - strike) * call_oi
            for strike, call_oi in calls_by_strike.items():
                total_pain += max(0, expiry_price - strike) * call_oi
            
            # Put pain: max(0, strike - expiry) * put_oi
            for strike, put_oi in puts_by_strike.items():
                total_pain += max(0, strike - expiry_price) * put_oi
            
            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = expiry_price
        
        metrics.max_pain = max_pain_strike
        
        # Calculate distance from current price
        if state.index_price > 0:
            metrics.max_pain_distance_pct = (max_pain_strike - state.index_price) / state.index_price
        
        log.debug(
            f"Max Pain: {max_pain_strike} (distance: {metrics.max_pain_distance_pct*100:.2f}%)"
        )
    
    # =========================================================================
    # VARIABLE 3: Liquidity Walls (Support/Resistance)
    # =========================================================================
    
    def _calculate_walls(self, state: MarketState, metrics: IntelligenceMetrics):
        """
        Identify Call Wall (resistance) and Put Wall (support).
        
        Call Wall = Strike with highest Call OI (above current price)
        Put Wall = Strike with highest Put OI (below current price)
        
        These represent levels where market makers have significant exposure
        and may defend price action.
        """
        if not state.open_interest:
            return
        
        calls_by_strike: Dict[float, float] = {}
        puts_by_strike: Dict[float, float] = {}
        
        for symbol, oi_data in state.open_interest.items():
            parts = symbol.split("-")
            if len(parts) < 4:
                continue
            
            try:
                strike = float(parts[2])
                oi_usd = float(oi_data.get("open_interest_usd", 0))
                option_type = parts[3]
                
                if option_type == "C":
                    calls_by_strike[strike] = calls_by_strike.get(strike, 0) + oi_usd
                elif option_type == "P":
                    puts_by_strike[strike] = puts_by_strike.get(strike, 0) + oi_usd
            except (ValueError, IndexError):
                continue
        
        current_price = state.index_price
        
        # Find Call Wall (highest OI call above current price)
        calls_above = {k: v for k, v in calls_by_strike.items() if k >= current_price}
        if calls_above:
            call_wall = max(calls_above.items(), key=lambda x: x[1])
            metrics.call_resistance = call_wall[0]
            metrics.call_wall_oi = call_wall[1]
        
        # Find Put Wall (highest OI put below current price)
        puts_below = {k: v for k, v in puts_by_strike.items() if k <= current_price}
        if puts_below:
            put_wall = max(puts_below.items(), key=lambda x: x[1])
            metrics.put_support = put_wall[0]
            metrics.put_wall_oi = put_wall[1]
        
        log.debug(
            f"Walls: Resistance={metrics.call_resistance} (${metrics.call_wall_oi:.0f}), "
            f"Support={metrics.put_support} (${metrics.put_wall_oi:.0f})"
        )
    
    # =========================================================================
    # VARIABLE 4: IV Risk (Volatility Warning)
    # =========================================================================
    
    def _calculate_iv_risk(self, asset: str, state: MarketState, metrics: IntelligenceMetrics):
        """
        Calculate average Implied Volatility and risk assessment.
        
        High IV indicates:
        - Market expects large moves
        - Option premiums are expensive
        - Potential mean reversion opportunity
        
        IV > 80%: High risk, use wider stops, reduce position
        IV > 100%: Extreme risk, consider fading moves
        """
        if not state.mark_prices:
            return
        
        iv_values = []
        
        for symbol, mark in state.mark_prices.items():
            # Use volatility (mark IV) if available
            vol = mark.get("volatility")
            if vol and vol > 0:
                iv_values.append(vol)
            # Fallback to average of bid/ask IV
            elif mark.get("bid_iv") and mark.get("ask_iv"):
                avg_iv = (mark["bid_iv"] + mark["ask_iv"]) / 2
                if avg_iv > 0:
                    iv_values.append(avg_iv)
        
        if not iv_values:
            return
        
        avg_iv = sum(iv_values) / len(iv_values)
        metrics.avg_iv = avg_iv
        
        # Update IV history for percentile calculation
        if asset not in self._iv_history:
            self._iv_history[asset] = []
        
        self._iv_history[asset].append(avg_iv)
        if len(self._iv_history[asset]) > self.IV_HISTORY_SIZE:
            self._iv_history[asset] = self._iv_history[asset][-self.IV_HISTORY_SIZE:]
        
        # Calculate percentile
        if len(self._iv_history[asset]) >= 10:
            sorted_iv = sorted(self._iv_history[asset])
            idx = sorted_iv.index(avg_iv) if avg_iv in sorted_iv else len(sorted_iv) // 2
            metrics.iv_percentile = idx / len(sorted_iv)
        
        log.debug(f"Average IV: {avg_iv:.2%} (percentile: {metrics.iv_percentile:.0%})")
    
    # =========================================================================
    # VARIABLE 5: Whale Net Delta (Institutional Footprints)
    # =========================================================================
    
    def _calculate_whale_delta(self, state: MarketState, metrics: IntelligenceMetrics):
        """
        Calculate net delta exposure from BLOCK trades.
        
        BLOCK TRADES are the key signal - they represent institutional activity
        with these characteristics:
        - Large notional value
        - Often informed trading
        - Execute outside regular order book
        - Reported with "X": "BLOCK" flag in WebSocket
        
        Formula:
        Net Delta = Σ(BLOCK Trade Qty × Option Delta × Direction)
        
        Where:
        - Trade Qty: Quantity from block trade
        - Option Delta: From mark prices (Δ for calls, -Δ for puts)
        - Direction: +1 for BUY, -1 for SELL
        """
        if not state.trades or not state.mark_prices:
            return
        
        net_delta = 0.0
        block_trades = []
        buy_delta = 0.0
        sell_delta = 0.0
        
        for trade in state.trades:
            # Only process BLOCK trades
            if trade.get("trade_type") != "BLOCK":
                continue
            
            symbol = trade.get("symbol", "")
            qty = trade.get("quantity", 0)
            side = trade.get("side", "")
            
            # Get delta from mark prices
            mark = state.mark_prices.get(symbol, {})
            delta = mark.get("delta", 0)
            
            if not delta:
                # Estimate delta from moneyness if not available
                parts = symbol.split("-")
                if len(parts) >= 3:
                    try:
                        strike = float(parts[2])
                        option_type = parts[3]
                        spot = state.index_price
                        
                        if spot > 0:
                            moneyness = spot / strike
                            if option_type == "C":
                                # Call delta approximation
                                delta = max(0, min(1, (moneyness - 0.9) * 5))
                            else:
                                # Put delta approximation (negative)
                                delta = -max(0, min(1, (1.1 - moneyness) * 5))
                    except (ValueError, IndexError):
                        pass
            
            # Adjust for put delta (already negative in mark data, but quantity sign matters)
            if symbol.endswith("-P"):
                # Put delta is negative, buying puts = negative delta exposure
                delta = -abs(delta) if delta > 0 else delta
            
            # Calculate directional delta
            direction = 1 if side == "BUY" else -1
            trade_delta = qty * delta * direction
            
            net_delta += trade_delta
            block_trades.append(trade)
            
            if side == "BUY":
                buy_delta += abs(trade_delta)
            else:
                sell_delta += abs(trade_delta)
        
        metrics.whale_net_delta = net_delta
        metrics.whale_trade_count = len(block_trades)
        
        # Calculate buy pressure (-1 to +1)
        total_delta = buy_delta + sell_delta
        if total_delta > 0:
            metrics.whale_buy_pressure = (buy_delta - sell_delta) / total_delta
        
        log.debug(
            f"Whale Delta: {net_delta:.2f} (trades: {len(block_trades)}, "
            f"buy pressure: {metrics.whale_buy_pressure:.2f})"
        )
    
    # =========================================================================
    # TRADING DECISION ENGINE
    # =========================================================================
    
    def _generate_trading_decision(self, metrics: IntelligenceMetrics) -> TradingDecision:
        """
        Generate trading decision based on weighted confidence scoring.
        
        Score Range: -100 to +100
        - +100 to +50: Strong Buy
        - +50 to +15: Scalp Long
        - +15 to -15: Neutral
        - -15 to -50: Scalp Short
        - -50 to -100: Strong Sell
        """
        score = 0.0
        reasons = []
        risk_factors = []
        
        # =====================================================================
        # 1. SENTIMENT CHECK (PCR) - Weight: 20
        # =====================================================================
        
        if metrics.pcr >= self.PCR_EXTREME_FEAR:
            # Extreme fear = Contrarian bullish (everyone's bearish)
            score += self.PCR_WEIGHT
            reasons.append(f"Contrarian LONG: PCR {metrics.pcr:.2f} shows extreme fear")
        elif metrics.pcr >= self.PCR_FEAR:
            score += self.PCR_WEIGHT * 0.5
            reasons.append(f"Moderate fear: PCR {metrics.pcr:.2f} (slightly bullish)")
        elif metrics.pcr <= self.PCR_EXTREME_GREED:
            # Extreme greed = Contrarian bearish (everyone's bullish)
            score -= self.PCR_WEIGHT
            reasons.append(f"Contrarian SHORT: PCR {metrics.pcr:.2f} shows extreme greed")
        elif metrics.pcr <= self.PCR_GREED:
            score -= self.PCR_WEIGHT * 0.5
            reasons.append(f"Moderate greed: PCR {metrics.pcr:.2f} (slightly bearish)")
        
        # =====================================================================
        # 2. MAGNET CHECK (Max Pain) - Weight: 15
        # =====================================================================
        
        if abs(metrics.max_pain_distance_pct) > self.MAX_PAIN_STRONG_MAGNET:
            if metrics.max_pain_distance_pct > 0:
                # Price below max pain = magnet pull up
                score += self.MAX_PAIN_WEIGHT
                reasons.append(f"Strong magnet UP: Max Pain {metrics.max_pain:.0f} is {abs(metrics.max_pain_distance_pct)*100:.1f}% above")
            else:
                # Price above max pain = magnet pull down
                score -= self.MAX_PAIN_WEIGHT
                reasons.append(f"Strong magnet DOWN: Max Pain {metrics.max_pain:.0f} is {abs(metrics.max_pain_distance_pct)*100:.1f}% below")
        elif abs(metrics.max_pain_distance_pct) > self.MAX_PAIN_WEAK_MAGNET:
            if metrics.max_pain_distance_pct > 0:
                score += self.MAX_PAIN_WEIGHT * 0.5
                reasons.append(f"Weak magnet UP: Max Pain at {metrics.max_pain:.0f}")
            else:
                score -= self.MAX_PAIN_WEIGHT * 0.5
                reasons.append(f"Weak magnet DOWN: Max Pain at {metrics.max_pain:.0f}")
        
        # =====================================================================
        # 3. LIQUIDITY WALLS - Weight: 15
        # =====================================================================
        
        # Check if price is near a wall
        if metrics.index_price > 0:
            if metrics.call_resistance > 0:
                call_distance = (metrics.call_resistance - metrics.index_price) / metrics.index_price
                if 0 < call_distance < 0.02:  # Within 2% of call wall
                    score -= self.WALLS_WEIGHT * 0.5
                    reasons.append(f"Near CALL WALL resistance at {metrics.call_resistance:.0f}")
            
            if metrics.put_support > 0:
                put_distance = (metrics.index_price - metrics.put_support) / metrics.index_price
                if 0 < put_distance < 0.02:  # Within 2% of put wall
                    score += self.WALLS_WEIGHT * 0.5
                    reasons.append(f"Near PUT WALL support at {metrics.put_support:.0f}")
        
        # =====================================================================
        # 4. IV RISK CHECK - Confidence Reducer
        # =====================================================================
        
        if metrics.avg_iv >= self.IV_EXTREME:
            score *= self.IV_PENALTY
            risk_factors.append("EXTREME")
            reasons.append(f"⚠️ EXTREME IV {metrics.avg_iv:.0%}: High volatility expected")
        elif metrics.avg_iv >= self.IV_HIGH:
            score *= 0.7
            risk_factors.append("HIGH")
            reasons.append(f"High IV {metrics.avg_iv:.0%}: Consider wider stops")
        elif metrics.avg_iv < self.IV_LOW:
            risk_factors.append("LOW")
            reasons.append(f"Low IV {metrics.avg_iv:.0%}: Good entry environment")
        else:
            risk_factors.append("NORMAL")
        
        # =====================================================================
        # 5. WHALE DELTA CHECK - Weight: 40 (MOST IMPORTANT!)
        # =====================================================================
        
        if abs(metrics.whale_net_delta) >= self.WHALE_DELTA_STRONG:
            if metrics.whale_net_delta > 0:
                score += self.WHALE_WEIGHT
                reasons.append(
                    f"🐋 STRONG BULLISH whale delta: +{metrics.whale_net_delta:.0f} "
                    f"({metrics.whale_trade_count} block trades)"
                )
            else:
                score -= self.WHALE_WEIGHT
                reasons.append(
                    f"🐋 STRONG BEARISH whale delta: {metrics.whale_net_delta:.0f} "
                    f"({metrics.whale_trade_count} block trades)"
                )
        elif abs(metrics.whale_net_delta) >= self.WHALE_DELTA_MODERATE:
            if metrics.whale_net_delta > 0:
                score += self.WHALE_WEIGHT * 0.7
                reasons.append(f"🐋 Moderate bullish delta: +{metrics.whale_net_delta:.0f}")
            else:
                score -= self.WHALE_WEIGHT * 0.7
                reasons.append(f"🐋 Moderate bearish delta: {metrics.whale_net_delta:.0f}")
        elif abs(metrics.whale_net_delta) >= self.WHALE_DELTA_WEAK:
            if metrics.whale_net_delta > 0:
                score += self.WHALE_WEIGHT * 0.3
                reasons.append(f"🐋 Slight bullish delta: +{metrics.whale_net_delta:.0f}")
            else:
                score -= self.WHALE_WEIGHT * 0.3
                reasons.append(f"🐋 Slight bearish delta: {metrics.whale_net_delta:.0f}")
        
        # =====================================================================
        # DETERMINE ACTION & TRADE SETUP
        # =====================================================================
        
        if score >= 50:
            action = TradingSignal.STRONG_BUY.value
        elif score >= 15:
            action = TradingSignal.SCALP_LONG.value
        elif score <= -50:
            action = TradingSignal.STRONG_SELL.value
        elif score <= -15:
            action = TradingSignal.SCALP_SHORT.value
        else:
            action = TradingSignal.NEUTRAL.value
        
        # Calculate trade setup based on direction
        if score > 0:  # Long bias
            entry_zone = metrics.put_support if metrics.put_support > 0 else metrics.index_price * 0.99
            take_profit = metrics.call_resistance if metrics.call_resistance > 0 else metrics.index_price * 1.03
            stop_loss = entry_zone * 0.98  # 2% below entry
        else:  # Short bias
            entry_zone = metrics.call_resistance if metrics.call_resistance > 0 else metrics.index_price * 1.01
            take_profit = metrics.put_support if metrics.put_support > 0 else metrics.index_price * 0.97
            stop_loss = entry_zone * 1.02  # 2% above entry
        
        # Calculate confidence (0-100 scale)
        confidence = min(100, abs(score))
        
        # Determine risk level
        if "EXTREME" in risk_factors:
            risk_level = "HIGH"
        elif "HIGH" in risk_factors:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"
        
        return TradingDecision(
            action=action,
            score=score,
            confidence=confidence,
            reasons=reasons if reasons else ["No significant signals detected"],
            entry_zone=entry_zone,
            take_profit=take_profit,
            stop_loss=stop_loss,
            max_pain_target=metrics.max_pain,
            risk_level=risk_level,
        )
    
    # =========================================================================
    # UTILITY METHODS
    # =========================================================================
    
    def quick_signal(self, asset: str, state: MarketState) -> str:
        """
        Quick signal generation for real-time display.
        Returns just the action without full analysis.
        """
        metrics = self._calculate_all_metrics(asset, state)
        decision = self._generate_trading_decision(metrics)
        return decision.action
    
    def get_delta_imbalance(self, state: MarketState) -> Dict[str, Any]:
        """
        Get detailed delta imbalance analysis.
        Useful for understanding whale positioning.
        """
        if not state.trades:
            return {"imbalance": 0, "direction": "NEUTRAL"}
        
        buy_volume = 0.0
        sell_volume = 0.0
        block_count = 0
        
        for trade in state.trades:
            if trade.get("trade_type") == "BLOCK":
                block_count += 1
                qty = abs(trade.get("quantity", 0))
                if trade.get("side") == "BUY":
                    buy_volume += qty
                else:
                    sell_volume += qty
        
        total = buy_volume + sell_volume
        if total == 0:
            return {"imbalance": 0, "direction": "NEUTRAL", "block_count": block_count}
        
        imbalance = (buy_volume - sell_volume) / total
        
        if imbalance > 0.3:
            direction = "BULLISH"
        elif imbalance < -0.3:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"
        
        return {
            "imbalance": round(imbalance, 3),
            "direction": direction,
            "buy_volume": round(buy_volume, 2),
            "sell_volume": round(sell_volume, 2),
            "block_count": block_count,
        }
