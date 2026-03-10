#!/usr/bin/env python3
"""
Hotmart Video Downloader

This script uses browser automation to download all videos from a Hotmart course.
It navigates through the course, intercepts video URLs, and downloads them.
"""

import asyncio
import json
import os
import re
import time
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Any, List, Dict, Optional

try:
    from playwright.async_api import async_playwright, Page, Browser
    import aiohttp
    from aiohttp import ClientSession
except ImportError:
    print("Required packages not installed. Please run:")
    print("pip install playwright aiohttp")
    print("playwright install chromium")
    sys.exit(1)


def format_size(num_bytes: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(max(num_bytes, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def load_failed_downloads(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for content_id, details in data.items():
        if isinstance(content_id, str) and isinstance(details, dict):
            cleaned[content_id] = details
    return cleaned


def write_failed_downloads(path: Path, failed_downloads: Dict[str, Dict[str, Any]]) -> None:
    if failed_downloads:
        path.write_text(json.dumps(failed_downloads, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


class HotmartVideoDownloader:
    def __init__(self, product_url: str, output_dir: str = "videos", headless: bool = False):
        """
        Initialize the Hotmart video downloader.
        
        Args:
            product_url: URL to the Hotmart product page
            output_dir: Directory to save downloaded videos
            headless: Run browser in headless mode
        """
        self.product_url = product_url
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.headless = headless
        self.video_urls: List[Dict[str, str]] = []
        self.video_urls: List[Dict[str, str]] = []
        self.content_ids: List[str] = []
        self.content_metadata: Dict[str, Dict[str, str]] = {}
        self.current_content_id: Optional[str] = None
        # Store list of dicts: [{'url': url, 'headers': headers}]
        self.video_urls_by_id: Dict[str, List[Dict]] = {}
        self.failed_downloads_path = self.output_dir / "FAILED_DOWNLOADS.json"
        self.failed_downloads = load_failed_downloads(self.failed_downloads_path)

    def _save_failed_downloads(self) -> None:
        write_failed_downloads(self.failed_downloads_path, self.failed_downloads)

    def mark_download_failed(self, content_id: str, title: str, reason: str) -> None:
        self.failed_downloads[content_id] = {
            "title": title,
            "reason": reason,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_failed_downloads()

    def clear_failed_download(self, content_id: str) -> None:
        if content_id in self.failed_downloads:
            self.failed_downloads.pop(content_id, None)
            self._save_failed_downloads()

    def build_output_filename(self, item: Dict[str, Any], index: int) -> str:
        file_name = item.get("file_name")
        if file_name:
            return file_name
        safe_title = re.sub(r'[^\w\s-]', '', item['title']).strip()
        safe_title = re.sub(r'[-\s]+', '-', safe_title)
        filename = f"{index:03d}_{item['id']}_{safe_title}.mp4"
        return filename[:200]

    async def wait_for_ffmpeg(self, process, output_path: Path, filename: str):
        stdout_task = asyncio.create_task(process.stdout.read())
        stderr_task = asyncio.create_task(process.stderr.read())
        wait_task = asyncio.create_task(process.wait())
        started_at = time.monotonic()

        while True:
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=10)
                break
            except asyncio.TimeoutError:
                written = output_path.stat().st_size if output_path.exists() else 0
                elapsed = int(time.monotonic() - started_at)
                print(
                    f"  ... still downloading {filename} ({elapsed}s elapsed, {format_size(written)} written)",
                    flush=True,
                )

        stdout = await stdout_task
        stderr = await stderr_task
        await wait_task
        return stdout, stderr
        
    async def extract_content_ids_from_html(self, html_file: Optional[str] = None) -> List[str]:
        """
        Extract content IDs from the saved HTML file or content_ids.txt.
        
        Args:
            html_file: Path to the HTML file (optional)
            
        Returns:
            List of content IDs found in the HTML or txt file
        """
        content_ids = []
        seen_ids = set()
        
        # First check if content_ids.txt exists
        if os.path.exists('content_ids.txt'):
            print(f"Reading content IDs from: content_ids.txt")
            with open('content_ids.txt', 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if line and line not in seen_ids:  # Skip empty lines and duplicates
                        content_ids.append(line)
                        seen_ids.add(line)
            print(f"Found {len(content_ids)} content IDs in content_ids.txt")
        
        # Also check HTML file if provided
        if html_file and os.path.exists(html_file):
            print(f"Reading HTML file: {html_file}")
            with open(html_file, 'r', encoding='utf-8') as f:
                content = f.read()
                
            # Find all content URLs
            pattern = r'/content/([a-zA-Z0-9]+)'
            matches = re.findall(pattern, content)
            for match in matches:
                if match not in seen_ids:
                    content_ids.append(match)
                    seen_ids.add(match)
            
            print(f"Found {len(matches)} content IDs in HTML (merged total: {len(content_ids)})")
        
        return list(content_ids)
    
    async def intercept_video_urls(self, page: Page):
        """
        Set up network interception to capture video URLs.
        
        Args:
            page: Playwright page object
        """
        video_urls = []
        
        async def handle_response(response):
            """Intercept network responses to find video URLs."""
            url = response.url
            
            # Log all URLs to debug
            with open('network_log.txt', 'a') as f:
                f.write(f"{url}\n")

            # Check for video file extensions
            if any(url.endswith(ext) for ext in ['.mp4', '.m3u8', '.webm', '.mov', '.mkv', '.mpd', '.ts']):
                video_urls.append({
                    'url': url,
                    'content_type': response.headers.get('content-type', ''),
                })
                print(f"Found video URL: {url}")
            
            # Check for API responses that might contain video URLs
            if 'api' in url.lower() or 'media' in url.lower():
                try:
                    content_type = response.headers.get('content-type', '')
                    if 'json' in content_type:
                        try:
                            data = await response.json()
                            # Recursively search for video URLs in JSON
                            self._extract_video_urls_from_json(data, video_urls)
                        except:
                            pass
                except:
                    pass
        
        page.on("response", handle_response)
        return video_urls
    
    def _extract_video_urls_from_json(self, data, video_urls: List[Dict], path=""):
        """Recursively extract video URLs from JSON data."""
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    # Check if it's a video URL
                    if any(value.endswith(ext) for ext in ['.mp4', '.m3u8', '.webm', '.mov', '.mkv']):
                        if value not in [v['url'] for v in video_urls]:
                            video_urls.append({
                                'url': value,
                                'path': f"{path}.{key}" if path else key
                            })
                    # Check for video-related keys
                    elif key.lower() in ['videourl', 'video_url', 'mediaurl', 'media_url', 'src', 'source']:
                        if value.startswith('http') and value not in [v['url'] for v in video_urls]:
                            video_urls.append({
                                'url': value,
                                'path': f"{path}.{key}" if path else key
                            })
                else:
                    self._extract_video_urls_from_json(value, video_urls, f"{path}.{key}" if path else key)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                self._extract_video_urls_from_json(item, video_urls, f"{path}[{i}]")
    
    async def get_course_structure(self, page: Page) -> List[Dict]:
        """
        Extract course structure and all content IDs from the page.
        
        Args:
            page: Playwright page object
            
        Returns:
            List of content items with their IDs and titles
        """
        print("Extracting course structure...")
        
        # Wait for the page to load
        await page.wait_for_load_state("domcontentloaded")
        
        # Try to find the course menu/sidebar
        content_items = []
        
        # Method 1: Look for links with content IDs
        content_links = await page.query_selector_all('a[href*="/content/"]')
        for link in content_links:
            href = await link.get_attribute('href')
            if href and '/content/' in href:
                content_id = href.split('/content/')[-1].split('?')[0].split('#')[0]
                try:
                    title = await link.inner_text()
                    title = title.strip()[:100]  # Limit title length
                except:
                    title = f"Content {content_id}"
                
                if content_id and content_id not in [item['id'] for item in content_items]:
                    content_items.append({
                        'id': content_id,
                        'title': title,
                        'url': href if href.startswith('http') else f"https://hotmart.com{href}"
                    })
        
        # Method 2: Execute JavaScript to find content in the page
        try:
            js_result = await page.evaluate("""
                () => {
                    const items = [];
                    const links = document.querySelectorAll('a[href*="/content/"]');
                    links.forEach(link => {
                        const href = link.getAttribute('href');
                        if (href && href.includes('/content/')) {
                            const contentId = href.split('/content/')[1].split('?')[0].split('#')[0];
                            const title = link.innerText.trim() || link.textContent.trim() || `Content ${contentId}`;
                            items.push({
                                id: contentId,
                                title: title.substring(0, 100),
                                url: href.startsWith('http') ? href : `https://hotmart.com${href}`
                            });
                        }
                    });
                    return items;
                }
            """)
            
            # Merge results
            for item in js_result:
                if item['id'] not in [i['id'] for i in content_items]:
                    content_items.append(item)
        except Exception as e:
            print(f"Error extracting course structure via JS: {e}")
        
        print(f"Found {len(content_items)} content items")
        return content_items
    
    async def extract_video_from_page(self, page: Page, content_id: str) -> Optional[str]:
        """
        Extract video URL from a specific content page.
        
        Args:
            page: Playwright page object
            content_id: Content ID to extract video from
            
        Returns:
            Video URL if found, None otherwise
        """
        print(f"Extracting video from content: {content_id}")
        
        # Wait for video player to load
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)  # Give time for video to load
        
        # Method 1: Look for video elements
        video_elements = await page.query_selector_all('video')
        for video in video_elements:
            src = await video.get_attribute('src')
            if src:
                return src
        
        # Method 2: Look for iframe with video player
        iframes = await page.query_selector_all('iframe')
        for iframe in iframes:
            src = await iframe.get_attribute('src')
            if src and ('player' in src.lower() or 'video' in src.lower()):
                # Navigate to iframe and check for video
                try:
                    iframe_content = await iframe.content_frame()
                    if iframe_content:
                        video_in_iframe = await iframe_content.query_selector('video')
                        if video_in_iframe:
                            src = await video_in_iframe.get_attribute('src')
                            if src:
                                return src
                except:
                    pass
        
        # Method 3: Execute JavaScript to find video URLs
        try:
            video_url = await page.evaluate(r"""
                () => {
                    // Look for video element
                    const video = document.querySelector('video');
                    if (video && video.src) return video.src;
                    
                    // Look in iframes
                    const iframes = document.querySelectorAll('iframe');
                    for (let iframe of iframes) {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            const iframeVideo = iframeDoc.querySelector('video');
                            if (iframeVideo && iframeVideo.src) return iframeVideo.src;
                        } catch(e) {}
                    }
                    
                    // Look for video URLs in page source
                    const scripts = document.querySelectorAll('script');
                    for (let script of scripts) {
                        const content = script.textContent || script.innerHTML;
                        const match = content.match(/https?:\/\/[^"'\s]+\.(mp4|m3u8|webm)/i);
                        if (match) return match[0];
                    }
                    
                    return null;
                }
            """)
            
            if video_url:
                return video_url
        except Exception as e:
            print(f"Error extracting video via JS: {e}")
        
        return None
    
    async def download_video(self, session: ClientSession, url: str, filename: str) -> bool:
        """
        Download a video file.
        
        Args:
            session: aiohttp client session
            url: Video URL to download
            filename: Output filename
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print(f"Downloading: {filename}")
            
            async with session.get(url) as response:
                if response.status == 200:
                    total_size = int(response.headers.get('content-length', 0))
                    downloaded = 0
                    
                    filepath = self.output_dir / filename
                    
                    with open(filepath, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = (downloaded / total_size) * 100
                                print(f"\r  Progress: {percent:.1f}%", end='', flush=True)
                    
                    print(f"\n  ✓ Downloaded: {filename}")
                    return True
                else:
                    print(f"  ✗ Failed to download: {url} (Status: {response.status})")
                    return False
        except Exception as e:
            print(f"  ✗ Error downloading {url}: {e}")
            return False
    async def download_with_ffmpeg(self, url: str, filename: str, headers: Dict = None) -> bool:
        """
        Download video using ffmpeg with headers.
        
        Args:
            url: m3u8 URL
            filename: Output filename
            headers: Dictionary of headers to pass to ffmpeg
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print(f"Downloading with ffmpeg: {filename}")
            output_path = self.output_dir / filename
            
            # Construct ffmpeg command
            cmd = ['ffmpeg', '-y']
            
            # Add headers if provided
            if headers:
                header_str = ""
                for key, value in headers.items():
                    # ffmpeg requires headers to be CRLF separated
                    if key.lower() in ['user-agent', 'referer', 'cookie', 'origin']:
                        header_str += f"{key}: {value}\r\n"
                
                if header_str:
                    cmd.extend(['-headers', header_str])
                    
                # Explicitly set UA if available (sometimes -headers handles it, but specific flag is safer)
                if 'user-agent' in headers:
                   cmd.extend(['-user_agent', headers['user-agent']])
            
            cmd.extend([
                '-i', url,
                '-c', 'copy',
                '-bsf:a', 'aac_adtstoasc',
                str(output_path)
            ])
            
            # Run ffmpeg
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await self.wait_for_ffmpeg(process, output_path, filename)
            
            if process.returncode == 0:
                print(f"  ✓ Downloaded: {filename}")
                return True
            else:
                print(f"  ✗ Failed to download: {filename}")
                print(f"  ffmpeg stderr: {stderr.decode()[-500:]}")
                output_path.unlink(missing_ok=True)
                return False
                
        except Exception as e:
            print(f"  ✗ Error downloading with ffmpeg: {e}")
            output_path = self.output_dir / filename
            output_path.unlink(missing_ok=True)
            return False

    async def run(
        self,
        html_file: Optional[str] = None,
        cookies_file: Optional[str] = None,
        titles_only: bool = False,
        content_ids_file: Optional[str] = None,
    ):
        """
        Main execution method.
        
        Args:
            html_file: Path to saved HTML file (optional, for extracting content IDs)
            cookies_file: Path to cookies JSON file (optional, for authentication)
            content_ids_file: Path to a text file with one content ID per line
        """
        print("Starting Hotmart Video Downloader")
        print(f"Product URL: {self.product_url}")
        print(f"Output directory: {self.output_dir}")
        if self.failed_downloads:
            print(f"Retry ledger contains {len(self.failed_downloads)} previous failed download(s)")
        stats = {
            'processed': 0,
            'downloaded': 0,
            'skipped': 0,
            'failed': 0,
            'retried': 0,
        }
        
        # Extract content IDs from HTML or content_ids.txt if available
        if self.content_ids:
            print(f"Using {len(self.content_ids)} preloaded content IDs")
        elif content_ids_file and os.path.exists(content_ids_file):
            print(f"Reading content IDs from: {content_ids_file}")
            with open(content_ids_file, 'r', encoding='utf-8') as f:
                self.content_ids = [line.strip() for line in f if line.strip()]
            print(f"Found {len(self.content_ids)} content IDs in {content_ids_file}")
        elif html_file:
            self.content_ids = await self.extract_content_ids_from_html(html_file)
        elif os.path.exists('content_ids.txt'):
            self.content_ids = await self.extract_content_ids_from_html(None)
        
        async with async_playwright() as p:
            # Launch browser
            browser = await p.chromium.launch(
                headless=self.headless,
                chromium_sandbox=False,
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            
            # Load cookies if provided
            if cookies_file and os.path.exists(cookies_file):
                print(f"Loading cookies from: {cookies_file}")
                with open(cookies_file, 'r') as f:
                    cookies = json.load(f)
                    await context.add_cookies(cookies)
            
            page = await context.new_page()
            
            # Set up network interception
            intercepted_urls = []
            
            async def handle_response(response):
                url = response.url
                
                # Capture m3u8 URLs
                if '.m3u8' in url and 'master' in url and self.current_content_id:
                     print(f"Intercepted m3u8 for {self.current_content_id}: {url}")
                     
                     # Capture headers
                     headers = await response.request.all_headers()
                     
                     if self.current_content_id not in self.video_urls_by_id:
                         self.video_urls_by_id[self.current_content_id] = []
                     
                     # Check if URL already exists
                     existing_urls = [x['url'] for x in self.video_urls_by_id[self.current_content_id]]
                     if url not in existing_urls:
                         self.video_urls_by_id[self.current_content_id].append({
                             'url': url,
                             'headers': headers
                         })
            
            page.on("response", handle_response)
            
            # Navigate to product page
            print(f"\nNavigating to: {self.product_url}")
            await page.goto(self.product_url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait a bit for page to fully load
            await asyncio.sleep(3)
            
            # If not in headless mode, wait for user to log in
            if not self.headless:
                print("\n" + "="*60)
                print("⚠️  PLEASE LOG IN TO HOTMART IN THE BROWSER WINDOW")
                print("="*60)
                print("\nOnce you're logged in and can see the course content,")
                print("press ENTER to continue...")
                input()
                print("\nContinuing with download...\n")
            
            # Get course structure
            content_items = await self.get_course_structure(page)
            if content_items:
                titles_path = self.output_dir / "content_titles.json"
                with open(titles_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {item["id"]: item["title"] for item in content_items if item.get("id")},
                        f,
                        indent=2,
                        ensure_ascii=False,
                    )
                print(f"Saved content titles to: {titles_path}")
            if titles_only:
                await browser.close()
                print("Titles-only mode enabled; stopping after saving titles.")
                return stats
            
            # If we have specific content IDs, use those instead of scraping
            if self.content_ids:
                print(f"Using {len(self.content_ids)} specific content IDs from file/HTML")
                base_url = self.product_url.rstrip('/')
                content_items = [
                    {
                        'id': cid,
                        'title': self.content_metadata.get(cid, {}).get('title', f"Content {cid}"),
                        'file_name': self.content_metadata.get(cid, {}).get('video_file_name'),
                        'url': f"{base_url}/content/{cid}"
                    }
                    for cid in self.content_ids
                ]
            
            if not content_items:
                print("No content items found. Please check:")
                print("1. You're logged in to Hotmart")
                print("2. You have access to the course")
                print("3. The URL is correct")
                return stats
            
            print(f"\nFound {len(content_items)} content items to process")
            
            # Initialize video URLs list
            all_video_urls = []
            
            # Load existing video URLs to avoid re-downloading
            urls_file = self.output_dir / "video_urls.json"
            if urls_file.exists():
                with open(urls_file, 'r') as f:
                    try:
                        saved_urls = json.load(f)
                        for v in saved_urls:
                            if v not in all_video_urls:
                                all_video_urls.append(v)
                        print(f"Loaded {len(saved_urls)} existing video URLs")
                    except:
                        pass

            # Process each content item
            for i, item in enumerate(content_items, 1):
                stats['processed'] += 1
                print(f"\n[{i}/{len(content_items)}] Processing: {item['title']}")
                if item['id'] in self.failed_downloads:
                    stats['retried'] += 1
                    print(f"  Retrying previous failure: {self.failed_downloads[item['id']].get('reason', 'unknown error')}")
                target_filename = self.build_output_filename(item, i)
                target_path = self.output_dir / target_filename
                
                # Check if we already have this video downloaded
                existing_files = [target_path] if target_path.exists() else list(self.output_dir.glob(f"*_{item['id']}_*.mp4"))
                if existing_files:
                    if existing_files[0] != target_path and not target_path.exists():
                        existing_files[0].rename(target_path)
                        existing_files = [target_path]
                    print(f"  ✓ Video already downloaded: {existing_files[0].name}")
                    stats['skipped'] += 1
                    self.clear_failed_download(item['id'])
                    continue
                
                # Check if we already have the URL for this content but maybe download failed
                existing_url_entry = next((u for u in all_video_urls if u.get('content_id') == item['id']), None)
                
                if existing_url_entry:
                    print(f"  Using cached URL for {item['id']}")
                    url = existing_url_entry['url']
                    title = existing_url_entry['title']
                    content_id = existing_url_entry['content_id'] 
                    headers = existing_url_entry.get('headers')
                    downloaded_now = False
                    filename = target_filename
                    
                    download_success = False
                    if url.endswith('.m3u8') or '.m3u8' in url:
                        filename_mp4 = filename.rsplit('.', 1)[0] + '.mp4'
                        if not (self.output_dir / filename_mp4).exists():
                            download_success = await self.download_with_ffmpeg(url, filename_mp4, headers)
                            downloaded_now = download_success
                        else:
                             print(f"  ✓ File exists: {filename_mp4}")
                             stats['skipped'] += 1
                             download_success = True
                    else:
                        if not (self.output_dir / filename).exists():
                            async with aiohttp.ClientSession() as session:
                                download_success = await self.download_video(session, url, filename)
                                downloaded_now = download_success
                        else:
                             print(f"  ✓ File exists: {filename}")
                             stats['skipped'] += 1
                             download_success = True
                    
                    if download_success:
                        if downloaded_now:
                            stats['downloaded'] += 1
                        self.clear_failed_download(item['id'])
                        continue
                    else:
                        print("  ! Cached URL failed (likely expired). Re-scanning page...")
                        # Fall through to scraping logic


                try:
                    self.current_content_id = item['id']
                    item_succeeded = False
                    failure_reason = "No playable stream found"
                    
                    # Navigate to content page
                    await page.goto(item['url'], wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(5)  # Wait longer for video to trigger requests
                    
                    # Check if we intercepted any m3u8
                    found_new_video = False
                    video_url = None
                    try:
                        video_url = await page.evaluate(
                            """() => {
                                const v = document.querySelector("video");
                                if (!v) return null;
                                return v.currentSrc || v.src || (v.querySelector("source") && v.querySelector("source").src) || null;
                            }"""
                        )
                    except Exception:
                        video_url = None
                    if item['id'] in self.video_urls_by_id:
                        streams = self.video_urls_by_id[item['id']]
                        print(f"  Found {len(streams)} m3u8 stream(s)")
                        for stream in streams:
                            video_entry = {
                                'url': stream['url'],
                                'title': item['title'],
                                'content_id': item['id'],
                                'headers': stream['headers']
                            }
                            all_video_urls.append(video_entry)
                            found_new_video = True
                            
                            # Immediate download
                            url = stream['url']
                            title = item['title']
                            content_id = item['id']
                            headers = stream['headers']
                            filename = target_filename
                            
                            if await self.download_with_ffmpeg(url, filename, headers):
                                item_succeeded = True
                                stats['downloaded'] += 1
                                failure_reason = ""
                            
                    elif video_url: # Blob or other direct URL found via JS
                        print(f"  Found video via JS: {video_url}")
                        if video_url.startswith("blob:"):
                            failure_reason = "Video URL was a blob URL and could not be downloaded directly"
                        else:
                            url = video_url
                            title = item['title']
                            content_id = item['id']
                            filename = target_filename
                            if not (self.output_dir / filename).exists():
                                async with aiohttp.ClientSession() as session:
                                    if await self.download_video(session, url, filename):
                                        item_succeeded = True
                                        stats['downloaded'] += 1
                                        failure_reason = ""
                                    else:
                                        failure_reason = "Direct video download failed"
                    else:
                        print(f"  No m3u8 stream found yet")
                        
                        # Fallback: Try to play the video to trigger requests if it hasn't started
                        try:
                            play_selectors = [
                                'video',
                                'button[aria-label="Play"]',
                                '.vjs-big-play-button',
                                'button[title="Play"]',
                            ]
                            for selector in play_selectors:
                                try:
                                    await page.click(selector, timeout=2000)
                                    break
                                except Exception:
                                    continue
                            try:
                                await page.evaluate(
                                    """() => {
                                        const v = document.querySelector("video");
                                        if (v && v.paused) v.play();
                                    }"""
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(6)
                            if item['id'] in self.video_urls_by_id:
                                streams = self.video_urls_by_id[item['id']]
                                print(f"  Found {len(streams)} m3u8 stream(s) after click")
                                for stream in streams:
                                    video_entry = {
                                        'url': stream['url'],
                                        'title': item['title'],
                                        'content_id': item['id'],
                                        'headers': stream['headers']
                                    }
                                    all_video_urls.append(video_entry)
                                    found_new_video = True
                                    url = stream['url']
                                    title = item['title']
                                    content_id = item['id']
                                    headers = stream['headers']
                                    filename = target_filename
                                    if await self.download_with_ffmpeg(url, filename, headers):
                                        item_succeeded = True
                                        stats['downloaded'] += 1
                                        failure_reason = ""
                        except:
                            pass
                    
                    # Save incremental progress
                    if found_new_video:
                        with open(urls_file, 'w') as f:
                            json.dump(all_video_urls, f, indent=2)
                            print(f"  Saved progress to video_urls.json")

                    if item_succeeded:
                        self.clear_failed_download(item['id'])
                    else:
                        stats['failed'] += 1
                        self.mark_download_failed(item['id'], item['title'], failure_reason)
                
                except Exception as e:
                    print(f"  Error processing {item['title']}: {e}")
                    stats['failed'] += 1
                    self.mark_download_failed(item['id'], item['title'], str(e))
                    continue
            
            # Also collect intercepted URLs (legacy/fallback)
            for url_info in intercepted_urls:
                if url_info['url'] not in [v['url'] for v in all_video_urls]:
                    all_video_urls.append({
                        'url': url_info['url'],
                        'title': 'Intercepted Video',
                        'content_id': 'unknown'
                    })
            
            await browser.close()
            return stats
            
            print(f"\n\n✓ Processing complete! Videos saved to: {self.output_dir}")


async def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Download videos from Hotmart course')
    parser.add_argument('url', help='Hotmart product URL')
    parser.add_argument('-o', '--output', default='videos', help='Output directory (default: videos)')
    parser.add_argument('--html', help='Path to saved HTML file (optional)')
    parser.add_argument('--cookies', help='Path to cookies JSON file (optional)')
    parser.add_argument('--headless', action='store_true', help='Run browser in headless mode')
    parser.add_argument('--titles-only', action='store_true', help='Only save content titles and exit')
    parser.add_argument('--content-ids', help='Path to content_ids.txt file (one ID per line)')
    
    args = parser.parse_args()
    
    downloader = HotmartVideoDownloader(
        product_url=args.url,
        output_dir=args.output,
        headless=args.headless
    )
    
    await downloader.run(
        html_file=args.html,
        cookies_file=args.cookies,
        titles_only=args.titles_only,
        content_ids_file=args.content_ids,
    )


if __name__ == "__main__":
    asyncio.run(main())
