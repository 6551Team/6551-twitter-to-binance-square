---
name: twitter-to-binance-square
description: |
  Auto-mirror Twitter/X content to Binance Square, or publish a new Square post directly.
  Supports text posts, up to 4 images, one video, and #tags. Use for requests like
  "mirror tweets to Binance Square", "post this image to Square", "publish this video to Square",
  or "run the Binance Square MCP server".
user-invocable: true
allowed-tools: Read, Bash, Grep, Glob
---

# Twitter to Binance Square

Use the scripts in this skill directory. The scripts own Binance Square upload, status polling, video cover extraction, and publish behavior.

## Credentials

- Tweet mirroring requires `TWITTER_TOKEN`.
- `TWITTER_TOKEN` must be a valid 6551 Twitter API token supplied by the user's environment.
- Square publishing requires `BINANCE_SQUARE_OPENAPI_KEY`; `SQUARE_API_KEY` is accepted only for backward compatibility.
- Never pass the Square key as a command-line argument or MCP tool parameter.
- Never print the full key.

## Supported Publish Shapes

| Shape | Binance body fields |
|---|---|
| Text | `contentType=1`, `bodyTextOnly` |
| Images | `contentType=1`, `bodyTextOnly`, `imageList` |
| Video | `contentType=3`, `fileTicket`, `cover`, `videoTimeSeconds`, `isPublish=true` |
| Tags | normalized `#tags` appended into `bodyTextOnly` |

Images and video are mutually exclusive. Image posts support max 4 images. Video posts support exactly 1 video.

## Automation Script

Run from this directory:

```bash
python scripts/auto_mirror.py --mode account --accounts VitalikButerin --tags Crypto,Web3 --once --dry-run
```

Effective CLI parameters:

| Parameter | Effect |
|---|---|
| `--config`, `-c` | Load JSON config. |
| `--mode` | `account`, `search`, or `hashtag`. |
| `--accounts` | Comma-separated Twitter usernames for account mode. |
| `--keywords` | Search keywords for search mode. |
| `--hashtag` | Hashtag for hashtag mode, without `#`. |
| `--interval` | Poll interval in seconds. |
| `--min-likes` | Minimum likes threshold. |
| `--min-retweets` | Minimum retweets threshold. |
| `--max-posts` | Max posts per cycle. |
| `--include-replies` | Include replies in account mode. |
| `--include-retweets` | Include retweets in account mode. |
| `--tags` | Comma-separated tags appended as `#tags`. |
| `--image` | Image path or URL to attach; repeat or comma-separate, max 4. |
| `--video` | One video path or URL to attach. |
| `--video-duration` | Video duration in seconds; if omitted, `ffprobe` is used. |
| `--video-cover` | Image path or URL used as video cover; if omitted, `ffmpeg` extracts the first frame. |
| `--no-tweet-images` | Ignore images found on source tweets. |
| `--no-tweet-videos` | Ignore videos found on source tweets. |
| `--dry-run` | Preview request body without posting or uploading. |
| `--once` | Run one cycle and exit. |
| `--state-file` | State file for posted tweet IDs and post log. |

The JSON config uses `mirror_config.example.json` as the source of truth.

## MCP Server

Run:

```bash
python scripts/mcp_server.py
```

MCP tool: `publish_square_post`

| Parameter | Effect |
|---|---|
| `text` | Required post text. |
| `tags` | Optional list of tags. Items may include or omit `#`. |
| `images` | Optional list of local image paths or image URLs, max 4. |
| `video` | Optional local video path or video URL. |
| `video_duration_seconds` | Optional positive duration; if omitted, `ffprobe` is used. |
| `video_cover` | Optional local image path or URL for video cover; if omitted, `ffmpeg` extracts the first frame. |
| `dry_run` | Return request body without upload or publish. |

Do not pass both `images` and `video`.

## Agent Rules

1. Use `dry_run` before first live publishing when practical.
2. Only attach media explicitly provided by the user or present on the source tweet.
3. Do not rewrite user text unless the user asks.
4. Preserve existing `#tags` and `$symbols`; append configured tags only once.
5. Treat `220009` as the daily Square OpenAPI limit.

## Verification Boundary

Local dry-run verifies parameter wiring and request-body construction. Live Square publishing was verified on 2026-05-23 with a temporary OpenAPI key for image+tags and video+tags posts. Live Twitter account fetching was verified with a valid 6551 `TWITTER_TOKEN`. Twitter image URL publishing was also verified end to end: a direct `pbs.twimg.com/media/...` source image was uploaded and published as a Square-hosted `public.bnbstatic.com` image. Tweet image extraction must prefer direct `pbs.twimg.com/media/...` image URLs over `x.com/.../photo/...` page URLs.
