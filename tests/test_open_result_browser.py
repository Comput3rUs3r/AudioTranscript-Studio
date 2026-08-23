from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import result_catalog as catalog
import split_audio_gui as gui
from tests.test_result_catalog import ResultFixture, incomplete_coverage


class _FakeDialog:
    selection = None

    def __init__(self, _parent, descriptors, issues=()):
        self.descriptors = tuple(descriptors)
        self.issues = tuple(issues)
        self.result = self.selection(self.descriptors) if self.selection else None


class _FakeTree:
    def __init__(self, item="row-0"):
        self.item = item
        self.selected = ()
        self.focused = None

    def identify_row(self, _y):
        return self.item

    def selection_set(self, item):
        self.selected = (item,)

    def focus(self, item):
        self.focused = item

    def selection(self):
        return self.selected


class _FakeVariable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class OpenResultBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.fixture = ResultFixture(self.root)

    def _descriptor_with_source(self, folder, suffix, *, exists=True):
        speakers, segments = self.fixture.legacy(folder, title=folder)
        source = self.root / f"{folder}{suffix}"
        if exists:
            source.write_bytes(b"synthetic source")
        data = json.loads(segments.read_text(encoding="utf-8"))
        data["source_path"] = str(source.resolve())
        segments.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return catalog.descriptor_from_json_pair(speakers, segments)

    @staticmethod
    def _app_harness(pending=None):
        app = object.__new__(gui.App)
        app.pending_review_result = pending
        app.log = mock.Mock()
        app.review_page = mock.Mock()
        app.review_page.load_result.return_value = True
        app.show_page = mock.Mock()
        app.winfo_toplevel = lambda: None
        app.wait_window = mock.Mock()
        app._current_ner_engine = lambda: "auto"
        return app

    @staticmethod
    def _dialog_returning(selector):
        class Dialog(_FakeDialog):
            selection = staticmethod(selector)

        return Dialog

    def test_legacy_whisperx_and_crisper_results_appear(self):
        self.fixture.legacy("whisper")
        self.fixture.legacy(
            "crisper",
            transcription={
                "engine": "crisperwhisper",
                "model": {"family": "medium"},
                "effective_settings": {"transcription": {"mode": "intended"}},
                "execution_backend": "transformers",
            },
        )
        discovery = catalog.discover_results(self.root / "output")
        model = gui._ResultBrowserModel(discovery.results)
        self.assertEqual(
            {descriptor.engine for descriptor in model.visible()},
            {"whisperx", "crisperwhisper"},
        )
        crisper = model.visible(engine="CrisperWhisper")
        self.assertEqual(len(crisper), 1)
        self.assertEqual(crisper[0].model, "medium")
        self.assertEqual(crisper[0].mode, "intended")

    def test_sorting_and_all_filters(self):
        alpha_paths = self.fixture.legacy("alpha", title="Alpha interview")
        beta_paths = self.fixture.legacy(
            "beta",
            title="Beta discussion",
            transcription={"engine": "crisperwhisper"},
        )
        os.utime(alpha_paths[0], (1000, 1000))
        os.utime(alpha_paths[1], (1000, 1000))
        os.utime(beta_paths[0], (2000, 2000))
        os.utime(beta_paths[1], (2000, 2000))
        descriptors = list(catalog.discover_results(self.root / "output").results)
        pending_alpha = replace(
            next(item for item in descriptors if item.display_title.startswith("Alpha")),
            pending=True,
        )
        descriptors = [
            pending_alpha,
            next(item for item in descriptors if item.display_title.startswith("Beta")),
        ]
        model = gui._ResultBrowserModel(descriptors)

        self.assertEqual(model.visible()[0].display_title, "Alpha interview")
        model.set_sort("title")
        self.assertEqual(
            [item.display_title for item in model.visible()],
            ["Alpha interview", "Beta discussion"],
        )
        model.set_sort("title")
        self.assertEqual(model.visible()[0].display_title, "Alpha interview")
        self.assertEqual(len(model.visible(title="beta")), 1)
        self.assertEqual(len(model.visible(engine="crisperwhisper")), 1)
        self.assertEqual(len(model.visible(source="unverified")), 2)
        self.assertEqual(model.visible(status="pending"), (pending_alpha,))
        self.assertEqual(len(model.visible(status="complete")), 1)

    def test_pending_is_first_and_deduplicated(self):
        first = self.fixture.legacy("first")
        self.fixture.legacy("second")
        pending = catalog.descriptor_from_json_pair(*first, pending=True)
        discovery = catalog.discover_results(
            self.root / "output",
            pending=(pending,),
        )
        self.assertEqual(len(discovery.results), 2)
        self.assertTrue(discovery.results[0].pending)
        self.assertEqual(discovery.results[0].paths, tuple(path.resolve() for path in first))
        self.assertEqual(gui._ResultBrowserModel(discovery.results).visible()[0], discovery.results[0])

    def test_incomplete_status_filter_sort_and_display(self):
        self.fixture.project_result(
            project_name="Incomplete--0123456789ab",
            revision="20260813T120100Z-abcdef12",
            title="Incomplete interview",
            status="incomplete",
            coverage=incomplete_coverage(),
        )
        self.fixture.project_result(
            project_name="Complete--0123456789ab",
            revision="20260813T120000Z-abcdef12",
            title="Complete interview",
        )
        descriptors = catalog.discover_results(self.root / "output").results
        model = gui._ResultBrowserModel(descriptors)
        incomplete = model.visible(status="incomplete")
        complete = model.visible(status="complete")

        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0].status, "incomplete")
        self.assertEqual(model.display_status(incomplete[0]), "Incomplete")
        self.assertEqual(len(complete), 1)
        model.set_sort("status")
        self.assertEqual(
            [item.status for item in model.visible()],
            ["complete", "incomplete"],
        )

    def test_browser_opens_video_audio_and_missing_source_results(self):
        for folder, suffix, exists in (
            ("video", ".mp4", True),
            ("audio", ".wav", True),
            ("missing", ".mp4", False),
        ):
            with self.subTest(folder=folder):
                case_root = self.root / folder
                case_root.mkdir()
                case_fixture = ResultFixture(case_root)
                speakers, segments = case_fixture.legacy(folder, title=folder)
                source = case_root / f"source{suffix}"
                if exists:
                    source.write_bytes(b"source")
                data = json.loads(segments.read_text(encoding="utf-8"))
                data["source_path"] = str(source.resolve())
                segments.write_text(json.dumps(data, indent=2), encoding="utf-8")

                app = self._app_harness()
                dialog_type = self._dialog_returning(lambda descriptors: descriptors[0])
                with mock.patch.object(gui, "output_root", return_value=case_root / "output"), mock.patch.object(
                    gui, "_OpenResultDialog", dialog_type
                ):
                    self.assertTrue(app.on_open_result_browser())
                app.review_page.load_result.assert_called_once_with(
                    speakers.resolve(),
                    segments.resolve(),
                    result_descriptor=mock.ANY,
                )
                app.show_page.assert_called_once_with("review")

    def test_cancel_keeps_pending_and_does_not_create_player_or_workspace(self):
        paths = self.fixture.legacy("pending")
        pending = catalog.descriptor_from_json_pair(*paths, pending=True)
        app = self._app_harness(pending)
        with mock.patch.object(gui, "output_root", return_value=self.root / "output"), mock.patch.object(
            gui, "_OpenResultDialog", _FakeDialog
        ), mock.patch.object(gui, "NamingWorkspace") as workspace, mock.patch.object(
            gui, "get_embedded_vlc_status"
        ) as vlc_status:
            self.assertFalse(app.on_open_result_browser())
        self.assertIs(app.pending_review_result, pending)
        app.review_page.load_result.assert_not_called()
        workspace.assert_not_called()
        vlc_status.assert_not_called()

    def test_successful_pending_open_does_not_clear_pending(self):
        paths = self.fixture.legacy("pending-open")
        pending = catalog.descriptor_from_json_pair(*paths, pending=True)
        app = self._app_harness(pending)
        dialog_type = self._dialog_returning(lambda descriptors: descriptors[0])
        with mock.patch.object(gui, "output_root", return_value=self.root / "output"), mock.patch.object(
            gui, "_OpenResultDialog", dialog_type
        ):
            self.assertTrue(app.on_open_result_browser())
        self.assertIs(app.pending_review_result, pending)

    def test_disappearing_selection_preserves_current_review(self):
        descriptor = self._descriptor_with_source("disappearing", ".wav")
        app = self._app_harness()
        dialog_type = self._dialog_returning(lambda descriptors: descriptors[0])

        def remove_after_selection(_dialog):
            descriptor.segments_json.unlink()

        app.wait_window = remove_after_selection
        with mock.patch.object(gui, "output_root", return_value=self.root / "output"), mock.patch.object(
            gui, "_OpenResultDialog", dialog_type
        ), mock.patch.object(gui.messagebox, "showerror") as showerror:
            self.assertFalse(app.on_open_result_browser())
        app.review_page.load_result.assert_not_called()
        app.show_page.assert_not_called()
        showerror.assert_called_once()
        self.assertTrue(any("could not be opened" in str(call).lower() for call in app.log.call_args_list))

    def test_malformed_results_are_logged_and_cannot_open(self):
        speakers, _segments = self.fixture.legacy("malformed")
        speakers.write_text("{bad json", encoding="utf-8")
        app = self._app_harness()
        with mock.patch.object(gui, "output_root", return_value=self.root / "output"), mock.patch.object(
            gui, "_OpenResultDialog", _FakeDialog
        ):
            self.assertFalse(app.on_open_result_browser())
        app.review_page.load_result.assert_not_called()
        self.assertTrue(any("Skipped invalid result" in str(call) for call in app.log.call_args_list))

    def test_enter_double_click_and_escape_actions(self):
        descriptor = self._descriptor_with_source("actions", ".wav")

        enter_dialog = object.__new__(gui._OpenResultDialog)
        enter_dialog.tree = _FakeTree()
        enter_dialog.tree.selection_set("row-0")
        enter_dialog._row_descriptors = {"row-0": descriptor}
        enter_dialog.result = None
        enter_dialog.destroy = mock.Mock()
        self.assertEqual(enter_dialog._accept_selected(), "break")
        self.assertIs(enter_dialog.result, descriptor)
        enter_dialog.destroy.assert_called_once_with()

        double_dialog = object.__new__(gui._OpenResultDialog)
        double_dialog.tree = _FakeTree()
        double_dialog._row_descriptors = {"row-0": descriptor}
        double_dialog.result = None
        double_dialog.destroy = mock.Mock()
        self.assertEqual(double_dialog._double_click(SimpleNamespace(y=10)), "break")
        self.assertIs(double_dialog.result, descriptor)
        self.assertEqual(double_dialog.tree.focused, "row-0")

        cancel_dialog = object.__new__(gui._OpenResultDialog)
        cancel_dialog.result = descriptor
        cancel_dialog.destroy = mock.Mock()
        self.assertEqual(cancel_dialog._cancel(), "break")
        self.assertIsNone(cancel_dialog.result)
        cancel_dialog.destroy.assert_called_once_with()


class ReviewReplacementProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        fixture = ResultFixture(self.root)
        self.old_descriptor = catalog.descriptor_from_json_pair(
            *fixture.legacy("old-result")
        )
        self.new_descriptor = catalog.descriptor_from_json_pair(
            *fixture.legacy("new-result")
        )

    def _page(self):
        page = object.__new__(gui.ReviewNamePage)
        old_workspace = mock.Mock()
        old_workspace.has_unsaved_changes.return_value = True
        old_workspace.apply_changes.return_value = True
        old_workspace._suspend_embedded_player_for_replacement.return_value = None
        page.workspace = old_workspace
        page.current_result_identity = self.old_descriptor.file_identity
        page.current_result_paths = self.old_descriptor.paths
        page._report_callback = mock.Mock()
        page._back_to_transcribe_callback = mock.Mock()
        page._apply_complete_callback = mock.Mock()
        page.winfo_toplevel = lambda: None
        page.winfo_children = lambda: []
        page.empty_state = mock.Mock()
        return page, old_workspace

    def test_dirty_apply_discard_and_cancel_replacement_paths(self):
        for decision, expect_success, expect_apply in (
            (True, True, True),
            (False, True, False),
            (None, False, False),
        ):
            with self.subTest(decision=decision):
                page, old_workspace = self._page()
                new_workspace = mock.Mock()
                with mock.patch.object(
                    gui.messagebox,
                    "askyesnocancel",
                    return_value=decision,
                ), mock.patch.object(
                    gui,
                    "NamingWorkspace",
                    return_value=new_workspace,
                ) as constructor:
                    result = page.load_result(
                        self.new_descriptor.speakers_json,
                        self.new_descriptor.segments_json,
                    )
                self.assertEqual(result, expect_success)
                self.assertEqual(old_workspace.apply_changes.called, expect_apply)
                if expect_success:
                    constructor.assert_called_once()
                    self.assertIs(page.workspace, new_workspace)
                    new_workspace.start.assert_called_once_with()
                    old_workspace.shutdown.assert_called_once()
                else:
                    constructor.assert_not_called()
                    self.assertIs(page.workspace, old_workspace)
                    old_workspace.shutdown.assert_not_called()

    def test_same_result_reuses_workspace_without_player_creation(self):
        page, old_workspace = self._page()
        with mock.patch.object(gui, "NamingWorkspace") as constructor:
            self.assertTrue(
                page.load_result(
                    self.old_descriptor.speakers_json,
                    self.old_descriptor.segments_json,
                )
            )
        constructor.assert_not_called()
        old_workspace._suspend_embedded_player_for_replacement.assert_not_called()
        old_workspace.shutdown.assert_not_called()


class SavedResultNamesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _named_descriptor(
        self,
        case_name,
        *,
        engine="whisperx",
        media_suffix=".wav",
        source_state="unverified",
        names=None,
    ):
        case_root = self.root / case_name
        case_root.mkdir()
        fixture = ResultFixture(case_root)
        transcription = None
        if engine == "crisperwhisper":
            transcription = {
                "engine": "crisperwhisper",
                "model": {"family": "medium"},
                "execution_backend": "transformers",
            }
        speakers_path, segments_path = fixture.legacy(
            "result",
            title=case_name,
            transcription=transcription,
        )
        speakers_data = json.loads(speakers_path.read_text(encoding="utf-8"))
        speakers_data["speakers"] = ["SPEAKER_00", "SPEAKER_01"]
        speakers_data["names"] = dict(
            names
            or {
                "SPEAKER_00": "Alice Example",
                "SPEAKER_01": "Bob Example",
                "SPEAKER_99": "Stale Name",
            }
        )
        speakers_path.write_text(json.dumps(speakers_data, indent=2), encoding="utf-8")

        source = case_root / f"source{media_suffix}"
        source.write_bytes(b"original source")
        segments_data = json.loads(segments_path.read_text(encoding="utf-8"))
        segments_data["source_path"] = str(source.resolve())
        if source_state != "unverified":
            segments_data["source_identity"] = catalog.build_source_identity(source)
        segments_path.write_text(json.dumps(segments_data, indent=2), encoding="utf-8")
        if source_state == "missing":
            source.unlink()
        elif source_state == "changed":
            source.write_bytes(b"changed source with a different identity")

        descriptor = catalog.descriptor_from_json_pair(speakers_path, segments_path)
        self.assertEqual(descriptor.source_state, source_state)
        return descriptor

    @staticmethod
    def _empty_review_page():
        page = object.__new__(gui.ReviewNamePage)
        page.workspace = None
        page.current_result_identity = None
        page.current_result_paths = None
        page._report_callback = mock.Mock()
        page._back_to_transcribe_callback = mock.Mock()
        page._apply_complete_callback = mock.Mock()
        page.winfo_toplevel = lambda: None
        page.winfo_children = lambda: []
        page.empty_state = mock.Mock()
        return page

    def test_catalog_preflight_keeps_names_for_engines_media_and_source_states(self):
        cases = (
            ("whisper-audio-available", "whisperx", ".wav", "available"),
            ("whisper-video-missing", "whisperx", ".mp4", "missing"),
            ("crisper-audio-changed", "crisperwhisper", ".mp3", "changed"),
            ("crisper-video-unverified", "crisperwhisper", ".mkv", "unverified"),
        )
        expected = {
            "SPEAKER_00": "Alice Example",
            "SPEAKER_01": "Bob Example",
        }
        for case_name, engine, media_suffix, source_state in cases:
            with self.subTest(case=case_name):
                descriptor = self._named_descriptor(
                    case_name,
                    engine=engine,
                    media_suffix=media_suffix,
                    source_state=source_state,
                )
                preflight = catalog.preflight_result_pair(*descriptor.paths)
                self.assertIn("names", preflight.speakers_data)
                self.assertEqual(
                    preflight.speakers_data["names"]["SPEAKER_00"],
                    "Alice Example",
                )
                self.assertEqual(
                    gui._result_local_speaker_names(preflight.speakers_data),
                    expected,
                )

    def test_result_local_names_are_authoritative_and_filtered(self):
        self.assertEqual(
            gui._result_local_speaker_names(
                {
                    "speakers": ["SPEAKER_00", "SPEAKER_01"],
                    "names": {
                        "SPEAKER_00": "  Alice  ",
                        "SPEAKER_99": "Other result",
                    },
                    "name_map": {"SPEAKER_01": "Legacy stale value"},
                }
            ),
            {"SPEAKER_00": "Alice"},
        )
        self.assertEqual(
            gui._result_local_speaker_names(
                {
                    "speakers": ["SPEAKER_00"],
                    "names": {},
                    "name_map": {"SPEAKER_00": "Obsolete fallback"},
                }
            ),
            {},
        )

    def test_workspace_hydration_uses_local_names_without_becoming_dirty(self):
        workspace = object.__new__(gui.NamingWorkspace)
        workspace.speakers = ["SPEAKER_00", "SPEAKER_01"]
        workspace.name_vars = {
            "SPEAKER_00": _FakeVariable("Candidate guess"),
            "SPEAKER_01": _FakeVariable(),
        }
        restored = workspace._set_result_local_speaker_names(
            {
                "SPEAKER_00": "Alice",
                "SPEAKER_99": "Wrong result",
            }
        )
        self.assertEqual(restored, {"SPEAKER_00": "Alice"})
        self.assertEqual(workspace.saved_names, restored)
        self.assertEqual(workspace.name_vars["SPEAKER_00"].get(), "Alice")
        self.assertEqual(workspace.name_vars["SPEAKER_01"].get(), "")

        workspace.inputs = workspace.name_vars
        workspace.name_pool = mock.Mock()
        workspace.name_pool.size.return_value = 0
        workspace.segments = []
        workspace._review_option_variables = lambda: ()
        workspace._clean_baseline = workspace._review_state_snapshot()
        workspace._dirty_tracking_suspended = False
        workspace.manual_corrections_pending = False
        workspace._audio_ass_palette_cache = None
        workspace._audio_caption_render_key = None
        workspace._update_audio_caption = mock.Mock()
        workspace._set_dirty_indicator = mock.Mock()
        self.assertFalse(workspace.has_unsaved_changes())

    def test_direct_descriptor_load_passes_complete_named_preflight_to_workspace(self):
        descriptor = self._named_descriptor(
            "direct-descriptor",
            source_state="missing",
        )
        page = self._empty_review_page()
        captured = {}
        new_workspace = mock.Mock()

        def construct(_master, _speakers, _segments, **kwargs):
            captured["preflight"] = kwargs["result_preflight"]
            captured["descriptor"] = kwargs["result_descriptor"]
            captured["names"] = gui._result_local_speaker_names(
                kwargs["result_preflight"].speakers_data
            )
            return new_workspace

        with mock.patch.object(gui, "NamingWorkspace", side_effect=construct):
            self.assertTrue(page.load_result(descriptor))
        self.assertEqual(
            captured["names"],
            {"SPEAKER_00": "Alice Example", "SPEAKER_01": "Bob Example"},
        )
        self.assertIsNotNone(captured["descriptor"])
        self.assertIn("names", captured["preflight"].speakers_data)

    def test_browser_open_passes_named_result_to_review_load(self):
        descriptor = self._named_descriptor(
            "browser-result",
            engine="crisperwhisper",
            media_suffix=".mp4",
            source_state="changed",
        )
        app = OpenResultBrowserTests._app_harness()
        dialog_type = OpenResultBrowserTests._dialog_returning(
            lambda descriptors: next(
                item for item in descriptors if item.paths == descriptor.paths
            )
        )
        with mock.patch.object(
            gui, "output_root", return_value=self.root / "browser-result" / "output"
        ), mock.patch.object(gui, "_OpenResultDialog", dialog_type):
            self.assertTrue(app.on_open_result_browser())
        call = app.review_page.load_result.call_args
        selected = call.kwargs["result_descriptor"]
        preflight = catalog.preflight_result_pair(*selected.paths)
        self.assertEqual(
            gui._result_local_speaker_names(preflight.speakers_data),
            {"SPEAKER_00": "Alice Example", "SPEAKER_01": "Bob Example"},
        )

    def test_automatic_completion_uses_the_same_named_descriptor(self):
        descriptor = self._named_descriptor(
            "automatic-result",
            engine="crisperwhisper",
            source_state="available",
        )
        app = object.__new__(gui.App)
        app.pending_review_result = descriptor
        app.log = mock.Mock()
        app.review_page = mock.Mock()
        app.review_page.load_result.return_value = True
        app.show_page = mock.Mock()
        app._current_ner_engine = lambda: "auto"

        self.assertTrue(app._open_completed_review_result())
        opened_descriptor = app.review_page.load_result.call_args.args[0]
        preflight = catalog.preflight_result_pair(*opened_descriptor.paths)
        self.assertEqual(
            gui._result_local_speaker_names(preflight.speakers_data),
            {"SPEAKER_00": "Alice Example", "SPEAKER_01": "Bob Example"},
        )
        self.assertIsNone(app.pending_review_result)
        app.show_page.assert_called_once_with("review")

    def test_same_result_reuse_and_page_navigation_keep_names(self):
        descriptor = self._named_descriptor("same-result")
        page = object.__new__(gui.ReviewNamePage)
        workspace = mock.Mock()
        workspace.saved_names = {"SPEAKER_00": "Alice Example"}
        page.workspace = workspace
        page.current_result_identity = descriptor.file_identity
        page.current_result_paths = descriptor.paths
        page._report_callback = mock.Mock()
        page.winfo_toplevel = lambda: None
        with mock.patch.object(gui, "NamingWorkspace") as constructor:
            self.assertTrue(page.load_result(descriptor))
            page.on_activated()
        constructor.assert_not_called()
        self.assertEqual(workspace.saved_names, {"SPEAKER_00": "Alice Example"})
        self.assertEqual(workspace.on_host_activated.call_count, 2)

    def test_revert_restores_result_local_names(self):
        descriptor = self._named_descriptor(
            "revert-names",
            source_state="missing",
        )
        preflight = catalog.preflight_result_pair(*descriptor.paths)
        workspace = object.__new__(gui.NamingWorkspace)
        workspace.speakers = ["SPEAKER_00", "SPEAKER_01"]
        workspace.name_vars = {
            "SPEAKER_00": _FakeVariable("Unsaved replacement"),
            "SPEAKER_01": _FakeVariable(""),
        }
        workspace.inputs = workspace.name_vars
        workspace.saved_names = {}
        workspace.segments_data = {}
        workspace.segments = []
        workspace.manual_corrections_pending = False
        workspace.result_identity = descriptor.file_identity
        workspace._clean_baseline = {"candidate_pool": (), "options": {}}
        workspace._dirty_tracking_suspended = False
        workspace.has_unsaved_changes = lambda: True
        workspace._subtitle_playback_snapshot = lambda: {}
        workspace._restore_subtitle_playback_state = mock.Mock()
        workspace._preflight_current_disk_result = lambda _label: preflight
        workspace._review_option_variables = lambda: ()
        workspace._refresh_transcript_preview = mock.Mock()
        workspace._capture_clean_baseline = mock.Mock()
        workspace.name_pool = mock.Mock()
        workspace.text = mock.Mock()
        workspace.text.yview.return_value = (0.25, 0.75)
        workspace.winfo_toplevel = lambda: None

        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            self.assertTrue(workspace.revert_unsaved_changes())
        self.assertEqual(
            workspace.saved_names,
            {"SPEAKER_00": "Alice Example", "SPEAKER_01": "Bob Example"},
        )
        self.assertEqual(workspace.name_vars["SPEAKER_00"].get(), "Alice Example")
        self.assertEqual(workspace.name_vars["SPEAKER_01"].get(), "Bob Example")
        workspace._refresh_transcript_preview.assert_called_once_with()
        workspace._capture_clean_baseline.assert_called_once_with()

    def test_replacement_receives_each_results_own_names(self):
        first = self._named_descriptor(
            "first-named",
            names={"SPEAKER_00": "First Name"},
        )
        second = self._named_descriptor(
            "second-named",
            names={"SPEAKER_01": "Second Name"},
        )
        page = self._empty_review_page()
        workspaces = []

        def construct(_master, _speakers, _segments, **kwargs):
            workspace = mock.Mock()
            workspace.saved_names = gui._result_local_speaker_names(
                kwargs["result_preflight"].speakers_data
            )
            workspace.has_unsaved_changes.return_value = False
            workspace._suspend_embedded_player_for_replacement.return_value = None
            workspaces.append(workspace)
            return workspace

        with mock.patch.object(gui, "NamingWorkspace", side_effect=construct), mock.patch.object(
            gui.messagebox, "askyesno", return_value=True
        ):
            self.assertTrue(page.load_result(first))
            self.assertTrue(page.load_result(second))
        self.assertEqual(workspaces[0].saved_names, {"SPEAKER_00": "First Name"})
        self.assertEqual(workspaces[1].saved_names, {"SPEAKER_01": "Second Name"})
        workspaces[0].shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
