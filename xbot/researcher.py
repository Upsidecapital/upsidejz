"""
Geopolitical news researcher — uses Claude API with web search to find
trending topics related to US geopolitics and financial markets.
"""

import os
import json
import logging
from typing import AsyncGenerator

import anthropic

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM_PROMPT = """\
You are an elite geopolitical and financial markets intelligence analyst.
Your job is to identify the TOP trending geopolitical and macroeconomic stories
that are driving financial market moves right now.

Focus areas (in priority order):
1. US foreign policy, trade wars, sanctions, diplomatic tensions
2. US domestic politics with market implications (Fed, fiscal policy, elections)
3. Middle East conflicts and oil/energy market impact
4. China-Taiwan tensions, China-US trade relations
5. Russia-Ukraine war and energy/commodity market effects
6. European political instability and EUR impact
7. Emerging market crises (currency collapses, sovereign debt)
8. Central bank decisions (Fed, ECB, BOJ, BOE, PBoC)
9. Major commodity moves (oil, gold, copper) with geopolitical drivers
10. Corporate/sector news with geopolitical angle (chip export controls, supply chains)

For each story you identify, assess:
- VIRALITY SCORE (1-10): How trending/breaking is this right now?
- MARKET IMPACT (HIGH/MEDIUM/LOW): Does it move markets?
- ENGAGEMENT POTENTIAL (1-10): Will financial Twitter care about this?
- UNIQUE ANGLE: What's the non-obvious insight most analysts are missing?

Output format: Return ONLY valid JSON (no markdown, no preamble) with this structure:
{
  "research_date": "<ISO date>",
  "top_stories": [
    {
      "title": "<compelling headline>",
      "summary": "<2-3 sentence summary of key facts>",
      "market_impact": "<which assets/currencies/sectors are affected and how>",
      "unique_angle": "<non-obvious insight or contrarian view>",
      "virality_score": <1-10>,
      "engagement_potential": <1-10>,
      "key_data_points": ["<stat or fact 1>", "<stat or fact 2>"],
      "hashtags": ["<relevant hashtag 1>", "<relevant hashtag 2>"],
      "tweet_hooks": ["<punchy opening line option 1>", "<punchy opening line option 2>"]
    }
  ],
  "macro_themes": ["<overarching theme 1>", "<overarching theme 2>"],
  "market_sentiment": "<RISK-ON / RISK-OFF / MIXED>",
  "dxy_outlook": "<brief DXY direction and reasoning>"
}

Return exactly 8-10 stories, sorted by (virality_score + engagement_potential) descending.
"""


async def research_trending_stories() -> dict:
    """
    Uses Claude with web search to find the top geopolitical + financial
    stories right now. Returns a structured dict of stories ready for
    tweet generation.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                "Research and identify the top 8-10 most trending geopolitical and "
                "financial market stories RIGHT NOW. Use web search to find:\n\n"
                "1. Breaking news in the last 24 hours on geopolitics (wars, sanctions, "
                "diplomatic events, elections)\n"
                "2. US government actions affecting global markets (tariffs, Fed signals, "
                "treasury moves)\n"
                "3. Central bank news from Fed, ECB, BOJ, BOE with market implications\n"
                "4. Commodity market moves (oil, gold, copper) with geopolitical drivers\n"
                "5. Currency market moves and the macro story behind them\n"
                "6. Anything trending on financial Twitter/X right now\n\n"
                "Search multiple sources: Bloomberg, Reuters, FT, WSJ, Axios, Politico, "
                "Zero Hedge, and X/Twitter trending topics.\n\n"
                "Return ONLY the JSON object as specified in your instructions. "
                "No markdown formatting. No code blocks. Pure JSON only."
            ),
        }
    ]

    full_text = ""
    max_continuations = 6

    for _ in range(max_continuations):
        try:
            async with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=RESEARCH_SYSTEM_PROMPT,
                tools=[
                    {"type": "web_search_20260209", "name": "web_search"},
                    {"type": "web_fetch_20260209", "name": "web_fetch"},
                ],
                messages=messages,
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"
                    ):
                        full_text += event.delta.text

                final = await stream.get_final_message()

            if final.stop_reason == "end_turn":
                break

            if final.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": final.content})
                continue

            break

        except anthropic.APIError as exc:
            logger.error("Research API error: %s", exc)
            raise

    # Parse the JSON response — Claude should return pure JSON
    text = full_text.strip()
    # Strip code fences if Claude added them despite instructions
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse research JSON: %s\nRaw: %s", exc, text[:500])
        # Return a minimal fallback so the system doesn't crash
        return {
            "top_stories": [],
            "macro_themes": [],
            "market_sentiment": "MIXED",
            "dxy_outlook": "Unavailable",
            "error": str(exc),
            "raw_response": text[:2000],
        }


async def stream_research_progress() -> AsyncGenerator[dict, None]:
    """
    Same as research_trending_stories but yields SSE events for the dashboard,
    so users can see the research progress in real time.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                "Research and identify the top 8-10 most trending geopolitical and "
                "financial market stories RIGHT NOW. Use web search to find breaking "
                "geopolitics, Fed/central bank news, commodity moves, and anything "
                "trending on financial Twitter. "
                "Return ONLY the JSON object as specified. No markdown. Pure JSON."
            ),
        }
    ]

    full_text = ""
    max_continuations = 6

    for _ in range(max_continuations):
        current_block_type: str | None = None
        current_tool_name: str | None = None

        try:
            async with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=RESEARCH_SYSTEM_PROMPT,
                tools=[
                    {"type": "web_search_20260209", "name": "web_search"},
                    {"type": "web_fetch_20260209", "name": "web_fetch"},
                ],
                messages=messages,
            ) as stream:
                async for event in stream:
                    etype = event.type

                    if etype == "content_block_start":
                        cb = event.content_block
                        current_block_type = cb.type
                        if cb.type == "server_tool_use":
                            current_tool_name = getattr(cb, "name", "searching")
                            yield {"type": "tool_start", "tool": current_tool_name}

                    elif etype == "content_block_stop":
                        if current_block_type == "server_tool_use" and current_tool_name:
                            yield {"type": "tool_end", "tool": current_tool_name}
                        current_block_type = None
                        current_tool_name = None

                    elif etype == "content_block_delta":
                        delta = event.delta
                        if getattr(delta, "type", None) == "text_delta":
                            full_text += delta.text
                            yield {"type": "delta", "text": delta.text}

                final = await stream.get_final_message()

            if final.stop_reason == "end_turn":
                break

            if final.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": final.content})
                continue

            break

        except anthropic.APIError as exc:
            yield {"type": "error", "message": str(exc)}
            return

    # Try to parse and yield the structured result
    text = full_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        data = json.loads(text)
        yield {"type": "stories", "data": data}
    except json.JSONDecodeError:
        yield {"type": "stories", "data": {"top_stories": [], "raw": text[:2000]}}

    yield {"type": "done"}
