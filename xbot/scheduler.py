"""
Scheduler — orchestrates daily research, content generation, and posting.
Posts 10 times per day at optimal X engagement windows (Eastern Time).
"""

import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from researcher import research_trending_stories
from writer import generate_daily_posts, validate_tweet_lengths
from poster import post_scheduled_post, verify_credentials
from database import Database

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── Posting schedule (Eastern Time) ────────────────────────────────────────
# These times are chosen to hit peak financial Twitter engagement windows:
# - Pre-market (7:00-8:30): traders reading overnight news
# - Market open (9:45-11:00): first major activity window
# - Midday (12:30-14:00): lunch scroll, European close
# - Pre/post close (15:30-16:30): traders winding down, high activity
# - Evening (19:00-21:30): evening news cycle, casual finance audience
# ────────────────────────────────────────────────────────────────────────────

POST_SCHEDULE = [
    {"slot": "07:00", "hour": 7,  "minute": 0},
    {"slot": "08:30", "hour": 8,  "minute": 30},
    {"slot": "09:45", "hour": 9,  "minute": 45},
    {"slot": "11:00", "hour": 11, "minute": 0},
    {"slot": "12:30", "hour": 12, "minute": 30},
    {"slot": "14:00", "hour": 14, "minute": 0},
    {"slot": "15:30", "hour": 15, "minute": 30},
    {"slot": "16:30", "hour": 16, "minute": 30},
    {"slot": "19:00", "hour": 19, "minute": 0},
    {"slot": "21:30", "hour": 21, "minute": 30},
]

# Research runs at 6:00 AM ET daily, giving 1 hour before first post
RESEARCH_HOUR = 6
RESEARCH_MINUTE = 0


class DailyPostingScheduler:
    """
    Manages the full daily posting cycle:
    1. 6:00 AM ET — Research trending stories + generate 10 posts
    2. Each scheduled time — Post the corresponding tweet/thread
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=ET)
        self.db = Database()
        self._today_posts: list[dict] = []
        self._research_complete = False

    async def initialize(self):
        """Initialize database and verify credentials."""
        await self.db.initialize()
        creds = verify_credentials()
        if creds["success"]:
            logger.info(
                "X credentials verified: @%s (%d followers)",
                creds["username"], creds["followers"]
            )
        else:
            logger.error("X credential check failed: %s", creds.get("error"))

    def start(self):
        """Start the scheduler with all jobs registered."""
        # Daily research job at 6:00 AM ET
        self.scheduler.add_job(
            self._run_daily_research,
            CronTrigger(hour=RESEARCH_HOUR, minute=RESEARCH_MINUTE, timezone=ET),
            id="daily_research",
            name="Daily Research & Content Generation",
            replace_existing=True,
            misfire_grace_time=600,  # 10 min grace
        )

        # Register a posting job for each time slot
        for slot in POST_SCHEDULE:
            self.scheduler.add_job(
                self._post_for_slot,
                CronTrigger(hour=slot["hour"], minute=slot["minute"], timezone=ET),
                id=f"post_{slot['slot'].replace(':', '')}",
                name=f"Post at {slot['slot']} ET",
                kwargs={"time_slot": slot["slot"]},
                replace_existing=True,
                misfire_grace_time=300,  # 5 min grace
            )

        self.scheduler.start()
        logger.info(
            "Scheduler started. Research at %02d:%02d ET. "
            "Posting at: %s",
            RESEARCH_HOUR, RESEARCH_MINUTE,
            ", ".join(s["slot"] for s in POST_SCHEDULE)
        )

    def stop(self):
        """Gracefully stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    async def _run_daily_research(self):
        """
        Morning research job: scrapes news, generates today's 10 posts,
        and stores them in the database ready for posting.
        """
        today = datetime.now(ET).date().isoformat()
        logger.info("Starting daily research for %s", today)
        self._research_complete = False
        self._today_posts = []

        try:
            # Research trending stories
            logger.info("Researching trending geopolitical stories...")
            research = await research_trending_stories()

            story_count = len(research.get("top_stories", []))
            logger.info("Research complete: %d stories found", story_count)

            # Generate 10 posts from the research
            logger.info("Generating 10 posts from research...")
            posts = await generate_daily_posts(research)
            posts = validate_tweet_lengths(posts)

            if not posts:
                logger.error("Post generation returned 0 posts — check API")
                return

            # Save to database
            session_id = await self.db.save_session(
                date=today,
                research_data=research,
                posts=posts,
            )

            self._today_posts = posts
            self._research_complete = True
            logger.info(
                "Daily content ready: %d posts saved (session %s)",
                len(posts), session_id
            )

        except Exception as exc:
            logger.error("Daily research failed: %s", exc, exc_info=True)

    async def _post_for_slot(self, time_slot: str):
        """
        Post the tweet/thread assigned to this time slot.
        If today's posts aren't ready, trigger research first.
        """
        today = datetime.now(ET).date().isoformat()

        # If research hasn't run (e.g. bot started mid-day), run it now
        if not self._research_complete or not self._today_posts:
            logger.info("Posts not ready for slot %s — checking DB", time_slot)
            self._today_posts = await self.db.get_today_posts(today)

            if not self._today_posts:
                logger.info("No posts in DB — running research now")
                await self._run_daily_research()

        if not self._today_posts:
            logger.error("Still no posts after research — skipping slot %s", time_slot)
            return

        # Find the post assigned to this time slot
        post = next(
            (p for p in self._today_posts
             if p.get("optimal_time_slot") == time_slot
             and p.get("status") == "pending"),
            None,
        )

        if not post:
            # Fallback: find any pending post
            post = next(
                (p for p in self._today_posts if p.get("status") == "pending"),
                None,
            )

        if not post:
            logger.info("No pending posts for slot %s — all posted", time_slot)
            return

        logger.info("Posting for slot %s: %s", time_slot, post.get("topic", "unknown"))

        # Mark as in-progress in DB
        await self.db.update_post_status(post["db_id"], "in_progress")

        # Post to X
        post = await post_scheduled_post(post)

        # Update database with result
        await self.db.update_post_status(
            post["db_id"],
            post["status"],
            tweet_ids=post.get("tweet_ids", []),
            error=post.get("error"),
        )

        if post["status"] == "posted":
            logger.info(
                "Successfully posted slot %s — tweet IDs: %s",
                time_slot, post.get("tweet_ids")
            )
        else:
            logger.error(
                "Failed to post slot %s: %s",
                time_slot, post.get("error")
            )

    async def run_research_now(self) -> dict:
        """
        Trigger research and post generation immediately (for manual runs/dashboard).
        Returns the research results.
        """
        await self._run_daily_research()
        return {
            "posts_generated": len(self._today_posts),
            "posts": self._today_posts,
            "research_complete": self._research_complete,
        }

    async def post_now(self, post_index: int) -> dict:
        """
        Immediately post a specific post by index (for manual posting from dashboard).
        """
        if post_index >= len(self._today_posts):
            return {"success": False, "error": "Post index out of range"}

        post = self._today_posts[post_index]
        if post.get("status") == "posted":
            return {"success": False, "error": "Already posted"}

        post = await post_scheduled_post(post)
        self._today_posts[post_index] = post

        if post.get("db_id"):
            await self.db.update_post_status(
                post["db_id"],
                post["status"],
                tweet_ids=post.get("tweet_ids", []),
                error=post.get("error"),
            )

        return {
            "success": post["status"] == "posted",
            "tweet_ids": post.get("tweet_ids", []),
            "error": post.get("error"),
        }

    def get_next_jobs(self) -> list[dict]:
        """Return upcoming scheduled jobs for dashboard display."""
        jobs = []
        for job in self.scheduler.get_jobs():
            next_run = job.next_run_time
            if next_run:
                jobs.append({
                    "id": job.id,
                    "name": job.name,
                    "next_run": next_run.isoformat(),
                })
        return sorted(jobs, key=lambda j: j["next_run"])
