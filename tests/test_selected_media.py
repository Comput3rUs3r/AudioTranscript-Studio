import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import split_audio_gui as gui


class _Widget:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class SelectedMediaRowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def media(self, relative_path, content=b"fixture"):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_single_and_multiple_rows_preserve_processing_order_and_types(self):
        video = self.media("video/clip.mp4")
        audio = self.media("audio/voice.wav")

        rows = gui.selected_media_rows((video, audio))

        self.assertEqual([row.sequence for row in rows], [1, 2])
        self.assertEqual([row.path for row in rows], [video.resolve(), audio.resolve()])
        self.assertEqual([row.filename for row in rows], ["clip.mp4", "voice.wav"])
        self.assertEqual([row.media_type for row in rows], ["Video", "Audio"])
        self.assertTrue(all(row.issue is None for row in rows))

    def test_long_paths_and_duplicate_basenames_have_distinct_compact_locations(self):
        first = self.media(
            "a-very-long-parent-name/one/distinct-location/shared.wav"
        )
        second = self.media(
            "another-very-long-parent-name/two/distinct-location/shared.wav"
        )

        rows = gui.selected_media_rows((first, second))

        self.assertEqual(rows[0].filename, rows[1].filename)
        self.assertNotEqual(rows[0].parent_location, rows[1].parent_location)
        self.assertLessEqual(len(rows[0].parent_location), 64)
        self.assertLessEqual(len(rows[1].parent_location), 64)
        self.assertNotEqual(rows[0].parent_location, str(first.parent.resolve()))
        self.assertNotEqual(rows[1].parent_location, str(second.parent.resolve()))

    def test_missing_and_unsupported_rows_are_reported_without_reading_media(self):
        missing = self.root / "missing.wav"
        unsupported = self.media("documents/notes.txt", b"not media")

        rows = gui.selected_media_rows((missing, unsupported))

        self.assertEqual(rows[0].media_type, "Audio")
        self.assertEqual(rows[0].issue, "Missing file")
        self.assertEqual(rows[1].media_type, "Unsupported")
        self.assertEqual(rows[1].issue, "Unsupported media type")


class SelectedMediaAppBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.audio = self.root / "first.wav"
        self.video = self.root / "second.mp4"
        self.audio.write_bytes(b"audio")
        self.video.write_bytes(b"video")

    @staticmethod
    def bare_app(paths=()):
        app = object.__new__(gui.App)
        app.input_files = [str(Path(path).resolve()) for path in paths]
        app.log = mock.Mock()
        app.winfo_toplevel = mock.Mock(return_value=None)
        app._refresh_selected_media_display = mock.Mock()
        return app

    def test_file_picker_replaces_selection_and_cancel_keeps_it(self):
        app = self.bare_app((self.audio,))
        with mock.patch.object(
            gui.filedialog,
            "askopenfilenames",
            return_value=(str(self.video), str(self.audio)),
        ):
            gui.App.select_input_files(app)
        self.assertEqual(app.input_files, [str(self.video.resolve()), str(self.audio.resolve())])
        app._refresh_selected_media_display.assert_called_once_with()

        app._refresh_selected_media_display.reset_mock()
        with mock.patch.object(gui.filedialog, "askopenfilenames", return_value=()):
            gui.App.select_input_files(app)
        self.assertEqual(app.input_files, [str(self.video.resolve()), str(self.audio.resolve())])
        app._refresh_selected_media_display.assert_not_called()

    def test_remove_selected_and_clear_selection_update_exact_input_list(self):
        app = self.bare_app((self.audio, self.video))
        app.selected_media_tree = mock.Mock()
        app.selected_media_tree.selection.return_value = ("0",)

        gui.App.remove_selected_media(app)

        self.assertEqual(app.input_files, [str(self.video.resolve())])
        app._refresh_selected_media_display.assert_called_once_with()

        app._refresh_selected_media_display.reset_mock()
        gui.App.clear_input_selection(app)
        self.assertEqual(app.input_files, [])
        app._refresh_selected_media_display.assert_called_once_with()

    def test_missing_or_unsupported_item_blocks_entire_selection(self):
        unsupported = self.root / "notes.txt"
        unsupported.write_text("fixture", encoding="utf-8")
        missing = self.root / "gone.wav"
        app = self.bare_app((self.audio, missing, unsupported))

        with mock.patch.object(gui.messagebox, "showwarning") as warning:
            result = gui.App._validate_selected_media_inputs(app)

        self.assertFalse(result)
        warning.assert_called_once()
        warning_text = warning.call_args.args[1]
        self.assertIn(str(missing.resolve()), warning_text)
        self.assertIn(str(unsupported.resolve()), warning_text)
        self.assertIn("no files were processed", warning_text)

    def test_no_selection_or_no_valid_selection_disables_start(self):
        app = self.bare_app()
        app.btn_start = _Widget()
        app._runtime_status = "Ready"
        gui.App._update_start_button_state(app)
        self.assertEqual(app.btn_start.options["state"], "disabled")

        unsupported = self.root / "notes.txt"
        unsupported.write_text("fixture", encoding="utf-8")
        app.input_files = [str(unsupported.resolve())]
        gui.App._update_start_button_state(app)
        self.assertEqual(app.btn_start.options["state"], "disabled")

        app.input_files = [str(self.audio.resolve())]
        gui.App._update_start_button_state(app)
        self.assertEqual(app.btn_start.options["state"], "normal")

        app._runtime_status = "Running"
        gui.App._update_start_button_state(app)
        self.assertEqual(app.btn_start.options["state"], "disabled")

    def test_launch_command_uses_displayed_order_exactly(self):
        app = self.bare_app((self.video, self.audio))
        app.var_compute = _Value("float16")
        app.var_tf32 = _Value("off")
        app.pending_review_result = mock.sentinel.pending
        app.proc = None
        app._set_runtime_state = mock.Mock()
        context = {
            "backend": "whisperx",
            "whisperx_model": "large-v3",
            "crisper_model": "medium",
            "crisper_mode": "verbatim",
            "workers": 4,
            "input_files": list(app.input_files),
        }
        process = mock.Mock()
        process.stdout = None
        with mock.patch.object(gui.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
            gui.threading,
            "Thread",
        ):
            gui.App._launch_pipeline_process(app, context)

        command = popen.call_args.args[0]
        input_index = command.index("--inputs")
        self.assertEqual(command[input_index + 1 :], app.input_files)
        self.assertIsNone(app.pending_review_result)

    def test_engine_page_and_completion_state_changes_retain_selection(self):
        app = self.bare_app((self.video, self.audio))
        original = list(app.input_files)
        app._update_transcription_engine_controls = mock.Mock()
        gui.App._on_transcription_engine_changed(app)
        self.assertEqual(app.input_files, original)

        page = object()
        app.pages = {"activity": page}
        app.notebook = mock.Mock()
        app.review_page = mock.Mock()
        gui.App.show_page(app, "activity")
        self.assertEqual(app.input_files, original)

        for returncode, cancelled in ((0, False), (1, False), (1, True)):
            with self.subTest(returncode=returncode, cancelled=cancelled):
                run_app = self.bare_app((self.video, self.audio))
                run_app.queue = queue.Queue()
                run_app.queue.put(("process_finished", returncode))
                run_app.proc = mock.Mock()
                run_app.cancel_requested = cancelled
                run_app._set_runtime_state = mock.Mock()
                run_app._open_completed_review_result = mock.Mock()
                run_app.after = mock.Mock()
                gui.App._poll_queue(run_app)
                self.assertEqual(run_app.input_files, original)


if __name__ == "__main__":
    unittest.main()
