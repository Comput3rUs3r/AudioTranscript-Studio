from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import datetime
import io
import json
from pathlib import Path
import queue
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import crisperwhisper_backend as crisper_backend
import crisperwhisper_worker as crisper_worker
import result_catalog
import result_comparison
import split_audio
import split_audio_gui as gui


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _GridWidget:
    def __init__(self):
        self.visible = None
        self.options = None

    def grid(self, **options):
        self.visible = True
        self.options = options

    def grid_remove(self):
        self.visible = False


class _Label:
    def __init__(self):
        self.text = None
        self.options = {}

    def configure(self, **options):
        self.options.update(options)
        self.text = options.get("text", self.text)


class DualEngineGuiTests(unittest.TestCase):
    def test_missing_backend_defaults_to_whisperx_and_both_round_trips(self):
        selection, warnings = gui.crisper_gui_selection_from_conf({})
        self.assertEqual(selection["backend"], "whisperx")
        self.assertEqual(warnings, [])

        selection, warnings = gui.crisper_gui_selection_from_conf(
            {
                "transcription_backend": "both",
                "model": "large-v3",
                "crisperwhisper": {
                    "model": {"family": "turbo"},
                    "transcription": {"mode": "intended"},
                },
            }
        )
        self.assertEqual(selection["backend"], "both")
        self.assertEqual(selection["model"], "turbo")
        self.assertEqual(selection["mode"], "intended")
        self.assertEqual(warnings, [])

        merged = gui.merge_gui_conf(
            {
                "unknown": {"preserve": True},
                "crisperwhisper": {
                    "decoding": {"expert": "keep"},
                    "model": {"device_index": 1},
                },
            },
            {
                "transcription_backend": "both",
                "model": "large-v3",
                "crisperwhisper": gui.crisper_gui_config_update(
                    "turbo", "intended"
                ),
            },
        )
        self.assertEqual(merged["transcription_backend"], "both")
        self.assertEqual(merged["model"], "large-v3")
        self.assertEqual(merged["crisperwhisper"]["model"]["family"], "turbo")
        self.assertEqual(merged["crisperwhisper"]["model"]["device_index"], 1)
        self.assertEqual(merged["crisperwhisper"]["decoding"]["expert"], "keep")
        self.assertEqual(merged["unknown"], {"preserve": True})

    def test_both_displays_both_model_groups_and_single_states_are_unchanged(self):
        app = object.__new__(gui.App)
        app.var_transcription_backend = _Var("both")
        app.whisperx_model_controls = _GridWidget()
        app.crisper_model_controls = _GridWidget()
        app.lbl_whisperx_model = _Label()
        app.lbl_crisper_model = _Label()
        app._crisper_license_acknowledged = True

        app._update_transcription_engine_controls(show_notice=False)
        self.assertTrue(app.whisperx_model_controls.visible)
        self.assertTrue(app.crisper_model_controls.visible)
        self.assertEqual(app.whisperx_model_controls.options["column"], 0)
        self.assertEqual(app.crisper_model_controls.options["column"], 2)
        self.assertEqual(app.lbl_whisperx_model.text, "WhisperX model")
        self.assertEqual(app.lbl_crisper_model.text, "CrisperWhisper model")

        app.var_transcription_backend.set("whisperx")
        app._update_transcription_engine_controls(show_notice=False)
        self.assertTrue(app.whisperx_model_controls.visible)
        self.assertFalse(app.crisper_model_controls.visible)
        self.assertEqual(app.lbl_whisperx_model.text, "Model")

        app.var_transcription_backend.set("crisperwhisper")
        app._update_transcription_engine_controls(show_notice=False)
        self.assertFalse(app.whisperx_model_controls.visible)
        self.assertTrue(app.crisper_model_controls.visible)
        self.assertEqual(app.crisper_model_controls.options["column"], 0)
        self.assertEqual(app.lbl_crisper_model.text, "Model")

    def test_combined_progress_is_monotonic_and_engine_labeled(self):
        app = object.__new__(gui.App)
        app.cancel_requested = False
        app._last_progress = 0
        app._set_progress_display = mock.Mock()
        events = (
            ("whisperx", 1, 0.0, "WhisperX: Preparing input"),
            ("whisperx", 1, 25.0, "WhisperX: File complete"),
            ("whisperx", 2, 25.0, "WhisperX: Preparing input"),
            ("whisperx", 2, 50.0, "WhisperX: File complete"),
            ("crisperwhisper", 1, 50.0, "CrisperWhisper: Preparing input"),
            ("crisperwhisper", 1, 75.0, "CrisperWhisper: File complete"),
            ("crisperwhisper", 2, 75.0, "CrisperWhisper: Preparing input"),
            ("crisperwhisper", 2, 100.0, "CrisperWhisper: File complete"),
        )
        accepted = []
        for engine, file_index, overall, label in events:
            payload = {
                "phase": "fixture",
                "label": label,
                "file_percent": 0 if "Preparing" in label else 100,
                "file_index": file_index,
                "file_total": 2,
                "engine": engine,
                "engine_index": 1 if engine == "whisperx" else 2,
                "engine_total": 2,
                "overall_percent": overall,
            }
            self.assertTrue(
                app._handle_progress_line(
                    gui._PIPELINE_PROGRESS_PREFIX + json.dumps(payload)
                )
            )
            accepted.append(app._last_progress)
        self.assertEqual(accepted, sorted(accepted))
        self.assertEqual(accepted[-1], 99)
        labels = [call.args[0] for call in app._set_progress_display.call_args_list]
        self.assertTrue(all(label.startswith(("WhisperX:", "CrisperWhisper:")) for label in labels))

    def test_review_preference_and_incomplete_protection(self):
        app = object.__new__(gui.App)
        app._active_run_backend = "both"
        whisper = SimpleNamespace(engine="whisperx", status="complete")
        crisper = SimpleNamespace(engine="crisperwhisper", status="complete")
        app._run_review_results = [whisper, crisper]
        self.assertIs(app._select_run_review_result(), whisper)

        pending = SimpleNamespace(
            engine="crisperwhisper",
            status="incomplete",
            file_identity="new-incomplete",
        )
        loaded = SimpleNamespace(
            engine="whisperx",
            status="complete",
            file_identity="loaded-complete",
        )
        app.pending_review_result = pending
        app._validated_pending_review_result = lambda: pending
        app.review_page = SimpleNamespace(
            current_result_descriptor=loaded,
            load_result=mock.Mock(),
        )
        app.log = mock.Mock()
        with mock.patch.object(gui, "ResultDescriptor", SimpleNamespace):
            self.assertFalse(app._open_completed_review_result())
        app.review_page.load_result.assert_not_called()
        self.assertIs(app.pending_review_result, pending)
        self.assertIn("was not replaced automatically", app.log.call_args.args[0])

    def test_loaded_result_display_uses_exact_descriptor_metadata(self):
        created_at = datetime.datetime(2026, 8, 23, 4, 26)
        whisper = SimpleNamespace(
            engine="whisperx",
            model="large-v3",
            mode=None,
            status="complete",
            layout="project",
            created_at=created_at,
            modified_at=None,
        )
        crisper = SimpleNamespace(
            engine="crisperwhisper",
            model="large",
            mode="verbatim",
            status="incomplete",
            layout="project",
            created_at=datetime.datetime(2026, 8, 23, 4, 29),
            modified_at=None,
        )
        app = object.__new__(gui.App)
        app._active_run_backend = "both"
        app._run_review_results = [whisper, crisper]

        selected = app._select_run_review_result()
        whisper_display = gui.review_result_display(selected)
        self.assertEqual(
            whisper_display.text,
            "WhisperX • large-v3 • Complete • Aug 23, 2026 4:26 AM",
        )
        self.assertEqual(whisper_display.status_style, "success")

        crisper_display = gui.review_result_display(crisper)
        self.assertEqual(
            crisper_display.text,
            "CrisperWhisper • large • Verbatim • Incomplete • Aug 23, 2026 4:29 AM",
        )
        self.assertEqual(crisper_display.status_style, "warning")

    def test_legacy_result_display_does_not_invent_model_or_revision(self):
        legacy = SimpleNamespace(
            engine="whisperx",
            model=None,
            mode=None,
            status="complete",
            layout="legacy",
            created_at=None,
            modified_at=None,
        )
        display = gui.review_result_display(legacy)
        self.assertEqual(
            display.text,
            "WhisperX • Model unknown • Legacy result",
        )
        self.assertEqual(display.status_style, "secondary")

    def test_combined_result_display_uses_exact_descriptor_metadata(self):
        descriptor = SimpleNamespace(
            engine="combined",
            model="large-v3 + large",
            mode="User-reviewed fusion",
            status="complete",
            layout="project",
            created_at=datetime.datetime(2026, 8, 23, 16, 26),
            modified_at=None,
        )
        display = gui.review_result_display(descriptor)
        self.assertEqual(
            display.text,
            "Combined | large-v3 + large | User-reviewed fusion | Complete | "
            "Aug 23, 2026 4:26 PM",
        )
        self.assertEqual(display.status_style, "success")

    def test_workspace_metadata_switch_reuse_apply_and_revert_are_descriptor_bound(self):
        whisper_identity = SimpleNamespace(value="whisper-result")
        crisper_identity = SimpleNamespace(value="crisper-result")
        whisper = SimpleNamespace(
            file_identity=whisper_identity,
            engine="whisperx",
            model="large-v3",
            mode=None,
            status="complete",
            layout="project",
            created_at=None,
            modified_at=None,
        )
        crisper = SimpleNamespace(
            file_identity=crisper_identity,
            engine="crisperwhisper",
            model="large",
            mode="verbatim",
            status="incomplete",
            layout="project",
            created_at=None,
            modified_at=None,
        )
        workspace = object.__new__(gui.NamingWorkspace)
        workspace.result_identity = whisper_identity
        workspace.result_descriptor = None
        workspace.lbl_result_metadata_leading = _Label()
        workspace.lbl_result_metadata_status = _Label()
        workspace.lbl_result_metadata_trailing = _Label()
        with mock.patch.object(gui, "ResultDescriptor", SimpleNamespace):
            self.assertTrue(workspace.set_result_descriptor(whisper))
            self.assertEqual(
                workspace.lbl_result_metadata_leading.text,
                "WhisperX • large-v3 • ",
            )
            self.assertEqual(workspace.lbl_result_metadata_status.text, "Complete")
            self.assertEqual(
                workspace.lbl_result_metadata_status.options["bootstyle"],
                "success",
            )

            # Same-result reuse re-applies the same descriptor without deriving
            # anything from current Transcribe controls.
            self.assertTrue(workspace.set_result_descriptor(whisper))
            self.assertIs(workspace.result_descriptor, whisper)

            # A same-source engine revision updates immediately when its exact
            # descriptor becomes the workspace identity.
            workspace.result_identity = crisper.file_identity
            self.assertTrue(workspace.set_result_descriptor(crisper))
            self.assertEqual(
                workspace.lbl_result_metadata_leading.text,
                "CrisperWhisper • large • Verbatim • ",
            )
            self.assertEqual(workspace.lbl_result_metadata_status.text, "Incomplete")
            self.assertEqual(
                workspace.lbl_result_metadata_status.options["bootstyle"],
                "warning",
            )
            self.assertIs(workspace.result_descriptor, crisper)

        # Apply/Revert do not synthesize a new status; retaining the loaded
        # descriptor keeps the exact Incomplete identity and display.
        self.assertEqual(workspace.result_descriptor.status, "incomplete")
        self.assertEqual(workspace.lbl_result_metadata_status.text, "Incomplete")

    def test_apply_refreshes_exact_descriptor_without_changing_incomplete_status(self):
        identity = SimpleNamespace(paths=(Path("speakers.json"), Path("segments.json")))
        refreshed = SimpleNamespace(
            file_identity=identity,
            engine="crisperwhisper",
            model="large",
            mode="verbatim",
            status="incomplete",
        )
        workspace = mock.Mock()
        workspace.result_identity = identity
        page = object.__new__(gui.ReviewNamePage)
        page.workspace = workspace
        page.current_result_descriptor = refreshed
        page._set_incomplete_warning = mock.Mock()
        page._apply_complete_callback = None
        with mock.patch.object(
            gui,
            "descriptor_from_json_pair",
            return_value=refreshed,
        ):
            page._on_workspace_applied()
        self.assertIs(page.current_result_descriptor, refreshed)
        workspace.set_result_descriptor.assert_called_once_with(refreshed)
        page._set_incomplete_warning.assert_called_once_with(refreshed)
        workspace.refresh_available_subtitles.assert_called_once_with()

    def test_reader_converts_engine_result_and_summary_without_logging_machine_lines(self):
        result_payload = {
            "engine": "whisperx",
            "status": "complete",
            "speakers_json": "C:/fixture/speakers.json",
            "segments_json": "C:/fixture/segments.json",
            "file_index": 1,
            "file_total": 1,
        }
        summary_payload = {
            "mode": "both",
            "status": "partial_success",
            "complete": 1,
            "incomplete": 0,
            "result_count": 1,
        }
        lines = "\n".join(
            (
                gui._PIPELINE_RESULT_PREFIX + json.dumps(result_payload),
                gui._PIPELINE_RUN_SUMMARY_PREFIX + json.dumps(summary_payload),
                "human-readable line",
            )
        ) + "\n"
        process = mock.Mock()
        process.stdout = io.StringIO(lines)
        process.wait.return_value = 0
        process.pid = 42
        app = object.__new__(gui.App)
        app.proc = process
        app.queue = queue.Queue()

        app._reader()
        events = []
        while not app.queue.empty():
            events.append(app.queue.get_nowait())
        self.assertIn(("result", result_payload), events)
        self.assertIn(("run_summary", summary_payload), events)
        logged = [event[1] for event in events if event[0] == "log"]
        self.assertIn("human-readable line", logged)
        self.assertFalse(any(line.startswith("@@ATS_RESULT@@") for line in logged))
        self.assertFalse(any(line.startswith("@@ATS_RUN_SUMMARY@@") for line in logged))

    def test_both_probe_failure_is_beginner_friendly_and_starts_no_pipeline(self):
        app = object.__new__(gui.App)
        app._crisper_launch_waiting = False
        app.cancel_requested = False
        app._set_runtime_state = mock.Mock()
        app._set_progress_display = mock.Mock()
        app.log = mock.Mock()
        app.winfo_toplevel = lambda: None
        app._launch_pipeline_process = mock.Mock()
        app._crisper_unavailable_message = lambda error: str(error)
        app._start_crisper_probe = lambda callback, allow_cached=True: callback(
            None, "venv-crisper is incomplete"
        )
        context = {
            "backend": "both",
            "crisper_model": "medium",
            "crisper_mode": "verbatim",
        }
        with mock.patch.object(gui.messagebox, "showerror") as showerror:
            app._begin_crisper_launch(context)
        app._launch_pipeline_process.assert_not_called()
        message = showerror.call_args.args[1]
        self.assertIn("No WhisperX pass was started", message)
        self.assertIn("Select WhisperX", message)


class DualEnginePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.first = self.root / "second.mp4"
        self.second = self.root / "first.wav"
        self.first.write_bytes(b"video fixture")
        self.second.write_bytes(b"wav fixture")
        self.cfg = split_audio.Conf(
            diarize=False,
            slice_audio=False,
            slice_video=False,
            output_format="both",
            transcription_backend="both",
            crisperwhisper_license_acknowledged=True,
            crisperwhisper={},
        )

    def _orchestration_context(self, process_side_effect):
        stack = ExitStack()
        summary = stack.enter_context(
            mock.patch.object(split_audio, "emit_run_summary")
        )
        release = stack.enter_context(
            mock.patch.object(split_audio, "release_gpu_resources")
        )
        process = stack.enter_context(
            mock.patch.object(
                split_audio,
                "_process_source_revision",
                side_effect=process_side_effect,
            )
        )
        stack.enter_context(
            mock.patch.object(split_audio, "load_conf", return_value=(self.cfg, None))
        )
        stack.enter_context(
            mock.patch.object(split_audio, "setup_device", return_value="cuda")
        )
        stack.enter_context(
            mock.patch.object(split_audio, "cleanup_abandoned_staging", return_value=[])
        )
        stack.enter_context(mock.patch.object(split_audio, "preflight_pipeline_locations"))
        stack.enter_context(mock.patch.object(split_audio, "preflight_whisperx_runtime"))
        adapter = mock.Mock()
        stack.enter_context(
            mock.patch.object(
                crisper_backend,
                "CrisperWhisperBackend",
                return_value=adapter,
            )
        )
        stack.enter_context(
            mock.patch.object(
                crisper_backend,
                "resolve_crisperwhisper_settings",
                return_value={"model": {"family": "medium"}, "transcription": {"mode": "verbatim"}},
            )
        )
        return stack, process, release, summary, adapter

    def _revision_outcome(self, engine, source, file_index, status="complete"):
        return split_audio.PipelineRevisionOutcome(
            engine=engine,
            status=status,
            source=source,
            result_dir=self.root / engine / str(file_index),
            file_index=file_index,
            file_total=2,
        )

    def test_passes_are_sequential_and_preserve_exact_media_order(self):
        calls = []
        active = []

        def process(source, **kwargs):
            self.assertEqual(active, [])
            active.append(kwargs["backend_name"])
            calls.append((kwargs["backend_name"], source.resolve()))
            active.pop()
            return self._revision_outcome(
                kwargs["backend_name"], source, kwargs["file_index"]
            )

        stack, _process, release, summary, _adapter = self._orchestration_context(process)
        with stack, redirect_stdout(io.StringIO()):
            split_audio.run_pipeline([str(self.first), str(self.second)])

        expected = [
            ("whisperx", self.first.resolve()),
            ("whisperx", self.second.resolve()),
            ("crisperwhisper", self.first.resolve()),
            ("crisperwhisper", self.second.resolve()),
        ]
        self.assertEqual(calls, expected)
        self.assertEqual(release.call_count, 2)
        summary.assert_called_once()
        self.assertEqual(summary.call_args.args[0], "success")

    def test_pipeline_maps_two_engine_multifile_progress_across_the_whole_job(self):
        captured = []

        def process(source, **kwargs):
            callback = kwargs["progress_callback"]
            callback("preparing_input", "Preparing input", 0)
            callback("file_complete", "File complete", 100)
            return self._revision_outcome(
                kwargs["backend_name"], source, kwargs["file_index"]
            )

        stack, _process, _release, _summary, _adapter = self._orchestration_context(process)

        def capture(phase, label, file_percent, file_index, file_total, **details):
            captured.append(
                (phase, label, file_percent, file_index, file_total, details)
            )

        stack.enter_context(mock.patch.object(split_audio, "emit_progress", side_effect=capture))
        with stack, redirect_stdout(io.StringIO()):
            split_audio.run_pipeline([str(self.first), str(self.second)])

        self.assertEqual(
            [item[5]["overall_percent"] for item in captured],
            [0.0, 25.0, 25.0, 50.0, 50.0, 75.0, 75.0, 100.0],
        )
        self.assertEqual(
            [item[5]["engine"] for item in captured],
            ["whisperx"] * 4 + ["crisperwhisper"] * 4,
        )
        self.assertTrue(
            all(
                item[1].startswith(
                    "WhisperX:" if item[5]["engine"] == "whisperx" else "CrisperWhisper:"
                )
                for item in captured
            )
        )

    def test_whisper_failure_still_runs_crisper_and_reports_partial_success(self):
        calls = []

        def process(source, **kwargs):
            calls.append(kwargs["backend_name"])
            if kwargs["backend_name"] == "whisperx":
                raise RuntimeError("synthetic WhisperX failure")
            return self._revision_outcome("crisperwhisper", source, kwargs["file_index"])

        stack, _process, _release, summary, _adapter = self._orchestration_context(process)
        with stack, redirect_stdout(io.StringIO()):
            split_audio.run_pipeline([str(self.first)])
        self.assertEqual(calls, ["whisperx", "crisperwhisper"])
        self.assertEqual(summary.call_args.args[0], "partial_success")
        self.assertEqual([item.engine for item in summary.call_args.args[1]], ["crisperwhisper"])

    def test_crisper_failure_retains_whisper_and_reports_partial_success(self):
        calls = []

        def process(source, **kwargs):
            calls.append(kwargs["backend_name"])
            if kwargs["backend_name"] == "crisperwhisper":
                raise RuntimeError("synthetic CrisperWhisper failure")
            return self._revision_outcome("whisperx", source, kwargs["file_index"])

        stack, _process, _release, summary, _adapter = self._orchestration_context(process)
        with stack, redirect_stdout(io.StringIO()):
            split_audio.run_pipeline([str(self.first)])
        self.assertEqual(calls, ["whisperx", "crisperwhisper"])
        self.assertEqual(summary.call_args.args[0], "partial_success")
        self.assertEqual([item.engine for item in summary.call_args.args[1]], ["whisperx"])

    def test_complete_plus_incomplete_is_success_with_warnings(self):
        def process(source, **kwargs):
            status = "incomplete" if kwargs["backend_name"] == "crisperwhisper" else "complete"
            return self._revision_outcome(
                kwargs["backend_name"], source, kwargs["file_index"], status
            )

        stack, _process, _release, summary, _adapter = self._orchestration_context(process)
        output = io.StringIO()
        with stack, redirect_stdout(output):
            split_audio.run_pipeline([str(self.first)])
        self.assertEqual(summary.call_args.args[0], "success_with_warnings")
        self.assertIn("1 Complete and 1 Incomplete result", output.getvalue())

    def test_both_failures_raise_overall_failure(self):
        def process(_source, **_kwargs):
            raise RuntimeError("synthetic engine failure")

        stack, _process, _release, summary, _adapter = self._orchestration_context(process)
        with stack, redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            RuntimeError, "failed to produce a usable result"
        ):
            split_audio.run_pipeline([str(self.first)])
        self.assertEqual(summary.call_args.args[0], "failure")
        self.assertEqual(summary.call_args.args[1], [])

    def test_cancellation_during_whisper_prevents_crisper_start(self):
        calls = []

        def process(_source, **kwargs):
            calls.append(kwargs["backend_name"])
            raise KeyboardInterrupt

        stack, _process, release, summary, _adapter = self._orchestration_context(process)
        with stack, redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            split_audio.run_pipeline([str(self.first)])
        self.assertEqual(calls, ["whisperx"])
        self.assertEqual(release.call_count, 1)
        summary.assert_not_called()

    def test_cancellation_during_crisper_follows_committed_whisper(self):
        calls = []

        def process(source, **kwargs):
            calls.append(kwargs["backend_name"])
            if kwargs["backend_name"] == "crisperwhisper":
                raise KeyboardInterrupt
            return self._revision_outcome("whisperx", source, kwargs["file_index"])

        stack, _process, release, summary, _adapter = self._orchestration_context(process)
        with stack, redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            split_audio.run_pipeline([str(self.first)])
        self.assertEqual(calls, ["whisperx", "crisperwhisper"])
        self.assertEqual(release.call_count, 2)
        summary.assert_not_called()

    def test_preflight_probe_failure_starts_no_engine_pass(self):
        process = mock.Mock()
        stack, _patched_process, _release, _summary, adapter = self._orchestration_context(process)
        adapter.probe.side_effect = RuntimeError("isolated runtime unavailable")
        with stack, redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            RuntimeError, "isolated runtime unavailable"
        ):
            split_audio.run_pipeline([str(self.first)])
        process.assert_not_called()


class DualEngineStorageTests(unittest.TestCase):
    def _run_both_timestamp_fixture(self, words, *, duration=2.0):
        root = Path(self.temporary_directory.name)
        output = root / "output"
        wav_cache = root / "wav-cache"
        speaker_names = root / "speaker-names"
        source = root / "same-source.mp4"
        source.write_bytes(b"synthetic media")
        cfg = split_audio.Conf(
            diarize=False,
            slice_audio=False,
            slice_video=False,
            output_format="both",
            transcription_backend="both",
            crisperwhisper_license_acknowledged=True,
            crisperwhisper={},
        )
        observed_before_crisper = []
        project_metadata_before_crisper = []

        def fake_ffmpeg(arguments):
            Path(arguments[-1]).write_bytes(b"verified synthetic wav")

        def fake_transcription(_wav, _device, engine_cfg, **_kwargs):
            if engine_cfg.transcription_backend == "whisperx":
                return {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": " whisper result",
                            "words": [
                                {
                                    "word": " whisper result",
                                    "start": 0.0,
                                    "end": 1.0,
                                }
                            ],
                        }
                    ]
                }, None

            existing = result_catalog.discover_results(output).results
            observed_before_crisper.append(existing)
            project_metadata_before_crisper.append(
                next(output.glob("same-source--*/project.json")).read_bytes()
            )
            normalized, repairs = crisper_worker.normalize_word_timestamps(
                [
                    {"word": word, "start": start, "end": end}
                    for word, start, end in words
                ],
                " ".join(word for word, _start, _end in words),
                duration,
                diagnostic_context={
                    "model_family": "medium",
                    "strategy": "continuation",
                    "chunks": [
                        {
                            "chunk_index": 0,
                            "start": 0.0,
                            "end": duration,
                            "text": " ".join(
                                word for word, _start, _end in words
                            ),
                        }
                    ],
                },
            )
            return {
                "segments": [
                    {
                        "start": normalized[0]["start"],
                        "end": normalized[-1]["end"],
                        "text": " " + " ".join(item["word"] for item in normalized),
                        "words": normalized,
                    }
                ]
            }, {
                "model": {"family": "medium"},
                "requested_settings": {
                    "transcription": {"mode": "verbatim"}
                },
                "execution_backend": "transformers",
                "result_status": "complete",
                "coverage_complete": True,
                "word_timestamp_repairs": repairs,
            }

        adapter = mock.Mock()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(split_audio, "OUT_DIR", output))
            stack.enter_context(mock.patch.object(split_audio, "WAV_DIR", wav_cache))
            stack.enter_context(
                mock.patch.object(split_audio, "SPEAKER_NAMES_DIR", speaker_names)
            )
            stack.enter_context(
                mock.patch.object(split_audio, "load_conf", return_value=(cfg, None))
            )
            stack.enter_context(
                mock.patch.object(split_audio, "setup_device", return_value="cuda")
            )
            stack.enter_context(
                mock.patch.object(split_audio, "preflight_whisperx_runtime")
            )
            stack.enter_context(
                mock.patch.object(split_audio, "probe_duration_seconds", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(split_audio, "run_ffmpeg", side_effect=fake_ffmpeg)
            )
            stack.enter_context(
                mock.patch.object(
                    split_audio,
                    "transcribe_configured_backend",
                    side_effect=fake_transcription,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crisper_backend,
                    "CrisperWhisperBackend",
                    return_value=adapter,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crisper_backend,
                    "resolve_crisperwhisper_settings",
                    return_value={
                        "model": {"family": "medium"},
                        "transcription": {"mode": "verbatim"},
                    },
                )
            )
            with redirect_stdout(io.StringIO()):
                split_audio.run_pipeline([str(source)])

        return output, observed_before_crisper, project_metadata_before_crisper

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    def test_same_chunk_repair_makes_both_revisions_comparison_eligible(self):
        (
            output,
            observed_before_crisper,
            _project_before,
        ) = self._run_both_timestamp_fixture(
            [
                ("private-alpha", 0.98, 1.00),
                ("private-beta", 0.98, 1.005),
                ("private-gamma", 1.015, 1.40),
                ("private-delta", 1.45, 1.80),
            ]
        )

        self.assertEqual(len(observed_before_crisper), 1)
        before = observed_before_crisper[0]
        self.assertEqual([item.engine for item in before], ["whisperx"])
        self.assertEqual(
            result_comparison.compatible_counterparts(before[0], before),
            (),
        )

        results = result_catalog.discover_results(output).results
        self.assertEqual(
            {item.engine for item in results},
            {"whisperx", "crisperwhisper"},
        )
        whisper = next(item for item in results if item.engine == "whisperx")
        counterparts = result_comparison.compatible_counterparts(whisper, results)
        self.assertEqual(len(counterparts), 1)
        self.assertEqual(counterparts[0].engine, "crisperwhisper")

    def test_unsafe_same_chunk_repair_keeps_only_whisper_revision(self):
        output, observed_before_crisper, project_before = (
            self._run_both_timestamp_fixture(
                [
                    ("private-anchor", 0.98, 1.00),
                    *[
                        (
                            f"private-{index}",
                            0.99 + index * 0.005,
                            0.995 + index * 0.005,
                        )
                        for index in range(9)
                    ],
                ],
                duration=3.0,
            )
        )

        self.assertEqual(len(observed_before_crisper), 1)
        results = result_catalog.discover_results(output).results
        self.assertEqual([item.engine for item in results], ["whisperx"])
        self.assertEqual(
            result_comparison.compatible_counterparts(results[0], results),
            (),
        )
        project_dir = next(output.glob("same-source--*"))
        self.assertEqual(list((project_dir / "crisperwhisper").glob("*")), [])
        self.assertEqual(
            (project_dir / "project.json").read_bytes(),
            project_before[0],
        )
        self.assertFalse(any(output.rglob(".staging-*")))

    def test_repeated_both_runs_create_independent_revisions_and_reuse_wav(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output"
            wav_cache = root / "wav-cache"
            speaker_names = root / "speaker-names"
            source = root / "same-source.mp4"
            source.write_bytes(b"synthetic media")
            cfg = split_audio.Conf(
                diarize=False,
                slice_audio=False,
                slice_video=False,
                output_format="both",
                transcription_backend="both",
                crisperwhisper_license_acknowledged=True,
                crisperwhisper={},
            )
            conversion_calls = []

            def fake_ffmpeg(arguments):
                conversion_calls.append(tuple(arguments))
                Path(arguments[-1]).write_bytes(b"verified synthetic wav")

            def fake_transcription(_wav, _device, engine_cfg, **_kwargs):
                result = {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": " hello",
                            "words": [
                                {"word": " hello", "start": 0.0, "end": 1.0}
                            ],
                        }
                    ]
                }
                if engine_cfg.transcription_backend == "whisperx":
                    return result, None
                return result, {
                    "model": {"family": "medium"},
                    "requested_settings": {
                        "transcription": {"mode": "verbatim"}
                    },
                    "execution_backend": "transformers",
                    "result_status": "complete",
                    "coverage_complete": True,
                }

            adapter = mock.Mock()
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(split_audio, "OUT_DIR", output))
                stack.enter_context(mock.patch.object(split_audio, "WAV_DIR", wav_cache))
                stack.enter_context(
                    mock.patch.object(split_audio, "SPEAKER_NAMES_DIR", speaker_names)
                )
                stack.enter_context(
                    mock.patch.object(split_audio, "load_conf", return_value=(cfg, None))
                )
                stack.enter_context(
                    mock.patch.object(split_audio, "setup_device", return_value="cuda")
                )
                stack.enter_context(
                    mock.patch.object(split_audio, "preflight_whisperx_runtime")
                )
                stack.enter_context(
                    mock.patch.object(split_audio, "probe_duration_seconds", return_value=None)
                )
                stack.enter_context(
                    mock.patch.object(split_audio, "run_ffmpeg", side_effect=fake_ffmpeg)
                )
                stack.enter_context(
                    mock.patch.object(
                        split_audio,
                        "transcribe_configured_backend",
                        side_effect=fake_transcription,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        crisper_backend,
                        "CrisperWhisperBackend",
                        return_value=adapter,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        crisper_backend,
                        "resolve_crisperwhisper_settings",
                        return_value={
                            "model": {"family": "medium"},
                            "transcription": {"mode": "verbatim"},
                        },
                    )
                )
                output_log = io.StringIO()
                with redirect_stdout(output_log):
                    split_audio.run_pipeline([str(source)])
                    split_audio.run_pipeline([str(source)])

            catalog = result_catalog.discover_results(output)
            self.assertEqual(catalog.issues, ())
            self.assertEqual(len(catalog.results), 4)
            engines = [descriptor.engine for descriptor in catalog.results]
            self.assertEqual(engines.count("whisperx"), 2)
            self.assertEqual(engines.count("crisperwhisper"), 2)
            self.assertEqual(len({descriptor.result_id for descriptor in catalog.results}), 4)
            self.assertTrue(all(descriptor.status == "complete" for descriptor in catalog.results))
            whisper_results = [item for item in catalog.results if item.engine == "whisperx"]
            crisper_results = [
                item for item in catalog.results if item.engine == "crisperwhisper"
            ]
            self.assertTrue(all(item.model == "large-v3" for item in whisper_results))
            self.assertTrue(all(item.mode is None for item in whisper_results))
            self.assertTrue(all(item.model == "medium" for item in crisper_results))
            self.assertTrue(all(item.mode == "verbatim" for item in crisper_results))
            comparison_groups = {}
            for descriptor in catalog.results:
                self.assertIsNotNone(descriptor.comparison_job_id)
                comparison_groups.setdefault(descriptor.comparison_job_id, set()).add(
                    descriptor.engine
                )
            self.assertEqual(len(comparison_groups), 2)
            self.assertTrue(
                all(
                    engines == {"whisperx", "crisperwhisper"}
                    for engines in comparison_groups.values()
                )
            )
            result_events = [
                json.loads(line[len(split_audio.RESULT_PREFIX):])
                for line in output_log.getvalue().splitlines()
                if line.startswith(split_audio.RESULT_PREFIX)
            ]
            self.assertEqual(len(result_events), 4)
            self.assertEqual(
                [event["engine"] for event in result_events],
                ["whisperx", "crisperwhisper", "whisperx", "crisperwhisper"],
            )
            self.assertEqual(len(conversion_calls), 1)
            self.assertFalse(any(path.name.lower() in {"both", "combined", "hybrid"} for path in output.rglob("*")))


if __name__ == "__main__":
    unittest.main()
