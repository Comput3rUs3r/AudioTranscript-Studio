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
