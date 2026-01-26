import unittest
from pathlib import Path
import tempfile

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

    def test_extract_token_from_value(self):
        token = run_course.extract_token_from_value("Bearer AT-1234567890TOKEN")
        self.assertEqual(token, "AT-1234567890TOKEN")
        token = run_course.extract_token_from_value('{"access_token": "AT-abcdef123456"}')
        self.assertEqual(token, "AT-abcdef123456")
        token = run_course.extract_token_from_value("eyJhbGciOiJub3QiLCJ0eXAiOiJKV1QifQ.payload.sig")
        self.assertTrue(token)

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
        manifest = run_course.build_manifest(navigation, "123")
        items = manifest["items"]
        self.assertEqual([item["content_id"] for item in items], ["L1", "S1", "L2"])
        self.assertEqual(items[0]["module"], "Module A")
        self.assertEqual(items[0]["lesson"], "Lesson 1")

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


if __name__ == "__main__":
    unittest.main()
