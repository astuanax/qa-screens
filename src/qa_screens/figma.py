"""Download Figma frames as reference screenshots (needs FIGMA_TOKEN)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from .errors import QAUserError

logger = logging.getLogger("qa_screens.figma")
API = "https://api.figma.com/v1/"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def sync_figma(file_key: str, output_dir: Path, frame: str | None = None, token: str | None = None) -> list[str]:
    token = token or os.environ.get("FIGMA_TOKEN")
    if not token:
        raise QAUserError("FIGMA_TOKEN is not set")
    headers = {"X-Figma-Token": token}
    with httpx.Client(base_url=API, headers=headers, timeout=60) as client:
        resp = client.get(f"files/{file_key}")
        if resp.status_code in (403, 404):
            raise QAUserError(f"Figma file {file_key} not accessible (HTTP {resp.status_code})")
        resp.raise_for_status()

        frames: dict[str, str] = {}

        def walk(node):
            if node.get("type") in ("FRAME", "COMPONENT"):
                frames[node["name"]] = node["id"]
            for child in node.get("children", []):
                walk(child)

        walk(resp.json()["document"])
        if frame:
            if frame not in frames:
                raise QAUserError(f"Frame '{frame}' not found. Some available: {list(frames)[:10]}")
            frames = {frame: frames[frame]}
        if not frames:
            return []

        images = client.get(f"images/{file_key}", params={"ids": ",".join(frames.values()), "format": "png"})
        images.raise_for_status()
        id_to_name = {v: k for k, v in frames.items()}
        output_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for node_id, url in images.json().get("images", {}).items():
            if not url:
                continue
            path = output_dir / f"{_safe(id_to_name.get(node_id, node_id))}.png"
            img = httpx.get(url, timeout=120)
            img.raise_for_status()
            path.write_bytes(img.content)
            written.append(path.stem)
        logger.info("Synced %d Figma frame(s) into %s", len(written), output_dir)
        return written
