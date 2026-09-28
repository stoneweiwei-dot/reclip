import os
import re
import glob
import json
import base64
import binascii
import shutil
import socket
import ipaddress
import mimetypes
import subprocess
import zipfile
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, unquote, unquote_to_bytes

import requests
from bs4 import BeautifulSoup

ARCHIVE_MAX_BYTES = int(os.environ.get("RECLIP_ARCHIVE_MAX_MB", "1024")) * 1024 * 1024
ASSET_MAX_BYTES = int(os.environ.get("RECLIP_ASSET_MAX_MB", "512")) * 1024 * 1024
PAGE_MAX_BYTES = int(os.environ.get("RECLIP_PAGE_MAX_MB", "25")) * 1024 * 1024
HTTP_TIMEOUT = int(os.environ.get("RECLIP_HTTP_TIMEOUT", "45"))
UA = os.environ.get(
    "RECLIP_USER_AGENT",
    "Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36 ReClip/2.0",
)
MEDIA_EXTS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi",
    ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac",
}


def safe_name(value, fallback="file", limit=100):
    value = unquote(str(value or "")).strip()
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value[:limit].strip() or fallback)


def unique_path(folder, filename):
    base, ext = os.path.splitext(filename)
    path = os.path.join(folder, filename)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}-{n}{ext}")
        n += 1
    return path


def validate_public_url(url):
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError("Only public http/https URLs are supported")
    host = p.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Local/private addresses are not allowed")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, p.port or 443)}
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host: {host}") from exc
    for value in addresses:
        ip = ipaddress.ip_address(value.split("%")[0])
        if not ip.is_global:
            raise ValueError("Local/private addresses are not allowed")


def fetch(url, max_bytes, referer=None, session=None):
    session = session or requests.Session()
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    current = url
    for _ in range(6):
        validate_public_url(current)
        with session.get(
            current, headers=headers, timeout=(10, HTTP_TIMEOUT),
            allow_redirects=False, stream=True
        ) as r:
            if r.is_redirect or r.is_permanent_redirect:
                location = r.headers.get("Location")
                if not location:
                    raise ValueError("Redirect response had no Location")
                current = urljoin(current, location)
                continue
            r.raise_for_status()
            size = r.headers.get("Content-Length")
            if size and size.isdigit() and int(size) > max_bytes:
                raise ValueError("Resource exceeds configured size limit")
            body = bytearray()
            for chunk in r.iter_content(128 * 1024):
                if chunk:
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("Resource exceeds configured size limit")
            return (
                bytes(body),
                current,
                r.headers.get("Content-Type", "").split(";")[0].lower(),
                r.encoding or "utf-8",
            )
    raise ValueError("Too many redirects")


def decode_html(data, encoding):
    encodings = [encoding]
    if (encoding or "").lower() == "iso-8859-1":
        encodings = ["utf-8", encoding]
    encodings += ["utf-8", "gb18030", "big5"]
    for name in encodings:
        try:
            return data.decode(name)
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", errors="replace")


def absolute(base, value):
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("data:"):
        return value
    value = urljoin(base, value)
    return value if urlparse(value).scheme in {"http", "https"} else None


def best_srcset(value):
    items = [x.strip() for x in (value or "").split(",") if x.strip()]
    if not items:
        return None

    def weight(item):
        bits = item.rsplit(None, 1)
        if len(bits) < 2:
            return 0
        d = bits[1].lower()
        try:
            return int(float(d[:-1]) * (10000 if d.endswith("x") else 1))
        except (ValueError, IndexError):
            return 0

    return max(items, key=weight).split()[0]


def dedupe(values):
    seen, out = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def title_from(soup, url):
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            return node["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(" ", strip=True):
        return h1.get_text(" ", strip=True)
    return urlparse(url).netloc or "page"


def article_text(soup, url, title):
    doc = BeautifulSoup(str(soup), "html.parser")
    for node in doc(["script", "style", "noscript", "template", "form", "button", "nav", "footer", "aside"]):
        node.decompose()
    root = doc.find("article") or doc.find("main") or doc.find(attrs={"role": "main"}) or doc.body or doc

    blocks = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "p", "blockquote", "li", "pre", "figcaption"]):
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        tag = node.name.lower()
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            line = "#" * int(tag[1]) + " " + text
        elif tag == "blockquote":
            line = "> " + text
        elif tag == "li":
            line = "- " + text
        elif tag == "pre":
            line = "~~~\n" + text + "\n~~~"
        elif tag == "figcaption":
            line = "*" + text + "*"
        else:
            line = text
        if not blocks or blocks[-1] != line:
            blocks.append(line)

    if sum(map(len, blocks)) < 80:
        raw = root.get_text("\n", strip=True)
        blocks = [x.strip() for x in raw.splitlines() if x.strip()]

    md = "# " + title + "\n\nSource: " + url + "\n\n" + "\n\n".join(blocks).strip() + "\n"
    plain = "\n\n".join(
        re.sub(r"^(#{1,4}\s+|>\s+|-\s+|\*)", "", x).replace("~~~", "").strip()
        for x in blocks if x.strip()
    ).strip() + "\n"
    return md, plain


def image_urls(soup, base):
    out = []
    for img in soup.find_all("img"):
        for candidate in (
            img.get("data-original"), img.get("data-src"), img.get("data-lazy-src"),
            best_srcset(img.get("srcset")), img.get("src")
        ):
            value = absolute(base, candidate)
            if value:
                out.append(value)
                break
    for node in soup.select("picture source"):
        value = absolute(base, best_srcset(node.get("srcset")) or node.get("src"))
        if value:
            out.append(value)
    for attrs in (
        {"property": "og:image"}, {"property": "og:image:url"},
        {"name": "twitter:image"}, {"name": "twitter:image:src"}
    ):
        for node in soup.find_all("meta", attrs=attrs):
            value = absolute(base, node.get("content"))
            if value:
                out.append(value)
    pattern = re.compile(r"url\((['\"]?)(.*?)\1\)", re.I)
    for node in soup.find_all(style=True):
        for _, raw in pattern.findall(node.get("style", "")):
            value = absolute(base, raw)
            if value:
                out.append(value)
    return dedupe(out)


def media_urls(soup, base):
    out = []
    for tag in soup.find_all(["video", "audio"]):
        value = absolute(base, tag.get("src"))
        if value and not value.startswith("data:"):
            out.append(value)
        for source in tag.find_all("source"):
            value = absolute(base, source.get("src"))
            if value and not value.startswith("data:"):
                out.append(value)
    for source in soup.find_all("source"):
        if (source.get("type") or "").lower().startswith(("video/", "audio/")):
            value = absolute(base, source.get("src"))
            if value and not value.startswith("data:"):
                out.append(value)
    for attrs in (
        {"property": "og:video"}, {"property": "og:video:url"},
        {"property": "og:audio"}, {"property": "og:audio:url"},
        {"name": "twitter:player:stream"}
    ):
        for node in soup.find_all("meta", attrs=attrs):
            value = absolute(base, node.get("content"))
            if value and not value.startswith("data:"):
                out.append(value)
    for a in soup.find_all("a", href=True):
        value = absolute(base, a.get("href"))
        if value and os.path.splitext(urlparse(value).path.lower())[1] in MEDIA_EXTS:
            out.append(value)
    return dedupe(out)


def extension(url, content_type, fallback=".bin"):
    ext = mimetypes.guess_extension((content_type or "").split(";")[0]) or ""
    if ext == ".jpe":
        ext = ".jpg"
    url_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if (not ext or ext == ".bin") and 1 < len(url_ext) <= 8:
        ext = url_ext
    return ext or fallback


def save_data_image(uri, folder, index):
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", uri, re.I | re.S)
    if not match:
        raise ValueError("Invalid data URI")
    content_type = (match.group(1) or "application/octet-stream").lower()
    try:
        data = base64.b64decode(match.group(3), validate=False) if match.group(2) else unquote_to_bytes(match.group(3))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid data URI") from exc
    if len(data) > ASSET_MAX_BYTES:
        raise ValueError("Embedded image exceeds asset size limit")
    path = unique_path(folder, f"{index:03d}-embedded{extension('', content_type)}")
    with open(path, "wb") as f:
        f.write(data)
    return path, len(data), content_type, "embedded:data-uri"


def download_asset(url, folder, index, kind, referer, remaining, session):
    if remaining <= 0:
        raise ValueError("Archive size limit reached")
    data, final_url, content_type, _ = fetch(url, min(ASSET_MAX_BYTES, remaining), referer, session)
    original = safe_name(os.path.basename(urlparse(final_url).path), "")
    stem = safe_name(os.path.splitext(original)[0], kind, 70) if original else kind
    path = unique_path(folder, f"{index:03d}-{stem}{extension(final_url, content_type)}")
    with open(path, "wb") as f:
        f.write(data)
    return path, len(data), content_type, final_url


def ytdlp_media(url, folder):
    before = set(glob.glob(os.path.join(folder, "*")))
    cmd = [
        "yt-dlp", "--no-playlist", "--no-part",
        "-f", "bestvideo+bestaudio/best/bestaudio",
        "--merge-output-format", "mp4",
        "-o", os.path.join(folder, "page-video.%(ext)s"),
        url,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return []
    return sorted(set(glob.glob(os.path.join(folder, "*"))) - before)


def archive_page(url, folder, budget):
    session = requests.Session()
    images_dir = os.path.join(folder, "images")
    media_dir = os.path.join(folder, "media")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)
    result = {
        "source_url": url, "final_url": url, "title": "",
        "text_file": None, "markdown_file": None, "html_file": None,
        "images": [], "media": [], "errors": []
    }
    used = 0

    data, final_url, content_type, encoding = fetch(url, min(PAGE_MAX_BYTES, budget), session=session)
    used += len(data)
    result["final_url"] = final_url

    if "html" not in content_type and not content_type.startswith("text/"):
        target = images_dir if content_type.startswith("image/") else media_dir
        ext = extension(final_url, content_type)
        name = safe_name(os.path.basename(urlparse(final_url).path), "download" + ext)
        if not os.path.splitext(name)[1]:
            name += ext
        path = unique_path(target, name)
        with open(path, "wb") as f:
            f.write(data)
        entry = {
            "url": final_url, "file": os.path.relpath(path, folder).replace(os.sep, "/"),
            "bytes": len(data), "content_type": content_type
        }
        (result["images"] if content_type.startswith("image/") else result["media"]).append(entry)
        result["title"] = name
        return result, used

    html = decode_html(data, encoding)
    with open(os.path.join(folder, "source.html"), "w", encoding="utf-8") as f:
        f.write(html)
    result["html_file"] = "source.html"

    soup = BeautifulSoup(html, "html.parser")
    title = title_from(soup, final_url)
    result["title"] = title
    md, txt = article_text(soup, final_url, title)
    with open(os.path.join(folder, "content.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(folder, "content.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    result["markdown_file"] = "content.md"
    result["text_file"] = "content.txt"

    for index, item_url in enumerate(image_urls(soup, final_url), 1):
        try:
            if item_url.startswith("data:"):
                path, size, ctype, resolved = save_data_image(item_url, images_dir, index)
            else:
                path, size, ctype, resolved = download_asset(
                    item_url, images_dir, index, "image", final_url, budget - used, session
                )
            used += size
            result["images"].append({
                "url": resolved, "file": os.path.relpath(path, folder).replace(os.sep, "/"),
                "bytes": size, "content_type": ctype
            })
        except Exception as exc:
            result["errors"].append({"kind": "image", "url": item_url[:500], "error": str(exc)})

    for index, item_url in enumerate(media_urls(soup, final_url), 1):
        try:
            path, size, ctype, resolved = download_asset(
                item_url, media_dir, index, "media", final_url, budget - used, session
            )
            used += size
            result["media"].append({
                "url": resolved, "file": os.path.relpath(path, folder).replace(os.sep, "/"),
                "bytes": size, "content_type": ctype
            })
        except Exception as exc:
            result["errors"].append({"kind": "media", "url": item_url[:500], "error": str(exc)})

    if used < budget:
        for path in ytdlp_media(final_url, media_dir):
            size = os.path.getsize(path)
            if used + size > budget:
                try:
                    os.remove(path)
                except OSError:
                    pass
                result["errors"].append({"kind": "media", "url": final_url, "error": "Archive size limit reached"})
                continue
            used += size
            result["media"].append({
                "url": final_url, "file": os.path.relpath(path, folder).replace(os.sep, "/"),
                "bytes": size, "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "via": "yt-dlp"
            })
    return result, used


def run_archive(job_id, urls, jobs, download_dir):
    job = jobs[job_id]
    work = os.path.join(download_dir, job_id + "-archive")
    zip_path = os.path.join(download_dir, job_id + ".zip")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pages": [],
        "limits": {
            "archive_max_mb": ARCHIVE_MAX_BYTES // 1048576,
            "asset_max_mb": ASSET_MAX_BYTES // 1048576,
            "page_max_mb": PAGE_MAX_BYTES // 1048576,
        },
    }
    used = 0
    try:
        os.makedirs(work, exist_ok=True)
        for index, url in enumerate(urls, 1):
            job["progress"] = f"Extracting {index}/{len(urls)}"
            folder_name = f"{index:02d}-{safe_name(urlparse(url).netloc, 'page', 50)}"
            page_folder = os.path.join(work, folder_name)
            try:
                if used >= ARCHIVE_MAX_BYTES:
                    raise ValueError("Archive size limit reached")
                page, page_bytes = archive_page(url, page_folder, ARCHIVE_MAX_BYTES - used)
                used += page_bytes
                page["folder"] = folder_name
                manifest["pages"].append(page)
            except Exception as exc:
                manifest["pages"].append({
                    "source_url": url, "folder": folder_name,
                    "errors": [{"kind": "page", "url": url, "error": str(exc)}]
                })

        with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        job["progress"] = "Building ZIP"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for root, _, names in os.walk(work):
                for name in names:
                    path = os.path.join(root, name)
                    zf.write(path, os.path.relpath(path, work))

        pages_ok = sum(bool(p.get("title") or p.get("text_file") or p.get("images") or p.get("media")) for p in manifest["pages"])
        images = sum(len(p.get("images", [])) for p in manifest["pages"])
        media = sum(len(p.get("media", [])) for p in manifest["pages"])
        errors = sum(len(p.get("errors", [])) for p in manifest["pages"])
        first_title = next((p.get("title") for p in manifest["pages"] if p.get("title")), "")
        job.update({
            "status": "done",
            "file": zip_path,
            "filename": (safe_name(first_title, "ReClip archive", 80) + ".zip") if len(urls) == 1 else f"ReClip archive - {len(urls)} pages.zip",
            "progress": "Done",
            "summary": {"pages": pages_ok, "images": images, "media": media, "errors": errors, "bytes": used},
        })
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
