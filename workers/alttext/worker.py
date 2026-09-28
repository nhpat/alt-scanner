import base64
import io
import ipaddress
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import anthropic
import httpx
import psycopg
import redis
from PIL import Image

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

ALTTEXT_QUEUE = "alttext-queue"
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = {"http", "https"}
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Claude's hard cap is 8000px per edge; well above that, bigger images just cost more tokens
# for no quality benefit, so resize down to the documented efficient max edge.
MAX_IMAGE_DIMENSION = 1568
MEDIA_TYPE_TO_PIL_FORMAT = {
    "image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP",
}

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
anthropic_client = anthropic.Anthropic()
http_client = httpx.Client(headers={"User-Agent": "alt-text-scanner/1.0"})


def get_db():
    return psycopg.connect(DATABASE_URL)


def _validate_host(hostname):
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host: {hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"refusing to fetch non-public address: {ip}")


def safe_get(url):
    """GET a URL while blocking requests to private/internal addresses, including via redirects."""
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"unsupported scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValueError("missing host in URL")
        _validate_host(parsed.hostname)

        resp = http_client.get(url, follow_redirects=False, timeout=10)
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            url = urljoin(url, resp.headers["location"])
            continue
        resp.raise_for_status()
        return resp
    raise ValueError("too many redirects")


def fetch_finding(finding_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT scan_id, page_url, image_url, context_snippet FROM image_findings WHERE id = %s",
            (finding_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"finding {finding_id} not found")
    return {"scan_id": row[0], "page_url": row[1], "image_url": row[2], "context_snippet": row[3]}


def set_finding_status(finding_id, status, **fields):
    columns = ["status"]
    values = [status]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    values.append(finding_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE image_findings SET {set_clause}, updated_at = now() WHERE id = %s",
            values,
        )


def maybe_finalize_scan(scan_id):
    with get_db() as conn:
        crawl_done = conn.execute(
            "SELECT crawl_done FROM scans WHERE id = %s",
            (scan_id,),
        ).fetchone()
        if crawl_done is None or not crawl_done[0]:
            return  # crawl still discovering pages/images: too early to finalize

        pending = conn.execute(
            "SELECT COUNT(*) FROM image_findings WHERE scan_id = %s AND status = 'pending'",
            (scan_id,),
        ).fetchone()
        if pending[0] == 0:
            conn.execute(
                "UPDATE scans SET status = 'done', updated_at = now() WHERE id = %s",
                (scan_id,),
            )


def suggest_alt_with_retry(image_b64, media_type, context_snippet, page_url):
    prompt = (
        "You are writing accessibility alt text for an image on a web page, for screen reader users.\n"
        f"Page URL: {page_url}\n"
        f"Surrounding page text near the image (context only — do not repeat it back): "
        f"{context_snippet or '(none found)'}\n\n"
        "Write a short, literal description of what the image actually shows. Aim for under 80 characters; "
        "never exceed 125. If it's a logo or icon, just identify it (e.g. 'Acme Corp logo') — do not explain "
        "its purpose. If it's a diagram, chart, or photo carrying real information, describe the key visual "
        "content plainly. Do not start with 'Image of' or 'Picture of', and do not editorialize about why "
        "the image matters. Reply with only the alt text, nothing else."
    )
    delay = 1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = anthropic_client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return next(block.text for block in response.content if block.type == "text").strip()
        except anthropic.RateLimitError:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def resize_if_needed(image_bytes, media_type):
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    if max(img.size) <= MAX_IMAGE_DIMENSION:
        return image_bytes

    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    pil_format = MEDIA_TYPE_TO_PIL_FORMAT[media_type]
    if pil_format == "JPEG" and img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format=pil_format)
    return buffer.getvalue()


def process_finding(finding_id):
    finding = fetch_finding(finding_id)

    resp = safe_get(finding["image_url"])
    media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not media_type.startswith("image/"):
        raise ValueError(f"not an image: content-type {media_type}")
    if media_type not in SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"{media_type} isn't supported for AI suggestions yet (needs manual review)")
    image_bytes = resize_if_needed(resp.content, media_type)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    suggested_alt = suggest_alt_with_retry(
        image_b64, media_type, finding["context_snippet"], finding["page_url"]
    )

    set_finding_status(finding_id, "done", suggested_alt=suggested_alt)
    maybe_finalize_scan(finding["scan_id"])
    print(f"[alttext-worker] finding {finding_id} done")


def main():
    print("[alttext-worker] waiting for jobs on alttext-queue")
    while True:
        _, finding_id = redis_client.blpop(ALTTEXT_QUEUE, timeout=0)
        try:
            process_finding(finding_id)
        except Exception as exc:
            print(f"[alttext-worker] finding {finding_id} failed: {exc}")
            try:
                finding = fetch_finding(finding_id)
                set_finding_status(finding_id, "failed", error_message=str(exc))
                maybe_finalize_scan(finding["scan_id"])
            except Exception as update_exc:
                print(f"[alttext-worker] failed to record failure for {finding_id}: {update_exc}")


if __name__ == "__main__":
    main()
