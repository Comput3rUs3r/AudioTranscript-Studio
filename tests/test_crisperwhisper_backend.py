from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import crisperwhisper_backend as backend
import crisperwhisper_worker as worker


def make_settings(*, family="medium", mode="verbatim"):
    config = copy.deepcopy(backend.DEFAULT_CRISPERWHISPER_CONFIG)
    config["model"]["family"] = family
    config["model"]["model_id"] = backend.OFFICIAL_MODEL_IDS[family]
    config["transcription"]["mode"] = mode
    config.pop("schema_version")
    config["transcription"]["language"] = "en"
    return config


def make_response(settings=None, *, mode=None):
    settings = copy.deepcopy(settings or make_settings())
    mode = mode or settings["transcription"]["mode"]
    effective = copy.deepcopy(settings)
    effective["model"]["resolved_model_id"] = effective["model"]["model_id"]
    return {
        "protocol_version": 1,
        "operation": "transcribe",
        "status": "success",
        "engine": "crisperwhisper",
        "runtime": {
            "versions": {
                "crisperwhisper": "2.0.2",
                "torch": "2.8.0+cu128",
                "transformers": "5.15.0",
                "ctranslate2": "4.8.1",
            },
            "cuda_available": True,
            "cuda_version": "12.8",
            "gpu_name": "Synthetic GPU",
            "execution_backend": "transformers",
        },
        "settings": {
            "requested": copy.deepcopy(settings),
            "effective": effective,
            "not_applied": [],
        },
        "transcription": {
            "text": "Hello world. Next phrase ends!",
            "language": "en",
            "mode": mode,
            "duration": 4.5,
            "processing_time": 0.25,
            "chunks": [
                {
                    "position": 0,
                    "chunk_index": 0,
                    "start": 0.0,
                    "end": 4.5,
                    "text": "Hello world. Next phrase ends!",
                    "context": None,
                    "is_last": True,
                    "stitch_lcs_length": None,
                    "stitch_lcs_words": None,
                }
            ],
            "words": [
                {"index": 0, "word": "Hello", "start": 0.1, "end": 0.5},
                {"index": 1, "word": "world.", "start": 0.55, "end": 1.0},
                {"index": 2, "word": "Next", "start": 2.0, "end": 2.3},
                {"index": 3, "word": "phrase", "start": 2.35, "end": 2.9},
                {"index": 4, "word": "ends!", "start": 3.0, "end": 3.5},
            ],
        },
    }


def make_probe_response():
    return {
        "protocol_version": 1,
        "operation": "probe",
        "status": "success",
        "runtime": {
            "versions": {
                "crisperwhisper": "2.0.2",
                "torch": "2.8.0+cu128",
                "transformers": "5.15.0",
                "ctranslate2": "4.8.1",
            },
            "cuda_available": True,
            "cuda_version": "12.8",
            "gpu_name": "Synthetic GPU",
            "available_execution_backends": ["transformers", "ct2"],
        },
        "capabilities": {
            "supported_execution_backends": ["transformers"],
        },
        "compatibility": {
            "installed_version_supported": True,
        },
    }


def make_coverage_metadata(*, fallback=False):
    configuration = {
        "substantial_gap_seconds": 5.0,
        "sample_rate": 16000,
        "frame_seconds": 0.03,
        "block_seconds": 1.0,
        "absolute_floor_dbfs": -50.0,
        "noise_percentile": 20.0,
        "reference_percentile": 90.0,
        "noise_margin_db": 10.0,
        "minimum_active_seconds": 0.75,
        "minimum_active_blocks": 2,
        "minimum_block_active_ratio": 0.2,
        "minimum_zero_crossing_rate": 0.01,
        "maximum_zero_crossing_rate": 0.4,
        "effective_threshold_dbfs": -42.0,
        "measured_noise_floor_dbfs": -60.0,
        "measured_reference_dbfs": -24.0,
    }
    initial_active_count = 2 if fallback else 0
    initial_active_duration = 45.7 if fallback else 0.0

    def audit(active_count, active_duration, substantial_count=2, substantial_duration=45.7):
        return {
            "substantial_gap_count": substantial_count,
            "substantial_gap_duration": substantial_duration,
            "speech_active_gap_count": active_count,
            "speech_active_gap_duration": active_duration,
            "active_frame_seconds": 25.0 if active_count else 0.0,
            "configuration": copy.deepcopy(configuration),
        }

    attempted = ["continuation", "chunked_lcs"] if fallback else ["continuation"]
    selected = "chunked_lcs" if fallback else "continuation"
    candidate_audits = {
        "continuation": audit(initial_active_count, initial_active_duration)
    }
    if fallback:
        candidate_audits["chunked_lcs"] = audit(0, 0.0, 0, 0.0)
    return {
        "requested_strategy": "continuation",
        "attempted_strategies": attempted,
        "selected_strategy": selected,
        "fallback_used": fallback,
        "substantial_gap_count": 2,
        "substantial_gap_duration": 45.7,
        "speech_active_gap_count": initial_active_count,
        "speech_active_gap_duration": initial_active_duration,
        "recovered_gap_count": initial_active_count if fallback else 0,
        "recovered_duration": initial_active_duration if fallback else 0.0,
        "remaining_speech_active_gap_count": 0,
        "remaining_speech_active_gap_duration": 0.0,
        "remaining_speech_active_gap_ranges": [],
        "configuration": configuration,
        "candidate_audits": candidate_audits,
        "warnings": (
            ["Continuation coverage was incomplete; chunked LCS passed the speech-active coverage audit."]
            if fallback
            else ["Substantial interior timeline gaps were classified as non-speech audio."]
        ),
    }


def make_timestamp_diagnostic():
    return {
        "diagnostic_type": "timestamp_overlap",
        "model_family": "large",
        "effective_strategy": "continuation",
        "previous_native_chunk_index": 11,
        "current_native_chunk_index": 12,
        "previous_word_index": 430,
        "current_word_index": 431,
        "previous_start": 182.2,
        "previous_end": 184.0,
        "current_start": 182.58,
        "current_end": 183.1,
        "overlap_duration": 1.42,
        "overlap_milliseconds": 1420.0,
        "crosses_native_chunk_boundary": True,
        "previous_chunk_window": {"chunk_index": 11, "start": 156.0, "end": 186.0},
        "current_chunk_window": {"chunk_index": 12, "start": 182.0, "end": 212.0},
        "normalized_tokens_identical": False,
        "repeated_token_sequence": False,
        "repeated_sequence_length": 0,
        "timestamp_reset_relative_to_chunk_start": False,
        "previous_boundary_was_repaired": False,
        "current_boundary_was_repaired": False,
        "repair_actions_attempted": [],
        "reason": "previous_end_exceeds_current_start_beyond_tolerance",
        "repair_failure_reason": "overlap_exceeds_tolerance",
        "tolerance_seconds": 0.25,
    }


def make_error_coverage_audit(ranges):
    gap_ranges = [
        {
            "start": start,
            "end": end,
            "duration": round(end - start, 3),
            "active_seconds": 2.0,
            "active_blocks": 2,
        }
        for start, end in ranges
    ]
    active_duration = round(sum(item["duration"] for item in gap_ranges), 3)
    return {
        "substantial_gap_count": len(gap_ranges) + 1,
        "substantial_gap_duration": active_duration + 5.0,
        "speech_active_gap_count": len(gap_ranges),
        "speech_active_gap_duration": active_duration,
        "silence_gap_count": 1,
        "silence_gap_duration": 5.0,
        "speech_active_gap_ranges": gap_ranges,
        "active_frame_seconds": 2.0 * len(gap_ranges),
        "known_diagnostic_interval": {
            "start": 168.2,
            "end": 177.42,
            "covered": False,
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
    }


def make_coverage_failure_diagnostic():
    continuation = make_error_coverage_audit([(159.06, 182.46), (185.9, 208.2)])
    fallback = make_error_coverage_audit([(159.06, 170.0)])
    return {
        "diagnostic_type": "coverage_failure",
        "model_family": "medium",
        "requested_strategy": "continuation",
        "attempted_strategies": ["continuation", "chunked_lcs"],
        "fallback_rejection_reason": "speech_active_gaps_remain",
        "recovered_gap_count": 1,
        "recovered_duration": round(
            continuation["speech_active_gap_duration"]
            - fallback["speech_active_gap_duration"],
            3,
        ),
        "remaining_speech_active_gap_count": 1,
        "remaining_speech_active_gap_duration": fallback["speech_active_gap_duration"],
        "candidate_audits": {
            "continuation": continuation,
            "chunked_lcs": fallback,
        },
    }


def make_targeted_recovery_metadata(*, complete=True):
    base = make_error_coverage_audit([(1.0, 12.0)])
    final = make_error_coverage_audit([] if complete else [(6.0, 12.0)])
    unresolved_count = final["speech_active_gap_count"]
    unresolved_duration = final["speech_active_gap_duration"]
    return {
        "base_strategy": "continuation",
        "base_selection_reason": "chunked_lcs_not_improved",
        "attempted": True,
        "target_gap_count": base["speech_active_gap_count"],
        "target_gap_duration": base["speech_active_gap_duration"],
        "short_window_attempt_count": 1,
        "failed_window_attempt_count": 0,
        "framing_geometries": [
            {
                "attempt": 1,
                "gap_index": 1,
                "window_index": 1,
                "core_start": 1.0,
                "core_end": 12.0,
                "slice_start": 0.0,
                "slice_end": 14.0,
                "padding_seconds": 2.0,
                "shift_seconds": 0.0,
                "input_duration": 14.0,
            }
        ],
        "recovered_gap_count": max(0, 1 - unresolved_count),
        "recovered_duration": round(
            base["speech_active_gap_duration"] - unresolved_duration,
            3,
        ),
        "unresolved_gap_count": unresolved_count,
        "unresolved_gap_duration": unresolved_duration,
        "final_coverage_audit": final,
        "warnings": [],
    }


def make_targeted_coverage_metadata(*, complete=True):
    targeted = make_targeted_recovery_metadata(complete=complete)
    primary = make_error_coverage_audit([(1.0, 12.0)])
    fallback = copy.deepcopy(primary)
    final = targeted["final_coverage_audit"]
    return {
        "requested_strategy": "continuation",
        "attempted_strategies": ["continuation", "chunked_lcs"],
        "selected_strategy": "continuation_plus_targeted_recovery",
        "fallback_used": False,
        "substantial_gap_count": primary["substantial_gap_count"],
        "substantial_gap_duration": primary["substantial_gap_duration"],
        "speech_active_gap_count": primary["speech_active_gap_count"],
        "speech_active_gap_duration": primary["speech_active_gap_duration"],
        "silence_gap_count": primary["silence_gap_count"],
        "silence_gap_duration": primary["silence_gap_duration"],
        "speech_active_gap_ranges": copy.deepcopy(primary["speech_active_gap_ranges"]),
        "recovered_gap_count": targeted["recovered_gap_count"],
        "recovered_duration": targeted["recovered_duration"],
        "remaining_speech_active_gap_count": final["speech_active_gap_count"],
        "remaining_speech_active_gap_duration": final["speech_active_gap_duration"],
        "remaining_speech_active_gap_ranges": copy.deepcopy(
            final["speech_active_gap_ranges"]
        ),
        "known_diagnostic_interval": copy.deepcopy(
            targeted["final_coverage_audit"]["known_diagnostic_interval"]
        ),
        "configuration": copy.deepcopy(primary["configuration"]),
        "candidate_audits": {
            "continuation": primary,
            "chunked_lcs": fallback,
        },
        "targeted_recovery": targeted,
        "warnings": [
            "Targeted context-free short windows passed the final speech-active coverage audit."
        ],
    }


def classify_response(response, *, complete, coverage=None):
    response = copy.deepcopy(response)
    coverage = coverage or make_targeted_coverage_metadata(complete=complete)
    response["status"] = "complete" if complete else "incomplete"
    response["coverage_complete"] = complete
    response["remaining_speech_active_gap_count"] = coverage[
        "remaining_speech_active_gap_count"
    ]
    response["remaining_speech_active_gap_duration"] = coverage[
        "remaining_speech_active_gap_duration"
    ]
    response["remaining_speech_active_gap_ranges"] = copy.deepcopy(
        coverage["remaining_speech_active_gap_ranges"]
    )
    response["selected_strategy"] = coverage["selected_strategy"]
    response["settings"]["effective"]["longform"]["strategy"] = coverage[
        "selected_strategy"
    ]
    response["transcription"]["longform_coverage"] = coverage
    return response


def event(payload):
    return backend.EVENT_PREFIX + json.dumps(payload, separators=(",", ":")) + "\n"


class FakeProcess:
    next_pid = 9000

    def __init__(self, lines, returncode=0):
        self.stdout = io.StringIO("".join(lines))
        self.returncode = returncode
        self.terminated = 0
        self.killed = 0
        self.pid = type(self).next_pid
        type(self).next_pid += 1

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 130

    def kill(self):
        self.killed += 1
        self.returncode = 130


class InterruptingStream:
    def __iter__(self):
        raise KeyboardInterrupt

    def close(self):
        pass


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.interpreter = self.root / "venv-crisper" / "Scripts" / "python.exe"
        self.interpreter.parent.mkdir(parents=True)
        self.interpreter.write_bytes(b"fixture")
        self.worker = self.root / "crisperwhisper_worker.py"
        self.worker.write_text("# fixture", encoding="utf-8")
        self.audio = self.root / "sample.wav"
        self.audio.write_bytes(b"fixture audio")

    def adapter(self, popen_factory, logs=None):
        return backend.CrisperWhisperBackend(
            self.root,
            interpreter_path=self.interpreter,
            worker_path=self.worker,
            popen_factory=popen_factory,
            log_callback=(logs.append if logs is not None else lambda _message: None),
        )


class ConfigurationTests(AdapterTestCase):
    def test_defaults_and_partial_model_family_resolution(self):
        settings = backend.resolve_crisperwhisper_settings({}, "en")
        self.assertEqual(settings["model"]["family"], "medium")
        self.assertEqual(settings["model"]["model_id"], backend.OFFICIAL_MODEL_IDS["medium"])
        self.assertEqual(settings["transcription"]["mode"], "verbatim")

        settings = backend.resolve_crisperwhisper_settings(
            {"schema_version": 1, "model": {"family": "turbo"}},
            "de",
        )
        self.assertEqual(settings["model"]["model_id"], backend.OFFICIAL_MODEL_IDS["turbo"])
        self.assertEqual(settings["transcription"]["language"], "de")

    def test_version_unknown_setting_and_unknown_backend_fail_clearly(self):
        with self.assertRaises(backend.CrisperWhisperConfigurationError):
            backend.resolve_crisperwhisper_settings({"schema_version": 2}, "en")
        with self.assertRaises(backend.CrisperWhisperConfigurationError):
            backend.resolve_crisperwhisper_settings({"private_cache": "x"}, "en")
        with self.assertRaisesRegex(backend.CrisperWhisperConfigurationError, "whisperx"):
            backend.normalize_backend_name("automatic")

    def test_absent_backend_defaults_to_whisperx(self):
        self.assertEqual(backend.normalize_backend_name(None), "whisperx")
        self.assertEqual(backend.normalize_backend_name("whisperx"), "whisperx")
        self.assertEqual(backend.normalize_backend_name("crisperwhisper"), "crisperwhisper")


class ProtocolTests(AdapterTestCase):
    def test_probe_validation_failures(self):
        cases = []
        response = make_probe_response()
        response["protocol_version"] = 2
        cases.append(response)
        response = make_probe_response()
        response["runtime"]["versions"]["crisperwhisper"] = "2.0.3"
        cases.append(response)
        response = make_probe_response()
        response["runtime"]["cuda_available"] = False
        cases.append(response)
        response = make_probe_response()
        response["runtime"]["available_execution_backends"] = ["ct2"]
        cases.append(response)
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(backend.CrisperWhisperBackendError):
                    backend.validate_probe_response(response)

    def test_invalid_word_timestamps_and_substitution_are_rejected(self):
        settings = make_settings()
        response = make_response(settings)
        response["transcription"]["words"][1]["start"] = 0.01
        with self.assertRaisesRegex(backend.CrisperWhisperProtocolError, "timestamps"):
            backend.validate_transcribe_response(response, settings)

        response = make_response(settings)
        response["transcription"]["words"][0]["end"] = 0.1
        with self.assertRaisesRegex(backend.CrisperWhisperProtocolError, "timestamps"):
            backend.validate_transcribe_response(response, settings)

        response = make_response(settings)
        response["settings"]["effective"]["model"]["model_id"] = backend.OFFICIAL_MODEL_IDS["large"]
        with self.assertRaisesRegex(backend.CrisperWhisperProtocolError, "substituted"):
            backend.validate_transcribe_response(response, settings)

        response = make_response(settings)
        response["transcription"]["mode"] = "intended"
        with self.assertRaisesRegex(backend.CrisperWhisperProtocolError, "mode"):
            backend.validate_transcribe_response(response, settings)

    def test_optional_repair_metadata_is_backward_compatible_and_strictly_validated(self):
        settings = make_settings()
        legacy_response = make_response(settings)
        validated = backend.validate_transcribe_response(legacy_response, settings)
        self.assertNotIn("word_timestamp_repairs", validated["transcription"])

        response = make_response(settings)
        response["transcription"]["word_timestamp_repairs"] = {
            "repair_count": 2,
            "categories": {"zero_duration": 1, "adjacent_overlap": 1},
            "words_remained_untimed": False,
            "untimed_word_count": 0,
            "warnings": ["Minor word timestamp anomalies were normalized conservatively."],
        }
        validated = backend.validate_transcribe_response(response, settings)
        self.assertEqual(
            validated["transcription"]["word_timestamp_repairs"]["repair_count"],
            2,
        )

        malformed = copy.deepcopy(response)
        malformed["transcription"]["word_timestamp_repairs"]["repair_count"] = 1
        with self.assertRaisesRegex(backend.CrisperWhisperProtocolError, "counts"):
            backend.validate_transcribe_response(malformed, settings)

        clustered = make_response(settings)
        clustered["transcription"]["word_timestamp_repairs"] = {
            "repair_count": 1,
            "categories": {"cross_chunk_overlap_cluster_reflow": 1},
            "cluster_reflows": [
                {"word_count": 2, "max_displacement_milliseconds": 20.0}
            ],
            "words_remained_untimed": False,
            "untimed_word_count": 0,
            "warnings": ["Minor word timestamp anomalies were normalized conservatively."],
        }
        validated = backend.validate_transcribe_response(clustered, settings)
        self.assertEqual(
            validated["transcription"]["word_timestamp_repairs"]["cluster_reflows"],
            [{"word_count": 2, "max_displacement_milliseconds": 20.0}],
        )
        for mutate in (
            lambda metadata: metadata.pop("cluster_reflows"),
            lambda metadata: metadata["cluster_reflows"][0].update(word_count=9),
            lambda metadata: metadata["cluster_reflows"][0].update(
                max_displacement_milliseconds=250.01
            ),
        ):
            malformed = copy.deepcopy(clustered)
            mutate(malformed["transcription"]["word_timestamp_repairs"])
            with self.assertRaises(backend.CrisperWhisperProtocolError):
                backend.validate_transcribe_response(malformed, settings)

    def test_optional_coverage_metadata_accepts_valid_fallback_and_rejects_inconsistency(self):
        settings = make_settings()
        legacy_response = make_response(settings)
        validated_legacy = backend.validate_transcribe_response(
            legacy_response,
            settings,
        )
        self.assertNotIn("longform_coverage", validated_legacy["transcription"])

        unreported_fallback = make_response(settings)
        unreported_fallback["settings"]["effective"]["longform"][
            "strategy"
        ] = "chunked_lcs"
        with self.assertRaisesRegex(
            backend.CrisperWhisperProtocolError,
            "unreported",
        ):
            backend.validate_transcribe_response(unreported_fallback, settings)

        response = make_response(settings)
        response["settings"]["effective"]["longform"]["strategy"] = "chunked_lcs"
        response["transcription"]["longform_coverage"] = make_coverage_metadata(
            fallback=True
        )
        validated = backend.validate_transcribe_response(response, settings)
        normalized = backend.normalize_worker_result(validated)
        self.assertEqual(
            normalized["transcription"]["longform_coverage"]["selected_strategy"],
            "chunked_lcs",
        )

        malformed_cases = []
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"][
            "remaining_speech_active_gap_count"
        ] = 1
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        malformed["settings"]["effective"]["longform"]["strategy"] = "continuation"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        del malformed["transcription"]["longform_coverage"]["candidate_audits"][
            "chunked_lcs"
        ]
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"]["recovered_gap_count"] = 1
        malformed_cases.append(malformed)
        for malformed in malformed_cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(backend.CrisperWhisperProtocolError):
                    backend.validate_transcribe_response(malformed, settings)

    def test_targeted_recovery_metadata_is_optional_backward_compatible_and_strict(self):
        settings = make_settings()
        response = make_response(settings)
        response["settings"]["effective"]["longform"][
            "strategy"
        ] = "continuation_plus_targeted_recovery"
        response["transcription"][
            "longform_coverage"
        ] = make_targeted_coverage_metadata()
        validated = backend.validate_transcribe_response(response, settings)
        targeted = validated["transcription"]["longform_coverage"][
            "targeted_recovery"
        ]
        self.assertEqual(targeted["base_strategy"], "continuation")
        self.assertEqual(targeted["unresolved_gap_count"], 0)

        legacy = backend.validate_transcribe_response(make_response(settings), settings)
        self.assertNotIn("longform_coverage", legacy["transcription"])

        malformed_cases = []
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"]["targeted_recovery"][
            "source_path"
        ] = "C:/private/source.wav"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"]["targeted_recovery"][
            "framing_geometries"
        ][0]["input_duration"] = 25.1
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"]["targeted_recovery"][
            "base_strategy"
        ] = "chunked_lcs"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"]["targeted_recovery"][
            "unresolved_gap_count"
        ] = 1
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(response)
        malformed["transcription"]["longform_coverage"]["targeted_recovery"][
            "warnings"
        ] = ["private transcript content"]
        malformed_cases.append(malformed)
        for malformed in malformed_cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(backend.CrisperWhisperProtocolError):
                    backend.validate_transcribe_response(malformed, settings)

    def test_complete_and_incomplete_protocol_one_classification_is_strict(self):
        settings = make_settings()
        complete = classify_response(
            make_response(settings),
            complete=True,
        )
        validated_complete = backend.validate_transcribe_response(complete, settings)
        self.assertEqual(validated_complete["status"], "complete")
        self.assertTrue(validated_complete["coverage_complete"])

        incomplete = classify_response(
            make_response(settings),
            complete=False,
        )
        validated_incomplete = backend.validate_transcribe_response(incomplete, settings)
        normalized = backend.normalize_worker_result(validated_incomplete)
        self.assertEqual(normalized["transcription"]["result_status"], "incomplete")
        self.assertFalse(normalized["transcription"]["coverage_complete"])
        self.assertEqual(
            normalized["transcription"]["remaining_speech_active_gap_count"],
            1,
        )
        self.assertEqual(
            normalized["transcription"]["remaining_speech_active_gap_ranges"],
            incomplete["remaining_speech_active_gap_ranges"],
        )

        malformed_cases = []
        malformed = copy.deepcopy(incomplete)
        malformed["coverage_complete"] = True
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(incomplete)
        malformed["remaining_speech_active_gap_count"] = 0
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(incomplete)
        malformed["remaining_speech_active_gap_ranges"] = []
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(incomplete)
        malformed["selected_strategy"] = "chunked_lcs"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(incomplete)
        del malformed["coverage_complete"]
        malformed_cases.append(malformed)
        for malformed in malformed_cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(backend.CrisperWhisperProtocolError):
                    backend.validate_transcribe_response(malformed, settings)

    def test_incomplete_targeted_recovery_error_details_are_strictly_validated(self):
        details = make_coverage_failure_diagnostic()
        targeted = make_targeted_recovery_metadata(complete=False)
        details["fallback_rejection_reason"] = "targeted_recovery_incomplete"
        details["remaining_speech_active_gap_count"] = targeted[
            "unresolved_gap_count"
        ]
        details["remaining_speech_active_gap_duration"] = targeted[
            "unresolved_gap_duration"
        ]
        details["candidate_audits"]["continuation"] = make_error_coverage_audit(
            [(1.0, 12.0)]
        )
        details["targeted_recovery"] = targeted
        validated = backend.validate_worker_error_details(details)
        self.assertEqual(validated, details)

        malformed = copy.deepcopy(details)
        malformed["targeted_recovery"]["framing_geometries"][0][
            "transcript"
        ] = "private"
        with self.assertRaises(backend.CrisperWhisperProtocolError):
            backend.validate_worker_error_details(malformed)

    def test_content_free_timestamp_error_diagnostic_is_strictly_validated(self):
        details = make_timestamp_diagnostic()
        validated = backend.validate_worker_error_details(details)
        self.assertEqual(validated, details)

        malformed_cases = []
        malformed = copy.deepcopy(details)
        malformed["source_path"] = "C:/private/source.wav"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(details)
        malformed["previous_word"] = "private transcript content"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(details)
        malformed["current_native_chunk_index"] = 11
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(details)
        malformed["repair_actions_attempted"] = ["discarded_duplicate_text"]
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(details)
        malformed["overlap_milliseconds"] = 20.0
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(details)
        malformed["repair_failure_reason"] = "contains transcript text"
        malformed_cases.append(malformed)
        malformed = copy.deepcopy(details)
        del malformed["overlap_milliseconds"]
        malformed_cases.append(malformed)
        for malformed in malformed_cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(backend.CrisperWhisperProtocolError):
                    backend.validate_worker_error_details(malformed)

        cluster = make_timestamp_diagnostic()
        cluster.update(
            {
                "previous_end": 183.0,
                "current_start": 182.98,
                "overlap_duration": 0.02,
                "overlap_milliseconds": 20.0,
                "reason": "overlap_has_no_positive_repair_interval",
                "repair_failure_reason": "cluster_insufficient_available_span",
                "cluster_words_examined": 2,
                "cluster_required_span_milliseconds": 40.0,
                "cluster_available_span_milliseconds": 30.0,
            }
        )
        self.assertEqual(backend.validate_worker_error_details(cluster), cluster)
        for key in (
            "cluster_words_examined",
            "cluster_required_span_milliseconds",
            "cluster_available_span_milliseconds",
        ):
            malformed = copy.deepcopy(cluster)
            del malformed[key]
            with self.assertRaises(backend.CrisperWhisperProtocolError):
                backend.validate_worker_error_details(malformed)
        malformed = copy.deepcopy(cluster)
        malformed["cluster_words_examined"] = 10
        with self.assertRaises(backend.CrisperWhisperProtocolError):
            backend.validate_worker_error_details(malformed)
        malformed = copy.deepcopy(cluster)
        del malformed["overlap_milliseconds"]
        del malformed["repair_failure_reason"]
        with self.assertRaises(backend.CrisperWhisperProtocolError):
            backend.validate_worker_error_details(malformed)

    def test_coverage_failure_diagnostic_retains_both_audits_and_rejects_unsafe_data(self):
        details = make_coverage_failure_diagnostic()
        validated = backend.validate_worker_error_details(details)
        self.assertEqual(validated, details)

        malformed = copy.deepcopy(details)
        malformed["candidate_audits"]["continuation"]["source_path"] = "C:/private.wav"
        with self.assertRaises(backend.CrisperWhisperProtocolError):
            backend.validate_worker_error_details(malformed)

        malformed = copy.deepcopy(details)
        malformed["candidate_audits"]["chunked_lcs"]["speech_active_gap_ranges"][0][
            "transcript"
        ] = "private words"
        with self.assertRaises(backend.CrisperWhisperProtocolError):
            backend.validate_worker_error_details(malformed)

    def test_legacy_worker_error_without_diagnostics_remains_supported(self):
        with self.assertRaises(backend.CrisperWhisperRuntimeError) as raised:
            backend.CrisperWhisperBackend._worker_failure(
                "transcription",
                5,
                {
                    "protocol_version": 1,
                    "event": "error",
                    "operation": "transcribe",
                    "code": "transcription_failed",
                    "message": "CrisperWhisper transcription failed.",
                },
            )
        self.assertIsNone(raised.exception.diagnostic_details)

    def test_worker_error_diagnostics_survive_adapter_failure(self):
        details = make_timestamp_diagnostic()
        with self.assertRaises(backend.CrisperWhisperRuntimeError) as raised:
            backend.CrisperWhisperBackend._worker_failure(
                "transcription",
                5,
                {
                    "protocol_version": 1,
                    "event": "error",
                    "operation": "transcribe",
                    "code": "transcription_failed",
                    "message": "CrisperWhisper timestamp validation failed.",
                    "details": details,
                },
            )
        self.assertEqual(raised.exception.diagnostic_details, details)


class NormalizationTests(AdapterTestCase):
    def test_words_are_preserved_once_and_segments_are_sensible(self):
        settings = make_settings()
        response = backend.validate_transcribe_response(make_response(settings), settings)
        normalized = backend.normalize_worker_result(response)
        segments = normalized["segments"]

        self.assertEqual([segment["text"] for segment in segments], ["Hello world.", "Next phrase ends!"])
        flattened = [word for segment in segments for word in segment["words"]]
        self.assertEqual(
            [word["word"] for word in flattened],
            ["Hello", "world.", "Next", "phrase", "ends!"],
        )
        self.assertEqual(flattened[0], {"word": "Hello", "start": 0.1, "end": 0.5})
        self.assertEqual(segments[0]["start"], 0.1)
        self.assertEqual(segments[-1]["end"], 3.5)

        metadata = normalized["transcription"]
        self.assertEqual(metadata["engine"], "crisperwhisper")
        self.assertEqual(metadata["package_version"], "2.0.2")
        self.assertEqual(metadata["model"]["family"], "medium")
        self.assertEqual(metadata["model"]["license_tier"], "nyra-health-non-commercial-research")
        self.assertEqual(metadata["execution_backend"], "transformers")
        self.assertEqual(metadata["native_text"], "Hello world. Next phrase ends!")
        self.assertEqual(len(metadata["native_chunks"]), 1)
        self.assertNotIn("gpu_name", metadata)

    def test_untimed_native_text_is_retained_in_segment_text(self):
        settings = make_settings()
        response = make_response(settings)
        response["transcription"]["text"] = "Hello unplaced material world. Next phrase ends!"
        response["transcription"]["chunks"][0]["text"] = response["transcription"]["text"]
        validated = backend.validate_transcribe_response(response, settings)
        normalized = backend.normalize_worker_result(validated)
        combined = " ".join(segment["text"] for segment in normalized["segments"])
        self.assertEqual(combined, response["transcription"]["text"])
        self.assertEqual(
            [word["word"] for segment in normalized["segments"] for word in segment["words"]],
            ["Hello", "world.", "Next", "phrase", "ends!"],
        )

    def test_unreconcilable_native_text_fails_instead_of_losing_words(self):
        settings = make_settings()
        response = make_response(settings)
        response["transcription"]["text"] = "Completely different transcript."
        validated = backend.validate_transcribe_response(response, settings)
        with self.assertRaisesRegex(backend.CrisperWhisperProtocolError, "reconciled"):
            backend.normalize_worker_result(validated)

    def test_repaired_words_form_valid_segments_and_retain_untimed_native_text(self):
        settings = make_settings()
        source_words = [
            {"word": "Hello,", "start": -0.02, "end": 0.45},
            {"word": "hello", "start": 0.40, "end": 0.40},
            {"word": "untimed", "start": 4.5, "end": None},
        ]
        native_text = "Hello, hello untimed"
        repaired_words, repair_metadata = worker.normalize_word_timestamps(
            source_words,
            native_text,
            4.5,
        )
        response = make_response(settings)
        response["transcription"]["text"] = native_text
        response["transcription"]["chunks"] = None
        response["transcription"]["words"] = repaired_words
        response["transcription"]["word_timestamp_repairs"] = repair_metadata

        validated = backend.validate_transcribe_response(response, settings)
        normalized = backend.normalize_worker_result(validated)
        segments = normalized["segments"]
        flattened = [word for segment in segments for word in segment["words"]]

        self.assertEqual([word["word"] for word in flattened], ["Hello,", "hello"])
        self.assertEqual(" ".join(segment["text"] for segment in segments), native_text)
        self.assertTrue(
            all(segment["start"] < segment["end"] for segment in segments)
        )
        self.assertTrue(
            all(
                current["end"] <= following["start"]
                for current, following in zip(flattened, flattened[1:])
            )
        )
        self.assertTrue(
            normalized["transcription"]["word_timestamp_repairs"]["words_remained_untimed"]
        )

    def test_normalization_preserves_a_decoded_middle_sentence_exactly(self):
        settings = make_settings()
        phrase = "I am here because this sentence must survive."
        native_text = f"Before. {phrase} After."
        tokens = native_text.split()
        starts = [150.0] + [168.2 + index * 0.6 for index in range(8)] + [208.2]
        response = make_response(settings)
        response["transcription"].update(
            {
                "text": native_text,
                "duration": 220.0,
                "chunks": [
                    {
                        "position": 0,
                        "chunk_index": 0,
                        "start": 130.0,
                        "end": 160.0,
                        "text": "Before.",
                        "context": None,
                        "is_last": False,
                        "stitch_lcs_length": None,
                        "stitch_lcs_words": None,
                    },
                    {
                        "position": 1,
                        "chunk_index": 1,
                        "start": 156.0,
                        "end": 186.0,
                        "text": phrase,
                        "context": "Before.",
                        "is_last": False,
                        "stitch_lcs_length": None,
                        "stitch_lcs_words": None,
                    },
                    {
                        "position": 2,
                        "chunk_index": 2,
                        "start": 182.0,
                        "end": 212.0,
                        "text": "After.",
                        "context": phrase,
                        "is_last": True,
                        "stitch_lcs_length": None,
                        "stitch_lcs_words": None,
                    },
                ],
                "words": [
                    {
                        "index": index,
                        "word": token,
                        "start": starts[index],
                        "end": starts[index] + 0.4,
                    }
                    for index, token in enumerate(tokens)
                ],
            }
        )

        validated = backend.validate_transcribe_response(response, settings)
        normalized = backend.normalize_worker_result(validated)
        normalized_text = " ".join(
            segment["text"] for segment in normalized["segments"]
        )
        normalized_words = [
            word["word"]
            for segment in normalized["segments"]
            for word in segment["words"]
        ]

        self.assertEqual(normalized_text, native_text)
        self.assertEqual(normalized_words, tokens)
        self.assertIn(phrase, normalized_text)
        self.assertTrue(
            all(
                segment["start"] < segment["end"]
                for segment in normalized["segments"]
            )
        )


class SubprocessTests(AdapterTestCase):
    def test_probe_nonzero_exit_uses_safe_worker_error(self):
        def factory(_command, **_kwargs):
            return FakeProcess(
                [
                    event(
                        {
                            "protocol_version": 1,
                            "event": "error",
                            "operation": "probe",
                            "code": "runtime_unavailable",
                            "message": "Required CrisperWhisper runtime package is unavailable.",
                        }
                    )
                ],
                returncode=3,
            )

        adapter = self.adapter(factory)
        with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "unavailable"):
            adapter.probe()

    def test_probe_success_and_cache(self):
        calls = []
        response = make_probe_response()

        def factory(command, **kwargs):
            calls.append((command, kwargs))
            return FakeProcess(
                [
                    event({"protocol_version": 1, "event": "status", "operation": "probe", "stage": "probing", "message": "Inspecting runtime."}),
                    event({"protocol_version": 1, "event": "success", "operation": "probe", "response": response}),
                ]
            )

        adapter = self.adapter(factory)
        self.assertEqual(adapter.probe()["status"], "success")
        self.assertEqual(adapter.probe()["status"], "success")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]["shell"])

    def test_missing_interpreter_and_worker(self):
        missing_interpreter = self.root / "missing-python.exe"
        adapter = backend.CrisperWhisperBackend(
            self.root,
            interpreter_path=missing_interpreter,
            worker_path=self.worker,
        )
        with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "venv-crisper"):
            adapter.probe()

        adapter = backend.CrisperWhisperBackend(
            self.root,
            interpreter_path=self.interpreter,
            worker_path=self.root / "missing-worker.py",
        )
        with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "worker"):
            adapter.probe()

    def test_transcription_subprocess_result_and_temporary_cleanup(self):
        settings = make_settings(mode="intended")
        response = make_response(settings)
        temporary_directories = []
        calls = []

        def factory(command, **kwargs):
            calls.append((command, kwargs))
            if "probe" in command and "transcribe" not in command:
                return FakeProcess(
                    [event({"protocol_version": 1, "event": "success", "operation": "probe", "response": make_probe_response()})]
                )
            request_path = Path(command[command.index("--request") + 1])
            output_path = Path(command[command.index("--output") + 1])
            temporary_directories.append(request_path.parent)
            written_request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(written_request["job"]["settings"], settings)
            output_path.write_text(json.dumps(response), encoding="utf-8")
            return FakeProcess(
                [
                    "untrusted library output containing C:\\private\\cache\n",
                    event({"protocol_version": 1, "event": "status", "operation": "transcribe", "stage": "transcribing", "message": "Transcribing audio."}),
                    event({"protocol_version": 1, "event": "success", "operation": "transcribe", "word_count": 5}),
                ]
            )

        logs = []
        adapter = self.adapter(factory, logs)
        result = adapter.transcribe(self.audio, settings)
        self.assertEqual(result["transcription"]["mode"], "intended")
        self.assertEqual(logs, ["[crisper] Transcribing audio."])
        self.assertEqual(len(calls), 2)
        self.assertTrue(temporary_directories)
        self.assertFalse(temporary_directories[0].exists())

    def test_coverage_status_events_are_activity_compatible(self):
        settings = make_settings()
        response = make_response(settings)
        logs = []
        statuses = []

        def factory(command, **_kwargs):
            output_path = Path(command[command.index("--output") + 1])
            output_path.write_text(json.dumps(response), encoding="utf-8")
            return FakeProcess(
                [
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "auditing_coverage",
                            "message": "Auditing long-form transcript coverage.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "coverage_fallback",
                            "message": "Detected 2 speech-active coverage gaps; retrying with chunked LCS.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "coverage_summary",
                            "message": "Continuation coverage: 13 speech-active gaps totaling 198.40 seconds.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "coverage_summary",
                            "message": "Chunked LCS coverage: 4 speech-active gaps totaling 31.20 seconds.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "coverage_recovery_summary",
                            "message": "Chunked LCS recovered 9 gaps totaling 167.20 seconds.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "coverage_rejected",
                            "message": "Coverage recovery rejected: 4 speech-active gaps remain.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "coverage_remaining_ranges",
                            "message": "Remaining gap ranges: 02:39.06-02:50.00",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "timestamp_rejected",
                            "message": "Timestamp validation rejected a 1.42-second overlap at continuation chunk boundary 11->12.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "targeted_recovery_start",
                            "message": "Starting targeted recovery for 9 speech-active gaps.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "targeted_recovery_gap",
                            "message": "Recovering gap 1/9: 00:11.02-00:28.40.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "targeted_recovery_summary",
                            "message": "Targeted recovery restored 8 gaps totaling 132.40 seconds.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "targeted_recovery_failed",
                            "message": "Targeted recovery left 1 speech-active gap totaling 12.32 seconds; no result was committed.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "status",
                            "operation": "transcribe",
                            "stage": "targeted_recovery_passed",
                            "message": "Final coverage audit passed.",
                        }
                    ),
                    event(
                        {
                            "protocol_version": 1,
                            "event": "success",
                            "operation": "transcribe",
                        }
                    ),
                ]
            )

        adapter = self.adapter(factory, logs)
        adapter._probe_response = make_probe_response()
        adapter.transcribe(
            self.audio,
            settings,
            status_callback=lambda stage, message: statuses.append((stage, message)),
        )
        self.assertEqual(
            logs,
            [
                "[crisper] Auditing long-form transcript coverage.",
                "[crisper] Detected 2 speech-active coverage gaps; retrying with chunked LCS.",
                "[crisper] Continuation coverage: 13 speech-active gaps totaling 198.40 seconds.",
                "[crisper] Chunked LCS coverage: 4 speech-active gaps totaling 31.20 seconds.",
                "[crisper] Chunked LCS recovered 9 gaps totaling 167.20 seconds.",
                "[crisper] Coverage recovery rejected: 4 speech-active gaps remain.",
                "[crisper] Remaining gap ranges: 02:39.06-02:50.00",
                "[crisper] Timestamp validation rejected a 1.42-second overlap at continuation chunk boundary 11->12.",
                "[crisper] Starting targeted recovery for 9 speech-active gaps.",
                "[crisper] Recovering gap 1/9: 00:11.02-00:28.40.",
                "[crisper] Targeted recovery restored 8 gaps totaling 132.40 seconds.",
                "[crisper] Targeted recovery left 1 speech-active gap totaling 12.32 seconds; no result was committed.",
                "[crisper] Final coverage audit passed.",
            ],
        )
        self.assertEqual(
            [stage for stage, _message in statuses],
            [
                "auditing_coverage",
                "coverage_fallback",
                "coverage_summary",
                "coverage_summary",
                "coverage_recovery_summary",
                "coverage_rejected",
                "coverage_remaining_ranges",
                "timestamp_rejected",
                "targeted_recovery_start",
                "targeted_recovery_gap",
                "targeted_recovery_summary",
                "targeted_recovery_failed",
                "targeted_recovery_passed",
            ],
        )

    def test_nonzero_missing_and_malformed_results_fail(self):
        settings = make_settings()

        def run_case(kind):
            def factory(command, **_kwargs):
                output_path = Path(command[command.index("--output") + 1])
                if kind == "nonzero":
                    return FakeProcess(
                        [event({"protocol_version": 1, "event": "error", "operation": "transcribe", "code": "transcription_failed", "message": "CrisperWhisper transcription failed."})],
                        returncode=5,
                    )
                if kind == "malformed":
                    output_path.write_text("{bad", encoding="utf-8")
                return FakeProcess(
                    [event({"protocol_version": 1, "event": "success", "operation": "transcribe"})]
                )

            adapter = self.adapter(factory)
            adapter._probe_response = make_probe_response()
            with self.assertRaises(backend.CrisperWhisperBackendError):
                adapter.transcribe(self.audio, settings)

        for kind in ("nonzero", "missing", "malformed"):
            with self.subTest(kind=kind):
                run_case(kind)

    def test_cancellation_terminates_worker_and_removes_temporary_directory(self):
        settings = make_settings()
        process = FakeProcess([])
        process.stdout = InterruptingStream()
        process.returncode = None
        temporary_parent = None

        def factory(command, **_kwargs):
            nonlocal temporary_parent
            temporary_parent = Path(command[command.index("--request") + 1]).parent
            return process

        adapter = self.adapter(factory)
        adapter._probe_response = make_probe_response()
        with self.assertRaises(KeyboardInterrupt):
            adapter.transcribe(self.audio, settings)
        self.assertEqual(process.terminated, 1)
        self.assertFalse(temporary_parent.exists())
        adapter.cancel()
        self.assertEqual(process.terminated, 1)


if __name__ == "__main__":
    unittest.main()
