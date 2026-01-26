import os
import subprocess
import glob
import re
import json
import argparse
from html.parser import HTMLParser
from html import unescape
from pathlib import Path

def clean_filename(filename):
    """Extract a readable title from the filename."""
    # Remove the 001_id_ part and extension
    name = Path(filename).stem
    # Try to find the title part after the ID
    parts = name.split('_', 2)
    if len(parts) >= 3:
        return parts[2].replace('-', ' ')
    return name

def normalize_title(raw_title):
    if not raw_title:
        return ""
    lines = []
    for line in raw_title.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if "tocando agora" in lower or "disponível até" in lower or "está sendo reproduzida" in lower:
            continue
        if re.fullmatch(r"\d+%+", line):
            continue
        if re.fullmatch(r"\d+\s+aulas?", line, re.IGNORECASE):
            continue
        if line.lower() in {"modulo", "módulo", "extra", "acessar", "completo"}:
            continue
        lines.append(line)
    if not lines:
        return ""
    return max(lines, key=len)

def load_titles_from_html(html_path):
    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_a = False
            self.current_href = ""
            self.current_text = []
            self.items = {}

        def handle_starttag(self, tag, attrs):
            if tag != "a":
                return
            href = ""
            for key, value in attrs:
                if key == "href":
                    href = value or ""
                    break
            if "/content/" in href:
                self.in_a = True
                self.current_href = href
                self.current_text = []

        def handle_data(self, data):
            if self.in_a:
                self.current_text.append(data)

        def handle_endtag(self, tag):
            if tag != "a" or not self.in_a:
                return
            text = " ".join("".join(self.current_text).split())
            href = self.current_href
            self.in_a = False
            self.current_href = ""
            self.current_text = []
            if not text:
                return
            content_id = href.split("/content/")[-1].split("?")[0].split("#")[0]
            if content_id and content_id not in self.items:
                self.items[content_id] = unescape(text)

    parser = LinkParser()
    parser.feed(Path(html_path).read_text(encoding="utf-8", errors="ignore"))
    return parser.items

def load_title_map(videos_dir):
    title_map = {}
    titles_path = Path(videos_dir) / "content_titles.json"
    if titles_path.exists():
        try:
            titles_data = json.loads(titles_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            titles_data = {}
        for content_id, raw_title in titles_data.items():
            if isinstance(raw_title, dict):
                lesson = normalize_title(raw_title.get("lesson", "")) or raw_title.get("lesson", "").strip()
                module = normalize_title(raw_title.get("module", "")) or raw_title.get("module", "").strip()
                if module and lesson and module.lower() not in lesson.lower():
                    title = f"{module} - {lesson}".strip()
                else:
                    title = lesson or module
            else:
                title = normalize_title(raw_title) or raw_title.strip()
            if content_id and title and not title.lower().startswith("content "):
                title_map[content_id] = title

    urls_path = Path(videos_dir) / "video_urls.json"
    if not urls_path.exists():
        data = []
    else:
        try:
            data = json.loads(urls_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = []

    for entry in data:
        content_id = entry.get("content_id")
        title = normalize_title(entry.get("title", ""))
        if content_id and title and not title.lower().startswith("content "):
            title_map[content_id] = title

    if title_map:
        return title_map

    for html_path in ["Jannuzzi _ Hotmart Club.html"]:
        html_file = Path(html_path)
        if not html_file.exists():
            continue
        for content_id, raw_title in load_titles_from_html(html_file).items():
            title = normalize_title(raw_title) or raw_title.strip()
            if not title or title.lower().startswith("content "):
                continue
            existing = title_map.get(content_id, "")
            if len(title) > len(existing):
                title_map[content_id] = title

    return title_map

def get_content_id_from_filename(filename):
    match = re.match(r"^\d+_([A-Za-z0-9]+)_", filename)
    return match.group(1) if match else None

def resolve_title(video_file, title_map):
    content_id = get_content_id_from_filename(video_file.name)
    if content_id and content_id in title_map:
        return title_map[content_id]
    return clean_filename(video_file.name)

def rebuild_transcript(files, output_file, transcripts_dir, title_map):
    with open(output_file, "w", encoding="utf-8") as out_f:
        out_f.write("# Course Transcripts\n\nTable of Contents\n\n")
        for video_file in files:
            expected_txt = transcripts_dir / f"{video_file.name}.txt"
            if not expected_txt.exists():
                continue
            text = expected_txt.read_text(encoding="utf-8").strip()
            if not text:
                continue
            title = resolve_title(video_file, title_map)
            out_f.write(f"\n\n## {title}\n\n")
            out_f.write(f"_{video_file.name}_\n\n")
            out_f.write(text)
            out_f.write("\n\n---\n")

def get_whisper_impl():
    impl = os.environ.get("WHISPER_IMPL", "openai").strip().lower()
    if impl not in {"openai", "whispercpp"}:
        raise ValueError("WHISPER_IMPL must be 'openai' or 'whispercpp'")
    return impl

def run_openai_whisper(video_file, transcripts_dir):
    model = os.environ.get("WHISPER_MODEL", "base")
    cmd = [
        "whisper",
        str(video_file),
        "--model", model,
        "--output_dir", str(transcripts_dir),
        "--task", "transcribe",
        "--language", "Portuguese",
        "--beam_size", "1",
        "--best_of", "1",
        "--verbose", "False",
        "--device", "cpu",
        "--fp16", "False",
    ]
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""
    subprocess.run(cmd, check=True, env=env)

def run_whisper_cpp(video_file, transcripts_dir):
    whisper_cpp_bin = os.environ.get("WHISPER_CPP_BIN")
    whisper_cpp_model = os.environ.get("WHISPER_CPP_MODEL")
    if not whisper_cpp_bin or not whisper_cpp_model:
        raise ValueError("WHISPER_CPP_BIN and WHISPER_CPP_MODEL must be set for whispercpp")

    wav_dir = transcripts_dir / "whispercpp_wav"
    wav_dir.mkdir(exist_ok=True)
    wav_path = wav_dir / f"{video_file.stem}.wav"

    ffmpeg = "ffmpeg"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_file),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    output_prefix = transcripts_dir / video_file.name
    cmd = [
        whisper_cpp_bin,
        "-m", whisper_cpp_model,
        "-f", str(wav_path),
        "-l", "pt",
        "-otxt",
        "-of", str(output_prefix),
        "-ng",
    ]
    subprocess.run(cmd, check=True)
    if wav_path.exists():
        wav_path.unlink()

def main():
    parser = argparse.ArgumentParser(description="Transcribe videos with Whisper")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild transcript from existing files")
    parser.add_argument("--videos-dir", default="videos", help="Directory containing videos")
    parser.add_argument("--transcripts-dir", default="transcripts", help="Directory containing transcripts")
    parser.add_argument("--output-file", default="COURSE_TRANSCRIPT.md", help="Output transcript file")
    args = parser.parse_args()

    videos_dir = Path(args.videos_dir)
    output_file = Path(args.output_file)
    transcripts_dir = Path(args.transcripts_dir)
    failed_file = transcripts_dir / "FAILED_TRANSCRIPTIONS.txt"
    
    transcripts_dir.mkdir(exist_ok=True)
    title_map = load_title_map(videos_dir)
    
    # Get all mp4 files sorted
    files = sorted(list(videos_dir.glob("*.mp4")))
    
    print(f"Found {len(files)} videos to transcribe.")

    if args.rebuild:
        rebuild_transcript(files, output_file, transcripts_dir, title_map)
        print(f"\nRebuilt transcript saved to: {output_file}")
        return
    
    # Check what's already done in the consolidated transcript
    processed_files = set()
    if output_file.exists():
        with open(output_file, 'r', encoding='utf-8') as f:
            content = f.read()
            for file in files:
                if f"_{file.name}_" in content:
                    processed_files.add(file.name)
    
    failed_videos = set()
    if failed_file.exists():
        failed_videos = {line.strip() for line in failed_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    print(f"Skipping {len(processed_files)} already transcribed videos.")
    if failed_videos:
        print(f"Skipping {len(failed_videos)} previously failed videos.")
    
    with open(output_file, 'a', encoding='utf-8') as out_f:
        # Write header if new file
        if output_file.stat().st_size == 0:
            out_f.write("# Course Transcripts\n\nTable of Contents\n\n")
        
        for i, video_file in enumerate(files, 1):
            if video_file.name in processed_files:
                continue
            if video_file.name in failed_videos:
                continue

            # Whisper creates [filename].mp4.txt in output dir
            expected_txt = transcripts_dir / f"{video_file.name}.txt"

            if expected_txt.exists():
                text = expected_txt.read_text(encoding='utf-8').strip()
                title = resolve_title(video_file, title_map)
                out_f.write(f"\n\n## {title}\n\n")
                out_f.write(f"_{video_file.name}_\n\n")
                out_f.write(text)
                out_f.write("\n\n---\n")
                out_f.flush()
                print(f"\n[{i}/{len(files)}] Added existing transcript: {video_file.name}")
                continue

            print(f"\n[{i}/{len(files)}] Transcribing: {video_file.name}")
            
            try:
                # Run command and show output
                impl = get_whisper_impl()
                if impl == "openai":
                    run_openai_whisper(video_file, transcripts_dir)
                else:
                    run_whisper_cpp(video_file, transcripts_dir)
                
                if expected_txt.exists():
                    text = expected_txt.read_text(encoding='utf-8').strip()
                    
                    # Append to main file
                    title = resolve_title(video_file, title_map)
                    out_f.write(f"\n\n## {title}\n\n")
                    out_f.write(f"_{video_file.name}_\n\n")
                    out_f.write(text)
                    out_f.write("\n\n---\n")
                    out_f.flush() # Ensure it's saved
                    
                    print(f"  ✓ Added to transcript ({len(text)} chars)")
                else:
                    print(f"  ✗ Error: Transcript file not found at {expected_txt}")
                    
            except subprocess.CalledProcessError as e:
                print(f"  ✗ Error running whisper: {e}")
                failed_videos.add(video_file.name)
                failed_file.write_text(
                    "\n".join(sorted(failed_videos)) + "\n",
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"  ✗ Unexpected error: {e}")
                failed_videos.add(video_file.name)
                failed_file.write_text(
                    "\n".join(sorted(failed_videos)) + "\n",
                    encoding="utf-8",
                )

    print(f"\nDone! Full transcript saved to: {output_file}")

if __name__ == "__main__":
    main()
