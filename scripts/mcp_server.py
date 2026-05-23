#!/usr/bin/env python3
"""
MCP server for publishing new Binance Square posts.

Run with:
    python scripts/mcp_server.py
"""

from __future__ import annotations

from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on host MCP runtime
    raise SystemExit("Missing dependency: install the Python 'mcp' package to run this server.") from exc

from auto_mirror import publish_square_content, resolve_square_key


mcp = FastMCP("open-binance-square-post")


@mcp.tool()
def publish_square_post(
    text: str,
    tags: list[str] | None = None,
    images: list[str] | None = None,
    video: str = "",
    video_duration_seconds: float | None = None,
    video_cover: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Publish a new Binance Square post with optional images, one video, and #tags.

    Images and video are mutually exclusive. The API key is read from
    BINANCE_SQUARE_OPENAPI_KEY or SQUARE_API_KEY; it is never accepted as a tool
    argument.
    """
    api_key = resolve_square_key()
    if not api_key and not dry_run:
        return {
            "success": False,
            "error": "BINANCE_SQUARE_OPENAPI_KEY or SQUARE_API_KEY is not set.",
        }

    try:
        result = publish_square_content(
            api_key=api_key,
            content=text,
            image_sources=images or [],
            video_source=video,
            video_duration_seconds=video_duration_seconds,
            video_cover_source=video_cover,
            tags=tags or [],
            dry_run=dry_run,
        )
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    return {
        "success": True,
        "dry_run": dry_run,
        "post_id": result.get("post_id", ""),
        "square_url": result.get("square_url", ""),
        "request_body": result.get("request_body", {}),
    }


if __name__ == "__main__":
    mcp.run()
