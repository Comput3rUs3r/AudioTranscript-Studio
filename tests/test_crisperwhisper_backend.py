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
