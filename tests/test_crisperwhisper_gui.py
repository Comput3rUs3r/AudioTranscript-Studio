from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock

import yaml

import crisperwhisper_backend as backend
import split_audio
import split_audio_gui as gui


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
            "gpu_name": "NVIDIA GeForce RTX 4080",
            "available_execution_backends": ["transformers", "ct2"],
        },
        "official_model_families": [
            "small", "medium", "large", "turbo",
            "small_pro", "medium_pro", "large_pro", "turbo_pro",
        ],
        "supported_model_families": ["small", "medium", "large", "turbo"],
        "capabilities": {"supported_execution_backends": ["transformers"]},
        "compatibility": {"installed_version_supported": True},
    }


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeGridWidget:
    def __init__(self):
        self.visible = None

    def grid(self):
        self.visible = True

    def grid_remove(self):
        self.visible = False


class GuiConfigTests(unittest.TestCase):
    def test_existing_configuration_defaults_to_whisperx(self):
        selection, warnings = gui.crisper_gui_selection_from_conf({"model": "large-v3"})
        self.assertEqual(selection["backend"], "whisperx")
        self.assertEqual(selection["model"], "medium")
        self.assertEqual(selection["mode"], "verbatim")
        self.assertEqual(warnings, [])

    def test_backend_model_and_mode_persist_with_exact_values(self):
        update = {
            "transcription_backend": "crisperwhisper",
            "crisperwhisper": gui.crisper_gui_config_update("turbo", "intended"),
        }
        merged = gui.merge_gui_conf({}, update)
        selection, warnings = gui.crisper_gui_selection_from_conf(merged)
        self.assertEqual(warnings, [])
        self.assertEqual(selection["backend"], "crisperwhisper")
        self.assertEqual(selection["model"], "turbo")
        self.assertEqual(selection["mode"], "intended")
        self.assertEqual(
            merged["crisperwhisper"]["model"]["model_id"],
            backend.OFFICIAL_MODEL_IDS["turbo"],
        )

    def test_collected_run_configuration_uses_displayed_backend_model_and_mode(self):
        app = object.__new__(gui.App)
        values = {
            "var_transcription_backend": "crisperwhisper",
            "var_crisper_model": "medium",
            "var_crisper_mode": "intended",
            "var_compute": "float32",
            "var_tf32": "on",
            "var_output": "both",
            "var_srt": True,
            "var_txt": True,
            "var_lang": "en",
            "var_model": "large-v3",
            "var_diar": True,
            "var_slice": True,
            "var_slice_video": False,
            "var_fast_cut": False,
            "var_player": "",
            "var_workers": 4,
            "var_onefolder": False,
            "var_tags": True,
            "var_pad": 0.25,
            "var_hf": "",
        }
        for name, value in values.items():
            setattr(app, name, FakeVar(value))
        app._speaker_count_config_values = lambda: ("auto", 2, 2)
        app._current_ner_engine = lambda: "auto"
        app._crisper_license_acknowledged = False
        collected = app._collect_ui_to_conf()
        self.assertEqual(collected["transcription_backend"], "crisperwhisper")
        self.assertEqual(collected["crisperwhisper"]["model"]["family"], "medium")
        self.assertEqual(collected["crisperwhisper"]["transcription"]["mode"], "intended")
        self.assertTrue(collected["crisperwhisper"]["transcription"]["word_timestamps"])
        self.assertEqual(collected["compute_type"], "float16")
        self.assertEqual(collected["tf32"], "off")

    def test_deep_merge_preserves_unknown_expert_settings(self):
        existing = {
            "unknown_top_level": {"keep": True},
            "crisperwhisper": {
                "longform": {"chunk_duration": 42.0, "future_option": "keep"},
                "decoding": {"max_new_tokens": 384},
                "model": {"device_index": 1, "family": "small"},
            },
        }
        merged = gui.merge_gui_conf(
            existing,
            {
                "transcription_backend": "crisperwhisper",
                "crisperwhisper": gui.crisper_gui_config_update("large", "verbatim"),
            },
        )
        self.assertEqual(merged["unknown_top_level"], {"keep": True})
        self.assertEqual(
            merged["crisperwhisper"]["longform"],
            {"chunk_duration": 42.0, "future_option": "keep"},
        )
        self.assertEqual(merged["crisperwhisper"]["decoding"]["max_new_tokens"], 384)
        self.assertEqual(merged["crisperwhisper"]["model"]["device_index"], 1)

    def test_invalid_saved_values_warn_and_use_safe_defaults(self):
        selection, warnings = gui.crisper_gui_selection_from_conf(
            {
                "transcription_backend": "automatic",
                "crisperwhisper": {
                    "model": {
                        "family": "large_pro",
                        "model_id": "private/custom",
                        "execution_backend": "ct2",
                    },
                    "transcription": {"mode": "dual", "word_timestamps": False},
                },
            }
        )
        self.assertEqual(selection["backend"], "whisperx")
        self.assertEqual(selection["model"], "medium")
        self.assertEqual(selection["mode"], "verbatim")
        self.assertGreaterEqual(len(warnings), 5)

    def test_conditional_controls_preserve_whisperx_model(self):
        app = object.__new__(gui.App)
        app.var_transcription_backend = FakeVar("whisperx")
        app.var_model = FakeVar("large-v3")
        app.whisperx_model_controls = FakeGridWidget()
        app.crisper_model_controls = FakeGridWidget()
        app._crisper_license_acknowledged = True
        app._update_transcription_engine_controls(show_notice=False)
        self.assertTrue(app.whisperx_model_controls.visible)
        self.assertFalse(app.crisper_model_controls.visible)
        app.var_transcription_backend.set("crisperwhisper")
        app._update_transcription_engine_controls(show_notice=False)
        self.assertFalse(app.whisperx_model_controls.visible)
        self.assertTrue(app.crisper_model_controls.visible)
        self.assertEqual(app.var_model.get(), "large-v3")

    def test_license_notice_is_saved_once_and_preserves_unknown_keys(self):
        app = object.__new__(gui.App)
        app._crisper_license_acknowledged = False
        app.log = mock.Mock()
        app.winfo_toplevel = lambda: None
        with tempfile.TemporaryDirectory() as temporary_directory:
            conf = Path(temporary_directory) / "conf.yaml"
            conf.write_text("unknown_key: keep\n", encoding="utf-8")
            with mock.patch.object(gui, "conf_path", return_value=conf), mock.patch.object(
                gui.messagebox, "showinfo"
            ) as showinfo:
                app._show_crisper_license_notice_once()
                app._show_crisper_license_notice_once()
            saved = yaml.safe_load(conf.read_text(encoding="utf-8"))
        self.assertEqual(showinfo.call_count, 1)
        self.assertTrue(saved["crisperwhisper_license_acknowledged"])
        self.assertEqual(saved["unknown_key"], "keep")


class ProbeTests(unittest.TestCase):
    def test_successful_probe_requires_rtx_and_supported_models(self):
        response = gui.validate_crisper_probe_for_gui(make_probe_response())
        self.assertEqual(response["runtime"]["versions"]["crisperwhisper"], "2.0.2")

    def test_protocol_cuda_version_and_model_failures_are_rejected(self):
        mutations = []
        mutations.append(lambda value: value.update(protocol_version=99))
        mutations.append(lambda value: value["runtime"].update(cuda_available=False))
        mutations.append(
            lambda value: value["runtime"]["versions"].update(crisperwhisper="2.0.3")
        )
        mutations.append(lambda value: value["runtime"].update(gpu_name="Synthetic GPU"))
        mutations.append(lambda value: value.update(supported_model_families=["medium"]))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                response = make_probe_response()
                mutate(response)
                with self.assertRaises(backend.CrisperWhisperBackendError):
                    gui.validate_crisper_probe_for_gui(response)

    def test_missing_venv_and_worker_have_actionable_errors(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            adapter = backend.CrisperWhisperBackend(root)
            with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "venv-crisper"):
                adapter.probe()
            interpreter = root / "venv-crisper" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "worker"):
                adapter.probe()

    def test_probe_runs_off_thread_and_delivers_on_queue(self):
        class SlowAdapter:
            def probe(self, force=False):
                time.sleep(0.12)
                return make_probe_response()

            def cancel(self):
                pass

        app = object.__new__(gui.App)
        app.queue = queue.Queue()
        app._crisper_probe_adapter = None
        app._crisper_probe_in_progress = False
        app._crisper_probe_callbacks = []
        app._crisper_probe_response = None
        app._crisper_probe_error = None
        app._crisper_probe_time = 0.0
        app._crisper_probe_thread = None
        app._create_crisper_probe_adapter = lambda: SlowAdapter()
        app.log = mock.Mock()
        received = []
        started = time.monotonic()
        app._start_crisper_probe(lambda response, error: received.append((response, error)), allow_cached=False)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.08)
        app._crisper_probe_thread.join(timeout=2)
        event = app.queue.get(timeout=1)
        self.assertEqual(event[0], "crisper_probe_finished")
        app._finish_crisper_probe(event[1], event[2])
        self.assertEqual(len(received), 1)
        self.assertIsNone(received[0][1])

    def test_probe_failure_never_falls_back_to_whisperx(self):
        app = object.__new__(gui.App)
        app._crisper_launch_waiting = False
        app.cancel_requested = False
        app._set_runtime_state = mock.Mock()
        app._set_progress_display = mock.Mock()
        app.log = mock.Mock()
        app.winfo_toplevel = lambda: None
        app._launch_pipeline_process = mock.Mock()
        app._start_crisper_probe = lambda callback, allow_cached=True: callback(
            None, "venv-crisper is incomplete"
        )
        context = {"crisper_model": "medium", "crisper_mode": "verbatim"}
        with mock.patch.object(gui.messagebox, "showerror"):
            app._begin_crisper_launch(context)
        app._launch_pipeline_process.assert_not_called()
        app._set_runtime_state.assert_any_call("Failed")

    def test_probe_cancellation_uses_adapter_and_does_not_launch(self):
        app = object.__new__(gui.App)
        adapter = mock.Mock()
        app._crisper_launch_waiting = True
        app._crisper_probe_adapter = adapter
        app.cancel_requested = False
        app._set_runtime_state = mock.Mock()
        app.log = mock.Mock()
        app.proc = None
        app.on_stop()
        self.assertTrue(app.cancel_requested)
        adapter.cancel.assert_called_once_with()
        app._set_runtime_state.assert_called_with("Cancelling")


class LaunchAndDiagnosticsTests(unittest.TestCase):
    def test_crisper_launch_hands_off_probe_and_exact_context(self):
        app = object.__new__(gui.App)
        app.proc = None
        app.pending_review_result = "old"
        app.log = mock.Mock()
        app._set_runtime_state = mock.Mock()
        app._reader = mock.Mock()
        context = {
            "backend": "crisperwhisper",
            "whisperx_model": "large-v3",
            "crisper_model": "medium",
            "crisper_mode": "intended",
            "workers": 3,
            "input_files": ["short.wav"],
        }
        process = SimpleNamespace()
        with mock.patch.object(gui.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
            gui.threading, "Thread"
        ) as thread:
            app._launch_pipeline_process(context, crisper_probe=make_probe_response())
        kwargs = popen.call_args.kwargs
        handed_off = json.loads(kwargs["env"][backend.PREFLIGHT_ENV_VAR])
        self.assertEqual(handed_off["runtime"]["gpu_name"], "NVIDIA GeForce RTX 4080")
        self.assertEqual(popen.call_args.args[0][-2:], ["--inputs", "short.wav"])
        self.assertIsNone(app.pending_review_result)
        thread.return_value.start.assert_called_once_with()

    def test_explicit_whisperx_launch_keeps_existing_command_path(self):
        app = object.__new__(gui.App)
        app.proc = None
        app.pending_review_result = "old"
        app.log = mock.Mock()
        app._set_runtime_state = mock.Mock()
        app._reader = mock.Mock()
        app.var_compute = FakeVar("float16")
        app.var_tf32 = FakeVar("off")
        context = {
            "backend": "whisperx",
            "whisperx_model": "large-v3",
            "crisper_model": "medium",
            "crisper_mode": "verbatim",
            "workers": 4,
            "input_files": [],
        }
        with mock.patch.object(gui.subprocess, "Popen", return_value=SimpleNamespace()) as popen, mock.patch.object(
            gui.threading, "Thread"
        ):
            app._launch_pipeline_process(context)
        command = popen.call_args.args[0]
        self.assertEqual(command[1], str(gui.program_root() / "split_audio.py"))
        self.assertEqual(command[-2:], ["--workers", "4"])
        self.assertIsNone(popen.call_args.kwargs["env"])

    def test_seeded_probe_is_revalidated_and_cached(self):
        adapter = backend.CrisperWhisperBackend(Path.cwd())
        seeded = adapter.seed_probe_response(make_probe_response())
        self.assertEqual(adapter.probe(), seeded)
        broken = make_probe_response()
        broken["protocol_version"] = 2
        with self.assertRaises(backend.CrisperWhisperProtocolError):
            adapter.seed_probe_response(broken)

    def test_pipeline_reuses_gui_probe_without_running_worker_probe_twice(self):
        instances = []

        class SeededAdapter:
            def __init__(self, _root):
                self.seed_calls = 0
                self.probe_calls = 0
                instances.append(self)

            def seed_probe_response(self, response):
                backend.validate_probe_response(response)
                self.seed_calls += 1

            def probe(self):
                self.probe_calls += 1
                raise AssertionError("duplicate probe")

            def transcribe(self, *_args, **_kwargs):
                raise backend.CrisperWhisperRuntimeError("stop after preflight assertion")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output"
            wav = root / "wav"
            output.mkdir()
            wav.mkdir()
            audio = wav / "sample.wav"
            audio.write_bytes(b"synthetic")
            cfg = split_audio.Conf(
                diarize=False,
                slice_audio=False,
                slice_video=False,
                transcription_backend="crisperwhisper",
            )
            settings = backend.resolve_crisperwhisper_settings({}, "en")
            with mock.patch.dict(
                os.environ,
                {backend.PREFLIGHT_ENV_VAR: json.dumps(make_probe_response())},
            ), mock.patch.object(
                split_audio, "load_conf", return_value=(cfg, None)
            ), mock.patch.object(
                split_audio, "setup_device", return_value="cuda"
            ), mock.patch.object(
                split_audio, "discover_sources", return_value=[audio]
            ), mock.patch.object(
                split_audio, "probe_duration_seconds", return_value=1.0
            ), mock.patch.object(
                split_audio, "load_speaker_names_with_migration", return_value=({}, "missing")
            ), mock.patch.object(
                split_audio, "OUT_DIR", output
            ), mock.patch.object(
                split_audio, "WAV_DIR", wav
            ), mock.patch.object(
                backend, "CrisperWhisperBackend", SeededAdapter
            ), mock.patch.object(
                backend, "resolve_crisperwhisper_settings", return_value=settings
            ):
                with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "preflight"):
                    split_audio.run_pipeline()
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].seed_calls, 1)
        self.assertEqual(instances[0].probe_calls, 0)

    def test_about_diagnostics_available_and_unavailable(self):
        available = "\n".join(
            gui.format_crisper_diagnostics({"response": make_probe_response(), "error": None})
        )
        unavailable = "\n".join(
            gui.format_crisper_diagnostics({"response": None, "error": "CUDA is unavailable"})
        )
        self.assertIn("CrisperWhisper: 2.0.2", available)
        self.assertIn("CUDA available: True", available)
        self.assertIn("small, medium, large, turbo", available)
        self.assertIn("unavailable", unavailable)
        self.assertIn("CUDA is unavailable", unavailable)
        original = "Header\n\nCrisperWhisper isolated runtime:\n  Status: not checked yet\n\nFooter"
        refreshed = gui.refresh_crisper_diagnostics_text(
            original,
            {"response": make_probe_response(), "error": None},
        )
        self.assertTrue(refreshed.startswith("Header"))
        self.assertTrue(refreshed.endswith("Footer"))
        self.assertIn("CrisperWhisper: 2.0.2", refreshed)


class OptionalTkRegressionTests(unittest.TestCase):
    def test_four_pages_navigation_hidden_logging_and_default_whisperx(self):
        try:
            root = gui.tb.Window(themename="litera")
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk is unavailable: {exc}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                conf = Path(temporary_directory) / "conf.yaml"
                with mock.patch.object(gui, "conf_path", return_value=conf):
                    app = gui.App(root)
                    self.assertEqual(tuple(app.pages), ("transcribe", "review", "activity", "settings"))
                    self.assertEqual(app.var_transcription_backend.get(), "whisperx")
                    app.show_page("settings")
                    app.log("hidden activity message")
                    self.assertIn("hidden activity message", app.txt.get("1.0", "end"))
                    app.show_page("activity")
                    app.show_page("transcribe")
                    app.close_application()
        finally:
            try:
                if root.winfo_exists():
                    root.destroy()
            except gui.tk.TclError:
                pass


if __name__ == "__main__":
    unittest.main()
