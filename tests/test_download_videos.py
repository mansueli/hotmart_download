import tempfile
import unittest
from pathlib import Path

import download_videos


class DownloadVideosTests(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(download_videos.format_size(0), "0 B")
        self.assertEqual(download_videos.format_size(1024), "1.0 KiB")
        self.assertEqual(download_videos.format_size(5 * 1024 * 1024), "5.0 MiB")

    def test_failed_downloads_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "FAILED_DOWNLOADS.json"
            payload = {
                "abc123": {
                    "title": "Lesson 1",
                    "reason": "ffmpeg missing",
                }
            }
            download_videos.write_failed_downloads(path, payload)
            self.assertEqual(download_videos.load_failed_downloads(path), payload)

    def test_write_failed_downloads_removes_empty_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "FAILED_DOWNLOADS.json"
            path.write_text("{}", encoding="utf-8")
            download_videos.write_failed_downloads(path, {})
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()