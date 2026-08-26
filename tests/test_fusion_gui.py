from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import result_comparison as comparison
import result_fusion as fusion
import split_audio_gui as gui


SOURCE_IDENTITY = tuple(
    sorted(
        {
            "path": "C:/synthetic/old-gregg.wav",
            "size": 42,
            "mtime_ns": 123,
            "st_dev": 7,
            "st_ino": 9,
        }.items()
    )
)


class FusionGuiFixture:
    def __init__(self):
        self.ordinal = {"whisperx": 0, "crisperwhisper": 0}
        self.names = {
            "whisperx": {
                "SPEAKER_00": "Howard",
                "SPEAKER_01": "Old Gregg",
                "SPEAKER_02": "Vince",
            },
            "crisperwhisper": {
                "SPEAKER_00": "Someone Else",
                "SPEAKER_01": "Howard",
                "SPEAKER_03": "Old Gregg",
            },
        }

    @staticmethod
    def normalize(token):
        return token.casefold().strip(".,!?\"'")

    def words(self, engine, text, start, end, speaker):
        tokens = text.split()
        step = (end - start) / max(1, len(tokens))
        result = []
        for index, token in enumerate(tokens):
            ordinal = self.ordinal[engine]
            self.ordinal[engine] += 1
            word_start = start + index * step
            result.append(
                comparison.WordReference(
                    engine=engine,
                    ordinal=ordinal,
                    segment_index=ordinal,
                    word_index=0,
                    text=token,
                    normalized=self.normalize(token),
                    start=word_start,
                    end=word_start + step,
                    segment_start=start,
                    segment_end=end,
                    segment_text=text,
                    speaker_id=speaker,
                    speaker_name=self.names[engine].get(speaker),
                )
            )
        return tuple(result)

    def region(
        self,
        region_id,
        classification,
        *,
        whisper_text="",
        crisper_text="",
        whisper_time=(None, None),
        crisper_time=(None, None),
        whisper_speaker="SPEAKER_00",
        crisper_speaker="SPEAKER_00",
    ):
        whisper_words = (
            self.words(
                "whisperx",
                whisper_text,
                whisper_time[0],
                whisper_time[1],
                whisper_speaker,
            )
            if whisper_text
            else ()
        )
        crisper_words = (
            self.words(
                "crisperwhisper",
                crisper_text,
                crisper_time[0],
                crisper_time[1],
                crisper_speaker,
            )
            if crisper_text
            else ()
        )
        starts = [
            value
            for value in (whisper_time[0], crisper_time[0])
            if value is not None
        ]
        ends = [
            value
            for value in (whisper_time[1], crisper_time[1])
            if value is not None
        ]
        whisper_display = tuple(
            filter(
                None,
                (
                    f"{whisper_speaker} ({self.names['whisperx'].get(whisper_speaker)})"
                    if whisper_words
                    else None,
                ),
            )
        )
        crisper_display = tuple(
            filter(
                None,
                (
                    f"{crisper_speaker} ({self.names['crisperwhisper'].get(crisper_speaker)})"
                    if crisper_words
                    else None,
                ),
            )
        )
        return comparison.ComparisonRegion(
            region_id=region_id,
            classification=classification,
            whisperx_words=whisper_words,
            crisperwhisper_words=crisper_words,
            whisperx_text=whisper_text,
            crisperwhisper_text=crisper_text,
            whisperx_start=whisper_time[0],
            whisperx_end=whisper_time[1],
            crisperwhisper_start=crisper_time[0],
            crisperwhisper_end=crisper_time[1],
            start=min(starts) if starts else None,
            end=max(ends) if ends else None,
            whisperx_speakers=whisper_display,
            crisperwhisper_speakers=crisper_display,
            speaker_disagreement=bool(
                whisper_display and crisper_display and whisper_display != crisper_display
            ),
            maximum_timing_delta=(
                abs(whisper_time[0] - crisper_time[0])
                if whisper_time[0] is not None and crisper_time[0] is not None
                else None
            ),
            explanation=f"Synthetic {classification} evidence.",
        )

    @staticmethod
    def revision(engine, *, duration=None, status="complete"):
        return comparison.RevisionMetadata(
            engine=engine,
            result_id=f"20260823-{engine}",
            project_id="old-gregg-project",
            display_title="HD Old Gregg",
            model="large-v3" if engine == "whisperx" else "large",
            mode=None if engine == "whisperx" else "verbatim",
            execution_backend="ctranslate2" if engine == "whisperx" else "transformers",
            status=status,
            created_at=datetime(2026, 8, 23, 20, 26, tzinfo=timezone.utc),
            modified_at=None,
            comparison_job_id="both:old-gregg",
            coverage=None,
            source_identity=SOURCE_IDENTITY,
            source_state="available",
            speakers_json=Path(f"{engine}-speakers.json"),
            segments_json=Path(f"{engine}-segments.json"),
            speakers_sha256=("a" if engine == "whisperx" else "c") * 64,
            segments_sha256=("b" if engine == "whisperx" else "d") * 64,
            authoritative_duration=duration,
            duration_source="segments.transcription.duration" if duration else None,
        )

    def comparison(self, regions, *, whisper_duration=None, crisper_duration=500.0):
        whisper_revision = self.revision("whisperx", duration=whisper_duration)
        crisper_revision = self.revision(
            "crisperwhisper",
            duration=crisper_duration,
        )
        whisper_words = tuple(word for region in regions for word in region.whisperx_words)
        crisper_words = tuple(word for region in regions for word in region.crisperwhisper_words)
        material = json.dumps(
            {
                "whisperx": [
                    whisper_revision.result_id,
                    whisper_revision.speakers_sha256,
                    whisper_revision.segments_sha256,
                ],
                "crisperwhisper": [
                    crisper_revision.result_id,
                    crisper_revision.speakers_sha256,
                    crisper_revision.segments_sha256,
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        summary = comparison.ComparisonSummary(
            whisperx_word_count=len(whisper_words),
            crisperwhisper_word_count=len(crisper_words),
            matched_normalized_words=0,
            whisperx_unique_words=0,
            crisperwhisper_unique_words=0,
            agreement_percentage=0.0,
            discrepancy_region_count=sum(
                item.classification != comparison.AGREEMENT for item in regions
            ),
            discrepancy_duration=0.0,
            timing_conflict_count=sum(
                item.classification == comparison.TIMING_CONFLICT for item in regions
            ),
            possible_duplicate_count=sum(
                item.classification == comparison.POSSIBLE_DUPLICATE for item in regions
            ),
            unresolved_count=sum(
                item.classification == comparison.UNRESOLVED for item in regions
            ),
        )
        return comparison.ResultComparison(
            pair_id=hashlib.sha256(material).hexdigest(),
            whisperx=comparison.TranscriptSnapshot(whisper_revision, whisper_words),
            crisperwhisper=comparison.TranscriptSnapshot(crisper_revision, crisper_words),
            summary=summary,
            regions=tuple(regions),
        )

    def standard(self):
        return self.comparison(
            (
                self.region(
                    "agreement",
                    comparison.AGREEMENT,
                    whisper_text="Hello there",
                    crisper_text="Hello there",
                    whisper_time=(0.0, 0.8),
                    crisper_time=(0.02, 0.82),
                    crisper_speaker="SPEAKER_01",
                ),
                self.region(
                    "whisper-only",
                    comparison.WHISPERX_ONLY,
                    whisper_text="Whisper passage",
                    whisper_time=(2.0, 2.5),
                ),
                self.region(
                    "crisper-only",
                    comparison.CRISPERWHISPER_ONLY,
                    crisper_text="Crisper passage",
                    crisper_time=(3.0, 3.5),
                    crisper_speaker="SPEAKER_00",
                ),
                self.region(
                    "text-conflict",
                    comparison.TEXT_CONFLICT,
                    whisper_text="one wording",
                    crisper_text="other wording",
                    whisper_time=(4.0, 4.6),
                    crisper_time=(4.05, 4.65),
                ),
                self.region(
                    "timing",
                    comparison.TIMING_CONFLICT,
                    whisper_text="same words",
                    crisper_text="same words",
                    whisper_time=(6.0, 6.6),
                    crisper_time=(8.0, 8.6),
                    whisper_speaker="SPEAKER_01",
                    crisper_speaker="SPEAKER_03",
                ),
                self.region(
                    "duplicate",
                    comparison.POSSIBLE_DUPLICATE,
                    whisper_text="repeat this",
                    crisper_text="repeat this",
                    whisper_time=(10.0, 10.5),
                    crisper_time=(10.05, 10.55),
                ),
                self.region(
                    "unresolved",
                    comparison.UNRESOLVED,
                    whisper_text="uncertain ending",
                    whisper_time=(12.0, 12.5),
                ),
            )
        )


class FusionDraftSessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FusionGuiFixture()
        self.comparison = self.fixture.standard()
        self.session = gui._FusionDraftSession(self.comparison)

    def test_context_actions_and_agreement_protection(self):
        self.assertEqual(self.session.valid_actions("agreement"), ())
        self.assertEqual(
            set(self.session.valid_actions("timing")),
            {fusion.USE_WHISPERX_TIMING, fusion.USE_CRISPERWHISPER_TIMING},
        )
        self.assertNotIn(
            fusion.KEEP_BOTH,
            self.session.valid_actions("text-conflict"),
        )
        self.assertEqual(
            set(self.session.valid_actions("whisper-only")),
            {fusion.INCLUDE_ENGINE_ONLY, fusion.OMIT_ENGINE_ONLY},
        )
        with self.assertRaises(fusion.FusionDecisionError):
            self.session.apply("agreement", fusion.KEEP_BOTH)

    def test_text_conflict_exposes_independent_wording_and_timing_actions(self):
        supported = self.session.supported_actions("text-conflict")
        self.assertIn(fusion.USE_WHISPERX_TEXT, supported)
        self.assertIn(fusion.USE_CRISPERWHISPER_TEXT, supported)
        self.assertIn(fusion.USE_WHISPERX_TIMING, supported)
        self.assertIn(fusion.USE_CRISPERWHISPER_TIMING, supported)
        self.assertEqual(
            gui._fusion_action_label(
                fusion.USE_CRISPERWHISPER_TEXT,
                comparison.TEXT_CONFLICT,
            ),
            "Use CrisperWhisper wording",
        )

        self.session.apply("text-conflict", fusion.USE_CRISPERWHISPER_TEXT)
        region = self.session.region("text-conflict")
        self.assertEqual(
            gui._fusion_decision_label(region),
            "Text: CrisperWhisper | Timing: WhisperX",
        )
        self.session.apply("text-conflict", fusion.USE_CRISPERWHISPER_TIMING)
        region = self.session.region("text-conflict")
        self.assertEqual(region.decision.text_source_engine, "crisperwhisper")
        self.assertEqual(region.decision.timing_source_engine, "crisperwhisper")

    def test_engine_only_actions_remain_visible_independent_of_safety_filtering(self):
        expected = (fusion.INCLUDE_ENGINE_ONLY, fusion.OMIT_ENGINE_ONLY)
        self.assertEqual(self.session.supported_actions("whisper-only"), expected)
        self.assertEqual(self.session.supported_actions("crisper-only"), expected)
        self.assertEqual(
            tuple(action for action, _enabled, _reason in self.session.action_states("crisper-only")),
            expected,
        )

    def test_unique_name_mapping_keeps_crisper_include_available(self):
        fixture = FusionGuiFixture()
        result = fixture.comparison(
            (
                fixture.region(
                    "named-agreement",
                    comparison.AGREEMENT,
                    whisper_text="Hello Howard",
                    crisper_text="Hello Howard",
                    whisper_time=(0.0, 0.6),
                    crisper_time=(0.02, 0.62),
                    whisper_speaker="SPEAKER_01",
                    crisper_speaker="SPEAKER_03",
                ),
                fixture.region(
                    "named-crisper-only",
                    comparison.CRISPERWHISPER_ONLY,
                    crisper_text="Moon passage",
                    crisper_time=(1.0, 1.5),
                    crisper_speaker="SPEAKER_03",
                ),
            ),
            crisper_duration=3.0,
        )
        session = gui._FusionDraftSession(result)
        mapping = session.mapping_for("SPEAKER_03")
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.decision_source, "automatic_unique_name")
        states = {
            action: (enabled, reason)
            for action, enabled, reason in session.action_states("named-crisper-only")
        }
        self.assertEqual(states[fusion.INCLUDE_ENGINE_ONLY], (True, None))

    def test_keep_both_is_exposed_only_when_chronology_is_safe(self):
        fixture = FusionGuiFixture()
        result = fixture.comparison(
            (
                fixture.region(
                    "safe-conflict",
                    comparison.TEXT_CONFLICT,
                    whisper_text="first version",
                    crisper_text="second version",
                    whisper_time=(1.0, 1.5),
                    crisper_time=(1.6, 2.1),
                ),
            ),
            crisper_duration=5.0,
        )
        session = gui._FusionDraftSession(result)
        self.assertIn(fusion.KEEP_BOTH, session.valid_actions("safe-conflict"))
        session.apply("safe-conflict", fusion.KEEP_BOTH)
        session.map_to_existing("SPEAKER_00", "SPEAKER_00")
        preview = session.validate_final()
        self.assertEqual(len(preview.candidates), 2)

    def test_plan_replacement_undo_and_resets(self):
        original = self.session.plan
        self.session.apply("whisper-only", fusion.INCLUDE_ENGINE_ONLY)
        replacement = self.session.plan
        self.assertIsNot(replacement, original)
        self.assertIsNone(original.regions[1].decision)
        self.assertTrue(self.session.undo())
        self.assertIs(self.session.plan, original)

        self.session.apply("whisper-only", fusion.INCLUDE_ENGINE_ONLY)
        self.assertTrue(self.session.reset_selected("whisper-only"))
        self.assertIsNone(self.session.region("whisper-only").decision)
        self.session.apply("whisper-only", fusion.INCLUDE_ENGINE_ONLY)
        self.assertTrue(self.session.reset_all())
        self.assertEqual(self.session.plan, original)
        self.assertFalse(self.session.has_changes)

    def test_explicit_baseline_and_individual_override(self):
        actions = dict(self.session.resolve_whisperx_baseline())
        self.assertEqual(actions["text-conflict"], fusion.USE_WHISPERX)
        self.assertEqual(actions["timing"], fusion.USE_WHISPERX_TIMING)
        self.assertEqual(actions["whisper-only"], fusion.INCLUDE_ENGINE_ONLY)
        self.assertEqual(actions["crisper-only"], fusion.OMIT_ENGINE_ONLY)
        self.assertTrue(self.session.plan.ready_for_final_generation)
        history_length = len(self.session.plan.decision_history)
        self.assertEqual(history_length, 6)

        self.session.apply("crisper-only", fusion.INCLUDE_ENGINE_ONLY)
        self.assertEqual(
            self.session.region("crisper-only").decision.action,
            fusion.INCLUDE_ENGINE_ONLY,
        )
        self.assertEqual(self.session.unresolved_speaker_ids(), ("SPEAKER_00",))

    def test_speaker_mapping_policy_and_deterministic_new_speaker(self):
        # The same numeric ID has different names and is never mapped automatically.
        self.assertIsNone(self.session.mapping_for("SPEAKER_00"))
        # Same unique nonempty name maps regardless of its different raw ID.
        mapping = self.session.mapping_for("SPEAKER_01")
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.target_speaker_id, "SPEAKER_00")
        self.assertEqual(mapping.decision_source, "automatic_unique_name")

        created = self.session.create_and_map_speaker(
            "SPEAKER_00",
            "New Person",
        )
        self.assertEqual(created.speaker_id, "SPEAKER_02")
        self.assertTrue(created.created_for_combined)
        with self.assertRaises(fusion.FusionValidationError):
            second = gui._FusionDraftSession(self.comparison)
            second.create_and_map_speaker("SPEAKER_00", "")

    def test_included_crisper_text_requires_mapping_and_preserves_provenance(self):
        self.session.resolve_whisperx_baseline()
        self.session.apply("crisper-only", fusion.INCLUDE_ENGINE_ONLY)
        with self.assertRaisesRegex(fusion.FusionValidationError, "mapping"):
            self.session.validate_final()
        self.session.map_to_existing("SPEAKER_00", "SPEAKER_01")
        preview = self.session.validate_final()
        word = next(
            item
            for item in preview.words
            if item.text == "Crisper"
        )
        self.assertEqual(word.speaker_id, "SPEAKER_01")
        self.assertEqual(word.text_source_engine, "crisperwhisper")
        self.assertEqual(word.timing_source_engine, "crisperwhisper")
        self.assertEqual(word.speaker_source_engine, "crisperwhisper")
        self.assertEqual(word.speaker_decision_source, "explicit_user_mapping")
        self.assertTrue(word.provenance)

    def test_timing_and_region_speaker_sources_remain_separate(self):
        self.session.resolve_whisperx_baseline()
        self.session.apply("timing", fusion.USE_CRISPERWHISPER_TIMING)
        self.session.set_region_speaker(
            "timing",
            "SPEAKER_03",
            "SPEAKER_01",
        )
        preview = self.session.validate_final()
        word = next(item for item in preview.words if item.text == "same")
        self.assertEqual(word.text_source_engine, "whisperx")
        self.assertEqual(word.timing_source_engine, "crisperwhisper")
        self.assertEqual(word.speaker_id, "SPEAKER_01")
        self.assertEqual(word.speaker_decision_source, "explicit_region_speaker")

    def test_text_and_timing_choices_are_independent_for_real_style_conflict(self):
        fixture = FusionGuiFixture()
        result = fixture.comparison(
            (
                fixture.region(
                    "before-number",
                    comparison.AGREEMENT,
                    whisper_text="before",
                    crisper_text="before",
                    whisper_time=(7.4, 8.0),
                    crisper_time=(7.4, 8.0),
                ),
                fixture.region(
                    "number-conflict",
                    comparison.TEXT_CONFLICT,
                    whisper_text="73,000",
                    crisper_text="seventy three thousand",
                    whisper_time=(8.18, 8.76),
                    crisper_time=(7.95, 9.08),
                ),
                fixture.region(
                    "after-number",
                    comparison.AGREEMENT,
                    whisper_text="after",
                    crisper_text="after",
                    whisper_time=(8.9, 9.4),
                    crisper_time=(8.9, 9.4),
                ),
            ),
            crisper_duration=12.0,
        )
        session = gui._FusionDraftSession(result)
        states = {
            action: (enabled, reason)
            for action, enabled, reason in session.action_states("number-conflict")
        }
        self.assertTrue(states[fusion.USE_CRISPERWHISPER_TEXT][0])
        self.assertFalse(states[fusion.USE_CRISPERWHISPER_TIMING][0])
        self.assertIn("overlap", states[fusion.USE_CRISPERWHISPER_TIMING][1])

        session.apply("number-conflict", fusion.USE_CRISPERWHISPER_TEXT)
        decision = session.region("number-conflict").decision
        self.assertEqual(decision.text_source_engine, "crisperwhisper")
        self.assertEqual(decision.timing_source_engine, "whisperx")
        session.map_to_existing("SPEAKER_00", "SPEAKER_00")
        preview = session.validate_final()
        candidate = next(
            item
            for item in preview.candidates
            if item.region_id == "number-conflict"
        )
        self.assertEqual(candidate.text, "seventy three thousand")
        self.assertEqual((candidate.start, candidate.end), (8.18, 8.76))
        self.assertTrue(candidate.timing_is_derived)
        self.assertEqual(candidate.timing_transformation, "proportional_region_fit")
        self.assertTrue(session.final_ready)

        state_before_preview = session._state
        undo_before_preview = tuple(session._undo)
        presentation = session.preview_presentation()
        self.assertEqual(presentation.state, "final_ready")
        self.assertEqual(
            presentation.window_title,
            "Final-ready Combined Draft Preview",
        )
        self.assertEqual(
            presentation.banner,
            "Final-ready. This Combined Draft has not been saved yet.",
        )
        self.assertIn("seventy three thousand", presentation.clean_text)
        self.assertNotIn("73,000", presentation.clean_text)
        self.assertNotIn("[AUTOMATIC", presentation.clean_text)
        self.assertNotIn("[EXPLICIT", presentation.clean_text)
        self.assertNotIn("[UNRESOLVED", presentation.clean_text)
        self.assertNotIn("revision", presentation.clean_text.casefold())
        self.assertIn("seventy three thousand", presentation.audit_text)
        self.assertIn("Text: CrisperWhisper", presentation.audit_text)
        self.assertIn("Timing: WhisperX", presentation.audit_text)
        self.assertIn(
            "Timing status: Derived (proportional_region_fit)",
            presentation.audit_text,
        )
        self.assertIn("revision 20260823-crisperwhisper", presentation.audit_text)
        self.assertNotIn("[UNRESOLVED", presentation.audit_text)
        self.assertIs(presentation.preview.pair_identity, preview.pair_identity)
        self.assertEqual(presentation.preview.words, preview.words)
        self.assertIs(session._state, state_before_preview)
        self.assertEqual(tuple(session._undo), undo_before_preview)

        self.assertTrue(session.undo())
        self.assertFalse(session.final_ready)
        self.assertTrue(session.reset_selected("number-conflict"))
        self.assertIsNone(session.region("number-conflict").decision)
        session.resolve_whisperx_baseline()
        baseline_candidate = next(
            item
            for item in session.preview(provisional=False).candidates
            if item.region_id == "number-conflict"
        )
        self.assertEqual(baseline_candidate.text, "73,000")
        self.assertFalse(baseline_candidate.timing_is_derived)

    def test_provisional_markers_and_final_ready_validation(self):
        presentation = self.session.preview_presentation()
        self.assertEqual(presentation.state, "provisional")
        self.assertEqual(
            presentation.window_title,
            "Provisional Combined Draft Preview",
        )
        self.assertEqual(
            presentation.banner,
            "Provisional only. Unresolved regions are marked and this draft is not ready to save.",
        )
        self.assertNotIn("[UNRESOLVED:", presentation.clean_text)
        self.assertIn("[UNRESOLVED:", presentation.audit_text)
        with self.assertRaises(fusion.FusionNotReadyError):
            self.session.validate_final()
        self.session.resolve_whisperx_baseline()
        preview = self.session.validate_final()
        self.assertTrue(preview.final_ready)
        self.assertFalse(preview.provisional)
        final_presentation = self.session.preview_presentation()
        self.assertEqual(final_presentation.state, "final_ready")
        self.assertNotIn("Unresolved", final_presentation.banner)

    def test_duration_authority_is_distinct_from_transcript_bound(self):
        plan = self.session.plan
        self.assertEqual(plan.authoritative_media_duration, 500.0)
        self.assertIn("crisperwhisper", plan.authoritative_duration_source)
        self.assertLess(plan.known_transcript_bound, 500.0)

        fresh = FusionGuiFixture()
        result = fresh.standard()
        without_duration = replace(
            result,
            crisperwhisper=replace(
                result.crisperwhisper,
                revision=replace(
                    result.crisperwhisper.revision,
                    authoritative_duration=None,
                    duration_source=None,
                ),
            ),
        )
        plan = fusion.create_fusion_plan(without_duration)
        self.assertIsNone(plan.authoritative_media_duration)
        self.assertEqual(plan.source_duration_bound, plan.known_transcript_bound)

    def test_crisper_worker_duration_is_recognized_as_authoritative_metadata(self):
        descriptor = SimpleNamespace(layout="project", engine="crisperwhisper")
        preflight = SimpleNamespace(
            segments_data={"transcription": {"duration": 783.74}}
        )
        self.assertEqual(
            comparison._authoritative_duration(descriptor, preflight),
            (783.74, "segments.transcription.duration"),
        )

    def test_inconsistent_authoritative_durations_are_rejected(self):
        fresh = FusionGuiFixture()
        result = fresh.standard()
        altered = replace(
            result,
            whisperx=replace(
                result.whisperx,
                revision=replace(
                    result.whisperx.revision,
                    authoritative_duration=499.0,
                    duration_source="segments.media_duration",
                ),
            ),
        )
        with self.assertRaisesRegex(fusion.FusionIdentityError, "durations"):
            fusion.create_fusion_plan(altered)

    def test_two_close_authoritative_durations_use_a_conservative_shared_bound(self):
        fresh = FusionGuiFixture()
        result = fresh.standard()
        altered = replace(
            result,
            whisperx=replace(
                result.whisperx,
                revision=replace(
                    result.whisperx.revision,
                    authoritative_duration=499.9,
                    duration_source="segments.media_duration",
                ),
            ),
        )
        plan = fusion.create_fusion_plan(altered)
        self.assertAlmostEqual(plan.authoritative_media_duration, 499.9)
        self.assertEqual(
            plan.authoritative_duration_source,
            "WhisperX and CrisperWhisper metadata",
        )


class FusionDraftDialogContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FusionGuiFixture()
        self.comparison = self.fixture.standard()

    def test_exact_selected_pair_is_revalidated_before_in_page_fusion(self):
        whisper_descriptor = object()
        crisper_descriptor = object()
        compare_dialog = object.__new__(gui._ResultComparisonWorkspace)
        compare_dialog._selected_descriptors = lambda: (
            whisper_descriptor,
            crisper_descriptor,
        )
        compare_dialog._open_fusion_callback = mock.Mock(return_value=True)
        compare_dialog._report = mock.Mock()

        with mock.patch.object(
            gui,
            "compare_results",
            return_value=self.comparison,
        ) as compare_call, mock.patch.object(gui, "get_embedded_vlc_status") as vlc:
            compare_dialog._open_fusion_draft()
        compare_call.assert_called_once_with(whisper_descriptor, crisper_descriptor)
        compare_dialog._open_fusion_callback.assert_called_once_with(self.comparison)
        vlc.assert_not_called()

    def test_dual_preview_uses_exact_engine_times_and_existing_callback(self):
        region = self.comparison.regions[4]
        dialog = object.__new__(gui._FusionDraftWorkspace)
        dialog._preview_available = True
        dialog._preview_callback = mock.Mock(return_value=(True, None))
        dialog._current_region = lambda: SimpleNamespace(region_id=region.region_id)
        dialog._comparison_regions = {region.region_id: region}
        dialog.lbl_preview_status = mock.Mock()
        dialog._report = mock.Mock()

        self.assertEqual(dialog._preview_engine("whisperx"), "break")
        self.assertEqual(dialog._preview_engine("crisperwhisper"), "break")
        self.assertEqual(
            dialog._preview_callback.call_args_list,
            [mock.call(6.0), mock.call(8.0)],
        )

    def test_missing_media_disables_both_sides(self):
        region = self.comparison.regions[4]
        dialog = object.__new__(gui._FusionDraftWorkspace)
        dialog._preview_available = False
        dialog._preview_callback = mock.Mock()
        self.assertFalse(dialog._can_preview(region, "whisperx"))
        self.assertFalse(dialog._can_preview(region, "crisperwhisper"))

    def test_back_to_compare_preserves_in_memory_draft(self):
        dialog = object.__new__(gui._FusionDraftWorkspace)
        dialog.session = SimpleNamespace(has_changes=True)
        dialog._back_to_compare_callback = mock.Mock()
        dialog.destroy = mock.Mock()
        with mock.patch.object(gui.messagebox, "askyesno") as ask:
            self.assertEqual(dialog._close(), "break")
        ask.assert_not_called()
        dialog._back_to_compare_callback.assert_called_once_with()
        dialog.destroy.assert_not_called()

    def test_model_operations_do_not_write_or_create_a_player(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "unchanged.json"
            sentinel.write_bytes(b'{"unchanged": true}\n')
            before = sentinel.read_bytes()
            with mock.patch.object(gui, "get_embedded_vlc_status") as vlc:
                session = gui._FusionDraftSession(self.comparison)
                session.resolve_whisperx_baseline()
                session.validate_final()
                session.preview_presentation()
            self.assertEqual(sentinel.read_bytes(), before)
            vlc.assert_not_called()


class OptionalFusionDraftLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = gui.tb.Window(themename="litera")
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        gui.register_midnightstudio_theme(self.root)
        self.root.geometry("1366x768+0+0")
        self.root.update()
        fixture = FusionGuiFixture()
        regions = tuple(
            fixture.region(
                f"region-{index}",
                comparison.UNRESOLVED,
                whisper_text=f"Whisper passage {index}",
                whisper_time=(index * 2.0, index * 2.0 + 0.6),
            )
            for index in range(102)
        )
        self.dialog = gui._FusionDraftWorkspace(
            self.root,
            fixture.comparison(regions, crisper_duration=240.0),
        )
        self.dialog.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self):
        dialog = getattr(self, "dialog", None)
        root = getattr(self, "root", None)
        try:
            if dialog is not None and dialog.winfo_exists():
                dialog.destroy()
        except gui.tk.TclError:
            pass
        try:
            if root is not None and root.winfo_exists():
                root.destroy()
        except gui.tk.TclError:
            pass

    def _show_region(self, region_id):
        item = next(
            item
            for item, region in self.dialog._row_regions.items()
            if region.region_id == region_id
        )
        self.dialog.tree.selection_set(item)
        self.dialog.tree.focus(item)
        self.dialog._selection_changed()

    def test_normal_sizes_keep_eight_rows_text_and_footer_reachable(self):
        for geometry in ("1366x768+0+0", "1480x930+0+0", "1920x1080+0+0"):
            with self.subTest(geometry=geometry):
                self.root.geometry(geometry)
                self.root.update()
                eighth = self.dialog.tree.bbox(self.dialog.tree.get_children()[7])
                self.assertTrue(eighth)
                self.assertLessEqual(eighth[1] + eighth[3], self.dialog.tree.winfo_height())
                self.assertGreaterEqual(self.dialog.txt_whisper.winfo_height(), 50)
                self.assertGreaterEqual(self.dialog.txt_crisper.winfo_height(), 50)
                footer_bottom = (
                    self.dialog.btn_validate.winfo_rooty()
                    + self.dialog.btn_validate.winfo_height()
                )
                self.assertLessEqual(
                    footer_bottom,
                    self.root.winfo_rooty() + self.root.winfo_height(),
                )

    def test_main_pane_is_user_resizable_and_text_panes_scroll_independently(self):
        self.root.geometry("1480x930+0+0")
        self.root.update()
        original = self.dialog.main_paned.sash_coord(0)[1]
        self.dialog.main_paned.sash_place(0, 0, original + 50)
        self.dialog.update()
        self.assertGreater(self.dialog.main_paned.sash_coord(0)[1], original)
        self.assertTrue(self.dialog.txt_whisper.cget("yscrollcommand"))
        self.assertTrue(self.dialog.txt_crisper.cget("yscrollcommand"))


    def test_provisional_preview_has_usable_text_area_while_unresolved(self):
        self.assertEqual(str(self.dialog.btn_provisional.cget("state")), "normal")
        self.assertEqual(
            str(self.dialog.btn_provisional.cget("text")),
            "Preview Combined Draft",
        )
        self.assertEqual(str(self.dialog.btn_validate.cget("state")), "disabled")
        presentation, error = self.dialog._combined_preview_payload()
        self.assertIsNone(error)
        self.assertEqual(presentation.state, "provisional")
        self.assertIn("[UNRESOLVED:", presentation.audit_text)
        preview = gui._FusionPreviewDialog(self.dialog, presentation)
        try:
            preview.grab_release()
            preview.update()
            state_before_switch = self.dialog.session._state
            undo_before_switch = tuple(self.dialog.session._undo)
            self.assertEqual(
                preview.title(),
                "Provisional Combined Draft Preview",
            )
            self.assertEqual(preview.notebook.index("current"), 0)
            self.assertGreaterEqual(preview.clean_text.winfo_height(), 300)
            preview.notebook.select(1)
            preview.update()
            self.assertGreaterEqual(preview.audit_text.winfo_height(), 300)
            self.assertIs(self.dialog.session._state, state_before_switch)
            self.assertEqual(tuple(self.dialog.session._undo), undo_before_switch)
        finally:
            preview.destroy()

    def test_final_ready_preview_uses_final_title_banner_and_clean_default(self):
        self.dialog.session.resolve_whisperx_baseline()
        presentation, error = self.dialog._combined_preview_payload()
        self.assertIsNone(error)
        self.assertEqual(presentation.state, "final_ready")
        self.assertEqual(
            presentation.banner,
            "Final-ready. This Combined Draft has not been saved yet.",
        )
        preview = gui._FusionPreviewDialog(self.dialog, presentation)
        try:
            preview.grab_release()
            preview.update()
            self.assertEqual(preview.title(), "Final-ready Combined Draft Preview")
            self.assertEqual(preview.notebook.index("current"), 0)
            self.assertNotIn("[UNRESOLVED", preview.clean_text.get("1.0", "end"))
        finally:
            preview.destroy()


class OptionalFusionDecisionActionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = gui.tb.Window(themename="litera")
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        gui.register_midnightstudio_theme(self.root)
        self.root.geometry("1100x760+0+0")
        self.root.update()
        self.fixture = FusionGuiFixture()
        self.dialog = gui._FusionDraftWorkspace(self.root, self.fixture.standard())
        self.dialog.pack(fill="both", expand=True)
        self.dialog.var_filter.set("All")
        self.dialog.update()

    def tearDown(self):
        try:
            if self.dialog.winfo_exists():
                self.dialog.destroy()
        except (AttributeError, gui.tk.TclError):
            pass
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except (AttributeError, gui.tk.TclError):
            pass

    def _show_region(self, region_id):
        item = next(
            item
            for item, region in self.dialog._row_regions.items()
            if region.region_id == region_id
        )
        self.dialog.tree.selection_set(item)
        self.dialog.tree.focus(item)
        self.dialog._selection_changed()

    def _buttons(self):
        return {
            str(button.cget("text")): str(button.cget("state"))
            for button in self.dialog._action_buttons
        }

    def test_engine_only_rows_show_include_and_omit_and_decisions_show_reset(self):
        self._show_region("crisper-only")
        buttons = self._buttons()
        self.assertEqual(buttons["Include CrisperWhisper passage"], "normal")
        self.assertEqual(buttons["Omit passage"], "normal")

        self._show_region("whisper-only")
        buttons = self._buttons()
        self.assertEqual(buttons["Include WhisperX passage"], "normal")
        self.assertEqual(buttons["Omit passage"], "normal")
        self.dialog.session.apply("whisper-only", fusion.INCLUDE_ENGINE_ONLY)
        self.dialog._refresh_all(selected_region_id="whisper-only")
        self.assertIn("Reset to unresolved", self._buttons())

    def test_mapping_requirement_does_not_hide_crisper_include(self):
        self._show_region("crisper-only")
        self.assertIn("Include CrisperWhisper passage", self._buttons())
        self.assertIn("Mapping required", str(self.dialog.lbl_mapping.cget("text")))

    def test_text_conflict_shows_separate_wording_and_timing_controls(self):
        self._show_region("text-conflict")
        buttons = self._buttons()
        self.assertIn("Use WhisperX wording", buttons)
        self.assertIn("Use CrisperWhisper wording", buttons)
        self.assertIn("Use WhisperX timing", buttons)
        self.assertIn("Use CrisperWhisper timing", buttons)

        self.dialog.session.apply(
            "text-conflict",
            fusion.USE_CRISPERWHISPER_TEXT,
        )
        self.dialog._refresh_all(selected_region_id="text-conflict")
        detail = str(self.dialog.lbl_detail.cget("text"))
        self.assertIn("Text: CrisperWhisper", detail)
        self.assertIn("Timing: WhisperX", detail)


class IntegratedReviewModeContractTests(unittest.TestCase):
    def test_compare_and_fusion_are_frames_without_modal_or_loan_code(self):
        self.assertTrue(issubclass(gui._ResultComparisonWorkspace, gui.ttk.Frame))
        self.assertTrue(issubclass(gui._FusionDraftWorkspace, gui.ttk.Frame))
        self.assertFalse(issubclass(gui._ResultComparisonWorkspace, gui.tk.Toplevel))
        self.assertFalse(issubclass(gui._FusionDraftWorkspace, gui.tk.Toplevel))
        self.assertFalse(hasattr(gui.NamingWorkspace, "loan_embedded_player"))
        self.assertFalse(hasattr(gui.NamingWorkspace, "return_embedded_player"))
        self.assertNotIn(
            "grab_set",
            inspect.getsource(gui._ResultComparisonWorkspace),
        )
        self.assertNotIn(
            "grab_set",
            inspect.getsource(gui._FusionDraftWorkspace),
        )

    def test_mode_switching_never_changes_player_surface_or_polling_loops(self):
        class Paned:
            def __init__(self):
                self.position = 400

            @staticmethod
            def winfo_width():
                return 1200

            @staticmethod
            def panes():
                return ("left", "right")

            def sashpos(self, _index, position=None):
                if position is not None:
                    self.position = position
                return self.position

        workspace = object.__new__(gui.NamingWorkspace)
        workspace._review_mode_widgets = tuple(mock.Mock() for _ in range(4))
        workspace._embedded_mode_frame = None
        workspace._embedded_mode = "review"
        workspace._mode_left_ratio = 0.64
        workspace._workspace_left_ratio = 0.40
        workspace._workspace_paned = Paned()
        workspace.after_idle = lambda callback: callback()
        workspace._vlc_player = object()
        workspace.video_surface = object()
        workspace._video_update_after = object()
        workspace._word_sync_after = object()
        player = workspace._vlc_player
        surface = workspace.video_surface
        video_loop = workspace._video_update_after
        word_loop = workspace._word_sync_after
        compare_view = mock.Mock()
        fusion_view = mock.Mock()

        workspace.show_embedded_mode(compare_view, "compare")
        workspace.show_embedded_mode(fusion_view, "fusion")
        workspace.show_review_mode()

        self.assertIs(workspace._vlc_player, player)
        self.assertIs(workspace.video_surface, surface)
        self.assertIs(workspace._video_update_after, video_loop)
        self.assertIs(workspace._word_sync_after, word_loop)
        self.assertEqual(workspace._workspace_paned.position, 480)

    def test_navigation_preserves_player_and_in_memory_fusion_state(self):
        player = object()
        comparison_view = mock.Mock()
        fusion_view = mock.Mock()
        fusion_view.session = SimpleNamespace(has_changes=True)
        workspace = mock.Mock()
        workspace._vlc_player = player

        page = object.__new__(gui.ReviewNamePage)
        page.workspace = workspace
        page._comparison_workspace = comparison_view
        page._fusion_workspace = fusion_view
        page._mode = "review"
        page._update_mode_navigation = mock.Mock()

        self.assertTrue(page.show_comparison_mode())
        workspace.show_embedded_mode.assert_called_with(comparison_view, "compare")
        self.assertTrue(page.show_review_mode())
        workspace.show_review_mode.assert_called_once_with()
        self.assertIs(workspace._vlc_player, player)
        self.assertTrue(fusion_view.session.has_changes)

    def test_fusion_back_callbacks_do_not_destroy_state(self):
        view = object.__new__(gui._FusionDraftWorkspace)
        view.session = SimpleNamespace(has_changes=True)
        view._back_to_compare_callback = mock.Mock()
        view._back_to_review_callback = mock.Mock()
        view.destroy = mock.Mock()

        self.assertEqual(view._close(), "break")
        view._back_to_compare_callback.assert_called_once_with()
        self.assertEqual(view._back_to_review(), "break")
        view._back_to_review_callback.assert_called_once_with()
        view.destroy.assert_not_called()

    def test_engine_previews_use_exact_times_without_replacing_review(self):
        fixture = FusionGuiFixture()
        result = fixture.standard()
        region = result.regions[4]
        player = object()
        descriptor = object()
        previewed = []
        view = object.__new__(gui._FusionDraftWorkspace)
        view._preview_available = True
        view._preview_callback = lambda seconds: (previewed.append(seconds) or True, None)
        view._current_region = lambda: SimpleNamespace(region_id=region.region_id)
        view._comparison_regions = {region.region_id: region}
        view.lbl_preview_status = mock.Mock()
        view.tree = mock.Mock()
        view.tree.selection.return_value = ("selected",)
        view._report = mock.Mock()

        view._preview_engine("whisperx")
        view._preview_engine("crisperwhisper")

        self.assertEqual(previewed, [6.0, 8.0])
        self.assertIs(player, player)
        self.assertIs(descriptor, descriptor)

    def test_result_replacement_confirmation_keeps_fusion_when_cancelled(self):
        page = object.__new__(gui.ReviewNamePage)
        fusion_view = mock.Mock()
        fusion_view.session.has_changes = True
        page._fusion_workspace = fusion_view
        page.winfo_toplevel = lambda: None
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            self.assertFalse(page._confirm_fusion_discard_for_replacement())
        self.assertIs(page._fusion_workspace, fusion_view)

class OldGreggCombinedWorkflowTests(unittest.TestCase):
    def test_user_reviewed_workflow_is_chronological_and_nonduplicated(self):
        fixture = FusionGuiFixture()
        result = fixture.comparison(
            (
                fixture.region(
                    "opening",
                    comparison.AGREEMENT,
                    whisper_text="Do you love me",
                    crisper_text="Do you love me",
                    whisper_time=(1.0, 2.0),
                    crisper_time=(1.02, 2.02),
                ),
                fixture.region(
                    "later-lyrics",
                    comparison.CRISPERWHISPER_ONLY,
                    crisper_text="I need your love tonight",
                    crisper_time=(300.0, 302.0),
                    crisper_speaker="SPEAKER_03",
                ),
                fixture.region(
                    "repeated-lyrics",
                    comparison.POSSIBLE_DUPLICATE,
                    whisper_text="love games",
                    crisper_text="love games",
                    whisper_time=(310.0, 311.0),
                    crisper_time=(310.05, 311.05),
                ),
                fixture.region(
                    "nice-spread",
                    comparison.TIMING_CONFLICT,
                    whisper_text="Well, you've put on a nice spread.",
                    crisper_text="Well, you've put on a nice spread.",
                    whisper_time=(394.33, 395.59),
                    crisper_time=(417.78, 419.04),
                    whisper_speaker="SPEAKER_01",
                    crisper_speaker="SPEAKER_03",
                ),
                fixture.region(
                    "later-dialogue",
                    comparison.AGREEMENT,
                    whisper_text="What do you want",
                    crisper_text="What do you want",
                    whisper_time=(430.0, 431.0),
                    crisper_time=(430.02, 431.02),
                ),
            ),
            crisper_duration=783.74,
        )
        session = gui._FusionDraftSession(result)
        session.resolve_whisperx_baseline()
        session.apply("later-lyrics", fusion.INCLUDE_ENGINE_ONLY)
        session.apply("repeated-lyrics", fusion.OMIT_BOTH)
        session.apply("nice-spread", fusion.USE_CRISPERWHISPER_TIMING)
        session.map_to_existing("SPEAKER_03", "SPEAKER_01")
        session.set_region_speaker(
            "nice-spread",
            "SPEAKER_03",
            "SPEAKER_01",
        )

        preview = session.validate_final()
        starts = [
            word.start for word in preview.words if word.start is not None
        ]
        self.assertEqual(starts, sorted(starts))
        self.assertIn("I need your love tonight", preview.text)
        self.assertNotIn("love games", preview.text)
        spread = next(word for word in preview.words if word.text.startswith("Well"))
        self.assertAlmostEqual(spread.start, 417.78)
        self.assertEqual(spread.text_source_engine, "whisperx")
        self.assertEqual(spread.timing_source_engine, "crisperwhisper")
        self.assertEqual(spread.speaker_decision_source, "explicit_region_speaker")
        self.assertEqual(len(spread.provenance), 2)


if __name__ == "__main__":
    unittest.main()
