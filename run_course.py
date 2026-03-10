#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from http.cookiejar import CookieJar

NAVIGATION_URL = "https://api-club-course-consumption-gateway-ga.cb.hotmart.com/v1/navigation"
COMPLEMENTARY_URL = (
    "https://api-club-course-consumption-gateway-ga.cb.hotmart.com/v1/pages/{content_id}/complementary-content"
)
ATTACHMENT_DOWNLOAD_URL = (
    "https://api-club-hot-club-api.cb.hotmart.com/rest/v3/attachment/{file_id}/download"
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def ensure_dependencies(args: argparse.Namespace) -> None:
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg (install with: brew install ffmpeg)")
    if not shutil.which("pdftotext"):
        missing.append("pdftotext (install with: brew install poppler)")
    if args.auth_browser == "system":
        try:
            find_chrome_binary(args.chrome_bin)
        except RuntimeError as exc:
            missing.append(str(exc))
    if missing:
        details = "\n".join(f"- {item}" for item in missing)
        raise SystemExit(f"Missing required external dependencies:\n{details}")


def merge_video_stats(*stats_list: Optional[Dict[str, int]]) -> Dict[str, int]:
    merged = {"processed": 0, "downloaded": 0, "skipped": 0, "failed": 0, "retried": 0}
    for stats in stats_list:
        if not stats:
            continue
        for key in merged:
            merged[key] += int(stats.get(key, 0))
    return merged


def load_failed_video_downloads(videos_dir: Path) -> Dict[str, Any]:
    failed_path = videos_dir / "FAILED_DOWNLOADS.json"
    if not failed_path.exists():
        return {}
    try:
        payload = json.loads(failed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def log_pipeline_summary(
    manifest: Dict[str, Any],
    videos_dir: Path,
    materials_dir: Path,
    transcripts_dir: Path,
    video_stats: Dict[str, int],
) -> None:
    total_video_items = sum(1 for item in manifest["items"] if item.get("has_media"))
    downloaded_videos = sum(
        1 for item in manifest["items"] if find_video_file_for_item(item, videos_dir)
    )
    failed_video_downloads = load_failed_video_downloads(videos_dir)
    remaining_video_failures = len(failed_video_downloads)
    total_attachments = sum(len(item.get("attachments") or []) for item in manifest["items"])
    downloaded_attachments = sum(
        1
        for item in manifest["items"]
        for attachment in item.get("attachments") or []
        if (materials_dir / attachment.get("local_name", "")).exists()
    )
    total_video_transcripts = len(list(transcripts_dir.glob("*.mp4.txt")))
    total_attachment_transcripts = sum(
        1
        for item in manifest["items"]
        for attachment in item.get("attachments") or []
        if attachment.get("local_name")
        and (transcripts_dir / f"{attachment['local_name']}.txt").exists()
    )

    log("Run summary")
    log(
        "Videos: "
        f"{downloaded_videos}/{total_video_items} present, "
        f"{video_stats['downloaded']} downloaded this run, "
        f"{video_stats['skipped']} skipped existing, "
        f"{video_stats['retried']} retried, "
        f"{remaining_video_failures} remaining in FAILED_DOWNLOADS.json"
    )
    log(
        "Attachments: "
        f"{downloaded_attachments}/{total_attachments} files present"
    )
    log(
        "Transcripts: "
        f"{total_video_transcripts} video, {total_attachment_transcripts} attachment"
    )


def parse_product_id(value: str) -> str:
    value = value.strip()
    if value.isdigit():
        return value
    match = re.search(r"/products/(\d+)", value)
    if match:
        return match.group(1)
    raise SystemExit("Could not parse product id from input.")


def normalize_product_url(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")


def load_cached_product_url(output_root: Path) -> Optional[str]:
    manifest_path = output_root / "course_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    product_url = manifest.get("product_url")
    if isinstance(product_url, str) and "/products/" in product_url:
        return normalize_product_url(product_url)
    return None


def missing_product_url_error(product_id: str) -> SystemExit:
    return SystemExit(
        "Hotmart requires the full course URL for browser navigation. "
        f"Rerun with the complete URL, for example: "
        f"https://hotmart.com/pt-br/club/<club-slug>/products/{product_id}"
    )


def resolve_product_url(value: str, product_id: str, output_root: Path) -> str:
    value = value.strip()
    if "/products/" in value:
        return normalize_product_url(value)
    cached_url = load_cached_product_url(output_root)
    if cached_url:
        return cached_url
    raise missing_product_url_error(product_id)


def find_chrome_binary(explicit_path: Optional[str] = None) -> str:
    if explicit_path:
        return explicit_path
    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("Could not find Chrome/Chromium binary in PATH.")


def launch_system_browser(url: str, chrome_bin: str) -> None:
    try:
        subprocess.Popen([chrome_bin, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        raise RuntimeError(f"Failed to launch system browser: {exc}") from exc


def cookiejar_to_playwright(cj: CookieJar) -> List[Dict[str, Any]]:
    cookies = []
    for cookie in cj:
        expires = cookie.expires
        if expires is not None:
            try:
                expires = float(expires)
            except (TypeError, ValueError):
                expires = None
        entry = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "httpOnly": getattr(cookie, "httponly", False),
            "secure": bool(cookie.secure),
            "sameSite": "Lax",
        }
        if expires is not None:
            entry["expires"] = expires
        cookies.append(entry)
    return cookies


def load_cookies_from_system(domain: str) -> List[Dict[str, Any]]:
    import browser_cookie3

    cj = browser_cookie3.chrome(domain_name=domain)
    return cookiejar_to_playwright(cj)


def cookie_dict_from_file(cookies_path: Path) -> Dict[str, str]:
    if not cookies_path.exists():
        return {}
    try:
        cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {c.get("name"): c.get("value") for c in cookies if c.get("name") and c.get("value")}


def is_video_valid(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def purge_corrupt_videos(videos_dir: Path) -> List[Path]:
    corrupt = []
    for video in videos_dir.glob("*.mp4"):
        if not is_video_valid(video):
            corrupt.append(video)
    for video in corrupt:
        video.unlink(missing_ok=True)
    return corrupt


def safe_filename(name: str) -> str:
    name = name.strip().replace("/", " ").replace("\\", " ")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    base = re.sub(r"-+\.", ".", base)
    if not base or base.strip(".-") == "":
        return "file"
    return base


def safe_display_filename(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace("/", " - ").replace("\\", " - ")
    ascii_name = re.sub(r"[^A-Za-z0-9 ._-]+", "", ascii_name)
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip()
    ascii_name = re.sub(r"\s*-\s*", " - ", ascii_name)
    ascii_name = re.sub(r"( - ){2,}", " - ", ascii_name)
    return ascii_name or "Content"


def normalize_name_for_comparison(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).strip()


def lesson_title_needs_module(item: Dict[str, Any]) -> bool:
    lesson = normalize_name_for_comparison(str(item.get("lesson") or ""))
    module = normalize_name_for_comparison(str(item.get("module") or ""))
    if not lesson:
        return False
    generic_titles = {
        "introducao",
        "introduction",
        "bonus",
        "conteudo",
        "content",
        "ebook",
        "aula",
        "lesson",
    }
    if lesson in generic_titles:
        return True
    if re.fullmatch(r"(aula|lesson|content|conteudo) \d+", lesson):
        return True
    if len(lesson) <= 8:
        return True
    if module and lesson == module:
        return True
    return False


def build_content_base_name(item: Dict[str, Any]) -> str:
    lesson = safe_display_filename(str(item.get("lesson") or f"Content {item['content_id']}"))
    module = safe_display_filename(str(item.get("module") or ""))
    parts = [f"{int(item['order']):03d}"]
    if module and lesson_title_needs_module(item) and normalize_name_for_comparison(module) not in normalize_name_for_comparison(lesson):
        parts.append(module)
    parts.append(lesson)
    base = " - ".join(part for part in parts if part)
    return base[:180].rstrip(" .-")


def build_video_file_name(item: Dict[str, Any]) -> str:
    return f"{build_content_base_name(item)}.mp4"


def build_attachment_file_name(item: Dict[str, Any], attachment: Dict[str, Any], index: int, total: int) -> str:
    source_name = str(attachment.get("file_name") or f"attachment-{index}")
    source_path = Path(source_name)
    stem = safe_display_filename(source_path.stem or f"attachment-{index}")
    suffix = source_path.suffix or ".bin"
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    base = build_content_base_name(item)
    if stem and normalize_name_for_comparison(stem) not in normalize_name_for_comparison(base):
        base = f"{base} - {stem}"
    if total > 1:
        base = f"{base} - {index:02d}"
    base = base[:180].rstrip(" .-")
    return f"{base}{suffix}"


def ensure_manifest_video_names(manifest: Dict[str, Any]) -> bool:
    changed = False
    for item in manifest.get("items", []):
        expected_name = build_video_file_name(item)
        if item.get("video_file_name") != expected_name:
            item["video_file_name"] = expected_name
            changed = True
    return changed


def ensure_manifest_attachment_names(manifest: Dict[str, Any]) -> bool:
    changed = False
    for item in manifest.get("items", []):
        attachments = item.get("attachments") or []
        total = len(attachments)
        for index, attachment in enumerate(attachments, 1):
            expected_name = build_attachment_file_name(item, attachment, index, total)
            if attachment.get("local_name") != expected_name:
                if attachment.get("local_name") and not attachment.get("legacy_local_name"):
                    attachment["legacy_local_name"] = attachment["local_name"]
                attachment["local_name"] = expected_name
                changed = True
    return changed


def find_video_file_for_item(item: Dict[str, Any], videos_dir: Path) -> Optional[Path]:
    target_name = item.get("video_file_name")
    if target_name:
        target_path = videos_dir / target_name
        if target_path.exists():
            return target_path
    content_id = item["content_id"]
    legacy_matches = sorted(videos_dir.glob(f"*_{content_id}_*.mp4"))
    if legacy_matches:
        return legacy_matches[0]
    return None


def migrate_video_filenames(manifest: Dict[str, Any], videos_dir: Path) -> int:
    renamed = 0
    for item in manifest.get("items", []):
        target_name = item.get("video_file_name")
        if not target_name:
            continue
        target_path = videos_dir / target_name
        if target_path.exists():
            continue
        legacy_matches = sorted(videos_dir.glob(f"*_{item['content_id']}_*.mp4"))
        if not legacy_matches:
            continue
        legacy_matches[0].rename(target_path)
        renamed += 1
    return renamed


def migrate_attachment_filenames(
    manifest: Dict[str, Any],
    materials_dir: Path,
    transcripts_dir: Path,
) -> int:
    renamed = 0
    for item in manifest.get("items", []):
        attachments = item.get("attachments") or []
        for attachment in attachments:
            target_name = attachment.get("local_name")
            legacy_name = attachment.get("legacy_local_name")
            if not target_name or not legacy_name or target_name == legacy_name:
                continue
            legacy_path = materials_dir / legacy_name
            target_path = materials_dir / target_name
            if legacy_path.exists() and not target_path.exists():
                legacy_path.rename(target_path)
                legacy_transcript = transcripts_dir / f"{legacy_name}.txt"
                target_transcript = transcripts_dir / f"{target_name}.txt"
                if legacy_transcript.exists() and not target_transcript.exists():
                    legacy_transcript.rename(target_transcript)
                renamed += 1
    return renamed


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def compute_state(
    manifest: Dict[str, Any],
    videos_dir: Path,
    materials_dir: Path,
    transcripts_dir: Path,
) -> Dict[str, Any]:
    items_state: Dict[str, Any] = {}
    for item in manifest["items"]:
        content_id = item["content_id"]
        video_file = find_video_file_for_item(item, videos_dir)
        video_downloaded = video_file is not None
        attachments = item.get("attachments") or []
        attachment_files = [
            materials_dir / att.get("local_name", "") for att in attachments if att.get("local_name")
        ]
        attachments_downloaded = all(
            path.exists() and path.stat().st_size > 0 for path in attachment_files
        ) if attachments else True

        video_transcribed = (transcripts_dir / f"{video_file.name}.txt").exists() if video_file else True
        attachments_transcribed = all(
            (transcripts_dir / f"{att.get('local_name')}.txt").exists()
            for att in attachments
            if att.get("local_name")
        ) if attachments else True

        items_state[content_id] = {
            "video_downloaded": video_downloaded,
            "attachments_downloaded": attachments_downloaded,
            "transcribed": video_transcribed and attachments_transcribed,
        }
    return {
        "product_id": manifest.get("product_id"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "items": items_state,
    }


async def validate_token(token: str, product_id: str) -> bool:
    import aiohttp

    headers = {
        "Authorization": f"Bearer {token}",
        "x-product-id": product_id,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(NAVIGATION_URL, headers=headers) as resp:
            return resp.status == 200


async def token_from_storage(page) -> Optional[str]:
    async def extract_from_storage(storage_name: str) -> Optional[str]:
        entries = await page.evaluate(
            f"""() => Object.entries({storage_name}).map(([k,v]) => [k, v])"""
        )
        for _, value in entries:
            if not isinstance(value, str):
                continue
            candidate = extract_token_from_value(value)
            if candidate:
                return candidate
        return None

    token = await extract_from_storage("localStorage")
    if token:
        return token
    return await extract_from_storage("sessionStorage")


def extract_token_from_value(value: str) -> Optional[str]:
    if value.startswith("Bearer "):
        value = value.split(" ", 1)[1].strip()
    if value.startswith("AT-") and len(value) > 10:
        return value
    if value.count(".") >= 2 and len(value) > 40:
        return value
    try:
        data = json.loads(value)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("access_token", "accessToken", "token"):
        token = data.get(key)
        if isinstance(token, str) and len(token) > 10:
            return token
    return None


async def capture_token_from_requests(page, timeout_sec: int) -> Optional[str]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Optional[str]] = loop.create_future()

    def handler(request):
        url = request.url
        if "api-club-course-consumption-gateway" not in url:
            return
        if "navigation" not in url and "lessons" not in url:
            return
        headers = request.headers
        auth = headers.get("authorization") or headers.get("Authorization")
        if auth and auth.startswith("Bearer ") and not future.done():
            future.set_result(auth.split(" ", 1)[1])

    page.on("request", handler)
    try:
        return await asyncio.wait_for(future, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return None
    finally:
        try:
            page.off("request", handler)
        except Exception:
            pass


def sanitize_playwright_cookies(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = []
    for cookie in cookies:
        if "expires" in cookie:
            expires = cookie.get("expires")
            if expires is None:
                cookie.pop("expires", None)
            else:
                try:
                    cookie["expires"] = float(expires)
                except (TypeError, ValueError):
                    cookie.pop("expires", None)
        cookie["secure"] = bool(cookie.get("secure", False))
        cookie["httpOnly"] = bool(cookie.get("httpOnly", False))
        cleaned.append(cookie)
    return cleaned


async def get_token_with_cookies(product_url: str, cookies_path: Path, timeout_sec: int) -> Optional[str]:
    if not cookies_path.exists():
        return None
    from playwright.async_api import async_playwright

    log(f"Trying saved cookies from {cookies_path}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
        cookies = sanitize_playwright_cookies(cookies)
        await context.add_cookies(cookies)
        page = await context.new_page()
        probe_timeout = min(timeout_sec, 15)
        token_task = asyncio.create_task(capture_token_from_requests(page, probe_timeout))
        log(f"Opening course page for token capture: {product_url}")
        await page.goto(product_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        token = None
        if token_task.done():
            token = token_task.result()
        if not token:
            log("No token captured from requests; checking browser storage")
            token = await token_from_storage(page)
        if not token:
            log("Saved cookies did not yield a token")
        else:
            log("Recovered token from saved browser session")
        await browser.close()
        return token


async def login_and_capture_token(
    product_url: str,
    cookies_path: Path,
    timeout_sec: int,
) -> str:
    log("Opening browser for login. Please complete Google authentication...")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, chromium_sandbox=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        token_task = asyncio.create_task(capture_token_from_requests(page, timeout_sec))
        await page.goto(product_url, wait_until="networkidle")
        token = await token_task
        if not token:
            token = await token_from_storage(page)
        if not token:
            await browser.close()
            raise RuntimeError("Failed to capture access token after login.")
        cookies = await context.cookies()
        write_json(cookies_path, cookies)
        await browser.close()
        log(f"Saved cookies to {cookies_path}")
        return token


async def ensure_token(product_url: str, product_id: str, cookies_path: Path, timeout_sec: int) -> str:
    log("Checking whether saved authentication is still valid")
    token = await get_token_with_cookies(product_url, cookies_path, timeout_sec)
    if token and await validate_token(token, product_id):
        log("Saved authentication is valid")
        return token

    log("Saved authentication is missing or invalid; starting interactive login")
    token = await login_and_capture_token(product_url, cookies_path, timeout_sec)
    if not await validate_token(token, product_id):
        raise RuntimeError("Token validation failed after login.")
    log("Authentication succeeded")
    return token


async def ensure_token_system_browser(
    product_url: str,
    product_id: str,
    cookies_path: Path,
    timeout_sec: int,
    chrome_bin: Optional[str],
) -> str:
    if cookies_path.exists():
        log(f"Trying saved cookies from {cookies_path}")
        token = await get_token_with_cookies(product_url, cookies_path, 30)
        if token and await validate_token(token, product_id):
            log("Saved authentication is valid")
            return token

    log("Loading Hotmart cookies from the local Chrome profile")
    cookies = load_cookies_from_system("hotmart.com")
    if not cookies:
        raise RuntimeError(
            "No Hotmart cookies found in Chrome. Please log in to Hotmart in Chrome and rerun."
        )

    cleaned = sanitize_playwright_cookies(cookies)
    write_json(cookies_path, cleaned)
    token = await get_token_with_cookies(product_url, cookies_path, 30)
    if token and await validate_token(token, product_id):
        log("System browser authentication succeeded")
        return token
    raise RuntimeError(
        "Hotmart cookies loaded but token validation failed. Please open the course in Chrome and rerun."
    )


async def fetch_navigation(token: str, product_id: str) -> Dict[str, Any]:
    import aiohttp

    log("Fetching course navigation")
    headers = {
        "Authorization": f"Bearer {token}",
        "x-product-id": product_id,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(NAVIGATION_URL, headers=headers) as resp:
            if resp.status in {401, 403}:
                raise RuntimeError("Authentication failed fetching navigation. Refresh cookies and retry.")
            if resp.status != 200:
                raise RuntimeError(f"Navigation request failed: {resp.status}")
            return await resp.json()


async def fetch_complementary_content(
    session,
    token: str,
    product_id: str,
    content_id: str,
    product_url: str,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "x-product-id": product_id,
        "Origin": "https://hotmart.com",
        "Referer": f"{product_url}/content/{content_id}",
    }
    url = COMPLEMENTARY_URL.format(content_id=content_id)
    async with session.get(url, headers=headers) as resp:
        if resp.status in {401, 403}:
            raise RuntimeError(
                f"Authentication failed fetching attachments for content {content_id} ({resp.status})."
            )
        if resp.status != 200:
            return {}
        return await resp.json()


def build_manifest(navigation: Dict[str, Any], product_id: str, product_url: str) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    seen = set()

    def add_item(module_name: str, module_index: int, page: Dict[str, Any], page_index: int) -> None:
        content_id = page.get("hash") or page.get("id")
        if not content_id or content_id in seen:
            return
        seen.add(content_id)
        lesson = page.get("name") or page.get("title") or f"Content {content_id}"
        has_media = page.get("hasPlayerMedia")
        if has_media is None:
            has_media = page.get("hasMedia")
        items.append(
            {
                "content_id": content_id,
                "module": module_name,
                "lesson": lesson,
                "order": len(items) + 1,
                "module_index": module_index,
                "lesson_index": page_index,
                "has_media": bool(has_media),
                "type": page.get("type") or "CONTENT",
                "video_file_name": None,
                "attachments": None,
            }
        )

    def walk_pages(module_name: str, module_index: int, pages: List[Dict[str, Any]]) -> None:
        if not pages:
            return
        for page_index, page in enumerate(pages, 1):
            if isinstance(page, dict):
                add_item(module_name, module_index, page, page_index)
                for key in ("pages", "children", "items", "lessons"):
                    if page.get(key):
                        walk_pages(module_name, module_index, page[key])

    for module_index, module in enumerate(navigation.get("modules", []), 1):
        module_name = module.get("name") or f"Module {module_index}"
        walk_pages(module_name, module_index, module.get("pages") or module.get("lessons") or [])

    manifest = {
        "product_id": product_id,
        "product_url": product_url,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "items": items,
    }
    ensure_manifest_video_names(manifest)
    return manifest


def write_content_titles(manifest: Dict[str, Any], videos_dir: Path) -> None:
    titles = {}
    for item in manifest["items"]:
        titles[item["content_id"]] = {
            "module": item["module"],
            "lesson": item["lesson"],
            "video_file_name": item.get("video_file_name"),
        }
    write_json(videos_dir / "content_titles.json", titles)


async def enrich_manifest_with_attachments(
    manifest: Dict[str, Any],
    token: str,
    product_id: str,
    product_url: str,
    output_dir: Path,
    cookies_path: Path,
) -> Dict[str, Any]:
    changed = False
    import aiohttp

    total_items = len(manifest["items"])
    log(f"Checking attachment metadata for {total_items} content items")
    cookies = cookie_dict_from_file(cookies_path)
    async with aiohttp.ClientSession(cookies=cookies) as session:
        for index, item in enumerate(manifest["items"], 1):
            if item.get("attachments") is not None:
                continue
            content_id = item["content_id"]
            log(f"Loading attachments [{index}/{total_items}] for {content_id}")
            try:
                data = await fetch_complementary_content(
                    session,
                    token,
                    product_id,
                    content_id,
                    product_url,
                )
            except RuntimeError as exc:
                log(f"Skipping attachment metadata for {content_id}: {exc}")
                item["attachments"] = []
                changed = True
                write_json(output_dir / "course_manifest.json", manifest)
                continue
            attachments = []
            raw_attachments = data.get("attachments", [])
            total_attachments = len(raw_attachments)
            for attachment_index, attachment in enumerate(raw_attachments, 1):
                file_id = attachment.get("fileMembershipId")
                file_name = attachment.get("fileName") or f"{file_id}.bin"
                local_name = build_attachment_file_name(item, {"file_name": file_name}, attachment_index, total_attachments)
                attachments.append(
                    {
                        "file_membership_id": file_id,
                        "file_name": file_name,
                        "file_size": attachment.get("fileSize"),
                        "local_name": local_name,
                    }
                )
            item["attachments"] = attachments
            changed = True
            if changed:
                write_json(output_dir / "course_manifest.json", manifest)
    return manifest


async def download_attachments(
    manifest: Dict[str, Any],
    token: str,
    materials_dir: Path,
    cookies_path: Path,
) -> None:
    import aiohttp

    headers = {"Authorization": f"Bearer {token}"}
    materials_dir.mkdir(parents=True, exist_ok=True)
    cookies = cookie_dict_from_file(cookies_path)
    async with aiohttp.ClientSession(cookies=cookies) as session:
        for item in manifest["items"]:
            for attachment in item.get("attachments") or []:
                file_id = attachment.get("file_membership_id")
                if not file_id:
                    continue
                local_name = attachment.get("local_name") or safe_filename(attachment.get("file_name", "file"))
                dest_path = materials_dir / local_name
                if dest_path.exists() and dest_path.stat().st_size > 0:
                    attachment["local_path"] = str(dest_path)
                    continue
                url = ATTACHMENT_DOWNLOAD_URL.format(file_id=file_id)
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    if resp.status != 200:
                        print(f"  ! Failed to download attachment {file_id} ({resp.status})")
                        continue
                    dest_path.write_bytes(await resp.read())
                    attachment["local_path"] = str(dest_path)
                    print(f"  ✓ Downloaded attachment: {dest_path.name}")


def transcribe_videos(videos_dir: Path, transcripts_dir: Path, retry_failed: bool) -> None:
    from transcribe_videos import get_whisper_impl, run_openai_whisper, run_whisper_cpp

    transcripts_dir.mkdir(parents=True, exist_ok=True)
    failed_file = transcripts_dir / "FAILED_ITEMS.txt"
    failed = set()
    if failed_file.exists() and not retry_failed:
        failed = {line.strip() for line in failed_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    files = sorted(videos_dir.glob("*.mp4"))
    if not files:
        print("No videos found to transcribe.")
        return

    impl = get_whisper_impl()
    for idx, video_file in enumerate(files, 1):
        key = f"video:{video_file.name}"
        if key in failed:
            continue
        expected_txt = transcripts_dir / f"{video_file.name}.txt"
        if expected_txt.exists() and expected_txt.stat().st_size > 0:
            continue
        print(f"[{idx}/{len(files)}] Transcribing video: {video_file.name}")
        try:
            if impl == "openai":
                run_openai_whisper(video_file, transcripts_dir)
            else:
                run_whisper_cpp(video_file, transcripts_dir)
        except Exception as exc:
            print(f"  ! Failed to transcribe {video_file.name}: {exc}")
            failed.add(key)
            failed_file.write_text("\n".join(sorted(failed)) + "\n", encoding="utf-8")


def transcribe_attachments(materials_dir: Path, transcripts_dir: Path, retry_failed: bool) -> None:
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    failed_file = transcripts_dir / "FAILED_ITEMS.txt"
    failed = set()
    if failed_file.exists() and not retry_failed:
        failed = {line.strip() for line in failed_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    pdftotext = shutil.which("pdftotext")

    for attachment_path in sorted(materials_dir.glob("*")):
        if not attachment_path.is_file():
            continue
        key = f"attachment:{attachment_path.name}"
        if key in failed:
            continue
        transcript_path = transcripts_dir / f"{attachment_path.name}.txt"
        if transcript_path.exists() and transcript_path.stat().st_size > 0:
            continue
        try:
            if attachment_path.suffix.lower() == ".pdf":
                if not pdftotext:
                    raise RuntimeError("pdftotext not found in PATH")
                subprocess.run(
                    ["pdftotext", "-layout", str(attachment_path), str(transcript_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif attachment_path.suffix.lower() == ".txt":
                transcript_path.write_text(attachment_path.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                transcript_path.write_text(
                    f"Unsupported attachment type: {attachment_path.name}\n",
                    encoding="utf-8",
                )
        except Exception as exc:
            print(f"  ! Failed to transcribe attachment {attachment_path.name}: {exc}")
            failed.add(key)
            failed_file.write_text("\n".join(sorted(failed)) + "\n", encoding="utf-8")


def build_transcript(
    manifest: Dict[str, Any],
    videos_dir: Path,
    materials_dir: Path,
    transcripts_dir: Path,
    output_file: Path,
) -> None:
    with output_file.open("w", encoding="utf-8") as out_f:
        out_f.write("# Course Transcripts\n\nTable of Contents\n\n")
        for item in manifest["items"]:
            module = item.get("module", "").strip()
            lesson = item.get("lesson", "").strip()
            if module and lesson and module.lower() not in lesson.lower():
                title = f"{module} - {lesson}"
            else:
                title = lesson or module or f"Content {item['content_id']}"

            out_f.write(f"\n\n## {title}\n\n")

            video_file = find_video_file_for_item(item, videos_dir)
            wrote_any = False

            if video_file:
                text_path = transcripts_dir / f"{video_file.name}.txt"
                if text_path.exists():
                    text = text_path.read_text(encoding="utf-8", errors="ignore").strip()
                    if text:
                        out_f.write(f"_{video_file.name}_\n\n")
                        out_f.write(text)
                        out_f.write("\n")
                        wrote_any = True

            for attachment in item.get("attachments") or []:
                local_name = attachment.get("local_name")
                if not local_name:
                    continue
                text_path = transcripts_dir / f"{local_name}.txt"
                if not text_path.exists():
                    continue
                text = text_path.read_text(encoding="utf-8", errors="ignore").strip()
                if not text:
                    continue
                out_f.write("\n")
                out_f.write(f"_{local_name}_\n\n")
                out_f.write(text)
                out_f.write("\n")
                wrote_any = True

            if not wrote_any:
                out_f.write("_Transcript unavailable yet._\n")

            out_f.write("\n---\n")


async def run_pipeline(args: argparse.Namespace) -> None:
    from download_videos import HotmartVideoDownloader

    product_id = parse_product_id(args.product)
    output_root = Path(args.output_dir) / product_id
    product_url = resolve_product_url(args.product, product_id, output_root)
    videos_dir = output_root / "videos"
    materials_dir = output_root / "materials"
    transcripts_dir = output_root / "transcripts"
    output_file = output_root / "COURSE_TRANSCRIPT.md"
    cookies_path = Path(args.cookies)
    manifest_path = output_root / "course_manifest.json"
    state_path = output_root / "state.json"

    output_root.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(exist_ok=True)

    log("Checking external dependencies")
    ensure_dependencies(args)
    log(f"Starting pipeline for product {product_id}")
    log(f"Using course URL: {product_url}")

    if args.auth_browser == "system":
        token = await ensure_token_system_browser(
            product_url, product_id, cookies_path, args.auth_timeout, args.chrome_bin
        )
    else:
        token = await ensure_token(product_url, product_id, cookies_path, args.auth_timeout)

    if manifest_path.exists() and not args.refresh_manifest:
        log(f"Using existing manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        navigation = await fetch_navigation(token, product_id)
        manifest = build_manifest(navigation, product_id, product_url)
        write_json(manifest_path, manifest)
        log(f"Wrote manifest: {manifest_path}")

    if manifest.get("product_url") != product_url:
        manifest["product_url"] = product_url
    manifest_changed = ensure_manifest_video_names(manifest)
    manifest_changed = ensure_manifest_attachment_names(manifest) or manifest_changed
    if manifest_changed:
        write_json(manifest_path, manifest)

    renamed_videos = migrate_video_filenames(manifest, videos_dir)
    if renamed_videos:
        log(f"Renamed {renamed_videos} existing video file(s) to title-based names")
    renamed_attachments = migrate_attachment_filenames(manifest, materials_dir, transcripts_dir)
    if renamed_attachments:
        log(f"Renamed {renamed_attachments} existing attachment file(s) to title-based names")

    manifest = await enrich_manifest_with_attachments(
        manifest,
        token,
        product_id,
        product_url,
        output_root,
        cookies_path,
    )
    if ensure_manifest_attachment_names(manifest):
        write_json(manifest_path, manifest)
    write_json(manifest_path, manifest)

    content_ids_path = output_root / "content_ids.txt"
    content_ids_path.write_text(
        "\n".join(item["content_id"] for item in manifest["items"]) + "\n",
        encoding="utf-8",
    )

    write_content_titles(manifest, videos_dir)

    log("Starting video download stage")
    downloader = HotmartVideoDownloader(product_url, output_dir=str(videos_dir), headless=True)
    downloader.content_ids = [item["content_id"] for item in manifest["items"]]
    downloader.content_metadata = {
        item["content_id"]: {
            "title": item.get("lesson") or f"Content {item['content_id']}",
            "video_file_name": item.get("video_file_name"),
        }
        for item in manifest["items"]
    }
    video_stats = await downloader.run(cookies_file=str(cookies_path))
    write_content_titles(manifest, videos_dir)
    if not list(videos_dir.glob("*.mp4")):
        raise RuntimeError("No videos downloaded. Likely auth failure; refresh cookies and retry.")
    write_json(state_path, compute_state(manifest, videos_dir, materials_dir, transcripts_dir))

    corrupt = purge_corrupt_videos(videos_dir)
    if corrupt:
        log(f"Re-downloading {len(corrupt)} corrupt videos")
        redownload_stats = await downloader.run(cookies_file=str(cookies_path))
        video_stats = merge_video_stats(video_stats, redownload_stats)
        write_content_titles(manifest, videos_dir)
        write_json(state_path, compute_state(manifest, videos_dir, materials_dir, transcripts_dir))

    log("Downloading attachments")
    await download_attachments(manifest, token, materials_dir, cookies_path)
    write_json(manifest_path, manifest)
    write_json(state_path, compute_state(manifest, videos_dir, materials_dir, transcripts_dir))

    log("Transcribing videos")
    transcribe_videos(videos_dir, transcripts_dir, retry_failed=args.retry_failed)
    log("Transcribing attachments")
    transcribe_attachments(materials_dir, transcripts_dir, retry_failed=args.retry_failed)
    write_json(state_path, compute_state(manifest, videos_dir, materials_dir, transcripts_dir))

    log("Building combined transcript")
    build_transcript(manifest, videos_dir, materials_dir, transcripts_dir, output_file)
    log_pipeline_summary(manifest, videos_dir, materials_dir, transcripts_dir, video_stats)
    log(f"Done. Transcript saved to {output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="End-to-end Hotmart course downloader")
    parser.add_argument("product", help="Product id or full product URL")
    parser.add_argument("--output-dir", default="outputs", help="Output root directory")
    parser.add_argument("--cookies", default="cookies.json", help="Path to cookies JSON file")
    parser.add_argument("--refresh-manifest", action="store_true", help="Rebuild manifest from API")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed items")
    parser.add_argument("--auth-timeout", type=int, default=900, help="Auth wait timeout in seconds")
    parser.add_argument(
        "--auth-browser",
        choices=["playwright", "system"],
        default="playwright",
        help="Auth flow browser (system uses local Chrome)",
    )
    parser.add_argument("--chrome-bin", help="Path to Chrome/Chromium binary for system auth")
    args = parser.parse_args()

    asyncio.run(run_pipeline(args))


if __name__ == "__main__":
    main()
