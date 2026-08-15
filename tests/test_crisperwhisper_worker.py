from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np

import crisperwhisper_worker as worker


class FakeCuda:
    def __init__(self, available=True):
        self.available = available
        self.empty_cache_calls = 0
        self.ipc_collect_calls = 0

    def is_available(self):
        return self.available

    def get_device_name(self, index):
        assert index == 0
        return "Synthetic GPU"

    def empty_cache(self):
        self.empty_cache_calls += 1

    def ipc_collect(self):
        self.ipc_collect_calls += 1


class FakeModel:
    init_calls = []
    transcribe_calls = []
    init_error = None
    transcribe_error = None
    returned_backend = "transformers"
    returned_model_version = 2

    @classmethod
    def reset(cls):
        cls.init_calls = []
        cls.transcribe_calls = []
        cls.init_error = None
        cls.transcribe_error = None
        cls.returned_backend = "transformers"
        cls.returned_model_version = 2

    def __init__(self, model_id, **kwargs):
        type(self).init_calls.append((model_id, copy.deepcopy(kwargs)))
        if type(self).init_error is not None:
            raise type(self).init_error
        self.backend = type(self).returned_backend
        self.model_version = type(self).returned_model_version

    def transcribe(self, audio_path, **kwargs):
        type(self).transcribe_calls.append((audio_path, copy.deepcopy(kwargs)))
        if type(self).transcribe_error is not None:
            raise type(self).transcribe_error
        mode = kwargs["mode"]
        return SimpleNamespace(
            text="Hello world.",
            language=kwargs["language"],
            mode=mode,
            duration=2.5,
            processing_time=0.125,
            chunks=[
                SimpleNamespace(
                    chunk_idx=0,
                    start_sec=0.0,
                    end_sec=2.5,
                    text="Hello world.",
                    context=None,
                    is_last=True,
                    stitch_lcs_length=None,
                    stitch_lcs_words=None,
                )
            ],
            words=[
                SimpleNamespace(word="Hello", start=0.1, end=0.7),
                SimpleNamespace(word="world.", start=0.8, end=1.4),
            ],
        )


def make_request(audio_path: Path, *, family="medium", mode="verbatim"):
    return {
        "protocol_version": worker.PROTOCOL_VERSION,
        "operation": "transcribe",
        "job": {
            "audio_path": str(audio_path),
            "settings": {
                "model": {
                    "family": family,
                    "model_id": worker.OFFICIAL_MODEL_IDS[family],
                    "execution_backend": "transformers",
                    "compute_type": "float16",
                    "device": "cuda",
                    "device_index": 0,
                },
                "transcription": {
                    "language": "en",
                    "mode": mode,
                    "word_timestamps": True,
                    "hotwords": [],
                },
                "longform": {
                    "strategy": "continuation",
                    "chunk_duration": 30.0,
                    "stride": 26.0,
                    "context_words": 12,
                    "drop_words": 2,
                    "timestamp_aware_drop": True,
                },
                "decoding": {
                    "temperature_fallback": True,
                    "hallucination_mitigation": True,
                    "early_eot_recovery": True,
                    "max_new_tokens": 256,
                    "suppress_tokens": None,
                    "alignment_heads": None,
                },
                "speculative": {
                    "enabled": False,
                    "draft_model": None,
                    "mode": "strict",
                    "k": "auto",
                    "min_tokens": 0,
                    "max_tokens": 0,
                },
            },
        },
    }


def fake_runtime(*, cuda=True, crisper_version=worker.SUPPORTED_CRISPERWHISPER_VERSION):
    cuda_object = FakeCuda(cuda)
    torch_module = SimpleNamespace(
        cuda=cuda_object,
        version=SimpleNamespace(cuda="12.8"),
    )
    runtime = {
        "versions": {
            "crisperwhisper": crisper_version,
            "torch": "2.8.0+cu128",
            "transformers": "5.15.0",
            "ctranslate2": "4.8.1",
        },
        "cuda_available": cuda,
        "cuda_version": "12.8",
        "gpu_name": "Synthetic GPU" if cuda else None,
        "available_execution_backends": ["transformers", "ct2"],
    }
    crisper_module = SimpleNamespace(CrisperWhisperModel=FakeModel)
    return runtime, crisper_module, torch_module


def native_result(words, *, duration=60.0, mode="verbatim", chunks=None):
    return SimpleNamespace(
        text=" ".join(word[0] for word in words),
        language="en",
        mode=mode,
        duration=duration,
        processing_time=0.25,
        chunks=chunks,
        words=[
            SimpleNamespace(word=text, start=start, end=end)
            for text, start, end in words
        ],
    )


def native_chunk(index, start, end, text, *, is_last=False):
    return SimpleNamespace(
        chunk_idx=index,
        start_sec=start,
        end_sec=end,
        text=text,
        context=None,
        is_last=is_last,
        stitch_lcs_length=None,
        stitch_lcs_words=None,
    )


def diagnostic_coverage_audit(ranges, *, silence_count=0, silence_duration=0.0):
    active_ranges = [
        {
            "start": float(start),
            "end": float(end),
            "duration": round(float(end) - float(start), 3),
            "active_seconds": round(min(float(end) - float(start), 4.0), 3),
            "active_blocks": 2,
        }
        for start, end in ranges
    ]
    active_duration = round(
        sum(item["duration"] for item in active_ranges),
        3,
    )
    return {
        "substantial_gap_count": len(active_ranges) + silence_count,
        "substantial_gap_duration": round(active_duration + silence_duration, 3),
        "speech_active_gap_count": len(active_ranges),
        "speech_active_gap_duration": active_duration,
        "silence_gap_count": silence_count,
        "silence_gap_duration": float(silence_duration),
        "speech_active_gap_ranges": active_ranges,
        "active_frame_seconds": round(
            sum(item["active_seconds"] for item in active_ranges),
            3,
        ),
        "known_diagnostic_interval": {
            "start": 168.20,
            "end": 177.42,
            "covered": not any(start < 177.42 and end > 168.20 for start, end in ranges),
        },
        "configuration": {
            "substantial_gap_seconds": 5.0,
            "sample_rate": 16000,
            "frame_seconds": 0.03,
            "block_seconds": 1.0,
            "absolute_floor_dbfs": -50.0,
            "noise_percentile": 20.0,
            "reference_percentile": 90.0,
            "noise_margin_db": 10.0,
            "minimum_dynamic_range_db": 6.0,
            "minimum_active_seconds": 0.75,
            "minimum_active_blocks": 2,
            "minimum_block_active_ratio": 0.2,
            "minimum_zero_crossing_rate": 0.01,
            "maximum_zero_crossing_rate": 0.4,
            "effective_threshold_dbfs": -42.0,
            "measured_noise_floor_dbfs": -60.0,
            "measured_reference_dbfs": -24.0,
            "measured_dynamic_range_db": 36.0,
        },
        "_speech_active_gaps": copy.deepcopy(active_ranges),
    }


def synthetic_activity_audio(duration, intervals=()):
    audio = np.zeros(int(duration * worker.COVERAGE_SAMPLE_RATE), dtype=np.float32)
    for start, end in intervals:
        first = int(start * worker.COVERAGE_SAMPLE_RATE)
        last = min(len(audio), int(end * worker.COVERAGE_SAMPLE_RATE))
        timeline = np.arange(last - first, dtype=np.float32) / worker.COVERAGE_SAMPLE_RATE
        audio[first:last] = (
            0.08 * np.sin(2.0 * np.pi * 180.0 * timeline)
            + 0.03 * np.sin(2.0 * np.pi * 430.0 * timeline)
        )
    return audio


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        FakeModel.reset()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.audio = self.root / "sample audio.wav"
        self.audio.write_bytes(b"synthetic fixture")

    def validated_request(self, **kwargs):
        return worker.validate_transcribe_request(make_request(self.audio, **kwargs))


class ProbeTests(WorkerTestCase):
    def test_probe_success_reports_versions_models_and_capabilities(self):
        fake_torch = SimpleNamespace(
            cuda=FakeCuda(True),
            version=SimpleNamespace(cuda="12.8"),
        )
        fake_modules = {
            "crisperwhisper": SimpleNamespace(),
            "torch": fake_torch,
            "transformers": SimpleNamespace(),
            "ctranslate2": SimpleNamespace(),
        }
        versions = {
            "crisperwhisper": "2.0.2",
            "torch": "2.8.0+cu128",
            "transformers": "5.15.0",
            "ctranslate2": "4.8.1",
        }
        with mock.patch.object(worker, "_package_version", side_effect=versions.__getitem__), mock.patch.object(
            worker.importlib, "import_module", side_effect=fake_modules.__getitem__
        ):
            response = worker.build_probe_response()

        self.assertEqual(response["protocol_version"], 1)
        self.assertEqual(response["runtime"]["versions"], versions)
        self.assertTrue(response["runtime"]["cuda_available"])
        self.assertEqual(response["runtime"]["cuda_version"], "12.8")
        self.assertEqual(response["runtime"]["gpu_name"], "Synthetic GPU")
        self.assertEqual(response["runtime"]["available_execution_backends"], ["transformers", "ct2"])
        self.assertEqual(response["supported_model_families"], ["small", "medium", "large", "turbo"])
        self.assertIn("large_pro", response["official_model_families"])
        self.assertEqual(response["capabilities"]["word_timestamps"], "required")

    def test_probe_missing_package_is_a_clear_failure(self):
        with mock.patch.object(
            worker,
            "_package_version",
            side_effect=worker.DependencyError("Required package 'crisperwhisper' is not installed."),
        ):
            with self.assertRaisesRegex(worker.DependencyError, "crisperwhisper"):
                worker.build_probe_response()


class RequestValidationTests(WorkerTestCase):
    def test_each_standard_family_resolves_to_its_exact_official_id(self):
        for family in worker.SUPPORTED_MODEL_FAMILIES:
            with self.subTest(family=family):
                validated = worker.validate_transcribe_request(make_request(self.audio, family=family))
                self.assertEqual(
                    validated["job"]["settings"]["model"]["model_id"],
                    worker.OFFICIAL_MODEL_IDS[family],
                )

    def test_mismatched_or_custom_model_id_is_rejected(self):
        request = make_request(self.audio)
        request["job"]["settings"]["model"]["model_id"] = "someone/custom-model"
        with self.assertRaisesRegex(worker.UnsupportedSettingError, "custom model IDs"):
            worker.validate_transcribe_request(request)

    def test_missing_audio_is_rejected_without_echoing_its_path(self):
        request = make_request(self.root / "private missing.wav")
        with self.assertRaises(worker.RequestError) as caught:
            worker.validate_transcribe_request(request)
        self.assertNotIn("private missing", str(caught.exception))

    def test_malformed_request_json(self):
        path = self.root / "request.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(worker.RequestError, "malformed JSON"):
            worker.read_request(path)

    def test_wrong_protocol_and_unknown_fields_are_rejected(self):
        request = make_request(self.audio)
        request["protocol_version"] = 99
        with self.assertRaisesRegex(worker.RequestError, "protocol_version"):
            worker.validate_transcribe_request(request)
        request = make_request(self.audio)
        request["job"]["settings"]["typo"] = True
        with self.assertRaisesRegex(worker.RequestError, "unknown field"):
            worker.validate_transcribe_request(request)

    def test_all_stage_one_unsupported_options_are_rejected(self):
        cases = []

        request = make_request(self.audio)
        request["job"]["settings"]["model"].update(
            family="medium_pro", model_id=worker.OFFICIAL_MODEL_IDS["medium_pro"]
        )
        cases.append(("pro model", request))

        request = make_request(self.audio)
        request["job"]["settings"]["transcription"]["hotwords"] = ["private term"]
        cases.append(("hotwords", request))

        request = make_request(self.audio)
        request["job"]["settings"]["speculative"]["enabled"] = True
        cases.append(("speculative decoding", request))

        request = make_request(self.audio)
        request["job"]["settings"]["speculative"]["draft_model"] = "small"
        cases.append(("draft model", request))

        request = make_request(self.audio)
        request["job"]["settings"]["decoding"]["alignment_heads"] = [[1, 2]]
        cases.append(("alignment heads", request))

        request = make_request(self.audio)
        request["job"]["settings"]["decoding"]["suppress_tokens"] = [1]
        cases.append(("suppress tokens", request))

        request = make_request(self.audio)
        request["job"]["settings"]["model"]["execution_backend"] = "ct2"
        cases.append(("ct2 backend", request))

        request = make_request(self.audio)
        request["job"]["settings"]["transcription"]["word_timestamps"] = False
        cases.append(("disabled word timestamps", request))

        request = make_request(self.audio)
        request["job"]["settings"]["transcription"]["mode"] = "clean"
        cases.append(("unknown mode", request))

        request = make_request(self.audio)
        request["job"]["settings"]["longform"]["strategy"] = "chunked_lcs"
        cases.append(("other longform strategy", request))

        request = make_request(self.audio)
        request["job"]["settings"]["model"]["compute_type"] = "float32"
        cases.append(("other compute type", request))

        request = make_request(self.audio)
        request["job"]["settings"]["model"]["device"] = "cpu"
        cases.append(("CPU", request))

        request = make_request(self.audio)
        request["job"]["settings"]["speculative"]["mode"] = "semantic"
        cases.append(("speculative mode override", request))

        request = make_request(self.audio)
        request["job"]["settings"]["speculative"]["k"] = 4
        cases.append(("speculative K override", request))

        request = make_request(self.audio)
        request["job"]["settings"]["speculative"]["min_tokens"] = 1
        cases.append(("speculative token window", request))

        for label, request in cases:
            with self.subTest(option=label):
                with self.assertRaises(worker.UnsupportedSettingError):
                    worker.validate_transcribe_request(request)

    def test_invalid_longform_window_is_rejected(self):
        request = make_request(self.audio)
        request["job"]["settings"]["longform"]["stride"] = 31.0
        with self.assertRaisesRegex(worker.RequestError, "stride"):
            worker.validate_transcribe_request(request)


class TranscriptionTests(WorkerTestCase):
    def test_valid_medium_verbatim_request_and_exact_model_call(self):
        response = worker.transcribe_job(self.validated_request(), runtime_loader=fake_runtime)

        self.assertEqual(len(FakeModel.init_calls), 1)
        model_id, init_kwargs = FakeModel.init_calls[0]
        self.assertEqual(model_id, worker.OFFICIAL_MODEL_IDS["medium"])
        self.assertEqual(
            init_kwargs,
            {
                "backend": "transformers",
                "compute_type": "float16",
                "device": "cuda",
                "device_index": 0,
            },
        )
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["transcription"]["text"], "Hello world.")
        self.assertEqual(response["transcription"]["mode"], "verbatim")
        self.assertEqual(len(response["transcription"]["words"]), 2)
        self.assertEqual(response["transcription"]["words"][0]["start"], 0.1)
        self.assertEqual(response["transcription"]["chunks"][0]["text"], "Hello world.")
        requested = response["settings"]["requested"]
        effective = response["settings"]["effective"]
        self.assertEqual(requested["model"]["model_id"], worker.OFFICIAL_MODEL_IDS["medium"])
        self.assertEqual(effective["model"]["resolved_model_id"], worker.OFFICIAL_MODEL_IDS["medium"])
        self.assertEqual(response["settings"]["not_applied"][0]["reason"], "The media duration did not exceed 30 seconds.")
        self.assertEqual(FakeModel.transcribe_calls[0][1]["word_timestamps"], True)
        self.assertEqual(FakeModel.transcribe_calls[0][1]["longform_strategy"], "continuation")
        self.assertIsNone(FakeModel.transcribe_calls[0][1]["hotwords"])
        self.assertNotIn("longform_coverage", response["transcription"])

    def test_intended_mode_is_passed_through_and_recorded(self):
        response = worker.transcribe_job(
            self.validated_request(mode="intended"), runtime_loader=fake_runtime
        )
        self.assertEqual(FakeModel.transcribe_calls[0][1]["mode"], "intended")
        self.assertEqual(response["settings"]["requested"]["transcription"]["mode"], "intended")
        self.assertEqual(response["transcription"]["mode"], "intended")

    def test_unavailable_cuda_and_wrong_package_version_fail_before_model_load(self):
        with self.assertRaises(worker.DependencyError):
            worker.transcribe_job(
                self.validated_request(), runtime_loader=lambda: fake_runtime(cuda=False)
            )
        self.assertEqual(FakeModel.init_calls, [])

        with self.assertRaises(worker.DependencyError):
            worker.transcribe_job(
                self.validated_request(),
                runtime_loader=lambda: fake_runtime(crisper_version="2.0.3"),
            )
        self.assertEqual(FakeModel.init_calls, [])

    def test_model_and_transcription_failures_are_public_and_cleaned_up(self):
        FakeModel.init_error = OSError("private cache path")
        runtime = fake_runtime()
        with self.assertRaises(worker.ModelLoadError) as caught:
            worker.transcribe_job(self.validated_request(), runtime_loader=lambda: runtime)
        self.assertNotIn("private cache path", str(caught.exception))
        self.assertGreaterEqual(runtime[2].cuda.empty_cache_calls, 1)

        FakeModel.reset()
        FakeModel.transcribe_error = RuntimeError("private account detail")
        runtime = fake_runtime()
        with self.assertRaises(worker.TranscriptionError) as caught:
            worker.transcribe_job(self.validated_request(), runtime_loader=lambda: runtime)
        self.assertNotIn("private account detail", str(caught.exception))
        self.assertGreaterEqual(runtime[2].cuda.empty_cache_calls, 1)

    def test_cancelled_transcription_leaves_no_result(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")
        FakeModel.transcribe_error = KeyboardInterrupt()
        stdout = io.StringIO()
        with mock.patch.object(worker, "load_runtime", side_effect=fake_runtime), redirect_stdout(stdout):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )
        self.assertEqual(return_code, 130)
        self.assertFalse(output_path.exists())
        self.assertIn('"event":"error"', stdout.getvalue())
        self.assertIn('"code":"cancelled"', stdout.getvalue())

    def test_model_failure_preserves_existing_result_file(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        original = b"original complete result"
        output_path.write_bytes(original)
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")
        FakeModel.init_error = RuntimeError("load failed")
        with mock.patch.object(worker, "load_runtime", side_effect=fake_runtime), redirect_stdout(io.StringIO()):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )
        self.assertEqual(return_code, worker.ModelLoadError.exit_code)
        self.assertEqual(output_path.read_bytes(), original)

    def test_worker_preserves_a_decoded_middle_sentence_across_longform_chunks(self):
        phrase = "I am here because this sentence must survive."
        native_text = f"Before. {phrase} After."
        native_words = []
        for index, token in enumerate(native_text.split()):
            start = 150.0 + index * 4.0
            native_words.append(
                SimpleNamespace(word=token, start=start, end=start + 0.5)
            )
        native_chunks = [
            SimpleNamespace(
                chunk_idx=0,
                start_sec=130.0,
                end_sec=160.0,
                text="Before.",
                context=None,
                is_last=False,
                stitch_lcs_length=None,
                stitch_lcs_words=None,
            ),
            SimpleNamespace(
                chunk_idx=1,
                start_sec=156.0,
                end_sec=186.0,
                text=phrase,
                context="Before.",
                is_last=False,
                stitch_lcs_length=None,
                stitch_lcs_words=None,
            ),
            SimpleNamespace(
                chunk_idx=2,
                start_sec=182.0,
                end_sec=212.0,
                text="After.",
                context=phrase,
                is_last=True,
                stitch_lcs_length=None,
                stitch_lcs_words=None,
            ),
        ]
        native_result = SimpleNamespace(
            text=native_text,
            language="en",
            mode="verbatim",
            duration=220.0,
            processing_time=1.0,
            chunks=native_chunks,
            words=native_words,
        )

        with mock.patch.object(FakeModel, "transcribe", return_value=native_result):
            response = worker.transcribe_job(
                self.validated_request(), runtime_loader=fake_runtime
            )

        transcription = response["transcription"]
        self.assertEqual(transcription["text"], native_text)
        self.assertEqual(
            " ".join(chunk["text"] for chunk in transcription["chunks"]),
            native_text,
        )
        self.assertEqual(
            " ".join(word["word"] for word in transcription["words"]),
            native_text,
        )
        self.assertIn(phrase, transcription["text"])
        self.assertEqual(
            transcription["word_timestamp_repairs"]["repair_count"],
            0,
        )


class WordTimestampNormalizationTests(WorkerTestCase):
    @staticmethod
    def normalize(words, *, text=None, duration=2.0):
        transcript = text if text is not None else " ".join(item["word"] for item in words)
        return worker.normalize_word_timestamps(words, transcript, duration)

    def test_zero_duration_and_tiny_reversal_are_repaired_deterministically(self):
        normalized, metadata = self.normalize(
            [
                {"word": "zero", "start": 0.10, "end": 0.10},
                {"word": "reverse", "start": 0.50, "end": 0.48},
                {"word": "next", "start": 0.80, "end": 1.00},
            ]
        )

        self.assertEqual(normalized[0]["end"], 0.50)
        self.assertEqual(normalized[1]["end"], 0.80)
        self.assertEqual(metadata["categories"]["zero_duration"], 1)
        self.assertEqual(metadata["categories"]["tiny_end_before_start"], 1)
        self.assertEqual(metadata["repair_count"], 2)

    def test_small_overlap_and_media_boundary_rounding_are_clamped(self):
        normalized, metadata = self.normalize(
            [
                {"word": "first", "start": -0.05, "end": 0.60},
                {"word": "second", "start": 0.55, "end": 1.10},
                {"word": "last", "start": 1.20, "end": 2.05},
            ]
        )

        self.assertEqual(normalized[0]["start"], 0.0)
        self.assertEqual(normalized[0]["end"], 0.55)
        self.assertEqual(normalized[-1]["end"], 2.0)
        self.assertEqual(metadata["categories"]["clamped_to_media_start"], 1)
        self.assertEqual(metadata["categories"]["clamped_to_media_end"], 1)
        self.assertEqual(metadata["categories"]["adjacent_overlap"], 1)

    def test_missing_boundaries_use_neighboring_timed_words(self):
        normalized, metadata = self.normalize(
            [
                {"word": "first", "start": 0.10, "end": 0.40},
                {"word": "missing-start", "start": None, "end": 0.75},
                {"word": "missing-end", "start": 0.80, "end": None},
                {"word": "next", "start": 1.10, "end": 1.40},
            ]
        )

        self.assertEqual(normalized[1]["start"], 0.40)
        self.assertEqual(normalized[2]["end"], 1.10)
        self.assertEqual(metadata["categories"]["inferred_missing_start"], 1)
        self.assertEqual(metadata["categories"]["inferred_missing_end"], 1)

    def test_first_and_last_word_anomalies_have_bounded_repairs(self):
        normalized, metadata = self.normalize(
            [
                {"word": "first", "start": None, "end": 0.20},
                {"word": "middle", "start": 0.30, "end": 1.60},
                {"word": "last", "start": 2.0, "end": 2.0},
            ]
        )

        self.assertEqual(normalized[0]["start"], 0.0)
        self.assertEqual(normalized[-1]["end"], 2.0)
        self.assertAlmostEqual(normalized[-1]["start"], 1.98)
        self.assertAlmostEqual(
            normalized[-1]["end"] - normalized[-1]["start"],
            worker.WORD_MIN_REPAIR_DURATION_SECONDS,
        )
        self.assertEqual(metadata["categories"]["inferred_missing_start"], 1)
        self.assertEqual(metadata["categories"]["zero_duration"], 1)

    def test_punctuation_repeated_words_order_and_text_are_preserved(self):
        words = [
            {"word": "Wait,", "start": 0.10, "end": 0.10},
            {"word": "wait", "start": 0.30, "end": 0.50},
            {"word": "wait!", "start": 0.60, "end": 0.90},
        ]
        normalized, _metadata = self.normalize(words, text="Wait, wait wait!")
        self.assertEqual(
            [item["word"] for item in normalized],
            [item["word"] for item in words],
        )
        self.assertEqual([item["index"] for item in normalized], [0, 1, 2])
        self.assertTrue(
            all(
                current["end"] <= following["start"]
                for current, following in zip(normalized, normalized[1:])
            )
        )

    def test_unassignable_single_boundary_keeps_native_text_untimed(self):
        normalized, metadata = self.normalize(
            [
                {"word": "timed", "start": 0.10, "end": 0.50},
                {"word": "untimed", "start": 2.0, "end": None},
            ],
            text="timed untimed",
        )
        self.assertEqual([item["word"] for item in normalized], ["timed"])
        self.assertTrue(metadata["words_remained_untimed"])
        self.assertEqual(metadata["untimed_word_count"], 1)
        self.assertEqual(metadata["categories"]["retained_untimed_text"], 1)
        self.assertNotIn("timed untimed", " ".join(metadata["warnings"]))

    def test_nonfinite_wrong_type_gross_bounds_and_unrecoverable_sequences_are_fatal(self):
        fatal_cases = {
            "nan": [{"word": "bad", "start": float("nan"), "end": 0.5}],
            "infinity": [{"word": "bad", "start": 0.1, "end": float("inf")}],
            "wrong type": [{"word": "bad", "start": "0.1", "end": 0.5}],
            "missing both": [{"word": "bad", "start": None, "end": None}],
            "gross negative": [{"word": "bad", "start": -0.251, "end": 0.5}],
            "far beyond": [{"word": "bad", "start": 0.1, "end": 2.251}],
            "large reversal": [{"word": "bad", "start": 1.0, "end": 0.5}],
            "unrecoverable order": [
                {"word": "first", "start": 0.0, "end": 1.0},
                {"word": "second", "start": 0.40, "end": 0.60},
            ],
        }
        for label, words in fatal_cases.items():
            with self.subTest(label=label):
                with self.assertRaises(worker.TranscriptionError):
                    self.normalize(words)

    def test_unrecoverable_worker_result_leaves_no_partial_output(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")

        def malformed_result(_model, _audio_path, **kwargs):
            return SimpleNamespace(
                text="Broken timing.",
                language=kwargs["language"],
                mode=kwargs["mode"],
                duration=2.0,
                processing_time=0.1,
                chunks=[],
                words=[SimpleNamespace(word="Broken", start=1.0, end=0.1)],
            )

        with mock.patch.object(FakeModel, "transcribe", malformed_result), mock.patch.object(
            worker, "load_runtime", side_effect=fake_runtime
        ), redirect_stdout(io.StringIO()):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )
        self.assertEqual(return_code, worker.TranscriptionError.exit_code)
        self.assertFalse(output_path.exists())
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])


class TimestampDiagnosticTests(WorkerTestCase):
    def rejected_details(self, words, chunks):
        result = native_result(words, duration=60.0, chunks=chunks)
        with self.assertRaises(worker.TranscriptionError) as raised:
            worker._normalize_transcription_candidate(
                result,
                "verbatim",
                model_family="large",
                strategy="continuation",
            )
        self.assertIsInstance(raised.exception.details, dict)
        return raised.exception.details

    def test_overlap_within_one_chunk_is_classified_without_content(self):
        details = self.rejected_details(
            [("private-alpha", 28.0, 30.0), ("private-beta", 28.5, 29.5)],
            [native_chunk(11, 26.0, 56.0, "private-alpha private-beta")],
        )
        self.assertEqual(details["previous_native_chunk_index"], 11)
        self.assertEqual(details["current_native_chunk_index"], 11)
        self.assertFalse(details["crosses_native_chunk_boundary"])
        self.assertFalse(details["normalized_tokens_identical"])
        self.assertFalse(details["repeated_token_sequence"])
        self.assertNotIn("private", json.dumps(details))

    def test_distinct_overlap_across_continuation_chunks_is_classified(self):
        details = self.rejected_details(
            [("private-alpha", 27.0, 29.0), ("private-beta", 27.58, 28.5)],
            [
                native_chunk(11, 0.0, 30.0, "private-alpha"),
                native_chunk(12, 26.0, 56.0, "private-beta", is_last=True),
            ],
        )
        self.assertTrue(details["crosses_native_chunk_boundary"])
        self.assertEqual(
            (details["previous_native_chunk_index"], details["current_native_chunk_index"]),
            (11, 12),
        )
        self.assertAlmostEqual(details["overlap_duration"], 1.42)
        self.assertFalse(details["repeated_token_sequence"])
        self.assertFalse(details["timestamp_reset_relative_to_chunk_start"])

    def test_duplicate_word_and_multiword_sequences_are_detected_only_at_boundary(self):
        single = self.rejected_details(
            [("Repeat", 27.0, 29.0), ("repeat!", 27.5, 28.5)],
            [
                native_chunk(11, 0.0, 30.0, "Repeat"),
                native_chunk(12, 26.0, 56.0, "repeat!", is_last=True),
            ],
        )
        self.assertTrue(single["normalized_tokens_identical"])
        self.assertTrue(single["repeated_token_sequence"])
        self.assertEqual(single["repeated_sequence_length"], 1)

        multiple = self.rejected_details(
            [
                ("Alpha", 25.0, 25.5),
                ("beta", 26.0, 28.0),
                ("alpha", 27.0, 27.4),
                ("BETA!", 28.1, 28.5),
            ],
            [
                native_chunk(11, 0.0, 30.0, "Alpha beta"),
                native_chunk(12, 26.0, 56.0, "alpha BETA!", is_last=True),
            ],
        )
        self.assertTrue(multiple["repeated_token_sequence"])
        self.assertEqual(multiple["repeated_sequence_length"], 2)

    def test_chunk_relative_timestamp_reset_is_identified(self):
        details = self.rejected_details(
            [("private-alpha", 27.0, 29.0), ("private-beta", 1.0, 1.8)],
            [
                native_chunk(11, 0.0, 30.0, "private-alpha"),
                native_chunk(12, 26.0, 56.0, "private-beta", is_last=True),
            ],
        )
        self.assertTrue(details["crosses_native_chunk_boundary"])
        self.assertTrue(details["timestamp_reset_relative_to_chunk_start"])

    def test_safe_overlap_still_uses_existing_repair_policy(self):
        normalized, metadata = worker.normalize_word_timestamps(
            [
                {"word": "private-alpha", "start": 1.0, "end": 2.0},
                {"word": "private-beta", "start": 1.80, "end": 2.5},
            ],
            "private-alpha private-beta",
            3.0,
            diagnostic_context={
                "model_family": "large",
                "strategy": "continuation",
                "chunks": [
                    {
                        "chunk_index": 0,
                        "start": 0.0,
                        "end": 3.0,
                        "text": "private-alpha private-beta",
                    }
                ],
            },
        )
        self.assertEqual(normalized[0]["end"], 1.80)
        self.assertEqual(metadata["categories"]["adjacent_overlap"], 1)

    def test_fatal_boundary_reports_an_earlier_word_repair(self):
        details = self.rejected_details(
            [("private-alpha", 27.0, 27.0), ("private-beta", 26.0, 26.5)],
            [
                native_chunk(11, 0.0, 30.0, "private-alpha"),
                native_chunk(12, 26.0, 56.0, "private-beta", is_last=True),
            ],
        )
        self.assertTrue(details["crosses_native_chunk_boundary"])
        self.assertTrue(details["previous_boundary_was_repaired"])
        self.assertFalse(details["current_boundary_was_repaired"])
        self.assertEqual(details["repair_actions_attempted"], ["zero_duration"])
        self.assertNotIn("private", json.dumps(details))

    def test_cli_overlap_error_is_content_free_and_activity_compatible(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(
            json.dumps(make_request(self.audio, family="large")),
            encoding="utf-8",
        )
        malformed = native_result(
            [("sensitive-one", 27.0, 29.0), ("sensitive-two", 27.58, 28.5)],
            duration=60.0,
            chunks=[
                native_chunk(11, 0.0, 30.0, "sensitive-one"),
                native_chunk(12, 26.0, 56.0, "sensitive-two", is_last=True),
            ],
        )
        stdout = io.StringIO()
        with mock.patch.object(FakeModel, "transcribe", return_value=malformed), mock.patch.object(
            worker, "load_runtime", side_effect=fake_runtime
        ), redirect_stdout(stdout):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )

        events = [
            json.loads(line[len(worker.EVENT_PREFIX) :])
            for line in stdout.getvalue().splitlines()
            if line.startswith(worker.EVENT_PREFIX)
        ]
        error = events[-1]
        serialized = json.dumps(error)
        self.assertEqual(return_code, worker.TranscriptionError.exit_code)
        self.assertEqual(error["details"]["effective_strategy"], "continuation")
        self.assertIn(
            "Timestamp validation rejected a 1.42-second overlap at continuation chunk boundary 11->12.",
            [event.get("message") for event in events],
        )
        self.assertNotIn("sensitive", serialized)
        self.assertNotIn(str(self.audio), serialized)
        self.assertFalse(output_path.exists())


class TargetedRecoveryTests(WorkerTestCase):
    def base_candidate(self, words, *, duration=60.0):
        return worker._normalize_transcription_candidate(
            native_result(words, duration=duration),
            "verbatim",
            model_family="medium",
            strategy="continuation",
        )

    def run_recovery(
        self,
        *,
        base_words,
        gaps,
        short_results,
        duration=60.0,
        final_after_words=1,
        slice_writer=None,
    ):
        base = self.base_candidate(base_words, duration=duration)
        base_audit = diagnostic_coverage_audit(gaps)
        results = list(short_results)
        calls = []
        written = []

        class Model:
            def transcribe(model_self, audio_path, **kwargs):
                calls.append((audio_path, copy.deepcopy(kwargs)))
                result_factory = results.pop(0)
                return result_factory(written[-1])

        def writer(_audio, start, end, path):
            written.append((start, end, path))
            path.write_bytes(b"synthetic wav")
            return start, end

        def auditor(words, _duration, _supplier):
            recovered = max(0, len(words) - len(base_words))
            return (
                diagnostic_coverage_audit([])
                if recovered >= final_after_words
                else diagnostic_coverage_audit(gaps)
            )

        selected, final_audit, metadata = worker._run_targeted_recovery(
            base,
            base_audit,
            base_strategy="continuation",
            base_selection_reason="chunked_lcs_not_improved",
            model=Model(),
            model_family="medium",
            transcription=self.validated_request()["job"]["settings"]["transcription"],
            decoding=self.validated_request()["job"]["settings"]["decoding"],
            audio_supplier=lambda: object(),
            auditor=auditor,
            slice_writer=slice_writer or writer,
        )
        return selected, final_audit, metadata, calls, written

    @staticmethod
    def local_result(token, local_start, local_end):
        def factory(frame):
            start, end, _path = frame
            return native_result(
                [(token, local_start, local_end)],
                duration=end - start,
            )

        return factory

    def test_one_standalone_slice_recovers_a_missing_middle_region(self):
        selected, final_audit, metadata, calls, written = self.run_recovery(
            base_words=[("before", 0.2, 0.8), ("after", 20.0, 20.5)],
            gaps=[(0.8, 20.0)],
            short_results=[self.local_result("middle", 10.0, 10.5)],
        )
        self.assertEqual([word["word"] for word in selected["words"]], ["before", "middle", "after"])
        self.assertEqual(final_audit["speech_active_gap_count"], 0)
        self.assertEqual(metadata["recovered_gap_count"], 1)
        self.assertEqual(metadata["short_window_attempt_count"], 1)
        self.assertEqual(len(written), 1)
        kwargs = calls[0][1]
        self.assertNotIn("longform_strategy", kwargs)
        self.assertNotIn("context_words", kwargs)
        self.assertEqual(kwargs["mode"], "verbatim")
        self.assertTrue(kwargs["word_timestamps"])
        self.assertTrue(kwargs["temperature_fallback"])
        self.assertTrue(kwargs["hallucination_mitigation"])
        self.assertTrue(kwargs["early_eot_recovery"])

    def test_long_gap_uses_overlapping_inputs_no_longer_than_25_seconds(self):
        factories = [
            self.local_result(f"part{index}", 8.0, 8.4)
            for index in range(3)
        ]
        selected, final_audit, metadata, _calls, written = self.run_recovery(
            base_words=[("before", 0.2, 0.8), ("after", 52.0, 52.5)],
            gaps=[(0.8, 52.0)],
            short_results=factories,
            duration=60.0,
            final_after_words=3,
        )
        geometries = metadata["framing_geometries"]
        self.assertEqual(len(geometries), 3)
        self.assertTrue(all(item["input_duration"] <= 25.0 for item in geometries))
        self.assertLess(geometries[1]["core_start"], geometries[0]["core_end"])
        self.assertEqual(final_audit["speech_active_gap_count"], 0)
        self.assertEqual(
            [word["word"] for word in selected["words"]],
            ["before", "part0", "part1", "part2", "after"],
        )

    def test_separated_gaps_are_recovered_in_source_order(self):
        selected, _audit, metadata, _calls, _written = self.run_recovery(
            base_words=[
                ("first", 0.2, 0.8),
                ("second", 12.0, 12.5),
                ("third", 25.0, 25.5),
            ],
            gaps=[(0.8, 12.0), (12.5, 25.0)],
            short_results=[
                self.local_result("gap-one", 5.0, 5.4),
                self.local_result("gap-two", 5.0, 5.4),
            ],
            final_after_words=2,
        )
        self.assertEqual(
            [word["word"] for word in selected["words"]],
            ["first", "gap-one", "second", "gap-two", "third"],
        )
        self.assertEqual([item["gap_index"] for item in metadata["framing_geometries"]], [1, 2])

    def test_second_framing_attempt_runs_only_after_first_still_has_gap(self):
        selected, final_audit, metadata, _calls, written = self.run_recovery(
            base_words=[("before", 0.2, 0.8), ("after", 20.0, 20.5)],
            gaps=[(0.8, 20.0)],
            short_results=[
                self.local_result("", 1.0, 1.2),
                self.local_result("recovered", 8.0, 8.4),
            ],
        )
        self.assertEqual(final_audit["speech_active_gap_count"], 0)
        self.assertIn("recovered", [word["word"] for word in selected["words"]])
        self.assertEqual(metadata["short_window_attempt_count"], 2)
        self.assertEqual(metadata["failed_window_attempt_count"], 1)
        self.assertEqual(
            [item["attempt"] for item in metadata["framing_geometries"]],
            [1, 2],
        )

    def test_boundary_duplicates_are_removed_but_legitimate_repetition_remains(self):
        merged, inserted = worker._merge_targeted_words(
            [
                {"word": "Alpha", "start": 0.0, "end": 1.0},
                {"word": "Omega", "start": 10.0, "end": 11.0},
            ],
            [
                {"word": "alpha!", "start": 0.6, "end": 1.1},
                {"word": "middle", "start": 3.0, "end": 3.5},
                {"word": "omega", "start": 9.8, "end": 10.2},
            ],
            core_start=1.0,
            core_end=10.0,
            duration=12.0,
        )
        self.assertEqual(inserted, 1)
        self.assertEqual([word["word"] for word in merged], ["Alpha", "middle", "Omega"])

        repeated, inserted = worker._merge_targeted_words(
            [
                {"word": "yes", "start": 0.0, "end": 1.0},
                {"word": "later", "start": 5.0, "end": 5.5},
            ],
            [{"word": "yes", "start": 1.1, "end": 1.5}],
            core_start=1.0,
            core_end=5.0,
            duration=6.0,
        )
        self.assertEqual(inserted, 1)
        self.assertEqual([word["word"] for word in repeated], ["yes", "yes", "later"])

    def test_absolute_timestamp_conversion_and_base_words_are_preserved(self):
        selected, _audit, _metadata, _calls, written = self.run_recovery(
            base_words=[("before", 8.0, 8.5), ("after", 30.0, 30.5)],
            gaps=[(8.5, 30.0)],
            short_results=[
                self.local_result("absolute", 4.0, 4.5),
                self.local_result("tail", 4.0, 4.5),
            ],
            final_after_words=2,
        )
        starts = {word["word"]: word["start"] for word in selected["words"]}
        self.assertAlmostEqual(starts["absolute"], written[0][0] + 4.0)
        self.assertEqual(
            [(selected["words"][0]["word"], selected["words"][0]["start"]),
             (selected["words"][-1]["word"], selected["words"][-1]["start"])],
            [("before", 8.0), ("after", 30.0)],
        )

    def test_remaining_gap_and_invalid_windows_fail_closed_after_bounded_attempts(self):
        selected, final_audit, metadata, _calls, written = self.run_recovery(
            base_words=[("before", 0.2, 0.8), ("after", 20.0, 20.5)],
            gaps=[(0.8, 20.0)],
            short_results=[
                self.local_result("", 1.0, 1.2),
                lambda frame: native_result(
                    [("bad", float("nan"), 1.0)],
                    duration=frame[1] - frame[0],
                ),
            ],
            final_after_words=99,
        )
        self.assertEqual([word["word"] for word in selected["words"]], ["before", "after"])
        self.assertEqual(final_audit["speech_active_gap_count"], 1)
        self.assertEqual(metadata["short_window_attempt_count"], 2)
        self.assertEqual(metadata["failed_window_attempt_count"], 2)
        self.assertTrue(all(not path.exists() for _start, _end, path in written))

    def test_non_monotonic_distinct_recovery_is_rejected_without_altering_base(self):
        base = [
            {"word": "before", "start": 0.0, "end": 2.0},
            {"word": "after", "start": 10.0, "end": 11.0},
        ]
        with self.assertRaises(worker.TranscriptionError):
            worker._merge_targeted_words(
                base,
                [{"word": "distinct", "start": 1.0, "end": 3.0}],
                core_start=1.0,
                core_end=10.0,
                duration=12.0,
            )
        self.assertEqual(base[0]["end"], 2.0)

    def test_cancellation_during_slice_and_inference_cleans_temporary_directory(self):
        real_temporary_directory = worker.tempfile.TemporaryDirectory

        def local_temporary_directory(*args, **kwargs):
            kwargs["dir"] = self.root
            return real_temporary_directory(*args, **kwargs)

        def cancelled_writer(*_args):
            raise worker.WorkerCancelled("cancelled during slicing")

        with mock.patch.object(worker.tempfile, "TemporaryDirectory", local_temporary_directory):
            with self.assertRaises(worker.WorkerCancelled):
                self.run_recovery(
                    base_words=[("before", 0.2, 0.8), ("after", 20.0, 20.5)],
                    gaps=[(0.8, 20.0)],
                    short_results=[],
                    slice_writer=cancelled_writer,
                )
        self.assertEqual(list(self.root.glob("ats-crisper-recovery-*")), [])

        class CancelledModel:
            def transcribe(self, *_args, **_kwargs):
                raise worker.WorkerCancelled("cancelled during inference")

        base = self.base_candidate([("before", 0.2, 0.8), ("after", 20.0, 20.5)])
        with mock.patch.object(worker.tempfile, "TemporaryDirectory", local_temporary_directory):
            with self.assertRaises(worker.WorkerCancelled):
                worker._run_targeted_recovery(
                    base,
                    diagnostic_coverage_audit([(0.8, 20.0)]),
                    base_strategy="continuation",
                    base_selection_reason="chunked_lcs_not_improved",
                    model=CancelledModel(),
                    model_family="medium",
                    transcription=self.validated_request()["job"]["settings"]["transcription"],
                    decoding=self.validated_request()["job"]["settings"]["decoding"],
                    audio_supplier=lambda: object(),
                    auditor=lambda *_args: diagnostic_coverage_audit([(0.8, 20.0)]),
                    slice_writer=lambda _audio, start, end, path: (
                        path.write_bytes(b"slice") and start,
                        end,
                    ),
                )
        self.assertEqual(list(self.root.glob("ats-crisper-recovery-*")), [])


class LongformCoverageTests(WorkerTestCase):
    @staticmethod
    def points(tokens, start=0.0, step=2.0, length=0.4):
        return [
            (token, start + index * step, start + index * step + length)
            for index, token in enumerate(tokens)
        ]

    def run_candidates(self, primary, fallback=None, *, audio, auditor=None):
        strategies = []
        self.candidate_calls = []
        self.candidate_audio_paths = []

        def transcribe(_model, audio_path, **kwargs):
            strategy = kwargs["longform_strategy"]
            strategies.append(strategy)
            self.candidate_calls.append(copy.deepcopy(kwargs))
            self.candidate_audio_paths.append(audio_path)
            if strategy == "continuation":
                return primary
            if fallback is None:
                self.fail("Unexpected chunked-LCS fallback")
            return fallback

        with mock.patch.object(FakeModel, "transcribe", transcribe):
            response = worker.transcribe_job(
                self.validated_request(),
                runtime_loader=fake_runtime,
                coverage_audio_loader=lambda _path: audio,
                coverage_auditor=auditor,
            )
        return response, strategies

    def observed_primary(self):
        return native_result(
            [
                ("competent.", 158.66, 159.06),
                ("The-individual", 182.46, 185.90),
                ("structure", 208.20, 208.70),
            ],
            duration=220.0,
        )

    def observed_recovery(self, *, zero_duration=False):
        tokens = [
            "competent.",
            "daily",
            "media",
            "personalities",
            "commentary.",
            "I",
            "am",
            "here",
            "because",
            "this",
            "sentence",
            "must",
            "survive.",
            "The",
            "individual",
            "and",
            "the",
            "persona",
            "continue",
            "through",
            "the",
            "missing",
            "middle",
            "section.",
            "structure",
        ]
        points = self.points(tokens, start=158.66, step=2.0)
        if zero_duration:
            token, start, _end = points[8]
            points[8] = (token, start, start)
        return native_result(points, duration=220.0)

    def test_normal_continuation_and_ordinary_pauses_do_not_load_audio_or_fallback(self):
        primary = native_result(
            [("one", 2.0, 2.4), ("two", 6.0, 6.4)],
            duration=60.0,
        )

        def unexpected_loader(_path):
            self.fail("Audio should not be loaded when no substantial gap exists")

        with mock.patch.object(FakeModel, "transcribe", return_value=primary) as transcribe:
            response = worker.transcribe_job(
                self.validated_request(),
                runtime_loader=fake_runtime,
                coverage_audio_loader=unexpected_loader,
            )

        coverage = response["transcription"]["longform_coverage"]
        self.assertEqual(coverage["attempted_strategies"], ["continuation"])
        self.assertEqual(coverage["speech_active_gap_count"], 0)
        self.assertFalse(coverage["fallback_used"])
        transcribe.assert_called_once()

    def test_long_silent_interior_gap_is_audited_without_fallback(self):
        primary = native_result(
            [("before", 1.0, 1.4), ("after", 20.0, 20.4)],
            duration=60.0,
        )
        response, strategies = self.run_candidates(
            primary,
            audio=synthetic_activity_audio(60.0),
        )
        coverage = response["transcription"]["longform_coverage"]
        self.assertEqual(strategies, ["continuation"])
        self.assertEqual(coverage["substantial_gap_count"], 1)
        self.assertEqual(coverage["speech_active_gap_count"], 0)
        self.assertIn("non-speech", coverage["warnings"][0])

    def test_single_short_transient_does_not_count_as_speech_activity(self):
        primary = native_result(
            [("before", 1.0, 1.4), ("after", 20.0, 20.4)],
            duration=60.0,
        )
        response, strategies = self.run_candidates(
            primary,
            audio=synthetic_activity_audio(60.0, intervals=((10.0, 10.2),)),
        )
        self.assertEqual(strategies, ["continuation"])
        self.assertEqual(
            response["transcription"]["longform_coverage"]["speech_active_gap_count"],
            0,
        )

    def test_observed_gap_fixture_selects_complete_chunked_lcs_result(self):
        primary = self.observed_primary()
        fallback = self.observed_recovery()
        audio = synthetic_activity_audio(
            220.0,
            intervals=((160.0, 181.0), (186.0, 207.0)),
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            response, strategies = self.run_candidates(primary, fallback, audio=audio)

        transcription = response["transcription"]
        coverage = transcription["longform_coverage"]
        self.assertEqual(strategies, ["continuation", "chunked_lcs"])
        primary_call, fallback_call = self.candidate_calls
        self.assertEqual(primary_call.pop("longform_strategy"), "continuation")
        self.assertEqual(fallback_call.pop("longform_strategy"), "chunked_lcs")
        self.assertEqual(fallback_call, primary_call)
        self.assertEqual(
            self.candidate_audio_paths,
            [str(self.audio), str(self.audio)],
        )
        self.assertEqual(response["settings"]["effective"]["longform"]["strategy"], "chunked_lcs")
        self.assertEqual(coverage["speech_active_gap_count"], 2)
        self.assertEqual(coverage["recovered_gap_count"], 2)
        self.assertEqual(coverage["remaining_speech_active_gap_count"], 0)
        self.assertTrue(coverage["fallback_used"])
        self.assertIn("I am here because", transcription["text"])
        self.assertEqual(
            [word["word"] for word in transcription["words"]],
            [word.word for word in fallback.words],
        )
        self.assertIn("Detected 2 speech-active coverage gaps", stdout.getvalue())
        self.assertIn("Coverage recovery succeeded using chunked LCS", stdout.getvalue())

    def test_transcribe_job_accepts_only_after_targeted_final_audit_passes(self):
        primary = native_result(
            [("before", 0.2, 0.8), ("after", 20.0, 20.5)],
            duration=60.0,
        )
        fallback = copy.deepcopy(primary)
        written = []
        calls = []

        def transcribe(_model, audio_path, **kwargs):
            calls.append((audio_path, copy.deepcopy(kwargs)))
            strategy = kwargs.get("longform_strategy")
            if strategy == "continuation":
                return primary
            if strategy == "chunked_lcs":
                return fallback
            start, end, _path = written[-1]
            return native_result(
                [("recovered", 8.0, 8.4)],
                duration=end - start,
            )

        def writer(_audio, start, end, path):
            written.append((start, end, path))
            path.write_bytes(b"slice")
            return start, end

        def auditor(words, _duration, _supplier):
            if any(word["word"] == "recovered" for word in words):
                return diagnostic_coverage_audit([])
            return diagnostic_coverage_audit([(0.8, 20.0)])

        stdout = io.StringIO()
        with mock.patch.object(FakeModel, "transcribe", transcribe), redirect_stdout(stdout):
            response = worker.transcribe_job(
                self.validated_request(),
                runtime_loader=fake_runtime,
                coverage_audio_loader=lambda _path: object(),
                coverage_auditor=auditor,
                targeted_slice_writer=writer,
            )

        coverage = response["transcription"]["longform_coverage"]
        self.assertEqual(
            coverage["selected_strategy"],
            "continuation_plus_targeted_recovery",
        )
        self.assertEqual(coverage["remaining_speech_active_gap_count"], 0)
        self.assertEqual(
            response["settings"]["effective"]["longform"]["strategy"],
            "continuation_plus_targeted_recovery",
        )
        self.assertIn("recovered", response["transcription"]["text"])
        self.assertEqual(
            [path for path, _kwargs in calls[:2]],
            [str(self.audio), str(self.audio)],
        )
        self.assertNotIn("longform_strategy", calls[2][1])
        self.assertIn("Starting targeted recovery for 1 speech-active gap.", stdout.getvalue())
        self.assertIn("Final coverage audit passed.", stdout.getvalue())
        self.assertTrue(all(not path.exists() for _start, _end, path in written))
        import crisperwhisper_backend as adapter_protocol

        adapter_protocol.validate_transcribe_response(
            response,
            self.validated_request()["job"]["settings"],
        )

    def test_improved_incomplete_chunked_lcs_is_the_targeted_base(self):
        primary = native_result(
            [("before", 0.2, 0.8), ("after", 30.0, 30.5)],
            duration=60.0,
        )
        fallback = copy.deepcopy(primary)
        written = []

        def transcribe(_model, _audio_path, **kwargs):
            strategy = kwargs.get("longform_strategy")
            if strategy == "continuation":
                return primary
            if strategy == "chunked_lcs":
                return fallback
            start, end, _path = written[-1]
            return native_result([("recovered", 5.0, 5.4)], duration=end - start)

        audits = iter(
            [
                diagnostic_coverage_audit([(0.8, 12.0), (15.0, 30.0)]),
                diagnostic_coverage_audit([(0.8, 12.0)]),
                diagnostic_coverage_audit([]),
            ]
        )

        def writer(_audio, start, end, path):
            written.append((start, end, path))
            path.write_bytes(b"slice")
            return start, end

        with mock.patch.object(FakeModel, "transcribe", transcribe):
            response = worker.transcribe_job(
                self.validated_request(),
                runtime_loader=fake_runtime,
                coverage_audio_loader=lambda _path: object(),
                coverage_auditor=lambda *_args: next(audits),
                targeted_slice_writer=writer,
            )

        coverage = response["transcription"]["longform_coverage"]
        self.assertEqual(
            coverage["selected_strategy"],
            "chunked_lcs_plus_targeted_recovery",
        )
        self.assertTrue(coverage["fallback_used"])
        self.assertEqual(
            coverage["targeted_recovery"]["base_selection_reason"],
            "chunked_lcs_improved_but_incomplete",
        )

    def test_medium_style_failed_fallback_retains_both_audits_and_exact_reason(self):
        primary_ranges = [
            (100.0, 115.0),
            (130.0, 145.0),
            (160.0, 178.4),
            (200.0, 215.0),
            (230.0, 245.0),
            (260.0, 275.0),
            (290.0, 305.0),
            (320.0, 335.0),
            (350.0, 365.0),
            (380.0, 395.0),
            (410.0, 425.0),
            (440.0, 455.0),
            (470.0, 485.0),
        ]
        fallback_ranges = [
            (159.06, 170.0),
            (185.0, 192.0),
            (300.0, 306.0),
            (500.0, 507.26),
        ]
        audits = [
            diagnostic_coverage_audit(primary_ranges, silence_count=2, silence_duration=12.5),
            diagnostic_coverage_audit(fallback_ranges, silence_count=1, silence_duration=6.0),
        ]
        audit_calls = 0

        def auditor(_words, _duration, _audio_supplier):
            nonlocal audit_calls
            result = audits[min(audit_calls, len(audits) - 1)]
            audit_calls += 1
            return result

        stdout = io.StringIO()
        primary_result = self.observed_primary()
        fallback_result = self.observed_recovery()
        primary_result.duration = 783.74
        fallback_result.duration = 783.74
        with redirect_stdout(stdout):
            with self.assertRaises(worker.CoverageError) as raised:
                self.run_candidates(
                    primary_result,
                    fallback_result,
                    audio=None,
                    auditor=auditor,
                )

        details = raised.exception.details
        continuation = details["candidate_audits"]["continuation"]
        fallback = details["candidate_audits"]["chunked_lcs"]
        self.assertEqual(continuation["speech_active_gap_count"], 13)
        self.assertEqual(continuation["speech_active_gap_duration"], 198.4)
        self.assertEqual(fallback["speech_active_gap_count"], 4)
        self.assertEqual(fallback["speech_active_gap_duration"], 31.2)
        self.assertEqual(details["recovered_gap_count"], 9)
        self.assertEqual(details["recovered_duration"], 167.2)
        self.assertEqual(details["remaining_speech_active_gap_count"], 4)
        self.assertEqual(
            details["fallback_rejection_reason"],
            "targeted_recovery_incomplete",
        )
        self.assertEqual(
            details["targeted_recovery"]["base_selection_reason"],
            "chunked_lcs_improved_but_incomplete",
        )
        self.assertFalse(
            fallback["known_diagnostic_interval"]["covered"]
        )
        activity = stdout.getvalue()
        self.assertIn(
            "Continuation coverage: 13 speech-active gaps totaling 198.40 seconds.",
            activity,
        )
        self.assertIn(
            "Chunked LCS coverage: 4 speech-active gaps totaling 31.20 seconds.",
            activity,
        )
        self.assertIn(
            "Chunked LCS recovered 9 gaps totaling 167.20 seconds.",
            activity,
        )
        self.assertIn(
            "Coverage recovery rejected: 4 speech-active gaps remain.",
            activity,
        )
        self.assertIn("Remaining gap ranges:", activity)

        audits[0]["speech_active_gap_ranges"].clear()
        audits[1]["speech_active_gap_ranges"].clear()
        self.assertEqual(
            len(details["candidate_audits"]["continuation"]["speech_active_gap_ranges"]),
            13,
        )
        self.assertEqual(
            len(details["candidate_audits"]["chunked_lcs"]["speech_active_gap_ranges"]),
            4,
        )

    def test_fallback_timestamp_repair_is_applied_before_selection(self):
        response, _strategies = self.run_candidates(
            self.observed_primary(),
            self.observed_recovery(zero_duration=True),
            audio=synthetic_activity_audio(
                220.0,
                intervals=((160.0, 181.0), (186.0, 207.0)),
            ),
        )
        repairs = response["transcription"]["word_timestamp_repairs"]
        self.assertEqual(repairs["categories"]["zero_duration"], 1)
        self.assertTrue(
            all(
                word["start"] < word["end"]
                for word in response["transcription"]["words"]
            )
        )

    def test_nonfinite_and_gross_fallback_timestamps_fail_as_coverage_errors(self):
        malformed_fallback_words = {
            "nan": [("bad", float("nan"), 170.0)],
            "infinity": [("bad", 168.0, float("inf"))],
            "gross reversal": [("bad", 180.0, 168.0)],
        }
        for label, points in malformed_fallback_words.items():
            with self.subTest(label=label):
                fallback = native_result(points, duration=220.0)
                with self.assertRaises(worker.CoverageError):
                    self.run_candidates(
                        self.observed_primary(),
                        fallback,
                        audio=synthetic_activity_audio(
                            220.0,
                            intervals=((160.0, 181.0), (186.0, 207.0)),
                        ),
                    )

    def test_improved_but_incomplete_fallback_fails_closed(self):
        fallback = native_result(
            self.points(
                ["competent."] + [f"fill{index}" for index in range(13)],
                start=158.66,
                step=2.0,
            )
            + [("structure", 208.20, 208.70)],
            duration=220.0,
        )
        with self.assertRaises(worker.CoverageError):
            self.run_candidates(
                self.observed_primary(),
                fallback,
                audio=synthetic_activity_audio(
                    220.0,
                    intervals=((160.0, 181.0), (186.0, 207.0)),
                ),
            )

    def test_equally_incomplete_or_worse_fallback_fails_closed(self):
        worse = native_result(
            [
                ("competent.", 158.66, 159.06),
                ("middle", 190.0, 190.4),
                ("structure", 214.0, 214.4),
            ],
            duration=220.0,
        )
        with self.assertRaises(worker.CoverageError) as raised:
            self.run_candidates(
                self.observed_primary(),
                worse,
                audio=synthetic_activity_audio(
                    220.0,
                    intervals=((160.0, 189.0), (191.0, 213.0)),
                ),
            )
        details = raised.exception.details
        self.assertEqual(details["fallback_rejection_reason"], "targeted_recovery_incomplete")
        self.assertEqual(
            details["targeted_recovery"]["base_selection_reason"],
            "chunked_lcs_not_improved",
        )
        self.assertIn("continuation", details["candidate_audits"])
        self.assertIn("chunked_lcs", details["candidate_audits"])
        self.assertGreater(
            details["candidate_audits"]["chunked_lcs"]["speech_active_gap_duration"],
            0.0,
        )

    def test_later_large_job_does_not_inherit_prior_fallback_strategy(self):
        medium_primary = self.observed_primary()
        medium_fallback = self.observed_recovery()
        large_overlap = native_result(
            [("private-alpha", 27.0, 29.0), ("private-beta", 27.58, 28.5)],
            duration=60.0,
            chunks=[
                native_chunk(11, 0.0, 30.0, "private-alpha"),
                native_chunk(12, 26.0, 56.0, "private-beta", is_last=True),
            ],
        )
        candidates = [medium_primary, medium_fallback, large_overlap]
        calls = []

        def transcribe(_model, audio_path, **kwargs):
            calls.append((audio_path, kwargs["longform_strategy"]))
            return candidates.pop(0)

        with mock.patch.object(FakeModel, "transcribe", transcribe):
            medium_response = worker.transcribe_job(
                self.validated_request(),
                runtime_loader=fake_runtime,
                coverage_audio_loader=lambda _path: synthetic_activity_audio(
                    220.0,
                    intervals=((160.0, 181.0), (186.0, 207.0)),
                ),
            )
            with self.assertRaises(worker.TranscriptionError) as raised:
                worker.transcribe_job(
                    self.validated_request(family="large"),
                    runtime_loader=fake_runtime,
                )

        self.assertEqual(
            medium_response["settings"]["effective"]["longform"]["strategy"],
            "chunked_lcs",
        )
        self.assertEqual(
            [strategy for _path, strategy in calls],
            ["continuation", "chunked_lcs", "continuation"],
        )
        self.assertEqual([path for path, _strategy in calls], [str(self.audio)] * 3)
        self.assertEqual(raised.exception.details["model_family"], "large")
        self.assertEqual(raised.exception.details["effective_strategy"], "continuation")

    def test_cancellation_during_fallback_transcription_propagates(self):
        calls = 0

        def transcribe(_model, _audio_path, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.observed_primary()
            raise KeyboardInterrupt

        with mock.patch.object(FakeModel, "transcribe", transcribe):
            with self.assertRaises(KeyboardInterrupt):
                worker.transcribe_job(
                    self.validated_request(),
                    runtime_loader=fake_runtime,
                    coverage_audio_loader=lambda _path: synthetic_activity_audio(
                        220.0,
                        intervals=((160.0, 181.0), (186.0, 207.0)),
                    ),
                )

    def test_cancellation_during_fallback_audit_propagates(self):
        calls = 0

        def auditor(_words, _duration, _audio_supplier):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise worker.WorkerCancelled("cancelled")
            return {
                "substantial_gap_count": 1,
                "substantial_gap_duration": 20.0,
                "speech_active_gap_count": 1,
                "speech_active_gap_duration": 20.0,
                "active_frame_seconds": 10.0,
                "configuration": {"substantial_gap_seconds": 5.0},
                "_speech_active_gaps": [
                    {"start": 1.0, "end": 21.0, "duration": 20.0}
                ],
            }

        with self.assertRaises(worker.WorkerCancelled):
            self.run_candidates(
                self.observed_primary(),
                self.observed_recovery(),
                audio=synthetic_activity_audio(220.0),
                auditor=auditor,
            )

    def test_cli_cancellation_during_fallback_returns_130_without_partial_result(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")
        calls = 0

        def transcribe(_model, _audio_path, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.observed_primary()
            raise KeyboardInterrupt

        with mock.patch.object(FakeModel, "transcribe", transcribe), mock.patch.object(
            worker, "load_runtime", side_effect=fake_runtime
        ), mock.patch.object(
            worker,
            "_load_coverage_audio",
            return_value=synthetic_activity_audio(
                220.0,
                intervals=((160.0, 181.0), (186.0, 207.0)),
            ),
        ), redirect_stdout(io.StringIO()):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )

        self.assertEqual(return_code, 130)
        self.assertFalse(output_path.exists())
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])

    def test_cli_cancellation_during_fallback_audit_leaves_no_partial_result(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")
        calls = 0

        def transcribe(_model, _audio_path, **kwargs):
            return (
                self.observed_primary()
                if kwargs["longform_strategy"] == "continuation"
                else self.observed_recovery()
            )

        def auditor(_words, _duration, _audio_supplier):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return {
                "substantial_gap_count": 1,
                "substantial_gap_duration": 20.0,
                "speech_active_gap_count": 1,
                "speech_active_gap_duration": 20.0,
                "active_frame_seconds": 10.0,
                "configuration": {"substantial_gap_seconds": 5.0},
                "_speech_active_gaps": [
                    {"start": 1.0, "end": 21.0, "duration": 20.0}
                ],
            }

        with mock.patch.object(FakeModel, "transcribe", transcribe), mock.patch.object(
            worker, "load_runtime", side_effect=fake_runtime
        ), mock.patch.object(
            worker, "audit_longform_coverage", side_effect=auditor
        ), redirect_stdout(io.StringIO()):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )

        self.assertEqual(return_code, 130)
        self.assertFalse(output_path.exists())
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])

    def test_cli_cancellation_during_targeted_inference_cleans_slices_and_returns_130(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")

        def transcribe(_model, _audio_path, **kwargs):
            if "longform_strategy" in kwargs:
                return self.observed_primary()
            raise KeyboardInterrupt

        real_temporary_directory = worker.tempfile.TemporaryDirectory

        def local_temporary_directory(*args, **kwargs):
            kwargs["dir"] = self.root
            return real_temporary_directory(*args, **kwargs)

        with mock.patch.object(FakeModel, "transcribe", transcribe), mock.patch.object(
            worker, "load_runtime", side_effect=fake_runtime
        ), mock.patch.object(
            worker,
            "_load_coverage_audio",
            return_value=synthetic_activity_audio(
                220.0,
                intervals=((160.0, 181.0), (186.0, 207.0)),
            ),
        ), mock.patch.object(
            worker.tempfile,
            "TemporaryDirectory",
            local_temporary_directory,
        ), redirect_stdout(io.StringIO()):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )

        self.assertEqual(return_code, 130)
        self.assertFalse(output_path.exists())
        self.assertEqual(list(self.root.glob("ats-crisper-recovery-*")), [])
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])

    def test_cli_both_strategies_incomplete_emits_coverage_error_without_output(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch.object(
            FakeModel, "transcribe", return_value=self.observed_primary()
        ), mock.patch.object(
            worker, "load_runtime", side_effect=fake_runtime
        ), mock.patch.object(
            worker,
            "_load_coverage_audio",
            return_value=synthetic_activity_audio(
                220.0,
                intervals=((160.0, 181.0), (186.0, 207.0)),
            ),
        ), redirect_stdout(stdout):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )

        self.assertEqual(return_code, worker.CoverageError.exit_code)
        self.assertFalse(output_path.exists())
        self.assertIn('"code":"coverage_incomplete"', stdout.getvalue())
        self.assertIn(
            "Coverage recovery failed; no incomplete result was committed.",
            stdout.getvalue(),
        )
        events = [
            json.loads(line[len(worker.EVENT_PREFIX) :])
            for line in stdout.getvalue().splitlines()
            if line.startswith(worker.EVENT_PREFIX)
        ]
        error = events[-1]
        self.assertEqual(error["event"], "error")
        self.assertEqual(error["details"]["diagnostic_type"], "coverage_failure")
        self.assertEqual(
            error["details"]["fallback_rejection_reason"],
            "targeted_recovery_incomplete",
        )
        self.assertIn("targeted_recovery", error["details"])
        self.assertEqual(
            set(error["details"]["candidate_audits"]),
            {"continuation", "chunked_lcs"},
        )
        serialized = json.dumps(error)
        self.assertNotIn(str(self.audio), serialized)
        self.assertNotIn("competent", serialized)
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])


class AtomicOutputTests(WorkerTestCase):
    def test_atomic_write_replaces_complete_target_and_leaves_no_temporary_file(self):
        output = self.root / "result.json"
        output.write_text("old", encoding="utf-8")
        payload = {"complete": True, "words": [1, 2, 3]}
        worker.atomic_write_json(output, payload)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])

    def test_failed_atomic_replace_preserves_target_and_cleans_temporary_file(self):
        output = self.root / "result.json"
        original = b"old complete data"
        output.write_bytes(original)
        with mock.patch.object(worker.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(worker.OutputError):
                worker.atomic_write_json(output, {"new": True})
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])

    def test_cancelled_atomic_replace_preserves_target_and_cleans_temporary_file(self):
        output = self.root / "result.json"
        original = b"old complete data"
        output.write_bytes(original)
        with mock.patch.object(worker.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker.atomic_write_json(output, {"new": True})
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".result.json.*.tmp")), [])

    def test_successful_cli_write_has_final_success_event(self):
        request_path = self.root / "request.json"
        output_path = self.root / "result.json"
        request_path.write_text(json.dumps(make_request(self.audio)), encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch.object(worker, "load_runtime", side_effect=fake_runtime), redirect_stdout(stdout):
            return_code = worker.main(
                ["transcribe", "--request", str(request_path), "--output", str(output_path)]
            )
        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["status"], "success")
        events = [line for line in stdout.getvalue().splitlines() if line.startswith(worker.EVENT_PREFIX)]
        self.assertGreaterEqual(len(events), 4)
        final = json.loads(events[-1][len(worker.EVENT_PREFIX) :])
        self.assertEqual(final["event"], "success")
        self.assertEqual(final["word_count"], 2)


if __name__ == "__main__":
    unittest.main()
