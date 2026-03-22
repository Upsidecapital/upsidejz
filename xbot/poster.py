"""
X (Twitter) poster — handles all Twitter API v2 interactions via Tweepy.
Supports single tweets and multi-tweet threads.
"""

import os
import logging
import asyncio
from typing import Optional

import tweepy

logger = logging.getLogger(__name__)


def _get_client() -> tweepy.Client:
    """Create and return an authenticated Tweepy v2 client."""
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=True,
    )


def post_single_tweet(text: str) -> Optional[str]:
    """
    Post a single tweet. Returns the tweet ID on success, None on failure.
    """
    client = _get_client()
    try:
        response = client.create_tweet(text=text)
        tweet_id = str(response.data["id"])
        logger.info("Posted tweet %s: %s", tweet_id, text[:60])
        return tweet_id
    except tweepy.TweepyException as exc:
        logger.error("Failed to post tweet: %s", exc)
        return None


def post_thread(tweets: list[str]) -> list[str]:
    """
    Post a thread of tweets. Each tweet replies to the previous one.
    Returns a list of tweet IDs for all successfully posted tweets.
    Returns empty list if the first tweet fails.
    """
    if not tweets:
        return []

    client = _get_client()
    posted_ids: list[str] = []
    reply_to_id: Optional[str] = None

    for i, text in enumerate(tweets):
        try:
            if reply_to_id:
                response = client.create_tweet(
                    text=text,
                    in_reply_to_tweet_id=reply_to_id,
                )
            else:
                response = client.create_tweet(text=text)

            tweet_id = str(response.data["id"])
            posted_ids.append(tweet_id)
            reply_to_id = tweet_id
            logger.info("Posted thread tweet %d/%d: %s", i + 1, len(tweets), tweet_id)

            # Small delay between thread tweets to avoid rate limits
            if i < len(tweets) - 1:
                import time
                time.sleep(1.5)

        except tweepy.TweepyException as exc:
            logger.error("Failed to post thread tweet %d: %s", i + 1, exc)
            if i == 0:
                # First tweet failed — abort the whole thread
                return []
            # Partial thread — return what we have
            break

    return posted_ids


async def post_tweet_async(text: str) -> Optional[str]:
    """Async wrapper for post_single_tweet."""
    return await asyncio.get_event_loop().run_in_executor(
        None, post_single_tweet, text
    )


async def post_thread_async(tweets: list[str]) -> list[str]:
    """Async wrapper for post_thread."""
    return await asyncio.get_event_loop().run_in_executor(
        None, post_thread, tweets
    )


async def post_scheduled_post(post: dict) -> dict:
    """
    Takes a post dict (from writer.py) and posts it to X.
    Updates the post dict with status and tweet IDs.
    Returns the updated post dict.
    """
    post_type = post.get("post_type", "single")
    tweets = post.get("tweets", [])
    hashtags = post.get("hashtags", [])

    if not tweets:
        post["status"] = "failed"
        post["error"] = "No tweet content"
        return post

    # Append hashtags to the first (or only) tweet if they aren't already embedded
    first_tweet = tweets[0]
    if hashtags:
        hashtag_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags[:2])
        # Only append if they're not already in the tweet
        if not any(h.lstrip("#").lower() in first_tweet.lower() for h in hashtags):
            candidate = f"{first_tweet}\n\n{hashtag_str}"
            if len(candidate) <= 280:
                tweets = [candidate] + tweets[1:]

    try:
        if post_type in ("thread",) or len(tweets) > 1:
            tweet_ids = await post_thread_async(tweets)
            if tweet_ids:
                post["status"] = "posted"
                post["tweet_ids"] = tweet_ids
                logger.info(
                    "Thread posted: %d tweets, first ID: %s",
                    len(tweet_ids), tweet_ids[0]
                )
            else:
                post["status"] = "failed"
                post["error"] = "Thread posting failed"
        else:
            tweet_id = await post_tweet_async(tweets[0])
            if tweet_id:
                post["status"] = "posted"
                post["tweet_ids"] = [tweet_id]
            else:
                post["status"] = "failed"
                post["error"] = "Tweet posting failed"

    except Exception as exc:
        post["status"] = "failed"
        post["error"] = str(exc)
        logger.error("Unexpected error posting: %s", exc)

    return post


def verify_credentials() -> dict:
    """
    Verify that the X API credentials are valid.
    Returns a dict with success status and account info.
    """
    try:
        client = _get_client()
        me = client.get_me(user_fields=["public_metrics", "description"])
        if me.data:
            metrics = me.data.public_metrics or {}
            return {
                "success": True,
                "username": me.data.username,
                "name": me.data.name,
                "followers": metrics.get("followers_count", 0),
                "following": metrics.get("following_count", 0),
                "tweet_count": metrics.get("tweet_count", 0),
            }
        return {"success": False, "error": "No user data returned"}
    except tweepy.TweepyException as exc:
        return {"success": False, "error": str(exc)}
    except KeyError as exc:
        return {"success": False, "error": f"Missing env var: {exc}"}
