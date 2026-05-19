"""SFW + on-brand image verification via Ollama cloud (kimi-k2.6:cloud).

Public API:
    verify(image_path, brand_context="") -> dict
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

_OLLAMA_URL = "http://localhost:11434/api/chat"
_MODEL = "kimi-k2.6:cloud"
_TIMEOUT = 60

_PROMPT_TEMPLATE = """\
You are an image safety and brand reviewer for a professional web design agency.
Analyze the provided image and return ONLY a JSON object — no markdown, no code fences, no explanation outside the JSON.

The JSON must have exactly these four fields:
{{
  "description": "<1-2 sentence plain-English description of what is in the image>",
  "sfw_pass": <true if the image is safe for a professional workplace context, false otherwise>,
  "on_brand": <true if the image is appropriate for {brand_note}, false if it is generic or off-topic>,
  "issues": [<list any specific problems; empty array if none>]
}}

Criteria:
- sfw_pass=false: nudity, graphic violence, hate imagery, explicit drug use, anything unsuitable for a workplace or public business website.
- on_brand=false: clearly unrelated to the stated brand context, or contains competing brand marks.
- issues: name each specific problem succinctly. Empty array is fine when sfw_pass=true and on_brand=true.

Return the JSON object only. Nothing else.
"""


def _build_prompt(brand_context: str) -> str:
    if brand_context:
        brand_note = f"the following brand/site context: {brand_context}"
    else:
        brand_note = "a generic professional website (no specific brand context provided)"
    return _PROMPT_TEMPLATE.format(brand_note=brand_note)


# Cloud relay has a practical payload cap; downsample before sending.
_MAX_PIXELS = 1280 * 1280  # ~1.6 MP — safe for cloud relay
_MAX_B64_BYTES = 500_000   # hard ceiling on encoded size


def _load_image_b64(image_path: str) -> str:
    """Read image file, downsample if needed, return base64-encoded JPEG bytes.

    Large images are resized to fit within _MAX_PIXELS before encoding so the
    Ollama cloud relay can forward the payload. Pillow is a declared dep; if
    unavailable we fall back to raw bytes (works for small files).
    """
    img_path = Path(image_path)
    raw = img_path.read_bytes()

    # Fast path: small file, no resize needed.
    if len(base64.b64encode(raw)) <= _MAX_B64_BYTES:
        return base64.b64encode(raw).decode("ascii")

    # Resize via Pillow.
    try:
        from PIL import Image
        import io

        img = Image.open(img_path)
        img = img.convert("RGB")  # drop alpha, ensure JPEG-compatible
        w, h = img.size
        total = w * h
        if total > _MAX_PIXELS:
            scale = (_MAX_PIXELS / total) ** 0.5
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        data = buf.getvalue()
        return base64.b64encode(data).decode("ascii")
    except Exception:
        # Pillow unavailable or failed — fall back to raw bytes.
        # Vision check may degrade but will not crash the caller.
        return base64.b64encode(raw).decode("ascii")


def _parse_content(content: str) -> dict[str, Any]:
    """Try JSON parse, then regex fallback, then hard failure dict."""
    # Strip markdown code fences if the model wrapped its reply.
    stripped = re.sub(r"^```[a-z]*\n?", "", content.strip(), flags=re.IGNORECASE)
    stripped = re.sub(r"\n?```$", "", stripped.strip())

    # Attempt direct JSON parse.
    try:
        parsed = json.loads(stripped)
        # Normalise types: sfw_pass and on_brand must be bool.
        return {
            "description": str(parsed.get("description", "")),
            "sfw_pass": bool(parsed.get("sfw_pass", False)),
            "on_brand": bool(parsed.get("on_brand", False)),
            "issues": list(parsed.get("issues", [])),
            "raw_response": content,
        }
    except (json.JSONDecodeError, ValueError):
        pass

    # Regex fallback — pull individual fields from raw text.
    desc_m = re.search(r'"description"\s*:\s*"([^"]*)"', content, re.IGNORECASE)
    sfw_m = re.search(r'"sfw_pass"\s*:\s*(true|false)', content, re.IGNORECASE)
    brand_m = re.search(r'"on_brand"\s*:\s*(true|false)', content, re.IGNORECASE)
    issues_m = re.search(r'"issues"\s*:\s*(\[.*?\])', content, re.IGNORECASE | re.DOTALL)

    if desc_m or sfw_m or brand_m:
        issues_val: list[str] = []
        if issues_m:
            try:
                issues_val = json.loads(issues_m.group(1))
            except (json.JSONDecodeError, ValueError):
                issues_val = ["issues_parse_error"]

        return {
            "description": desc_m.group(1) if desc_m else content[:200],
            "sfw_pass": sfw_m.group(1).lower() == "true" if sfw_m else False,
            "on_brand": brand_m.group(1).lower() == "true" if brand_m else False,
            "issues": issues_val,
            "raw_response": content,
        }

    # Complete parse failure — fail safe (sfw_pass=False so the caller notices).
    return {
        "description": content[:200] if content else "no response",
        "sfw_pass": False,
        "on_brand": False,
        "issues": ["parse_error"],
        "raw_response": content,
    }


def verify(image_path: str, brand_context: str = "") -> dict[str, Any]:
    """Verify image safety and brand compliance via kimi-k2.6:cloud.

    Args:
        image_path: Absolute or relative path to an image file.
        brand_context: Optional free-text description of the site/brand
                       (e.g. "brangembringem.com — BBQ catering, earthy tones").

    Returns:
        dict with keys:
            description  (str)   — 1-2 sentence description of the image
            sfw_pass     (bool)  — True if workplace-safe
            on_brand     (bool)  — True if fits brand_context
            issues       (list)  — named problems, empty if none
            raw_response (str)   — full model reply, for debugging
    """
    image_b64 = _load_image_b64(image_path)
    prompt_text = _build_prompt(brand_context)

    payload = {
        "model": _MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt_text,
                "images": [image_b64],
            }
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except Exception as exc:
        err_str = str(exc)
        return {
            "description": f"HTTP error: {err_str}",
            "sfw_pass": False,
            "on_brand": False,
            "issues": [f"http_error: {err_str}"],
            "raw_response": err_str,
        }

    try:
        data = json.loads(raw)
        content: str = data["message"]["content"]
    except (json.JSONDecodeError, KeyError) as exc:
        raw_str = raw.decode("utf-8", errors="replace")
        return {
            "description": f"Malformed Ollama response: {exc}",
            "sfw_pass": False,
            "on_brand": False,
            "issues": ["response_parse_error"],
            "raw_response": raw_str,
        }

    return _parse_content(content)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 -m imgsuite.vision <image_path> [brand_context]", file=sys.stderr)
        sys.exit(1)

    image_arg = sys.argv[1]
    brand_arg = sys.argv[2] if len(sys.argv) > 2 else ""

    result = verify(image_arg, brand_context=brand_arg)
    print(json.dumps(result, indent=2))
