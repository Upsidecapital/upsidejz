"""
GreymatterAI — AI Learning Engine (Multi-Provider)

Analyses trade history using an AI model and returns structured adjustments
that improve the bot's conviction scoring and position sizing.

Provider selection (set AI_PROVIDER in .env):
  gemini    — Google Gemini 1.5 Flash  ← DEFAULT (completely free, no card needed)
              15 req/min, 1M tokens/day free  |  pip install google-generativeai
              Key: https://aistudio.google.com/app/apikey

  deepseek  — DeepSeek V3  (free credits on signup, then ~$0.001/analysis)
              pip install openai  (DeepSeek uses OpenAI-compatible API)
              Key: https://platform.deepseek.com

  groq      — Groq (Llama 3.3 70B) — 14,400 req/day free
              pip install groq
              Key: https://console.groq.com

  claude    — Anthropic Claude (requires separate API billing — NOT included in Pro)
              pip install anthropic
              Key: https://console.anthropic.com

How it works:
  After every ANALYSIS_INTERVAL closed trades (default 5), the engine:
  1. Fetches last HISTORY_WINDOW trades from DB
  2. Sends structured JSON to the selected AI provider
  3. Receives back: setup_adjustments, risk_adjustment, avoid_conditions,
     focus_setups, summary, confidence
  4. Stores in claude_insights table (name kept for DB compatibility)
  5. Orchestrator applies adjustments on top of EWMA weights
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

ANALYSIS_INTERVAL = 5
HISTORY_WINDOW    = 30

# Read provider from env — default to gemini (free)
_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower().strip()


# ---------------------------------------------------------------------------
# Provider availability checks
# ---------------------------------------------------------------------------
def _has_gemini() -> bool:
    try:
        import google.generativeai  # noqa: F401
        return bool(os.environ.get("GEMINI_API_KEY"))
    except ImportError:
        return False

def _has_deepseek() -> bool:
    try:
        import openai  # noqa: F401
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    except ImportError:
        return False

def _has_groq() -> bool:
    try:
        import groq  # noqa: F401
        return bool(os.environ.get("GROQ_API_KEY"))
    except ImportError:
        return False

def _has_claude() -> bool:
    try:
        import anthropic  # noqa: F401
        return bool(os.environ.get("CLAUDE_API_KEY"))
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Provider call implementations
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str) -> Optional[str]:
    """Google Gemini 1.5 Flash — free tier (15 RPM, 1M tokens/day)."""
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        generation_config={"temperature": 0.1, "max_output_tokens": 800},
    )
    response = model.generate_content(prompt)
    return response.text


def _call_deepseek(prompt: str) -> Optional[str]:
    """DeepSeek V3 — free credits on signup, then ~$0.001/call."""
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.1,
    )
    return resp.choices[0].message.content


def _call_groq(prompt: str) -> Optional[str]:
    """Groq — Llama 3.3 70B, 14,400 free req/day."""
    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.1,
    )
    return resp.choices[0].message.content


def _call_claude(prompt: str) -> Optional[str]:
    """Anthropic Claude Haiku — requires separate API billing."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text if msg.content else None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "gemini":   (_has_gemini,   _call_gemini,   "GEMINI_API_KEY"),
    "deepseek": (_has_deepseek, _call_deepseek, "DEEPSEEK_API_KEY"),
    "groq":     (_has_groq,     _call_groq,     "GROQ_API_KEY"),
    "claude":   (_has_claude,   _call_claude,   "CLAUDE_API_KEY"),
}


def _dispatch(prompt: str) -> Optional[str]:
    """
    Try the configured provider. If unavailable, auto-fall-through to the
    next available one (gemini → deepseek → groq → claude).
    Logs which provider is actually used.
    """
    order = [_PROVIDER] + [p for p in ("gemini", "deepseek", "groq", "claude") if p != _PROVIDER]
    for provider in order:
        check, call, key_env = _PROVIDERS.get(provider, (lambda: False, None, ""))
        if not check():
            if provider == _PROVIDER and not os.environ.get(key_env):
                logger.info(
                    "AI provider '%s' not available (%s not set) — trying next",
                    provider, key_env,
                )
            continue
        try:
            logger.info("AI learning: using provider '%s'", provider)
            return call(prompt)
        except Exception as exc:
            logger.warning("AI provider '%s' failed: %s — trying next", provider, exc)
    logger.warning(
        "AI learning disabled — no provider available. "
        "Set GEMINI_API_KEY (free: https://aistudio.google.com/app/apikey) to enable."
    )
    return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AIInsight:
    setup_adjustments: dict[str, float]
    risk_adjustment:   float
    avoid_conditions:  list[str]
    focus_setups:      list[str]
    summary:           str
    confidence:        float
    provider:          str
    raw_response:      str
    created_at:        datetime


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class AILearnEngine:

    def __init__(self) -> None:
        self._closed_since_last = 0
        self._latest: Optional[AIInsight] = None

    def notify_trade_closed(self) -> bool:
        self._closed_since_last += 1
        return self._closed_since_last >= ANALYSIS_INTERVAL

    def reset_counter(self) -> None:
        self._closed_since_last = 0

    @property
    def latest_insight(self) -> Optional[AIInsight]:
        return self._latest

    # ------------------------------------------------------------------
    async def analyse(self, account_size_usd: float) -> Optional[AIInsight]:
        trades = await self._fetch_recent_trades()
        if len(trades) < 3:
            logger.info("AI learning: fewer than 3 trades — skipping")
            return None

        prompt = self._build_prompt(trades, account_size_usd)
        raw    = _dispatch(prompt)
        if raw is None:
            return None

        insight = self._parse(raw)
        if insight:
            self._latest = insight
            await self._persist(insight)
            self.reset_counter()
            logger.info(
                "AI insight applied | provider=%s risk=%.2f conf=%.2f | %s",
                insight.provider, insight.risk_adjustment,
                insight.confidence, insight.summary[:80],
            )
        return insight

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
                "pnl_r":    round(r.pnl_r,   2) if r.pnl_r   is not None else None,
                "pnl_usd":  round(r.pnl_usd, 2) if r.pnl_usd is not None else None,
                "conviction": r.conviction,
                "result":   "WIN" if r.pnl_usd and r.pnl_usd > 0 else "LOSS",
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                "notes":    r.notes,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(trades: list[dict], account_size_usd: float) -> str:
        setup_stats: dict[str, dict] = {}
        for t in trades:
            key = f"{t['strategy']}/{t['setup']}"
            s = setup_stats.setdefault(key, {"wins": 0, "losses": 0, "total_r": 0.0, "count": 0})
            s["count"]   += 1
            s["total_r"] += t["pnl_r"] or 0.0
            if t["result"] == "WIN":
                s["wins"] += 1
            else:
                s["losses"] += 1

        summary_lines = [
            f"  {k}: {v['count']} trades, "
            f"{v['wins']/v['count']*100:.0f}% WR, "
            f"avg {v['total_r']/v['count']:+.2f}R"
            for k, v in setup_stats.items()
        ]

        trades_json = json.dumps(trades[-20:], indent=2)

        return f"""You are an expert NAS100 day-trading analyst reviewing an automated trading bot
that implements Fabio Valentini's ORB / IVB / Order Flow strategies.

ACCOUNT SIZE: ${account_size_usd:.2f} USD
(Small account — weight recommendations conservatively. Risk-of-ruin matters.)

SETUP PERFORMANCE ({len(trades)} closed trades):
{chr(10).join(summary_lines) if summary_lines else "  No data yet."}

RECENT TRADES (last {min(20, len(trades))}):
{trades_json}

Return ONLY a JSON object — no markdown, no explanation, just the JSON:

{{
  "setup_adjustments": {{"<strategy>/<setup>": <float 0.50-1.50>}},
  "risk_adjustment": <float 0.50-1.00>,
  "avoid_conditions": ["<string>"],
  "focus_setups": ["<strategy>/<setup>"],
  "summary": "<2-3 sentence plain English>",
  "confidence": <float 0.0-1.0>
}}

Rules:
- setup_adjustments: only include setups present in the data. >1.0 = boost, <1.0 = reduce.
- risk_adjustment: NEVER exceed 1.00. If overall WR < 40%, use 0.50-0.70. If account < $500, cap at 0.80.
- confidence: 0.0 for <5 trades, scales to 1.0 at 50+ trades.
- Return ONLY the JSON."""

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(raw: str) -> Optional["AIInsight"]:
        try:
            text = raw.strip()
            # Strip markdown code fences if present
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            # Find first { to last } in case of leading/trailing text
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON object found in response")
            data = json.loads(text[start:end])

            setup_adj = {
                k: float(max(0.5, min(1.5, v)))
                for k, v in data.get("setup_adjustments", {}).items()
            }
            return AIInsight(
                setup_adjustments=setup_adj,
                risk_adjustment=float(max(0.5, min(1.0, data.get("risk_adjustment", 1.0)))),
                avoid_conditions=list(data.get("avoid_conditions", [])),
                focus_setups=list(data.get("focus_setups", [])),
                summary=str(data.get("summary", ""))[:500],
                confidence=float(max(0.0, min(1.0, data.get("confidence", 0.5)))),
                provider=_PROVIDER,
                raw_response=raw[:2000],
                created_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error("AI response parse failed: %s\nRaw: %.200s", exc, raw)
            return None

    # ------------------------------------------------------------------
    @staticmethod
    async def _persist(insight: "AIInsight") -> None:
        from sqlalchemy import text
        from database import AsyncSessionLocal
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
                        "sa":  json.dumps(insight.setup_adjustments),
                        "ra":  insight.risk_adjustment,
                        "ac":  json.dumps(insight.avoid_conditions),
                        "fs":  json.dumps(insight.focus_setups),
                        "s":   insight.summary,
                        "c":   insight.confidence,
                        "rr":  insight.raw_response,
                        "ts":  insight.created_at,
                    }
                )
                await db.commit()
        except Exception as exc:
            logger.warning("AI insight persist failed: %s", exc)

    # ------------------------------------------------------------------
    def get_setup_adjustment(self, strategy: str, setup: str) -> float:
        if self._latest is None:
            return 1.0
        return self._latest.setup_adjustments.get(f"{strategy}/{setup}", 1.0)

    def get_risk_adjustment(self) -> float:
        if self._latest is None:
            return 1.0
        return self._latest.risk_adjustment


# Module-level singleton — imported as `claude_learn` everywhere for compatibility
claude_learn = AILearnEngine()
