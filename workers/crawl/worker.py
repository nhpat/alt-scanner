import ipaddress
import json
import os
import re
import socket
import urllib.robotparser
import uuid
from urllib.parse import unquote, urljoin, urlparse

import httpx
import psycopg
import redis
from bs4 import BeautifulSoup

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

CRAWL_QUEUE = "crawl-queue"
ALTTEXT_QUEUE = "alttext-queue"
ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 5
MAX_PAGES = 20
MAX_IMAGES_PER_SCAN = 100
BAD_ALT_PATTERN = re.compile(r"^(img|image|dsc|photo|pic|screenshot|untitled)[\s_-]*\d*$", re.IGNORECASE)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
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


def is_bad_alt(alt, src):
    if alt is None:
        return True  # no alt attribute at all: real WCAG violation
    alt = alt.strip()
    if alt == "":
        return False  # explicitly empty alt is a deliberate "decorative image" marker
    if len(alt) < 3:
        return True
    if BAD_ALT_PATTERN.match(alt):
        return True
    if src.lower().split("?")[0].endswith(".svg"):
        return False  # SVG icon filenames routinely match their alt text on purpose (e.g. Instagram.svg / "Instagram")
    filename = os.path.splitext(os.path.basename(urlparse(src).path))[0]
    if filename and alt.lower() == filename.lower():
        return True
    return False


def extract_context(img_tag):
    for parent in img_tag.parents:
        text = parent.get_text(" ", strip=True)
        if text and len(text) > 10:
            return text[:300]
    heading = img_tag.find_previous(["h1", "h2", "h3"])
    if heading:
        return heading.get_text(" ", strip=True)[:300]
    return None


def is_tracking_pixel(img_tag):
    """1x1 (or 0x0) analytics beacons embedded as <img> tags — not real visual content,
    so they don't need alt text and would just fail the vision API anyway."""
    if img_tag.get("width") in ("0", "1") and img_tag.get("height") in ("0", "1"):
        return True
    style = (img_tag.get("style") or "").lower().replace(" ", "")
    return "display:none" in style


def find_image_candidates(page_url, html, budget, seen_image_urls):
    """seen_image_urls is shared across the whole crawl (not just this page) so the same
    image — e.g. a footer social icon repeated on every page — is only flagged once per scan."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for img in soup.find_all("img"):
        if len(candidates) >= budget:
            break
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue
        if is_tracking_pixel(img):
            continue
        alt = img.get("alt")
        if not is_bad_alt(alt, src):
            continue
        image_url = urljoin(page_url, src)
        if image_url in seen_image_urls:
            continue
        seen_image_urls.add(image_url)
        candidates.append({
            "image_url": image_url,
            "current_alt": alt,
            "context_snippet": extract_context(img),
        })
    return candidates


WIKI_NAMESPACES = {
    "wikipedia", "wp", "talk", "user", "user_talk", "file", "file_talk",
    "mediawiki", "mediawiki_talk", "template", "template_talk", "help", "help_talk",
    "category", "category_talk", "portal", "portal_talk", "draft", "draft_talk",
    "module", "module_talk", "special", "media", "timedtext", "timedtext_talk", "gadget",
}


def is_wiki_namespace_link(path):
    """Skip MediaWiki namespace pages (Wikipedia:, Talk:, File:, Portal:, User:, Category:,
    Help:, Template:, ...) — meta/administrative pages, not page content to audit. Uses an
    explicit namespace allowlist rather than "any word before a colon" so legitimate article
    titles containing a colon (e.g. "Kill Bill: Volume 1") aren't mistakenly excluded."""
    segment = unquote(path.rsplit("/", 1)[-1])
    if ":" not in segment:
        return False
    prefix = segment.split(":", 1)[0].strip().lower().replace(" ", "_")
    return prefix in WIKI_NAMESPACES


def find_same_origin_links(page_url, html, origin):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        parsed = urlparse(href)
        if parsed.scheme not in ALLOWED_SCHEMES or parsed.netloc != origin:
            continue
        if is_wiki_namespace_link(parsed.path):
            continue
        links.append(parsed._replace(fragment="").geturl())
    return links


def set_scan_fields(scan_id, **fields):
    columns = list(fields.keys())
    values = list(fields.values())
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    values.append(scan_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE scans SET {set_clause}, updated_at = now() WHERE id = %s",
            values,
        )


def insert_finding(scan_id, page_url, candidate):
    finding_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO image_findings (id, scan_id, page_url, image_url, current_alt, context_snippet, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            """,
            (finding_id, scan_id, page_url, candidate["image_url"], candidate["current_alt"], candidate["context_snippet"]),
        )
    redis_client.lpush(ALTTEXT_QUEUE, finding_id)


def finalize_if_done(scan_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM image_findings WHERE scan_id = %s AND status = 'pending'",
            (scan_id,),
        ).fetchone()
        if row[0] == 0:
            conn.execute(
                "UPDATE scans SET status = 'done', updated_at = now() WHERE id = %s",
                (scan_id,),
            )


def crawl(scan_id, root_url):
    parsed_root = urlparse(root_url)
    origin = parsed_root.netloc

    robots = urllib.robotparser.RobotFileParser()
    try:
        resp = safe_get(f"{parsed_root.scheme}://{origin}/robots.txt")
        robots.parse(resp.text.splitlines())
    except Exception:
        robots = None  # no robots.txt or unreachable: treat as allowed

    visited = set()
    seen_image_urls = set()
    queue = [root_url]
    pages_scanned = 0
    images_found = 0

    while queue and pages_scanned < MAX_PAGES and images_found < MAX_IMAGES_PER_SCAN:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        if robots is not None and not robots.can_fetch("*", url):
            continue

        try:
            resp = safe_get(url)
        except Exception as exc:
            print(f"[crawl-worker] skipping {url}: {exc}")
            continue

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type:
            continue

        pages_scanned += 1
        candidates = find_image_candidates(url, resp.text, MAX_IMAGES_PER_SCAN - images_found, seen_image_urls)
        for candidate in candidates:
            insert_finding(scan_id, url, candidate)
            images_found += 1

        set_scan_fields(scan_id, pages_scanned=pages_scanned, images_found=images_found)

        if images_found >= MAX_IMAGES_PER_SCAN:
            break

        for link in find_same_origin_links(url, resp.text, origin):
            if link not in visited:
                queue.append(link)

    if pages_scanned == 0:
        raise RuntimeError(
            f"could not fetch any pages starting from {root_url} — it may block automated "
            "requests, disallow crawling via robots.txt, or be unreachable"
        )

    set_scan_fields(scan_id, crawl_done=True)
    finalize_if_done(scan_id)
    print(f"[crawl-worker] scan {scan_id} crawl complete: {pages_scanned} pages, {images_found} images")


def process_scan(scan_id, root_url):
    try:
        crawl(scan_id, root_url)
    except Exception as exc:
        print(f"[crawl-worker] scan {scan_id} failed: {exc}")
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE scans SET status = 'failed', error_message = %s, crawl_done = true, updated_at = now() WHERE id = %s",
                    (str(exc), scan_id),
                )
        except Exception as update_exc:
            print(f"[crawl-worker] failed to record failure for {scan_id}: {update_exc}")


def main():
    print("[crawl-worker] waiting for jobs on crawl-queue")
    while True:
        _, payload = redis_client.blpop(CRAWL_QUEUE, timeout=0)
        data = json.loads(payload)
        process_scan(data["scan_id"], data["root_url"])


if __name__ == "__main__":
    main()
