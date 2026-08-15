from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest import mock

import result_catalog
import result_storage
import split_audio
import split_audio_gui


class ResultStorageFixture:
    def __init__(self, root: Path):
        self.root = root
        self.output = root / "output"
        self.output.mkdir()

    def source(self, relative="media/source.wav", content=b"source"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    @staticmethod
    def write_result_files(output_root: Path, title="Shared title"):
        (output_root / "segments.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "source_path": "C:/synthetic/source.wav",
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": "Hello.",
                            "speaker": "SPEAKER_00",
                            "words": [
                                {"word": "Hello.", "start": 0.0, "end": 1.0}
                            ],
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (output_root / "speakers.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "diarization": True,
                    "speakers": ["SPEAKER_00"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (output_root / f"{title}.srt").write_text("subtitle", encoding="utf-8")
        (output_root / f"{title}.txt").write_text("transcript", encoding="utf-8")

    def commit(
        self,
        layout,
        engine,
        result_id,
        *,
        model,
        mode=None,
        execution_backend=None,
    ):
        with result_storage.ResultRevision(
            layout,
            engine,
            result_id=result_id,
        ) as revision:
            self.write_result_files(revision.output_root, layout.display_title)
            return revision.commit(
                model=model,
                mode=mode,
                execution_backend=execution_backend,
                validate_outputs=lambda output: result_catalog.preflight_result_pair(
                    output / "speakers.json",
                    output / "segments.json",
                ),
            )


class ProjectIdentityAndRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.fixture = ResultStorageFixture(self.root)

    def test_project_identity_is_canonical_and_collision_suffix_extends(self):
        source = self.fixture.source()
        layout = result_storage.project_layout_for_source(
            self.fixture.output,
            "Readable title",
            source,
        )
        digest = result_storage.source_identity_hash(layout.source_identity)
        self.assertEqual(layout.project_id, f"sha256:{digest}")
        self.assertEqual(layout.project_root.name, f"Readable title--{digest[:12]}")

        layout.project_root.mkdir()
        (layout.project_root / "project.json").write_text("{malformed", encoding="utf-8")
        collided = result_storage.project_layout_for_source(
            self.fixture.output,
            "Readable title",
            source,
        )
        self.assertEqual(collided.project_root.name, f"Readable title--{digest[:16]}")

    def test_same_title_different_source_and_modified_source_get_new_projects(self):
        first = self.fixture.source("one/shared.wav", b"first")
        second = self.fixture.source("two/shared.wav", b"second")
        first_layout = result_storage.project_layout_for_source(
            self.fixture.output, "shared", first
        )
        second_layout = result_storage.project_layout_for_source(
            self.fixture.output, "shared", second
        )
        self.assertNotEqual(first_layout.project_root, second_layout.project_root)
        self.assertNotEqual(first_layout.project_id, second_layout.project_id)

        first.write_bytes(b"modified source at the same path")
        modified_layout = result_storage.project_layout_for_source(
            self.fixture.output, "shared", first
        )
        self.assertNotEqual(first_layout.project_root, modified_layout.project_root)
        self.assertNotEqual(first_layout.project_id, modified_layout.project_id)

    def test_engines_coexist_reprocessing_adds_revisions_and_updates_only_active_engine(self):
        source = self.fixture.source()
        layout = result_storage.project_layout_for_source(
            self.fixture.output, "Shared title", source
        )
        whisper_one = self.fixture.commit(
            layout,
            "whisperx",
            "20260814T120000000000Z-00000001",
            model="large-v3",
            execution_backend="ctranslate2",
        )
        whisper_snapshot = {
            path.relative_to(whisper_one): path.read_bytes()
            for path in whisper_one.rglob("*")
            if path.is_file()
        }
        project_path = layout.project_root / "project.json"
        project_with_future_data = json.loads(project_path.read_text(encoding="utf-8"))
        project_with_future_data["future_catalog_key"] = {"preserve": True}
        project_path.write_text(
            json.dumps(project_with_future_data, indent=2),
            encoding="utf-8",
        )
        crisper = self.fixture.commit(
            layout,
            "crisperwhisper",
            "20260814T120100000000Z-00000002",
            model="medium",
            mode="verbatim",
            execution_backend="transformers",
        )
        whisper_two = self.fixture.commit(
            layout,
            "whisperx",
            "20260814T120200000000Z-00000003",
            model="large-v3",
            execution_backend="ctranslate2",
        )

        self.assertTrue(crisper.is_dir())
        self.assertTrue(whisper_two.is_dir())
        self.assertEqual(
            {
                path.relative_to(whisper_one): path.read_bytes()
                for path in whisper_one.rglob("*")
                if path.is_file()
            },
            whisper_snapshot,
        )
        project_data = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertEqual(project_data["future_catalog_key"], {"preserve": True})
        self.assertEqual(len(project_data["results"]), 3)
        self.assertEqual(
            project_data["active_results"],
            {
                "whisperx": whisper_two.name,
                "crisperwhisper": crisper.name,
            },
        )
        discovery = result_catalog.discover_results(self.fixture.output)
        self.assertEqual(len(discovery.results), 3)
        metadata = {
            (item.engine, item.model, item.mode) for item in discovery.results
        }
        self.assertIn(("whisperx", "large-v3", None), metadata)
        self.assertIn(("crisperwhisper", "medium", "verbatim"), metadata)
        self.assertFalse(list(layout.project_root.glob(".staging-*")))

    def test_failed_validation_and_project_update_roll_back_without_artifacts(self):
        source = self.fixture.source()
        layout = result_storage.project_layout_for_source(
            self.fixture.output, "Rollback", source
        )
        previous = self.fixture.commit(
            layout,
            "whisperx",
            "20260814T121000000000Z-00000001",
            model="large-v3",
        )
        project_before = (layout.project_root / "project.json").read_bytes()
        previous_before = {
            path.relative_to(previous): path.read_bytes()
            for path in previous.rglob("*")
            if path.is_file()
        }

        failed_id = "20260814T121100000000Z-00000002"
        with self.assertRaisesRegex(RuntimeError, "deliberate validation"):
            with result_storage.ResultRevision(
                layout, "whisperx", result_id=failed_id
            ) as revision:
                self.fixture.write_result_files(revision.output_root, "Rollback")
                revision.commit(
                    model="large-v3",
                    mode=None,
                    execution_backend="ctranslate2",
                    validate_outputs=lambda _path: (_ for _ in ()).throw(
                        RuntimeError("deliberate validation failure")
                    ),
                )

        failed_commit_id = "20260814T121200000000Z-00000003"
        with mock.patch.object(
            result_storage,
            "_write_project_update",
            side_effect=RuntimeError("deliberate project update failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "project update"):
                with result_storage.ResultRevision(
                    layout, "crisperwhisper", result_id=failed_commit_id
                ) as revision:
                    self.fixture.write_result_files(revision.output_root, "Rollback")
                    revision.commit(
                        model="medium",
                        mode="verbatim",
                        execution_backend="transformers",
                    )

        self.assertEqual(
            (layout.project_root / "project.json").read_bytes(), project_before
        )
        self.assertEqual(
            {
                path.relative_to(previous): path.read_bytes()
                for path in previous.rglob("*")
                if path.is_file()
            },
            previous_before,
        )
        self.assertFalse((layout.project_root / "whisperx" / failed_id).exists())
        self.assertFalse(
            (layout.project_root / "crisperwhisper" / failed_commit_id).exists()
        )
        self.assertFalse(list(layout.project_root.glob(".staging-*")))

    def test_cancellation_cleans_staging_without_creating_metadata(self):
        source = self.fixture.source()
        layout = result_storage.project_layout_for_source(
            self.fixture.output, "Cancelled", source
        )
        with self.assertRaises(KeyboardInterrupt):
            with result_storage.ResultRevision(
                layout,
                "whisperx",
                result_id="20260814T122000000000Z-00000001",
            ) as revision:
                self.fixture.write_result_files(revision.output_root, "Cancelled")
                raise KeyboardInterrupt
        self.assertFalse(layout.project_root.exists())

    def test_abandoned_pid_staging_is_removed_without_touching_live_run(self):
        dead_process_id = 99999999
        dead_project = self.fixture.output / "dead-project"
        live_project = self.fixture.output / "live-project"
        dead_stage = dead_project / f".staging-p{dead_process_id}-result-dead"
        live_stage = live_project / f".staging-p{os.getpid()}-result-live"
        dead_stage.mkdir(parents=True)
        live_stage.mkdir(parents=True)
        (dead_stage / "partial.json").write_text("{}", encoding="utf-8")
        (live_stage / "partial.json").write_text("{}", encoding="utf-8")

        with mock.patch.object(
            result_storage,
            "_process_is_running",
            side_effect=lambda process_id: process_id == os.getpid(),
        ):
            removed = result_storage.cleanup_abandoned_staging(self.fixture.output)
        self.assertIn(dead_stage.resolve(), removed)
        self.assertFalse(dead_stage.exists())
        self.assertTrue(live_stage.exists())

    def test_live_process_staging_is_not_removed(self):
        project_root = self.fixture.output / "live-process-project"
        staging_root = project_root / f".staging-p{os.getpid()}-result-live"
        staging_root.mkdir(parents=True)
        (staging_root / "partial.json").write_text("{}", encoding="utf-8")

        self.assertTrue(result_storage._process_is_running(os.getpid()))
        removed = result_storage.cleanup_process_staging(
            self.fixture.output,
            os.getpid(),
        )
        self.assertEqual(removed, ())
        self.assertTrue(staging_root.exists())

    def test_result_sidecar_remains_discoverable_without_valid_project_index(self):
        source = self.fixture.source()
        layout = result_storage.project_layout_for_source(
            self.fixture.output, "Sidecar", source
        )
        result_root = self.fixture.commit(
            layout,
            "crisperwhisper",
            "20260814T123000000000Z-00000001",
            model="medium",
            mode="intended",
            execution_backend="transformers",
        )
        project_path = layout.project_root / "project.json"
        project_path.unlink()
        missing_discovery = result_catalog.discover_results(self.fixture.output)
        self.assertEqual(len(missing_discovery.results), 1)
        self.assertEqual(missing_discovery.results[0].segments_json.parent, result_root)

        project_path.write_text("{malformed", encoding="utf-8")
        malformed_discovery = result_catalog.discover_results(self.fixture.output)
        self.assertEqual(len(malformed_discovery.results), 1)
        self.assertTrue(malformed_discovery.issues)


class IdentitySafeWavCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cache = self.root / "cache"
        self.conversions = []

    def converter(self, source: Path, destination: Path):
        self.conversions.append(source)
        destination.write_bytes(b"RIFF" + source.read_bytes())

    def test_verified_reuse_and_same_stem_sources_never_collide(self):
        first = self.root / "one" / "shared.mp4"
        second = self.root / "two" / "shared.mp4"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"first source")
        second.write_bytes(b"second source")

        first_wav, reused = result_storage.create_or_reuse_cached_wav(
            first, self.cache, "shared", self.converter
        )
        self.assertFalse(reused)
        reused_wav, reused = result_storage.create_or_reuse_cached_wav(
            first, self.cache, "shared", self.converter
        )
        self.assertTrue(reused)
        self.assertEqual(reused_wav, first_wav)
        second_wav, reused = result_storage.create_or_reuse_cached_wav(
            second, self.cache, "shared", self.converter
        )
        self.assertFalse(reused)
        self.assertNotEqual(first_wav, second_wav)
        self.assertEqual(len(self.conversions), 2)
        self.assertTrue(first_wav.with_suffix(".source.json").is_file())
        self.assertTrue(second_wav.with_suffix(".source.json").is_file())

    def test_missing_corrupt_wrong_source_and_changed_wav_regenerate(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        wav, _ = result_storage.create_or_reuse_cached_wav(
            source, self.cache, "source", self.converter
        )
        sidecar = wav.with_suffix(".source.json")

        sidecar.unlink()
        result_storage.create_or_reuse_cached_wav(
            source, self.cache, "source", self.converter
        )
        sidecar.write_text("{malformed", encoding="utf-8")
        result_storage.create_or_reuse_cached_wav(
            source, self.cache, "source", self.converter
        )
        sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
        sidecar_data["source_identity"]["size"] += 1
        sidecar.write_text(json.dumps(sidecar_data), encoding="utf-8")
        result_storage.create_or_reuse_cached_wav(
            source, self.cache, "source", self.converter
        )
        wav.write_bytes(b"corrupted cached wav")
        result_storage.create_or_reuse_cached_wav(
            source, self.cache, "source", self.converter
        )
        self.assertEqual(len(self.conversions), 5)
        verified, reason = result_storage.verify_cached_wav(source, wav, sidecar)
        self.assertTrue(verified, reason)

    def test_legacy_stem_cache_is_not_reused_for_conversion_or_discovery(self):
        source = self.root / "input" / "sample.mp4"
        source.parent.mkdir()
        source.write_bytes(b"source")
        self.cache.mkdir()
        legacy = self.cache / "sample.wav"
        legacy.write_bytes(b"legacy unverified")

        new_wav, reused = result_storage.create_or_reuse_cached_wav(
            source, self.cache, "sample", self.converter
        )
        self.assertFalse(reused)
        self.assertNotEqual(new_wav, legacy)

        with mock.patch.object(split_audio, "INPUT_DIR", self.root / "empty"), mock.patch.object(
            split_audio, "WAV_DIR", self.cache
        ):
            discovered = split_audio.discover_sources()
        self.assertIn(legacy.resolve(), discovered)
        self.assertNotIn(new_wav.resolve(), discovered)


class ApplyManifestUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.fixture = ResultStorageFixture(self.root)

    def test_review_apply_manifest_updates_follow_staged_json_fingerprints(self):
        source = self.fixture.source()
        layout = result_storage.project_layout_for_source(
            self.fixture.output, "Apply", source
        )
        result_root = self.fixture.commit(
            layout,
            "whisperx",
            "20260814T124000000000Z-00000001",
            model="large-v3",
            execution_backend="ctranslate2",
        )
        staged_speakers = self.root / "staged-speakers.json"
        staged_segments = self.root / "staged-segments.json"
        staged_speakers.write_text(
            json.dumps(
                {
                    "title": "Apply",
                    "speakers": ["SPEAKER_00"],
                    "names": {"SPEAKER_00": "Saved Name"},
                }
            ),
            encoding="utf-8",
        )
        staged_segments.write_bytes((result_root / "segments.json").read_bytes())

        updates = dict(
            result_storage.build_apply_manifest_updates(
                result_root,
                staged_speakers,
                staged_segments,
            )
        )
        expected_speakers_hash = hashlib.sha256(
            staged_speakers.read_bytes()
        ).hexdigest()
        self.assertEqual(
            updates[result_root / "result.json"]["fingerprint"]["speakers"],
            expected_speakers_hash,
        )
        project_update = updates[layout.project_root / "project.json"]
        self.assertEqual(
            project_update["results"][0]["fingerprint"]["speakers"],
            expected_speakers_hash,
        )
        (result_root / "speakers.json").write_bytes(staged_speakers.read_bytes())
        for target, data in updates.items():
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        reopened = result_catalog.descriptor_from_json_pair(
            result_root / "speakers.json",
            result_root / "segments.json",
        )
        self.assertEqual(reopened.layout, "project")
        saved = json.loads(
            reopened.speakers_json.read_text(encoding="utf-8")
        )["names"]
        self.assertEqual(saved, {"SPEAKER_00": "Saved Name"})


class PipelineStorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.output = self.root / "output"
        self.wav_cache = self.root / "wav-cache"
        self.output.mkdir()
        self.wav_cache.mkdir()

    @staticmethod
    def transcription_result(label="Transcript"):
        return {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": label,
                    "words": [{"word": label, "start": 0.0, "end": 1.0}],
                }
            ],
            "__diar_ok__": False,
        }

    def _run(self, sources, *, cached_wav=None):
        cfg = split_audio.Conf(
            diarize=False,
            slice_audio=False,
            slice_video=False,
            transcription_backend="whisperx",
        )
        conversion = mock.patch.object(split_audio, "convert_to_wav16k")
        convert_mock = conversion.start()
        self.addCleanup(conversion.stop)
        if cached_wav is not None:
            convert_mock.return_value = cached_wav
        with mock.patch.object(
            split_audio, "load_conf", return_value=(cfg, None)
        ), mock.patch.object(
            split_audio, "setup_device", return_value="cuda"
        ), mock.patch.object(
            split_audio, "discover_sources", return_value=list(sources)
        ), mock.patch.object(
            split_audio, "probe_duration_seconds", return_value=1.0
        ), mock.patch.object(
            split_audio, "load_speaker_names_with_migration", return_value=({}, "missing")
        ), mock.patch.object(
            split_audio,
            "transcribe_configured_backend",
            side_effect=lambda *_args, **_kwargs: (self.transcription_result(), None),
        ), mock.patch.object(
            split_audio, "OUT_DIR", self.output
        ), mock.patch.object(
            split_audio, "WAV_DIR", self.wav_cache
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                split_audio.run_pipeline()
        return output.getvalue(), convert_mock

    def test_original_video_identity_is_used_when_transcribing_cached_wav(self):
        video = self.root / "video" / "shared.mp4"
        video.parent.mkdir()
        video.write_bytes(b"original video")
        cached_wav = self.wav_cache / "verified-cache.wav"
        cached_wav.write_bytes(b"cached audio")

        _output, convert_mock = self._run([video], cached_wav=cached_wav)
        convert_mock.assert_called_once_with(video, self.wav_cache)
        project_paths = list(self.output.glob("shared--*/project.json"))
        self.assertEqual(len(project_paths), 1)
        project = json.loads(project_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(
            project["source"]["identity"],
            split_audio.build_source_identity(video),
        )
        self.assertEqual(project["source"]["last_known_path"], str(video.resolve()))
        result_root = next(project_paths[0].parent.glob("whisperx/*"))
        segments = json.loads(
            (result_root / "segments.json").read_text(encoding="utf-8")
        )
        self.assertEqual(segments["source_path"], str(video.resolve()))

    def test_direct_wav_skips_conversion_and_multifile_paths_are_emitted_in_order(self):
        first = self.root / "one" / "first.wav"
        second = self.root / "two" / "second.wav"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"first wav")
        second.write_bytes(b"second wav")

        output, convert_mock = self._run([first, second])
        convert_mock.assert_not_called()
        speakers_lines = [
            line.removeprefix("[speakers-json] ")
            for line in output.splitlines()
            if line.startswith("[speakers-json] ")
        ]
        self.assertEqual(len(speakers_lines), 2)
        self.assertIn("first--", speakers_lines[0])
        self.assertIn("second--", speakers_lines[1])
        for line in speakers_lines:
            descriptor = result_catalog.descriptor_from_json_pair(
                Path(line),
                Path(line).with_name("segments.json"),
                pending=True,
            )
            self.assertEqual(descriptor.engine, "whisperx")
            self.assertTrue(descriptor.pending)

    def test_new_revision_has_no_stale_exports_or_speaker_folders(self):
        source = self.root / "source.wav"
        source.write_bytes(b"source")
        self._run([source])
        first_result = next(self.output.glob("source--*/whisperx/*"))
        (first_result / "source.words.ass").write_text("old export", encoding="utf-8")
        stale_speaker = first_result / "SPEAKER_00"
        stale_speaker.mkdir()
        (stale_speaker / "0001.wav").write_bytes(b"old cut")

        self._run([source])
        results = sorted(first_result.parent.iterdir())
        self.assertEqual(len(results), 2)
        second_result = next(result for result in results if result != first_result)
        self.assertFalse((second_result / "source.words.ass").exists())
        self.assertFalse((second_result / "SPEAKER_00").exists())

    def test_clear_output_preserves_gitkeep_and_persistent_speaker_names(self):
        output = self.root / "data" / "output"
        persistent = self.root / "data" / "speaker_names"
        output.mkdir(parents=True)
        persistent.mkdir(parents=True)
        (output / ".gitkeep").write_text("", encoding="utf-8")
        (output / "project").mkdir()
        (output / "project" / "result.json").write_text("{}", encoding="utf-8")
        record = persistent / "identity.yaml"
        record.write_text("speaker_names: {}\n", encoding="utf-8")
        app = object.__new__(split_audio_gui.App)
        app.log = mock.Mock()

        with mock.patch.object(
            split_audio_gui, "output_root", return_value=output
        ), mock.patch.object(
            split_audio_gui.messagebox, "askyesno", return_value=True
        ):
            app.on_clear_output()
        self.assertEqual([path.name for path in output.iterdir()], [".gitkeep"])
        self.assertEqual(record.read_text(encoding="utf-8"), "speaker_names: {}\n")

    def test_missing_saved_source_is_not_replaced_by_same_basename_media(self):
        result_root = self.root / "legacy-result"
        result_root.mkdir()
        missing_source = self.root / "missing" / "shared.mp4"
        nearby_substitute = result_root / "shared.mp4"
        nearby_substitute.write_bytes(b"wrong media")
        (result_root / "segments.json").write_text(
            json.dumps(
                {
                    "title": "shared",
                    "source_path": str(missing_source.resolve()),
                    "segments": [],
                }
            ),
            encoding="utf-8",
        )
        (result_root / "speakers.json").write_text(
            json.dumps({"title": "shared", "speakers": []}),
            encoding="utf-8",
        )
        srt_path = result_root / "shared.srt"
        srt_path.write_text("", encoding="utf-8")
        self.assertIsNone(
            split_audio_gui.guess_media_for_srt(srt_path, self.root)
        )

    def test_gui_process_completion_cleans_only_finished_process_staging(self):
        app = object.__new__(split_audio_gui.App)
        app.queue = queue.Queue()
        app.queue.put(("process_finished", 1, 4242))
        app.proc = mock.Mock()
        app.cancel_requested = False
        app.log = mock.Mock()
        app._set_runtime_state = mock.Mock()
        app.after = mock.Mock()
        removed = self.root / "removed-stage"
        with mock.patch.object(
            split_audio_gui,
            "cleanup_process_staging",
            return_value=(removed,),
        ) as cleanup, mock.patch.object(
            split_audio_gui, "output_root", return_value=self.output
        ):
            app._poll_queue()
        cleanup.assert_called_once_with(
            self.output,
            4242,
            require_stopped=True,
        )
        self.assertIsNone(app.proc)
        app._set_runtime_state.assert_called_once_with("Failed")
        app.after.assert_called_once_with(200, app._poll_queue)


if __name__ == "__main__":
    unittest.main()
