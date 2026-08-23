from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest import mock

import result_catalog as catalog
import split_audio_gui as gui


def incomplete_coverage():
    return {
        "coverage_complete": False,
        "remaining_speech_active_gap_count": 2,
        "remaining_speech_active_gap_duration": 9.5,
        "remaining_speech_active_gap_ranges": [
            {
                "start": 10.0,
                "end": 14.0,
                "duration": 4.0,
                "active_seconds": 2.0,
                "active_blocks": 2,
            },
            {
                "start": 20.0,
                "end": 25.5,
                "duration": 5.5,
                "active_seconds": 3.0,
                "active_blocks": 3,
            },
        ],
        "selected_strategy": "continuation_plus_targeted_recovery",
    }


class ResultFixture:
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source.wav"
        self.source.write_bytes(b"synthetic source")

    @staticmethod
    def _write_json(path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def legacy(self, folder_name="legacy", *, title="Shared title", transcription=None):
        result_dir = self.root / "output" / folder_name
        speakers_path = result_dir / "speakers.json"
        segments_path = result_dir / "segments.json"
        speakers = {"title": title, "speakers": ["SPEAKER_00"]}
        segments = {
            "title": title,
            "source_path": str(self.source.resolve()),
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Hello.",
                    "speaker": "SPEAKER_00",
                    "words": [{"word": "Hello.", "start": 0.0, "end": 1.0}],
                }
            ],
        }
        if transcription is not None:
            segments["transcription"] = transcription
        self._write_json(speakers_path, speakers)
        self._write_json(segments_path, segments)
        return speakers_path, segments_path

    def project_result(
        self,
        *,
        project_name="Shared title--0123456789ab",
        engine="crisperwhisper",
        revision="20260813T120000Z-abcdef12",
        title="Shared title",
        model="medium",
        mode="verbatim",
        execution_backend="transformers",
        status="complete",
        coverage=None,
    ):
        project_root = self.root / "output" / project_name
        result_root = project_root / engine / revision
        speakers_path = result_root / "speakers.json"
        segments_path = result_root / "segments.json"
        source_identity = catalog.build_source_identity(self.source)
        self._write_json(
            speakers_path,
            {"title": title, "speakers": ["SPEAKER_00"]},
        )
        self._write_json(
            segments_path,
            {
                "title": title,
                "source_path": str(self.source.resolve()),
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "Hello.",
                        "speaker": "SPEAKER_00",
                        "words": [{"word": "Hello.", "start": 0.0, "end": 1.0}],
                    }
                ],
            },
        )
        speakers_hash = hashlib.sha256(speakers_path.read_bytes()).hexdigest()
        segments_hash = hashlib.sha256(segments_path.read_bytes()).hexdigest()
        project_id = "sha256:" + "1" * 64
        timestamps = {
            "created_at": "2026-08-13T12:00:00Z",
            "updated_at": "2026-08-13T12:01:00Z",
        }
        result_manifest = {
            "schema_version": 1,
            "project_id": project_id,
            "result_id": revision,
            "display_title": title,
            "engine": engine,
            "model": model,
            "mode": mode,
            "execution_backend": execution_backend,
            "status": status,
            "source": {
                "identity": source_identity,
                "last_known_path": str(self.source.resolve()),
            },
            **timestamps,
            "paths": {
                "speakers": "speakers.json",
                "segments": "segments.json",
            },
            "fingerprint": {
                "algorithm": "sha256",
                "speakers": speakers_hash,
                "segments": segments_hash,
            },
            "comparison_job_id": None,
        }
        if coverage is not None:
            result_manifest["coverage"] = coverage
        result_manifest_path = result_root / "result.json"
        self._write_json(result_manifest_path, result_manifest)
        relative_root = result_root.relative_to(project_root).as_posix()
        project_manifest = {
            "schema_version": 1,
            "project_id": project_id,
            "display_title": title,
            "source": {
                "identity": source_identity,
                "last_known_path": str(self.source.resolve()),
            },
            **timestamps,
            "active_results": {engine: revision},
            "results": [
                {
                    "result_id": revision,
                    "engine": engine,
                    "model": model,
                    "mode": mode,
                    "execution_backend": execution_backend,
                    "status": status,
                    **timestamps,
                    "paths": {
                        "root": relative_root,
                        "metadata": f"{relative_root}/result.json",
                        "speakers": f"{relative_root}/speakers.json",
                        "segments": f"{relative_root}/segments.json",
                    },
                    "fingerprint": {
                        "algorithm": "sha256",
                        "speakers": speakers_hash,
                        "segments": segments_hash,
                    },
                    "comparison_job_id": None,
                }
            ],
        }
        project_manifest_path = project_root / "project.json"
        self._write_json(project_manifest_path, project_manifest)
        return {
            "project_root": project_root,
            "result_root": result_root,
            "project_manifest": project_manifest_path,
            "result_manifest": result_manifest_path,
            "speakers": speakers_path,
            "segments": segments_path,
        }


class ResultCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.fixture = ResultFixture(self.root)

    def test_legacy_whisperx_is_classified_without_inventing_model(self):
        speakers, segments = self.fixture.legacy("whisper-result")
        descriptor = catalog.descriptor_from_json_pair(speakers, segments)
        self.assertEqual(descriptor.layout, "legacy")
        self.assertEqual(descriptor.engine, "whisperx")
        self.assertIsNone(descriptor.model)
        self.assertIsNone(descriptor.mode)
        self.assertEqual(descriptor.source_state, "unverified")

    def test_legacy_crisperwhisper_metadata_is_preserved(self):
        speakers, segments = self.fixture.legacy(
            "crisper-result",
            transcription={
                "engine": "crisperwhisper",
                "model": {"family": "medium", "model_id": "nyralabs/model"},
                "execution_backend": "transformers",
                "effective_settings": {"transcription": {"mode": "verbatim"}},
            },
        )
        descriptor = catalog.descriptor_from_json_pair(speakers, segments)
        self.assertEqual(descriptor.engine, "crisperwhisper")
        self.assertEqual(descriptor.model, "medium")
        self.assertEqual(descriptor.mode, "verbatim")
        self.assertEqual(descriptor.execution_backend, "transformers")

    def test_preflight_rejects_malformed_and_structurally_invalid_json(self):
        speakers, segments = self.fixture.legacy("invalid")
        speakers.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(catalog.ResultPreflightError):
            catalog.preflight_result_pair(speakers, segments)

        self.fixture._write_json(speakers, {"speakers": "SPEAKER_00"})
        with self.assertRaises(catalog.ResultPreflightError):
            catalog.preflight_result_pair(speakers, segments)

        self.fixture._write_json(speakers, {"speakers": ["SPEAKER_00"]})
        self.fixture._write_json(segments, {"segments": [{"words": "not a list"}]})
        with self.assertRaises(catalog.ResultPreflightError):
            catalog.preflight_result_pair(speakers, segments)

    def test_source_states_do_not_prevent_transcript_discovery(self):
        speakers, segments = self.fixture.legacy("source-state")
        segment_data = json.loads(segments.read_text(encoding="utf-8"))
        segment_data["source_identity"] = catalog.build_source_identity(self.fixture.source)
        self.fixture._write_json(segments, segment_data)

        self.assertEqual(
            catalog.descriptor_from_json_pair(speakers, segments).source_state,
            "available",
        )
        self.fixture.source.write_bytes(b"modified source with a different size")
        self.assertEqual(
            catalog.descriptor_from_json_pair(speakers, segments).source_state,
            "changed",
        )
        self.fixture.source.unlink()
        missing = catalog.descriptor_from_json_pair(speakers, segments)
        self.assertEqual(missing.source_state, "missing")
        self.assertEqual(missing.display_title, "Shared title")

    def test_valid_project_layout_is_discovered_and_validated(self):
        paths = self.fixture.project_result()
        project = catalog.validate_project_manifest(paths["project_manifest"])
        result = catalog.validate_result_manifest(paths["result_manifest"])
        discovery = catalog.discover_results(self.root / "output")
        self.assertEqual(project.project_id, result.project_id)
        self.assertEqual(len(discovery.results), 1)
        descriptor = discovery.results[0]
        self.assertEqual(descriptor.layout, "project")
        self.assertEqual(descriptor.engine, "crisperwhisper")
        self.assertEqual(descriptor.model, "medium")
        self.assertEqual(descriptor.source_state, "available")
        self.assertFalse(discovery.issues)

    def test_incomplete_project_coverage_survives_validation_and_discovery(self):
        coverage = incomplete_coverage()
        paths = self.fixture.project_result(
            status="incomplete",
            coverage=coverage,
        )
        manifest = catalog.validate_result_manifest(paths["result_manifest"])
        descriptor = catalog.descriptor_from_json_pair(
            paths["speakers"], paths["segments"]
        )
        discovered = catalog.discover_results(self.root / "output").results

        self.assertEqual(manifest.status, "incomplete")
        self.assertFalse(manifest.coverage.coverage_complete)
        self.assertEqual(descriptor.status, "incomplete")
        self.assertEqual(
            descriptor.coverage.remaining_speech_active_gap_duration,
            coverage["remaining_speech_active_gap_duration"],
        )
        self.assertEqual(discovered, (descriptor,))

    def test_incomplete_project_requires_valid_coverage_metadata(self):
        paths = self.fixture.project_result(
            status="incomplete",
            coverage=incomplete_coverage(),
        )
        data = json.loads(paths["result_manifest"].read_text(encoding="utf-8"))
        data["coverage"]["remaining_speech_active_gap_ranges"] = []
        self.fixture._write_json(paths["result_manifest"], data)
        with self.assertRaises(catalog.ManifestValidationError):
            catalog.validate_result_manifest(paths["result_manifest"])
        discovery = catalog.discover_results(self.root / "output")
        self.assertFalse(discovery.results)
        self.assertTrue(discovery.issues)

    def test_malformed_project_index_does_not_hide_valid_result_sidecar(self):
        paths = self.fixture.project_result()
        paths["project_manifest"].write_text("{not json", encoding="utf-8")
        discovery = catalog.discover_results(self.root / "output")
        self.assertEqual(len(discovery.results), 1)
        self.assertEqual(discovery.results[0].layout, "project")
        self.assertTrue(any(issue.path.name == "project.json" for issue in discovery.issues))

    def test_manifest_paths_reject_traversal_and_absolute_paths(self):
        paths = self.fixture.project_result()
        project_data = json.loads(paths["project_manifest"].read_text(encoding="utf-8"))
        project_data["results"][0]["paths"]["segments"] = "../outside.json"
        self.fixture._write_json(paths["project_manifest"], project_data)
        with self.assertRaisesRegex(catalog.ManifestValidationError, "traversal"):
            catalog.validate_project_manifest(paths["project_manifest"])

        result_data = json.loads(paths["result_manifest"].read_text(encoding="utf-8"))
        result_data["paths"]["speakers"] = str((self.root / "outside.json").resolve())
        self.fixture._write_json(paths["result_manifest"], result_data)
        with self.assertRaisesRegex(catalog.ManifestValidationError, "relative path"):
            catalog.validate_result_manifest(paths["result_manifest"])

    def test_discovery_orders_distinct_same_title_folders_by_modified_time(self):
        older = self.fixture.legacy("same-name-a", title="Identical title")
        newer = self.fixture.legacy("same-name-b", title="Identical title")
        os.utime(older[0], (1000, 1000))
        os.utime(older[1], (1000, 1000))
        os.utime(newer[0], (2000, 2000))
        os.utime(newer[1], (2000, 2000))
        discovery = catalog.discover_results(self.root / "output")
        self.assertEqual(len(discovery.results), 2)
        self.assertEqual(
            [item.speakers_json.parent.name for item in discovery.results],
            ["same-name-b", "same-name-a"],
        )

    def test_pending_descriptor_conversion_is_immutable_and_preferred(self):
        first = self.fixture.legacy("first")
        second = self.fixture.legacy("second")
        pending = catalog.descriptor_from_json_pair(*first, pending=True)
        discovery = catalog.discover_results(
            self.root / "output",
            pending=(pending,),
        )
        self.assertTrue(discovery.results[0].pending)
        self.assertEqual(discovery.results[0].paths, tuple(path.resolve() for path in first))
        self.assertEqual(len(discovery.results), 2)
        with self.assertRaises(AttributeError):
            pending.pending = False

    def test_revalidation_updates_a_changed_fingerprint(self):
        speakers, segments = self.fixture.legacy("changed")
        original = catalog.descriptor_from_json_pair(speakers, segments, pending=True)
        data = json.loads(segments.read_text(encoding="utf-8"))
        data["segments"][0]["text"] = "Changed text."
        self.fixture._write_json(segments, data)
        current = catalog.revalidate_descriptor(original)
        self.assertNotEqual(current.segments_sha256, original.segments_sha256)
        self.assertTrue(current.pending)


class ResultCatalogGuiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.fixture = ResultFixture(self.root)

    def test_open_latest_uses_catalog_order(self):
        older = self.fixture.legacy("older")
        newer = self.fixture.legacy("newer")
        os.utime(older[0], (1000, 1000))
        os.utime(older[1], (1000, 1000))
        os.utime(newer[0], (2000, 2000))
        os.utime(newer[1], (2000, 2000))
        app = object.__new__(gui.App)
        app.pending_review_result = None
        app.log = mock.Mock()
        with mock.patch.object(gui, "output_root", return_value=self.root / "output"):
            latest = app._latest_review_result()
        self.assertIsInstance(latest, catalog.ResultDescriptor)
        self.assertEqual(latest.speakers_json.parent.name, "newer")

    def test_same_descriptor_reuses_workspace_and_player(self):
        speakers, segments = self.fixture.legacy("same")
        descriptor = catalog.descriptor_from_json_pair(speakers, segments)
        page = object.__new__(gui.ReviewNamePage)
        existing_workspace = mock.Mock()
        page.workspace = existing_workspace
        page.current_result_identity = descriptor.file_identity
        page.on_activated = mock.Mock()
        page._report_callback = mock.Mock()
        self.assertTrue(page.load_result(descriptor))
        self.assertIs(page.workspace, existing_workspace)
        page.on_activated.assert_called_once_with()
        existing_workspace.shutdown.assert_not_called()

    def test_invalid_replacement_preserves_active_workspace(self):
        speakers, segments = self.fixture.legacy("current")
        descriptor = catalog.descriptor_from_json_pair(speakers, segments)
        invalid_speakers, invalid_segments = self.fixture.legacy("invalid-replacement")
        invalid_segments.write_text("[]", encoding="utf-8")

        page = object.__new__(gui.ReviewNamePage)
        existing_workspace = mock.Mock()
        page.workspace = existing_workspace
        page.current_result_identity = descriptor.file_identity
        page.current_result_paths = descriptor.paths
        page._report_callback = mock.Mock()
        page.winfo_toplevel = lambda: None
        with mock.patch.object(gui.messagebox, "showerror"):
            self.assertFalse(page.load_result(invalid_speakers, invalid_segments))
        self.assertIs(page.workspace, existing_workspace)
        existing_workspace.shutdown.assert_not_called()

    def test_pending_revalidation_keeps_descriptor_contract(self):
        speakers, segments = self.fixture.legacy("pending")
        app = object.__new__(gui.App)
        app.pending_review_result = catalog.descriptor_from_json_pair(
            speakers,
            segments,
            pending=True,
        )
        app.log = mock.Mock()
        validated = app._validated_pending_review_result()
        self.assertIsInstance(validated, catalog.ResultDescriptor)
        self.assertTrue(validated.pending)

    def test_speakers_queue_event_is_converted_to_pending_descriptor(self):
        speakers, segments = self.fixture.legacy("queue-result")
        app = object.__new__(gui.App)
        app.queue = queue.Queue()
        app.queue.put(("speakers", speakers, segments))
        app.pending_review_result = None
        app.log = mock.Mock()
        app.after = mock.Mock()
        app._poll_queue()
        self.assertIsInstance(app.pending_review_result, catalog.ResultDescriptor)
        self.assertTrue(app.pending_review_result.pending)
        self.assertEqual(app.pending_review_result.paths, (speakers.resolve(), segments.resolve()))
        app.after.assert_called_once_with(200, app._poll_queue)

    def test_incomplete_review_warning_persists_across_activation_and_revert(self):
        paths = self.fixture.project_result(
            status="incomplete",
            coverage=incomplete_coverage(),
        )
        descriptor = catalog.descriptor_from_json_pair(
            paths["speakers"], paths["segments"]
        )
        page = object.__new__(gui.ReviewNamePage)
        page.incomplete_banner = mock.Mock()
        page.lbl_incomplete_warning = mock.Mock()
        page.current_result_descriptor = descriptor
        page.workspace = mock.Mock()

        page._set_incomplete_warning(descriptor)
        warning_text = page.lbl_incomplete_warning.configure.call_args.kwargs["text"]
        self.assertIn("Incomplete CrisperWhisper coverage", warning_text)
        self.assertIn("2 speech-active gaps totaling 9.50 seconds", warning_text)
        page.workspace.revert_unsaved_changes.return_value = True
        self.assertTrue(page.workspace.revert_unsaved_changes())
        page.on_activated()
        page.incomplete_banner.grid_remove.assert_not_called()
        page.workspace.on_host_activated.assert_called_once_with()

    def test_complete_review_has_no_incomplete_warning(self):
        paths = self.fixture.project_result()
        descriptor = catalog.descriptor_from_json_pair(
            paths["speakers"], paths["segments"]
        )
        page = object.__new__(gui.ReviewNamePage)
        page.incomplete_banner = mock.Mock()
        page.lbl_incomplete_warning = mock.Mock()
        page._set_incomplete_warning(descriptor)
        page.incomplete_banner.grid_remove.assert_called_once_with()
        page.lbl_incomplete_warning.configure.assert_not_called()

    def test_incomplete_process_completion_uses_warning_runtime_state(self):
        paths = self.fixture.project_result(
            status="incomplete",
            coverage=incomplete_coverage(),
        )
        descriptor = catalog.descriptor_from_json_pair(
            paths["speakers"], paths["segments"], pending=True
        )
        app = object.__new__(gui.App)
        app.queue = queue.Queue()
        app.queue.put(("process_finished", 0))
        app.proc = mock.Mock()
        app.cancel_requested = False
        app.pending_review_result = descriptor
        app.log = mock.Mock()
        app._set_runtime_state = mock.Mock()
        app._open_completed_review_result = mock.Mock()
        app.after = mock.Mock()

        app._poll_queue()

        app._set_runtime_state.assert_called_once_with("Completed with warnings")
        app._open_completed_review_result.assert_called_once_with()
        app.after.assert_called_once_with(200, app._poll_queue)


if __name__ == "__main__":
    unittest.main()
