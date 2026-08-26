from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import result_catalog
import result_comparison as comparison
import result_storage
import split_audio_gui as gui
from tests.test_result_catalog import ResultFixture, incomplete_coverage


class ComparisonFixture:
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source.wav"
        self.source.write_bytes(b"comparison source")
        self.identity = result_catalog.build_source_identity(self.source)

    @staticmethod
    def words(text, start, *, speaker="SPEAKER_00", step=0.4):
        values = []
        current = float(start)
        for token in text.split():
            values.append(
                {
                    "word": token,
                    "start": current,
                    "end": current + step,
                    "speaker": speaker,
                }
            )
            current += step
        return values

    def result(
        self,
        folder,
        engine,
        segments,
        *,
        title="Comparison fixture",
        names=None,
        source=None,
        source_identity=None,
    ):
        result_root = self.root / "output" / folder
        result_root.mkdir(parents=True)
        speakers = result_root / "speakers.json"
        segments_path = result_root / "segments.json"
        speakers.write_text(
            json.dumps(
                {
                    "title": title,
                    "speakers": ["SPEAKER_00", "SPEAKER_01"],
                    "names": names or {},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        data = {
            "title": title,
            "source_path": str((source or self.source).resolve()),
            "source_identity": source_identity or self.identity,
            "segments": segments,
        }
        if engine == "crisperwhisper":
            data["transcription"] = {
                "engine": "crisperwhisper",
                "model": {"family": "medium"},
                "effective_settings": {"transcription": {"mode": "verbatim"}},
                "execution_backend": "transformers",
            }
        segments_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return result_catalog.descriptor_from_json_pair(speakers, segments_path)

    def one_segment(self, text, start, *, speaker="SPEAKER_00"):
        words = self.words(text, start, speaker=speaker)
        return {
            "start": words[0]["start"],
            "end": words[-1]["end"],
            "text": text,
            "speaker": speaker,
            "words": words,
        }

    def bounded_segment(self, text, start, end, *, speaker="SPEAKER_00"):
        tokens = text.split()
        step = (float(end) - float(start)) / len(tokens)
        words = self.words(text, start, speaker=speaker, step=step)
        return {
            "start": float(start),
            "end": float(end),
            "text": text,
            "speaker": speaker,
            "words": words,
        }


class ResultComparisonCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = ComparisonFixture(self.root)

    def compare(self, whisper_segments, crisper_segments):
        whisper = self.fixture.result("whisper", "whisperx", whisper_segments)
        crisper = self.fixture.result(
            "crisper", "crisperwhisper", crisper_segments
        )
        return comparison.compare_results(whisper, crisper)

    def classifications(self, result):
        return [region.classification for region in result.regions]

    def test_identical_words_ignore_cue_segmentation_punctuation_and_case(self):
        whisper = [
            self.fixture.one_segment("Hello, WORLD!", 0.0),
            self.fixture.one_segment("This is Greg.", 1.0),
        ]
        crisper = [self.fixture.one_segment("hello world this IS greg", 0.0)]
        result = self.compare(whisper, crisper)
        self.assertEqual(self.classifications(result), [comparison.AGREEMENT])
        self.assertEqual(result.summary.matched_normalized_words, 5)
        self.assertEqual(result.summary.agreement_percentage, 100.0)

    def test_verbatim_filler_and_each_engine_only_passage(self):
        whisper = [self.fixture.one_segment("well I think yes extra", 0.0)]
        crisper = [self.fixture.one_segment("um well I really think yes", 0.0)]
        result = self.compare(whisper, crisper)
        classes = self.classifications(result)
        self.assertIn(comparison.WHISPERX_ONLY, classes)
        self.assertIn(comparison.CRISPERWHISPER_ONLY, classes)
        self.assertNotIn(comparison.TEXT_CONFLICT, classes)

    def test_replace_region_is_text_conflict(self):
        whisper = [self.fixture.one_segment("the cat sleeps now", 0.0)]
        crisper = [self.fixture.one_segment("the dog waits now", 0.0)]
        result = self.compare(whisper, crisper)
        conflict = [
            region
            for region in result.regions
            if region.classification == comparison.TEXT_CONFLICT
        ]
        self.assertEqual(len(conflict), 1)
        self.assertIn("cat", conflict[0].whisperx_text)
        self.assertIn("dog", conflict[0].crisperwhisper_text)

    def test_substantial_matched_time_shift_is_timing_conflict(self):
        whisper = [self.fixture.one_segment("unique dialogue follows", 10.0)]
        crisper = [self.fixture.one_segment("unique dialogue follows", 14.0)]
        result = self.compare(whisper, crisper)
        self.assertEqual(self.classifications(result), [comparison.TIMING_CONFLICT])
        self.assertEqual(result.regions[0].whisperx_start, 10.0)
        self.assertEqual(result.regions[0].crisperwhisper_start, 14.0)
        self.assertGreater(
            result.regions[0].maximum_timing_delta,
            comparison.TIMING_CONFLICT_THRESHOLD_SECONDS,
        )

    def test_real_style_timing_conflict_keeps_distinct_engine_ranges(self):
        transcript = "Well, you've put on a nice spread"
        whisper_words = self.fixture.words(transcript, 394.33, step=0.18)
        crisper_words = self.fixture.words(transcript, 417.78, step=0.18)
        whisper = [
            {
                "start": whisper_words[0]["start"],
                "end": whisper_words[-1]["end"],
                "text": transcript,
                "speaker": "SPEAKER_00",
                "words": whisper_words,
            }
        ]
        crisper = [
            {
                "start": crisper_words[0]["start"],
                "end": crisper_words[-1]["end"],
                "text": transcript,
                "speaker": "SPEAKER_00",
                "words": crisper_words,
            }
        ]
        region = self.compare(whisper, crisper).regions[0]
        self.assertEqual(region.classification, comparison.TIMING_CONFLICT)
        self.assertAlmostEqual(region.whisperx_start, 394.33)
        self.assertAlmostEqual(region.crisperwhisper_start, 417.78)
        self.assertAlmostEqual(region.maximum_timing_delta, 23.45)
        self.assertNotEqual(region.whisperx_start, region.crisperwhisper_start)

    def test_repeated_passage_is_possible_duplicate_not_forced_alignment(self):
        whisper = [self.fixture.one_segment("sing the moon tonight ending", 0.0)]
        crisper = [
            self.fixture.one_segment("sing the moon tonight", 0.0),
            self.fixture.one_segment("sing the moon tonight", 10.0),
            self.fixture.one_segment("ending", 12.0),
        ]
        result = self.compare(whisper, crisper)
        duplicates = [
            region
            for region in result.regions
            if region.classification == comparison.POSSIBLE_DUPLICATE
        ]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0].crisperwhisper_text, "sing the moon tonight")

    def test_real_repeated_local_phrases_are_agreements(self):
        examples = (
            ("I've got something.", 3.56, 4.04, 3.46, 4.04),
            ("let me.", 438.68, 439.02, 438.60, 439.02),
            ("in my", 40.45, 40.67, 40.38, 40.58),
        )
        for index, (phrase, wx_start, wx_end, cw_start, cw_end) in enumerate(
            examples
        ):
            with self.subTest(phrase=phrase):
                whisper = [
                    self.fixture.bounded_segment(
                        phrase, wx_start, wx_end
                    ),
                    self.fixture.one_segment(f"unique bridge {index}", wx_end + 3.0),
                    self.fixture.bounded_segment(
                        phrase, wx_end + 10.0, wx_end + 10.5
                    ),
                ]
                crisper = [
                    self.fixture.bounded_segment(
                        phrase, cw_start, cw_end
                    ),
                    self.fixture.one_segment(f"unique bridge {index}", cw_end + 3.0),
                    self.fixture.bounded_segment(
                        phrase, cw_end + 10.0, cw_end + 10.5
                    ),
                ]
                whisper_descriptor = self.fixture.result(
                    f"whisper-real-repeat-{index}",
                    "whisperx",
                    whisper,
                )
                crisper_descriptor = self.fixture.result(
                    f"crisper-real-repeat-{index}",
                    "crisperwhisper",
                    crisper,
                )
                result = comparison.compare_results(
                    whisper_descriptor,
                    crisper_descriptor,
                )
                self.assertNotIn(
                    comparison.POSSIBLE_DUPLICATE,
                    self.classifications(result),
                )
                self.assertTrue(
                    all(
                        region.classification == comparison.AGREEMENT
                        for region in result.regions
                    )
                )
                self.assertEqual(
                    result.summary.matched_normalized_words,
                    result.summary.whisperx_word_count,
                )
                self.assertEqual(result.summary.whisperx_unique_words, 0)
                self.assertEqual(result.summary.crisperwhisper_unique_words, 0)
                self.assertEqual(result.summary.possible_duplicate_count, 0)
                self.assertEqual(result.summary.agreement_percentage, 100.0)

    def test_repeated_phrase_with_strong_local_matches_stays_monotonic(self):
        phrase = "same local phrase"
        whisper = [
            self.fixture.one_segment(phrase, 10.0),
            self.fixture.one_segment("unique middle words", 20.0),
            self.fixture.one_segment(phrase, 30.0),
        ]
        crisper = [
            self.fixture.one_segment(phrase, 10.1),
            self.fixture.one_segment("unique middle words", 20.1),
            self.fixture.one_segment(phrase, 30.1),
        ]
        result = self.compare(whisper, crisper)
        self.assertNotIn(comparison.POSSIBLE_DUPLICATE, self.classifications(result))
        self.assertTrue(
            all(region.classification == comparison.AGREEMENT for region in result.regions)
        )
        whisper_starts = [
            word.start
            for region in result.regions
            for word in region.whisperx_words
            if word.start is not None
        ]
        crisper_starts = [
            word.start
            for region in result.regions
            for word in region.crisperwhisper_words
            if word.start is not None
        ]
        self.assertEqual(whisper_starts, sorted(whisper_starts))
        self.assertEqual(crisper_starts, sorted(crisper_starts))
        self.assertEqual(len(whisper_starts), len(crisper_starts))
        for whisper_time, crisper_time in zip(whisper_starts, crisper_starts):
            self.assertAlmostEqual(crisper_time - whisper_time, 0.1)

    def test_overlapping_repeated_wording_with_large_delta_is_timing_conflict(self):
        phrase = "this is a deliberately long repeated phrase"
        whisper = [
            self.fixture.bounded_segment(phrase, 10.0, 20.0),
            self.fixture.bounded_segment(phrase, 40.0, 50.0),
        ]
        crisper = [
            self.fixture.bounded_segment(phrase, 13.0, 23.0),
            self.fixture.bounded_segment(phrase, 43.0, 53.0),
        ]
        result = self.compare(whisper, crisper)
        self.assertNotIn(comparison.POSSIBLE_DUPLICATE, self.classifications(result))
        self.assertTrue(
            all(
                region.classification == comparison.TIMING_CONFLICT
                for region in result.regions
            )
        )

    def test_speaker_difference_is_reported_only_on_aligned_text(self):
        whisper = [self.fixture.one_segment("same words", 0.0, speaker="SPEAKER_00")]
        crisper = [self.fixture.one_segment("same words", 0.0, speaker="SPEAKER_01")]
        result = self.compare(whisper, crisper)
        self.assertEqual(result.regions[0].classification, comparison.AGREEMENT)
        self.assertTrue(result.regions[0].speaker_disagreement)
        self.assertIn("speaker labels differ", result.regions[0].explanation.lower())

    def test_strict_source_identity_rejects_basename_only_match(self):
        other_dir = self.root / "elsewhere"
        other_dir.mkdir()
        other_source = other_dir / self.fixture.source.name
        other_source.write_bytes(b"different source")
        whisper = self.fixture.result(
            "strict-whisper",
            "whisperx",
            [self.fixture.one_segment("hello", 0.0)],
        )
        crisper = self.fixture.result(
            "strict-crisper",
            "crisperwhisper",
            [self.fixture.one_segment("hello", 0.0)],
            source=other_source,
            source_identity=result_catalog.build_source_identity(other_source),
        )
        self.assertFalse(comparison.strict_same_source(whisper, crisper))
        with self.assertRaisesRegex(comparison.ResultComparisonError, "strict source"):
            comparison.compare_results(whisper, crisper)

    def test_missing_source_still_compares_transcripts(self):
        whisper = self.fixture.result(
            "missing-whisper",
            "whisperx",
            [self.fixture.one_segment("hello", 0.0)],
        )
        crisper = self.fixture.result(
            "missing-crisper",
            "crisperwhisper",
            [self.fixture.one_segment("hello", 0.0)],
        )
        self.fixture.source.unlink()
        result = comparison.compare_results(whisper, crisper)
        self.assertEqual(result.summary.agreement_percentage, 100.0)
        self.assertEqual(result.whisperx.revision.source_state, "missing")
        self.assertEqual(result.crisperwhisper.revision.source_state, "missing")

    def test_comparison_is_deterministic_and_does_not_mutate_results(self):
        whisper = self.fixture.result(
            "immutable-whisper",
            "whisperx",
            [self.fixture.one_segment("hello one", 0.0)],
        )
        crisper = self.fixture.result(
            "immutable-crisper",
            "crisperwhisper",
            [self.fixture.one_segment("hello two", 0.0)],
        )
        paths = (*whisper.paths, *crisper.paths)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        first = comparison.compare_results(whisper, crisper)
        second = comparison.compare_results(whisper, crisper)
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_disappearing_malformed_or_changed_result_is_rejected(self):
        for failure in ("missing", "malformed", "changed"):
            with self.subTest(failure=failure):
                case_root = self.root / failure
                case_root.mkdir()
                fixture = ComparisonFixture(case_root)
                whisper = fixture.result(
                    "whisper",
                    "whisperx",
                    [fixture.one_segment("hello", 0.0)],
                )
                crisper = fixture.result(
                    "crisper",
                    "crisperwhisper",
                    [fixture.one_segment("hello", 0.0)],
                )
                if failure == "missing":
                    crisper.segments_json.unlink()
                elif failure == "malformed":
                    crisper.segments_json.write_text("{bad", encoding="utf-8")
                else:
                    data = json.loads(crisper.segments_json.read_text(encoding="utf-8"))
                    data["segments"][0]["text"] = "changed"
                    crisper.segments_json.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(comparison.ResultComparisonError):
                    comparison.compare_results(whisper, crisper)

    def test_old_gregg_later_song_is_not_mistaken_for_entire_song_omission(self):
        first_song = "do you love me are you playing your love games with me"
        dialogue = "well you've put on a nice spread"
        whisper = [
            self.fixture.one_segment(first_song, 150.0),
            self.fixture.one_segment(dialogue, 180.0),
        ]
        crisper = [
            self.fixture.one_segment(first_song, 150.0),
            self.fixture.one_segment(first_song, 182.0),
            self.fixture.one_segment("I need your love tonight", 187.0),
            self.fixture.one_segment(dialogue, 220.0),
        ]
        result = self.compare(whisper, crisper)
        self.assertEqual(result.regions[0].classification, comparison.AGREEMENT)
        self.assertIn(comparison.POSSIBLE_DUPLICATE, self.classifications(result))
        self.assertIn(comparison.CRISPERWHISPER_ONLY, self.classifications(result))
        self.assertEqual(result.regions[-1].classification, comparison.TIMING_CONFLICT)
        self.assertFalse(
            any(
                region.classification == comparison.WHISPERX_ONLY
                and first_song in region.whisperx_text.casefold()
                for region in result.regions
            )
        )


class ComparisonSelectionAndStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = ComparisonFixture(self.root)

    def _pair(self, suffix="pair"):
        whisper = self.fixture.result(
            f"{suffix}-whisper",
            "whisperx",
            [self.fixture.one_segment("hello", 0.0)],
        )
        crisper = self.fixture.result(
            f"{suffix}-crisper",
            "crisperwhisper",
            [self.fixture.one_segment("hello", 0.0)],
        )
        return whisper, crisper

    def test_current_revision_and_shared_group_are_preferred(self):
        whisper, first_crisper = self._pair("group")
        second_crisper = self.fixture.result(
            "group-second-crisper",
            "crisperwhisper",
            [self.fixture.one_segment("hello", 0.0)],
        )
        whisper = replace(whisper, comparison_job_id="both:shared")
        second_crisper = replace(second_crisper, comparison_job_id="both:shared")
        selected = comparison.choose_default_pair(
            whisper,
            (first_crisper, second_crisper, whisper),
        )
        self.assertIs(selected[0], whisper)
        self.assertIs(selected[1], second_crisper)

    def test_nearest_creation_time_is_fallback(self):
        import datetime

        whisper, first_crisper = self._pair("nearest")
        second_crisper = self.fixture.result(
            "nearest-second-crisper",
            "crisperwhisper",
            [self.fixture.one_segment("hello", 0.0)],
        )
        base = datetime.datetime(2026, 8, 23, tzinfo=datetime.timezone.utc)
        whisper = replace(whisper, created_at=base)
        first_crisper = replace(
            first_crisper, created_at=base + datetime.timedelta(seconds=60)
        )
        second_crisper = replace(
            second_crisper, created_at=base + datetime.timedelta(seconds=10)
        )
        self.assertIs(
            comparison.choose_default_pair(
                whisper, (whisper, first_crisper, second_crisper)
            )[1],
            second_crisper,
        )

    def test_complete_and_incomplete_project_results_compare(self):
        project_fixture_root = self.root / "project-fixture"
        project_fixture_root.mkdir()
        fixture = ResultFixture(project_fixture_root)
        whisper_paths = fixture.project_result(
            project_name="whisper-project--0123456789ab",
            engine="whisperx",
            revision="20260823T120000000000Z-abcdef12",
            model="large-v3",
            mode=None,
            execution_backend="ctranslate2",
        )
        crisper_paths = fixture.project_result(
            project_name="crisper-project--0123456789ab",
            engine="crisperwhisper",
            revision="20260823T120100000000Z-abcdef12",
            status="incomplete",
            coverage=incomplete_coverage(),
        )
        whisper = result_catalog.descriptor_from_json_pair(
            whisper_paths["speakers"], whisper_paths["segments"]
        )
        crisper = result_catalog.descriptor_from_json_pair(
            crisper_paths["speakers"], crisper_paths["segments"]
        )
        result = comparison.compare_results(whisper, crisper)
        self.assertEqual(result.whisperx.revision.status, "complete")
        self.assertEqual(result.crisperwhisper.revision.status, "incomplete")
        self.assertIsNotNone(result.crisperwhisper.revision.coverage)

    def test_result_revision_persists_optional_comparison_job_id(self):
        source = self.fixture.source
        layout = result_storage.project_layout_for_source(
            self.root / "committed-output", "Comparison", source
        )
        with result_storage.ResultRevision(layout, "whisperx") as revision:
            (revision.output_root / "speakers.json").write_text(
                json.dumps({"speakers": []}), encoding="utf-8"
            )
            (revision.output_root / "segments.json").write_text(
                json.dumps({"segments": []}), encoding="utf-8"
            )
            final = revision.commit(
                model="large-v3",
                mode=None,
                execution_backend="ctranslate2",
                comparison_job_id="both:fixture",
            )
        descriptor = result_catalog.descriptor_from_json_pair(
            final / "speakers.json", final / "segments.json"
        )
        self.assertEqual(descriptor.comparison_job_id, "both:fixture")


class ComparisonGuiContractTests(unittest.TestCase):
    @staticmethod
    def assert_generated_text_is_encoding_safe(value):
        for bad_character in ("\u00e2", "\u00c2", "\ufffd"):
            if bad_character in value:
                raise AssertionError(
                    f"UI-generated text contains mojibake character {bad_character!r}: "
                    f"{value!r}"
                )

    @staticmethod
    def comparison_region(**overrides):
        values = {
            "classification": comparison.TIMING_CONFLICT,
            "start": 394.33,
            "end": 419.04,
            "whisperx_start": 394.33,
            "whisperx_end": 395.59,
            "crisperwhisper_start": 417.78,
            "crisperwhisper_end": 419.04,
            "whisperx_text": "Well, you've put on a nice spread",
            "crisperwhisper_text": "Well, you've put on a nice spread",
            "whisperx_speakers": ("SPEAKER_00",),
            "crisperwhisper_speakers": ("SPEAKER_01",),
            "maximum_timing_delta": 23.45,
            "explanation": "Matching words have substantially different timestamps.",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_comparison_ui_metadata_and_truncation_use_ascii(self):
        descriptor = SimpleNamespace(
            model="medium",
            mode="verbatim",
            status="complete",
            created_at=None,
            modified_at=None,
            result_id="revision-1",
        )
        label = gui._ResultComparisonWorkspace._descriptor_label(descriptor)
        self.assertEqual(
            label,
            "medium | Verbatim | Complete | Time unknown | revision-1",
        )

        summary = SimpleNamespace(
            matched_normalized_words=12,
            agreement_percentage=75.0,
            whisperx_unique_words=2,
            crisperwhisper_unique_words=3,
            discrepancy_region_count=4,
            timing_conflict_count=1,
        )
        comparison_result = SimpleNamespace(
            summary=summary,
            crisperwhisper=SimpleNamespace(
                revision=SimpleNamespace(coverage=None)
            ),
        )
        summary_text = gui._ResultComparisonWorkspace._summary_text(comparison_result)
        self.assertIn("Matched words: 12 | Agreement: 75.0%", summary_text)

        region = self.comparison_region(
            classification=comparison.TEXT_CONFLICT,
            whisperx_start=12.0,
            whisperx_end=13.5,
            crisperwhisper_start=14.0,
            crisperwhisper_end=15.5,
            maximum_timing_delta=2.0,
            explanation="The aligned wording differs.",
        )
        detail_text = gui._ResultComparisonWorkspace._region_detail_text(region)
        self.assertIn(
            "WhisperX: 00:12.00-00:13.50 | "
            "CrisperWhisper: 00:14.00-00:15.50 | "
            "Maximum timing difference: 2.00s",
            detail_text,
        )

        truncated = gui._ResultComparisonWorkspace._short_text(
            "abcdefghijklmnopqrstuvwxyz", limit=10
        )
        self.assertEqual(truncated, "abcdefg...")
        for generated in (label, summary_text, detail_text, truncated):
            self.assert_generated_text_is_encoding_safe(generated)

    def test_table_and_detail_show_separate_engine_times(self):
        region = self.comparison_region()
        values = gui._ResultComparisonWorkspace._region_table_values(region)
        self.assertEqual(values[1], "06:34.33-06:35.59")
        self.assertEqual(values[2], "06:57.78-06:59.04")
        detail = gui._ResultComparisonWorkspace._region_detail_text(region)
        self.assertIn("WhisperX: 06:34.33-06:35.59", detail)
        self.assertIn("CrisperWhisper: 06:57.78-06:59.04", detail)
        self.assertIn("Maximum timing difference: 23.45s", detail)

        whisper_only = self.comparison_region(
            classification=comparison.WHISPERX_ONLY,
            crisperwhisper_start=None,
            crisperwhisper_end=None,
            crisperwhisper_text="",
        )
        self.assertEqual(
            gui._ResultComparisonWorkspace._region_table_values(whisper_only)[2],
            "-",
        )

    def test_unicode_transcript_text_passes_through_comparison_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ComparisonFixture(Path(temporary))
            transcript = "Beyoncé said “don’t”—naïve résumé 中文"
            whisper = fixture.result(
                "unicode-whisper",
                "whisperx",
                [fixture.one_segment(transcript, 0.0)],
            )
            crisper = fixture.result(
                "unicode-crisper",
                "crisperwhisper",
                [fixture.one_segment(transcript, 0.0)],
            )
            paths = (*whisper.paths, *crisper.paths)
            before = {path: path.read_bytes() for path in paths}

            result = comparison.compare_results(whisper, crisper)

            self.assertEqual(result.regions[0].whisperx_text, transcript)
            self.assertEqual(result.regions[0].crisperwhisper_text, transcript)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_compare_control_enables_only_for_compatible_counterpart(self):
        page = object.__new__(gui.ReviewNamePage)
        page.current_result_descriptor = mock.Mock(spec=result_catalog.ResultDescriptor)
        page.btn_compare_results = mock.Mock()
        page._comparison_available_callback = mock.Mock(return_value=True)
        page._refresh_comparison_availability()
        page.btn_compare_results.configure.assert_called_with(state="normal")

        page.btn_compare_results.reset_mock()
        page._comparison_available_callback.return_value = False
        page._refresh_comparison_availability()
        page.btn_compare_results.configure.assert_called_with(state="disabled")

    def test_dialog_model_filters_all_classifications(self):
        regions = tuple(
            SimpleNamespace(region_id=str(index), classification=classification)
            for index, classification in enumerate(
                (
                    comparison.AGREEMENT,
                    comparison.WHISPERX_ONLY,
                    comparison.CRISPERWHISPER_ONLY,
                    comparison.TEXT_CONFLICT,
                    comparison.TIMING_CONFLICT,
                    comparison.POSSIBLE_DUPLICATE,
                    comparison.UNRESOLVED,
                )
            )
        )
        model = gui._ComparisonDialogModel(SimpleNamespace(regions=regions))
        self.assertEqual(len(model.visible("All")), 7)
        self.assertEqual(len(model.visible("Engine-only")), 2)
        self.assertEqual(model.visible("Text conflicts")[0].classification, comparison.TEXT_CONFLICT)
        self.assertEqual(model.visible("Timing conflicts")[0].classification, comparison.TIMING_CONFLICT)
        self.assertEqual(model.visible("Possible duplicates")[0].classification, comparison.POSSIBLE_DUPLICATE)
        self.assertEqual(model.visible("Unresolved")[0].classification, comparison.UNRESOLVED)

    def test_preview_delegates_to_existing_workspace_without_player_creation(self):
        page = object.__new__(gui.ReviewNamePage)
        page.workspace = mock.Mock()
        existing_player = object()
        page.workspace._vlc_player = existing_player
        page.workspace.preview_comparison_time.return_value = (True, None)
        self.assertEqual(page.preview_comparison_time(12.5), (True, None))
        self.assertEqual(page.preview_comparison_time(23.45), (True, None))
        self.assertEqual(
            page.workspace.preview_comparison_time.call_args_list,
            [mock.call(12.5), mock.call(23.45)],
        )
        self.assertIs(page.workspace._vlc_player, existing_player)
        with mock.patch.object(gui, "get_embedded_vlc_status") as vlc:
            page.comparison_preview_status()
        vlc.assert_not_called()

    def test_missing_or_changed_source_disables_preview(self):
        workspace = object.__new__(gui.NamingWorkspace)
        workspace._source_media_for_playback = mock.Mock(
            return_value=(None, None, "Playback is disabled for safety.")
        )
        self.assertEqual(
            workspace.comparison_preview_status(),
            (False, "Playback is disabled for safety."),
        )

    def test_engine_preview_buttons_use_exact_immutable_region_times(self):
        region = self.comparison_region()
        dialog = object.__new__(gui._ResultComparisonWorkspace)
        dialog._preview_available = True
        dialog._preview_callback = mock.Mock(return_value=(True, None))
        dialog._current_region = lambda: region
        dialog.lbl_preview = mock.Mock()
        dialog._report = mock.Mock()

        self.assertEqual(dialog._preview_engine("whisperx"), "break")
        self.assertEqual(dialog._preview_engine("crisperwhisper"), "break")
        self.assertEqual(
            dialog._preview_callback.call_args_list,
            [mock.call(394.33), mock.call(417.78)],
        )
        self.assertNotEqual(
            dialog._preview_callback.call_args_list[0],
            dialog._preview_callback.call_args_list[1],
        )
        self.assertIn(
            "CrisperWhisper",
            dialog.lbl_preview.configure.call_args.kwargs["text"],
        )

    def test_preview_enablement_is_engine_specific_and_source_safe(self):
        dialog = object.__new__(gui._ResultComparisonWorkspace)
        dialog._preview_available = True
        dialog._preview_callback = mock.Mock(return_value=(True, None))
        both = self.comparison_region()
        whisper_only = self.comparison_region(
            crisperwhisper_start=None,
            crisperwhisper_end=None,
        )
        crisper_only = self.comparison_region(
            whisperx_start=None,
            whisperx_end=None,
        )
        self.assertTrue(dialog._can_preview(both, "whisperx"))
        self.assertTrue(dialog._can_preview(both, "crisperwhisper"))
        self.assertTrue(dialog._can_preview(whisper_only, "whisperx"))
        self.assertFalse(dialog._can_preview(whisper_only, "crisperwhisper"))
        self.assertFalse(dialog._can_preview(crisper_only, "whisperx"))
        self.assertTrue(dialog._can_preview(crisper_only, "crisperwhisper"))

        dialog._preview_available = False
        self.assertFalse(dialog._can_preview(both, "whisperx"))
        self.assertFalse(dialog._can_preview(both, "crisperwhisper"))

    def test_no_selection_disables_both_preview_buttons(self):
        dialog = object.__new__(gui._ResultComparisonWorkspace)
        dialog._current_region = lambda: None
        dialog.lbl_detail = mock.Mock()
        dialog.txt_whisper = mock.Mock()
        dialog.txt_crisper = mock.Mock()
        dialog.btn_preview_whisper = mock.Mock()
        dialog.btn_preview_crisper = mock.Mock()
        dialog._set_text = mock.Mock()
        dialog._selection_changed()
        dialog.btn_preview_whisper.configure.assert_called_with(state="disabled")
        dialog.btn_preview_crisper.configure.assert_called_with(state="disabled")

    def test_enter_and_double_click_do_not_choose_between_two_times(self):
        region = self.comparison_region()
        dialog = object.__new__(gui._ResultComparisonWorkspace)
        dialog._preview_available = True
        dialog._preview_callback = mock.Mock(return_value=(True, None))
        dialog._current_region = lambda: region
        dialog.lbl_preview = mock.Mock()
        dialog._report = mock.Mock()
        self.assertEqual(dialog._preview_unambiguous(), "break")
        dialog._preview_callback.assert_not_called()
        dialog.lbl_preview.configure.assert_called_with(
            text="Choose an explicitly labeled engine preview button."
        )

        tree = mock.Mock()
        tree.identify_row.return_value = "row"
        dialog.tree = tree
        self.assertEqual(dialog._double_click(SimpleNamespace(y=8)), "break")
        tree.selection_set.assert_called_with("row")
        tree.focus.assert_called_with("row")
        dialog._preview_callback.assert_not_called()

        one_side = self.comparison_region(
            crisperwhisper_start=None,
            crisperwhisper_end=None,
        )
        dialog._current_region = lambda: one_side
        self.assertEqual(dialog._preview_unambiguous(), "break")
        dialog._preview_callback.assert_called_once_with(394.33)

    def test_escape_returns_to_review_without_destroying_comparison_state(self):
        dialog = object.__new__(gui._ResultComparisonWorkspace)
        dialog._back_to_review_callback = mock.Mock()
        self.assertEqual(dialog._close(), "break")
        dialog._back_to_review_callback.assert_called_once_with()

    def test_compare_results_opens_the_in_page_review_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ComparisonFixture(Path(temporary))
            whisper = fixture.result(
                "whisper",
                "whisperx",
                [fixture.one_segment("hello", 0.0)],
            )
            crisper = fixture.result(
                "crisper",
                "crisperwhisper",
                [fixture.one_segment("hello", 0.0)],
            )

            app = object.__new__(gui.App)
            app.review_page = mock.Mock()
            app.review_page.current_result_descriptor = whisper
            app.review_page.current_result_identity = whisper.file_identity
            app.review_page.open_comparison.return_value = True
            app._comparison_descriptors = mock.Mock(
                return_value=(whisper, crisper)
            )
            app.log = mock.Mock()
            app.winfo_toplevel = lambda: None
            app.show_page = mock.Mock()
            with mock.patch.object(gui, "get_embedded_vlc_status") as vlc:
                self.assertTrue(app.on_compare_results())
            vlc.assert_not_called()
            app.review_page.open_comparison.assert_called_once_with(
                whisper,
                (whisper, crisper),
            )
            app.show_page.assert_called_once_with("review")


class OptionalIntegratedReviewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = gui.tb.Window(themename="litera")
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        gui.register_midnightstudio_theme(self.root)
        self.root.geometry("1366x768+0+0")
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ComparisonFixture(Path(self.temporary.name))
        whisper_segments = [
            self.fixture.one_segment(f"Whisper passage {index}", index * 4.0)
            for index in range(12)
        ]
        crisper_segments = [
            self.fixture.one_segment(
                f"Crisper passage {index}",
                index * 4.0 + 2.0,
            )
            for index in range(12)
        ]
        self.whisper = self.fixture.result(
            "whisper",
            "whisperx",
            whisper_segments,
        )
        self.crisper = self.fixture.result(
            "crisper",
            "crisperwhisper",
            crisper_segments,
        )
        self.page = gui.ReviewNamePage(
            self.root,
            open_latest_callback=lambda: None,
            open_result_browser_callback=lambda: None,
            back_to_transcribe_callback=lambda: None,
            compare_results_callback=lambda: None,
            comparison_available_callback=lambda _descriptor: True,
            report_callback=lambda _message: None,
        )
        self.page.pack(fill="both", expand=True)
        self.vlc_patch = mock.patch.object(
            gui,
            "get_embedded_vlc_status",
            return_value={"available": False, "reason": "Synthetic unavailable VLC."},
        )
        self.candidate_patch = mock.patch.object(
            gui,
            "_extract_candidates_from_text",
            return_value=[],
        )
        self.vlc_patch.start()
        self.candidate_patch.start()
        self.assertTrue(self.page.load_result(self.whisper))
        self.root.update()

    def tearDown(self):
        try:
            workspace = getattr(self.page, "workspace", None)
            if workspace is not None:
                self.page._reset_embedded_modes()
                workspace.shutdown(save_view_preferences=False)
                workspace.destroy()
        except (AttributeError, gui.tk.TclError):
            pass
        self.candidate_patch.stop()
        self.vlc_patch.stop()
        try:
            self.root.destroy()
        except gui.tk.TclError:
            pass
        self.temporary.cleanup()

    def test_review_compare_fusion_navigation_is_in_page_and_keeps_player_host(self):
        workspace = self.page.workspace
        player = object()
        video_loop = object()
        word_loop = object()
        workspace._vlc_player = player
        workspace._video_update_after = video_loop
        workspace._word_sync_after = word_loop
        surface_id = workspace.video_surface.winfo_id()
        dirty_before = workspace.has_unsaved_changes()
        descriptor_before = self.page.current_result_descriptor

        self.assertTrue(
            self.page.open_comparison(
                self.whisper,
                (self.whisper, self.crisper),
            )
        )
        comparison_view = self.page._comparison_workspace
        comparison_view._open_fusion_draft()
        fusion_view = self.page._fusion_workspace
        self.assertIsNotNone(fusion_view)

        for geometry in ("1366x768+0+0", "1920x1080+0+0"):
            self.root.geometry(geometry)
            self.root.update()
            workspace._apply_embedded_mode_sash()
            fusion_view._set_initial_pane_position()
            self.root.update()
            children = fusion_view.tree.get_children()
            self.assertGreaterEqual(len(children), 8)
            eighth = fusion_view.tree.bbox(children[7])
            self.assertTrue(eighth)
            self.assertLessEqual(
                eighth[1] + eighth[3],
                fusion_view.tree.winfo_height(),
            )
            self.assertGreaterEqual(workspace.video_surface.winfo_width(), 420)
            self.assertGreaterEqual(workspace.video_surface.winfo_height(), 150)

        try:
            self.root.state("zoomed")
            self.root.update()
            workspace._apply_embedded_mode_sash()
            fusion_view._set_initial_pane_position()
            self.root.update()
            eighth = fusion_view.tree.bbox(fusion_view.tree.get_children()[7])
            self.assertTrue(eighth)
            self.assertGreaterEqual(workspace.video_surface.winfo_width(), 420)
            self.assertGreaterEqual(workspace.video_surface.winfo_height(), 150)
        except gui.tk.TclError:
            pass

        session = fusion_view.session
        self.assertTrue(self.page.show_comparison_mode())
        self.assertTrue(self.page.show_review_mode())
        self.assertTrue(self.page.show_comparison_mode())
        self.assertTrue(self.page._open_fusion_workspace(session.comparison))
        self.assertIs(self.page._fusion_workspace.session, session)
        self.assertIs(workspace._vlc_player, player)
        self.assertIs(workspace._video_update_after, video_loop)
        self.assertIs(workspace._word_sync_after, word_loop)
        self.assertEqual(workspace.video_surface.winfo_id(), surface_id)
        self.assertIs(self.page.current_result_descriptor, descriptor_before)
        self.assertEqual(workspace.has_unsaved_changes(), dirty_before)
        self.assertFalse(
            any(
                isinstance(child, gui.tk.Toplevel)
                for child in self.root.winfo_children()
            )
        )
        workspace._vlc_player = None
        workspace._video_update_after = None
        workspace._word_sync_after = None


if __name__ == "__main__":
    unittest.main()
