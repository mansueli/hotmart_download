import unittest
from pathlib import Path
import tempfile
import json
from unittest import mock
import argparse

import run_course


class RunCourseTests(unittest.TestCase):
    def test_parse_product_id(self):
        self.assertEqual(run_course.parse_product_id("4459938"), "4459938")
        self.assertEqual(
            run_course.parse_product_id("https://hotmart.com/pt-br/club/jannuzzi/products/4459938"),
            "4459938",
        )
        with self.assertRaises(SystemExit):
            run_course.parse_product_id("not-a-valid-id")

    def test_safe_filename(self):
        self.assertEqual(run_course.safe_filename(" Material /abc?.pdf "), "Material-abc.pdf")
        self.assertEqual(run_course.safe_filename("...."), "file")

    def test_build_video_file_name(self):
        item = {
            "content_id": "A1",
            "order": 1,
            "lesson": "Intro / Basics",
            "module": "Module A",
        }
        self.assertEqual(run_course.build_video_file_name(item), "001 - Intro - Basics.mp4")

    def test_build_video_file_name_includes_module_for_generic_title(self):
        item = {
            "content_id": "A1",
            "order": 1,
            "module": "Module A",
            "lesson": "Introduction",
        }
        self.assertEqual(run_course.build_video_file_name(item), "001 - Module A - Introduction.mp4")

    def test_build_attachment_file_name(self):
        item = {
            "content_id": "A1",
            "order": 1,
            "module": "Module A",
            "lesson": "Introduction",
        }
        attachment = {"file_name": "Workbook Final.pdf"}
        self.assertEqual(
            run_course.build_attachment_file_name(item, attachment, 1, 1),
            "001 - Module A - Introduction - Workbook Final.pdf",
        )

    def test_ensure_manifest_video_names(self):
        manifest = {
            "items": [
                {
                    "content_id": "A1",
                    "order": 1,
                    "lesson": "Lesson One",
                    "video_file_name": None,
                }
            ]
        }
        changed = run_course.ensure_manifest_video_names(manifest)
        self.assertTrue(changed)
        self.assertEqual(manifest["items"][0]["video_file_name"], "001 - Lesson One.mp4")

    def test_ensure_manifest_attachment_names_preserves_legacy_name(self):
        manifest = {
            "items": [
                {
                    "content_id": "A1",
                    "order": 1,
                    "module": "Module A",
                    "lesson": "Introduction",
                    "attachments": [
                        {
                            "file_name": "Workbook Final.pdf",
                            "local_name": "001_A1_Workbook-Final.pdf",
                        }
                    ],
                }
            ]
        }
        changed = run_course.ensure_manifest_attachment_names(manifest)
        self.assertTrue(changed)
        attachment = manifest["items"][0]["attachments"][0]
        self.assertEqual(attachment["legacy_local_name"], "001_A1_Workbook-Final.pdf")
        self.assertEqual(attachment["local_name"], "001 - Module A - Introduction - Workbook Final.pdf")

    def test_resolve_product_url_requires_full_url_without_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                run_course.resolve_product_url("4363237", "4363237", Path(tmpdir) / "4363237")

    def test_resolve_product_url_uses_cached_manifest_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "4363237"
            output_root.mkdir(parents=True)
            (output_root / "course_manifest.json").write_text(
                json.dumps({"product_url": "https://hotmart.com/pt-br/club/falascatennis/products/4363237?x=1"}),
                encoding="utf-8",
            )
            self.assertEqual(
                run_course.resolve_product_url("4363237", "4363237", output_root),
                "https://hotmart.com/pt-br/club/falascatennis/products/4363237",
            )

    def test_extract_token_from_value(self):
        token = run_course.extract_token_from_value("Bearer AT-1234567890TOKEN")
        self.assertEqual(token, "AT-1234567890TOKEN")
        token = run_course.extract_token_from_value('{"access_token": "AT-abcdef123456"}')
        self.assertEqual(token, "AT-abcdef123456")
        token = run_course.extract_token_from_value("eyJhbGciOiJub3QiLCJ0eXAiOiJKV1QifQ.payload.sig")
        self.assertTrue(token)
        self.assertIsNone(run_course.extract_token_from_value("123"))
        self.assertIsNone(run_course.extract_token_from_value('"plain-string"'))

    def test_build_manifest(self):
        navigation = {
            "modules": [
                {
                    "name": "Module A",
                    "pages": [
                        {"name": "Lesson 1", "hash": "L1", "hasPlayerMedia": True},
                        {
                            "name": "Section",
                            "hash": "S1",
                            "hasPlayerMedia": False,
                            "pages": [
                                {"name": "Lesson 2", "hash": "L2", "hasPlayerMedia": True},
                            ],
                        },
                    ],
                }
            ]
        }
        manifest = run_course.build_manifest(
            navigation,
            "123",
            "https://hotmart.com/pt-br/club/example/products/123",
        )
        items = manifest["items"]
        self.assertEqual([item["content_id"] for item in items], ["L1", "S1", "L2"])
        self.assertEqual(items[0]["module"], "Module A")
        self.assertEqual(items[0]["lesson"], "Lesson 1")
        self.assertEqual(
            manifest["product_url"],
            "https://hotmart.com/pt-br/club/example/products/123",
        )

    def test_compute_state(self):
        manifest = {
            "product_id": "123",
            "items": [
                {
                    "content_id": "A1",
                    "attachments": [
                        {"local_name": "001_A1_doc.pdf"},
                    ],
                },
                {
                    "content_id": "B2",
                    "attachments": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            videos = root / "videos"
            materials = root / "materials"
            transcripts = root / "transcripts"
            videos.mkdir()
            materials.mkdir()
            transcripts.mkdir()

            video = videos / "001_A1_title.mp4"
            video.write_bytes(b"data")
            (transcripts / f"{video.name}.txt").write_text("ok", encoding="utf-8")

            attachment = materials / "001_A1_doc.pdf"
            attachment.write_bytes(b"pdf")
            (transcripts / f"{attachment.name}.txt").write_text("doc", encoding="utf-8")

            state = run_course.compute_state(manifest, videos, materials, transcripts)
            self.assertTrue(state["items"]["A1"]["video_downloaded"])
            self.assertTrue(state["items"]["A1"]["attachments_downloaded"])
            self.assertTrue(state["items"]["A1"]["transcribed"])
            self.assertFalse(state["items"]["B2"]["video_downloaded"])
            self.assertTrue(state["items"]["B2"]["transcribed"])

    def test_build_transcript_placeholder(self):
        manifest = {
            "items": [
                {
                    "content_id": "A1",
                    "module": "Module X",
                    "lesson": "Lesson Y",
                    "attachments": [],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            videos = root / "videos"
            materials = root / "materials"
            transcripts = root / "transcripts"
            output = root / "COURSE_TRANSCRIPT.md"
            videos.mkdir()
            materials.mkdir()
            transcripts.mkdir()

            run_course.build_transcript(manifest, videos, materials, transcripts, output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("## Module X - Lesson Y", text)
            self.assertIn("_Transcript unavailable yet._", text)

    def test_merge_video_stats(self):
        merged = run_course.merge_video_stats(
            {"processed": 2, "downloaded": 1, "skipped": 1, "failed": 0, "retried": 0},
            {"processed": 3, "downloaded": 2, "skipped": 0, "failed": 1, "retried": 1},
        )
        self.assertEqual(
            merged,
            {"processed": 5, "downloaded": 3, "skipped": 1, "failed": 1, "retried": 1},
        )

    def test_ensure_dependencies_reports_missing_tools(self):
        args = argparse.Namespace(auth_browser="playwright", chrome_bin=None)

        def fake_which(name):
            return None if name == "ffmpeg" else "/usr/bin/fake"

        with mock.patch("run_course.shutil.which", side_effect=fake_which):
            with self.assertRaises(SystemExit) as ctx:
                run_course.ensure_dependencies(args)
        self.assertIn("ffmpeg", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
