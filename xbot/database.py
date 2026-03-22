"""
Database layer — SQLite via aiosqlite.
Tracks sessions, generated posts, and posting history to avoid duplicates.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DB_PATH = os.environ.get("XBOT_DB_PATH", "xbot_data.db")


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def initialize(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    date        TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    research    TEXT,          -- JSON blob
                    post_count  INTEGER DEFAULT 0
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      INTEGER REFERENCES sessions(id),
                    date            TEXT NOT NULL,
                    post_index      INTEGER,
                    post_type       TEXT,
                    topic           TEXT,
                    optimal_slot    TEXT,
                    content_strategy TEXT,
                    tweets_json     TEXT,     -- JSON array of tweet texts
                    hashtags_json   TEXT,     -- JSON array
                    status          TEXT DEFAULT 'pending',
                    tweet_ids_json  TEXT,     -- JSON array of posted tweet IDs
                    error           TEXT,
                    posted_at       TEXT,
                    created_at      TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT NOT NULL,
                    followers_count INTEGER,
                    following_count INTEGER,
                    tweet_count     INTEGER,
                    checked_at      TEXT NOT NULL
                )
            """)

            await db.commit()
            logger.info("Database initialized at %s", self.db_path)

    async def save_session(
        self, date: str, research_data: dict, posts: list[dict]
    ) -> int:
        """
        Save a research session and all its generated posts.
        Returns the session ID.
        """
        now = datetime.now(ET).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            # Create session record
            cursor = await db.execute(
                "INSERT INTO sessions (date, created_at, research, post_count) VALUES (?, ?, ?, ?)",
                (date, now, json.dumps(research_data), len(posts)),
            )
            session_id = cursor.lastrowid

            # Save each post
            for i, post in enumerate(posts):
                cursor2 = await db.execute(
                    """
                    INSERT INTO posts (
                        session_id, date, post_index, post_type, topic,
                        optimal_slot, content_strategy, tweets_json,
                        hashtags_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        date,
                        i,
                        post.get("post_type", "single"),
                        post.get("topic", ""),
                        post.get("optimal_time_slot", ""),
                        post.get("content_strategy", ""),
                        json.dumps(post.get("tweets", [])),
                        json.dumps(post.get("hashtags", [])),
                        "pending",
                        now,
                    ),
                )
                # Attach the DB ID to the post dict for later updates
                post["db_id"] = cursor2.lastrowid

            await db.commit()
            logger.info("Session %d saved with %d posts", session_id, len(posts))
            return session_id

    async def get_today_posts(self, date: str) -> list[dict]:
        """
        Retrieve today's posts from the database.
        Returns them as dicts with db_id attached.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM posts WHERE date = ? ORDER BY optimal_slot",
                (date,),
            ) as cursor:
                rows = await cursor.fetchall()

        posts = []
        for row in rows:
            post = dict(row)
            post["tweets"] = json.loads(post.pop("tweets_json", "[]"))
            post["hashtags"] = json.loads(post.pop("hashtags_json", "[]"))
            post["tweet_ids"] = json.loads(post.pop("tweet_ids_json") or "[]")
            post["optimal_time_slot"] = post.pop("optimal_slot", "")
            post["db_id"] = post["id"]
            posts.append(post)

        return posts

    async def update_post_status(
        self,
        post_db_id: int,
        status: str,
        tweet_ids: Optional[list[str]] = None,
        error: Optional[str] = None,
    ):
        """Update the status and result of a post after attempting to publish."""
        now = datetime.now(ET).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE posts SET
                    status = ?,
                    tweet_ids_json = ?,
                    error = ?,
                    posted_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(tweet_ids or []),
                    error,
                    now if status == "posted" else None,
                    post_db_id,
                ),
            )
            await db.commit()

    async def get_posting_stats(self, days: int = 7) -> dict:
        """Return posting statistics for the last N days."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                """
                SELECT
                    date,
                    COUNT(*) as total,
                    SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) as posted,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
                FROM posts
                GROUP BY date
                ORDER BY date DESC
                LIMIT ?
                """,
                (days,),
            ) as cursor:
                rows = await cursor.fetchall()

        return {
            "days": [dict(row) for row in rows],
            "total_posted": sum(r["posted"] for r in rows),
        }

    async def get_recent_posts(self, limit: int = 20) -> list[dict]:
        """Get the most recently posted tweets for the dashboard."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, date, post_type, topic, tweets_json, status,
                       tweet_ids_json, posted_at, optimal_slot, content_strategy
                FROM posts
                ORDER BY COALESCE(posted_at, created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()

        posts = []
        for row in rows:
            post = dict(row)
            post["tweets"] = json.loads(post.pop("tweets_json", "[]"))
            post["tweet_ids"] = json.loads(post.pop("tweet_ids_json") or "[]")
            posts.append(post)

        return posts

    async def save_follower_stats(self, followers: int, following: int, tweet_count: int):
        """Save a snapshot of follower count for growth tracking."""
        now = datetime.now(ET).isoformat()
        today = datetime.now(ET).date().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO stats (date, followers_count, following_count, tweet_count, checked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (today, followers, following, tweet_count, now),
            )
            await db.commit()

    async def get_follower_history(self, days: int = 30) -> list[dict]:
        """Return follower count history for growth chart."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT date, MAX(followers_count) as followers,
                       MAX(following_count) as following
                FROM stats
                GROUP BY date
                ORDER BY date DESC
                LIMIT ?
                """,
                (days,),
            ) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in reversed(rows)]
