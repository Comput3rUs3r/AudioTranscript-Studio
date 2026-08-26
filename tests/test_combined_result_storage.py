from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import result_catalog
import result_comparison
import result_fusion
import result_storage


class CombinedStorageFixture:
    def __init__(self, root: Path):
        self.root = root
        self.output = root / "output"
        self.output.mkdir()
        self.source = root / "media" / "fixture.wav"
        self.source.parent.mkdir()
        self.source.write_bytes(b"synthetic media identity")
        self.layout = result_storage.project_layout_for_source(
            self.output,
            "Fusion fixture",
            self.source,
        )

    @staticmethod
    def words(text, start, end, speaker="SPEAKER_00"):
        tokens = text.split()
        step = (end - start) / len(tokens)
        return [
            {
                "word": token,
                "start": start + index * step,
                "end": start + (index + 1) * step,
                "speaker": speaker,
            }
            for index, token in enumerate(tokens)
        ]

    def segment(self, text, start, end, speaker="SPEAKER_00"):
        return {
            "start": start,
            "end": end,
            "text": text,
            "speaker": speaker,
            "words": self.words(text, start, end, speaker),
        }

    def revision(self, engine, result_id, segments, *, model, mode=None, status="complete"):
        with result_storage.ResultRevision(
            self.layout,
            engine,
            result_id=result_id,
        ) as revision:
            (revision.output_root / "segments.json").write_text(
                json.dumps(
                    {
                        "title": self.layout.display_title,
                        "source_path": str(self.source.resolve()),
                        "source_identity": dict(self.layout.source_identity),
                        "media_duration": 12.0,
                        "segments": segments,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (revision.output_root / "speakers.json").write_text(
                json.dumps(
                    {
                        "title": self.layout.display_title,
                        "diarization": True,
                        "speakers": ["SPEAKER_00"],
                        "names": {"SPEAKER_00": "Howard"},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            root = revision.commit(
                model=model,
                mode=mode,
                execution_backend=(
                    "transformers" if engine == "crisperwhisper" else "ctranslate2"
                ),
                status=status,
                comparison_job_id="both:fixture",
                validate_outputs=lambda path: result_catalog.preflight_result_pair(
                    path / "speakers.json",
                    path / "segments.json",
                ),
            )
        return result_catalog.descriptor_from_json_pair(
            root / "speakers.json",
            root / "segments.json",
        )

    def comparison(self, whisper_segments=None, crisper_segments=None):
        whisper_segments = whisper_segments or [self.segment("Hello world.", 1.0, 2.0)]
        crisper_segments = crisper_segments or [self.segment("Hello world.", 1.0, 2.0)]
        whisper = self.revision(
            "whisperx",
            "20260826T120000000000Z-00000001",
            whisper_segments,
            model="large-v3",
        )
        crisper = self.revision(
            "crisperwhisper",
            "20260826T120100000000Z-00000002",
            crisper_segments,
            model="large",
            mode="verbatim",
        )
        return result_comparison.compare_results(whisper, crisper)

    @staticmethod
    def writer(root, title, segments, names):
        srt = root / f"{title}.srt"
        txt = root / f"{title}.txt"
        srt.write_text(
            "\n".join(str(segment["text"]) for segment in segments) + "\n",
            encoding="utf-8",
        )
        txt.write_text(
            "\n".join(str(segment["text"]) for segment in segments) + "\n",
            encoding="utf-8",
        )
        return {"srt": srt, "txt": txt}

    def save(self, comparison, plan=None, preview=None, **kwargs):
        plan = plan or result_fusion.create_fusion_plan(comparison)
        preview = preview or result_fusion.build_combined_preview(plan)
        return result_storage.save_combined_revision(
            output_root=self.output,
            comparison=comparison,
            plan=plan,
            preview=preview,
            canonical_speakers=(
                {
                    "speaker_id": "SPEAKER_00",
                    "name": "Howard",
                    "created_for_combined": False,
                    "decision_source": "whisperx_canonical",
                },
            ),
            mapping_audit={
                "canonical_speakers": [
                    {
                        "speaker_id": "SPEAKER_00",
                        "name": "Howard",
                        "decision_source": "whisperx_canonical",
                    }
                ],
                "crisperwhisper_to_combined": [],
                "region_overrides": [],
            },
            write_exports=kwargs.pop("write_exports", self.writer),
            **kwargs,
        )


class CombinedResultStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = CombinedStorageFixture(self.root)

    @staticmethod
    def file_hashes(root):
        return {
            path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_successful_save_is_discoverable_and_preserves_source_revisions(self):
        comparison = self.fixture.comparison()
        source_roots = (
            comparison.whisperx.revision.speakers_json.parent,
            comparison.crisperwhisper.revision.speakers_json.parent,
        )
        before = tuple(self.file_hashes(root) for root in source_roots)

        committed = self.fixture.save(comparison)

        self.assertEqual(
            set(path.name for path in committed.result_root.iterdir()),
            {
                "result.json",
                "segments.json",
                "speakers.json",
                "fusion.json",
                "Fusion fixture.srt",
                "Fusion fixture.txt",
            },
        )
        self.assertEqual(tuple(self.file_hashes(root) for root in source_roots), before)
        descriptor = result_catalog.descriptor_from_json_pair(
            committed.speakers_json,
            committed.segments_json,
        )
        self.assertEqual(descriptor.engine, "combined")
        self.assertEqual(descriptor.model, "large-v3 + large")
        self.assertEqual(descriptor.mode, "User-reviewed fusion")
        self.assertEqual(descriptor.status, "complete")
        project = json.loads(
            (self.fixture.layout.project_root / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(project["active_results"]["whisperx"], comparison.whisperx.revision.result_id)
        self.assertEqual(project["active_results"]["crisperwhisper"], comparison.crisperwhisper.revision.result_id)
        self.assertEqual(project["active_results"]["combined"], descriptor.result_id)
        self.assertEqual([item["engine"] for item in project["results"]].count("combined"), 1)
        self.assertEqual(list(self.fixture.layout.project_root.glob(".staging-*")), [])

    def test_fusion_audit_and_pipeline_json_round_trip(self):
        comparison = self.fixture.comparison()
        committed = self.fixture.save(comparison)
        fusion_data = json.loads(committed.fusion_json.read_text(encoding="utf-8"))
        segments_data = json.loads(committed.segments_json.read_text(encoding="utf-8"))
        speakers_data = json.loads(committed.speakers_json.read_text(encoding="utf-8"))

        self.assertEqual(fusion_data["schema_version"], 1)
        self.assertTrue(fusion_data["validation"]["final_ready"])
        self.assertEqual(fusion_data["validation"]["unresolved_count"], 0)
        self.assertEqual(len(fusion_data["per_word_provenance"]), 2)
        self.assertEqual(segments_data["transcription"]["engine"], "combined")
        self.assertEqual(segments_data["segments"][0]["text"], "Hello world.")
        self.assertEqual(speakers_data["names"], {"SPEAKER_00": "Howard"})
        self.assertEqual(
            "\n".join(item["text"] for item in segments_data["segments"]),
            "Hello world.",
        )

    def test_catalog_recovers_combined_without_project_manifest_or_source_revisions(self):
        comparison = self.fixture.comparison()
        committed = self.fixture.save(comparison)
        (self.fixture.layout.project_root / "project.json").unlink()
        shutil.rmtree(comparison.whisperx.revision.speakers_json.parent)
        shutil.rmtree(comparison.crisperwhisper.revision.speakers_json.parent)
        self.fixture.source.unlink()

        discovery = result_catalog.discover_results(self.fixture.output)
        combined = [item for item in discovery.results if item.engine == "combined"]
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0].source_state, "missing")
        self.assertEqual(combined[0].segments_json, committed.segments_json)

    def test_project_update_failure_rolls_back_revision_and_preserves_project(self):
        comparison = self.fixture.comparison()
        project_path = self.fixture.layout.project_root / "project.json"
        project_before = project_path.read_bytes()
        combined_root = self.fixture.layout.project_root / "combined"
        with mock.patch(
            "result_storage._write_project_update",
            side_effect=OSError("forced manifest failure"),
        ):
            with self.assertRaisesRegex(OSError, "forced manifest failure"):
                self.fixture.save(comparison)
        self.assertEqual(project_path.read_bytes(), project_before)
        self.assertFalse(combined_root.exists())
        self.assertEqual(list(self.fixture.layout.project_root.glob(".staging-*")), [])

    def test_export_validation_failure_leaves_no_revision(self):
        comparison = self.fixture.comparison()

        def missing_txt(root, title, segments, names):
            path = root / f"{title}.srt"
            path.write_text("subtitle", encoding="utf-8")
            return {"srt": path}

        with self.assertRaisesRegex(RuntimeError, "required TXT"):
            self.fixture.save(comparison, write_exports=missing_txt)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())
        self.assertEqual(list(self.fixture.layout.project_root.glob(".staging-*")), [])

    def test_stale_source_revision_is_rejected_without_a_combined_revision(self):
        comparison = self.fixture.comparison()
        segment_path = comparison.crisperwhisper.revision.segments_json
        data = json.loads(segment_path.read_text(encoding="utf-8"))
        data["segments"][0]["text"] = "changed after validation"
        segment_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed after the draft"):
            self.fixture.save(comparison)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())

    def test_provisional_and_unresolved_plans_cannot_be_saved(self):
        comparison = self.fixture.comparison(
            [self.fixture.segment("Whisper only", 1.0, 2.0)],
            [self.fixture.segment("Crisper only", 1.0, 2.0)],
        )
        plan = result_fusion.create_fusion_plan(comparison)
        preview = result_fusion.build_combined_preview(plan, provisional=True)
        with self.assertRaisesRegex(RuntimeError, "final-ready"):
            self.fixture.save(comparison, plan=plan, preview=preview)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())

    def test_mixed_text_and_timing_sources_are_persisted_exactly(self):
        comparison = self.fixture.comparison(
            [
                self.fixture.segment("before", 0.0, 1.0),
                self.fixture.segment("73,000", 2.0, 2.6),
                self.fixture.segment("after", 4.0, 5.0),
            ],
            [
                self.fixture.segment("before", 0.0, 1.0),
                self.fixture.segment("seventy three thousand", 1.8, 3.0),
                self.fixture.segment("after", 4.0, 5.0),
            ],
        )
        plan = result_fusion.create_fusion_plan(comparison)
        conflict = next(
            region
            for region in plan.regions
            if region.classification == result_comparison.TEXT_CONFLICT
        )
        plan = result_fusion.apply_decision(
            plan,
            conflict.region_id,
            result_fusion.USE_CRISPERWHISPER_TEXT,
        )
        preview = result_fusion.build_combined_preview(plan)
        committed = self.fixture.save(comparison, plan=plan, preview=preview)
        segments_data = json.loads(committed.segments_json.read_text(encoding="utf-8"))
        fusion_data = json.loads(committed.fusion_json.read_text(encoding="utf-8"))
        serialized_text = "\n".join(item["text"] for item in segments_data["segments"])

        self.assertIn("seventy three thousand", serialized_text)
        self.assertNotIn("73,000", serialized_text)
        mixed = next(
            item
            for item in fusion_data["per_word_provenance"]
            if item["text"] == "seventy"
        )
        self.assertEqual(mixed["text_source"], "crisperwhisper")
        self.assertEqual(mixed["timing_source"], "whisperx")
        self.assertEqual(mixed["timing"]["kind"], "derived")
        self.assertEqual(mixed["timing"]["transformation"], "proportional_region_fit")


if __name__ == "__main__":
    unittest.main()
