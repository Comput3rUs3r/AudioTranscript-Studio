from __future__ import annotations

import copy
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import tempfile
import unittest
from unittest import mock

import crisperwhisper_backend as backend
import split_audio

from test_crisperwhisper_backend import make_response, make_settings


class FakeAdapter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def transcribe(self, audio_path, settings, status_callback=None):
        self.calls.append((Path(audio_path), copy.deepcopy(settings)))
        if status_callback:
            status_callback("loading_model", "Loading CrisperWhisper model.")
            status_callback("transcribing", "Transcribing audio.")
        return copy.deepcopy(self.response)


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.audio = self.root / "sample.wav"
        self.audio.write_bytes(b"synthetic")

    def test_configuration_without_backend_defaults_to_whisperx(self):
        config = self.root / "conf.yaml"
        config.write_text("model: large-v3\nlanguage: en\n", encoding="utf-8")
        loaded, _token = split_audio.load_conf(config)
        self.assertEqual(loaded.transcription_backend, "whisperx")
        self.assertEqual(loaded.crisperwhisper, {})

    def test_nested_crisper_configuration_is_loaded_separately(self):
        config = self.root / "conf.yaml"
        config.write_text(
            "transcription_backend: crisperwhisper\n"
            "crisperwhisper:\n"
            "  schema_version: 1\n"
            "  model:\n"
            "    family: turbo\n"
            "  transcription:\n"
            "    mode: intended\n",
            encoding="utf-8",
        )
        loaded, _token = split_audio.load_conf(config)
        self.assertEqual(loaded.transcription_backend, "crisperwhisper")
        settings = backend.resolve_crisperwhisper_settings(loaded.crisperwhisper, loaded.language)
        self.assertEqual(settings["model"]["model_id"], backend.OFFICIAL_MODEL_IDS["turbo"])
        self.assertEqual(settings["transcription"]["mode"], "intended")

    def test_default_and_explicit_whisperx_use_existing_calls(self):
        expected = {"segments": [{"start": 0.0, "end": 1.0, "text": "WhisperX"}]}
        for selected in (None, "whisperx"):
            with self.subTest(selected=selected):
                cfg = split_audio.Conf(diarize=False)
                if selected is not None:
                    cfg.transcription_backend = selected
                progress = []
                model = object()
                with mock.patch.object(split_audio, "load_asr_model", return_value=model) as load_model, mock.patch.object(
                    split_audio, "transcribe_whisperx", return_value=expected
                ) as transcribe:
                    result, metadata = split_audio.transcribe_configured_backend(
                        self.audio,
                        "cuda",
                        cfg,
                        progress_callback=lambda *args: progress.append(args),
                    )
                self.assertIs(result, expected)
                self.assertIsNone(metadata)
                load_model.assert_called_once_with("cuda", cfg)
                transcribe.assert_called_once_with(
                    model,
                    self.audio,
                    "cuda",
                    cfg,
                    progress_callback=mock.ANY,
                )
                self.assertEqual(progress[0], ("loading_model", "Loading WhisperX model", 12))

    def test_crisper_medium_verbatim_skips_whisperx_transcription_and_alignment(self):
        settings = make_settings()
        response = backend.validate_transcribe_response(make_response(settings), settings)
        adapter = FakeAdapter(response)
        cfg = split_audio.Conf(
            language="en",
            diarize=False,
            transcription_backend="crisperwhisper",
        )
        progress = []
        with mock.patch.object(split_audio, "load_asr_model") as load_model, mock.patch.object(
            split_audio, "transcribe_whisperx"
        ) as transcribe_whisperx:
            result, metadata = split_audio.transcribe_configured_backend(
                self.audio,
                "cuda",
                cfg,
                progress_callback=lambda *args: progress.append(args),
                crisper_backend=adapter,
                crisper_settings=settings,
            )
        load_model.assert_not_called()
        transcribe_whisperx.assert_not_called()
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(result["__diar_ok__"], False)
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(metadata["engine"], "crisperwhisper")
        self.assertIn(
            ("transcribing", "Transcribing with CrisperWhisper medium, verbatim", 25),
            progress,
        )

    def test_intended_mode_is_retained(self):
        settings = make_settings(mode="intended")
        response = backend.validate_transcribe_response(make_response(settings), settings)
        cfg = split_audio.Conf(diarize=False, transcription_backend="crisperwhisper")
        result, metadata = split_audio.transcribe_configured_backend(
            self.audio,
            "cuda",
            cfg,
            crisper_backend=FakeAdapter(response),
            crisper_settings=settings,
        )
        self.assertTrue(result["segments"])
        self.assertEqual(metadata["requested_settings"]["transcription"]["mode"], "intended")

    def test_unknown_backend_has_no_fallback(self):
        cfg = split_audio.Conf(transcription_backend="automatic")
        with mock.patch.object(split_audio, "load_asr_model") as load_model:
            with self.assertRaises(backend.CrisperWhisperConfigurationError):
                split_audio.transcribe_configured_backend(self.audio, "cuda", cfg)
        load_model.assert_not_called()


class DiarizationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audio = Path(self.temp_dir.name) / "sample.wav"
        self.audio.write_bytes(b"synthetic")

    def test_existing_assignment_path_accepts_normalized_words(self):
        settings = make_settings()
        response = backend.validate_transcribe_response(make_response(settings), settings)
        result = {"segments": backend.normalize_worker_result(response)["segments"]}
        calls = []

        whisperx_module = ModuleType("whisperx")

        def assign_word_speakers(timeline, supplied_result):
            self.assertEqual(timeline, "synthetic timeline")
            calls.append(supplied_result)
            for segment in supplied_result["segments"]:
                segment["speaker"] = "SPEAKER_00"
                for word in segment["words"]:
                    word["speaker"] = "SPEAKER_00"
            return supplied_result

        whisperx_module.assign_word_speakers = assign_word_speakers
        diarize_module = ModuleType("whisperx.diarize")

        class FakeDiarizationPipeline:
            def __init__(self, token=None, device=None):
                self.token = token
                self.device = device

            def __call__(self, audio_path, **kwargs):
                return "synthetic timeline"

        diarize_module.DiarizationPipeline = FakeDiarizationPipeline
        cfg = split_audio.Conf(diarize=True, HF_token="fixture-token")
        with mock.patch.dict(
            "sys.modules",
            {"whisperx": whisperx_module, "whisperx.diarize": diarize_module},
        ):
            assigned = split_audio.apply_diarization(result, self.audio, "cuda", cfg)

        self.assertEqual(len(calls), 1)
        self.assertTrue(assigned["__diar_ok__"])
        self.assertTrue(
            all(
                word["speaker"] == "SPEAKER_00"
                for segment in assigned["segments"]
                for word in segment["words"]
            )
        )


class DownstreamCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        settings = make_settings()
        response = backend.validate_transcribe_response(make_response(settings), settings)
        self.normalized = backend.normalize_worker_result(response)
        self.segments = self.normalized["segments"]
        for segment in self.segments:
            segment["speaker"] = "SPEAKER_00"
            for word in segment["words"]:
                word["speaker"] = "SPEAKER_00"

    def test_every_current_exporter_consumes_normalized_result(self):
        import split_audio_gui

        mapping = {"SPEAKER_00": "Speaker Name"}
        outputs = {
            "srt": self.root / "sample.srt",
            "txt": self.root / "sample.txt",
            "vtt": self.root / "sample.words.vtt",
            "word_ass": self.root / "sample.words.ass",
            "plain_ass": self.root / "sample.plain.ass",
            "html": self.root / "word_player.html",
            "lrc": self.root / "sample.lrc",
        }
        split_audio.write_srt(self.segments, outputs["srt"])
        split_audio.write_txt(self.segments, outputs["txt"], diarized=True, include_speakers=True)
        split_audio_gui.write_word_vtt(outputs["vtt"], self.segments, mapping)
        split_audio_gui.write_word_ass(outputs["word_ass"], self.segments, mapping)
        split_audio_gui.write_ass_plain(outputs["plain_ass"], self.segments, mapping)
        split_audio_gui.write_word_player_html(outputs["html"], ["Speaker Name"])
        split_audio_gui.write_lrc(outputs["lrc"], self.segments, mapping)

        for label, path in outputs.items():
            with self.subTest(export=label):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

    def test_review_preflight_and_manual_correction_accept_metadata_and_words(self):
        import split_audio_gui

        segments_path = self.root / "segments.json"
        speakers_path = self.root / "speakers.json"
        segments_path.write_text(
            json.dumps(
                {
                    "title": "sample",
                    "source_path": str(self.root / "source.mp4"),
                    "segments": self.segments,
                    "transcription": self.normalized["transcription"],
                    "unknown_future_key": {"preserved": True},
                }
            ),
            encoding="utf-8",
        )
        speakers_path.write_text(
            json.dumps(
                {
                    "title": "sample",
                    "diarization": True,
                    "speakers": ["SPEAKER_00", "SPEAKER_01"],
                }
            ),
            encoding="utf-8",
        )
        preflight = split_audio_gui._preflight_review_result(speakers_path, segments_path)
        working = copy.deepcopy(preflight.segments_data["segments"])
        changed = split_audio_gui._assign_segment_speaker(working, [0], "SPEAKER_01")
        self.assertTrue(changed)
        self.assertEqual(working[0]["speaker"], "SPEAKER_01")
        self.assertTrue(all(word["speaker"] == "SPEAKER_01" for word in working[0]["words"]))
        self.assertTrue(preflight.segments_data["unknown_future_key"]["preserved"])


class PipelineFailureSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.wav_dir = self.root / "wav"
        self.output_dir = self.root / "output"
        self.wav_dir.mkdir()
        self.output_dir.mkdir()
        self.audio = self.wav_dir / "sample.wav"
        self.audio.write_bytes(b"synthetic")
        self.result_dir = self.output_dir / "sample"
        self.result_dir.mkdir()
        self.old_segments = b'{"old":"segments"}'
        self.old_speakers = b'{"old":"speakers"}'
        (self.result_dir / "segments.json").write_bytes(self.old_segments)
        (self.result_dir / "speakers.json").write_bytes(self.old_speakers)

    def test_worker_failure_does_not_overwrite_previous_valid_json(self):
        cfg = split_audio.Conf(
            diarize=False,
            slice_audio=False,
            slice_video=False,
            transcription_backend="crisperwhisper",
        )
        settings = backend.resolve_crisperwhisper_settings({}, "en")

        class FailingAdapter:
            def __init__(self, _root):
                pass

            def probe(self):
                return {}

            def transcribe(self, *_args, **_kwargs):
                raise backend.CrisperWhisperRuntimeError("synthetic worker failure")

        with mock.patch.object(split_audio, "load_conf", return_value=(cfg, None)), mock.patch.object(
            split_audio, "setup_device", return_value="cuda"
        ), mock.patch.object(
            split_audio, "discover_sources", return_value=[self.audio]
        ), mock.patch.object(
            split_audio, "probe_duration_seconds", return_value=1.0
        ), mock.patch.object(
            split_audio, "load_speaker_names_with_migration", return_value=({}, "missing")
        ), mock.patch.object(
            split_audio, "OUT_DIR", self.output_dir
        ), mock.patch.object(
            split_audio, "WAV_DIR", self.wav_dir
        ), mock.patch.object(
            backend, "CrisperWhisperBackend", FailingAdapter
        ), mock.patch.object(
            backend, "resolve_crisperwhisper_settings", return_value=settings
        ):
            with self.assertRaisesRegex(backend.CrisperWhisperRuntimeError, "synthetic"):
                split_audio.run_pipeline()

        self.assertEqual((self.result_dir / "segments.json").read_bytes(), self.old_segments)
        self.assertEqual((self.result_dir / "speakers.json").read_bytes(), self.old_speakers)


class PipelineOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.wav_dir = self.root / "wav"
        self.output_dir = self.root / "output"
        self.wav_dir.mkdir()
        self.output_dir.mkdir()
        self.audio = self.wav_dir / "sample.wav"
        self.audio.write_bytes(b"synthetic")

    def pipeline_patches(self, cfg):
        return (
            mock.patch.object(split_audio, "load_conf", return_value=(cfg, None)),
            mock.patch.object(split_audio, "setup_device", return_value="cuda"),
            mock.patch.object(split_audio, "discover_sources", return_value=[self.audio]),
            mock.patch.object(split_audio, "probe_duration_seconds", return_value=4.5),
            mock.patch.object(split_audio, "load_speaker_names_with_migration", return_value=({}, "missing")),
            mock.patch.object(split_audio, "OUT_DIR", self.output_dir),
            mock.patch.object(split_audio, "WAV_DIR", self.wav_dir),
        )

    def test_default_whisperx_pipeline_keeps_original_json_shape_and_filenames(self):
        cfg = split_audio.Conf(
            diarize=False,
            slice_audio=False,
            slice_video=False,
            transcription_backend="whisperx",
        )
        whisper_result = {
            "segments": [
                {
                    "start": 0.1,
                    "end": 1.0,
                    "text": "WhisperX result.",
                    "words": [{"word": "WhisperX", "start": 0.1, "end": 0.7}],
                }
            ],
            "__diar_ok__": False,
        }
        patches = self.pipeline_patches(cfg)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], mock.patch.object(
            split_audio, "load_asr_model", return_value=object()
        ), mock.patch.object(
            split_audio, "transcribe_whisperx", return_value=whisper_result
        ), mock.patch.object(
            backend, "CrisperWhisperBackend"
        ) as crisper_class:
            split_audio.run_pipeline()

        crisper_class.assert_not_called()
        result_dir = self.output_dir / "sample"
        segment_json = json.loads((result_dir / "segments.json").read_text(encoding="utf-8"))
        self.assertEqual(set(segment_json), {"title", "segments", "source_path"})
        self.assertEqual(segment_json["segments"], whisper_result["segments"])
        self.assertTrue((result_dir / "sample.srt").is_file())
        self.assertTrue((result_dir / "sample.txt").is_file())

    def test_crisper_pipeline_adds_metadata_only_after_success(self):
        cfg = split_audio.Conf(
            diarize=False,
            slice_audio=False,
            slice_video=False,
            transcription_backend="crisperwhisper",
        )
        settings = make_settings()
        response = backend.validate_transcribe_response(make_response(settings), settings)

        class SuccessfulAdapter:
            def __init__(self, _root):
                self.response = response

            def probe(self):
                return make_response(settings)

            def transcribe(self, _audio, supplied_settings, status_callback=None):
                self.assert_settings = supplied_settings
                if status_callback:
                    status_callback("transcribing", "Transcribing audio.")
                return copy.deepcopy(self.response)

        patches = self.pipeline_patches(cfg)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], mock.patch.object(
            backend, "CrisperWhisperBackend", SuccessfulAdapter
        ), mock.patch.object(
            backend, "resolve_crisperwhisper_settings", return_value=settings
        ), mock.patch.object(
            split_audio, "load_asr_model"
        ) as load_whisper, mock.patch.object(
            split_audio, "transcribe_whisperx"
        ) as transcribe_whisper:
            split_audio.run_pipeline()

        load_whisper.assert_not_called()
        transcribe_whisper.assert_not_called()
        result_dir = self.output_dir / "sample"
        segment_json = json.loads((result_dir / "segments.json").read_text(encoding="utf-8"))
        metadata = segment_json["transcription"]
        self.assertEqual(metadata["engine"], "crisperwhisper")
        self.assertEqual(metadata["model"]["model_id"], backend.OFFICIAL_MODEL_IDS["medium"])
        self.assertEqual(metadata["execution_backend"], "transformers")
        self.assertNotIn("transcription", json.loads((result_dir / "speakers.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
