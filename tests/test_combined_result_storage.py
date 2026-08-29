from __future__ import annotations

import hashlib
import json
from dataclasses import replace
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
        map_crisper = kwargs.pop("map_crisper", True)
        if map_crisper:
            candidates = tuple(
                replace(
                    candidate,
                    words=tuple(
                        replace(
                            word,
                            speaker_id="SPEAKER_00",
                            speaker_name="Howard",
                            speaker_decision_source="automatic_unique_name",
                        )
                        if word.speaker_source_engine == "crisperwhisper"
                        else word
                        for word in candidate.words
                    ),
                )
                for candidate in preview.candidates
            )
            preview = replace(
                preview,
                candidates=candidates,
                words=tuple(word for candidate in candidates for word in candidate.words),
            )
        return result_storage.save_combined_revision(
            output_root=self.output,
            comparison=comparison,
            plan=plan,
            preview=preview,
            canonical_speakers=kwargs.pop("canonical_speakers", (
                {
                    "speaker_id": "SPEAKER_00",
                    "name": "Howard",
                    "created_for_combined": False,
                    "decision_source": "whisperx_canonical",
                },
            )),
            mapping_audit=kwargs.pop("mapping_audit", {
                "canonical_speakers": [
                    {
                        "speaker_id": "SPEAKER_00",
                        "name": "Howard",
                        "decision_source": "whisperx_canonical",
                    }
                ],
                "crisperwhisper_to_combined": [
                    {
                        "source_speaker_id": "SPEAKER_00",
                        "target_speaker_id": "SPEAKER_00",
                        "target_name": "Howard",
                        "decision_source": "automatic_unique_name",
                    }
                ],
                "region_overrides": [],
            }),
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
        self.assertEqual(
            dict(descriptor.provenance_source_states),
            {"whisperx": "available", "crisperwhisper": "available"},
        )
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
        self.assertEqual(
            dict(combined[0].provenance_source_states),
            {"whisperx": "missing", "crisperwhisper": "missing"},
        )

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

    def test_manifest_and_fusion_audit_are_versioned_and_self_consistent(self):
        comparison = self.fixture.comparison()
        committed = self.fixture.save(comparison)
        manifest = json.loads(
            (committed.result_root / "result.json").read_text(encoding="utf-8")
        )
        fusion_data = json.loads(committed.fusion_json.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["revision_id"], manifest["result_id"])
        self.assertEqual(manifest["engine"], "combined")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["combined"]["schema_version"], 1)
        self.assertEqual(
            set(manifest["combined"]["source_revisions"]),
            {"whisperx", "crisperwhisper"},
        )
        self.assertEqual(set(manifest["outputs"]), {"srt", "txt"})
        self.assertEqual(fusion_data["schema_version"], 1)
        self.assertEqual(fusion_data["result_id"], committed.result_id)
        self.assertEqual(
            fusion_data["source_revisions"],
            manifest["combined"]["source_revisions"],
        )
        self.assertTrue(fusion_data["regions"][0]["candidates"][0]["words"])
        result_catalog.validate_result_manifest(committed.result_root / "result.json")

    def test_project_manifest_rejects_unsafe_combined_fusion_path(self):
        comparison = self.fixture.comparison()
        self.fixture.save(comparison)
        project_path = self.fixture.layout.project_root / "project.json"
        project_data = json.loads(project_path.read_text(encoding="utf-8"))
        combined = next(
            record for record in project_data["results"] if record["engine"] == "combined"
        )
        combined["paths"]["fusion"] = "../outside/fusion.json"
        project_path.write_text(json.dumps(project_data), encoding="utf-8")
        with self.assertRaisesRegex(
            result_catalog.ManifestValidationError,
            "traversal|result root",
        ):
            result_catalog.validate_project_manifest(project_path)

    def test_missing_or_mutated_source_revision_is_rejected(self):
        for mutation in ("delete", "manifest"):
            with self.subTest(mutation=mutation):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                fixture = CombinedStorageFixture(Path(temporary.name))
                comparison = fixture.comparison()
                crisper_root = comparison.crisperwhisper.revision.segments_json.parent
                if mutation == "delete":
                    shutil.rmtree(crisper_root)
                else:
                    manifest_path = crisper_root / "result.json"
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    data["model"] = "substituted-model"
                    manifest_path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed after the draft"):
                    fixture.save(comparison)
                self.assertFalse((fixture.layout.project_root / "combined").exists())

    def test_changed_original_media_is_rejected_before_staging(self):
        comparison = self.fixture.comparison()
        self.fixture.source.write_bytes(b"replacement media")
        with self.assertRaisesRegex(RuntimeError, "original media changed"):
            self.fixture.save(comparison)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())

    def test_unresolved_crisper_speaker_mapping_is_rejected(self):
        comparison = self.fixture.comparison(
            [self.fixture.segment("before", 0.0, 1.0)],
            [
                self.fixture.segment("before", 0.0, 1.0),
                self.fixture.segment("crisper addition", 2.0, 3.0),
            ],
        )
        plan = result_fusion.create_fusion_plan(comparison)
        region = next(
            item
            for item in plan.regions
            if item.classification == result_comparison.CRISPERWHISPER_ONLY
        )
        plan = result_fusion.apply_decision(
            plan,
            region.region_id,
            result_fusion.INCLUDE_ENGINE_ONLY,
        )
        preview = result_fusion.build_combined_preview(plan)
        with self.assertRaisesRegex(RuntimeError, "requires a verified mapping"):
            self.fixture.save(
                comparison,
                plan=plan,
                preview=preview,
                map_crisper=False,
                mapping_audit={
                    "canonical_speakers": [],
                    "crisperwhisper_to_combined": [],
                    "region_overrides": [],
                },
            )
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())

    def test_timestamp_outside_authoritative_duration_is_rejected(self):
        comparison = self.fixture.comparison()
        plan = result_fusion.create_fusion_plan(comparison)
        preview = result_fusion.build_combined_preview(plan)
        candidate = preview.candidates[0]
        words = list(candidate.words)
        words[-1] = replace(words[-1], end=12.5)
        candidate = replace(candidate, words=tuple(words), end=12.5)
        invalid = replace(preview, candidates=(candidate,), words=tuple(words))
        with self.assertRaisesRegex(RuntimeError, "authoritative media duration"):
            self.fixture.save(comparison, plan=plan, preview=invalid)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())

    def test_optional_outputs_are_indexed_and_validated(self):
        comparison = self.fixture.comparison()

        def all_outputs(root, title, segments, names):
            outputs = CombinedStorageFixture.writer(root, title, segments, names)
            for key, name in (
                ("word_vtt", f"{title}.words.vtt"),
                ("word_ass", f"{title}.words.ass"),
                ("plain_ass", f"{title}.plain.ass"),
                ("word_html", "word_player.html"),
                ("word_lrc", f"{title}.lrc"),
            ):
                path = root / name
                path.write_text(f"{key}\n", encoding="utf-8")
                outputs[key] = path
            return outputs

        committed = self.fixture.save(comparison, write_exports=all_outputs)
        manifest = result_catalog.validate_result_manifest(
            committed.result_root / "result.json"
        )
        self.assertEqual(
            {name for name, _path, _digest in manifest.output_files},
            {
                "srt",
                "txt",
                "word_vtt",
                "word_ass",
                "plain_ass",
                "word_html",
                "word_lrc",
            },
        )

    def test_apply_manifest_updates_preserve_fusion_and_add_optional_output(self):
        comparison = self.fixture.comparison()
        committed = self.fixture.save(comparison)
        manifest_before = json.loads(
            (committed.result_root / "result.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as staging_name:
            staging = Path(staging_name)
            staged_speakers = staging / "speakers.json"
            staged_segments = staging / "segments.json"
            staged_ass = staging / "words.ass"
            staged_speakers.write_bytes(committed.speakers_json.read_bytes())
            staged_segments.write_bytes(committed.segments_json.read_bytes())
            staged_ass.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
            target_ass = committed.result_root / "Fusion fixture.words.ass"
            updates = result_storage.build_apply_manifest_updates(
                committed.result_root,
                staged_speakers,
                staged_segments,
                combined_output_files={
                    "word_ass": (target_ass, staged_ass),
                },
            )
        result_update = next(
            data for path, data in updates if path.name == "result.json"
        )
        self.assertEqual(
            result_update["fingerprint"]["fusion"],
            manifest_before["fingerprint"]["fusion"],
        )
        self.assertEqual(
            result_update["combined"],
            manifest_before["combined"],
        )
        self.assertEqual(
            result_update["outputs"]["word_ass"]["path"],
            target_ass.name,
        )

    def test_combined_apply_updates_names_outputs_and_manifests_without_touching_fusion(self):
        comparison = self.fixture.comparison()
        committed = self.fixture.save(comparison)
        fusion_before = committed.fusion_json.read_bytes()
        manifest_path = committed.result_root / "result.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        srt_target = committed.result_root / manifest["outputs"]["srt"]["path"]
        txt_target = committed.result_root / manifest["outputs"]["txt"]["path"]

        with tempfile.TemporaryDirectory() as staging_name:
            staging = Path(staging_name)
            speakers_data = json.loads(committed.speakers_json.read_text(encoding="utf-8"))
            speakers_data["names"]["SPEAKER_00"] = "Old Gregg"
            staged_speakers = staging / "speakers.json"
            staged_segments = staging / "segments.json"
            staged_srt = staging / "combined.srt"
            staged_txt = staging / "combined.txt"
            staged_speakers.write_text(json.dumps(speakers_data), encoding="utf-8")
            staged_segments.write_bytes(committed.segments_json.read_bytes())
            staged_srt.write_text("Old Gregg: Hello world.\n", encoding="utf-8")
            staged_txt.write_text("Old Gregg: Hello world.\n", encoding="utf-8")
            staged_files = {
                committed.speakers_json: staged_speakers,
                committed.segments_json: staged_segments,
                srt_target: staged_srt,
                txt_target: staged_txt,
            }
            updates = result_storage.build_apply_manifest_updates(
                committed.result_root,
                staged_speakers,
                staged_segments,
                staged_output_files=staged_files,
                combined_output_files={
                    "srt": (srt_target, staged_srt),
                    "txt": (txt_target, staged_txt),
                },
            )
            for target, staged in staged_files.items():
                target.write_bytes(staged.read_bytes())
            for target, data in updates:
                target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        reopened = result_catalog.descriptor_from_json_pair(
            committed.speakers_json,
            committed.segments_json,
        )
        self.assertEqual(reopened.engine, "combined")
        self.assertEqual(reopened.status, "complete")
        self.assertEqual(committed.fusion_json.read_bytes(), fusion_before)
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_manifest["combined"], manifest["combined"])
        self.assertEqual(
            json.loads(committed.speakers_json.read_text(encoding="utf-8"))["names"],
            {"SPEAKER_00": "Old Gregg"},
        )

    def test_export_writer_exception_cleans_staging_and_keeps_source_revisions(self):
        comparison = self.fixture.comparison()
        source_roots = (
            comparison.whisperx.revision.speakers_json.parent,
            comparison.crisperwhisper.revision.speakers_json.parent,
        )
        before = tuple(self.file_hashes(root) for root in source_roots)

        def fail_after_partial_output(root, title, segments, names):
            (root / f"{title}.srt").write_text("partial", encoding="utf-8")
            raise OSError("forced exporter failure")

        with self.assertRaisesRegex(OSError, "forced exporter failure"):
            self.fixture.save(comparison, write_exports=fail_after_partial_output)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())
        self.assertEqual(list(self.fixture.layout.project_root.glob(".staging-*")), [])
        self.assertEqual(tuple(self.file_hashes(root) for root in source_roots), before)

    def test_decisions_history_omissions_inclusions_and_warnings_round_trip(self):
        shared = "do you love me"
        addition = "beneath the water"
        shifted = "nice spread"
        comparison = self.fixture.comparison(
            [
                self.fixture.segment(shared, 1.0, 2.0),
                self.fixture.segment(shifted, 8.0, 9.0),
            ],
            [
                self.fixture.segment(shared, 1.0, 2.0),
                self.fixture.segment(shared, 4.0, 5.0),
                self.fixture.segment(addition, 6.0, 7.0),
                self.fixture.segment(shifted, 10.5, 11.5),
            ],
        )
        plan = result_fusion.create_fusion_plan(comparison)
        duplicate = next(
            item
            for item in plan.regions
            if item.classification == result_comparison.POSSIBLE_DUPLICATE
        )
        engine_only = next(
            item
            for item in plan.regions
            if item.classification == result_comparison.CRISPERWHISPER_ONLY
        )
        timing = next(
            item
            for item in plan.regions
            if item.classification == result_comparison.TIMING_CONFLICT
        )
        plan = result_fusion.apply_decision(
            plan, duplicate.region_id, result_fusion.OMIT_BOTH
        )
        plan = result_fusion.apply_decision(
            plan, engine_only.region_id, result_fusion.INCLUDE_ENGINE_ONLY
        )
        plan = result_fusion.apply_decision(
            plan, timing.region_id, result_fusion.USE_CRISPERWHISPER_TIMING
        )
        plan = result_fusion.reset_decision(plan, timing.region_id)
        plan = result_fusion.apply_decision(
            plan, timing.region_id, result_fusion.USE_CRISPERWHISPER_TIMING
        )
        plan = replace(plan, warnings=("Source coverage warning retained.",))
        preview = result_fusion.build_combined_preview(plan)
        committed = self.fixture.save(comparison, plan=plan, preview=preview)
        fusion_data = json.loads(committed.fusion_json.read_text(encoding="utf-8"))
        manifest = json.loads(
            (committed.result_root / "result.json").read_text(encoding="utf-8")
        )

        self.assertIn(duplicate.region_id, fusion_data["omitted_region_ids"])
        self.assertIn(
            engine_only.region_id,
            fusion_data["included_engine_only_region_ids"],
        )
        self.assertGreaterEqual(len(fusion_data["decision_history"]), 5)
        self.assertEqual(
            fusion_data["source_coverage_warnings"],
            ["Source coverage warning retained."],
        )
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(
            manifest["combined"]["source_coverage_warnings"],
            ["Source coverage warning retained."],
        )

    def test_revision_collision_keeps_existing_combined_revision_unchanged(self):
        comparison = self.fixture.comparison()
        result_id = "20260826T130000000000Z-deadbeef"
        committed = self.fixture.save(comparison, result_id=result_id)
        before = self.file_hashes(committed.result_root)
        with self.assertRaises(FileExistsError):
            self.fixture.save(comparison, result_id=result_id)
        self.assertEqual(self.file_hashes(committed.result_root), before)
        self.assertEqual(list(self.fixture.layout.project_root.glob(".staging-*")), [])

    def test_validation_and_preview_alone_write_no_combined_files(self):
        comparison = self.fixture.comparison()
        plan = result_fusion.create_fusion_plan(comparison)
        preview = result_fusion.build_combined_preview(plan)
        self.assertTrue(preview.final_ready)
        self.assertFalse((self.fixture.layout.project_root / "combined").exists())

    def test_saved_combined_reopens_when_media_changes(self):
        comparison = self.fixture.comparison()
        committed = self.fixture.save(comparison)
        self.fixture.source.write_bytes(b"changed after Combined save")
        descriptor = result_catalog.descriptor_from_json_pair(
            committed.speakers_json,
            committed.segments_json,
        )
        self.assertEqual(descriptor.engine, "combined")
        self.assertEqual(descriptor.source_state, "changed")


if __name__ == "__main__":
    unittest.main()
