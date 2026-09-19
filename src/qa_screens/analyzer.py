"""Optional second-opinion critique from Gemini (install the `ai` extra and set GEMINI_API_KEY).

When the MCP client is itself a vision model it can read the preview image
directly, so this is off by default (config `ai_critique`)."""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("qa_screens.analyzer")

PROMPT = """You are a UI QA engineer. Compare the reference image (expected design) with the
live screenshot (current implementation). Return ONLY JSON: {"issues": [{"type": "layout|spacing|typography|color|missing|extra",
"severity": "low|medium|high", "description": "...", "suggested_fix": "concrete CSS-level fix"}]}"""


def generate_critique(ref_path: str, live_path: str, model: str) -> list[dict] | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        from PIL import Image
    except ImportError:
        logger.info("google-genai not installed; install qa-screens[ai] for AI critiques")
        return None
    try:
        client = genai.Client(api_key=api_key)
        with Image.open(ref_path) as ref, Image.open(live_path) as live:
            ref.thumbnail((1600, 8000))
            live.thumbnail((1600, 8000))
            resp = client.models.generate_content(
                model=model, contents=[PROMPT, "Reference image:", ref, "Live screenshot:", live]
            )
        text = resp.text or ""
        if "```" in text:
            text = text.split("```")[1].removeprefix("json").strip()
        data = json.loads(text)
        return data.get("issues", data) if isinstance(data, dict) else data
    except Exception as e:  # third-party service: degrade, don't fail the QA run
        logger.warning("AI critique failed: %s", e)
        return None
