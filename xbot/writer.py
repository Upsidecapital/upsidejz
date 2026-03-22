"""
Tweet content writer — generates 10 high-engagement X posts per day,
optimized for the X algorithm and geopolitical/financial audience growth.
"""

import os
import json
import logging
from datetime import datetime
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# ── X Algorithm Optimization Notes ─────────────────────────────────────────
# 1. Original tweets with media or strong text get boosted over retweets
# 2. Replies in first hour are heavily weighted (reply bait questions work)
# 3. Early engagement (likes/replies in first 30 min) is critical
# 4. Threads (3-7 tweets) get more reach than single tweets
# 5. Hooks in first line determine whether people expand long tweets
# 6. Data/statistics in tweets increase credibility and saves
# 7. Controversial-but-defensible takes drive reply chains
# 8. Specific hashtags (#Geopolitics, #Markets, #Trading) not tag spam
# 9. Post when your audience is online: 7-9 AM ET, 12 PM ET, 3-5 PM ET, 9 PM ET
# 10. Bookmarks and shares now matter more than likes for algorithmic reach
# ────────────────────────────────────────────────────────────────────────────

WRITER_SYSTEM_PROMPT = """\
You are a top-tier financial Twitter personality — think a cross between
Geopolitical Futures, ZeroHedge, and a sharp macro trader with 500k followers.
Your posts are known for:
- Cutting through mainstream narratives to the real story
- Presenting data and facts that surprise people
- Sharp, confident takes that spark debate
- Making complex geopolitics simple and actionable for traders

TWEET WRITING RULES:
1. HOOK FIRST: First line must grab attention immediately. Use numbers, bold claims,
   or surprising facts. Never start with "I think" or "In my opinion".
2. PUNCHY: Sentences are short. No fluff. Every word earns its place.
3. SPECIFIC: Vague tweets get ignored. Include % moves, dollar amounts, dates, names.
4. THREADED: Some posts should be 3-5 tweet threads (marked as thread) for complex topics.
   Threads get 2-3x the reach of single tweets.
5. QUESTION BAIT: End some tweets with a question to drive replies.
6. CONTRARIAN: Include at least 2-3 takes that challenge the mainstream narrative.
7. HASHTAGS: Max 2-3 relevant hashtags. Tag spam kills reach. Use only if they add value.
8. LENGTH: Single tweets max 260 chars (leave room for engagement).
   Thread tweets can be up to 280 chars each.
9. NO EMOJIS OVERLOAD: Max 1-2 per tweet. Financial audience hates clutter.
10. CALL TO ACTION: Some posts should end with "RT if you agree" or "Save this thread"
    or "Follow for daily geopolitical market intel".

CONTENT MIX FOR 10 POSTS (follow this formula for maximum growth):
- 2 breaking news / hot takes (ride the algo wave on trending topics)
- 2 data-driven insights (stats, charts references, hard numbers)
- 2 contrarian/non-consensus takes (the takes that go viral)
- 1 educational thread (3-5 tweets explaining a complex topic simply)
- 1 prediction/forecast (people love to react to predictions)
- 1 "most people don't know this" post (surprise = shares)
- 1 market brief / morning/evening summary

GROWTH TACTICS BAKED IN:
- Thread starters get MORE reach — use threads for your best content
- Ask questions that your target audience will debate in replies
- Reference specific data points that people will want to save
- Create posts series (e.g. "THREAD: Why gold is hitting $3000 [1/5]")
- Tease follow-up content to drive follows

Output ONLY valid JSON. No markdown. No preamble. Structure:
{
  "posts": [
    {
      "post_id": 1,
      "post_type": "single|thread|breaking",
      "optimal_time_slot": "07:00|08:30|10:00|12:00|13:30|15:30|17:00|19:00|21:00|22:30",
      "topic": "<brief topic label>",
      "content_strategy": "breaking|data|contrarian|educational|prediction|unknown_fact|summary",
      "tweets": ["<tweet text 1>", "<tweet text 2 if thread>"],
      "hashtags": ["<hashtag 1>", "<hashtag 2>"],
      "estimated_engagement": "high|medium",
      "engagement_hook": "<what makes this post likely to go viral>"
    }
  ]
}

Times are in ET (Eastern Time). Spread posts across the day.
"""

# Optimal posting times for financial/geopolitical audience (Eastern Time)
OPTIMAL_POST_TIMES = [
    "07:00",  # Pre-market: traders checking overnight news
    "08:30",  # US economic data releases often at 8:30 AM ET
    "09:45",  # Post-market open buzz
    "11:00",  # Mid-morning — European market overlap
    "12:30",  # Lunch scroll time
    "14:00",  # Afternoon — post-lunch engagement spike
    "15:30",  # 30 min before US close — high trader activity
    "16:30",  # Post-close analysis time
    "19:00",  # Evening — US audience peak social time
    "21:30",  # Night owls + Asia market pre-open
]


async def generate_daily_posts(research_data: dict) -> list[dict]:
    """
    Takes research data from researcher.py and generates 10 optimized X posts.
    Returns a list of post dicts with content and metadata.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    today = datetime.now().strftime("%A, %B %d, %Y")
    stories_json = json.dumps(research_data, indent=2)

    user_message = f"""
Today is {today}.

Here is today's research data on trending geopolitical and financial stories:

{stories_json}

Generate exactly 10 X (Twitter) posts for today based on this research.
Follow the content mix formula: 2 breaking hot takes, 2 data-driven insights,
2 contrarian takes, 1 educational thread (3-5 tweets), 1 prediction,
1 "most people don't know this" post, 1 market summary.

Assign each post one of these optimal time slots (no repeats):
{', '.join(OPTIMAL_POST_TIMES)}

CRITICAL RULES:
- Each tweet in a thread must be ≤280 characters
- Single tweets must be ≤260 characters (leave room for engagement)
- Include 0-2 hashtags max per post (no spam)
- Make the first tweet of every thread a standalone hook that works alone
- At least 3 posts should end with a question or call to action
- At least 2 posts should include specific numbers/percentages
- The educational thread should be 4-5 tweets explaining something complex simply

Return ONLY the JSON. No markdown. No code blocks. Pure JSON.
"""

    messages = [{"role": "user", "content": user_message}]
    full_text = ""
    max_continuations = 4

    for _ in range(max_continuations):
        try:
            response = await client.messages.create(
                model="claude-opus-4-6",
                max_tokens=4096,
                system=WRITER_SYSTEM_PROMPT,
                messages=messages,
            )

            for block in response.content:
                if hasattr(block, "text"):
                    full_text += block.text

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "max_tokens":
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": "Continue from where you left off. Return only the remaining JSON."})
                continue

            break

        except anthropic.APIError as exc:
            logger.error("Writer API error: %s", exc)
            raise

    text = full_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        data = json.loads(text)
        posts = data.get("posts", [])
        # Enrich posts with metadata
        for post in posts:
            post.setdefault("status", "pending")
            post.setdefault("posted_at", None)
            post.setdefault("tweet_ids", [])
        return posts
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse posts JSON: %s\nRaw: %s", exc, text[:500])
        return []


async def generate_single_post(topic: str, story_summary: str, post_type: str = "single") -> dict:
    """
    Generate a single post on demand for a specific topic.
    Useful for breaking news that needs an immediate response.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    user_message = f"""
Generate a single high-engagement X post about this topic:

Topic: {topic}
Story: {story_summary}
Post type: {post_type} (single tweet or thread)

Make it punchy, data-driven, and optimized for the X algorithm.
Return ONLY JSON with this structure:
{{
  "post_type": "single|thread",
  "topic": "{topic}",
  "tweets": ["<tweet text>"],
  "hashtags": ["<hashtag>"],
  "engagement_hook": "<why this will get engagement>"
}}
"""

    try:
        response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=WRITER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

        return json.loads(text)

    except (anthropic.APIError, json.JSONDecodeError) as exc:
        logger.error("Single post generation failed: %s", exc)
        return {}


def validate_tweet_lengths(posts: list[dict]) -> list[dict]:
    """
    Validates and truncates any tweets that exceed X's character limits.
    Single tweets: 280 chars. We target 260 to leave room.
    """
    for post in posts:
        tweets = post.get("tweets", [])
        validated = []
        for i, tweet in enumerate(tweets):
            if len(tweet) > 280:
                # Truncate at last word boundary before 277 chars, add "..."
                truncated = tweet[:277].rsplit(" ", 1)[0] + "..."
                logger.warning(
                    "Tweet truncated from %d to %d chars", len(tweet), len(truncated)
                )
                validated.append(truncated)
            else:
                validated.append(tweet)
        post["tweets"] = validated

    return posts
