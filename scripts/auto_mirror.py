#!/usr/bin/env python3
"""
Mirror Twitter/X posts to Binance Square.

The script supports Square text posts, image posts, video posts, and text tags.
It uses Binance Square OpenAPI v2 media upload endpoints and v1 content/add.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mirror")


TWITTER_API_BASE = "https://ai.6551.io"
SQUARE_API_BASE_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
SQUARE_API_BASE_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"

MAX_SQUARE_CONTENT_LENGTH = 2000
MAX_IMAGE_COUNT = 4
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 5
POLL_INTERVAL_SECONDS = 3
MAX_MEDIA_POLL_RETRIES = 10

CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "account",
    "accounts": [],
    "keywords": "",
    "hashtag": "",
    "poll_interval_seconds": 300,
    "min_likes": 0,
    "min_retweets": 0,
    "max_posts_per_run": 5,
    "include_replies": False,
    "include_retweets": False,
    "content_template": "{content}\n\n{source_attribution}\n{tool_attribution}\n{hashtags}",
    "show_source": True,
    "show_tool_attribution": True,
    "tags": [],
    "include_images": True,
    "include_videos": True,
    "image_paths": [],
    "video_path": "",
    "video_duration_seconds": None,
    "video_cover_path": "",
    "state_file": "mirror_state.json",
    "dry_run": False,
}

SUPPORTED_CONFIG_KEYS = set(DEFAULT_CONFIG)

_shutdown = False


class SquareApiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"Square API error [{code}]: {message}")
        self.code = code
        self.message = message


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown
    log.info("Shutdown signal received, finishing current cycle...")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _request_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    retries: int = MAX_RETRIES,
    return_error_json: bool = False,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if url.endswith("/content/add") and exc.code == 504:
                return {
                    "code": "000000",
                    "message": None,
                    "data": {
                        "id": None,
                        "shareLink": None,
                        "publishStatus": "success_without_post_id",
                    },
                }
            if attempt == retries:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc
                if return_error_json:
                    return parsed
                message = parsed.get("message") or parsed.get("msg") or parsed.get("detail") or raw
                raise RuntimeError(f"HTTP {exc.code}: {message}") from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise
            wait = RETRY_BACKOFF_BASE * attempt
            log.warning("Request failed (attempt %d/%d): %s; retrying in %ds", attempt, retries, exc, wait)
            time.sleep(wait)
    return {}


def _square_headers(api_key: str) -> dict[str, str]:
    return {
        "X-Square-OpenAPI-Key": api_key,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }


def _square_api(
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    base_url: str = SQUARE_API_BASE_V2,
) -> dict[str, Any]:
    envelope = _request_json(
        f"{base_url}{endpoint}",
        _square_headers(api_key),
        payload,
        return_error_json=True,
    )
    code = str(envelope.get("code", ""))
    if code != "000000":
        raise SquareApiError(code or "unknown", str(envelope.get("message") or "unknown error"))
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    return _request_json(url, headers, payload)


def resolve_square_key() -> str:
    return (
        os.environ.get("BINANCE_SQUARE_OPENAPI_KEY", "").strip()
        or os.environ.get("SQUARE_API_KEY", "").strip()
    )


def fetch_user_tweets(
    token: str,
    username: str,
    max_results: int = 10,
    include_replies: bool = False,
    include_retweets: bool = False,
) -> list[dict[str, Any]]:
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
    tweets = _extract_tweets(_post_json(url, headers, payload))
    for tweet in tweets:
        tweet.setdefault("userScreenName", username)
    return tweets


def search_tweets(
    token: str,
    keywords: str = "",
    hashtag: str = "",
    min_likes: int = 0,
    min_retweets: int = 0,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    url = f"{TWITTER_API_BASE}/open/twitter_search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"maxResults": max_results, "product": "Top"}
    if keywords:
        payload["keywords"] = keywords
    if hashtag:
        payload["hashtag"] = hashtag
    if min_likes > 0:
        payload["minLikes"] = min_likes
    if min_retweets > 0:
        payload["minRetweets"] = min_retweets

    return _extract_tweets(_post_json(url, headers, payload))


def _extract_tweets(resp: Any) -> list[dict[str, Any]]:
    if not resp:
        return []
    if isinstance(resp, list):
        return [item for item in resp if isinstance(item, dict)]
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("tweets", "results", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [data]
        if "id" in resp and "text" in resp:
            return [resp]
    return []


def normalize_tags(raw_tags: Any) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        candidates = re.split(r"[,\s]+", raw_tags)
    elif isinstance(raw_tags, list):
        candidates = [str(item) for item in raw_tags]
    else:
        candidates = [str(raw_tags)]

    tags: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        tag = item.strip()
        if not tag:
            continue
        tag = tag[1:] if tag.startswith("#") else tag
        if not tag:
            continue
        normalized = f"#{tag}"
        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            tags.append(normalized)
    return tags


def _tweet_username(tweet: dict[str, Any]) -> str:
    return str(tweet.get("userScreenName") or tweet.get("screenName") or "unknown")


def transform_tweet(tweet: dict[str, Any], config: dict[str, Any]) -> str:
    text = str(tweet.get("text") or "")
    username = _tweet_username(tweet)

    text = re.sub(r"https?://t\.co/\S+", "", text).strip()
    text = re.sub(r"^RT @\w+:\s*", "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    original_tags = normalize_tags(tweet.get("hashtags", []))
    configured_tags = normalize_tags(config.get("tags", []))
    all_tags = normalize_tags(original_tags + configured_tags)
    hashtags_str = " ".join(all_tags)

    source_attribution = f"Source: @{username} on X" if config.get("show_source", True) else ""
    tool_attribution = (
        "Publish by using 6551 twitter mirror tool"
        if config.get("show_tool_attribution", True)
        else ""
    )

    template = str(config.get("content_template") or DEFAULT_CONFIG["content_template"])
    content = template.format(
        content=text,
        username=username,
        hashtags=hashtags_str,
        source_attribution=source_attribution,
        tool_attribution=tool_attribution,
    )
    content = re.sub(r"\n{3,}", "\n\n", content).strip()

    if len(content) > MAX_SQUARE_CONTENT_LENGTH:
        suffix_parts = [part for part in (source_attribution, tool_attribution, hashtags_str) if part]
        suffix = "\n\n" + "\n".join(suffix_parts) if suffix_parts else ""
        max_text_len = max(1, MAX_SQUARE_CONTENT_LENGTH - len(suffix) - 3)
        text_trimmed = text[:max_text_len] + "..."
        content = template.format(
            content=text_trimmed,
            username=username,
            hashtags=hashtags_str,
            source_attribution=source_attribution,
            tool_attribution=tool_attribution,
        )
        content = re.sub(r"\n{3,}", "\n\n", content).strip()

    return content


def append_tags(content: str, tags: list[str]) -> str:
    missing = [tag for tag in tags if tag.lower() not in content.lower().split()]
    if not missing:
        return content.strip()
    return f"{content.strip()}\n\n{' '.join(missing)}".strip()


def passes_filter(tweet: dict[str, Any], config: dict[str, Any]) -> bool:
    likes = int(tweet.get("favoriteCount") or 0)
    retweets = int(tweet.get("retweetCount") or 0)
    return likes >= int(config.get("min_likes", 0)) and retweets >= int(config.get("min_retweets", 0))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string_candidates(*values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, list):
            found.extend(_string_candidates(*value))
    return found


def extract_tweet_media(tweet: dict[str, Any]) -> dict[str, list[str]]:
    images: list[str] = []
    videos: list[str] = []

    images.extend(_string_candidates(
        tweet.get("imageUrl"),
        tweet.get("imageURL"),
        tweet.get("image"),
        tweet.get("images"),
        tweet.get("imageUrls"),
        tweet.get("mediaUrls"),
        tweet.get("photos"),
    ))
    videos.extend(_string_candidates(tweet.get("videoUrl"), tweet.get("videoURL"), tweet.get("video")))

    media_items: list[Any] = []
    media_items.extend(_as_list(tweet.get("media")))
    entities = tweet.get("entities")
    if isinstance(entities, dict):
        media_items.extend(_as_list(entities.get("media")))
    extended = tweet.get("extendedEntities") or tweet.get("extended_entities")
    if isinstance(extended, dict):
        media_items.extend(_as_list(extended.get("media")))
    attachments = tweet.get("attachments")
    if isinstance(attachments, dict):
        media_items.extend(_as_list(attachments.get("media")))

    for item in media_items:
        if isinstance(item, str):
            images.append(item)
            continue
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("type") or item.get("mediaType") or "").lower()
        if media_type in {"photo", "image"}:
            for image_url in _string_candidates(
                item.get("thumbUrl"),
                item.get("thumbnailUrl"),
                item.get("media_url_https"),
                item.get("media_url"),
                item.get("imageUrl"),
                item.get("url"),
            ):
                images.append(image_url)
                break
            continue
        if media_type in {"video", "animated_gif", "gif"}:
            variants = item.get("videoInfo", {}).get("variants") if isinstance(item.get("videoInfo"), dict) else None
            variants = variants or item.get("video_info", {}).get("variants") if isinstance(item.get("video_info"), dict) else variants
            selected = _select_video_variant(_as_list(variants))
            videos.extend(_string_candidates(
                selected,
                item.get("videoUrl"),
                item.get("video_url"),
                item.get("url"),
            ))

    return {
        "images": _dedupe_urls(images),
        "videos": _dedupe_urls(videos),
    }


def _select_video_variant(variants: list[Any]) -> str:
    best_url = ""
    best_bitrate = -1
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        content_type = str(variant.get("content_type") or variant.get("contentType") or "")
        if content_type and "mp4" not in content_type.lower():
            continue
        url = str(variant.get("url") or "")
        bitrate = int(variant.get("bitrate") or 0)
        if url and bitrate >= best_bitrate:
            best_url = url
            best_bitrate = bitrate
    return best_url


def _dedupe_urls(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        if not clean:
            continue
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def collect_media_for_tweet(tweet: dict[str, Any], config: dict[str, Any]) -> tuple[list[str], str]:
    explicit_images = [str(item).strip() for item in config.get("image_paths", []) if str(item).strip()]
    explicit_video = str(config.get("video_path") or "").strip()
    if explicit_video:
        return [], explicit_video

    tweet_media = extract_tweet_media(tweet)
    tweet_video = tweet_media["videos"][0] if config.get("include_videos", True) and tweet_media["videos"] else ""
    if tweet_video:
        return [], tweet_video

    images = explicit_images[:]
    if config.get("include_images", True):
        images.extend(tweet_media["images"])
    return images[:MAX_IMAGE_COUNT], ""


def _is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"}


def _guess_content_type(path: Path) -> str:
    return CONTENT_TYPE_MAP.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _name_from_source(source: str, fallback: str) -> str:
    if _is_url(source):
        name = Path(urlparse(source).path).name
        return name or fallback
    return Path(source).name or fallback


@contextmanager
def materialized_media(source: str, fallback_name: str = "upload.bin") -> Iterator[Path]:
    if not _is_url(source):
        path = Path(source).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Media file not found: {source}")
        yield path
        return

    req = Request(source, headers={"User-Agent": "binance-square-skill/1.0"})
    temp_path: Path | None = None
    try:
        with urlopen(req, timeout=120) as resp:
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            suffix = Path(urlparse(source).path).suffix
            if not suffix and content_type:
                suffix = mimetypes.guess_extension(content_type) or ""
            fd, raw_path = tempfile.mkstemp(prefix="square-media-", suffix=suffix or Path(fallback_name).suffix)
            temp_path = Path(raw_path)
            with os.fdopen(fd, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        yield temp_path
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def upload_to_s3(presigned_url: str, file_path: Path, content_type: str) -> None:
    data = file_path.read_bytes()
    req = Request(presigned_url, data=data, headers={"Content-Type": content_type}, method="PUT")
    with urlopen(req, timeout=180) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"S3 upload failed: {resp.status} {resp.reason}")


def poll_media_status(api_key: str, file_ticket: str) -> dict[str, Any]:
    for attempt in range(1, MAX_MEDIA_POLL_RETRIES + 1):
        data = _square_api("/image/imageStatus", api_key, {"fileTicket": file_ticket})
        status = data.get("status")
        if status == 1:
            return data
        if status == 2:
            raise RuntimeError(f"Media processing failed: {data.get('failedReason') or 'unknown reason'}")
        log.info("Media processing... (%d/%d)", attempt, MAX_MEDIA_POLL_RETRIES)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Media processing timed out after {MAX_MEDIA_POLL_RETRIES} checks")


def upload_image(api_key: str, source: str) -> str:
    with materialized_media(source, "upload.jpg") as path:
        image_name = _name_from_source(source, path.name)
        log.info("Uploading image: %s", image_name)
        data = _square_api("/image/presignedUrl", api_key, {"imageName": image_name})
        file_ticket = str(data.get("fileTicket") or "")
        presigned_url = str(data.get("presignedUrl") or "")
        if not file_ticket or not presigned_url:
            raise RuntimeError("Image upload pre-sign response is missing fileTicket or presignedUrl")
        upload_to_s3(presigned_url, path, _guess_content_type(path))
        status = poll_media_status(api_key, file_ticket)
        image_url = str(status.get("imageUrl") or "")
        if not image_url:
            raise RuntimeError("Image processing response is missing imageUrl")
        return image_url


def upload_video(api_key: str, source: str, local_path: Path) -> str:
    file_name = _name_from_source(source, local_path.name)
    size = local_path.stat().st_size
    log.info("Uploading video: %s (%.1f MB)", file_name, size / 1024 / 1024)
    data = _square_api("/video/preSign", api_key, {"fileName": file_name, "size": size})
    file_ticket = str(data.get("fileTicket") or "")
    presigned_url = str(data.get("presignedUrl") or "")
    if not file_ticket or not presigned_url:
        raise RuntimeError("Video upload pre-sign response is missing fileTicket or presignedUrl")
    upload_to_s3(presigned_url, local_path, _guess_content_type(local_path))
    poll_media_status(api_key, file_ticket)
    return file_ticket


def probe_video_duration(video_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("video_duration_seconds is required because ffprobe is not installed")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip() or 'unknown error'}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe did not return a numeric duration") from exc
    if duration <= 0:
        raise RuntimeError("video_duration_seconds must be positive")
    return duration


@contextmanager
def extracted_video_cover(video_path: Path) -> Iterator[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("A video cover is required because ffmpeg is not installed")
    temp_dir = Path(tempfile.mkdtemp(prefix="square-video-cover-"))
    cover_path = temp_dir / f"{video_path.stem}-cover.png"
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(cover_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed to extract cover: {result.stderr.strip() or 'unknown error'}")
        if not cover_path.exists() or cover_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg created an empty cover image")
        yield cover_path
    finally:
        if cover_path.exists():
            cover_path.unlink()
        if temp_dir.exists():
            temp_dir.rmdir()


def build_square_body(
    api_key: str,
    content: str,
    image_sources: list[str] | None = None,
    video_source: str = "",
    video_duration_seconds: float | None = None,
    video_cover_source: str = "",
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    text = append_tags(content, normalize_tags(tags))
    if len(text) > MAX_SQUARE_CONTENT_LENGTH:
        text = text[: MAX_SQUARE_CONTENT_LENGTH - 3].rstrip() + "..."

    images = [source for source in (image_sources or []) if source]
    video = video_source.strip()
    if images and video:
        raise ValueError("Images and video are mutually exclusive in one Square post")
    if len(images) > MAX_IMAGE_COUNT:
        raise ValueError(f"Square image posts support at most {MAX_IMAGE_COUNT} images")

    if video:
        body: dict[str, Any] = {
            "contentType": 3,
            "isPublish": True,
        }
        if text:
            body["bodyTextOnly"] = text
        if dry_run:
            body["fileTicket"] = "<dry-run-video-ticket>"
            body["cover"] = video_cover_source or "<dry-run-auto-cover>"
            body["videoTimeSeconds"] = video_duration_seconds or 0
            body["_videoSource"] = video
            return body

        with materialized_media(video, "upload.mp4") as video_path:
            duration = video_duration_seconds or probe_video_duration(video_path)
            file_ticket = upload_video(api_key, video, video_path)
            if video_cover_source:
                cover = upload_image(api_key, video_cover_source)
            else:
                with extracted_video_cover(video_path) as cover_path:
                    cover = upload_image(api_key, str(cover_path))
            body.update({
                "fileTicket": file_ticket,
                "cover": cover,
                "videoTimeSeconds": duration,
            })
        return body

    body = {
        "contentType": 1,
        "bodyTextOnly": text,
    }
    if images:
        body["imageList"] = images if dry_run else [upload_image(api_key, source) for source in images]
    return body


def publish_square_content(
    api_key: str,
    content: str,
    image_sources: list[str] | None = None,
    video_source: str = "",
    video_duration_seconds: float | None = None,
    video_cover_source: str = "",
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    body = build_square_body(
        api_key=api_key,
        content=content,
        image_sources=image_sources,
        video_source=video_source,
        video_duration_seconds=video_duration_seconds,
        video_cover_source=video_cover_source,
        tags=tags,
        dry_run=dry_run,
    )
    if dry_run:
        return {"dry_run": True, "request_body": body}

    data = _square_api("/content/add", api_key, body, SQUARE_API_BASE_V1)
    post_id = str(data.get("id") or data.get("postId") or "") if data else ""
    post_url = str(data.get("shareLink") or "") if data else ""
    if post_id and not post_url:
        post_url = f"https://www.binance.com/square/post/{post_id}"
    return {
        "dry_run": False,
        "post_id": post_id,
        "square_url": post_url,
        "data": data,
        "request_body": body,
    }


def load_state(path: str) -> dict[str, Any]:
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


def save_state(state: dict[str, Any], path: str) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_daily_counter(state: dict[str, Any]) -> None:
    today = str(date.today())
    if state.get("last_reset_date") != today:
        state["post_count_today"] = 0
        state["last_reset_date"] = today


def run_cycle(config: dict[str, Any], state: dict[str, Any], twitter_token: str, square_key: str) -> int:
    tweets: list[dict[str, Any]] = []
    max_results = int(config.get("max_posts_per_run", 5)) * 2

    try:
        mode = config["mode"]
        if mode == "account":
            for username in config["accounts"]:
                log.info("Fetching tweets from @%s ...", username)
                tweets.extend(fetch_user_tweets(
                    twitter_token,
                    str(username),
                    max_results=max_results,
                    include_replies=bool(config.get("include_replies", False)),
                    include_retweets=bool(config.get("include_retweets", False)),
                ))
        elif mode == "search":
            log.info("Searching tweets for: %s", config.get("keywords", ""))
            tweets = search_tweets(
                twitter_token,
                keywords=str(config.get("keywords", "")),
                min_likes=int(config.get("min_likes", 0)),
                min_retweets=int(config.get("min_retweets", 0)),
                max_results=max_results,
            )
        elif mode == "hashtag":
            log.info("Searching tweets for #%s", config.get("hashtag", ""))
            tweets = search_tweets(
                twitter_token,
                hashtag=str(config.get("hashtag", "")),
                min_likes=int(config.get("min_likes", 0)),
                min_retweets=int(config.get("min_retweets", 0)),
                max_results=max_results,
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
    posted_ids = set(state.get("posted_tweet_ids", []))
    posts_made = 0
    max_posts = int(config.get("max_posts_per_run", 5))

    for tweet in tweets:
        if _shutdown or posts_made >= max_posts:
            break

        tweet_id = str(tweet.get("id") or "")
        if not tweet_id:
            continue
        if tweet_id in posted_ids:
            log.debug("Skipping already-posted tweet %s", tweet_id)
            continue
        if not passes_filter(tweet, config):
            log.info("Skipping tweet %s from @%s (below engagement threshold)", tweet_id, _tweet_username(tweet))
            continue

        content = transform_tweet(tweet, config)
        images, video = collect_media_for_tweet(tweet, config)
        username = _tweet_username(tweet)

        try:
            result = publish_square_content(
                api_key=square_key,
                content=content,
                image_sources=images,
                video_source=video,
                video_duration_seconds=config.get("video_duration_seconds"),
                video_cover_source=str(config.get("video_cover_path") or ""),
                dry_run=bool(config.get("dry_run")),
            )
        except SquareApiError as exc:
            if exc.code == "220009":
                log.warning("Daily post limit exceeded; stopping posts for today.")
                break
            if exc.code in {"220003", "220004"}:
                log.error("Square API key issue (code=%s). Stopping.", exc.code)
                break
            if exc.code in {"20002", "20022"}:
                log.warning("Sensitive content detected for tweet %s, skipping future retries.", tweet_id)
                posted_ids.add(tweet_id)
                state["posted_tweet_ids"] = list(posted_ids)
                continue
            log.error("Square API error for tweet %s: %s", tweet_id, exc)
            continue
        except Exception as exc:
            log.error("Failed to publish tweet %s: %s", tweet_id, exc)
            continue

        if config.get("dry_run"):
            log.info("[DRY RUN] Would post tweet %s from @%s:\n%s", tweet_id, username, json.dumps(result["request_body"], ensure_ascii=False, indent=2))
            posts_made += 1
            continue

        posted_ids.add(tweet_id)
        state["posted_tweet_ids"] = list(posted_ids)
        state["post_count_today"] = int(state.get("post_count_today", 0)) + 1
        state["post_log"].append({
            "tweet_id": tweet_id,
            "square_post_id": result.get("post_id", ""),
            "square_url": result.get("square_url", ""),
            "username": username,
            "media": {
                "image_count": len(images),
                "has_video": bool(video),
                "tags": normalize_tags(config.get("tags", [])),
            },
            "posted_at": datetime.now(timezone.utc).isoformat(),
        })
        posts_made += 1
        log.info("SUCCESS - Square post: %s", result.get("square_url") or "ID unavailable")

        if posts_made < max_posts:
            time.sleep(2)

    state["last_poll_time"] = datetime.now(timezone.utc).isoformat()
    return posts_made


def _parse_list_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown_keys = sorted(set(config) - SUPPORTED_CONFIG_KEYS)
    for key in unknown_keys:
        log.warning("Ignoring unknown config field: %s", key)
        config.pop(key, None)

    config["tags"] = normalize_tags(config.get("tags", []))
    config["image_paths"] = [str(item).strip() for item in config.get("image_paths", []) if str(item).strip()]
    if config.get("video_duration_seconds") in ("", None):
        config["video_duration_seconds"] = None
    elif config.get("video_duration_seconds") is not None:
        config["video_duration_seconds"] = float(config["video_duration_seconds"])

    for int_key in ("poll_interval_seconds", "min_likes", "min_retweets", "max_posts_per_run"):
        config[int_key] = int(config[int_key])
    return config


def _validate_config(config: dict[str, Any]) -> None:
    if config["mode"] == "account" and not config["accounts"]:
        raise ValueError("No accounts specified. Use --accounts or set accounts in config.")
    if config["mode"] == "search" and not config["keywords"]:
        raise ValueError("No keywords specified. Use --keywords or set keywords in config.")
    if config["mode"] == "hashtag" and not config["hashtag"]:
        raise ValueError("No hashtag specified. Use --hashtag or set hashtag in config.")
    if config["poll_interval_seconds"] <= 0:
        raise ValueError("poll_interval_seconds must be greater than 0")
    if config["max_posts_per_run"] <= 0:
        raise ValueError("max_posts_per_run must be greater than 0")
    if config["min_likes"] < 0 or config["min_retweets"] < 0:
        raise ValueError("min_likes and min_retweets cannot be negative")
    if len(config["image_paths"]) > MAX_IMAGE_COUNT:
        raise ValueError(f"image_paths supports at most {MAX_IMAGE_COUNT} images")
    if config["image_paths"] and config.get("video_path"):
        raise ValueError("image_paths and video_path are mutually exclusive")
    if config.get("video_duration_seconds") is not None and float(config["video_duration_seconds"]) <= 0:
        raise ValueError("video_duration_seconds must be positive")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror Twitter/X content to Binance Square")
    parser.add_argument("--config", "-c", help="Path to JSON config file")
    parser.add_argument("--mode", choices=["account", "search", "hashtag"], help="Monitoring mode")
    parser.add_argument("--accounts", help="Comma-separated Twitter usernames")
    parser.add_argument("--keywords", help="Search keywords")
    parser.add_argument("--hashtag", help="Hashtag to monitor without #")
    parser.add_argument("--interval", type=int, help="Poll interval in seconds")
    parser.add_argument("--min-likes", type=int, help="Minimum likes threshold")
    parser.add_argument("--min-retweets", type=int, help="Minimum retweets threshold")
    parser.add_argument("--max-posts", type=int, help="Max posts per cycle")
    parser.add_argument("--include-replies", action="store_true", help="Include replies in account mode")
    parser.add_argument("--include-retweets", action="store_true", help="Include retweets in account mode")
    parser.add_argument("--tags", help="Comma-separated tags appended as #tags")
    parser.add_argument("--image", action="append", help="Image path or URL to attach; repeat or comma-separate")
    parser.add_argument("--video", help="Video path or URL to attach")
    parser.add_argument("--video-duration", type=float, help="Video duration in seconds")
    parser.add_argument("--video-cover", help="Image path or URL used as video cover")
    parser.add_argument("--no-tweet-images", action="store_true", help="Do not mirror images found on tweets")
    parser.add_argument("--no-tweet-videos", action="store_true", help="Do not mirror videos found on tweets")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting or uploading")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--state-file", help="Path to state file")
    args = parser.parse_args()

    config = dict(DEFAULT_CONFIG)
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as fh:
                file_config = json.load(fh)
            if not isinstance(file_config, dict):
                raise ValueError("config JSON root must be an object")
            config.update(file_config)
            log.info("Loaded config from %s", args.config)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.error("Failed to load config file: %s", exc)
            sys.exit(1)

    if args.mode:
        config["mode"] = args.mode
    if args.accounts:
        config["accounts"] = _parse_list_arg(args.accounts)
    if args.keywords:
        config["keywords"] = args.keywords
    if args.hashtag:
        config["hashtag"] = args.hashtag.lstrip("#")
    if args.interval is not None:
        config["poll_interval_seconds"] = args.interval
    if args.min_likes is not None:
        config["min_likes"] = args.min_likes
    if args.min_retweets is not None:
        config["min_retweets"] = args.min_retweets
    if args.max_posts is not None:
        config["max_posts_per_run"] = args.max_posts
    if args.include_replies:
        config["include_replies"] = True
    if args.include_retweets:
        config["include_retweets"] = True
    if args.tags:
        config["tags"] = _parse_list_arg(args.tags)
    if args.image:
        images: list[str] = []
        for value in args.image:
            images.extend(_parse_list_arg(value))
        config["image_paths"] = images
    if args.video:
        config["video_path"] = args.video
    if args.video_duration is not None:
        config["video_duration_seconds"] = args.video_duration
    if args.video_cover:
        config["video_cover_path"] = args.video_cover
    if args.no_tweet_images:
        config["include_images"] = False
    if args.no_tweet_videos:
        config["include_videos"] = False
    if args.dry_run:
        config["dry_run"] = True
    if args.state_file:
        config["state_file"] = args.state_file

    try:
        config = _normalize_config(config)
        _validate_config(config)
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    twitter_token = os.environ.get("TWITTER_TOKEN", "").strip()
    square_key = resolve_square_key()

    if not twitter_token:
        log.error("TWITTER_TOKEN environment variable not set. Get yours at https://6551.io/mcp")
        sys.exit(1)
    if not square_key and not config.get("dry_run"):
        log.error("BINANCE_SQUARE_OPENAPI_KEY or SQUARE_API_KEY environment variable not set.")
        sys.exit(1)

    state = load_state(config["state_file"])

    mode_desc = {
        "account": f"accounts: {', '.join(config['accounts'])}",
        "search": f"keywords: {config['keywords']}",
        "hashtag": f"#{config['hashtag']}",
    }
    log.info("=" * 60)
    log.info("Twitter to Binance Square Auto-Mirror")
    log.info("Mode: %s (%s)", config["mode"], mode_desc.get(config["mode"], ""))
    log.info("Poll interval: %ds", config["poll_interval_seconds"])
    log.info("Max posts/cycle: %d", config["max_posts_per_run"])
    log.info("Tags: %s", " ".join(config["tags"]) or "none")
    log.info("Tweet images/videos: %s/%s", config["include_images"], config["include_videos"])
    log.info("Dry run: %s", config["dry_run"])
    log.info("State file: %s", config["state_file"])
    log.info("Already posted: %d tweets", len(state.get("posted_tweet_ids", [])))
    log.info("=" * 60)

    cycle = 0
    while not _shutdown:
        cycle += 1
        reset_daily_counter(state)
        log.info("--- Cycle %d ---", cycle)

        posts = run_cycle(config, state, twitter_token, square_key)
        log.info("Cycle %d complete: %d posts made (today total: %d)", cycle, posts, state.get("post_count_today", 0))
        save_state(state, config["state_file"])

        if args.once:
            log.info("--once flag set, exiting after single cycle.")
            break
        if _shutdown:
            break

        log.info("Sleeping %ds until next cycle...", config["poll_interval_seconds"])
        for _ in range(config["poll_interval_seconds"]):
            if _shutdown:
                break
            time.sleep(1)

    save_state(state, config["state_file"])
    log.info("State saved. Goodbye.")


if __name__ == "__main__":
    main()
