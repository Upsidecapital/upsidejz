"""
GreymatterAI — Claude AI Learning Engine

Uses Anthropic's Claude API as the intelligence layer to analyze trade history,
identify patterns, and produce structured adjustments that improve future decisions.

How it works:
  1. After every ANALYSIS_INTERVAL closed trades (default: 5), the engine fires.
  2. It compiles the last HISTORY_WINDOW trades into a structured JSON payload.
  3. This is sent to Claude (claude-haiku — fast and cheap, ideal for a small account).
  4. Claude returns a JSON object with:
       setup_adjustments  — per-setup conviction multipliers (0.50 – 1.50)
       risk_adjustment    — scale risk per trade up/down (0.50 – 1.00; never > 1)
       avoid_conditions   — list of conditions/times to skip
       focus_setups       — list of currently high-performing setups to prioritise
       summary            — short narrative the dashboard can display
       confidence         — 0–1, how confident Claude is (low = fewer trades seen)
  5. Adjustments are stored in the `claude_insights` DB table and applied by the
     signal orchestrator on top of the EWMA adaptive weights.

Small-account ($250) awareness:
  The prompt explicitly tells Claude the account size so it can factor in
  risk-of-ruin. With $250 it will tend to recommend caution and tighter filters.

Cost:
  Uses claude-haiku-4-5-20251001 (cheapest, ~$0.25/M tokens). A single analysis
  call with 20 trades costs < $0.001 — effectively free at this account size.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_ANTHROPIC_AVAILABLE = False
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    logger.warning("anthropic package not installed — Claude learning disabled. Run: pip install anthropic")

ANALYSIS_INTERVAL = 5     # run analysis every N closed trades
HISTORY_WINDOW    = 30    # send last N trades to Claude for context
_MODEL            = "claude-haiku-4-5-20251001"   # fast + cheap


@dataclass
class ClaudeInsight:
    """Parsed output from one Claude analysis run."""
    setup_adjustments: dict[str, float]  # {"retest_long": 1.2, "delta_flip": 0.8, ...}
    risk_adjustment:   float             # 0.5–1.0 scale factor on risk_usd per trade
    avoid_conditions:  list[str]
    focus_setups:      list[str]
    summary:           str
    confidence:        float             # 0–1
    raw_response:      str
    created_at:        datetime


class ClaudeLearnEngine:
    """
    Sends trade history to Claude and receives structured adjustments.
    Thread-safe for single-threaded async use (calls are made synchronously
    in a background thread via the scheduler).
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("CLAUDE_API_KEY", "")
        self._closed_since_last = 0
        self._latest_insight: Optional[ClaudeInsight] = None

    def notify_trade_closed(self) -> bool:
        """
        Call this whenever a trade closes.
        Returns True if an analysis should be triggered now.
        """
        self._closed_since_last += 1
        return self._closed_since_last >= ANALYSIS_INTERVAL

    def reset_counter(self) -> None:
        self._closed_since_last = 0

    @property
    def latest_insight(self) -> Optional[ClaudeInsight]:
        return self._latest_insight

    # ------------------------------------------------------------------
    # Main analysis method
    # ------------------------------------------------------------------
    async def analyse(self, account_size_usd: float) -> Optional[ClaudeInsight]:
        """
        Pull recent trade history from DB, send to Claude, parse response,
        store result, return ClaudeInsight (or None on any error).
        """
        if not _ANTHROPIC_AVAILABLE or not self._api_key:
            logger.info("Claude learning skipped — CLAUDE_API_KEY not set or anthropic not installed")
            return None

        trades = await self._fetch_recent_trades()
        if len(trades) < 3:
            logger.info("Claude learning skipped — fewer than 3 closed trades available")
            return None

        prompt = self._build_prompt(trades, account_size_usd)
        raw    = self._call_claude(prompt)
        if raw is None:
            return None

        insight = self._parse_response(raw)
        if insight:
            self._latest_insight = insight
            await self._persist(insight)
            self.reset_counter()
            logger.info(
                "Claude learning complete | confidence=%.2f risk_adj=%.2f setups=%s",
                insight.confidence, insight.risk_adjustment,
                list(insight.setup_adjustments.keys()),
            )
        return insight

    # ------------------------------------------------------------------
    # Trade history fetch
    # ------------------------------------------------------------------
    @staticmethod
    async def _fetch_recent_trades() -> list[dict]:
        from sqlalchemy import select, desc
        from database import AsyncSessionLocal, Trade, TradeStatus
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Trade)
                .where(Trade.status.in_([TradeStatus.CLOSED_WIN, TradeStatus.CLOSED_LOSS]))
                .order_by(desc(Trade.closed_at))
                .limit(HISTORY_WINDOW)
            )).scalars().all()
        return [
            {
                "id":        r.id,
                "strategy":  r.strategy.value if hasattr(r.strategy, "value") else str(r.strategy),
                "setup":     r.setup or "unknown",
                "direction": r.direction.value if hasattr(r.direction, "value") else str(r.direction),
                "entry":     r.entry_price,
                "sl":        r.stop_loss,
                "tp":        r.take_profit,
                "close":     r.close_price,
                "pnl_usd":  round(r.pnl_usd, 2) if r.pnl_usd is not None else None,
                "pnl_r":    round(r.pnl_r, 2) if r.pnl_r is not None else None,
                "conviction": r.conviction,
                "result":   "WIN" if r.pnl_usd and r.pnl_usd > 0 else "LOSS",
                "opened_at": r.opened_at.isoformat() if r.opened_at else None,
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                "notes":    r.notes,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(trades: list[dict], account_size_usd: float) -> str:
        # Aggregate per-setup stats for Claude context
        setup_stats: dict[str, dict] = {}
        for t in trades:
            key = f"{t['strategy']}/{t['setup']}"
            if key not in setup_stats:
                setup_stats[key] = {"wins": 0, "losses": 0, "total_r": 0.0, "count": 0}
            s = setup_stats[key]
            s["count"]   += 1
            s["total_r"] += t["pnl_r"] or 0.0
            if t["result"] == "WIN":
                s["wins"] += 1
            else:
                s["losses"] += 1

        setup_summary = []
        for setup, s in setup_stats.items():
            wr = s["wins"] / s["count"] * 100 if s["count"] else 0
            avg_r = s["total_r"] / s["count"] if s["count"] else 0
            setup_summary.append(
                f"  {setup}: {s['count']} trades, {wr:.0f}% WR, avg {avg_r:+.2f}R"
            )

        trades_json = json.dumps(trades[-20:], indent=2)   # last 20 for detail

        return f"""You are an expert NAS100 day-trading analyst reviewing the performance of an automated trading bot
that implements Fabio Valentini's ORB / IVB / Order Flow strategies.

ACCOUNT SIZE: ${account_size_usd:.2f} USD
(This is a small account. Risk-of-ruin is a serious concern. Weight your recommendations conservatively.)

SETUP PERFORMANCE SUMMARY (all {len(trades)} closed trades):
{chr(10).join(setup_summary) if setup_summary else "  No setup data yet."}

RECENT TRADE DETAIL (last {min(20, len(trades))} trades):
{trades_json}

TASK:
Analyse the trade data above and return a JSON object with EXACTLY this structure — no extra keys, no markdown:

{{
  "setup_adjustments": {{
    "<strategy>/<setup>": <float 0.50–1.50>
  }},
  "risk_adjustment": <float 0.50–1.00>,
  "avoid_conditions": ["<string>", ...],
  "focus_setups": ["<strategy>/<setup>", ...],
  "summary": "<2-3 sentence plain English analysis>",
  "confidence": <float 0.0–1.0>
}}

Rules:
- setup_adjustments: only include setups that appear in the data.
  Values > 1.0 mean "boost conviction for this setup", < 1.0 means "reduce it".
  Never exceed 1.50 or go below 0.50.
- risk_adjustment: never exceed 1.00 (never increase risk beyond baseline).
  If win rate < 40% overall, recommend 0.50–0.70.
  If account < $500, cap at 0.80 even if performance is good (protect capital).
- avoid_conditions: specific patterns that led to losses (e.g. "avoid delta_flip after 3 consecutive losses",
  "skip absorption setups on Fridays", "avoid ORB when IB range < 25pts").
- focus_setups: top 1-3 setups by risk-adjusted return.
- confidence: 0.0 if fewer than 5 trades, scales to 1.0 at 50+ trades.
- summary: plain English, no jargon, max 3 sentences.

Return ONLY the JSON object. No other text."""

    # ------------------------------------------------------------------
    # Claude API call (synchronous — run in thread)
    # ------------------------------------------------------------------
    def _call_claude(self, prompt: str) -> Optional[str]:
        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            msg = client.messages.create(
                model=_MODEL,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text if msg.content else None
        except Exception as exc:
            logger.error("Claude API call failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(raw: str) -> Optional[ClaudeInsight]:
        try:
            # Strip any accidental markdown fences
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())

            # Validate and clamp
            setup_adj = {
                k: float(max(0.50, min(1.50, v)))
                for k, v in data.get("setup_adjustments", {}).items()
            }
            risk_adj = float(max(0.50, min(1.00, data.get("risk_adjustment", 1.0))))
            confidence = float(max(0.0, min(1.0, data.get("confidence", 0.5))))

            return ClaudeInsight(
                setup_adjustments=setup_adj,
                risk_adjustment=risk_adj,
                avoid_conditions=list(data.get("avoid_conditions", [])),
                focus_setups=list(data.get("focus_setups", [])),
                summary=str(data.get("summary", ""))[:500],
                confidence=confidence,
                raw_response=raw[:2000],
                created_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error("Claude response parse failed: %s\nRaw: %s", exc, raw[:200])
            return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    @staticmethod
    async def _persist(insight: ClaudeInsight) -> None:
        from sqlalchemy import text
        from database import AsyncSessionLocal
        import json as _json
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        "INSERT INTO claude_insights "
                        "(setup_adjustments, risk_adjustment, avoid_conditions, "
                        " focus_setups, summary, confidence, raw_response, created_at) "
                        "VALUES (:sa, :ra, :ac, :fs, :s, :c, :rr, :ts)"
                    ),
                    {
                        "sa":  _json.dumps(insight.setup_adjustments),
                        "ra":  insight.risk_adjustment,
                        "ac":  _json.dumps(insight.avoid_conditions),
                        "fs":  _json.dumps(insight.focus_setups),
                        "s":   insight.summary,
                        "c":   insight.confidence,
                        "rr":  insight.raw_response,
                        "ts":  insight.created_at,
                    }
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Claude insight persist failed: %s", exc)

    # ------------------------------------------------------------------
    # Adjustment lookup (used by orchestrator)
    # ------------------------------------------------------------------
    def get_setup_adjustment(self, strategy: str, setup: str) -> float:
        """
        Returns the Claude-recommended multiplier for a given strategy+setup.
        Falls back to 1.0 (neutral) if no insight available or setup not covered.
        """
        if self._latest_insight is None:
            return 1.0
        key = f"{strategy}/{setup}"
        return self._latest_insight.setup_adjustments.get(key, 1.0)

    def get_risk_adjustment(self) -> float:
        """Returns the Claude-recommended risk scale factor (0.5–1.0)."""
        if self._latest_insight is None:
            return 1.0
        return self._latest_insight.risk_adjustment


# Module-level singleton
claude_learn = ClaudeLearnEngine()
