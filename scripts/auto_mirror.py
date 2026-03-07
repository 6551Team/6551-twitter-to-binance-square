#!/usr/bin/env python3
"""
Twitter → Binance Square Auto-Mirror Script

Monitors Twitter accounts or topics via the 6551 API,
transforms tweet content, and posts to Binance Square.

Usage:
    python auto_mirror.py --config mirror_config.json
    python auto_mirror.py --mode account --accounts VitalikButerin --interval 300

Environment variables required:
    TWITTER_TOKEN   - 6551 API token (get at https://6551.io/mcp)
    SQUARE_API_KEY  - Binance Square OpenAPI Key
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mirror")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TWITTER_API_BASE = "https://ai.6551.io"
SQUARE_API_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
MAX_SQUARE_CONTENT_LENGTH = 2000
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 5  # seconds

DEFAULT_CONFIG = {
    "mode": "account",           # account | search | hashtag
    "accounts": [],              # Twitter usernames (mode=account)
    "keywords": "",              # Search keywords (mode=search)
    "hashtag": "",               # Hashtag (mode=hashtag)
    "poll_interval_seconds": 300,
    "min_likes": 0,
    "min_retweets": 0,
    "max_posts_per_run": 5,
    "include_replies": False,
    "include_retweets": False,
    "translate": False,
    "translate_to": "zh",
    "content_template": "{content}\n\n{source_attribution}\n{tool_attribution}\n{hashtags}",
    "show_source": True,
    "show_tool_attribution": True,
    "add_hashtags": [],
    "state_file": "mirror_state.json",
    "dry_run": False,
}

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Shutdown signal received, finishing current cycle...")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_json(url: str, headers: dict, payload: dict, retries: int = MAX_RETRIES) -> dict:
    """POST JSON and return parsed response. Retries on transient errors."""
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (URLError, HTTPError, OSError) as exc:
            if attempt == retries:
                raise
            wait = RETRY_BACKOFF_BASE * attempt
            log.warning("Request failed (attempt %d/%d): %s — retrying in %ds",
                        attempt, retries, exc, wait)
            time.sleep(wait)
    return {}

# ---------------------------------------------------------------------------
# Twitter (6551 API)
# ---------------------------------------------------------------------------

def fetch_user_tweets(token: str, username: str, max_results: int = 10,
                      include_replies: bool = False, include_retweets: bool = False) -> list:
    """Fetch latest tweets from a specific user."""
    url = f"{TWITTER_API_BASE}/open/twitter_user_tweets"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "username": username,
        "maxResults": max_results,
        "product": "Latest",
        "includeReplies": include_replies,
        "includeRetweets": include_retweets,
    }
    resp = _post_json(url, headers, payload)
    return _extract_tweets(resp)


def search_tweets(token: str, keywords: str = "", hashtag: str = "",
                  min_likes: int = 0, min_retweets: int = 0,
                  max_results: int = 10, lang: str = "") -> list:
    """Search tweets by keywords or hashtag."""
    url = f"{TWITTER_API_BASE}/open/twitter_search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"maxResults": max_results, "product": "Top"}
    if keywords:
        payload["keywords"] = keywords
    if hashtag:
        payload["hashtag"] = hashtag
    if min_likes > 0:
        payload["minLikes"] = min_likes
    if min_retweets > 0:
        payload["minRetweets"] = min_retweets
    if lang:
        payload["lang"] = lang

    resp = _post_json(url, headers, payload)
    return _extract_tweets(resp)


def _extract_tweets(resp: dict) -> list:
    """Normalize the API response into a list of tweet dicts."""
    if not resp:
        return []
    # 6551 API may return data in different structures
    if isinstance(resp, list):
        return resp
    if "data" in resp:
        data = resp["data"]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # could be nested like {"tweets": [...]}
            for key in ("tweets", "results", "items"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data]
    # If root has tweet-like keys
    if "id" in resp and "text" in resp:
        return [resp]
    return []

# ---------------------------------------------------------------------------
# Content transformation
# ---------------------------------------------------------------------------

def transform_tweet(tweet: dict, config: dict) -> str:
    """Transform a tweet into Binance Square post content."""
    text = tweet.get("text", "")
    username = tweet.get("userScreenName", tweet.get("screenName", "unknown"))

    # Clean up Twitter-specific elements
    # Remove t.co links
    text = re.sub(r'https?://t\.co/\S+', '', text).strip()
    # Remove leading RT @user: pattern
    text = re.sub(r'^RT @\w+:\s*', '', text).strip()
    # Collapse multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Extract original hashtags from tweet
    hashtags_list = tweet.get("hashtags", [])
    if isinstance(hashtags_list, list):
        original_tags = [f"#{tag}" if not tag.startswith("#") else tag for tag in hashtags_list]
    else:
        original_tags = []

    # Add configured extra hashtags
    extra_tags = [f"#{tag}" if not tag.startswith("#") else tag for tag in config.get("add_hashtags", [])]
    all_tags = list(dict.fromkeys(original_tags + extra_tags))  # dedupe preserving order
    hashtags_str = " ".join(all_tags)

    # Build attribution lines based on config
    source_attribution = ""
    if config.get("show_source", True):
        source_attribution = f"Source: @{username} on X"

    tool_attribution = ""
    if config.get("show_tool_attribution", True):
        tool_attribution = "Publish by using 6551 twitter mirror tool"

    # Apply template
    template = config.get("content_template", DEFAULT_CONFIG["content_template"])
    content = template.format(
        content=text,
        username=username,
        hashtags=hashtags_str,
        source_attribution=source_attribution,
        tool_attribution=tool_attribution,
    )

    # Clean up empty lines from disabled attributions
    content = re.sub(r'\n{3,}', '\n\n', content).strip()

    # Enforce length limit
    if len(content) > MAX_SQUARE_CONTENT_LENGTH:
        # Trim content, keep suffix
        suffix_parts = []
        if source_attribution:
            suffix_parts.append(source_attribution)
        if tool_attribution:
            suffix_parts.append(tool_attribution)
        if hashtags_str:
            suffix_parts.append(hashtags_str)
        suffix = "\n\n" + "\n".join(suffix_parts)
        max_text_len = MAX_SQUARE_CONTENT_LENGTH - len(suffix) - 3
        text_trimmed = text[:max_text_len] + "..."
        content = template.format(
            content=text_trimmed,
            username=username,
            hashtags=hashtags_str,
            source_attribution=source_attribution,
            tool_attribution=tool_attribution,
        )
        content = re.sub(r'\n{3,}', '\n\n', content).strip()

    return content


def passes_filter(tweet: dict, config: dict) -> bool:
    """Check if tweet meets minimum engagement thresholds."""
    likes = tweet.get("favoriteCount", 0) or 0
    retweets = tweet.get("retweetCount", 0) or 0
    if likes < config.get("min_likes", 0):
        return False
    if retweets < config.get("min_retweets", 0):
        return False
    return True

# ---------------------------------------------------------------------------
# Binance Square posting
# ---------------------------------------------------------------------------

def post_to_square(api_key: str, content: str) -> dict:
    """Post content to Binance Square. Returns parsed API response."""
    headers = {
        "X-Square-OpenAPI-Key": api_key,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }
    payload = {"bodyTextOnly": content}
    return _post_json(SQUARE_API_URL, headers, payload)

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    """Load persisted state from disk."""
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupted state file, starting fresh")
    return {
        "posted_tweet_ids": [],
        "last_poll_time": None,
        "post_count_today": 0,
        "last_reset_date": str(date.today()),
        "post_log": [],
    }


def save_state(state: dict, path: str):
    """Persist state to disk."""
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_daily_counter(state: dict):
    """Reset daily post counter if date has changed."""
    today = str(date.today())
    if state.get("last_reset_date") != today:
        state["post_count_today"] = 0
        state["last_reset_date"] = today

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle(config: dict, state: dict, twitter_token: str, square_key: str) -> int:
    """
    Execute one polling cycle.
    Returns the number of posts made.
    """
    mode = config["mode"]
    tweets = []

    # --- Fetch tweets ---
    try:
        if mode == "account":
            for username in config["accounts"]:
                log.info("Fetching tweets from @%s ...", username)
                batch = fetch_user_tweets(
                    twitter_token, username,
                    max_results=config.get("max_posts_per_run", 5) * 2,
                    include_replies=config.get("include_replies", False),
                    include_retweets=config.get("include_retweets", False),
                )
                tweets.extend(batch)
        elif mode == "search":
            log.info("Searching tweets for: %s", config.get("keywords", ""))
            tweets = search_tweets(
                twitter_token,
                keywords=config.get("keywords", ""),
                min_likes=config.get("min_likes", 0),
                min_retweets=config.get("min_retweets", 0),
                max_results=config.get("max_posts_per_run", 5) * 2,
            )
        elif mode == "hashtag":
            log.info("Searching tweets for #%s", config.get("hashtag", ""))
            tweets = search_tweets(
                twitter_token,
                hashtag=config.get("hashtag", ""),
                min_likes=config.get("min_likes", 0),
                min_retweets=config.get("min_retweets", 0),
                max_results=config.get("max_posts_per_run", 5) * 2,
            )
        else:
            log.error("Unknown mode: %s", mode)
            return 0
    except Exception as exc:
        log.error("Failed to fetch tweets: %s", exc)
        return 0

    if not tweets:
        log.info("No tweets found this cycle")
        return 0

    log.info("Fetched %d tweets", len(tweets))

    # --- Filter, deduplicate, post ---
    posted_ids = set(state.get("posted_tweet_ids", []))
    posts_made = 0
    max_posts = config.get("max_posts_per_run", 5)

    for tweet in tweets:
        if _shutdown:
            break
        if posts_made >= max_posts:
            log.info("Reached max_posts_per_run (%d), stopping", max_posts)
            break

        tweet_id = str(tweet.get("id", ""))
        if not tweet_id:
            continue
        if tweet_id in posted_ids:
            log.debug("Skipping already-posted tweet %s", tweet_id)
            continue
        if not passes_filter(tweet, config):
            username = tweet.get("userScreenName", "?")
            log.info("Skipping tweet %s from @%s (below engagement threshold)", tweet_id, username)
            continue

        # Transform
        content = transform_tweet(tweet, config)
        username = tweet.get("userScreenName", tweet.get("screenName", "unknown"))

        if config.get("dry_run"):
            log.info("[DRY RUN] Would post tweet %s from @%s:\n%s\n", tweet_id, username, content)
            posted_ids.add(tweet_id)
            state["posted_tweet_ids"] = list(posted_ids)
            posts_made += 1
            continue

        # Post to Square
        try:
            log.info("Posting tweet %s from @%s to Square...", tweet_id, username)
            resp = post_to_square(square_key, content)
            code = resp.get("code", "")

            if code == "000000":
                post_id = resp.get("data", {}).get("id", "")
                square_url = f"https://www.binance.com/square/post/{post_id}" if post_id else "N/A"
                log.info("SUCCESS — Square post: %s", square_url)

                posted_ids.add(tweet_id)
                state["posted_tweet_ids"] = list(posted_ids)
                state["post_count_today"] = state.get("post_count_today", 0) + 1
                state["post_log"].append({
                    "tweet_id": tweet_id,
                    "square_post_id": post_id,
                    "square_url": square_url,
                    "username": username,
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                })
                posts_made += 1

            elif code == "220009":
                log.warning("Daily post limit exceeded! Stopping posts for today.")
                break
            elif code in ("220003", "220004"):
                log.error("Square API key issue (code=%s). Stopping.", code)
                break
            elif code in ("20002", "20022"):
                log.warning("Sensitive content detected for tweet %s, skipping.", tweet_id)
                posted_ids.add(tweet_id)  # mark so we don't retry
                state["posted_tweet_ids"] = list(posted_ids)
            else:
                msg = resp.get("message", "unknown error")
                log.error("Square API error code=%s message=%s — skipping tweet %s", code, msg, tweet_id)

        except Exception as exc:
            log.error("Failed to post tweet %s: %s", tweet_id, exc)

        # Small delay between posts to be polite
        if posts_made < max_posts:
            time.sleep(2)

    state["last_poll_time"] = datetime.now(timezone.utc).isoformat()
    return posts_made


def main():
    parser = argparse.ArgumentParser(
        description="Auto-mirror Twitter/X content to Binance Square"
    )
    parser.add_argument("--config", "-c", help="Path to JSON config file")
    parser.add_argument("--mode", choices=["account", "search", "hashtag"],
                        help="Monitoring mode")
    parser.add_argument("--accounts", help="Comma-separated Twitter usernames")
    parser.add_argument("--keywords", help="Search keywords")
    parser.add_argument("--hashtag", help="Hashtag to monitor (without #)")
    parser.add_argument("--interval", type=int, help="Poll interval in seconds")
    parser.add_argument("--min-likes", type=int, help="Minimum likes threshold")
    parser.add_argument("--max-posts", type=int, help="Max posts per cycle")
    parser.add_argument("--translate", action="store_true", help="Enable translation")
    parser.add_argument("--translate-to", help="Target language code")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--state-file", help="Path to state file")
    args = parser.parse_args()

    # --- Load config ---
    config = dict(DEFAULT_CONFIG)
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                file_config = json.load(f)
            config.update(file_config)
            log.info("Loaded config from %s", args.config)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to load config file: %s", exc)
            sys.exit(1)

    # CLI overrides
    if args.mode:
        config["mode"] = args.mode
    if args.accounts:
        config["accounts"] = [a.strip() for a in args.accounts.split(",")]
    if args.keywords:
        config["keywords"] = args.keywords
    if args.hashtag:
        config["hashtag"] = args.hashtag
    if args.interval:
        config["poll_interval_seconds"] = args.interval
    if args.min_likes is not None:
        config["min_likes"] = args.min_likes
    if args.max_posts:
        config["max_posts_per_run"] = args.max_posts
    if args.translate:
        config["translate"] = True
    if args.translate_to:
        config["translate_to"] = args.translate_to
    if args.dry_run:
        config["dry_run"] = True
    if args.state_file:
        config["state_file"] = args.state_file

    # --- Validate ---
    twitter_token = os.environ.get("TWITTER_TOKEN", "")
    square_key = os.environ.get("SQUARE_API_KEY", "")

    if not twitter_token:
        log.error("TWITTER_TOKEN environment variable not set. Get yours at https://6551.io/mcp")
        sys.exit(1)
    if not square_key and not config.get("dry_run"):
        log.error("SQUARE_API_KEY environment variable not set.")
        sys.exit(1)

    if config["mode"] == "account" and not config["accounts"]:
        log.error("No accounts specified. Use --accounts or set 'accounts' in config file.")
        sys.exit(1)
    if config["mode"] == "search" and not config["keywords"]:
        log.error("No keywords specified. Use --keywords or set 'keywords' in config file.")
        sys.exit(1)
    if config["mode"] == "hashtag" and not config["hashtag"]:
        log.error("No hashtag specified. Use --hashtag or set 'hashtag' in config file.")
        sys.exit(1)

    # --- Load state ---
    state = load_state(config["state_file"])

    # --- Print startup info ---
    mode_desc = {
        "account": f"accounts: {', '.join(config['accounts'])}",
        "search": f"keywords: {config['keywords']}",
        "hashtag": f"#{config['hashtag']}",
    }
    log.info("=" * 60)
    log.info("Twitter → Binance Square Auto-Mirror")
    log.info("Mode: %s (%s)", config["mode"], mode_desc.get(config["mode"], ""))
    log.info("Poll interval: %ds", config["poll_interval_seconds"])
    log.info("Max posts/cycle: %d", config["max_posts_per_run"])
    log.info("Dry run: %s", config["dry_run"])
    log.info("State file: %s", config["state_file"])
    log.info("Already posted: %d tweets", len(state.get("posted_tweet_ids", [])))
    log.info("=" * 60)

    # --- Main loop ---
    cycle = 0
    while not _shutdown:
        cycle += 1
        reset_daily_counter(state)
        log.info("--- Cycle %d ---", cycle)

        posts = run_cycle(config, state, twitter_token, square_key)
        log.info("Cycle %d complete: %d posts made (today total: %d)",
                 cycle, posts, state.get("post_count_today", 0))

        # Save state after each cycle
        save_state(state, config["state_file"])

        if args.once:
            log.info("--once flag set, exiting after single cycle.")
            break

        if _shutdown:
            break

        log.info("Sleeping %ds until next cycle...", config["poll_interval_seconds"])
        # Sleep in small increments so we can catch shutdown signals
        for _ in range(config["poll_interval_seconds"]):
            if _shutdown:
                break
            time.sleep(1)

    # --- Cleanup ---
    save_state(state, config["state_file"])
    log.info("State saved. Goodbye!")


if __name__ == "__main__":
    main()
