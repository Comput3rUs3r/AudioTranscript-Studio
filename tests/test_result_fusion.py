from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import result_catalog
import result_comparison as comparison
import result_fusion as fusion


class FusionFixture:
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source.wav"
        self.source.write_bytes(b"synthetic fusion source")
        self.identity = result_catalog.build_source_identity(self.source)
        self._result_number = 0

    @staticmethod
    def words(text, start, *, end=None, speaker="SPEAKER_00"):
        tokens = text.split()
        if start is None:
            return [
                {"word": token, "speaker": speaker}
                for token in tokens
            ]
        duration = (float(end) - float(start)) if end is not None else len(tokens) * 0.3
        step = duration / len(tokens)
        current = float(start)
        words = []
        for token in tokens:
            words.append(
                {
                    "word": token,
                    "start": current,
                    "end": current + step,
                    "speaker": speaker,
                }
            )
            current += step
        return words

    def segment(self, text, start, end=None, *, speaker="SPEAKER_00"):
        words = self.words(text, start, end=end, speaker=speaker)
        data = {
            "text": text,
            "speaker": speaker,
            "words": words,
        }
        if start is not None:
            data["start"] = float(start)
            data["end"] = words[-1]["end"] if end is None else float(end)
        return data

    def descriptor(
        self,
        engine,
        segments,
        *,
        names=None,
        source_path=None,
        source_identity=None,
    ):
        self._result_number += 1
        root = self.root / f"result-{self._result_number}-{engine}"
        root.mkdir()
        speakers_path = root / "speakers.json"
        segments_path = root / "segments.json"
        speakers_path.write_text(
            json.dumps(
                {
                    "title": "Fusion fixture",
                    "speakers": ["SPEAKER_00", "SPEAKER_01"],
                    "names": names or {},
                    "unknown_speaker_key": {"preserved": True},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        data = {
            "title": "Fusion fixture",
            "source_path": str((source_path or self.source).resolve()),
            "source_identity": source_identity or self.identity,
            "segments": segments,
            "unknown_top_level_key": ["unchanged"],
        }
        if engine == "crisperwhisper":
            data["transcription"] = {
                "engine": "crisperwhisper",
                "model": {"family": "medium"},
                "effective_settings": {"transcription": {"mode": "verbatim"}},
                "execution_backend": "transformers",
            }
        segments_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result_catalog.descriptor_from_json_pair(
            speakers_path,
            segments_path,
        )

    def compare(self, whisper_segments, crisper_segments, *, names=None):
        whisper = self.descriptor("whisperx", whisper_segments, names=names)
        crisper = self.descriptor(
            "crisperwhisper",
            crisper_segments,
            names=names,
        )
        return comparison.compare_results(whisper, crisper)


class FusionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = FusionFixture(self.root)

    def agreement_comparison(self):
        return self.fixture.compare(
            [self.fixture.segment("Hello, world!", 1.0, 1.8)],
            [self.fixture.segment("hello world", 1.05, 1.85)],
            names={"SPEAKER_00": "Howard"},
        )

    def test_strict_pair_identity_and_immutable_plan(self):
        result = self.agreement_comparison()
        plan = fusion.create_fusion_plan(result)
        self.assertEqual(plan.pair_identity.comparison_pair_id, result.pair_id)
        self.assertEqual(
            plan.pair_identity.whisperx_revision.result_id,
            result.whisperx.revision.result_id,
        )
        self.assertEqual(
            plan.pair_identity.crisperwhisper_revision.segments_sha256,
            result.crisperwhisper.revision.segments_sha256,
        )
        self.assertEqual(
            plan.pair_identity.source_identity,
            result.whisperx.revision.source_identity,
        )
        with self.assertRaises(FrozenInstanceError):
            plan.warnings = ()

        altered_revision = replace(
            result.crisperwhisper.revision,
            source_identity=(("path", "different"),),
        )
        altered = replace(
            result,
            crisperwhisper=replace(
                result.crisperwhisper,
                revision=altered_revision,
            ),
        )
        with self.assertRaises(fusion.FusionIdentityError):
            fusion.create_fusion_plan(altered)
        with self.assertRaises(fusion.FusionIdentityError):
            fusion.create_fusion_plan(replace(result, pair_id="0" * 64))

    def test_shared_comparison_job_identity_is_retained(self):
        result = self.agreement_comparison()
        job_id = "both:synthetic-comparison"
        altered = replace(
            result,
            whisperx=replace(
                result.whisperx,
                revision=replace(
                    result.whisperx.revision,
                    comparison_job_id=job_id,
                ),
            ),
            crisperwhisper=replace(
                result.crisperwhisper,
                revision=replace(
                    result.crisperwhisper.revision,
                    comparison_job_id=job_id,
                ),
            ),
        )
        identity = fusion.create_fusion_plan(altered).pair_identity
        self.assertEqual(identity.comparison_job_id, job_id)
        self.assertEqual(identity.whisperx_revision.comparison_job_id, job_id)
        self.assertEqual(identity.crisperwhisper_revision.comparison_job_id, job_id)

    def test_agreement_is_one_automatic_copy_with_dual_provenance(self):
        result = self.agreement_comparison()
        plan = fusion.create_fusion_plan(result)
        self.assertTrue(plan.ready_for_final_generation)
        self.assertEqual(plan.summary.automatically_resolved_region_count, 1)
        self.assertEqual(plan.summary.explicitly_resolved_region_count, 0)
        region = plan.regions[0]
        self.assertTrue(region.automatically_resolved)
        self.assertEqual(len(region.candidates), 1)
        self.assertEqual(region.candidates[0].source_engine, "whisperx")
        self.assertEqual(
            region.candidates[0].supporting_engines,
            ("whisperx", "crisperwhisper"),
        )
        preview = fusion.build_combined_preview(plan)
        self.assertEqual(len(preview.words), len(result.whisperx.words))
        self.assertTrue(preview.final_ready)
        self.assertTrue(
            all(
                tuple(item.source_engine for item in word.provenance)
                == ("whisperx", "crisperwhisper")
                for word in preview.words
            )
        )
        self.assertTrue(
            all(
                item.decision_source == fusion.AUTOMATIC_AGREEMENT
                for word in preview.words
                for item in word.provenance
            )
        )

    def test_discrepancy_classifications_remain_unresolved(self):
        scenarios = {
            comparison.TIMING_CONFLICT: self.fixture.compare(
                [self.fixture.segment("matching words", 1.0, 1.8)],
                [self.fixture.segment("matching words", 5.0, 5.8)],
            ),
            comparison.TEXT_CONFLICT: self.fixture.compare(
                [self.fixture.segment("the cat sleeps", 1.0, 1.9)],
                [self.fixture.segment("the dog waits", 1.0, 1.9)],
            ),
            comparison.WHISPERX_ONLY: self.fixture.compare(
                [self.fixture.segment("alpha extra words omega", 1.0, 2.2)],
                [self.fixture.segment("alpha omega", 1.0, 1.6)],
            ),
            comparison.CRISPERWHISPER_ONLY: self.fixture.compare(
                [self.fixture.segment("alpha omega", 1.0, 1.6)],
                [self.fixture.segment("alpha extra words omega", 1.0, 2.2)],
            ),
        }
        for classification, result in scenarios.items():
            with self.subTest(classification=classification):
                plan = fusion.create_fusion_plan(result)
                matching = [
                    region
                    for region in plan.regions
                    if region.classification == classification
                ]
                self.assertTrue(matching)
                self.assertTrue(all(not region.resolved for region in matching))
                self.assertFalse(plan.ready_for_final_generation)

        duplicate_result = self.fixture.compare(
            [self.fixture.segment("sing the moon tonight ending", 0.0)],
            [
                self.fixture.segment("sing the moon tonight", 0.0),
                self.fixture.segment("sing the moon tonight", 10.0),
                self.fixture.segment("ending", 12.0),
            ],
        )
        duplicate_plan = fusion.create_fusion_plan(duplicate_result)
        duplicates = [
            region
            for region in duplicate_plan.regions
            if region.classification == comparison.POSSIBLE_DUPLICATE
        ]
        self.assertTrue(duplicates)
        self.assertTrue(all(not region.resolved for region in duplicates))

        unresolved_comparison = replace(
            result,
            regions=(
                replace(result.regions[0], classification=comparison.UNRESOLVED),
            ),
        )
        unresolved_plan = fusion.create_fusion_plan(unresolved_comparison)
        self.assertEqual(unresolved_plan.regions[0].classification, comparison.UNRESOLVED)
        self.assertFalse(unresolved_plan.regions[0].resolved)

    def test_timing_conflict_preserves_common_wording_and_both_exact_timings(self):
        result = self.fixture.compare(
            [self.fixture.segment("same wording", 394.33, 395.59)],
            [self.fixture.segment("same wording", 417.78, 419.04)],
        )
        plan = fusion.create_fusion_plan(result)
        region = next(
            item
            for item in plan.regions
            if item.classification == comparison.TIMING_CONFLICT
        )
        self.assertEqual(
            tuple(candidate.start for candidate in region.candidates),
            (394.33, 417.78),
        )
        self.assertEqual(
            tuple(candidate.text for candidate in region.candidates),
            ("same wording", "same wording"),
        )
        self.assertTrue(all(len(word.provenance) == 2 for candidate in region.candidates for word in candidate.words))

    def test_valid_decisions_return_new_plan_and_reset_restores_unresolved(self):
        result = self.fixture.compare(
            [self.fixture.segment("same wording", 10.0, 11.0)],
            [self.fixture.segment("same wording", 14.0, 15.0)],
        )
        original = fusion.create_fusion_plan(result)
        region = original.regions[0]
        decided = fusion.apply_decision(
            original,
            region.region_id,
            fusion.USE_CRISPERWHISPER_TIMING,
        )
        self.assertIsNone(original.regions[0].decision)
        self.assertIsNotNone(decided.regions[0].decision)
        self.assertTrue(decided.ready_for_final_generation)
        self.assertEqual(decided.summary.explicitly_resolved_region_count, 1)
        self.assertEqual(len(decided.decision_history), 1)
        preview = fusion.build_combined_preview(decided)
        self.assertEqual(preview.words[0].start, 14.0)
        self.assertTrue(
            all(
                item.decision_source == fusion.EXPLICIT_USER_DECISION
                for word in preview.words
                for item in word.provenance
            )
        )

        reset = fusion.reset_decision(decided, region.region_id)
        self.assertFalse(reset.ready_for_final_generation)
        self.assertIsNone(reset.regions[0].decision)
        self.assertEqual(len(reset.decision_history), 2)
        self.assertIsNotNone(decided.regions[0].decision)

    def test_invalid_decisions_are_rejected_by_classification(self):
        agreement = fusion.create_fusion_plan(self.agreement_comparison())
        with self.assertRaises(fusion.FusionDecisionError):
            fusion.apply_decision(
                agreement,
                agreement.regions[0].region_id,
                fusion.USE_CRISPERWHISPER,
            )

        timing = fusion.create_fusion_plan(
            self.fixture.compare(
                [self.fixture.segment("same words", 1.0, 1.8)],
                [self.fixture.segment("same words", 5.0, 5.8)],
            )
        )
        with self.assertRaises(fusion.FusionDecisionError):
            fusion.apply_decision(
                timing,
                timing.regions[0].region_id,
                fusion.KEEP_BOTH,
            )

        engine_only = fusion.create_fusion_plan(
            self.fixture.compare(
                [self.fixture.segment("alpha extra words omega", 1.0, 2.2)],
                [self.fixture.segment("alpha omega", 1.0, 1.6)],
            )
        )
        region = next(
            item
            for item in engine_only.regions
            if item.classification == comparison.WHISPERX_ONLY
        )
        with self.assertRaises(fusion.FusionDecisionError):
            fusion.apply_decision(engine_only, region.region_id, fusion.USE_WHISPERX)

    def test_text_conflict_supports_explicit_both_or_omit_when_safe(self):
        result = self.fixture.compare(
            [self.fixture.segment("whisper alternative", 1.0, 1.8)],
            [self.fixture.segment("crisper version", 3.0, 3.8)],
        )
        plan = fusion.create_fusion_plan(result)
        region = next(
            item
            for item in plan.regions
            if item.classification == comparison.TEXT_CONFLICT
        )
        both = fusion.apply_decision(plan, region.region_id, fusion.KEEP_BOTH)
        both_preview = fusion.build_combined_preview(both)
        self.assertEqual(len(both_preview.candidates), 2)
        self.assertIn("whisper alternative", both_preview.text)
        self.assertIn("crisper version", both_preview.text)

        omitted = fusion.apply_decision(plan, region.region_id, fusion.OMIT_BOTH)
        self.assertTrue(omitted.ready_for_final_generation)
        self.assertEqual(fusion.build_combined_preview(omitted).words, ())

    def test_text_conflict_can_use_crisper_wording_with_safe_whisper_timing(self):
        result = self.fixture.compare(
            [
                self.fixture.segment("before", 7.4, 8.0),
                self.fixture.segment("73,000", 8.18, 8.76),
                self.fixture.segment("after", 8.9, 9.4),
            ],
            [
                self.fixture.segment("before", 7.4, 8.0),
                self.fixture.segment("seventy three thousand", 7.95, 9.08),
                self.fixture.segment("after", 8.9, 9.4),
            ],
        )
        plan = fusion.create_fusion_plan(result)
        region = next(
            item
            for item in plan.regions
            if item.classification == comparison.TEXT_CONFLICT
        )
        original_crisper_words = region.candidates[1].words
        valid = fusion.valid_decision_actions(plan, region.region_id)
        self.assertIn(fusion.USE_CRISPERWHISPER_TEXT, valid)
        self.assertNotIn(fusion.USE_CRISPERWHISPER_TIMING, valid)
        self.assertNotIn(fusion.KEEP_BOTH, valid)

        decided = fusion.apply_decision(
            plan,
            region.region_id,
            fusion.USE_CRISPERWHISPER_TEXT,
        )
        decision = next(
            item for item in decided.regions if item.region_id == region.region_id
        ).decision
        self.assertEqual(decision.text_source_engine, "crisperwhisper")
        self.assertEqual(decision.timing_source_engine, "whisperx")
        preview = fusion.build_combined_preview(decided)
        candidate = next(
            item for item in preview.candidates if item.region_id == region.region_id
        )
        self.assertEqual(candidate.text, "seventy three thousand")
        self.assertAlmostEqual(candidate.start, 8.18)
        self.assertAlmostEqual(candidate.end, 8.76)
        self.assertEqual(
            tuple(word.text for word in candidate.words),
            ("seventy", "three", "thousand"),
        )
        self.assertTrue(all(word.timing_is_derived for word in candidate.words))
        self.assertTrue(
            all(
                word.timing_transformation == "proportional_region_fit"
                for word in candidate.words
            )
        )
        self.assertEqual(region.candidates[1].words, original_crisper_words)
        self.assertTrue(
            all(
                word.text_source_engine == "crisperwhisper"
                and word.timing_source_engine == "whisperx"
                and word.speaker_source_engine == "crisperwhisper"
                for word in candidate.words
            )
        )
        self.assertTrue(
            all(word.end - word.start >= 0.020 - 1e-9 for word in candidate.words)
        )
        self.assertEqual(candidate.words[0].start, candidate.start)
        self.assertEqual(candidate.words[-1].end, candidate.end)
        self.assertTrue(
            all(
                {item.source_engine for item in word.provenance}
                == {"whisperx", "crisperwhisper"}
                for word in candidate.words
            )
        )
        self.assertTrue(
            all(
                any("text" in item.source_roles for item in word.provenance)
                and any(
                    "timing_region_reference" in item.source_roles
                    for item in word.provenance
                )
                for word in candidate.words
            )
        )
        with self.assertRaisesRegex(
            fusion.FusionValidationError,
            "overlap",
        ):
            fusion.apply_decision(
                decided,
                region.region_id,
                fusion.USE_CRISPERWHISPER_TIMING,
            )

    def test_mixed_word_fit_fails_when_region_cannot_hold_minimum_durations(self):
        result = self.fixture.compare(
            [self.fixture.segment("73,000", 1.0, 1.04)],
            [self.fixture.segment("seventy three thousand", 1.0, 1.08)],
        )
        plan = fusion.create_fusion_plan(result)
        region = next(
            item
            for item in plan.regions
            if item.classification == comparison.TEXT_CONFLICT
        )
        with self.assertRaisesRegex(
            fusion.FusionValidationError,
            "at least 20 ms",
        ):
            fusion.apply_decision(
                plan,
                region.region_id,
                fusion.USE_CRISPERWHISPER_TEXT,
            )

    def test_derived_fit_preserves_source_duration_weighting(self):
        result = self.fixture.compare(
            [self.fixture.segment("73,000", 2.0, 2.6)],
            [self.fixture.segment("seventy three thousand", 2.0, 3.2)],
        )
        conflict = next(
            item
            for item in result.regions
            if item.classification == comparison.TEXT_CONFLICT
        )
        weighted = tuple(
            replace(word, start=start, end=end)
            for word, (start, end) in zip(
                conflict.crisperwhisper_words,
                ((2.0, 2.1), (2.1, 2.4), (2.4, 3.2)),
            )
        )
        altered_conflict = replace(
            conflict,
            crisperwhisper_words=weighted,
        )
        altered = replace(
            result,
            regions=tuple(
                altered_conflict if item.region_id == conflict.region_id else item
                for item in result.regions
            ),
        )
        plan = fusion.create_fusion_plan(altered)
        decided = fusion.apply_decision(
            plan,
            conflict.region_id,
            fusion.USE_CRISPERWHISPER_TEXT,
        )
        candidate = fusion.build_combined_preview(decided).candidates[0]
        durations = tuple(word.end - word.start for word in candidate.words)
        self.assertLess(durations[0], durations[1])
        self.assertLess(durations[1], durations[2])
        self.assertAlmostEqual(sum(durations), 0.6)
        self.assertAlmostEqual(durations[1] / durations[0], 3.0)
        self.assertAlmostEqual(durations[2] / durations[0], 8.0)

    def test_text_and_timing_actions_preserve_the_other_dimension(self):
        result = self.fixture.compare(
            [self.fixture.segment("alpha", 1.0, 1.6)],
            [self.fixture.segment("beta gamma", 2.0, 2.8)],
        )
        plan = fusion.create_fusion_plan(result)
        region = next(
            item
            for item in plan.regions
            if item.classification == comparison.TEXT_CONFLICT
        )
        timing_first = fusion.apply_decision(
            plan,
            region.region_id,
            fusion.USE_CRISPERWHISPER_TIMING,
        )
        partial = timing_first.regions[plan.regions.index(region)]
        self.assertFalse(partial.resolved)
        self.assertIsNone(partial.decision.text_source_engine)
        self.assertEqual(partial.decision.timing_source_engine, "crisperwhisper")
        completed = fusion.apply_decision(
            timing_first,
            region.region_id,
            fusion.USE_WHISPERX_TEXT,
        )
        completed_region = completed.regions[plan.regions.index(region)]
        self.assertTrue(completed_region.resolved)
        self.assertEqual(completed_region.decision.text_source_engine, "whisperx")
        self.assertEqual(
            completed_region.decision.timing_source_engine,
            "crisperwhisper",
        )

    def test_each_engine_only_passage_can_be_included_explicitly(self):
        cases = (
            (
                comparison.WHISPERX_ONLY,
                [
                    self.fixture.segment("alpha", 0.0, 0.2),
                    self.fixture.segment("whisper addition", 1.0, 1.4),
                    self.fixture.segment("omega", 2.0, 2.2),
                ],
                [
                    self.fixture.segment("alpha", 0.0, 0.2),
                    self.fixture.segment("omega", 2.0, 2.2),
                ],
                "whisper addition",
            ),
            (
                comparison.CRISPERWHISPER_ONLY,
                [
                    self.fixture.segment("alpha", 0.0, 0.2),
                    self.fixture.segment("omega", 2.0, 2.2),
                ],
                [
                    self.fixture.segment("alpha", 0.0, 0.2),
                    self.fixture.segment("crisper addition", 1.0, 1.4),
                    self.fixture.segment("omega", 2.0, 2.2),
                ],
                "crisper addition",
            ),
        )
        for classification, whisper, crisper, expected in cases:
            with self.subTest(classification=classification):
                result = self.fixture.compare(whisper, crisper)
                plan = fusion.create_fusion_plan(result)
                region = next(
                    item
                    for item in plan.regions
                    if item.classification == classification
                )
                decided = fusion.apply_decision(
                    plan,
                    region.region_id,
                    fusion.INCLUDE_ENGINE_ONLY,
                )
                self.assertTrue(decided.ready_for_final_generation)
                self.assertIn(expected, fusion.build_combined_preview(decided).text)

    def test_provisional_preview_exposes_unresolved_regions(self):
        result = self.fixture.compare(
            [self.fixture.segment("alpha omitted words omega", 1.0, 2.2)],
            [self.fixture.segment("alpha omega", 1.0, 1.6)],
        )
        plan = fusion.create_fusion_plan(result)
        with self.assertRaises(fusion.FusionNotReadyError):
            fusion.build_combined_preview(plan)
        preview = fusion.build_combined_preview(plan, provisional=True)
        self.assertTrue(preview.provisional)
        self.assertFalse(preview.final_ready)
        self.assertTrue(preview.unresolved_region_ids)
        self.assertIn("omitted", preview.text)

    def test_omit_engine_only_can_make_plan_final_ready(self):
        result = self.fixture.compare(
            [self.fixture.segment("alpha extra words omega", 1.0, 2.2)],
            [self.fixture.segment("alpha omega", 1.0, 1.6)],
        )
        plan = fusion.create_fusion_plan(result)
        target = next(
            region
            for region in plan.regions
            if region.classification == comparison.WHISPERX_ONLY
        )
        decided = fusion.apply_decision(
            plan,
            target.region_id,
            fusion.OMIT_ENGINE_ONLY,
        )
        self.assertTrue(decided.ready_for_final_generation)
        preview = fusion.build_combined_preview(decided)
        self.assertNotIn("extra", preview.text)

    def test_unsafe_nonmonotonic_timing_decision_is_rejected(self):
        result = self.fixture.compare(
            [
                self.fixture.segment("stable opening", 10.0, 11.0),
                self.fixture.segment("matching shifted words", 20.0, 21.0),
            ],
            [
                self.fixture.segment("stable opening", 10.0, 11.0),
                self.fixture.segment("matching shifted words", 5.0, 6.0),
            ],
        )
        plan = fusion.create_fusion_plan(result)
        target = next(
            region
            for region in plan.regions
            if region.classification == comparison.TIMING_CONFLICT
        )
        with self.assertRaisesRegex(
            fusion.FusionValidationError,
            "nonmonotonic|overlap",
        ):
            fusion.apply_decision(
                plan,
                target.region_id,
                fusion.USE_CRISPERWHISPER_TIMING,
            )

    def test_overlapping_duplicate_insertion_is_rejected(self):
        result = self.agreement_comparison()
        duplicate_region = replace(
            result.regions[0],
            classification=comparison.POSSIBLE_DUPLICATE,
        )
        plan = fusion.create_fusion_plan(replace(result, regions=(duplicate_region,)))
        with self.assertRaisesRegex(fusion.FusionValidationError, "duplicate"):
            fusion.apply_decision(
                plan,
                plan.regions[0].region_id,
                fusion.KEEP_BOTH,
            )

    def test_final_preview_enforces_known_source_timeline_bound(self):
        plan = fusion.create_fusion_plan(self.agreement_comparison())
        region = plan.regions[0]
        candidate = region.candidates[0]
        invalid_word = replace(
            candidate.words[-1],
            end=plan.source_duration_bound + 1.0,
        )
        invalid_candidate = replace(
            candidate,
            words=candidate.words[:-1] + (invalid_word,),
            end=invalid_word.end,
        )
        invalid_plan = replace(
            plan,
            regions=(replace(region, candidates=(invalid_candidate,)),),
        )
        with self.assertRaisesRegex(fusion.FusionValidationError, "timeline bound"):
            fusion.build_combined_preview(invalid_plan)

    def test_missing_word_timestamps_are_preserved(self):
        result = self.fixture.compare(
            [self.fixture.segment("untimed Unicode café", None)],
            [self.fixture.segment("untimed Unicode café", None)],
        )
        plan = fusion.create_fusion_plan(result)
        preview = fusion.build_combined_preview(plan)
        self.assertTrue(all(word.start is None and word.end is None for word in preview.words))
        self.assertIn("café", preview.text)

    def test_missing_source_media_still_allows_planning(self):
        whisper = self.fixture.descriptor(
            "whisperx",
            [self.fixture.segment("transcript only", 1.0, 1.6)],
        )
        crisper = self.fixture.descriptor(
            "crisperwhisper",
            [self.fixture.segment("transcript only", 1.0, 1.6)],
        )
        self.fixture.source.unlink()
        result = comparison.compare_results(whisper, crisper)
        self.assertEqual(result.whisperx.revision.source_state, "missing")
        self.assertTrue(fusion.create_fusion_plan(result).ready_for_final_generation)

    def test_incomplete_crisper_metadata_is_retained_as_warning(self):
        result = self.agreement_comparison()
        coverage = result_catalog.ResultCoverage(
            coverage_complete=False,
            remaining_speech_active_gap_count=1,
            remaining_speech_active_gap_duration=12.5,
            remaining_speech_active_gap_ranges=(
                {"start": 20.0, "end": 32.5},
            ),
            selected_strategy="targeted_recovery",
        )
        crisper_revision = replace(
            result.crisperwhisper.revision,
            status="incomplete",
            coverage=coverage,
        )
        altered = replace(
            result,
            crisperwhisper=replace(
                result.crisperwhisper,
                revision=crisper_revision,
            ),
        )
        plan = fusion.create_fusion_plan(altered)
        self.assertEqual(
            plan.pair_identity.crisperwhisper_revision.status,
            "incomplete",
        )
        self.assertEqual(len(plan.warnings), 2)
        self.assertTrue(any("Incomplete" in warning for warning in plan.warnings))

    def test_unicode_punctuation_speakers_and_names_remain_exact(self):
        transcript = "Beyoncé said “don’t”—naïve résumé 中文"
        result = self.fixture.compare(
            [self.fixture.segment(transcript, 1.0, 3.0, speaker="SPEAKER_01")],
            [self.fixture.segment(transcript, 1.0, 3.0, speaker="SPEAKER_01")],
            names={"SPEAKER_01": "Zoë"},
        )
        preview = fusion.build_combined_preview(fusion.create_fusion_plan(result))
        self.assertEqual(preview.text, transcript)
        self.assertEqual(tuple(word.text for word in preview.words), tuple(transcript.split()))
        self.assertTrue(all(word.speaker_id == "SPEAKER_01" for word in preview.words))
        self.assertTrue(all(word.speaker_name == "Zoë" for word in preview.words))
        self.assertTrue(
            all(
                provenance.original_text == word.text
                for word in preview.words
                for provenance in word.provenance
            )
        )

    def test_fusion_is_read_only_and_imports_no_engines_or_gui(self):
        result = self.agreement_comparison()
        manifest = self.root / "project.json"
        manifest.write_bytes(b'{"fixture_manifest":"unchanged"}\n')
        paths = (
            result.whisperx.revision.speakers_json,
            result.whisperx.revision.segments_json,
            result.crisperwhisper.revision.speakers_json,
            result.crisperwhisper.revision.segments_json,
            manifest,
        )
        before_files = set(self.root.rglob("*"))
        before_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }
        plan = fusion.create_fusion_plan(result)
        fusion.build_combined_preview(plan)
        after_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }
        self.assertEqual(before_hashes, after_hashes)
        self.assertEqual(before_files, set(self.root.rglob("*")))

        source = Path(fusion.__file__).read_text(encoding="utf-8")
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(
            imports.isdisjoint(
                {"tkinter", "ttkbootstrap", "vlc", "torch", "whisperx", "crisperwhisper"}
            )
        )


class OldGreggFusionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = FusionFixture(self.root)

    def test_old_gregg_plan_and_explicit_final_preview(self):
        shared_song = "do you love me are you playing your love games with me"
        later_unique_lyrics = "beneath the water I will wait"
        nice_spread = "Well, you've put on a nice spread"
        later_dialogue = "What do you think of me"
        result = self.fixture.compare(
            [
                self.fixture.segment(shared_song, 150.0, 154.0),
                self.fixture.segment(nice_spread, 394.33, 395.59),
                self.fixture.segment(later_dialogue, 425.0, 427.0),
            ],
            [
                self.fixture.segment(shared_song, 150.0, 154.0),
                self.fixture.segment(shared_song, 360.0, 364.0),
                self.fixture.segment(later_unique_lyrics, 365.0, 367.0),
                self.fixture.segment(nice_spread, 417.78, 419.04),
                self.fixture.segment(later_dialogue, 425.0, 427.0),
            ],
        )
        original_classifications = tuple(
            region.classification for region in result.regions
        )
        plan = fusion.create_fusion_plan(result)
        provisional = fusion.build_combined_preview(plan, provisional=True)

        self.assertEqual(provisional.text.count(shared_song), 1)
        self.assertNotIn(later_unique_lyrics, provisional.text)
        self.assertTrue(provisional.unresolved_region_ids)
        initial = next(
            region
            for region in plan.regions
            if region.classification == comparison.AGREEMENT
            and shared_song in region.candidates[0].text
        )
        self.assertEqual(len(initial.candidates), 1)
        self.assertEqual(initial.candidates[0].supporting_engines, ("whisperx", "crisperwhisper"))

        repeated = next(
            region
            for region in plan.regions
            if region.classification == comparison.POSSIBLE_DUPLICATE
            and any(shared_song in candidate.text for candidate in region.candidates)
        )
        lyrics = next(
            region
            for region in plan.regions
            if region.classification == comparison.CRISPERWHISPER_ONLY
            and any(later_unique_lyrics in candidate.text for candidate in region.candidates)
        )
        timing = next(
            region
            for region in plan.regions
            if region.classification == comparison.TIMING_CONFLICT
            and any(nice_spread in candidate.text for candidate in region.candidates)
        )
        self.assertFalse(repeated.resolved)
        self.assertFalse(lyrics.resolved)
        self.assertFalse(timing.resolved)
        self.assertEqual(
            tuple(candidate.start for candidate in timing.candidates),
            (394.33, 417.78),
        )

        decided = fusion.apply_decision(
            plan,
            repeated.region_id,
            fusion.OMIT_BOTH,
        )
        decided = fusion.apply_decision(
            decided,
            lyrics.region_id,
            fusion.INCLUDE_ENGINE_ONLY,
        )
        decided = fusion.apply_decision(
            decided,
            timing.region_id,
            fusion.USE_CRISPERWHISPER_TIMING,
        )
        self.assertTrue(decided.ready_for_final_generation)
        preview = fusion.build_combined_preview(decided)
        self.assertEqual(preview.text.count(shared_song), 1)
        self.assertEqual(preview.text.count(later_unique_lyrics), 1)
        self.assertEqual(preview.text.count(nice_spread), 1)
        nice_word = next(word for word in preview.words if word.text == "Well,")
        self.assertEqual(nice_word.start, 417.78)
        starts = [word.start for word in preview.words if word.start is not None]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(word.provenance for word in preview.words))

        reset = fusion.reset_decision(decided, timing.region_id)
        reset = fusion.reset_decision(reset, lyrics.region_id)
        reset = fusion.reset_decision(reset, repeated.region_id)
        self.assertFalse(reset.ready_for_final_generation)
        self.assertEqual(reset.summary.unresolved_region_count, 3)
        self.assertEqual(
            tuple(region.classification for region in result.regions),
            original_classifications,
        )


if __name__ == "__main__":
    unittest.main()
