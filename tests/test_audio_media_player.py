from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import result_catalog
import split_audio_gui as gui


class _Var:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Font:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


class _Widget:
    def __init__(self):
        self.config = {}
        self.placed = False
        self.place_options = {}
        self.disabled = False

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def place(self, **kwargs):
        self.placed = True
        self.place_options = dict(kwargs)

    def place_forget(self):
        self.placed = False

    def state(self, states):
        self.disabled = "disabled" in states

    def winfo_exists(self):
        return True

    def winfo_id(self):
        return 123


class _CaptionText(_Widget):
    def __init__(self):
        super().__init__()
        self.content = ""
        self.tags = []
        self.tag_options = {}

    def delete(self, _start, _end):
        self.content = ""
        self.tags = []

    def insert(self, _index, text, tag=None):
        self.content += text
        if tag:
            self.tags.append((tag, None, None))

    def tag_add(self, tag, start, end):
        self.tags.append((tag, start, end))

    def tag_configure(self, tag, **kwargs):
        self.tag_options.setdefault(tag, {}).update(kwargs)

    def update_idletasks(self):
        return None

    def count(self, _start, _end, _option):
        return (max(1, (len(self.content) + 31) // 32),)


class _Media:
    def __init__(self, path):
        self.path = path
        self.release_count = 0

    def release(self):
        self.release_count += 1


class _Player:
    def __init__(self):
        self.media = None
        self.time_ms = 0
        self.length_ms = 10_000
        self.playing = False
        self.paused = False
        self.volume = 80
        self.hwnd_calls = 0
        self.release_count = 0
        self.stop_count = 0
        self.spu_calls = []

    def set_media(self, media):
        self.media = media

    def play(self):
        self.playing = True
        self.paused = False
        return 0

    def pause(self):
        self.playing = False
        self.paused = True

    def stop(self):
        self.stop_count += 1
        self.playing = False
        self.paused = False

    def release(self):
        self.release_count += 1

    def get_time(self):
        return self.time_ms

    def set_time(self, value):
        self.time_ms = int(value)
        return 0

    def get_length(self):
        return self.length_ms

    def is_playing(self):
        return self.playing

    def get_state(self):
        return "Paused" if self.paused else ("Playing" if self.playing else "Stopped")

    def audio_set_volume(self, value):
        self.volume = int(value)

    def audio_get_volume(self):
        return self.volume

    def set_hwnd(self, _hwnd):
        self.hwnd_calls += 1

    def video_set_spu(self, track):
        self.spu_calls.append(track)
        return 0


class _Instance:
    def __init__(self):
        self.created_media = []

    def media_new(self, path):
        media = _Media(path)
        self.created_media.append(media)
        return media


def _base_workspace(source: Path):
    workspace = object.__new__(gui.NamingWorkspace)
    workspace._player_closing = False
    workspace._vlc_status = {"available": True, "instance": object()}
    workspace._vlc_instance = _Instance()
    workspace._vlc_player = _Player()
    workspace._vlc_media = None
    workspace._loaded_video_path = None
    workspace._loaded_media_kind = None
    workspace._audio_caption_render_key = None
    workspace._audio_ass_palette_cache = None
    workspace._audio_caption_font_size = 12
    workspace._source_path = str(source)
    workspace._source_identity = None
    workspace._subtitle_track_ids = {}
    workspace._applied_subtitle_choice = None
    workspace._subtitle_paths = {}
    workspace._subtitle_choices = ["Off", "SRT"]
    workspace._pending_seek_after = None
    workspace._pending_subtitle_after = None
    workspace._apply_player_restore_after = None
    workspace._word_sync_after = None
    workspace._video_reattach_after = None
    workspace._video_update_after = None
    workspace._preview_ratio_after = None
    workspace._seek_dragging = False
    workspace._video_duration_seconds = 0.0
    workspace._word_clock_vlc_seconds = None
    workspace._word_clock_vlc_observed_at = None
    workspace._word_clock_estimate_seconds = None
    workspace._word_clock_seek_target = None
    workspace._word_clock_seek_started_at = None
    workspace._current_word_tag = None
    workspace._timed_word_index = []
    workspace._timed_word_starts = []
    workspace._timed_caption_index = []
    workspace._timed_caption_starts = []
    workspace.video_surface = _Widget()
    workspace.video_message = _Widget()
    workspace.audio_caption = _CaptionText()
    workspace.video_seek_var = _Var(0.0)
    workspace.video_volume_var = _Var(80.0)
    workspace.subtitle_var = _Var("Off")
    workspace.btn_video_play = _Widget()
    workspace.lbl_video_time = _Widget()
    workspace.text = _Widget()
    workspace._cancel_pending_apply_player_restore = mock.Mock()
    workspace._cancel_pending_video_seek = mock.Mock()
    workspace._cancel_pending_subtitle_apply = mock.Mock()
    workspace._cancel_word_synchronization = mock.Mock()
    workspace._clear_current_word = mock.Mock()
    workspace._reset_interpolated_playback_clock = mock.Mock()
    workspace._schedule_video_start_seek = mock.Mock()
    workspace._schedule_selected_subtitle_after_play = mock.Mock()
    workspace._schedule_word_synchronization = mock.Mock()
    workspace._cancel_pending_preview_ratio = mock.Mock()
    workspace._save_name_speakers_view_preferences = mock.Mock()
    workspace.after_cancel = mock.Mock()
    workspace.winfo_exists = lambda: True
    workspace.winfo_toplevel = lambda: None
    return workspace


class AudioMediaPlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _source(self, suffix=".wav"):
        path = self.root / f"source{suffix}"
        path.write_bytes(b"synthetic media")
        return path

    def test_all_pipeline_audio_extensions_are_supported(self):
        for suffix in gui._PIPELINE_AUDIO_EXTS:
            self.assertEqual(gui._supported_media_kind(Path("sample" + suffix)), "audio")
        for suffix in gui._PIPELINE_VIDEO_EXTS:
            self.assertEqual(gui._supported_media_kind(Path("sample" + suffix)), "video")
        self.assertIsNone(gui._supported_media_kind(Path("sample.txt")))

    def test_audio_load_uses_existing_player_without_hwnd_attachment(self):
        source = self._source()
        workspace = _base_workspace(source)
        player = workspace._vlc_player

        with mock.patch.object(workspace, "_update_audio_caption") as update_caption:
            workspace._load_embedded_media(source, 2.5)

        self.assertIs(workspace._vlc_player, player)
        self.assertEqual(workspace._loaded_media_kind, "audio")
        self.assertEqual(player.hwnd_calls, 0)
        self.assertTrue(player.playing)
        self.assertEqual(player.volume, 80)
        update_caption.assert_called_once_with(2.5)
        workspace._schedule_video_start_seek.assert_called_once_with(2.5)

    def test_video_load_retains_hwnd_behavior(self):
        source = self._source(".mp4")
        workspace = _base_workspace(source)
        with mock.patch.object(workspace, "_update_audio_caption"):
            workspace._load_embedded_media(source, 1.0)
        self.assertEqual(workspace._loaded_media_kind, "video")
        if gui.os.name == "nt":
            self.assertEqual(workspace._vlc_player.hwnd_calls, 1)
        self.assertFalse(workspace.audio_caption.placed)

    def test_timed_word_click_loads_audio_without_informational_popup(self):
        source = self._source(".mp3")
        workspace = _base_workspace(source)
        workspace._enable_transcript_following = mock.Mock()
        workspace._load_embedded_media = mock.Mock()
        record = {"media_start": 3.25}

        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            workspace._play_timed_word(record)

        showinfo.assert_not_called()
        workspace._load_embedded_media.assert_called_once_with(source.resolve(), 3.25)

    def test_missing_changed_and_unsupported_sources_remain_transcript_only(self):
        missing = self.root / "missing.wav"
        workspace = _base_workspace(missing)
        path, kind, reason = workspace._source_media_for_playback()
        self.assertIsNone(path)
        self.assertIsNone(kind)
        self.assertIn("missing", reason.lower())

        changed = self._source(".wav")
        workspace = _base_workspace(changed)
        workspace._source_identity = result_catalog.build_source_identity(changed)
        changed.write_bytes(b"replacement media with a different identity")
        path, kind, reason = workspace._source_media_for_playback()
        self.assertIsNone(path)
        self.assertIsNone(kind)
        self.assertIn("changed", reason.lower())

        unsupported = self._source(".txt")
        workspace = _base_workspace(unsupported)
        path, kind, reason = workspace._source_media_for_playback()
        self.assertIsNone(path)
        self.assertIsNone(kind)
        self.assertIn("not a supported", reason.lower())

    def test_audio_play_pause_stop_seek_and_volume_controls(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        player = workspace._vlc_player

        workspace._video_play_pause()
        self.assertTrue(player.playing)
        workspace._video_play_pause()
        self.assertTrue(player.paused)

        workspace._seek_embedded_video(4.0)
        self.assertEqual(player.time_ms, 4000)
        workspace.video_volume_var.set(37)
        workspace._video_volume_changed()
        self.assertEqual(player.volume, 37)

        workspace._video_stop()
        self.assertFalse(player.playing)
        self.assertEqual(workspace.video_seek_var.get(), 0.0)

    def test_audio_back_forward_and_timeline_controls_seek(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        workspace._vlc_player.time_ms = 7000
        workspace._seek_embedded_video = mock.Mock()

        workspace._video_back()
        workspace._seek_embedded_video.assert_called_with(2.0)
        workspace._video_forward()
        workspace._seek_embedded_video.assert_called_with(12.0)
        workspace.video_seek_var.set(4.25)
        workspace._video_seek_released()
        workspace._seek_embedded_video.assert_called_with(4.25)

    def test_audio_ass_karaoke_colors_speaker_style_and_mode_distinction(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_media_kind = "audio"
        workspace.speakers = ["SPEAKER_00"]
        workspace.inputs = {"SPEAKER_00": _Var("Adebayo")}
        workspace.segments = [
            {
                "start": 0.0,
                "end": 3.0,
                "speaker": "SPEAKER_00",
                "text": "Hello, hello world! bientôt",
                "words": [
                    {"word": "Hello,", "start": 0.0, "end": 0.6},
                    {"word": "hello", "start": 0.7, "end": 1.2},
                    {"word": "world!", "start": 1.3, "end": 2.0},
                    {"word": "bientôt"},
                ],
            }
        ]
        workspace._timed_caption_index = [
            {"segment_index": 0, "media_start": 0.0, "media_end": 3.0}
        ]
        workspace._timed_caption_starts = [0.0]

        workspace.subtitle_var.set("ASS (word highlighting)")
        workspace._update_audio_caption(0.8)
        self.assertEqual(workspace.audio_caption.content, "Hello, hello world! bientôt")
        states = {tag[0] for tag in workspace.audio_caption.tags}
        self.assertIn("audio_caption_ass_completed", states)
        self.assertIn("audio_caption_ass_current", states)
        self.assertIn("audio_caption_ass_upcoming", states)
        style_name, rgb = workspace._audio_ass_style_for_segment(workspace.segments[0])
        self.assertEqual(style_name, "Adebayo")
        self.assertEqual(rgb, gui._ASS_SPEAKER_COLORS[0])
        self.assertEqual(
            workspace.audio_caption.tag_options["audio_caption_ass_current"]["foreground"],
            "#FFD166",
        )
        self.assertEqual(
            workspace.audio_caption.tag_options["audio_caption_ass_completed"]["foreground"],
            "#FFD166",
        )
        self.assertEqual(
            workspace.audio_caption.tag_options["audio_caption_ass_upcoming"]["foreground"],
            "#FF0000",
        )
        self.assertEqual(
            workspace.audio_caption.tag_options["audio_caption_ass_current"]["background"],
            gui.MIDNIGHTSTUDIO_TOKENS["video_bg"],
        )

        workspace.subtitle_var.set("ASS (plain)")
        workspace._update_audio_caption(0.8)
        self.assertEqual(workspace.audio_caption.content, "Hello, hello world! bientôt")
        self.assertFalse(
            any("completed" in tag[0] or "current" in tag[0] or "upcoming" in tag[0]
                for tag in workspace.audio_caption.tags)
        )

        workspace.subtitle_var.set("SRT")
        workspace._update_audio_caption(0.8)
        self.assertEqual(
            workspace.audio_caption.content,
            "Adebayo: Hello, hello world! bientôt",
        )
        self.assertTrue(any(tag[0] == "audio_caption_srt" for tag in workspace.audio_caption.tags))

        workspace.subtitle_var.set("Off")
        workspace._update_audio_caption(0.8)
        self.assertEqual(workspace.audio_caption.content.strip(), "Audio playback")

    def test_audio_caption_wraps_unicode_and_stays_bottom_aligned(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_media_kind = "audio"
        workspace.speakers = ["SPEAKER_00"]
        workspace.inputs = {"SPEAKER_00": _Var("")}
        long_text = (
            "你好 — this is a deliberately long caption with punctuation, repeated words, "
            "repeated words, and Unicode café text for wrapping."
        )
        workspace.segments = [
            {
                "start": 0.0,
                "end": 4.0,
                "speaker": "SPEAKER_00",
                "text": long_text,
                "words": [],
            }
        ]
        workspace._timed_caption_index = [
            {"segment_index": 0, "media_start": 0.0, "media_end": 4.0}
        ]
        workspace._timed_caption_starts = [0.0]
        workspace.subtitle_var.set("ASS (plain)")

        workspace._update_audio_caption(1.0)

        self.assertEqual(workspace.audio_caption.content, long_text)
        self.assertGreater(workspace.audio_caption.config["height"], 1)
        self.assertEqual(workspace.audio_caption.place_options["anchor"], "s")
        self.assertEqual(workspace.audio_caption.place_options["rely"], 1.0)

    def test_audio_ass_font_scales_from_ass_play_resolution_without_new_player(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_media_kind = "audio"
        workspace.audio_ass_font = _Font()
        workspace.audio_srt_font = _Font()
        workspace._update_audio_caption = mock.Mock()
        player = workspace._vlc_player

        workspace._on_media_surface_configure(SimpleNamespace(height=540))

        self.assertEqual(workspace.audio_ass_font.options, {"size": 18, "weight": "normal"})
        self.assertEqual(workspace.audio_srt_font.options["size"], 18)
        self.assertIs(workspace._vlc_player, player)
        workspace._update_audio_caption.assert_called_once_with()

    def test_ass_speaker_palette_uses_assigned_name_order(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace.speakers = ["SPEAKER_00", "SPEAKER_01"]
        workspace.inputs = {
            "SPEAKER_00": _Var("Adebayo"),
            "SPEAKER_01": _Var("Mamdani"),
        }
        workspace.segments = [
            {"speaker": "SPEAKER_00", "text": "First"},
            {"speaker": "SPEAKER_01", "text": "Second"},
        ]

        first_style, first_rgb = workspace._audio_ass_style_for_segment(
            workspace.segments[0]
        )
        second_style, second_rgb = workspace._audio_ass_style_for_segment(
            workspace.segments[1]
        )

        self.assertEqual((first_style, first_rgb), ("Adebayo", (255, 209, 102)))
        self.assertEqual((second_style, second_rgb), ("Mamdani", (6, 214, 160)))

    def test_refactored_ass_constants_keep_generated_video_styles_unchanged(self):
        segments = [
            {
                "start": 0.0,
                "end": 1.0,
                "speaker": "SPEAKER_00",
                "text": "Hello world!",
                "words": [
                    {"word": "Hello", "start": 0.0, "end": 0.5},
                    {"word": "world!", "start": 0.5, "end": 1.0},
                ],
            }
        ]
        mapping = {"SPEAKER_00": "Adebayo"}
        word_path = self.root / "sample.words.ass"
        plain_path = self.root / "sample.plain.ass"

        gui.write_word_ass(word_path, segments, mapping)
        gui.write_ass_plain(plain_path, segments, mapping)

        expected_style = (
            "Style: Adebayo,Segoe UI,48,&H0066D1FF,&H000000FF,&H7F000000,"
            "&H00000000,0,0,0,0,100,100,0,0,1,4,0,2,40,40,60,1"
        )
        word_text = word_path.read_text(encoding="utf-8")
        plain_text = plain_path.read_text(encoding="utf-8")
        self.assertIn(expected_style, word_text)
        self.assertIn(expected_style, plain_text)
        self.assertIn(r"{\k50}Hello {\k50}world!", word_text)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Adebayo,,40,40,60,,Hello world!", plain_text)
        self.assertNotIn("Adebayo: Hello world!", word_text)
        self.assertNotIn("Adebayo: Hello world!", plain_text)

    def test_audio_subtitle_switch_never_adds_vlc_slave(self):
        source = self._source()
        subtitle = self.root / "sample.srt"
        subtitle.write_text("synthetic", encoding="utf-8")
        workspace = _base_workspace(source)
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        workspace._subtitle_paths = {"SRT": subtitle}
        workspace.subtitle_var.set("SRT")
        workspace._update_audio_caption = mock.Mock()
        workspace._restore_subtitle_playback_state = mock.Mock()
        workspace._subtitle_playback_snapshot = mock.Mock(return_value=(500, True, False))
        workspace._vlc_player.add_slave = mock.Mock()

        workspace._apply_selected_subtitle()

        workspace._vlc_player.add_slave.assert_not_called()
        self.assertEqual(workspace._applied_subtitle_choice, "SRT")

    def test_switching_all_audio_subtitle_modes_preserves_playback(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        workspace._vlc_player.time_ms = 1750
        workspace._vlc_player.playing = True
        workspace._subtitle_paths = {}
        for choice, suffix in (
            ("SRT", ".srt"),
            ("ASS (plain)", ".plain.ass"),
            ("ASS (word highlighting)", ".words.ass"),
        ):
            path = self.root / ("sample" + suffix)
            path.write_text("synthetic", encoding="utf-8")
            workspace._subtitle_paths[choice] = path
        workspace._update_audio_caption = mock.Mock()
        workspace._vlc_player.add_slave = mock.Mock()

        for choice in (
            "SRT",
            "ASS (plain)",
            "ASS (word highlighting)",
            "Off",
        ):
            workspace.subtitle_var.set(choice)
            workspace._apply_selected_subtitle()
            self.assertEqual(workspace._applied_subtitle_choice, choice)
            self.assertEqual(workspace._vlc_player.time_ms, 1750)
            self.assertTrue(workspace._vlc_player.playing)

        workspace._vlc_player.add_slave.assert_not_called()

    def test_video_subtitle_switch_still_uses_vlc_slave(self):
        source = self._source(".mp4")
        subtitle = self.root / "sample.srt"
        subtitle.write_text("synthetic", encoding="utf-8")
        workspace = _base_workspace(source)
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "video"
        workspace._subtitle_paths = {"SRT": subtitle}
        workspace.subtitle_var.set("SRT")
        workspace._subtitle_playback_snapshot = mock.Mock(return_value=(500, True, False))
        workspace._available_vlc_subtitle_track_ids = mock.Mock(return_value=set())
        workspace._vlc_player.add_slave = mock.Mock(return_value=0)
        workspace.after = mock.Mock(return_value="subtitle-after")
        fake_vlc = SimpleNamespace(MediaSlaveType=SimpleNamespace(subtitle=1))

        with mock.patch.dict(sys.modules, {"vlc": fake_vlc}):
            workspace._apply_selected_subtitle()

        workspace._vlc_player.add_slave.assert_called_once_with(
            1,
            subtitle.resolve().as_uri(),
            True,
        )
        workspace.after.assert_called_once()

    def test_preview_and_external_hit_actions_accept_audio(self):
        source = self._source(".flac")
        workspace = _base_workspace(source)
        srt_path = self.root / "sample.srt"
        chosen = {"start": 1.75, "text": "caption"}
        workspace._select_srt_hit_for_preview = mock.Mock(return_value=(srt_path, chosen))
        workspace._load_embedded_media = mock.Mock()

        with mock.patch.object(gui.messagebox, "showinfo") as showinfo:
            workspace.preview_video_at_query()
        showinfo.assert_not_called()
        workspace._load_embedded_media.assert_called_once_with(source.resolve(), 1.75)

        with mock.patch.object(gui, "read_yaml", return_value={}), mock.patch.object(
            gui, "_open_in_vlc", return_value=True
        ) as open_vlc, mock.patch.object(gui, "_open_in_ffplay") as open_ffplay:
            workspace.open_video_at_query()
        open_vlc.assert_called_once_with(source.resolve(), 1.75, vlc_path=None)
        open_ffplay.assert_not_called()

    def test_synchronization_updates_transcript_word_and_audio_caption_together(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        workspace._vlc_player.time_ms = 500
        workspace._vlc_player.length_ms = 2000
        workspace._vlc_player.playing = True
        record = {"media_start": 0.0, "media_end": 1.0, "tag": "word"}
        workspace._timed_word_index = [record]
        workspace._timed_word_starts = [0.0]
        workspace._set_current_word = mock.Mock()
        workspace._update_audio_caption = mock.Mock()
        workspace._interpolated_playback_time = mock.Mock(return_value=0.55)
        workspace._synchronize_current_word()

        workspace._set_current_word.assert_called_once_with(record)
        workspace._update_audio_caption.assert_called_once_with(0.55)
        workspace._schedule_word_synchronization.assert_called_once_with()

    def test_apply_detach_restore_retains_the_same_audio_player(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._vlc_media = _Media(str(source))
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        workspace._vlc_player.media = workspace._vlc_media
        workspace._vlc_player.time_ms = 2400
        workspace._vlc_player.playing = True
        workspace.subtitle_var.set("SRT")
        player = workspace._vlc_player
        workspace.refresh_available_subtitles = mock.Mock()
        workspace._load_embedded_video = mock.Mock()
        workspace._schedule_apply_player_state_restore = mock.Mock()

        snapshot = workspace._detach_embedded_player_for_apply()
        workspace._restore_embedded_player_after_apply(snapshot)

        self.assertIs(workspace._vlc_player, player)
        workspace._load_embedded_video.assert_called_once_with(source.resolve(), 2.4)
        workspace._schedule_apply_player_state_restore.assert_called_once_with(snapshot)

    def test_cleanup_releases_audio_player_once(self):
        source = self._source()
        workspace = _base_workspace(source)
        workspace._vlc_media = _Media(str(source))
        workspace._loaded_video_path = str(source.resolve())
        workspace._loaded_media_kind = "audio"
        player = workspace._vlc_player
        media = workspace._vlc_media

        workspace._release_embedded_player(save_view_preferences=False)
        workspace._release_embedded_player(save_view_preferences=False)

        self.assertEqual(player.release_count, 1)
        self.assertEqual(media.release_count, 1)
        self.assertIsNone(workspace._loaded_video_path)
        self.assertIsNone(workspace._loaded_media_kind)


if __name__ == "__main__":
    unittest.main()
