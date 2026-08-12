# split_audio_gui.py — v1.11.0 (Stop Button + Worker Control)
import os, sys, stat, json, yaml, queue, shutil, threading, subprocess, tkinter as tk, hashlib, datetime, re, signal, copy, math, time
import ttkbootstrap as tb
from tkinter import ttk, messagebox, filedialog, font as tkfont
from pathlib import Path
from collections import Counter
from bisect import bisect_right
from split_audio import (
    AUDIO_EXTS as _PIPELINE_AUDIO_EXTS,
    PROGRESS_PREFIX as _PIPELINE_PROGRESS_PREFIX,
    VIDEO_EXTS as _PIPELINE_VIDEO_EXTS,
    build_source_identity,
    discover_sources,
    write_speaker_name_record,
)

# === word-level exporters (VTT, ASS, and HTML player) ========================
def _has_word_level(segments):
    for s in segments:
        if isinstance(s, dict) and s.get("words"):
            return True
    return False

def _speaker_display(spk, mapping):
    if not spk:
        return ""
    return mapping.get(spk, spk)

def _vtt_timestamp(t):
    if t is None: t = 0.0
    if t < 0: t = 0.0
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def write_word_vtt(out_path, segments, mapping):
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("WEBVTT\n\n")
        for seg in segments:
            spk = _speaker_display(seg.get("speaker"), mapping)
            words = seg.get("words") or []
            for w in words:
                text = str(w.get("word","")).strip()
                if not text: continue
                start = float(w.get("start", seg.get("start", 0.0)))
                end   = float(w.get("end",   seg.get("end", start)))
                if end < start: end = start
                f.write(_vtt_timestamp(start) + " --> " + _vtt_timestamp(end) + "\n")
                text_safe = text.replace("-->", "⟶")
                if spk:
                    f.write("<v " + spk + ">" + text_safe + "\n\n")
                else:
                    f.write(text_safe + "\n\n")

def _ass_escape(s):
    return s.replace("{", r"\{").replace("}", r"\}")

def _fmt_ass_time(t):
    if t is None: t = 0.0
    if t < 0: t = 0.0
    cs = int(round(t * 100))
    h = cs // 360000; cs %= 360000
    m = cs // 6000; cs %= 6000
    s = cs // 100; cs %= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def _speaker_palette(speakers):
    base = [(255,209,102),(6,214,160),(84,190,255),(255,99,132),(255,140,66),(171,143,255),(255,183,197),(120,220,130)]
    pal = {}
    for i, sp in enumerate(speakers):
        pal[sp] = base[i % len(base)]
    return pal

def write_word_ass(out_path, segments, mapping):
    speakers = []
    for s in segments:
        disp = _speaker_display(s.get("speaker"), mapping)
        if disp and disp not in speakers:
            speakers.append(disp)
    if not speakers:
        speakers = ["Default"]
    palette = _speaker_palette(speakers)

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\nCollisions: Normal\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\nWrapStyle: 0\n")
        f.write("\n[V4+ Styles]\n")
        f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        for sp in speakers:
            r,g,b = palette.get(sp,(255,255,255))
            primary = "&H00%02X%02X%02X" % (b,g,r)
            outline = "&H7F000000"; back="&H00000000"
            f.write(f"Style: {sp},Segoe UI,48,{primary},&H000000FF,{outline},{back},0,0,0,0,100,100,0,0,1,4,0,2,40,40,60,1\n")

        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for seg in segments:
            words = seg.get("words") or []
            if not words:
                continue
            spk = _speaker_display(seg.get("speaker"), mapping) or "Default"
            start = float(words[0].get("start", seg.get("start", 0.0)))
            end   = float(words[-1].get("end", seg.get("end", start)))
            if end < start: end = start

            parts = []
            last_end = None
            for w in words:
                wstart = float(w.get("start", start)); wend = float(w.get("end", wstart))
                if last_end is not None and wstart > last_end:
                    gap_cs = max(1, int(round((wstart - last_end)*100)))
                    parts.append("{\\k%d}" % gap_cs)
                dur_cs = max(1, int(round((wend - wstart)*100)))
                parts.append("{\\k%d}%s " % (dur_cs, _ass_escape(str(w.get("word","")).strip() or " ")))
                last_end = wend
            line = "".join(parts).strip()

            f.write("Dialogue: 0,%s,%s,%s,,40,40,60,,%s\n" % (_fmt_ass_time(start), _fmt_ass_time(end), spk, line))

def write_word_player_html(out_path, speakers):
    pal = _speaker_palette(speakers or [])
    lines = []
    for sp, (r,g,b) in pal.items():
        sp_esc = sp.replace('"','\\"')
        line = '::cue(v[voice="' + sp_esc + '"]) { background: rgba(%d,%d,%d,.28); color: #111; }' % (r,g,b)
        lines.append(line)
    css_rules = "\\n  ".join(lines)

    html = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>Word Player</title>
<style>
  :root{
    --font-size: 1.3rem;
    --active-bg: rgba(255,225,0,.35);
    --active-color: #111;
    --outline: 0 0 6px rgba(0,0,0,.85);
  }
  body{font-family: system-ui,Segoe UI,Arial,sans-serif; margin: 16px;}
  video{width: 100%; max-height: 70vh; background: #000;}
  .row{display:flex; gap:12px; align-items:center; margin: 10px 0;}
  label{font-weight:600;}
  ::cue {
    font-size: var(--font-size);
    color: white;
    text-shadow: var(--outline);
    background: var(--active-bg);
    color: var(--active-color);
    padding: 0 .35em;
    border-radius: .4em;
  }
  __CSS_RULES__
</style>
</head>
<body>
  <h2>Word-level Player</h2>
  <div class="row"><label>Video:</label><input id="vfile" type="file" accept="video/*"/></div>
  <div class="row"><label>Word VTT:</label><input id="sfile" type="file" accept=".vtt"/></div>
  <video id="vid" controls></video>
<script>
const vid = document.getElementById('vid');
const vfile = document.getElementById('vfile');
const sfile = document.getElementById('sfile');
let trackEl = null;

vfile.addEventListener('change', () => {
  const f = vfile.files[0]; if(!f) return;
  const url = URL.createObjectURL(f);
  vid.src = url;
});

sfile.addEventListener('change', () => {
  const f = sfile.files[0]; if(!f) return;
  const url = URL.createObjectURL(f);
  if (trackEl) { vid.removeChild(trackEl); trackEl = null; }
  trackEl = document.createElement('track');
  trackEl.kind = 'subtitles';
  trackEl.label = 'Words';
  trackEl.srclang = 'en';
  trackEl.default = true;
  trackEl.src = url;
  vid.appendChild(trackEl);
});
</script>
</body></html>"""
    html = html.replace("__CSS_RULES__", css_rules)
    Path(out_path).write_text(html, encoding="utf-8")
# === end word-level exporters ================================================

def _lrc_ts(t):
    if t is None: t = 0.0
    if t < 0: t = 0.0
    m = int(t // 60)
    s = int(t % 60)
    hs = int(round((t - int(t)) * 100))
    return f"{m:02d}:{s:02d}.{hs:02d}"

def write_lrc(out_path, segments, mapping):
    lines = []
    lines.append("[re:audiosplitter]")
    for seg in segments:
        start = float(seg.get("start", 0.0))
        base = f"[{_lrc_ts(start)}]"
        words = seg.get("words") or []
        if words:
            parts = []
            for w in words:
                ws = float(w.get("start", start))
                wt = str(w.get("word","")).strip()
                if wt:
                    parts.append(f"<{_lrc_ts(ws)}>{wt}")
            text = " ".join(parts)
        else:
            text = (seg.get("text") or "").replace("\n", " ")
        lines.append(base + text)
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_ass_plain(out_path, segments, mapping):
    speakers = []
    for s in segments:
        disp = _speaker_display(s.get("speaker"), mapping)
        if disp and disp not in speakers:
            speakers.append(disp)
    if not speakers:
        speakers = ["Default"]
    palette = _speaker_palette(speakers)

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\nCollisions: Normal\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\nWrapStyle: 0\n")
        f.write("\n[V4+ Styles]\n")
        f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        for sp in speakers:
            r,g,b = palette.get(sp,(255,255,255))
            primary = "&H00%02X%02X%02X" % (b,g,r)
            outline = "&H7F000000"; back="&H00000000"
            f.write(f"Style: {sp},Segoe UI,48,{primary},&H000000FF,{outline},{back},0,0,0,0,100,100,0,0,1,4,0,2,40,40,60,1\n")

        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for seg in segments:
            spk = _speaker_display(seg.get("speaker"), mapping) or "Default"
            start = float(seg.get("start", 0.0))
            end   = float(seg.get("end", start))
            if end < start: end = start
            text = seg.get("text")
            if not text:
                ws = [str(w.get('word','')).strip() for w in (seg.get('words') or []) if str(w.get('word','')).strip()]
                text = " ".join(ws)
            text = (text or "").replace("\n", " ")
            f.write("Dialogue: 0,%s,%s,%s,,40,40,60,,%s\n" % (_fmt_ass_time(start), _fmt_ass_time(end), spk, _ass_escape(text)))

# === SRT → video jump helpers (VLC preferred, ffplay fallback) ===============
import re as _re_mod
import subprocess as _subproc_mod
from pathlib import Path as _PathMod

_SRT_TIME_RE = _re_mod.compile(
    r"(?:^\s*\d+\s*\n)?\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n(.*?)(?=\n{2,}|\Z)",
    _re_mod.DOTALL | _re_mod.MULTILINE
)

def _srt_to_seconds(hh, mm, ss, ms):
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

def parse_srt_segments(srt_path: _PathMod):
    try:
        text = srt_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = srt_path.read_text(errors="ignore")
    segs = []
    for m in _SRT_TIME_RE.finditer(text):
        sh, sm, ss, sms, eh, em, es, ems, seg_text = m.groups()
        segs.append({
            "start": _srt_to_seconds(sh, sm, ss, sms),
            "end":   _srt_to_seconds(eh, em, es, ems),
            "text":  (seg_text or "").strip()
        })
    return segs

def find_segments_matching_query(segments, query: str):
    q = (query or "").strip().lower()
    return [seg for seg in segments if q and q in (seg.get("text","").lower())]

_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".m4v")

_EMBEDDED_VLC_CHECKED = False
_EMBEDDED_VLC_INSTANCE = None
_EMBEDDED_VLC_VERSION = None
_EMBEDDED_VLC_RUNTIME_PATH = None
_EMBEDDED_VLC_FAILURE_REASON = "Embedded VLC availability has not been checked."
_VLC_DLL_DIRECTORY_HANDLES = {}


def _standard_windows_vlc_directories():
    """Return the standard Windows VLC installation directories, in priority order."""
    candidates = []
    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(env_name)
        if program_files:
            candidates.append(_PathMod(program_files) / "VideoLAN" / "VLC")
    candidates.extend((
        _PathMod(r"C:\Program Files\VideoLAN\VLC"),
        _PathMod(r"C:\Program Files (x86)\VideoLAN\VLC"),
    ))

    unique = []
    seen = set()
    for candidate in candidates:
        key = str(candidate).rstrip("\\/").casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _discover_windows_vlc_runtime():
    if os.name != "nt":
        return None, "Embedded VLC playback is currently available only on Windows."

    checked = []
    incomplete = []
    for directory in _standard_windows_vlc_directories():
        checked.append(str(directory))
        missing = []
        for required_name in ("libvlc.dll", "libvlccore.dll"):
            if not (directory / required_name).is_file():
                missing.append(required_name)
        if not (directory / "plugins").is_dir():
            missing.append("plugins")
        if not missing:
            return directory, ""
        if directory.exists():
            incomplete.append(f"{directory} (missing {', '.join(missing)})")

    if incomplete:
        return None, "VLC installation is incomplete: " + "; ".join(incomplete)
    return None, "VLC was not found in the standard Program Files locations: " + "; ".join(checked)


def _embedded_vlc_status_result():
    return {
        "available": _EMBEDDED_VLC_INSTANCE is not None,
        "instance": _EMBEDDED_VLC_INSTANCE,
        "version": _EMBEDDED_VLC_VERSION,
        "runtime_path": _EMBEDDED_VLC_RUNTIME_PATH,
        "reason": _EMBEDDED_VLC_FAILURE_REASON,
    }


def get_embedded_vlc_status(force_retry=False):
    """Initialize LibVLC lazily and report whether embedded playback is available."""
    global _EMBEDDED_VLC_CHECKED
    global _EMBEDDED_VLC_INSTANCE
    global _EMBEDDED_VLC_VERSION
    global _EMBEDDED_VLC_RUNTIME_PATH
    global _EMBEDDED_VLC_FAILURE_REASON

    if _EMBEDDED_VLC_CHECKED and not force_retry:
        return _embedded_vlc_status_result()

    _EMBEDDED_VLC_CHECKED = True
    _EMBEDDED_VLC_INSTANCE = None
    _EMBEDDED_VLC_VERSION = None
    _EMBEDDED_VLC_RUNTIME_PATH = None

    runtime_path, failure_reason = _discover_windows_vlc_runtime()
    if runtime_path is None:
        _EMBEDDED_VLC_FAILURE_REASON = failure_reason
        return _embedded_vlc_status_result()

    plugins_path = runtime_path / "plugins"
    os.environ["VLC_PLUGIN_PATH"] = str(plugins_path)

    try:
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is None:
            raise RuntimeError("this Python version does not provide os.add_dll_directory()")
        handle_key = str(runtime_path).casefold()
        if handle_key not in _VLC_DLL_DIRECTORY_HANDLES:
            _VLC_DLL_DIRECTORY_HANDLES[handle_key] = add_dll_directory(str(runtime_path))

        import vlc

        instance = vlc.Instance("--quiet", "--no-video-title-show")
        if instance is None:
            raise RuntimeError("LibVLC returned no instance")
        raw_version = vlc.libvlc_get_version()
        if isinstance(raw_version, bytes):
            version = raw_version.decode("utf-8", errors="replace")
        else:
            version = str(raw_version)
    except (Exception, SystemExit) as exc:
        detail = str(exc).strip() or type(exc).__name__
        _EMBEDDED_VLC_FAILURE_REASON = f"LibVLC could not initialize from {runtime_path}: {detail}"
        return _embedded_vlc_status_result()

    _EMBEDDED_VLC_INSTANCE = instance
    _EMBEDDED_VLC_VERSION = version
    _EMBEDDED_VLC_RUNTIME_PATH = str(runtime_path)
    _EMBEDDED_VLC_FAILURE_REASON = ""
    return _embedded_vlc_status_result()

def guess_video_for_srt(srt_path: _PathMod, project_root: _PathMod):
    try:
        seg_json = srt_path.parent / "segments.json"
        if seg_json.exists():
            data = json.loads(seg_json.read_text(encoding="utf-8"))
            src_str = data.get("source_path")
            if src_str:
                p = _PathMod(src_str)
                if p.exists():
                    return p
    except Exception:
        pass

    base = srt_path.stem
    candidates = []
    candidates += [srt_path.with_suffix(ext) for ext in _VIDEO_EXTS]
    candidates += [(srt_path.parent / (base + ext)) for ext in _VIDEO_EXTS]
    data_input = project_root / "data" / "input"
    if data_input.exists():
        candidates += [data_input / (base + ext) for ext in _VIDEO_EXTS]
        for ext in _VIDEO_EXTS:
            candidates += list(data_input.glob(f"{base}*{ext}"))
    for c in candidates:
        if c.exists():
            return c
    for ext in _VIDEO_EXTS:
        for p in project_root.rglob(f"{base}*{ext}"):
            return p
    return None

def _open_in_vlc(video_path: _PathMod, start_seconds: float, vlc_path: _PathMod | None = None):
    if vlc_path:
        if not vlc_path.exists():
            return False 
    else:
        default_win = _PathMod(r"C:\Program Files\VideoLAN\VLC\vlc.exe")
        vlc_path = default_win if default_win.exists() else None
    
    if vlc_path and vlc_path.exists():
        args = [str(vlc_path), "--play-and-exit", f"--start-time={start_seconds}", str(video_path)]
        try:
            _subproc_mod.Popen(args)
            return True
        except Exception:
            return False
    return False

def _open_in_ffplay(video_path: _PathMod, start_seconds: float):
    try:
        ffplay_local = program_root() / "ffplay.exe"
    except Exception:
        ffplay_local = _PathMod("ffplay.exe")
    ffplay = str(ffplay_local if ffplay_local.exists() else "ffplay")
    _subproc_mod.Popen([ffplay, "-autoexit", "-ss", str(start_seconds), "-i", str(video_path)])

def jump_video_to_srt_time(srt_path: _PathMod, target_seconds: float, vlc_path: _PathMod | None = None):
    try:
        proj = program_root()
    except Exception:
        proj = _PathMod(".")
    vid = guess_video_for_srt(srt_path, proj)
    if not vid:
        raise FileNotFoundError(f"Could not locate a video file automatically for source: {srt_path.name}")
    
    if not _open_in_vlc(vid, target_seconds, vlc_path=vlc_path):
        _open_in_ffplay(vid, target_seconds)

# === end helpers =============================================================

def _fmt_time_hhmmss(seconds: float) -> str:
    if seconds is None:
        return "00:00:00.000"
    s = float(seconds)
    ms = int(round((s - int(s)) * 1000))
    total = int(s)
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

APP_VER = "v1.11.0"

def app_build_id() -> str:
    try:
        p = Path(__file__).resolve()
        h = hashlib.sha256(p.read_bytes()).hexdigest()[:8]
        ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
        return f"sha{h}-{ts}"
    except Exception:
        return "unknown"

def program_root() -> Path: return Path(__file__).resolve().parent
def conf_path() -> Path: return program_root() / "conf.yaml"
def output_root() -> Path: return program_root() / "data" / "output"

def atomic_write_yaml(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def read_yaml(path: Path) -> dict:
    if not path.exists(): return {}
    with path.open("r", encoding="utf-8") as f: obj = yaml.safe_load(f)
    return obj or {}

def first_line(s: str) -> str: return s.splitlines()[0].strip() if s else ""
def try_cmd(args, cwd=None) -> str:
    try: return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT, timeout=8).strip()
    except Exception as e: return f"(unavailable: {e})"
def which_ff(name: str) -> str:
    local = program_root() / f"{name}.exe"
    return str(local) if local.exists() else name
def get_pkg_version(dist_name: str) -> str:
    try:
        from importlib import metadata as md
        return md.version(dist_name)
    except Exception:
        return "(not installed)"

def gather_about_info() -> str:
    lines = []
    lines.append(f"GUI version: {APP_VER} (build {app_build_id()})")
    lines.append(f"Config file: {conf_path()}")
    lines.append("")
    py = sys.executable or "python"
    script = program_root() / "split_audio.py"
    if script.exists():
        ver = try_cmd([py, str(script), "--version"], cwd=str(program_root()))
        lines.append(f"Pipeline script: {script.name} — {first_line(ver)}")
    else:
        lines.append("Pipeline script: split_audio.py not found")
    lines.append("")
    lines.append(f"Python: {first_line(sys.version)}")
    lines.append(f"Executable: {sys.executable}")
    lines.append("")
    try:
        import torch
        torch_ver = getattr(torch, '__version__', '(unknown)')
        cuda_avail = torch.cuda.is_available()
        cuda_ver = getattr(torch.version, 'cuda', None)
        gpu_name = torch.cuda.get_device_name(0) if cuda_avail else "(no CUDA)"
        lines.append(f"PyTorch: {torch_ver}")
        lines.append(f"CUDA available: {cuda_avail}  |  CUDA version: {cuda_ver}")
        lines.append(f"GPU: {gpu_name}")
    except Exception as e:
        lines.append(f"PyTorch: (not importable: {e})")
    lines.append("")
    lines.append("Libraries:")
    lines.append(f"  whisperx: {get_pkg_version('whisperx')}")
    lines.append(f"  pyannote.audio: {get_pkg_version('pyannote.audio')}")
    lines.append(f"  ctranslate2: {get_pkg_version('ctranslate2')}")
    lines.append("")
    ff = which_ff("ffmpeg"); fp = which_ff("ffprobe")
    lines.append(f"ffmpeg: {first_line(try_cmd([ff, '-version'])) or '(not found)'}")
    lines.append(f"ffprobe: {first_line(try_cmd([fp, '-version'])) or '(not found)'}")
    return "\n".join(lines)

_DEFAULTS = {
    "language": "en","model": "large-v3","diarize": True,"slice_audio": True,
    "slice_video": False, "fast_cut_video": False,
    "video_player_path": "", "parallel_workers": 4, # <--- NEW
    "merge_all_segments_into_one_folder": False,"txt_speaker_tags": True,"one_folder": False,
    "output_format": "both","srt": True,"txt": True,"compute_type": "float16","tf32": "off",
    "padding_seconds": 0.25,"hf_token": "",
    "diarization_speaker_mode": "auto", "min_speakers": 2, "max_speakers": 2,
    "ner_engine": "auto",
}
_MODEL_CHOICES = ["tiny","base","small","medium","large-v2","large-v3","large-v3-turbo","distil-large-v3"]
_COMPUTE_CHOICES = ["float16","float32"]
_TF32_CHOICES = ["on","off"]
_SPEAKER_MODE_LABELS = {"auto": "Automatic", "exact": "Exact number", "range": "Range"}
_SPEAKER_MODE_KEYS = {label: key for key, label in _SPEAKER_MODE_LABELS.items()}

def merge_gui_conf(existing: dict, gui_values: dict) -> dict:
    """Merge GUI-owned settings without discarding other configuration keys."""
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(gui_values)
    merged["compute_type"] = "float16"
    merged["tf32"] = "off"
    return merged

def srt_timestamp(t: float) -> str:
    if t < 0: t = 0.0
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

_TITLE_PREFIXES = {"mr", "mrs", "ms", "dr", "prof", "professor"}
_NER_SETTINGS = {"engine": "auto", "topk": 50, "min_score": 0.85}
_NER_CACHE = {}
_NER_CHOICES = ["auto", "hf", "spacy_trf", "spacy_md", "heuristic"]

def _set_ner_settings(engine: str|None=None, topk: int|None=None, min_score: float|None=None):
    if engine is not None: _NER_SETTINGS["engine"] = engine
    if topk is not None: _NER_SETTINGS["topk"] = int(topk)
    if min_score is not None: _NER_SETTINGS["min_score"] = float(min_score)

def _chunk_text(txt: str, max_chars: int = 1200) -> list[str]:
    txt = txt.strip()
    if not txt: return []
    return [txt[i:i+max_chars] for i in range(0, len(txt), max_chars)]

_NAME_REJECT_WORDS = {
    "a", "an", "and", "are", "as", "at", "but", "by", "for", "from", "in", "into",
    "is", "it", "its", "of", "on", "or", "so", "that", "the", "this", "to", "we",
    "you", "your", "okay", "ok", "right", "all", "well", "yes", "no", "there",
    "here", "oops", "um", "uh",

    # common project/topic words that are not speaker names
    "k", "means", "kmeans", "k-means", "algorithm", "cluster", "clusters",
    "centroid", "centroids", "data", "dataset", "set", "patch", "patches",
    "scrap", "scraps", "image", "images", "pixel", "pixels", "face", "faces",
    "olivetti", "figure", "cell", "code", "library", "libraries", "matplotlib",
    "visual", "dictionary", "presentation", "slide", "slides", "model",
}

_NAME_REJECT_PHRASES = {
    "and okay", "okay so", "patches so", "all right", "and then", "okay yes",
    "thank you", "you stopped", "where is", "there we", "here we",
}

_DIRECT_ADDRESS_RE = re.compile(
    r"\b(?:thank you|thanks|okay|ok|alright|all right|your turn|go ahead|welcome)\s*,?\s+"
    r"([A-Z][a-z]{1,24}(?:\s+[A-Z][a-z]{1,24})?)\b"
)

_SELF_INTRO_RE = re.compile(
    r"\b(?:i am|i'm|my name is|this is)\s+"
    r"([A-Z][a-z]{1,24}(?:\s+[A-Z][a-z]{1,24})?)\b",
    re.IGNORECASE
)

def _clean_name_candidate(name: str) -> str:
    name = str(name or "")
    name = name.replace(" ##", "").replace("##", "")
    name = re.sub(r"\bSPEAKER_\d+\b", "", name)
    name = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", name.strip())
    name = re.sub(r"\s+", " ", name)
    return name.strip()

def _is_likely_person_name(name: str) -> bool:
    name = _clean_name_candidate(name)
    if not name:
        return False
    if len(name) < 3 or len(name) > 40:
        return False
    if any(ch.isdigit() for ch in name):
        return False

    low = name.lower().strip()
    if low in _NAME_REJECT_PHRASES:
        return False
    for bad in _NAME_REJECT_PHRASES:
        if bad in low:
            return False

    parts = name.split()
    if len(parts) > 3:
        return False

    lows = [p.lower().strip(".,:;!?()[]{}") for p in parts]
    if any(w in _NAME_REJECT_WORDS for w in lows):
        return False

    # Avoid random sentence fragments like "And Okay"
    if lows[0] in {"and", "okay", "ok", "so", "the", "this", "that", "all", "well"}:
        return False

    # Keep normal names title-cased. This accepts "Olivia" and "John Smith".
    for part in parts:
        if not re.fullmatch(r"[A-Z][a-z]+", part):
            return False

    return True

def _filtered_name_list(items, limit: int = 50) -> list[str]:
    out = []
    seen = set()
    for item in items:
        nm = _clean_name_candidate(item)
        if not _is_likely_person_name(nm):
            continue
        low = nm.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(nm)
        if len(out) >= limit:
            break
    return out

def _extract_direct_address_names(text: str, limit: int = 20) -> list[str]:
    names = []
    for m in _DIRECT_ADDRESS_RE.finditer(text or ""):
        names.append(m.group(1))
    return _filtered_name_list(names, limit=limit)

def _extract_self_intro_names(text: str, limit: int = 20) -> list[str]:
    names = []
    for m in _SELF_INTRO_RE.finditer(text or ""):
        names.append(m.group(1))
    return _filtered_name_list(names, limit=limit)

def _addressed_name_counts_for_speakers(segments) -> dict:
    """
    If SPEAKER_01 says 'Thank you, Olivia' right after SPEAKER_00 was talking,
    Olivia is probably SPEAKER_00, not SPEAKER_01.
    """
    counts = {}
    prev_spk = None

    for seg in segments or []:
        spk = seg.get("speaker")
        text = str(seg.get("text", ""))

        # Self-introductions belong to the current speaker.
        if spk:
            for nm in _extract_self_intro_names(text):
                counts.setdefault(spk, Counter())[nm] += 12

        # Direct-address names usually refer to the previous different speaker.
        names = _extract_direct_address_names(text)
        if names and prev_spk and spk and prev_spk != spk:
            for nm in names:
                counts.setdefault(prev_spk, Counter())[nm] += 15

        if spk:
            prev_spk = spk

    return counts

def _extract_candidates_from_text(text: str, limit: int|None=None):
    if not text:
        return []

    engine = _NER_SETTINGS.get("engine","auto")
    topk = int(_NER_SETTINGS.get("topk", 50))
    min_score = float(_NER_SETTINGS.get("min_score", 0.85))
    if limit is not None:
        topk = min(topk, int(limit))

    direct_names = _extract_direct_address_names(text, limit=topk)
    self_intro_names = _extract_self_intro_names(text, limit=topk)

    def _heuristic(txt: str):
        words = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}", txt)
        cnt = Counter(_clean_name_candidate(w) for w in words)
        ordered = [w for w, _ in cnt.most_common(topk * 2)]
        return _filtered_name_list(direct_names + self_intro_names + ordered, limit=topk)

    try:
        if engine in ("auto","hf"):
            if "hf" not in _NER_CACHE:
                from transformers import pipeline
                import torch
                dev = 0 if torch.cuda.is_available() else -1
                _NER_CACHE["hf"] = pipeline(
                    "ner",
                    model="dbmdz/bert-large-cased-finetuned-conll03-english",
                    aggregation_strategy="simple",
                    device=dev
                )
            pipe = _NER_CACHE["hf"]
            chunks = _chunk_text(text, 1200)
            if not chunks:
                chunks = [text]
            results = pipe(chunks) if len(chunks) > 1 else [pipe(chunks[0])]
            persons = []
            seen = set()
            for ents in results:
                for x in ents:
                    if x.get("entity_group") in ("PER","PERSON") and float(x.get("score",0)) >= min_score:
                        name = _clean_name_candidate(x.get("word",""))
                        low = name.lower()
                        if _is_likely_person_name(name) and low not in seen:
                            seen.add(low)
                            persons.append(name)
                            if len(persons) >= topk:
                                break
                if len(persons) >= topk:
                    break
            persons = _filtered_name_list(direct_names + self_intro_names + persons, limit=topk)
            if persons:
                return persons

        if engine in ("auto","spacy_trf","spacy_md"):
            import spacy as _sp
            if engine in ("auto","spacy_trf"):
                try:
                    _sp.prefer_gpu()
                    nlp = _NER_CACHE.get("spacy_trf")
                    if nlp is None:
                        nlp = _sp.load("en_core_web_trf")
                        _NER_CACHE["spacy_trf"] = nlp
                    doc = nlp(text)
                    out = []
                    seen = set()
                    for e in doc.ents:
                        if e.label_ == "PERSON":
                            nm = _clean_name_candidate(e.text)
                            low = nm.lower()
                            if _is_likely_person_name(nm) and low not in seen:
                                seen.add(low)
                                out.append(nm)
                                if len(out) >= topk:
                                    break
                    out = _filtered_name_list(direct_names + self_intro_names + out, limit=topk)
                    if out:
                        return out
                except Exception:
                    pass

            if engine in ("spacy_md","auto"):
                try:
                    nlp = _NER_CACHE.get("spacy_md")
                    if nlp is None:
                        nlp = _sp.load("en_core_web_md")
                        _NER_CACHE["spacy_md"] = nlp
                    doc = nlp(text)
                    out = []
                    seen = set()
                    for e in doc.ents:
                        if e.label_ == "PERSON":
                            nm = _clean_name_candidate(e.text)
                            low = nm.lower()
                            if _is_likely_person_name(nm) and low not in seen:
                                seen.add(low)
                                out.append(nm)
                                if len(out) >= topk:
                                    break
                    out = _filtered_name_list(direct_names + self_intro_names + out, limit=topk)
                    if out:
                        return out
                except Exception:
                    pass
    except Exception:
        pass

    return _heuristic(text)[:topk]

def _best_names_list(*counters: Counter, limit: int = 8) -> list:
    total = Counter()
    for c in counters:
        total.update(c)

    seen = set()
    out = []
    for name, _ in total.most_common():
        nm = _clean_name_candidate(name)
        if not _is_likely_person_name(nm):
            continue
        low = nm.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(nm)
        if len(out) >= limit:
            break
    return out

_AUTO_SPEAKER_NAME_REJECT_WORDS = {
    "they", "them", "their", "he", "him", "his", "she", "her", "we", "us", "our",
    "you", "your", "people", "one", "then", "when", "where", "what", "why", "wait",
    "first", "now", "these",
}

def _best_auto_speaker_names(*counters: Counter, limit: int = 8) -> list:
    candidates = _best_names_list(*counters, limit=50)
    credible = []
    for candidate in candidates:
        words = [word.casefold() for word in candidate.split()]
        if any(word in _AUTO_SPEAKER_NAME_REJECT_WORDS for word in words):
            continue
        credible.append(candidate)

    # Keep the existing ranking within each group, but prefer full person names.
    credible.sort(key=lambda candidate: 0 if len(candidate.split()) > 1 else 1)
    return credible[:limit]

def safe_base(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9 _\-]", "_", name)
    name = re.sub(r"[\s]+", "_", name).strip("_")
    return name or "Speaker"

def unique_path(path: Path) -> Path:
    if not path.exists(): return path
    base = path.stem; ext = path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{base}_{i}{ext}")
        if not cand.exists(): return cand
        i += 1

def _ner_device_info(engine: str) -> str:
    try:
        if engine in ("hf", "auto"):
            try:
                import torch
                if torch.cuda.is_available(): return f"device cuda:0 ({torch.cuda.get_device_name(0)})"
                return "device cpu"
            except Exception: return "device cpu (torch unavailable)"
        if engine == "spacy_trf": return "spacy_trf (GPU if installed)"
        if engine == "spacy_md": return "CPU (spaCy md)"
        if engine == "heuristic": return "CPU (regex heuristic)"
    except Exception: pass
    return "device unknown"

class NERSelectDialog(tk.Toplevel):
    def __init__(self, master, initial: str = "auto"):
        super().__init__(master)
        self.title("NER Engine")
        self.geometry("340x180")
        self.resizable(False, False)
        self.result = None
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Choose NER engine:").pack(anchor="w")
        self.var_engine = tk.StringVar(value=initial)
        self.cb = ttk.Combobox(frm, textvariable=self.var_engine, values=_NER_CHOICES, state="readonly", width=16)
        self.cb.pack(anchor="w", pady=(6, 8))
        self.lbl = ttk.Label(frm, text="", foreground="#555")
        self.lbl.pack(anchor="w")
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="OK", command=self._accept).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right", padx=6)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.cb.focus_set()
        self.cb.bind("<<ComboboxSelected>>", lambda *_: self._update_preview())
        self.after(50, self._update_preview)

    def _update_preview(self):
        self.lbl.configure(text=f"Engine: {self.var_engine.get() or 'auto'} — " + _ner_device_info(self.var_engine.get()))

    def _accept(self):
        self.result = self.var_engine.get().strip()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

class _SrtHitsDialog(tk.Toplevel):
    def __init__(self, parent, hits):
        super().__init__(parent)
        self.title("SRT Matches")
        self.resizable(True, True)
        self.transient(parent)
        self.hits = hits
        self.result = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=8)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree_style_name = "SrtMatches.Treeview"
        tree_style = tb.Style.get_instance() or tb.Style()
        colors = tree_style.colors
        tree_style.configure(
            tree_style_name,
            background=colors.inputbg,
            foreground=colors.inputfg,
            fieldbackground=colors.inputbg,
        )
        tree_style.map(
            tree_style_name,
            background=[("selected", colors.primary)],
            foreground=[("selected", colors.get_foreground("primary"))],
        )
        self.tree = ttk.Treeview(
            frame,
            columns=("time","text"),
            show="headings",
            selectmode="browse",
            height=12,
            style=tree_style_name,
        )
        self.tree.heading("time", text="Start")
        self.tree.heading("text", text="Caption")
        self.tree.column("time", width=110, anchor="w")
        self.tree.column("text", width=680, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        ybar.grid(row=0, column=1, sticky="ns")
        for i, h in enumerate(self.hits):
            txt = (h.get("text") or "").replace("\n", " ")
            if len(txt) > 160:
                txt = txt[:157] + "…"
            self.tree.insert("", "end", iid=str(i), values=(_fmt_time_hhmmss(h.get("start")), txt))
        first_item = self.tree.get_children()
        if first_item:
            self.tree.selection_set(first_item[0])
            self.tree.focus(first_item[0])
            self.tree.see(first_item[0])
        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8,0))
        self.btn_open = ttk.Button(btns, text="Open at time", command=self._on_open)
        self.btn_open.pack(side="right", padx=(0,8))
        ttk.Button(btns, text="Cancel", command=self._on_cancel).pack(side="right")
        self.tree.bind("<ButtonRelease-1>", self._select_clicked_row, add="+")
        self.tree.bind("<Double-1>", lambda e: self._on_open())
        self.bind("<Return>", lambda e: self._on_open())
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.update_idletasks()
        self.geometry("820x340")
        self.lift()
        self.attributes("-topmost", True)
        self.after(50, lambda: self.attributes("-topmost", False))
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after_idle(self.tree.focus_set)

    def _select_clicked_row(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)

    def _on_open(self):
        sel = self.tree.selection()
        if sel:
            self.result = self.hits[int(sel[0])]
            self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()

def _assign_segment_speaker(segments, indices, target_speaker: str) -> bool:
    changed = False
    for index in indices:
        if index < 0 or index >= len(segments):
            continue
        segment = segments[index]
        if not isinstance(segment, dict):
            continue
        if segment.get("speaker") != target_speaker:
            segment["speaker"] = target_speaker
            changed = True
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for word in words:
            if not isinstance(word, dict):
                continue
            speech_text = word.get("word")
            if not str(speech_text or "").strip():
                speech_text = word.get("text", "")
            if not str(speech_text or "").strip():
                continue
            if word.get("speaker") != target_speaker:
                word["speaker"] = target_speaker
                changed = True
    return changed

def _write_corrected_segments_json(
    segments_json: Path,
    original_data: dict,
    corrected_segments: list,
    create_backup: bool,
) -> dict:
    if create_backup:
        backup_path = segments_json.with_name("segments.before_manual_corrections.json")
        if not backup_path.exists():
            original_bytes = segments_json.read_bytes()
            try:
                with backup_path.open("xb") as backup_file:
                    backup_file.write(original_bytes)
            except FileExistsError:
                pass
    updated_data = copy.deepcopy(original_data) if isinstance(original_data, dict) else {}
    updated_data["segments"] = copy.deepcopy(corrected_segments)
    segments_json.write_text(json.dumps(updated_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return updated_data

class SegmentCorrectionDialog(tk.Toplevel):
    def __init__(self, parent, segments, speakers, name_mapping):
        super().__init__(parent)
        self.title("Review Speaker Segments")
        self.geometry("1100x650")
        self.minsize(820, 480)
        self.transient(parent)
        self.result = None
        self.changed = False
        self.working_segments = copy.deepcopy(list(segments))
        self.name_mapping = dict(name_mapping or {})
        self.speakers = []
        for speaker in list(speakers or []) + [seg.get("speaker") for seg in self.working_segments if isinstance(seg, dict)]:
            if speaker and speaker not in self.speakers:
                self.speakers.append(speaker)
        self.friendly_to_raw = {self._friendly_speaker(speaker): speaker for speaker in self.speakers}

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        filters = ttk.LabelFrame(self, text="Filter Segments", padding=10)
        filters.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        filters.columnconfigure(4, weight=1)
        ttk.Label(filters, text="Speaker filter").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.var_filter_speaker = tk.StringVar(value="All speakers")
        filter_combo = ttk.Combobox(
            filters,
            textvariable=self.var_filter_speaker,
            values=("All speakers", *self.speakers),
            width=18,
            state="readonly",
        )
        filter_combo.grid(row=0, column=1, sticky="w", padx=(0, 16))
        filter_combo.bind("<<ComboboxSelected>>", self._populate_tree)
        ttk.Label(filters, text="Search transcript").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.var_search = tk.StringVar()
        search_entry = ttk.Entry(filters, textvariable=self.var_search, width=34)
        search_entry.grid(row=0, column=3, sticky="w")
        search_entry.bind("<KeyRelease>", self._populate_tree)
        tb.Label(
            filters,
            text=(
                "Whole-segment correction only: each row is one indivisible segments.json entry. "
                "If a row contains dialogue from more than one real speaker, this version cannot split "
                "the text inside it. Assigning changes the entire row."
            ),
            bootstyle="warning",
            font=("Segoe UI", 9, "bold"),
            justify="left",
            wraplength=1000,
            padding=(8, 6),
        ).grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))

        tree_frame = ttk.Frame(self, padding=(12, 0))
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        tree_style_name = "SegmentCorrection.Treeview"
        tree_style = tb.Style.get_instance() or tb.Style()
        colors = tree_style.colors
        tree_style.configure(
            tree_style_name,
            background=colors.inputbg,
            foreground=colors.inputfg,
            fieldbackground=colors.inputbg,
            rowheight=25,
        )
        tree_style.map(
            tree_style_name,
            background=[("selected", colors.primary)],
            foreground=[("selected", colors.get_foreground("primary"))],
        )
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("start", "speaker", "transcript"),
            show="headings",
            selectmode="extended",
            style=tree_style_name,
        )
        self.tree.heading("start", text="Start")
        self.tree.heading("speaker", text="Current speaker")
        self.tree.heading("transcript", text="Transcript")
        self.tree.column("start", width=105, minwidth=90, stretch=False, anchor="w")
        self.tree.column("speaker", width=210, minwidth=150, stretch=False, anchor="w")
        self.tree.column("transcript", width=700, minwidth=300, stretch=True, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        assignment = ttk.LabelFrame(self, text="Correction", padding=10)
        assignment.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 0))
        assignment.columnconfigure(3, weight=1)
        ttk.Label(assignment, text="Target speaker").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.var_target_speaker = tk.StringVar()
        self.target_combo = ttk.Combobox(
            assignment,
            textvariable=self.var_target_speaker,
            values=tuple(self.friendly_to_raw),
            width=30,
            state="readonly",
        )
        self.target_combo.grid(row=0, column=1, sticky="w", padx=(0, 10))
        tb.Button(
            assignment,
            text="Assign selected segments",
            command=self._assign_selected,
            bootstyle="primary",
        ).grid(row=0, column=2, sticky="w")

        buttons = ttk.Frame(self, padding=12)
        buttons.grid(row=3, column=0, sticky="e")
        tb.Button(buttons, text="Save Corrections", command=self._save, bootstyle="success", padding=(16, 6)).pack(side="right")
        tb.Button(buttons, text="Cancel", command=self._cancel, bootstyle="secondary-outline", padding=(14, 6)).pack(side="right", padx=(0, 8))

        self.tree.bind("<Control-a>", self._select_all_visible)
        self.tree.bind("<Control-A>", self._select_all_visible)
        self.tree.bind("<Double-1>", self._ignore_double_click)
        self.bind("<Escape>", lambda event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._populate_tree()
        self.grab_set()
        self.after_idle(self.tree.focus_set)

    def _friendly_speaker(self, speaker):
        name = str(self.name_mapping.get(speaker, "") or "").strip()
        return f"{speaker} ({name})" if name else str(speaker or "Unassigned")

    def _populate_tree(self, *_):
        selected = set(self.tree.selection()) if hasattr(self, "tree") else set()
        existing_rows = self.tree.get_children()
        if existing_rows:
            self.tree.delete(*existing_rows)
        speaker_filter = self.var_filter_speaker.get()
        query = self.var_search.get().strip().casefold()
        for index, segment in enumerate(self.working_segments):
            if not isinstance(segment, dict):
                continue
            raw_speaker = segment.get("speaker") or ""
            transcript = str(segment.get("text", "") or "").replace("\n", " ").strip()
            if speaker_filter != "All speakers" and raw_speaker != speaker_filter:
                continue
            if query and query not in transcript.casefold():
                continue
            iid = str(index)
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(_fmt_time_hhmmss(segment.get("start")), self._friendly_speaker(raw_speaker), transcript),
            )
        visible_selection = [iid for iid in selected if self.tree.exists(iid)]
        if visible_selection:
            self.tree.selection_set(*visible_selection)
            self.tree.focus(visible_selection[0])
            self.tree.see(visible_selection[0])

    def _select_all_visible(self, event=None):
        visible = self.tree.get_children()
        if visible:
            self.tree.selection_set(*visible)
            self.tree.focus(visible[0])
        return "break"

    def _ignore_double_click(self, event=None):
        return "break"

    def _assign_selected(self):
        selected = tuple(self.tree.selection())
        if not selected:
            messagebox.showinfo("No segments selected", "Select one or more transcript segments first.", parent=self)
            return
        target_display = self.var_target_speaker.get().strip()
        target_speaker = self.friendly_to_raw.get(target_display)
        if not target_speaker:
            messagebox.showinfo("No target speaker", "Choose a target speaker first.", parent=self)
            return
        selected_indices = [int(iid) for iid in selected]
        self.changed = _assign_segment_speaker(self.working_segments, selected_indices, target_speaker) or self.changed
        friendly = self._friendly_speaker(target_speaker)
        for iid in selected:
            if self.tree.exists(iid):
                self.tree.set(iid, "speaker", friendly)
        self.tree.selection_set(*selected)
        self.tree.focus(selected[0])
        self.tree.see(selected[0])

    def _save(self):
        self.result = copy.deepcopy(self.working_segments)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

class NamingWorkspace(ttk.Frame):
    _VIDEO_RATIO_DEFAULT = 0.65
    _VIDEO_RATIO_MIN = 0.35
    _VIDEO_RATIO_MAX = 0.80
    _TRANSCRIPT_FONT_MIN = 8
    _TRANSCRIPT_FONT_MAX = 28

    def __init__(
        self,
        master,
        speakers_json: Path,
        segments_json: Path,
        *,
        on_apply_complete=None,
        on_discard=None,
        discard_label="Cancel",
    ):
        super().__init__(master)
        self._on_apply_complete = on_apply_complete
        self._on_discard = on_discard
        self._discard_label = discard_label
        self._started = False
        self._vlc_status = {}
        self._vlc_instance = None
        self._vlc_player = None
        self._vlc_media = None
        self._loaded_video_path = None
        self._video_update_after = None
        self._pending_seek_after = None
        self._pending_subtitle_after = None
        self._seek_dragging = False
        self._video_duration_seconds = 0.0
        self._player_closing = False
        self._subtitle_track_ids = {}
        self._applied_subtitle_choice = None
        self._word_records = []
        self._word_tag_to_record = {}
        self._word_tag_names = []
        self._timed_word_index = []
        self._timed_word_starts = []
        self._current_word_tag = None
        self._word_sync_after = None
        self._word_sync_interval_ms = 40
        self._word_clock_vlc_seconds = None
        self._word_clock_vlc_observed_at = None
        self._word_clock_estimate_seconds = None
        self._word_clock_seek_target = None
        self._word_clock_seek_started_at = None
        self._transcript_follow_suspended_until = 0.0
        self._transcript_default_cursor = "xterm"
        self._view_preferences_saved = False
        self._preview_ratio_after = None
        self._preview_ratio_applied = False
        self._video_reattach_after = None
        try:
            self._naming_cfg = read_yaml(conf_path())
            if not isinstance(self._naming_cfg, dict):
                self._naming_cfg = {}
        except Exception:
            self._naming_cfg = {}
        self._preview_video_ratio = self._validated_video_ratio(
            self._naming_cfg.get("name_speakers_video_ratio")
        )
        self.speakers_json = speakers_json
        self.segments_json = segments_json
        spk_data = json.loads(speakers_json.read_text(encoding="utf-8"))
        seg_data = json.loads(segments_json.read_text(encoding="utf-8"))
        self.title_name = spk_data.get("title") or speakers_json.parent.name
        self.speakers = list(spk_data.get("speakers") or [])
        self.saved_names = dict(spk_data.get("names") or spk_data.get("name_map") or {})
        self.segments_data = copy.deepcopy(seg_data)
        self.segments = copy.deepcopy(seg_data.get("segments") or [])
        self._subtitle_paths, self._subtitle_choices = self._discover_subtitle_files()
        self.manual_corrections_pending = False
        global_counts = Counter(_extract_candidates_from_text(" ".join(str(s.get("text","")) for s in self.segments)))
        # Do not use the file title as a strong name source. Titles are usually topics, not speakers.
        title_counts = Counter()
        addressed_counts = _addressed_name_counts_for_speakers(self.segments)
        per_spk_counts = {}
        for spk in self.speakers:
            c = Counter(_extract_candidates_from_text(" ".join(str(s.get("text","")) for s in self.segments if (s.get("speaker") == spk))))
            c.update(addressed_counts.get(spk, Counter()))
            per_spk_counts[spk] = c
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        ttk.Label(main, text="Name Speakers", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(main, text=f"{self.title_name} — assign names and review the transcript").grid(row=1, column=0, sticky="w", pady=(2, 10))
        paned = ttk.PanedWindow(main, orient="horizontal")
        paned.grid(row=2, column=0, sticky="nsew")
        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        right = ttk.Frame(paned, padding=(6, 0, 0, 0))
        paned.add(left, weight=2)
        paned.add(right, weight=3)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1, minsize=150)
        left.rowconfigure(1, weight=1, minsize=125)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.inputs = {}
        self.selected_speaker = tk.StringVar(value=self.speakers[0] if self.speakers else "")

        assignments = ttk.LabelFrame(left, text="Speaker Assignments", padding=8)
        assignments.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        assignments.columnconfigure(0, weight=1)
        assignments.rowconfigure(1, weight=1)

        assignment_headings = ttk.Frame(assignments)
        assignment_headings.grid(row=0, column=0, sticky="ew", padx=(2, 18), pady=(0, 4))
        assignment_headings.columnconfigure(2, weight=1)
        ttk.Label(assignment_headings, text="Speaker", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(assignment_headings, text="Assigned name", font=("Segoe UI", 9, "bold")).grid(row=0, column=2, sticky="w")

        speaker_canvas = tk.Canvas(assignments, height=155, highlightthickness=0, borderwidth=0)
        speaker_scroll = ttk.Scrollbar(assignments, orient="vertical", command=speaker_canvas.yview)
        speaker_canvas.configure(yscrollcommand=speaker_scroll.set)
        speaker_canvas.grid(row=1, column=0, sticky="nsew")
        speaker_scroll.grid(row=1, column=1, sticky="ns")

        grid = ttk.Frame(speaker_canvas, padding=(2, 0, 4, 0))
        speaker_window = speaker_canvas.create_window((0, 0), window=grid, anchor="nw")

        for r, spk in enumerate(self.speakers):
            rb = ttk.Radiobutton(grid, variable=self.selected_speaker, value=spk)
            rb.grid(row=r, column=0, sticky="w", pady=4)

            ttk.Label(grid, text=spk, width=16).grid(row=r, column=1, sticky="w", padx=(0, 8), pady=4)

            # Keep this as an editable combobox so the user can still type any name manually.
            # The dropdown values are hints only. We do not auto-fill weak guesses anymore.
            suggestions = _best_names_list(per_spk_counts.get(spk, Counter()), global_counts, title_counts, limit=8)
            saved = self.saved_names.get(spk)

            if saved and saved not in suggestions:
                suggestions = [saved] + suggestions

            cb = ttk.Combobox(grid, values=suggestions, width=30, state="normal")
            cb.grid(row=r, column=2, sticky="ew", pady=4)

            cb.bind("<FocusIn>", lambda e, spk=spk: self.selected_speaker.set(spk))
            cb.bind("<Button-1>", lambda e, spk=spk: self.selected_speaker.set(spk))

            if saved:
                cb.set(saved)

            self.inputs[spk] = cb

        grid.columnconfigure(2, weight=1)
        grid.bind("<Configure>", lambda event: speaker_canvas.configure(scrollregion=speaker_canvas.bbox("all")))
        speaker_canvas.bind("<Configure>", lambda event: speaker_canvas.itemconfigure(speaker_window, width=event.width))
        tb.Button(
            assignments,
            text="Review speaker segments...",
            command=self.open_segment_corrections,
            bootstyle="primary-outline",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        def suggest_first_two_names():
            filled = self._prefill_first_two(per_spk_counts, global_counts, title_counts)
            if filled == 0:
                messagebox.showinfo(
                    "No names suggested",
                    "No empty fields for the first two speakers had an available name suggestion.",
                    parent=self.winfo_toplevel(),
                )

        tb.Button(
            assignments,
            text="Suggest names for first two speakers",
            command=suggest_first_two_names,
            bootstyle="primary-outline",
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # Candidate name pool: names/entities found in the transcript.
        # These are NOT automatically assigned. The user assigns them manually.
        pool_frame = ttk.LabelFrame(left, text="Candidate Name Pool", padding=8)
        pool_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        pool_frame.columnconfigure(0, weight=1)
        pool_frame.rowconfigure(0, weight=1)

        pool_names = _best_names_list(
            global_counts,
            *[per_spk_counts.get(spk, Counter()) for spk in self.speakers],
            title_counts,
            limit=40
        )

        pool_inner = ttk.Frame(pool_frame)
        pool_inner.grid(row=0, column=0, sticky="nsew")
        pool_inner.columnconfigure(0, weight=1)
        pool_inner.rowconfigure(0, weight=1)

        self.name_pool = tk.Listbox(pool_inner, height=6, exportselection=False)
        pool_scroll = ttk.Scrollbar(pool_inner, orient="vertical", command=self.name_pool.yview)
        self.name_pool.configure(yscrollcommand=pool_scroll.set)

        self.name_pool.grid(row=0, column=0, sticky="nsew")
        pool_scroll.grid(row=0, column=1, sticky="ns")

        for nm in pool_names:
            self.name_pool.insert("end", nm)

        def assign_selected_name(event=None):
            sel = self.name_pool.curselection()
            if not sel:
                return
            spk = self.selected_speaker.get()
            cb = self.inputs.get(spk)
            if not cb:
                return
            cb.set(self.name_pool.get(sel[0]))

        def clear_selected_speaker():
            spk = self.selected_speaker.get()
            cb = self.inputs.get(spk)
            if cb:
                cb.set("")

        def add_typed_name_to_pool():
            spk = self.selected_speaker.get()
            cb = self.inputs.get(spk)
            if not cb:
                return
            nm = cb.get().strip()
            if not nm:
                return
            existing = [self.name_pool.get(i) for i in range(self.name_pool.size())]
            if nm not in existing:
                self.name_pool.insert("end", nm)

        self.name_pool.bind("<Double-1>", assign_selected_name)

        pool_btns = ttk.Frame(pool_frame)
        pool_btns.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        pool_btns.columnconfigure(0, weight=1)
        pool_btns.columnconfigure(1, weight=1)
        tb.Button(pool_btns, text="Assign to selected speaker", command=assign_selected_name, bootstyle="primary-outline").grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(pool_btns, text="Clear selected speaker", command=clear_selected_speaker).grid(row=1, column=0, sticky="ew", pady=(6, 0), padx=(0, 3))
        ttk.Button(pool_btns, text="Add typed name to pool", command=add_typed_name_to_pool).grid(row=1, column=1, sticky="ew", pady=(6, 0), padx=(3, 0))

        opts = ttk.LabelFrame(left, text="Output Options", padding=8)
        opts.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.var_overwrite = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Overwrite existing SRT/TXT (recommended)", variable=self.var_overwrite).grid(row=0, column=0, sticky="w")
        self.var_rename_audio = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Rename folders and .wav files with names", variable=self.var_rename_audio).grid(row=1, column=0, sticky="w", pady=(2, 0))
        _cfg = self._naming_cfg
        self.var_export_vtt  = tk.BooleanVar(value=bool(_cfg.get("export_word_vtt", False)))
        self.var_export_ass  = tk.BooleanVar(value=bool(_cfg.get("export_word_ass", False)))
        self.var_export_html = tk.BooleanVar(value=bool(_cfg.get("export_word_html", False)))
        self.var_export_lrc  = tk.BooleanVar(value=bool(_cfg.get("export_word_lrc", False)))
        self.var_export_ass_plain = tk.BooleanVar(value=bool(_cfg.get("export_ass_plain", False)))
        ttk.Label(opts, text="Word-level exports", font=("Segoe UI", 9, "bold")).grid(row=3, column=0, sticky="w", pady=(8, 2))
        ttk.Checkbutton(opts, text="Word-level VTT", variable=self.var_export_vtt).grid(row=4, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Word-level ASS (VLC)", variable=self.var_export_ass).grid(row=5, column=0, sticky="w", pady=(2, 0))
        ttk.Checkbutton(opts, text="Write HTML word player", variable=self.var_export_html).grid(row=6, column=0, sticky="w", pady=(2, 0))
        ttk.Checkbutton(opts, text="Word-level LRC (CapCut)", variable=self.var_export_lrc).grid(row=7, column=0, sticky="w", pady=(2, 0))
        ttk.Checkbutton(opts, text="ASS (plain, no karaoke)", variable=self.var_export_ass_plain).grid(row=8, column=0, sticky="w", pady=(2, 0))

        btns = ttk.Frame(left)
        btns.grid(row=3, column=0, sticky="ew")
        tb.Button(btns, text="Apply", command=self.apply_changes, bootstyle="success", padding=(16, 6)).pack(side="right")
        tb.Button(
            btns,
            text=self._discard_label,
            command=self.discard_changes,
            bootstyle="secondary-outline",
            padding=(14, 6),
        ).pack(side="right", padx=(0, 8))

        toolbar = ttk.LabelFrame(right, text="Search", padding=10)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(4, weight=1)
        ttk.Label(toolbar, text="Find text").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.find_var = tk.StringVar(value="SPEAKER_00")
        find_entry = ttk.Entry(toolbar, textvariable=self.find_var, width=24)
        find_entry.grid(row=0, column=1, sticky="w", padx=(0, 14))
        ttk.Label(toolbar, text="Speaker filter").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.find_speaker_var = tk.StringVar(value=self.speakers[0] if self.speakers else "")
        find_spk = ttk.Combobox(toolbar, textvariable=self.find_speaker_var, values=self.speakers, width=14, state="readonly")
        find_spk.grid(row=0, column=3, sticky="w")

        search_actions = ttk.Frame(toolbar)
        search_actions.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        tb.Button(search_actions, text="Find next", command=self.find_next, bootstyle="primary-outline").pack(side="left")
        ttk.Button(search_actions, text="Find speaker tag", command=self.find_speaker_tag).pack(side="left", padx=(6, 0))
        ttk.Button(search_actions, text="Preview video at hit", command=self.preview_video_at_query).pack(side="left", padx=(6, 0))
        ttk.Button(search_actions, text="Open video externally", command=self.open_video_at_query).pack(side="left", padx=(6, 0))
        ttk.Button(search_actions, text="Open transcript file", command=self.open_txt_external).pack(side="left", padx=(6, 0))

        self._preview_paned = tk.PanedWindow(
            right,
            orient="vertical",
            sashwidth=8,
            sashrelief="raised",
            showhandle=False,
            opaqueresize=True,
            borderwidth=0,
        )
        self._preview_paned.grid(row=1, column=0, sticky="nsew")

        video = ttk.LabelFrame(self._preview_paned, text="Video Preview", padding=8)
        self._video_preview_frame = video
        video.columnconfigure(0, weight=1, minsize=480)
        video.rowconfigure(0, weight=1, minsize=160)
        self._preview_paned.add(video, minsize=240, stretch="always")

        video_host = ttk.Frame(video)
        video_host.grid(row=0, column=0, sticky="nsew")
        video_host.columnconfigure(0, weight=1)
        video_host.rowconfigure(0, weight=1)
        self.video_surface = tk.Frame(
            video_host,
            width=480,
            height=270,
            background="black",
            highlightthickness=0,
            borderwidth=0,
        )
        self.video_surface.grid(row=0, column=0, sticky="nsew")
        self.video_message = tk.Label(
            self.video_surface,
            text="No video loaded",
            background="black",
            foreground="#c8c8c8",
            justify="center",
            wraplength=430,
        )
        self.video_message.place(relx=0.5, rely=0.5, anchor="center")

        player_controls = ttk.Frame(video)
        player_controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        player_controls.columnconfigure(4, weight=1)
        self.btn_video_back = ttk.Button(player_controls, text="Back 5 seconds", command=self._video_back)
        self.btn_video_back.grid(row=0, column=0, padx=(0, 6))
        self.btn_video_play = tb.Button(player_controls, text="Play", command=self._video_play_pause, bootstyle="primary-outline")
        self.btn_video_play.grid(row=0, column=1, padx=(0, 6))
        self.btn_video_forward = ttk.Button(player_controls, text="Forward 5 seconds", command=self._video_forward)
        self.btn_video_forward.grid(row=0, column=2, padx=(0, 6))
        self.btn_video_stop = ttk.Button(player_controls, text="Stop", command=self._video_stop)
        self.btn_video_stop.grid(row=0, column=3, padx=(0, 8))
        self.lbl_video_time = ttk.Label(player_controls, text="00:00 / 00:00")
        self.lbl_video_time.grid(row=0, column=5, sticky="e")

        self.video_seek_var = tk.DoubleVar(value=0.0)
        self.video_seek = ttk.Scale(player_controls, from_=0.0, to=1.0, variable=self.video_seek_var)
        self.video_seek.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        self.video_seek.bind("<ButtonPress-1>", self._video_seek_started)
        self.video_seek.bind("<ButtonRelease-1>", self._video_seek_released)

        lower_controls = ttk.Frame(video)
        lower_controls.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        lower_controls.columnconfigure(2, weight=1)
        ttk.Label(lower_controls, text="Subtitles").grid(row=0, column=0, sticky="w", padx=(0, 6))
        default_subtitle = "SRT" if "SRT" in self._subtitle_paths else "Off"
        self.subtitle_var = tk.StringVar(value=default_subtitle)
        self.subtitle_selector = ttk.Combobox(
            lower_controls,
            textvariable=self.subtitle_var,
            values=self._subtitle_choices,
            width=24,
            state="readonly",
        )
        self.subtitle_selector.grid(row=0, column=1, sticky="w")
        self.subtitle_selector.bind("<<ComboboxSelected>>", self._subtitle_selection_changed)

        ttk.Label(lower_controls, text="Volume").grid(row=0, column=3, sticky="e", padx=(12, 6))
        self.video_volume_var = tk.DoubleVar(value=80.0)
        self.video_volume = ttk.Scale(
            lower_controls,
            from_=0.0,
            to=100.0,
            length=120,
            variable=self.video_volume_var,
            command=self._video_volume_changed,
        )
        self.video_volume.grid(row=0, column=4, sticky="e")
        self._video_controls = [
            self.btn_video_back,
            self.btn_video_play,
            self.btn_video_forward,
            self.btn_video_stop,
            self.video_seek,
            self.subtitle_selector,
            self.video_volume,
        ]

        viewer = ttk.LabelFrame(self._preview_paned, text="Transcript Preview", padding=8)
        self._transcript_preview_frame = viewer
        viewer.columnconfigure(0, weight=1)
        viewer.rowconfigure(1, weight=1)
        self._preview_paned.add(viewer, minsize=130, stretch="always")

        transcript_controls = ttk.Frame(viewer)
        transcript_controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(transcript_controls, text="Text size").pack(side="left", padx=(0, 6))
        self.btn_transcript_font_down = ttk.Button(
            transcript_controls,
            text="A\N{MINUS SIGN}",
            width=3,
            command=lambda: self._adjust_transcript_font_size(-1),
        )
        self.btn_transcript_font_down.pack(side="left")
        self.transcript_font_size_var = tk.StringVar()
        ttk.Label(
            transcript_controls,
            textvariable=self.transcript_font_size_var,
            width=3,
            anchor="center",
        ).pack(side="left", padx=4)
        self.btn_transcript_font_up = ttk.Button(
            transcript_controls,
            text="A+",
            width=3,
            command=lambda: self._adjust_transcript_font_size(1),
        )
        self.btn_transcript_font_up.pack(side="left")
        ttk.Button(
            transcript_controls,
            text="Reset",
            command=self._reset_transcript_font_size,
        ).pack(side="left", padx=(6, 0))

        self.text = tk.Text(viewer, wrap="word", height=8)
        self.transcript_font = tkfont.Font(root=self, font=self.text.cget("font"))
        try:
            captured_font_size = abs(int(self.transcript_font.cget("size")))
        except (TypeError, ValueError, tk.TclError):
            captured_font_size = 10
        self._transcript_default_font_size = max(
            self._TRANSCRIPT_FONT_MIN,
            min(self._TRANSCRIPT_FONT_MAX, captured_font_size),
        )
        self.text.configure(font=self.transcript_font)
        initial_font_size = self._validated_transcript_font_size(
            self._naming_cfg.get("name_speakers_transcript_font_size")
        )
        yscroll = ttk.Scrollbar(viewer, orient="vertical", command=self._on_transcript_scrollbar)
        self.text.configure(yscrollcommand=yscroll.set)
        self.text.grid(row=1, column=0, sticky="nsew")
        yscroll.grid(row=1, column=1, sticky="ns")
        self._set_transcript_font_size(initial_font_size)
        self.text.bind("<Control-MouseWheel>", self._on_transcript_ctrl_mousewheel)
        self.text.bind("<Control-Button-4>", self._on_transcript_ctrl_mousewheel)
        self.text.bind("<Control-Button-5>", self._on_transcript_ctrl_mousewheel)
        self._configure_transcript_word_tags()
        self.txt_path, _txt_content = self._load_transcript()
        self._render_transcript_preview()
        txt_content = self.text.get("1.0", "end-1c")
        self._reset_highlight()
        initial = "SPEAKER_00"
        if initial not in txt_content and self.speakers:
            initial = self.speakers[0]
        if "SPEAKER_" not in initial:
            initial = self.speakers[0] if self.speakers else "SPEAKER_00"
        self.find_var.set(initial)
        self._highlight_query(initial)
        self.bind("<Destroy>", self._on_workspace_destroyed, add="+")

    def start(self):
        """Start host-dependent services after the workspace has been placed."""
        if self._started or self._player_closing:
            return
        self._started = True
        self._initialize_embedded_player()
        self._preview_ratio_after = self.after(100, self._apply_initial_preview_ratio)

    def on_host_activated(self):
        """Reattach the existing video output after an embedded host is remapped."""
        if (
            os.name != "nt"
            or self._player_closing
            or self._vlc_player is None
            or self._video_reattach_after is not None
        ):
            return

        def reattach():
            self._video_reattach_after = None
            if self._player_closing or self._vlc_player is None:
                return
            try:
                if not self.winfo_exists() or not self.video_surface.winfo_exists():
                    return
                self.video_surface.update_idletasks()
                self._vlc_player.set_hwnd(self.video_surface.winfo_id())
            except (AttributeError, tk.TclError):
                pass
            except Exception:
                pass

        try:
            self._video_reattach_after = self.after_idle(reattach)
        except tk.TclError:
            self._video_reattach_after = None

    def _validated_video_ratio(self, value):
        if isinstance(value, bool):
            return self._VIDEO_RATIO_DEFAULT
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return self._VIDEO_RATIO_DEFAULT
        if not math.isfinite(ratio):
            return self._VIDEO_RATIO_DEFAULT
        return max(self._VIDEO_RATIO_MIN, min(self._VIDEO_RATIO_MAX, ratio))

    def _validated_transcript_font_size(self, value):
        default = self._transcript_default_font_size
        if isinstance(value, bool):
            return default
        try:
            size = int(value)
            if isinstance(value, float) and not value.is_integer():
                return default
            if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value.strip()):
                return default
        except (TypeError, ValueError):
            return default
        return max(self._TRANSCRIPT_FONT_MIN, min(self._TRANSCRIPT_FONT_MAX, size))

    def _set_transcript_font_size(self, size):
        size = max(self._TRANSCRIPT_FONT_MIN, min(self._TRANSCRIPT_FONT_MAX, int(size)))
        self.transcript_font.configure(size=size)
        self.transcript_font_size_var.set(str(size))
        self.btn_transcript_font_down.configure(
            state="disabled" if size <= self._TRANSCRIPT_FONT_MIN else "normal"
        )
        self.btn_transcript_font_up.configure(
            state="disabled" if size >= self._TRANSCRIPT_FONT_MAX else "normal"
        )

    def _adjust_transcript_font_size(self, amount):
        try:
            current = int(self.transcript_font.cget("size"))
        except (TypeError, ValueError, tk.TclError):
            current = self._transcript_default_font_size
        self._set_transcript_font_size(current + int(amount))

    def _reset_transcript_font_size(self):
        self._set_transcript_font_size(self._transcript_default_font_size)

    def _on_transcript_ctrl_mousewheel(self, event):
        direction = 0
        if getattr(event, "delta", 0) > 0 or getattr(event, "num", None) == 4:
            direction = 1
        elif getattr(event, "delta", 0) < 0 or getattr(event, "num", None) == 5:
            direction = -1
        if direction:
            self._adjust_transcript_font_size(direction)
        return "break"

    def _apply_initial_preview_ratio(self):
        self._preview_ratio_after = None
        if self._preview_ratio_applied or self._player_closing:
            return
        try:
            self.update_idletasks()
            available_height = self._preview_paned.winfo_height()
            if available_height <= 1 or len(self._preview_paned.panes()) < 2:
                self._preview_ratio_after = self.after(50, self._apply_initial_preview_ratio)
                return
            sash_position = int(round(available_height * self._preview_video_ratio))
            self._preview_paned.sash_place(0, 0, sash_position)
            self._preview_ratio_applied = True
        except (AttributeError, tk.TclError):
            if not self._player_closing:
                try:
                    self._preview_ratio_after = self.after(50, self._apply_initial_preview_ratio)
                except tk.TclError:
                    self._preview_ratio_after = None

    def _current_preview_video_ratio(self):
        if not self._preview_ratio_applied:
            return self._preview_video_ratio
        try:
            available_height = self._preview_paned.winfo_height()
            if available_height <= 1:
                return self._preview_video_ratio
            _x, sash_position = self._preview_paned.sash_coord(0)
            return self._validated_video_ratio(sash_position / available_height)
        except (AttributeError, tk.TclError):
            return self._preview_video_ratio

    def _cancel_pending_preview_ratio(self):
        if self._preview_ratio_after is None:
            return
        try:
            self.after_cancel(self._preview_ratio_after)
        except (AttributeError, tk.TclError):
            pass
        self._preview_ratio_after = None

    def _save_name_speakers_view_preferences(self):
        if self._view_preferences_saved:
            return
        self._view_preferences_saved = True
        ratio = self._current_preview_video_ratio()
        try:
            font_size = int(self.transcript_font.cget("size"))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            font_size = self._transcript_default_font_size
        try:
            cfg = read_yaml(conf_path())
            if not isinstance(cfg, dict):
                return
            cfg["name_speakers_video_ratio"] = round(ratio, 4)
            cfg["name_speakers_transcript_font_size"] = max(
                self._TRANSCRIPT_FONT_MIN,
                min(self._TRANSCRIPT_FONT_MAX, font_size),
            )
            atomic_write_yaml(conf_path(), cfg)
            self._preview_video_ratio = ratio
        except Exception:
            pass

    def _set_video_message(self, message):
        self.video_message.configure(text=message)
        self.video_message.place(relx=0.5, rely=0.5, anchor="center")

    def _initialize_embedded_player(self):
        self._vlc_status = get_embedded_vlc_status()
        if not self._vlc_status.get("available"):
            reason = self._vlc_status.get("reason") or "Embedded VLC playback is unavailable."
            self._set_video_message(f"Embedded video unavailable\n{reason}")
            for control in self._video_controls:
                control.state(["disabled"])
            return

        try:
            self._vlc_instance = self._vlc_status["instance"]
            self._vlc_player = self._vlc_instance.media_player_new()
            if self._vlc_player is None:
                raise RuntimeError("LibVLC returned no media player")
            if os.name == "nt":
                self.video_surface.update_idletasks()
                self._vlc_player.set_hwnd(self.video_surface.winfo_id())
            self._vlc_player.audio_set_volume(80)
        except Exception as exc:
            failed_player = self._vlc_player
            self._vlc_player = None
            if failed_player is not None:
                try:
                    failed_player.stop()
                except Exception:
                    pass
                try:
                    failed_player.release()
                except Exception:
                    pass
            self._vlc_status = {
                **self._vlc_status,
                "available": False,
                "reason": f"The embedded media player could not be created: {exc}",
            }
            self._set_video_message(f"Embedded video unavailable\n{self._vlc_status['reason']}")
            for control in self._video_controls:
                control.state(["disabled"])
            return

        self._schedule_video_ui_update()

    @staticmethod
    def _format_video_time(seconds):
        seconds = max(0, int(seconds or 0))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _schedule_video_ui_update(self):
        if self._player_closing or self._vlc_player is None:
            return
        self._video_update_after = self.after(250, self._update_video_ui)

    def _update_video_ui(self):
        self._video_update_after = None
        if self._player_closing or self._vlc_player is None:
            return
        try:
            duration_ms = max(0, int(self._vlc_player.get_length()))
            current_ms = max(0, int(self._vlc_player.get_time()))
            duration = duration_ms / 1000.0
            current = current_ms / 1000.0
            if duration > 0:
                self._video_duration_seconds = duration
                self.video_seek.configure(to=max(1.0, duration))
                current = min(current, duration)
            if not self._seek_dragging:
                self.video_seek_var.set(current)
            self.lbl_video_time.configure(
                text=f"{self._format_video_time(current)} / {self._format_video_time(duration)}"
            )
            self.btn_video_play.configure(text="Pause" if self._vlc_player.is_playing() else "Play")
        except Exception:
            pass
        self._schedule_video_ui_update()

    def _cancel_pending_video_seek(self):
        if self._pending_seek_after is not None:
            try:
                self.after_cancel(self._pending_seek_after)
            except Exception:
                pass
            self._pending_seek_after = None

    def _schedule_video_start_seek(self, seconds, retries=12):
        self._cancel_pending_video_seek()
        target = max(0.0, float(seconds))

        def try_seek(remaining):
            self._pending_seek_after = None
            if self._player_closing or self._vlc_player is None:
                return
            try:
                duration_ms = max(0, int(self._vlc_player.get_length()))
                target_ms = int(target * 1000)
                if duration_ms > 0:
                    target_ms = min(target_ms, duration_ms)
                result = self._vlc_player.set_time(target_ms)
                current_ms = max(0, int(self._vlc_player.get_time()))
                target_reached = target_ms <= 500 or abs(current_ms - target_ms) <= 1500
                if remaining > 0 and (result == -1 or not target_reached):
                    self._pending_seek_after = self.after(150, lambda: try_seek(remaining - 1))
            except Exception:
                if remaining > 0:
                    self._pending_seek_after = self.after(150, lambda: try_seek(remaining - 1))

        self._pending_seek_after = self.after(150, lambda: try_seek(retries))

    def _seek_embedded_video(self, seconds):
        if self._vlc_player is None or self._loaded_video_path is None:
            return
        try:
            duration_ms = max(0, int(self._vlc_player.get_length()))
            target_ms = max(0, int(float(seconds) * 1000))
            if duration_ms > 0:
                target_ms = min(target_ms, duration_ms)
            self._vlc_player.set_time(target_ms)
            self.video_seek_var.set(target_ms / 1000.0)
            self._reset_interpolated_playback_clock(
                target_ms / 1000.0,
                pending_seek=True,
            )
            self._schedule_word_synchronization()
        except Exception:
            pass

    def _video_seek_started(self, _event=None):
        self._seek_dragging = True
        self._rebase_interpolated_playback_clock_from_player()

    def _video_seek_released(self, _event=None):
        self._seek_dragging = False
        self._seek_embedded_video(self.video_seek_var.get())

    def _video_play_pause(self):
        if self._vlc_player is None or self._loaded_video_path is None:
            messagebox.showinfo(
                "Video Preview",
                "Load a video from a transcript hit first.",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            if self._vlc_player.is_playing():
                self._vlc_player.pause()
                self._rebase_interpolated_playback_clock_from_player()
                self.btn_video_play.configure(text="Play")
            else:
                self._rebase_interpolated_playback_clock_from_player()
                self._vlc_player.play()
                self.btn_video_play.configure(text="Pause")
                self._schedule_selected_subtitle_after_play()
                self._schedule_word_synchronization()
        except Exception as exc:
            messagebox.showerror(
                "Video Preview",
                f"Could not control playback:\n{exc}",
                parent=self.winfo_toplevel(),
            )

    def _video_back(self):
        if self._vlc_player is None:
            return
        try:
            current = max(0.0, self._vlc_player.get_time() / 1000.0)
        except Exception:
            current = self.video_seek_var.get()
        self._seek_embedded_video(current - 5.0)

    def _video_forward(self):
        if self._vlc_player is None:
            return
        try:
            current = max(0.0, self._vlc_player.get_time() / 1000.0)
        except Exception:
            current = self.video_seek_var.get()
        self._seek_embedded_video(current + 5.0)

    def _video_stop(self):
        self._cancel_word_synchronization()
        self._clear_current_word()
        self._reset_interpolated_playback_clock()
        self._cancel_pending_video_seek()
        self._cancel_pending_subtitle_apply()
        if self._vlc_player is None:
            return
        try:
            self._vlc_player.stop()
        except Exception:
            pass
        self.video_seek_var.set(0.0)
        self.lbl_video_time.configure(
            text=f"00:00 / {self._format_video_time(self._video_duration_seconds)}"
        )
        self.btn_video_play.configure(text="Play")

    def _video_volume_changed(self, _value=None):
        if self._vlc_player is None:
            return
        try:
            volume = max(0, min(100, int(round(self.video_volume_var.get()))))
            self._vlc_player.audio_set_volume(volume)
        except Exception:
            pass

    def _cancel_pending_subtitle_apply(self):
        if self._pending_subtitle_after is not None:
            try:
                self.after_cancel(self._pending_subtitle_after)
            except Exception:
                pass
            self._pending_subtitle_after = None

    def _subtitle_selection_changed(self, _event=None):
        self._cancel_pending_subtitle_apply()
        self._apply_selected_subtitle(preserve_state=True)

    def _subtitle_playback_snapshot(self):
        if self._vlc_player is None:
            return None
        try:
            current_ms = max(0, int(self._vlc_player.get_time()))
        except Exception:
            current_ms = 0
        try:
            was_playing = bool(self._vlc_player.is_playing())
        except Exception:
            was_playing = False
        try:
            was_paused = "paused" in str(self._vlc_player.get_state()).lower()
        except Exception:
            was_paused = False
        return current_ms, was_playing, was_paused

    def _restore_subtitle_playback_state(self, snapshot):
        if snapshot is None or self._vlc_player is None or self._player_closing:
            return
        saved_time, was_playing, was_paused = snapshot
        try:
            current_time = max(0, int(self._vlc_player.get_time()))
            tolerance = 250 if was_paused else 1500
            if abs(current_time - saved_time) > tolerance:
                self._vlc_player.set_time(saved_time)
            is_playing = bool(self._vlc_player.is_playing())
            if was_playing and not is_playing:
                self._vlc_player.play()
            elif was_paused and is_playing:
                self._vlc_player.pause()
        except Exception:
            pass

    def _set_subtitles_off(self):
        if self._vlc_player is not None:
            try:
                self._vlc_player.video_set_spu(-1)
            except Exception:
                pass
        self._applied_subtitle_choice = "Off"

    def _subtitle_file_candidates(self):
        subtitle_title = self.segments_data.get("title") or self.speakers_json.parent.name
        output_dir = self.speakers_json.parent
        return (
            ("SRT", output_dir / f"{subtitle_title}.srt"),
            ("ASS (plain)", output_dir / f"{subtitle_title}.plain.ass"),
            ("ASS (word highlighting)", output_dir / f"{subtitle_title}.words.ass"),
        )

    def _discover_subtitle_files(self):
        candidates = self._subtitle_file_candidates()
        paths = {label: path for label, path in candidates if path.is_file()}
        choices = ["Off"] + [label for label, _path in candidates if label in paths]
        return paths, choices

    def refresh_available_subtitles(self):
        """Rediscover generated subtitle files without disturbing player state."""
        previous_choice = self.subtitle_var.get() or "Off"
        previous_paths = self._subtitle_paths
        self._subtitle_paths, self._subtitle_choices = self._discover_subtitle_files()
        self.subtitle_selector.configure(values=self._subtitle_choices)
        if previous_choice in self._subtitle_choices:
            self.subtitle_var.set(previous_choice)
            return
        if previous_choice != "Off":
            missing_path = previous_paths.get(previous_choice) or "the selected subtitle file"
            self._report_subtitle_failure(
                previous_choice,
                f"The subtitle file is no longer available:\n{missing_path}\n\nVideo playback will continue without subtitles.",
            )
            return
        self.subtitle_var.set("Off")
        self._set_subtitles_off()

    def _report_subtitle_failure(self, choice, message, remove_choice=False):
        self._subtitle_track_ids.pop(choice, None)
        self._set_subtitles_off()
        self.subtitle_var.set("Off")
        if remove_choice:
            self._subtitle_paths.pop(choice, None)
            self._subtitle_choices = [
                available_choice
                for available_choice in self._subtitle_choices
                if available_choice == "Off" or available_choice in self._subtitle_paths
            ]
            self.subtitle_selector.configure(values=self._subtitle_choices)
        if not self._player_closing:
            messagebox.showwarning(
                "Video subtitles",
                message,
                parent=self.winfo_toplevel(),
            )

    def _available_vlc_subtitle_track_ids(self):
        track_ids = set()
        if self._vlc_player is None:
            return track_ids
        try:
            descriptions = self._vlc_player.video_get_spu_description() or ()
        except Exception:
            descriptions = ()
        for description in descriptions:
            try:
                track_id = int(description[0])
            except (TypeError, ValueError, IndexError):
                continue
            if track_id >= 0:
                track_ids.add(track_id)
        try:
            current_track = int(self._vlc_player.video_get_spu())
        except Exception:
            current_track = -1
        if current_track >= 0:
            track_ids.add(current_track)
        track_ids.update(
            track_id
            for track_id in self._subtitle_track_ids.values()
            if isinstance(track_id, int) and track_id >= 0
        )
        return track_ids

    def _finish_subtitle_apply(
        self,
        choice,
        snapshot,
        retries,
        previous_track_ids=None,
        expected_track_id=None,
    ):
        self._pending_subtitle_after = None
        if (
            self._player_closing
            or self._vlc_player is None
            or self.subtitle_var.get() != choice
        ):
            return
        try:
            selected_track = int(self._vlc_player.video_get_spu())
        except Exception:
            selected_track = -1
        verified_track = None
        if expected_track_id is not None:
            if selected_track == expected_track_id:
                verified_track = expected_track_id
        else:
            previous_track_ids = set(previous_track_ids or ())
            new_track_ids = self._available_vlc_subtitle_track_ids() - previous_track_ids
            if selected_track in new_track_ids:
                verified_track = selected_track
            else:
                for track_id in sorted(new_track_ids):
                    try:
                        result = self._vlc_player.video_set_spu(track_id)
                        current_track = int(self._vlc_player.video_get_spu())
                    except Exception:
                        continue
                    if result != -1 and current_track == track_id:
                        verified_track = track_id
                        break
        self._restore_subtitle_playback_state(snapshot)
        if verified_track is not None:
            self._subtitle_track_ids[choice] = verified_track
            self._applied_subtitle_choice = choice
            return
        if retries > 0:
            self._pending_subtitle_after = self.after(
                150,
                lambda: self._finish_subtitle_apply(
                    choice,
                    snapshot,
                    retries - 1,
                    previous_track_ids,
                    expected_track_id,
                ),
            )
            return
        subtitle_path = self._subtitle_paths.get(choice)
        self._report_subtitle_failure(
            choice,
            f"VLC could not activate the subtitle file:\n{subtitle_path}\n\nVideo playback will continue without subtitles.",
        )

    def _apply_selected_subtitle(self, preserve_state=True):
        if self._vlc_player is None or self._loaded_video_path is None:
            return
        choice = self.subtitle_var.get() or "Off"
        snapshot = self._subtitle_playback_snapshot() if preserve_state else None
        if choice == "Off":
            self._set_subtitles_off()
            self._restore_subtitle_playback_state(snapshot)
            return

        subtitle_path = self._subtitle_paths.get(choice)
        if subtitle_path is None or not subtitle_path.is_file():
            missing_path = subtitle_path or "the selected subtitle file"
            self._report_subtitle_failure(
                choice,
                f"The subtitle file is no longer available:\n{missing_path}\n\nVideo playback will continue without subtitles.",
                remove_choice=True,
            )
            self._restore_subtitle_playback_state(snapshot)
            return

        try:
            existing_track = self._subtitle_track_ids.get(choice)
            previous_track_ids = None
            if existing_track is not None:
                result = self._vlc_player.video_set_spu(existing_track)
                if result == -1:
                    self._subtitle_track_ids.pop(choice, None)
                    existing_track = None
            if existing_track is None:
                import vlc

                previous_track_ids = frozenset(self._available_vlc_subtitle_track_ids())
                subtitle_uri = subtitle_path.resolve().as_uri()
                result = self._vlc_player.add_slave(vlc.MediaSlaveType.subtitle, subtitle_uri, True)
                if result == -1:
                    raise RuntimeError("LibVLC rejected the subtitle track")
        except Exception as exc:
            self._report_subtitle_failure(
                choice,
                f"VLC could not load the subtitle file:\n{subtitle_path}\n\n{exc}\n\nVideo playback will continue without subtitles.",
            )
            self._restore_subtitle_playback_state(snapshot)
            return

        self._pending_subtitle_after = self.after(
            150,
            lambda: self._finish_subtitle_apply(
                choice,
                snapshot,
                12,
                previous_track_ids,
                existing_track,
            ),
        )

    def _schedule_selected_subtitle_after_play(self):
        self._cancel_pending_subtitle_apply()

        def apply_after_play():
            self._pending_subtitle_after = None
            self._apply_selected_subtitle(preserve_state=False)

        self._pending_subtitle_after = self.after(250, apply_after_play)

    def _load_embedded_video(self, video_path, start_seconds):
        if self._vlc_player is None or self._vlc_instance is None:
            raise RuntimeError(self._vlc_status.get("reason") or "Embedded VLC playback is unavailable.")

        video_path = Path(video_path).resolve()
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")
        normalized_path = os.path.normcase(str(video_path))
        same_video = normalized_path == self._loaded_video_path and self._vlc_media is not None

        self._cancel_pending_video_seek()
        self._cancel_pending_subtitle_apply()
        if not same_video:
            self._vlc_player.stop()
            media = self._vlc_instance.media_new(str(video_path))
            if media is None:
                raise RuntimeError("LibVLC could not create media for the selected video")
            old_media = self._vlc_media
            self._vlc_player.set_media(media)
            self._vlc_media = media
            self._loaded_video_path = normalized_path
            self._subtitle_track_ids.clear()
            self._applied_subtitle_choice = None
            if old_media is not None:
                try:
                    old_media.release()
                except Exception:
                    pass

        if os.name == "nt":
            self._vlc_player.set_hwnd(self.video_surface.winfo_id())
        self.video_message.place_forget()
        self._vlc_player.audio_set_volume(max(0, min(100, int(self.video_volume_var.get()))))
        if self._vlc_player.play() == -1:
            raise RuntimeError("LibVLC could not start playback")
        self.btn_video_play.configure(text="Pause")
        self._reset_interpolated_playback_clock(start_seconds, pending_seek=True)
        self._schedule_video_start_seek(start_seconds)
        self._schedule_selected_subtitle_after_play()
        self._schedule_word_synchronization()

    def _release_embedded_player(self):
        if self._player_closing:
            return
        self._save_name_speakers_view_preferences()
        self._player_closing = True
        self._cancel_pending_preview_ratio()
        self._cancel_word_synchronization()
        self._clear_current_word()
        self._reset_interpolated_playback_clock()
        self._cancel_pending_video_seek()
        self._cancel_pending_subtitle_apply()
        if self._video_reattach_after is not None:
            try:
                self.after_cancel(self._video_reattach_after)
            except (AttributeError, tk.TclError):
                pass
            self._video_reattach_after = None
        if self._video_update_after is not None:
            try:
                self.after_cancel(self._video_update_after)
            except Exception:
                pass
            self._video_update_after = None
        player = self._vlc_player
        media = self._vlc_media
        self._vlc_player = None
        self._vlc_media = None
        if player is not None:
            try:
                player.stop()
            except Exception:
                pass
            try:
                player.release()
            except Exception:
                pass
        if media is not None:
            try:
                media.release()
            except Exception:
                pass

    def shutdown(self):
        self._release_embedded_player()

    def _on_workspace_destroyed(self, event):
        if event.widget is self:
            self.shutdown()

    def discard_changes(self):
        if self._on_discard is not None:
            self._on_discard()

    def _current_name_mapping(self):
        return {
            speaker: self.inputs[speaker].get().strip()
            for speaker in self.speakers
            if self.inputs[speaker].get().strip()
        }

    def open_segment_corrections(self):
        if not self.segments:
            messagebox.showinfo(
                "No transcript segments",
                "There are no transcript segments to review.",
                parent=self.winfo_toplevel(),
            )
            return
        owner = self.winfo_toplevel()
        dialog = SegmentCorrectionDialog(
            owner,
            self.segments,
            self.speakers,
            self._current_name_mapping(),
        )
        owner.wait_window(dialog)
        if dialog.result is None:
            return
        self.segments = dialog.result
        self.manual_corrections_pending = dialog.changed or self.manual_corrections_pending
        self._refresh_transcript_preview()

    def _configure_transcript_word_tags(self):
        try:
            style = tb.Style.get_instance() or tb.Style()
            hover_color = style.colors.primary
            current_background = style.colors.primary
            current_foreground = style.colors.get_foreground("primary")
        except Exception:
            hover_color = "#0d6efd"
            current_background = "#0d6efd"
            current_foreground = "#ffffff"
        self._transcript_default_cursor = self.text.cget("cursor") or "xterm"
        self.text.tag_configure("clickable_word")
        self.text.tag_configure("hover_word", foreground=hover_color, underline=True)
        self.text.tag_configure(
            "current_word",
            background=current_background,
            foreground=current_foreground,
        )
        self.text.tag_bind("clickable_word", "<Enter>", self._on_clickable_word_enter)
        self.text.tag_bind("clickable_word", "<Leave>", self._on_clickable_word_leave)
        self.text.tag_bind("clickable_word", "<Button-1>", self._on_clickable_word_click)
        self.text.bind("<MouseWheel>", self._on_manual_transcript_scroll, add="+")
        self.text.bind("<Button-4>", self._on_manual_transcript_scroll, add="+")
        self.text.bind("<Button-5>", self._on_manual_transcript_scroll, add="+")
        self.text.tag_raise("current_word")

    def _clear_transcript_word_mappings(self):
        self.text.tag_remove("hover_word", "1.0", "end")
        self.text.tag_remove("clickable_word", "1.0", "end")
        self.text.configure(cursor=self._transcript_default_cursor)
        for tag_name in self._word_tag_names:
            try:
                self.text.tag_delete(tag_name)
            except tk.TclError:
                pass
        self._word_records = []
        self._word_tag_to_record = {}
        self._word_tag_names = []
        self._timed_word_index = []
        self._timed_word_starts = []

    @staticmethod
    def _valid_word_time_range(word):
        start = word.get("start")
        end = word.get("end")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            return None
        if isinstance(end, bool) or not isinstance(end, (int, float)):
            return None
        start = float(start)
        end = float(end)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            return None
        return start, end

    def _insert_segment_with_word_tags(self, segment, segment_index, segment_text):
        words = segment.get("words")
        if not isinstance(words, list):
            self.text.insert("end", segment_text)
            return

        matches = []
        search_from = 0
        for word_index, word in enumerate(words):
            if not isinstance(word, dict):
                continue
            raw_word = word.get("word")
            if raw_word is None:
                raw_word = word.get("text")
            word_text = str(raw_word or "")
            if not word_text.strip():
                continue
            match_start = segment_text.find(word_text, search_from)
            if match_start < 0:
                continue
            match_end = match_start + len(word_text)
            matches.append((match_start, match_end, word_index, word, self._valid_word_time_range(word)))
            search_from = match_end

        inserted_to = 0
        for match_start, match_end, word_index, word, timing in matches:
            if match_start > inserted_to:
                self.text.insert("end", segment_text[inserted_to:match_start])
            text_start = self.text.index("end-1c")
            displayed_word = segment_text[match_start:match_end]
            self.text.insert("end", displayed_word)
            text_end = self.text.index("end-1c")
            if timing is not None:
                tag_name = f"transcript_word_{segment_index}_{word_index}"
                record = {
                    "tag": tag_name,
                    "text_start": text_start,
                    "text_end": text_end,
                    "display_text": displayed_word,
                    "media_start": timing[0],
                    "media_end": timing[1],
                    "segment_index": segment_index,
                    "word_index": word_index,
                    "segment_speaker": segment.get("speaker"),
                    "word_speaker": word.get("speaker"),
                }
                self.text.tag_add(tag_name, text_start, text_end)
                self.text.tag_add("clickable_word", text_start, text_end)
                self._word_records.append(record)
                self._word_tag_to_record[tag_name] = record
                self._word_tag_names.append(tag_name)
            inserted_to = match_end
        if inserted_to < len(segment_text):
            self.text.insert("end", segment_text[inserted_to:])

    def _render_transcript_preview(self):
        self._clear_transcript_word_mappings()
        self.text.delete("1.0", "end")
        mapping = self._current_name_mapping()
        diarized = any(
            (segment.get("speaker") or "")
            for segment in self.segments
            if isinstance(segment, dict)
        )
        last_speaker = None
        rendered_any = False

        for segment_index, segment in enumerate(self.segments):
            if not isinstance(segment, dict):
                continue
            segment_text = str(segment.get("text", "") or "").strip()
            if not segment_text:
                continue
            if diarized:
                speaker = segment.get("speaker") or "SPEAKER_00"
                if speaker != last_speaker:
                    if rendered_any:
                        self.text.insert("end", "\n")
                    assigned_name = mapping.get(speaker)
                    speaker_label = f"{speaker} ({assigned_name})" if assigned_name else speaker
                    self.text.insert("end", f"{speaker_label}: ")
                elif rendered_any:
                    self.text.insert("end", " ")
                last_speaker = speaker
            elif rendered_any:
                self.text.insert("end", " ")
            self._insert_segment_with_word_tags(segment, segment_index, segment_text)
            rendered_any = True
        self.text.edit_reset()
        self._rebuild_timed_word_index()

    def _rebuild_timed_word_index(self):
        self._timed_word_index = sorted(
            self._word_records,
            key=lambda record: (
                record["media_start"],
                record["media_end"],
                record["segment_index"],
                record["word_index"],
            ),
        )
        self._timed_word_starts = [record["media_start"] for record in self._timed_word_index]
        self._rebase_interpolated_playback_clock_from_player()

    def _timed_word_at_playback_time(self, current_seconds):
        if not self._timed_word_index:
            return None
        try:
            current_seconds = float(current_seconds)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(current_seconds):
            return None
        index = bisect_right(self._timed_word_starts, current_seconds) - 1
        if index < 0:
            return None
        record = self._timed_word_index[index]
        if record["media_start"] <= current_seconds < record["media_end"]:
            return record
        return None

    def _clear_current_word(self):
        if self._current_word_tag is None:
            return
        try:
            self.text.tag_remove("current_word", "1.0", "end")
        except (AttributeError, tk.TclError):
            pass
        self._current_word_tag = None

    def _current_word_is_visible(self, text_index):
        try:
            bounds = self.text.bbox(text_index)
            if bounds is None:
                return False
            _x, y, _width, height = bounds
            widget_height = self.text.winfo_height()
            return y >= 0 and y + height <= widget_height
        except (AttributeError, tk.TclError):
            return False

    def _set_current_word(self, record):
        next_tag = record.get("tag") if record is not None else None
        if next_tag == self._current_word_tag:
            return
        self._clear_current_word()
        if record is None:
            return
        live_range = self._live_word_tag_range(record)
        if live_range is None:
            return
        try:
            self.text.tag_add("current_word", live_range[0], live_range[1])
            self.text.tag_raise("current_word")
            self._current_word_tag = next_tag
            if (
                time.monotonic() >= self._transcript_follow_suspended_until
                and not self._current_word_is_visible(live_range[0])
            ):
                self.text.see(live_range[0])
        except (AttributeError, tk.TclError):
            self._current_word_tag = None

    def _cancel_word_synchronization(self):
        if self._word_sync_after is None:
            return
        try:
            self.after_cancel(self._word_sync_after)
        except (AttributeError, tk.TclError):
            pass
        self._word_sync_after = None

    def _reset_interpolated_playback_clock(self, media_seconds=None, *, pending_seek=False):
        now = time.monotonic()
        if media_seconds is None:
            self._word_clock_vlc_seconds = None
            self._word_clock_vlc_observed_at = None
            self._word_clock_estimate_seconds = None
            self._word_clock_seek_target = None
            self._word_clock_seek_started_at = None
            return
        try:
            media_seconds = float(media_seconds)
        except (TypeError, ValueError):
            self._reset_interpolated_playback_clock()
            return
        if not math.isfinite(media_seconds):
            self._reset_interpolated_playback_clock()
            return
        media_seconds = max(0.0, media_seconds)
        self._word_clock_vlc_seconds = None if pending_seek else media_seconds
        self._word_clock_vlc_observed_at = now
        self._word_clock_estimate_seconds = media_seconds
        self._word_clock_seek_target = media_seconds if pending_seek else None
        self._word_clock_seek_started_at = now if pending_seek else None

    def _reported_vlc_playback_seconds(self):
        if self._vlc_player is None:
            return None
        try:
            reported_ms = int(self._vlc_player.get_time())
        except Exception:
            return None
        return reported_ms / 1000.0 if reported_ms >= 0 else None

    def _rebase_interpolated_playback_clock_from_player(self):
        self._reset_interpolated_playback_clock(self._reported_vlc_playback_seconds())

    def _interpolated_playback_time(
        self,
        reported_seconds,
        is_playing,
        duration_seconds=0.0,
    ):
        try:
            reported_seconds = max(0.0, float(reported_seconds))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(reported_seconds):
            return None
        try:
            duration_seconds = max(0.0, float(duration_seconds))
        except (TypeError, ValueError):
            duration_seconds = 0.0
        if not math.isfinite(duration_seconds):
            duration_seconds = 0.0

        now = time.monotonic()
        allow_seek_correction = False
        if self._word_clock_seek_target is not None:
            seek_target = self._word_clock_seek_target
            seek_started_at = self._word_clock_seek_started_at or now
            seek_elapsed = max(0.0, now - seek_started_at)
            reading_matches_seek = abs(reported_seconds - seek_target) <= 0.75
            if not reading_matches_seek and seek_elapsed < 2.0:
                estimate = seek_target + (seek_elapsed if is_playing else 0.0)
                previous_estimate = self._word_clock_estimate_seconds
                if is_playing and previous_estimate is not None:
                    estimate = max(previous_estimate, estimate)
                if duration_seconds > 0:
                    estimate = min(estimate, duration_seconds)
                self._word_clock_estimate_seconds = estimate
                return estimate
            allow_seek_correction = not reading_matches_seek
            self._word_clock_seek_target = None
            self._word_clock_seek_started_at = None

        if not is_playing:
            estimate = reported_seconds
            self._word_clock_vlc_seconds = reported_seconds
            self._word_clock_vlc_observed_at = now
        elif self._word_clock_vlc_seconds is None:
            estimate = reported_seconds
            self._word_clock_vlc_seconds = reported_seconds
            self._word_clock_vlc_observed_at = now
        elif reported_seconds != self._word_clock_vlc_seconds:
            estimate = reported_seconds
            if not allow_seek_correction and self._word_clock_estimate_seconds is not None:
                estimate = max(self._word_clock_estimate_seconds, estimate)
            self._word_clock_vlc_seconds = reported_seconds
            self._word_clock_vlc_observed_at = now
        else:
            observed_at = self._word_clock_vlc_observed_at or now
            estimate = reported_seconds + max(0.0, now - observed_at)
            if self._word_clock_estimate_seconds is not None:
                estimate = max(self._word_clock_estimate_seconds, estimate)

        if duration_seconds > 0:
            estimate = min(estimate, duration_seconds)
        self._word_clock_estimate_seconds = estimate
        return estimate

    def _schedule_word_synchronization(self):
        if (
            self._word_sync_after is not None
            or self._player_closing
            or self._vlc_player is None
            or self._loaded_video_path is None
        ):
            return
        try:
            if not self.winfo_exists() or not self.text.winfo_exists():
                return
            self._word_sync_after = self.after(
                self._word_sync_interval_ms,
                self._synchronize_current_word,
            )
        except (AttributeError, tk.TclError):
            self._word_sync_after = None

    def _synchronize_current_word(self):
        self._word_sync_after = None
        if self._player_closing or self._vlc_player is None or self._loaded_video_path is None:
            return
        try:
            if not self.winfo_exists() or not self.text.winfo_exists():
                return
            current_ms = int(self._vlc_player.get_time())
            duration_ms = max(0, int(self._vlc_player.get_length()))
            try:
                is_playing = bool(self._vlc_player.is_playing())
            except Exception:
                is_playing = False
            try:
                state = self._vlc_player.get_state()
                player_state = getattr(state, "name", None)
                if player_state is None:
                    player_state = str(state)
            except Exception:
                player_state = ""
            if "ended" in str(player_state).lower() or (
                duration_ms > 0 and current_ms >= duration_ms
            ):
                self._clear_current_word()
                self._reset_interpolated_playback_clock()
                return
            current_seconds = (
                self._interpolated_playback_time(
                    current_ms / 1000.0,
                    is_playing,
                    duration_ms / 1000.0,
                )
                if current_ms >= 0
                else None
            )
            record = (
                self._timed_word_at_playback_time(current_seconds)
                if current_seconds is not None
                else None
            )
            self._set_current_word(record)
        except Exception:
            pass
        self._schedule_word_synchronization()

    def _suspend_transcript_following(self):
        self._transcript_follow_suspended_until = time.monotonic() + 3.0

    def _on_manual_transcript_scroll(self, event=None):
        if event is not None and getattr(event, "state", 0) & 0x0004:
            return "break"
        self._suspend_transcript_following()

    def _on_transcript_scrollbar(self, *args):
        self._suspend_transcript_following()
        self.text.yview(*args)

    def _enable_transcript_following(self):
        self._transcript_follow_suspended_until = 0.0

    def _live_word_tag_range(self, record):
        try:
            ranges = self.text.tag_ranges(record["tag"])
            if len(ranges) != 2:
                return None
            if self.text.get(ranges[0], ranges[1]) != record["display_text"]:
                return None
            return str(ranges[0]), str(ranges[1])
        except (KeyError, tk.TclError):
            return None

    def _word_record_at_event(self, event):
        try:
            index = self.text.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        for tag_name in self.text.tag_names(index):
            record = self._word_tag_to_record.get(tag_name)
            if record is not None and self._live_word_tag_range(record) is not None:
                return record
        return None

    def _on_clickable_word_enter(self, event):
        record = self._word_record_at_event(event)
        self.text.tag_remove("hover_word", "1.0", "end")
        if record is None:
            self.text.configure(cursor=self._transcript_default_cursor)
            return
        live_range = self._live_word_tag_range(record)
        if live_range is None:
            self.text.configure(cursor=self._transcript_default_cursor)
            return
        self.text.tag_add("hover_word", live_range[0], live_range[1])
        self.text.configure(cursor="hand2")

    def _on_clickable_word_leave(self, _event=None):
        self.text.tag_remove("hover_word", "1.0", "end")
        self.text.configure(cursor=self._transcript_default_cursor)

    def _video_path_for_timed_word(self):
        if self._loaded_video_path:
            loaded_path = Path(self._loaded_video_path)
            if loaded_path.is_file():
                return loaded_path, None

        source_path = self.segments_data.get("source_path") if isinstance(self.segments_data, dict) else None
        if not source_path:
            return None, "The original video path is not available for this transcript."
        try:
            source = Path(source_path).expanduser()
        except (TypeError, ValueError):
            return None, "The original video path is invalid."
        if not source.is_file():
            return None, "The original video file is missing. Use Preview video at hit to locate it first."
        if source.suffix.lower() in _PIPELINE_AUDIO_EXTS:
            return None, "Clickable word playback requires a video source; this transcript contains audio only."
        if source.suffix.lower() not in _PIPELINE_VIDEO_EXTS:
            return None, "The original source is not a supported video file."
        return source, None

    def _play_timed_word(self, record):
        if self._vlc_player is None or self._vlc_instance is None:
            reason = self._vlc_status.get("reason") or "Embedded VLC playback is unavailable."
            messagebox.showinfo("Video Preview", reason, parent=self.winfo_toplevel())
            return
        video_path, unavailable_reason = self._video_path_for_timed_word()
        if video_path is None:
            messagebox.showinfo(
                "Video Preview",
                unavailable_reason,
                parent=self.winfo_toplevel(),
            )
            return
        try:
            self._enable_transcript_following()
            self._reset_interpolated_playback_clock(
                max(0.0, float(record["media_start"])),
                pending_seek=True,
            )
            self._load_embedded_video(video_path, max(0.0, float(record["media_start"])))
        except Exception as exc:
            self._rebase_interpolated_playback_clock_from_player()
            messagebox.showwarning(
                "Video Preview",
                f"Could not play the selected word:\n{exc}",
                parent=self.winfo_toplevel(),
            )

    def _on_clickable_word_click(self, event):
        record = self._word_record_at_event(event)
        if record is None:
            return None
        self._play_timed_word(record)
        return "break"

    def _transcript_content_from_segments(self):
        mapping = self._current_name_mapping()
        diarized = any((seg.get("speaker") or "") for seg in self.segments if isinstance(seg, dict))
        lines = []
        if diarized:
            last_speaker = None
            buffer = []

            def flush():
                nonlocal buffer, last_speaker
                if not buffer or last_speaker is None:
                    return
                transcript = " ".join(buffer).strip()
                if transcript:
                    assigned_name = mapping.get(last_speaker)
                    speaker_label = f"{last_speaker} ({assigned_name})" if assigned_name else last_speaker
                    lines.append(f"{speaker_label}: {transcript}")
                buffer = []

            for segment in self.segments:
                if not isinstance(segment, dict):
                    continue
                transcript = str(segment.get("text", "") or "").strip()
                if not transcript:
                    continue
                speaker = segment.get("speaker") or "SPEAKER_00"
                if speaker != last_speaker and last_speaker is not None:
                    flush()
                last_speaker = speaker
                buffer.append(transcript)
            flush()
        else:
            transcript = " ".join(
                str(segment.get("text", "") or "").strip()
                for segment in self.segments
                if isinstance(segment, dict) and segment.get("text")
            ).strip()
            if transcript:
                lines.append(transcript)
        return "\n".join(lines)

    def _refresh_transcript_preview(self):
        restart_synchronization = (
            not self._player_closing
            and self._vlc_player is not None
            and self._loaded_video_path is not None
        )
        self._cancel_word_synchronization()
        self._clear_current_word()
        self._render_transcript_preview()
        query = self.find_var.get().strip() if hasattr(self, "find_var") else ""
        self._highlight_query(query)
        if restart_synchronization:
            self._schedule_word_synchronization()

    def _load_transcript(self):
        out_dir = self.speakers_json.parent
        base = self.title_name
        txt_path = out_dir / f"{base}.txt"
        if txt_path.exists():
            try:
                content = txt_path.read_text(encoding="utf-8", errors="replace")
            except:
                content = ""
        else:
            diarized = any((seg.get("speaker") or "") for seg in self.segments)
            lines = []
            if diarized:
                last = None
                buf = []
                def flush():
                    nonlocal buf, last
                    if not buf or last is None: return
                    text = " ".join(buf).strip()
                    if text: lines.append(f"{last}: {text}")
                    buf.clear()
                for seg in self.segments:
                    t = str(seg.get("text","")).strip()
                    if not t: continue
                    sp = seg.get("speaker") or "SPEAKER_00"
                    if sp != last and last is not None: flush()
                    last = sp; buf.append(t)
                flush()
            else:
                line = " ".join(str(seg.get("text","")).strip() for seg in self.segments if seg.get("text")).strip()
                if line: lines.append(line)
            content = "\n".join(lines)
        return txt_path, content

    def _reset_highlight(self):
        self.text.tag_delete("find")
        self.text.tag_configure("find", background="#fff59d")
        self.text.tag_raise("current_word")

    def _highlight_query(self, query: str):
        self._reset_highlight()
        if not query:
            return
        start = "1.0"
        while True:
            pos = self.text.search(query, start, stopindex="end", nocase=False)
            if not pos:
                break
            end = f"{pos}+{len(query)}c"
            self.text.tag_add("find", pos, end)
            start = end
        first = self.text.search(query, "1.0", stopindex="end")
        if first:
            self.text.see(first)
            self.text.mark_set("insert", first)
        self.text.tag_raise("current_word")

    def find_next(self):
        self._suspend_transcript_following()
        query = self.find_var.get().strip()
        if not query:
            return
        idx = self.text.index("insert")
        pos = self.text.search(query, f"{idx}+1c", stopindex="end")
        if not pos:
            pos = self.text.search(query, "1.0", stopindex="end")
        if not pos:
            return
        self.text.see(pos)
        self.text.mark_set("insert", pos)
        self._highlight_query(query)

    def find_speaker_tag(self):
        self._suspend_transcript_following()
        tag = self.find_speaker_var.get().strip()
        if not tag:
            return
        self.find_var.set(tag)
        self._highlight_query(tag)

    def open_txt_external(self):
        if self.txt_path and self.txt_path.exists():
            os.startfile(str(self.txt_path))
        else:
            messagebox.showinfo(
                "No .txt file",
                "Transcript .txt not found on disk; showing generated preview only.",
                parent=self.winfo_toplevel(),
            )

    def _select_srt_hit_for_preview(self):
        query = (self.find_var.get() if hasattr(self, "find_var") else "").strip()
        if not query:
            messagebox.showinfo(
                "Jump by SRT",
                "Type something in the Find box first, then try again.",
                parent=self.winfo_toplevel(),
            )
            return None
        out_dir = self.speakers_json.parent
        srt_path = out_dir / f"{self.title_name}.srt"
        if not srt_path.exists():
            messagebox.showinfo(
                "Jump by SRT",
                f"SRT not found:\n{srt_path.name}\n\nRun transcription first or enable SRT output.",
                parent=self.winfo_toplevel(),
            )
            return None
        try:
            segments = parse_srt_segments(srt_path)
        except Exception as exc:
            messagebox.showerror(
                "Jump by SRT",
                f"Could not read SRT:\n{exc}",
                parent=self.winfo_toplevel(),
            )
            return None
        hits = find_segments_matching_query(segments, query)
        if not hits:
            messagebox.showinfo(
                "Jump by SRT",
                f"No SRT lines matched:\n\"{query}\"",
                parent=self.winfo_toplevel(),
            )
            return None
        if len(hits) == 1:
            return srt_path, hits[0]
        owner = self.winfo_toplevel()
        dialog = _SrtHitsDialog(owner, hits)
        owner.wait_window(dialog)
        if dialog.result is None:
            return None
        return srt_path, dialog.result

    def _locate_video_for_preview(self, srt_path):
        try:
            project_root = program_root()
        except Exception:
            project_root = Path(".")
        video_path = guess_video_for_srt(srt_path, project_root)
        if video_path:
            return Path(video_path)
        selected = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title=f"Locate original video for {self.title_name}",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All Files", "*.*")],
        )
        return Path(selected) if selected else None

    def _open_selected_hit_externally(self, srt_path, chosen):
        try:
            start = max(0.0, float(chosen["start"]))
            cfg = read_yaml(conf_path()) if callable(globals().get("read_yaml")) else {}
            vlc_p_str = cfg.get("video_player_path")
            vlc_path = Path(vlc_p_str) if vlc_p_str else None
            try:
                jump_video_to_srt_time(srt_path, start, vlc_path=vlc_path)
            except FileNotFoundError:
                video_path = filedialog.askopenfilename(
                    parent=self.winfo_toplevel(),
                    title=f"Locate original video for {self.title_name}",
                    filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All Files", "*.*")],
                )
                if not video_path:
                    return
                video_path = Path(video_path)
                if not _open_in_vlc(video_path, start, vlc_path=vlc_path):
                    _open_in_ffplay(video_path, start)
        except Exception as exc:
            messagebox.showerror(
                "Jump by SRT",
                f"Failed to open player:\n{exc}",
                parent=self.winfo_toplevel(),
            )

    def preview_video_at_query(self):
        selected_hit = self._select_srt_hit_for_preview()
        if selected_hit is None:
            return
        srt_path, chosen = selected_hit
        if self._vlc_player is None:
            reason = self._vlc_status.get("reason") or "Embedded VLC playback is unavailable."
            if messagebox.askyesno(
                "Embedded video unavailable",
                f"{reason}\n\nOpen this hit in the external video player instead?",
                parent=self.winfo_toplevel(),
            ):
                self._open_selected_hit_externally(srt_path, chosen)
            return
        try:
            video_path = self._locate_video_for_preview(srt_path)
            if video_path is None:
                return
            start = max(0.0, float(chosen["start"]))
            self._load_embedded_video(video_path, start)
        except Exception as exc:
            messagebox.showerror(
                "Video Preview",
                f"Failed to preview video:\n{exc}",
                parent=self.winfo_toplevel(),
            )

    def open_video_at_query(self):
        query = (self.find_var.get() if hasattr(self, "find_var") else "").strip()
        if not query:
            messagebox.showinfo(
                "Jump by SRT",
                "Type something in the Find box first, then try again.",
                parent=self.winfo_toplevel(),
            )
            return
        out_dir = self.speakers_json.parent
        srt_path = out_dir / f"{self.title_name}.srt"
        if not srt_path.exists():
            messagebox.showinfo(
                "Jump by SRT",
                f"SRT not found:\n{srt_path.name}\n\nRun transcription first or enable SRT output.",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            segs = parse_srt_segments(srt_path)
        except Exception as e:
            messagebox.showerror(
                "Jump by SRT",
                f"Could not read SRT:\n{e}",
                parent=self.winfo_toplevel(),
            )
            return
        hits = find_segments_matching_query(segs, query)
        if not hits:
            messagebox.showinfo(
                "Jump by SRT",
                f"No SRT lines matched:\n“{query}”",
                parent=self.winfo_toplevel(),
            )
            return
        if len(hits) > 1:
            owner = self.winfo_toplevel()
            dlg = _SrtHitsDialog(owner, hits)
            owner.wait_window(dlg)
            if not dlg.result:
                return
            chosen = dlg.result
        else:
            chosen = hits[0]
        try:
            start = max(0.0, float(chosen["start"]))
            cfg = read_yaml(conf_path()) if callable(globals().get("read_yaml")) else {}
            vlc_p_str = cfg.get("video_player_path")
            vlc_path = Path(vlc_p_str) if vlc_p_str else None
            
            # Use improved fallback logic directly here
            # First attempt: automatic
            try:
                jump_video_to_srt_time(srt_path, start, vlc_path=vlc_path)
            except FileNotFoundError:
                # If automatic fails, ask user to point to file
                vid = filedialog.askopenfilename(
                    parent=self.winfo_toplevel(),
                    title=f"Locate original video for {self.title_name}",
                    filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All Files", "*.*")]
                )
                if vid:
                    path_vid = Path(vid)
                    # Manually launch with the user-selected video
                    if not _open_in_vlc(path_vid, start, vlc_path=vlc_path):
                        _open_in_ffplay(path_vid, start)
                else:
                    return # User cancelled
        except Exception as e:
            messagebox.showerror(
                "Jump by SRT",
                f"Failed to open player:\n{e}",
                parent=self.winfo_toplevel(),
            )
            return

    def _prefill_first_two(self, per_spk_counts: dict, global_counts: list, title_counts: list):
        order = []
        seen = set()
        filled = 0
        for seg in self.segments:
            sp = seg.get("speaker")
            if not sp or sp in seen: continue
            seen.add(sp); order.append(sp)
            if len(order) >= 2: break
        for sp in order:
            cb = self.inputs.get(sp)
            if not cb: continue
            if cb.get().strip(): continue
            candidates = _best_auto_speaker_names(per_spk_counts.get(sp, Counter()), global_counts, title_counts, limit=8)
            if candidates:
                cb.set(candidates[0])
                filled += 1
        return filled

    def _rename_tree(self, out_dir: Path, mapping: dict):
        spk_dirs = [d for d in out_dir.iterdir() if d.is_dir() and d.name.startswith("SPEAKER_")]
        if spk_dirs:
            for d in spk_dirs:
                tag = d.name
                new_base = safe_base(mapping.get(tag, tag))
                new_dir = out_dir / new_base
                if new_dir.exists() and new_dir != d:
                    new_dir = unique_path(new_dir)
                if new_dir != d:
                    try:
                        d.rename(new_dir)
                    except:
                        continue
                    d = new_dir
                for f in sorted(d.glob("*.wav")):
                    num = f.stem
                    new_file = d / f"{new_base}_{num}.wav"
                    if new_file.exists() and new_file != f:
                        new_file = unique_path(new_file)
                    if new_file != f:
                        try:
                            f.rename(new_file)
                        except:
                            pass
        else:
            for f in sorted(out_dir.glob("*.wav")):
                stem = f.stem
                if "_" not in stem: continue
                prefix, rest = stem.split("_", 1)
                if not prefix.startswith("SPEAKER_"): continue
                new_base = safe_base(mapping.get(prefix, prefix))
                new_file = out_dir / f"{new_base}_{rest}.wav"
                if new_file.exists() and new_file != f:
                    new_file = unique_path(new_file)
                if new_file != f:
                    try:
                        f.rename(new_file)
                    except:
                        pass

    def apply_changes(self):
        mapping = self._current_name_mapping()
        try:
            _spk = json.loads(self.speakers_json.read_text(encoding='utf-8'))
            _spk['names'] = mapping
            self.speakers_json.write_text(json.dumps(_spk, ensure_ascii=False, indent=2), encoding='utf-8')
        except:
            pass
        if not mapping and not self.manual_corrections_pending:
            messagebox.showwarning(
                "Nothing to apply",
                "Please enter at least one name.",
                parent=self.winfo_toplevel(),
            )
            return
        segments = self.segments
        seg_data = self.segments_data
        if self.manual_corrections_pending:
            try:
                seg_data = _write_corrected_segments_json(
                    self.segments_json,
                    self.segments_data,
                    segments,
                    create_backup=True,
                )
                self.segments_data = seg_data
            except Exception as e:
                messagebox.showerror(
                    "Save corrections failed",
                    f"Could not update segments.json:\n{e}",
                    parent=self.winfo_toplevel(),
                )
                return
        out_dir = self.speakers_json.parent
        title = seg_data.get("title") or out_dir.name
        srt_path = out_dir / f"{title}.srt"
        txt_path = out_dir / f"{title}.txt"
        if self.var_overwrite.get():
            srt_tmp = srt_path
            txt_tmp = txt_path
        else:
            srt_tmp = out_dir / f"{title}.named.srt"
            txt_tmp = out_dir / f"{title}.named.txt"
        with srt_tmp.open("w", encoding="utf-8", newline="\n") as f:
            for idx, seg in enumerate(segments, 1):
                start = float(seg.get("start", 0.0))
                end = float(seg.get("end", start))
                text = str(seg.get("text", "")).strip()
                spk = seg.get("speaker")
                disp = mapping.get(spk, spk) if spk else text
                line = f"{disp}: {text}" if spk else text
                f.write(str(idx)); f.write("\n")
                f.write(f"{srt_timestamp(start)} --> {srt_timestamp(end)}"); f.write("\n")
                f.write(line); f.write("\n\n")
        diarized = any((seg.get("speaker") or "") for seg in segments)
        with txt_tmp.open("w", encoding="utf-8", newline="\n") as f:
            if diarized:
                last = None
                buf = []
                def flush():
                    nonlocal buf, last
                    if not buf or last is None: return
                    text = " ".join(buf).strip()
                    if text: f.write(f"{mapping.get(last, last)}: {text}\n")
                    buf = []
                for seg in segments:
                    t = str(seg.get("text", "")).strip()
                    if not t: continue
                    sp = seg.get("speaker") or "SPEAKER_00"
                    if sp != last and last is not None: flush()
                    last = sp; buf.append(t)
                flush()
            else:
                all_text = " ".join(str(seg.get("text", "")).strip() for seg in segments if seg.get("text")).strip()
                if all_text: f.write(all_text + "\n")
        export_created = []
        try:
            if _has_word_level(segments):
                if self.var_export_vtt.get():
                    vp = out_dir / f"{title}.words.vtt"
                    write_word_vtt(vp, segments, mapping)
                    export_created.append(vp.name)
                if self.var_export_ass.get():
                    ap = out_dir / f"{title}.words.ass"
                    write_word_ass(ap, segments, mapping)
                    export_created.append(ap.name)
                if self.var_export_html.get():
                    sps = sorted({mapping.get(seg.get("speaker"), seg.get("speaker")) for seg in segments if seg.get("speaker")})
                    hp = out_dir / "word_player.html"
                    write_word_player_html(hp, sps)
                    export_created.append(hp.name)
            if self.var_export_lrc.get():
                lp = out_dir / f"{title}.lrc"
                write_lrc(lp, segments, mapping)
                export_created.append(lp.name)
            if self.var_export_ass_plain.get():
                pp = out_dir / f"{title}.plain.ass"
                write_ass_plain(pp, segments, mapping)
                export_created.append(pp.name)
        except:
            pass
        names_yaml = out_dir / "names.yaml"
        names_data = {"speaker_names": mapping}
        source_identity = build_source_identity(seg_data.get("source_path"))
        if source_identity is not None:
            names_data["source_identity"] = source_identity
        atomic_write_yaml(names_yaml, names_data)
        persistent_warning = None
        try:
            persistent_path = write_speaker_name_record(seg_data.get("source_path"), mapping)
            if persistent_path is None:
                persistent_warning = (
                    "The original source file is missing or unavailable. The current outputs were updated, "
                    "but the speaker names could not be saved persistently."
                )
        except Exception as e:
            persistent_warning = (
                "The current outputs were updated, but the persistent speaker-name record could not be saved:\n"
                f"{e}"
            )
        if self.var_rename_audio.get():
            try:
                self._rename_tree(out_dir, mapping)
            except Exception as e:
                messagebox.showwarning(
                    "Rename issue",
                    f"Some files could not be renamed:\n{e}",
                    parent=self.winfo_toplevel(),
                )
        if persistent_warning:
            messagebox.showwarning(
                "Speaker names not persisted",
                persistent_warning,
                parent=self.winfo_toplevel(),
            )
        messagebox.showinfo(
            "Done",
            "Updated files:\n" + txt_tmp.name + "\n" + srt_tmp.name + ("\n\nExports:\n" + "\n".join(export_created) if export_created else "") + "\n\nSaved mapping: " + names_yaml.name,
            parent=self.winfo_toplevel(),
        )
        if self._on_apply_complete is not None:
            self._on_apply_complete()


class NamingDialog(tk.Toplevel):
    """Compatibility window hosting the reusable Name Speakers workspace."""

    def __init__(self, master, speakers_json: Path, segments_json: Path):
        super().__init__(master)
        self.title("Name Speakers")
        self.geometry("1180x700")
        self.minsize(960, 600)
        self.resizable(True, True)
        self._closing = False
        self.workspace = NamingWorkspace(
            self,
            speakers_json,
            segments_json,
            on_apply_complete=self._close_from_workspace,
            on_discard=self._close_from_workspace,
        )
        self.workspace.pack(fill="both", expand=True)
        self.workspace.start()
        self.protocol("WM_DELETE_WINDOW", self.workspace.discard_changes)

    def _close_from_workspace(self):
        self.destroy()

    def destroy(self):
        if self._closing:
            return
        self._closing = True
        workspace = getattr(self, "workspace", None)
        if workspace is not None:
            workspace.shutdown()
        try:
            super().destroy()
        except tk.TclError:
            pass


class ReviewNamePage(ttk.Frame):
    """Persistent host for at most one embedded Name Speakers workspace."""

    def __init__(
        self,
        master,
        *,
        open_latest_callback,
        back_to_transcribe_callback,
        apply_complete_callback=None,
    ):
        super().__init__(master)
        self._open_latest_callback = open_latest_callback
        self._back_to_transcribe_callback = back_to_transcribe_callback
        self._apply_complete_callback = apply_complete_callback
        self.workspace = None
        self.current_result_paths = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.empty_state = ttk.Frame(self, padding=16)
        self.empty_state.grid(row=0, column=0, sticky="nsew")
        self.empty_state.columnconfigure(0, weight=1)
        self.empty_state.rowconfigure(2, weight=1)
        ttk.Label(
            self.empty_state,
            text="Review & Name",
            font=("Segoe UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        empty_card = ttk.LabelFrame(self.empty_state, text="Completed Results", padding=16)
        empty_card.grid(row=1, column=0, sticky="ew")
        empty_card.columnconfigure(0, weight=1)
        ttk.Label(
            empty_card,
            text=(
                "No result is loaded. Open the latest completed transcription to review "
                "speaker assignments, playback, and transcript exports."
            ),
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        tb.Button(
            empty_card,
            text="Open latest result",
            command=self._open_latest_callback,
            bootstyle="primary-outline",
            padding=(16, 6),
        ).grid(row=1, column=0, sticky="w", pady=(12, 0))

    @staticmethod
    def _validated_result_paths(speakers_json, segments_json):
        speakers_path = Path(speakers_json).resolve()
        segments_path = Path(segments_json).resolve()
        if not speakers_path.is_file() or not segments_path.is_file():
            raise FileNotFoundError("Both speakers.json and segments.json are required.")
        return speakers_path, segments_path

    @staticmethod
    def _result_key(paths):
        return tuple(os.path.normcase(str(path)) for path in paths)

    def load_result(self, speakers_json, segments_json, *, confirm_replacement=True):
        try:
            result_paths = self._validated_result_paths(speakers_json, segments_json)
        except Exception as exc:
            messagebox.showerror(
                "Could not open result",
                f"The selected transcription result is unavailable:\n{exc}",
                parent=self.winfo_toplevel(),
            )
            return False

        if (
            self.workspace is not None
            and self.current_result_paths is not None
            and self._result_key(result_paths) == self._result_key(self.current_result_paths)
        ):
            self.on_activated()
            return True

        if self.workspace is not None and confirm_replacement:
            replace = messagebox.askyesno(
                "Replace review result",
                "Another result is already open. Replace it with the selected result?\n\n"
                "Any unapplied in-memory edits in the current review will be lost.",
                parent=self.winfo_toplevel(),
            )
            if not replace:
                return False

        if self.workspace is not None:
            self._unload_workspace()

        new_workspace = None
        try:
            new_workspace = NamingWorkspace(
                self,
                result_paths[0],
                result_paths[1],
                on_apply_complete=self._on_workspace_applied,
                on_discard=self._back_to_transcribe_callback,
                discard_label="Back to Transcribe",
            )
            new_workspace.grid(row=0, column=0, sticky="nsew")
            self.empty_state.grid_remove()
            self.workspace = new_workspace
            self.current_result_paths = result_paths
            new_workspace.start()
            return True
        except Exception as exc:
            if new_workspace is not None:
                new_workspace.shutdown()
                try:
                    new_workspace.destroy()
                except tk.TclError:
                    pass
            self.workspace = None
            self.current_result_paths = None
            self.empty_state.grid()
            messagebox.showerror(
                "Could not open result",
                f"The transcription result could not be loaded:\n{exc}",
                parent=self.winfo_toplevel(),
            )
            return False

    def _on_workspace_applied(self):
        if self.workspace is not None:
            self.workspace.refresh_available_subtitles()
        if self._apply_complete_callback is not None:
            self._apply_complete_callback()

    def _unload_workspace(self):
        workspace = self.workspace
        self.workspace = None
        self.current_result_paths = None
        if workspace is None:
            return
        workspace.shutdown()
        try:
            workspace.destroy()
        except tk.TclError:
            pass

    def on_activated(self):
        if self.workspace is not None:
            self.workspace.on_host_activated()

    def shutdown(self):
        self._unload_workspace()


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.master = master
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.proc = None
        self.cancel_requested = False
        self.queue = queue.Queue()
        self.input_files = []
        self.pending_review_result = None
        self._application_closing = False
        self.var_model = tk.StringVar(value=_DEFAULTS["model"])
        self.var_lang = tk.StringVar(value=_DEFAULTS["language"])
        self.var_output = tk.StringVar(value=_DEFAULTS["output_format"])
        self.var_diar = tk.BooleanVar(value=_DEFAULTS["diarize"])
        self.var_slice = tk.BooleanVar(value=_DEFAULTS["slice_audio"])
        self.var_slice_video = tk.BooleanVar(value=_DEFAULTS["slice_video"])
        self.var_fast_cut = tk.BooleanVar(value=_DEFAULTS["fast_cut_video"])
        self.var_onefolder = tk.BooleanVar(value=_DEFAULTS["one_folder"])
        self.var_tags = tk.BooleanVar(value=_DEFAULTS["txt_speaker_tags"])
        self.var_srt = tk.BooleanVar(value=_DEFAULTS["srt"])
        self.var_txt = tk.BooleanVar(value=_DEFAULTS["txt"])
        self.var_compute = tk.StringVar(value=_DEFAULTS["compute_type"])
        self.var_tf32 = tk.StringVar(value=_DEFAULTS["tf32"])
        self.var_pad = tk.DoubleVar(value=_DEFAULTS["padding_seconds"])
        self.var_hf = tk.StringVar(value=_DEFAULTS["hf_token"])
        self.var_player = tk.StringVar(value=_DEFAULTS["video_player_path"])
        self.var_workers = tk.IntVar(value=_DEFAULTS["parallel_workers"]) # <--- NEW
        self.var_speaker_mode = tk.StringVar(value=_SPEAKER_MODE_LABELS[_DEFAULTS["diarization_speaker_mode"]])
        self.var_min_speakers = tk.StringVar(value=str(_DEFAULTS["min_speakers"]))
        self.var_max_speakers = tk.StringVar(value=str(_DEFAULTS["max_speakers"]))
        self.var_ner_engine = tk.StringVar(value=_DEFAULTS["ner_engine"])
        self._build_ui()
        self._load_conf_to_ui()
        self._update_title_with_conf_path()
        self.master.protocol("WM_DELETE_WINDOW", self.close_application)
        self.after(120, self._poll_queue)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.review_page = ReviewNamePage(
            self.notebook,
            open_latest_callback=self.on_name_speakers,
            back_to_transcribe_callback=lambda: self.show_page("transcribe"),
            apply_complete_callback=lambda: self.show_page("review"),
        )
        self.pages = {
            "transcribe": ttk.Frame(self.notebook, padding=8),
            "review": self.review_page,
            "activity": ttk.Frame(self.notebook, padding=8),
            "settings": ttk.Frame(self.notebook, padding=8),
        }
        self.notebook.add(self.pages["transcribe"], text="Transcribe")
        self.notebook.add(self.pages["review"], text="Review & Name")
        self.notebook.add(self.pages["activity"], text="Activity")
        self.notebook.add(self.pages["settings"], text="Settings")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed, add="+")

        transcribe = self.pages["transcribe"]
        transcribe.columnconfigure(0, weight=1)
        transcribe.rowconfigure(4, weight=1)

        header = ttk.Frame(transcribe, padding=(8, 4, 8, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="AudioTranscript Studio", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Local transcription and speaker tools").grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.lbl_status = tb.Label(header, text="Ready", bootstyle="secondary")
        self.lbl_status.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        files = ttk.LabelFrame(transcribe, text="Files", padding=12)
        files.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        tb.Button(files, text="Select Files", command=self.select_input_files, bootstyle="primary-outline").grid(row=0, column=0, sticky="w")
        tb.Button(files, text="Open Output", command=self.open_output_folder, bootstyle="secondary-outline").grid(row=0, column=1, sticky="w", padx=(8, 0))
        tb.Button(files, text="Clear Output", command=self.on_clear_output, bootstyle="danger-outline").grid(row=0, column=2, sticky="w", padx=(8, 0))

        transcription_settings = ttk.LabelFrame(transcribe, text="Transcription Settings", padding=12)
        transcription_settings.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        transcription_settings.columnconfigure(7, weight=1)
        ttk.Label(transcription_settings, text="Model").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.cmb_model = ttk.Combobox(transcription_settings, textvariable=self.var_model, values=_MODEL_CHOICES, width=18, state="readonly")
        self.cmb_model.grid(row=0, column=1, sticky="w", padx=(0, 16))
        self.cmb_model.bind("<<ComboboxSelected>>", self._on_model_changed)
        ttk.Label(transcription_settings, text="Language").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(transcription_settings, textvariable=self.var_lang, width=10).grid(row=0, column=3, sticky="w", padx=(0, 16))
        ttk.Checkbutton(transcription_settings, text="Identify speakers", variable=self.var_diar).grid(row=0, column=4, sticky="w", padx=(0, 16))
        ttk.Label(transcription_settings, text="Output").grid(row=0, column=5, sticky="w", padx=(0, 6))
        output_options = ttk.Frame(transcription_settings)
        output_options.grid(row=0, column=6, sticky="w")
        ttk.Radiobutton(output_options, text="Both", variable=self.var_output, value="both").pack(side="left")
        ttk.Radiobutton(output_options, text="SRT", variable=self.var_output, value="srt").pack(side="left", padx=(8, 0))
        ttk.Radiobutton(output_options, text="TXT", variable=self.var_output, value="txt").pack(side="left", padx=(8, 0))
        ttk.Frame(transcription_settings).grid(row=0, column=7, sticky="ew")

        actions = ttk.Frame(transcribe, padding=(0, 2, 0, 10))
        actions.grid(row=3, column=0, sticky="ew")
        self.btn_start = tb.Button(actions, text="Start Transcription", command=self.on_run, bootstyle="success", padding=(20, 8))
        self.btn_start.pack(side="left")
        self.btn_cancel = tb.Button(actions, text="Cancel", command=self.on_stop, bootstyle="danger-outline", padding=(16, 8))
        self.btn_cancel.pack(side="left", padx=(8, 0))
        progress_area = ttk.Frame(actions)
        progress_area.pack(side="left", padx=(14, 0))
        self.lbl_progress = ttk.Label(progress_area, text="Ready — 0%")
        self.lbl_progress.pack(anchor="w")
        self.progress = tb.Progressbar(
            progress_area,
            mode="determinate",
            maximum=100,
            length=220,
            bootstyle="info-striped",
        )
        self.progress.pack(fill="x", pady=(2, 0))
        self._last_progress = 0
        self._set_runtime_state("Ready")

        settings_page = self.pages["settings"]
        settings_page.columnconfigure(0, weight=1)
        settings_page.rowconfigure(4, weight=1)
        ttk.Label(settings_page, text="Settings", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(4, 10)
        )

        self.advanced_content = ttk.LabelFrame(
            settings_page,
            text="Advanced Settings",
            padding=12,
        )
        self.advanced_content.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.advanced_content.columnconfigure(7, weight=1)
        ttk.Checkbutton(self.advanced_content, text="Slice audio", variable=self.var_slice).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(self.advanced_content, text="Slice video", variable=self.var_slice_video).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Checkbutton(self.advanced_content, text="Fast cut", variable=self.var_fast_cut).grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Checkbutton(self.advanced_content, text="Merge into one folder", variable=self.var_onefolder).grid(row=0, column=3, sticky="w", padx=(16, 0))
        ttk.Checkbutton(self.advanced_content, text="Speaker tags in TXT", variable=self.var_tags).grid(row=0, column=4, columnspan=3, sticky="w", padx=(16, 0))

        ttk.Label(self.advanced_content, text="Padding (s)").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(self.advanced_content, from_=0.0, to=3.0, increment=0.05, textvariable=self.var_pad, width=7).grid(row=1, column=1, sticky="w", padx=(6, 16), pady=(12, 0))
        ttk.Label(self.advanced_content, text="Workers").grid(row=1, column=2, sticky="w", pady=(12, 0))
        ttk.Spinbox(self.advanced_content, from_=1, to=16, textvariable=self.var_workers, width=5).grid(row=1, column=3, sticky="w", padx=(6, 16), pady=(12, 0))
        ttk.Label(self.advanced_content, text="Hugging Face token").grid(row=1, column=4, sticky="w", pady=(12, 0))
        ttk.Entry(self.advanced_content, textvariable=self.var_hf, width=30, show="*").grid(row=1, column=5, sticky="w", padx=(6, 8), pady=(12, 0))
        tb.Button(self.advanced_content, text="Check HF token", command=self.on_check_hf, bootstyle="secondary-outline").grid(row=1, column=6, sticky="w", pady=(12, 0))

        ttk.Label(self.advanced_content, text="Speaker count").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.cmb_speaker_mode = ttk.Combobox(
            self.advanced_content,
            textvariable=self.var_speaker_mode,
            values=tuple(_SPEAKER_MODE_LABELS.values()),
            width=14,
            state="readonly",
        )
        self.cmb_speaker_mode.grid(row=2, column=1, sticky="w", padx=(6, 16), pady=(10, 0))
        self.cmb_speaker_mode.bind("<<ComboboxSelected>>", self._update_speaker_count_controls)

        self.speaker_count_fields = ttk.Frame(self.advanced_content)
        self.speaker_count_fields.grid(row=2, column=2, columnspan=5, sticky="w", pady=(10, 0))
        self.speaker_exact_fields = ttk.Frame(self.speaker_count_fields)
        ttk.Label(self.speaker_exact_fields, text="Number").pack(side="left")
        ttk.Spinbox(self.speaker_exact_fields, from_=1, to=100, textvariable=self.var_min_speakers, width=5).pack(side="left", padx=(6, 0))
        self.speaker_range_fields = ttk.Frame(self.speaker_count_fields)
        ttk.Label(self.speaker_range_fields, text="Minimum").pack(side="left")
        ttk.Spinbox(self.speaker_range_fields, from_=1, to=100, textvariable=self.var_min_speakers, width=5).pack(side="left", padx=(6, 14))
        ttk.Label(self.speaker_range_fields, text="Maximum").pack(side="left")
        ttk.Spinbox(self.speaker_range_fields, from_=1, to=100, textvariable=self.var_max_speakers, width=5).pack(side="left", padx=(6, 0))
        self._update_speaker_count_controls()

        ttk.Label(self.advanced_content, text="Video player path").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(self.advanced_content, textvariable=self.var_player, width=48).grid(row=3, column=1, columnspan=5, sticky="w", padx=(6, 8), pady=(10, 0))
        tb.Button(self.advanced_content, text="Browse", command=self.browse_player, bootstyle="secondary-outline").grid(row=3, column=6, sticky="w", pady=(10, 0))
        ttk.Frame(self.advanced_content).grid(row=0, column=7, rowspan=4, sticky="ew")

        ner_settings = ttk.LabelFrame(settings_page, text="Name Detection", padding=12)
        ner_settings.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        ner_settings.columnconfigure(3, weight=1)
        ttk.Label(ner_settings, text="NER engine").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.cmb_ner_engine = ttk.Combobox(
            ner_settings,
            textvariable=self.var_ner_engine,
            values=_NER_CHOICES,
            state="readonly",
            width=16,
        )
        self.cmb_ner_engine.grid(row=0, column=1, sticky="w", padx=(0, 12))
        self.cmb_ner_engine.bind("<<ComboboxSelected>>", self._on_ner_engine_changed)
        self.lbl_ner_info = ttk.Label(ner_settings, text="")
        self.lbl_ner_info.grid(row=0, column=2, sticky="w")

        utilities = ttk.LabelFrame(settings_page, text="Utilities", padding=10)
        utilities.grid(row=3, column=0, sticky="ew", padx=8)
        ttk.Button(utilities, text="Save config", command=self.on_save).grid(row=0, column=0, sticky="w")
        ttk.Button(utilities, text="About", command=self.on_about).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.lbl_conf = ttk.Label(utilities, text="Configuration: conf.yaml", foreground="#666")
        self.lbl_conf.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        activity_page = self.pages["activity"]
        activity_page.columnconfigure(0, weight=1)
        activity_page.rowconfigure(1, weight=1)
        activity_toolbar = ttk.Frame(activity_page, padding=(0, 0, 0, 8))
        activity_toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(activity_toolbar, text="Activity", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(activity_toolbar, text="Clear Log", command=self.clear_log).pack(side="right")
        ttk.Button(activity_toolbar, text="Copy Log", command=self.copy_log).pack(side="right", padx=(0, 8))
        activity = ttk.LabelFrame(activity_page, text="Processing Log", padding=8)
        activity.grid(row=1, column=0, sticky="nsew")
        activity.rowconfigure(0, weight=1)
        activity.columnconfigure(0, weight=1)
        self.txt = tk.Text(activity, height=16, wrap="word")
        self.txt.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(activity, orient="vertical", command=self.txt.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=log_scrollbar.set)

        self.show_page("transcribe")

    def show_page(self, page_name: str):
        page = self.pages.get(page_name)
        if page is None:
            raise KeyError(f"Unknown workspace page: {page_name}")
        self.notebook.select(page)
        if page_name == "review":
            self.review_page.on_activated()

    def _on_notebook_tab_changed(self, _event=None):
        if self.notebook.select() == str(self.review_page):
            self.review_page.on_activated()

    def close_application(self):
        if self._application_closing:
            return
        self._application_closing = True
        try:
            self.review_page.shutdown()
        finally:
            try:
                self.master.destroy()
            except tk.TclError:
                pass

    def _current_ner_engine(self) -> str:
        engine = self.var_ner_engine.get().strip().lower()
        if engine not in _NER_CHOICES:
            engine = _DEFAULTS["ner_engine"]
            self.var_ner_engine.set(engine)
        _set_ner_settings(engine=engine)
        return engine

    def _update_ner_engine_info(self):
        engine = self._current_ner_engine()
        self.lbl_ner_info.configure(text=f"{engine} — {_ner_device_info(engine)}")

    def _on_ner_engine_changed(self, *_):
        self._update_ner_engine_info()

    def _update_speaker_count_controls(self, *_):
        self.speaker_exact_fields.pack_forget()
        self.speaker_range_fields.pack_forget()
        mode = _SPEAKER_MODE_KEYS.get(self.var_speaker_mode.get(), "auto")
        if mode == "exact":
            self.speaker_exact_fields.pack(side="left")
        elif mode == "range":
            self.speaker_range_fields.pack(side="left")

    def _set_runtime_state(self, status: str):
        styles = {
            "Ready": "secondary",
            "Running": "info",
            "Cancelling": "warning",
            "Cancelled": "secondary",
            "Complete": "success",
            "Failed": "danger",
        }
        self.lbl_status.configure(text=status, bootstyle=styles[status])
        if status == "Running":
            self.btn_start.configure(state="disabled")
            self.btn_cancel.configure(state="normal")
        elif status == "Cancelling":
            self.btn_start.configure(state="disabled")
            self.btn_cancel.configure(state="disabled")
            self._set_progress_display("Cancelling", self._last_progress)
        else:
            self.btn_start.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            if status == "Ready":
                self._reset_progress("Ready")
            elif status == "Complete":
                self._last_progress = 100
                self._set_progress_display("Complete", 100)
            elif status in ("Failed", "Cancelled"):
                self._set_progress_display(status, self._last_progress)

    def _set_progress_display(self, label: str, percent: int, file_index=None, file_total=None):
        if file_total and file_total > 1:
            text = f"{label} (file {file_index} of {file_total}) — {percent}%"
        else:
            text = f"{label} — {percent}%"
        self.lbl_progress.configure(text=text)
        self.progress.configure(value=percent)

    def _reset_progress(self, label: str = "Starting"):
        self._last_progress = 0
        self._set_progress_display(label, 0)

    def _handle_progress_line(self, message: str) -> bool:
        if not message.startswith(_PIPELINE_PROGRESS_PREFIX):
            return False
        try:
            payload = json.loads(message[len(_PIPELINE_PROGRESS_PREFIX):])
            if not isinstance(payload, dict):
                raise ValueError
            phase = payload["phase"]
            label = payload["label"]
            file_percent = payload["file_percent"]
            file_index = payload["file_index"]
            file_total = payload["file_total"]
            if not isinstance(phase, str) or not phase.strip():
                raise ValueError
            if not isinstance(label, str) or not label.strip():
                raise ValueError
            if isinstance(file_percent, bool) or not isinstance(file_percent, (int, float)):
                raise ValueError
            if isinstance(file_index, bool) or not isinstance(file_index, int):
                raise ValueError
            if isinstance(file_total, bool) or not isinstance(file_total, int):
                raise ValueError
            if not (0 <= file_percent <= 100 and file_total >= 1 and 1 <= file_index <= file_total):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

        if self.cancel_requested:
            return True
        overall = ((file_index - 1) + file_percent / 100) / file_total * 100
        percent = min(99, max(0, int(round(overall))))
        if percent < self._last_progress:
            return True
        self._last_progress = percent
        self._set_progress_display(label.strip(), percent, file_index, file_total)
        return True

    def log(self, s: str):
        self.txt.insert("end", s.rstrip() + "\n")
        self.txt.see("end")

    def _update_title_with_conf_path(self):
        self.master.title("AudioTranscript Studio")
        self.lbl_conf.configure(text="Configuration: conf.yaml")

    @staticmethod
    def _positive_speaker_count(value, label):
        text = str(value).strip()
        if not re.fullmatch(r"[0-9]+", text) or int(text) <= 0:
            raise ValueError(f"{label} must be a positive integer.")
        return int(text)

    def _speaker_count_config_values(self):
        mode = _SPEAKER_MODE_KEYS.get(self.var_speaker_mode.get(), "auto")
        if mode == "auto":
            try:
                minimum = self._positive_speaker_count(self.var_min_speakers.get(), "Minimum speaker count")
            except ValueError:
                minimum = _DEFAULTS["min_speakers"]
            try:
                maximum = self._positive_speaker_count(self.var_max_speakers.get(), "Maximum speaker count")
            except ValueError:
                maximum = _DEFAULTS["max_speakers"]
            return mode, minimum, maximum
        if mode == "exact":
            number = self._positive_speaker_count(self.var_min_speakers.get(), "Speaker count")
            return mode, number, number

        minimum = self._positive_speaker_count(self.var_min_speakers.get(), "Minimum speaker count")
        maximum = self._positive_speaker_count(self.var_max_speakers.get(), "Maximum speaker count")
        if minimum > maximum:
            raise ValueError("Minimum speaker count cannot be greater than maximum speaker count.")
        return mode, minimum, maximum

    def _validate_diarization_speaker_count(self):
        try:
            self._speaker_count_config_values()
            return True
        except ValueError as e:
            self._set_runtime_state("Ready")
            self.log(f"[validation] Invalid diarization speaker count: {e}")
            messagebox.showwarning("Invalid speaker count", str(e))
            return False

    def _load_conf_to_ui(self):
        cfg = read_yaml(conf_path())
        if not cfg:
            self._update_ner_engine_info()
            return
        self.var_lang.set(cfg.get("language", self.var_lang.get()))
        self.var_model.set(cfg.get("model", self.var_model.get()))
        self.var_diar.set(bool(cfg.get("diarize", self.var_diar.get())))
        self.var_slice.set(bool(cfg.get("slice_audio", self.var_slice.get())))
        self.var_slice_video.set(bool(cfg.get("slice_video", self.var_slice_video.get())))
        self.var_fast_cut.set(bool(cfg.get("fast_cut_video", self.var_fast_cut.get())))
        self.var_onefolder.set(bool(cfg.get("one_folder", self.var_onefolder.get())))
        self.var_tags.set(bool(cfg.get("txt_speaker_tags", self.var_tags.get())))
        self.var_output.set(cfg.get("output_format", self.var_output.get()))
        self.var_srt.set(bool(cfg.get("srt", self.var_srt.get())))
        self.var_txt.set(bool(cfg.get("txt", self.var_txt.get())))
        self.var_compute.set("float16")
        self.var_tf32.set("off")
        self.var_pad.set(float(cfg.get("padding_seconds", self.var_pad.get())))
        self.var_hf.set(cfg.get("hf_token", self.var_hf.get()))
        self.var_player.set(cfg.get("video_player_path", self.var_player.get()))
        self.var_workers.set(int(cfg.get("parallel_workers", self.var_workers.get()))) # <--- LOAD
        mode = str(cfg.get("diarization_speaker_mode", _DEFAULTS["diarization_speaker_mode"])).strip().lower()
        if mode not in _SPEAKER_MODE_LABELS:
            mode = "auto"
        self.var_speaker_mode.set(_SPEAKER_MODE_LABELS[mode])
        self.var_min_speakers.set(str(cfg.get("min_speakers", _DEFAULTS["min_speakers"])))
        self.var_max_speakers.set(str(cfg.get("max_speakers", _DEFAULTS["max_speakers"])))
        engine = str(cfg.get("ner_engine", _DEFAULTS["ner_engine"])).strip().lower()
        self.var_ner_engine.set(engine if engine in _NER_CHOICES else _DEFAULTS["ner_engine"])
        self._update_ner_engine_info()
        self._update_speaker_count_controls()

    def _collect_ui_to_conf(self) -> dict:
        self.var_compute.set("float16")
        self.var_tf32.set("off")
        speaker_mode, min_speakers, max_speakers = self._speaker_count_config_values()
        ofmt = self.var_output.get()
        srt = self.var_srt.get() if ofmt in ("both","srt") else False
        txt = self.var_txt.get() if ofmt in ("both","txt") else False
        if ofmt == "both": srt, txt = True, True
        return {
            "language": self.var_lang.get().strip() or "en",
            "model": self.var_model.get().strip(),
            "diarize": bool(self.var_diar.get()),
            "slice_audio": bool(self.var_slice.get()),
            "slice_video": bool(self.var_slice_video.get()),
            "fast_cut_video": bool(self.var_fast_cut.get()),
            "video_player_path": self.var_player.get().strip(),
            "parallel_workers": int(self.var_workers.get()), # <--- SAVE
            "merge_all_segments_into_one_folder": bool(self.var_onefolder.get()),
            "txt_speaker_tags": bool(self.var_tags.get()),
            "one_folder": bool(self.var_onefolder.get()),
            "output_format": ofmt, "srt": bool(srt), "txt": bool(txt),
            "compute_type": "float16",
            "tf32": "off",
            "padding_seconds": float(self.var_pad.get()),
            "hf_token": self.var_hf.get().strip(),
            "diarization_speaker_mode": speaker_mode,
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
            "ner_engine": self._current_ner_engine(),
        }

    def on_save(self):
        try:
            cfg = merge_gui_conf(read_yaml(conf_path()), self._collect_ui_to_conf())
            atomic_write_yaml(conf_path(), cfg)
            self.log(f"Saved {conf_path()}")
        except ValueError as e:
            messagebox.showwarning("Invalid speaker count", str(e))
            self.log(f"[validation] Invalid diarization speaker count: {e}")
        except Exception as e:
            messagebox.showerror("Save failed", f"{e}")
            self.log(f"ERROR saving conf: {e}")
        self._update_title_with_conf_path()

    def _on_model_changed(self, *_):
        model = self.var_model.get().strip()
        self.log(f"[cfg] Model set to: {model}")
        try:
            cfg = merge_gui_conf(read_yaml(conf_path()), {"model": model})
            atomic_write_yaml(conf_path(), cfg)
        except Exception:
            pass

    def browse_player(self):
        fn = filedialog.askopenfilename(title="Select Video Player Executable", filetypes=[("Executables", "*.exe"), ("All Files", "*.*")])
        if fn:
            self.var_player.set(fn)

    def select_input_files(self):
        files = filedialog.askopenfilenames(title="Select Audio/Video Files", filetypes=[("Media Files", "*.mp4 *.mkv *.mov *.avi *.mp3 *.wav *.m4a *.flac"), ("All Files", "*.*")])
        if files:
            self.input_files = list(files)
            self.log(f"[selection] Selected {len(files)} file(s):")
            for f in files:
                self.log(f" - {Path(f).name}")
        else:
            self.input_files = []
            self.log("[selection] Cleared selection (will use data/input folder).")

    def on_stop(self):
        if self.proc and self.proc.poll() is None:
            self.cancel_requested = True
            self._set_runtime_state("Cancelling")
            self.log("[stop] Terminating process...")
            # On Windows, terminate() is usually enough, but sometimes we need stronger measures
            try:
                self.proc.terminate() # Try soft kill first
            except:
                pass
            
            # Since subprocess spawned children, we might want to kill the whole tree if possible
            # Standard subprocess.kill() just kills the python wrapper.
            # We can use taskkill to be sure if simple terminate fails.
            if self.proc.poll() is None:
                try:
                    subprocess.call(['taskkill', '/F', '/T', '/PID', str(self.proc.pid)])
                except:
                    self.proc.kill()
            self.log("[stop] Process stopped.")
        else:
            self.log("[stop] No active process to stop.")

    def _effective_input_files(self):
        return discover_sources(self.input_files or None)

    def _validate_slice_video_inputs(self):
        if not self.var_slice_video.get():
            return True

        effective_inputs = self._effective_input_files()
        if not effective_inputs:
            return True

        video_inputs = [p for p in effective_inputs if p.suffix.lower() in _PIPELINE_VIDEO_EXTS]
        audio_inputs = [p for p in effective_inputs if p.suffix.lower() in _PIPELINE_AUDIO_EXTS]

        if audio_inputs and len(audio_inputs) == len(effective_inputs):
            self._set_runtime_state("Ready")
            self.log("[validation] Slice video requires a video input; all effective inputs are audio-only.")
            messagebox.showwarning(
                "Slice video requires video",
                "Slice video requires a video file. The current input contains audio only. "
                "Select an MP4 or other supported video, or turn off Slice video.",
            )
            return False

        if audio_inputs and video_inputs:
            proceed = messagebox.askyesno(
                "Mixed audio and video inputs",
                "Some inputs contain audio only and cannot produce video clips. Video clips will be "
                "created only for the supported video files.\n\nContinue processing all files?",
            )
            if not proceed:
                self._set_runtime_state("Ready")
                self.log("[validation] Run cancelled; audio-only inputs cannot produce video clips.")
                return False
            self.log("[validation] Continuing with mixed inputs; audio-only files will not produce video clips.")

        return True

    def on_run(self):
        self.cancel_requested = False
        if not self._validate_diarization_speaker_count():
            return
        self.on_save()
        if not self._validate_slice_video_inputs():
            return
        self._reset_progress("Starting")
        model = self.var_model.get().strip()
        workers = self.var_workers.get()
        try:
            import torch
            dev = "cuda:0" if torch.cuda.is_available() else "cpu"
            gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else ""
            self.log(f"[run] Launching with model '{model}', compute={self.var_compute.get()}, tf32={self.var_tf32.get()} — device {dev} {('('+gpu+')') if gpu else ''}".strip())
        except Exception:
            self.log(f"[run] Launching with model '{model}'")
        py = sys.executable or "python"
        
        cmd = [py, str(program_root() / "split_audio.py")]
        
        # Pass worker count explicitly
        cmd.append("--workers")
        cmd.append(str(workers))
        
        if self.input_files:
            cmd.append("--inputs")
            cmd.extend(self.input_files)
            
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(program_root()),
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            self.proc = None
            self._set_runtime_state("Failed")
            messagebox.showerror("Run failed", "split_audio.py not found in program folder.")
            return
        except OSError as e:
            self.proc = None
            self._set_runtime_state("Failed")
            messagebox.showerror("Run failed", str(e))
            return
        self._set_runtime_state("Running")
        threading.Thread(target=self._reader, daemon=True).start()

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str.strip())
        return p if p.is_absolute() else (program_root() / p)

    def _reader(self):
        proc = self.proc
        returncode = None
        reader_failed = False
        try:
            if proc and proc.stdout:
                for line in proc.stdout:
                    msg = line.rstrip()
                    self.queue.put(("log", msg))
                    if msg.startswith("[speakers-json]"):
                        try:
                            path_str = msg.split("]", 1)[1].strip()
                            spk_path = self._resolve_path(path_str)
                            seg_path = spk_path.parent / "segments.json"
                            if spk_path.exists() and seg_path.exists():
                                self.queue.put(("speakers", spk_path, seg_path))
                        except Exception:
                            pass
            elif proc is None:
                raise RuntimeError("No active process is available to read.")
        except Exception as e:
            reader_failed = True
            self.queue.put(("log", f"[reader error] Unexpected process output error: {e}"))
        finally:
            if proc and proc.stdout:
                try:
                    proc.stdout.close()
                except Exception as e:
                    reader_failed = True
                    self.queue.put(("log", f"[reader error] Could not close process output: {e}"))
            if proc:
                try:
                    returncode = proc.wait()
                except Exception as e:
                    reader_failed = True
                    self.queue.put(("log", f"[reader error] Could not wait for process completion: {e}"))
                    try:
                        returncode = proc.poll()
                    except Exception:
                        returncode = None
            if returncode is None or (reader_failed and returncode == 0):
                returncode = 1
            self.queue.put(("log", "[process finished]"))
            self.queue.put(("process_finished", returncode))

    def _poll_queue(self):
        try:
            while True:
                event = self.queue.get_nowait()
                if isinstance(event, tuple):
                    kind = event[0]
                    if kind == "log":
                        if not self._handle_progress_line(event[1]):
                            self.log(event[1])
                    elif kind == "speakers":
                        try:
                            self.pending_review_result = ReviewNamePage._validated_result_paths(
                                event[1],
                                event[2],
                            )
                        except Exception:
                            pass
                    elif kind == "process_finished":
                        self.proc = None
                        if self.cancel_requested:
                            self._set_runtime_state("Cancelled")
                        elif event[1] == 0:
                            self._set_runtime_state("Complete")
                        else:
                            self._set_runtime_state("Failed")
                else:
                    self.log(str(event).rstrip())
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def on_check_hf(self):
        self.on_save()
        script = program_root() / "verify_hf_env.py"
        if not script.exists():
            self.log(f"[check] {script} not found.")
            messagebox.showwarning("Missing script", f"verify_hf_env.py not found in {program_root()}")
            return
        self.log(f"[check] Running: {script.name}")
        env = os.environ.copy()
        tok = self.var_hf.get().strip()
        if tok:
            env["HF_TOKEN"] = tok
            env["HUGGINGFACE_HUB_TOKEN"] = tok
        py = sys.executable or "python"
        try:
            out = subprocess.check_output([py, str(script)], cwd=str(program_root()), env=env, text=True, stderr=subprocess.STDOUT)
            for line in out.splitlines():
                self.log(line)
        except subprocess.CalledProcessError as e:
            self.log(e.output or str(e))
            messagebox.showerror("Check failed", e.output or str(e))

    def open_input_folder(self):
        p = program_root() / "data" / "input"
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))

    def open_output_folder(self):
        p = output_root()
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))

    def on_clear_output(self):
        out = output_root()
        out.mkdir(parents=True, exist_ok=True)
        if not messagebox.askyesno("Confirm delete", f"Delete ALL contents of:\n{out}\n\nThis cannot be undone."):
            return
        errors = []
        def make_writable_then_retry(func, path, exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception as e:
                errors.append(f"{path}: {e}")
        for child in out.iterdir():
            if child.name == ".gitkeep":
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child, onerror=make_writable_then_retry)
                else:
                    child.chmod(stat.S_IWRITE)
                    child.unlink(missing_ok=True)
            except Exception as e:
                errors.append(f"{child}: {e}")
        if errors:
            self.log("[clear] Completed with errors (locked files?):")
            for e in errors[:10]:
                self.log("  " + e)
            if len(errors) > 10:
                self.log(f"  ... and {len(errors)-10} more")
            messagebox.showwarning("Output not fully cleared", "Some files could not be removed. Close any processes using the folder and try again.")
        else:
            self.log(f"[clear] Emptied folder: {out}")

    def copy_log(self):
        text = self.txt.get("1.0", "end-1c")
        self.master.clipboard_clear()
        self.master.clipboard_append(text)
        messagebox.showinfo("Copied", "Log copied to clipboard.")

    def clear_log(self):
        self.txt.delete("1.0", "end")

    def on_about(self):
        info = gather_about_info()
        self.log("--- About ---")
        for line in info.splitlines():
            self.log(line)
        self.log("-------------")
        win = tk.Toplevel(self.master)
        win.title("About - AudioTranscript Studio")
        win.geometry("820x460")
        frm = ttk.Frame(win, padding=8)
        frm.pack(fill="both", expand=True)
        text = tk.Text(frm, wrap="word")
        yscroll = ttk.Scrollbar(frm, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=yscroll.set)
        text.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        text.insert("1.0", info)
        text.configure(state="disabled")
        btns = ttk.Frame(win, padding=(8,0,8,8))
        btns.pack(fill="x")
        def copy_all():
            win.clipboard_clear()
            win.clipboard_append(info)
        ttk.Button(btns, text="Copy", command=copy_all).pack(side="right")
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right", padx=6)

    def _latest_review_result(self):
        out = output_root()
        if not out.exists():
            messagebox.showinfo("No output", f"No output folder {out}")
            return None
        candidates = []
        for child in out.iterdir():
            if child.is_dir() and (child / "speakers.json").exists() and (child / "segments.json").exists():
                candidates.append(
                    (
                        child.stat().st_mtime,
                        (child / "speakers.json").resolve(),
                        (child / "segments.json").resolve(),
                    )
                )
        if self.pending_review_result is not None:
            try:
                pending = ReviewNamePage._validated_result_paths(*self.pending_review_result)
                pending_key = ReviewNamePage._result_key(pending)
                if not any(ReviewNamePage._result_key(item[1:]) == pending_key for item in candidates):
                    candidates.append((pending[0].parent.stat().st_mtime, *pending))
            except Exception:
                pass
        if not candidates:
            messagebox.showinfo("Nothing to name", "No speakers.json found in output folders.")
            return None
        latest = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        return latest[1], latest[2]

    def on_name_speakers(self):
        latest = self._latest_review_result()
        if latest is None:
            return
        engine = self._current_ner_engine()
        self.log(f"[ner] Engine set to: {engine}  |  {_ner_device_info(engine)}")
        if not self.review_page.load_result(*latest):
            return
        if (
            self.pending_review_result is not None
            and ReviewNamePage._result_key(latest)
            == ReviewNamePage._result_key(self.pending_review_result)
        ):
            self.pending_review_result = None
        self.show_page("review")

def main():
    root = tb.Window(themename="litera")
    app = App(root)
    root.geometry("1140x620")
    root.title("AudioTranscript Studio")
    root.mainloop()

if __name__ == "__main__":
    main()
