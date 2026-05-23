# Twitter to Binance Square

This skill mirrors Twitter/X content to Binance Square and supports:

- Binance Square image publishing.
- Binance Square video publishing.
- Explicit tag handling.
- A local MCP server for direct Square publishing.

This folder provides two entry points:

- `scripts/auto_mirror.py` mirrors Twitter/X posts to Binance Square.
- `scripts/mcp_server.py` exposes one MCP tool for direct Square publishing.
- `LICENSE` contains the project license.

Supported Square publish types:

- Text post: `contentType=1`, `bodyTextOnly`.
- Image post: `contentType=1`, `bodyTextOnly`, `imageList`, max 4 images.
- Video post: `contentType=3`, `fileTicket`, `cover`, `videoTimeSeconds`, `isPublish=true`.
- Tags: normalized `#tags` appended into `bodyTextOnly`; Square parses them server-side.

Images and video are mutually exclusive in one post.

## Credentials

Get the credentials first:

| Credential | How to obtain |
|---|---|
| `TWITTER_TOKEN` | Register at the 6551 official site https://6551.io/mcp and get the API token. |
| `BINANCE_SQUARE_OPENAPI_KEY` | Sign in to Binance, open the [Creator Center](https://www.binance.com/en/square/creator-center/home), and apply for an OpenAPI key via "View API" on the right. |

PowerShell:

```powershell
$env:TWITTER_TOKEN = "your_6551_token"
$env:BINANCE_SQUARE_OPENAPI_KEY = "your_square_openapi_key"
```

`TWITTER_TOKEN` is a 6551 Twitter API token. Use your own token from 6551 or an existing environment variable that points to the same 6551 Twitter API service.
`SQUARE_API_KEY` is accepted as a backward-compatible alias for `BINANCE_SQUARE_OPENAPI_KEY`.

The Square key is read from the environment only. Do not pass it as a CLI or MCP parameter.

## What Was Verified

Verified locally:

- Python syntax for `auto_mirror.py` and `mcp_server.py`.
- CLI parameters shown by `python scripts/auto_mirror.py --help`.
- Dry-run request-body construction for text+tags, image posts, and video posts.
- MCP import and `publish_square_post(..., dry_run=True)`.
- Live Twitter account fetch with a valid 6551 `TWITTER_TOKEN`.
- Tweet image extraction now prefers direct `pbs.twimg.com/media/...` image URLs over `x.com/.../photo/...` page URLs.

Verified live with a temporary Square OpenAPI key on 2026-05-23:

- Image + tags post succeeded with `contentType=1`, `imageList`, and tags in `bodyTextOnly`.
- Video + tags post succeeded with `contentType=3`, `fileTicket`, `cover`, `videoTimeSeconds`, `isPublish=true`, and tags in `bodyTextOnly`.
- Twitter image URL end-to-end post succeeded: a `pbs.twimg.com` source image was uploaded and published as a Square-hosted `public.bnbstatic.com` image.

Not verified in this environment:

- Full unattended mirror loop over multiple tweets and polling cycles.

The Binance request-body mapping follows Binance's current public `square-post` skill v2 scripts.

## Mirror Tweets

Run from this directory.

Preview one account cycle:

```powershell
python scripts/auto_mirror.py --mode account --accounts VitalikButerin --tags Crypto,Web3 --dry-run --once
```

Search mode:

```powershell
python scripts/auto_mirror.py --mode search --keywords "bitcoin ETF" --min-likes 100 --min-retweets 10 --once
```

Hashtag mode:

```powershell
python scripts/auto_mirror.py --mode hashtag --hashtag bitcoin --min-likes 500 --once
```

Use a config file:

```powershell
Copy-Item mirror_config.example.json mirror_config.json
python scripts/auto_mirror.py --config mirror_config.json
```

## Attach Media

By default, the mirror tries to carry over media found in the source tweet.

Ignore source tweet media:

```powershell
python scripts/auto_mirror.py --mode account --accounts Binance --no-tweet-images --no-tweet-videos --once
```

Attach explicit images to each mirrored post:

```powershell
python scripts/auto_mirror.py --mode account --accounts Binance --image .\chart1.png --image .\chart2.png --once
```

Attach one explicit video:

```powershell
python scripts/auto_mirror.py --mode account --accounts Binance --video .\clip.mp4 --video-duration 12.5 --once
```

If `--video-duration` is omitted, `ffprobe` must be installed. If `--video-cover` is omitted, `ffmpeg` must be installed to extract the first frame as the cover.

## MCP Server

Run:

```powershell
python scripts/mcp_server.py
```

Tool: `publish_square_post`

Parameters:

| Parameter | Required | Description |
|---|---:|---|
| `text` | Yes | Post text. |
| `tags` | No | List of tags. Values may include or omit `#`. |
| `images` | No | List of local image paths or image URLs. Max 4. |
| `video` | No | One local video path or video URL. |
| `video_duration_seconds` | No | Positive duration in seconds. If omitted, `ffprobe` is used. |
| `video_cover` | No | Local image path or URL for the video cover. If omitted, `ffmpeg` extracts the first frame. |
| `dry_run` | No | Return the request body without uploading or publishing. |

Do not pass both `images` and `video`.

## Config Fields

Use `mirror_config.example.json` as the source of truth for config keys.
