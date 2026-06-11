# split_audio_gui.py — v1.11.0 (Stop Button + Worker Control)
import os, sys, stat, json, yaml, queue, shutil, threading, subprocess, tkinter as tk, hashlib, datetime, re, signal
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from collections import Counter

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
        f.write("ScriptType: v4.00+\nCollisions: Normal\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\nWrapStyle: 2\n")
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
        f.write("ScriptType: v4.00+\nCollisions: Normal\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\nWrapStyle: 2\n")
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
    "output_format": "both","srt": True,"txt": True,"compute_type": "float16","tf32": "on",
    "padding_seconds": 0.25,"hf_token": "",
}
_MODEL_CHOICES = ["tiny","base","small","medium","large-v2","large-v3","large-v3-turbo","distil-large-v3"]
_COMPUTE_CHOICES = ["float16","float32"]
_TF32_CHOICES = ["on","off"]

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
        self.tree = ttk.Treeview(frame, columns=("time","text"), show="headings", selectmode="browse", height=12)
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
        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8,0))
        self.btn_open = ttk.Button(btns, text="Open at time", command=self._on_open)
        self.btn_open.pack(side="right", padx=(0,8))
        ttk.Button(btns, text="Cancel", command=self._on_cancel).pack(side="right")
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

    def _on_open(self):
        sel = self.tree.selection()
        if sel:
            self.result = self.hits[int(sel[0])]
            self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()

class NamingDialog(tk.Toplevel):
    def __init__(self, master, speakers_json: Path, segments_json: Path):
        super().__init__(master)
        self.title("Name Speakers")
        self.geometry("980x600")
        self.resizable(True, True)
        self.speakers_json = speakers_json
        self.segments_json = segments_json
        spk_data = json.loads(speakers_json.read_text(encoding="utf-8"))
        seg_data = json.loads(segments_json.read_text(encoding="utf-8"))
        self.title_name = spk_data.get("title") or speakers_json.parent.name
        self.speakers = list(spk_data.get("speakers") or [])
        self.saved_names = dict(spk_data.get("names") or spk_data.get("name_map") or {})
        self.segments = seg_data.get("segments") or []
        global_counts = Counter(_extract_candidates_from_text(" ".join(str(s.get("text","")) for s in self.segments)))
        # Do not use the file title as a strong name source. Titles are usually topics, not speakers.
        title_counts = Counter()
        addressed_counts = _addressed_name_counts_for_speakers(self.segments)
        per_spk_counts = {}
        for spk in self.speakers:
            c = Counter(_extract_candidates_from_text(" ".join(str(s.get("text","")) for s in self.segments if (s.get("speaker") == spk))))
            c.update(addressed_counts.get(spk, Counter()))
            per_spk_counts[spk] = c
        main = ttk.Frame(self, padding=8)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text=f"File: {self.title_name}").pack(anchor="w")
        ttk.Label(main, text="Pick a suggested name or type a custom one for each speaker:").pack(anchor="w", pady=(2,8))
        paned = ttk.PanedWindow(main, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0,0,8,0))
        right = ttk.Frame(paned, padding=0)
        paned.add(left, weight=1)
        paned.add(right, weight=2)
        self.inputs = {}
        self.selected_speaker = tk.StringVar(value=self.speakers[0] if self.speakers else "")

        grid = ttk.Frame(left)
        grid.pack(fill="x", expand=False, pady=(0,6))

        for r, spk in enumerate(self.speakers):
            rb = ttk.Radiobutton(grid, variable=self.selected_speaker, value=spk)
            rb.grid(row=r, column=0, sticky="w", pady=3)

            ttk.Label(grid, text=spk, width=16).grid(row=r, column=1, sticky="e", padx=(0,6), pady=3)

            # Keep this as an editable combobox so the user can still type any name manually.
            # The dropdown values are hints only. We do not auto-fill weak guesses anymore.
            suggestions = _best_names_list(per_spk_counts.get(spk, Counter()), global_counts, title_counts, limit=8)
            saved = self.saved_names.get(spk)

            if saved and saved not in suggestions:
                suggestions = [saved] + suggestions

            cb = ttk.Combobox(grid, values=suggestions, width=40, state="normal")
            cb.grid(row=r, column=2, sticky="we", pady=3)

            cb.bind("<FocusIn>", lambda e, spk=spk: self.selected_speaker.set(spk))
            cb.bind("<Button-1>", lambda e, spk=spk: self.selected_speaker.set(spk))

            if saved:
                cb.set(saved)

            self.inputs[spk] = cb

        grid.columnconfigure(2, weight=1)
        # Candidate name pool: names/entities found in the transcript.
        # These are NOT automatically assigned. The user assigns them manually.
        pool_frame = ttk.LabelFrame(left, text="Candidate name pool")
        pool_frame.pack(fill="both", expand=False, pady=(8,0))

        pool_names = _best_names_list(
            global_counts,
            *[per_spk_counts.get(spk, Counter()) for spk in self.speakers],
            title_counts,
            limit=40
        )

        pool_inner = ttk.Frame(pool_frame)
        pool_inner.pack(fill="both", expand=True, padx=6, pady=6)

        self.name_pool = tk.Listbox(pool_inner, height=8, exportselection=False)
        pool_scroll = ttk.Scrollbar(pool_inner, orient="vertical", command=self.name_pool.yview)
        self.name_pool.configure(yscrollcommand=pool_scroll.set)

        self.name_pool.pack(side="left", fill="both", expand=True)
        pool_scroll.pack(side="right", fill="y")

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
        pool_btns.pack(fill="x", padx=6, pady=(0,6))
        ttk.Button(pool_btns, text="Assign to selected speaker", command=assign_selected_name).pack(side="left")
        ttk.Button(pool_btns, text="Clear selected speaker", command=clear_selected_speaker).pack(side="left", padx=(6,0))
        ttk.Button(pool_btns, text="Add typed name to pool", command=add_typed_name_to_pool).pack(side="left", padx=(6,0))

        opts = ttk.Frame(left)
        opts.pack(fill="x", pady=(8,0))
        self.var_overwrite = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Overwrite existing SRT/TXT (recommended)", variable=self.var_overwrite).pack(anchor="w")
        self.var_rename_audio = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Rename folders and .wav files with names", variable=self.var_rename_audio).pack(anchor="w")
        self.var_autofill2 = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Prefill first two speakers by earliest appearance", variable=self.var_autofill2).pack(anchor="w")
        try:
            _cfg = read_yaml(conf_path())
        except:
            _cfg = {}
        self.var_export_vtt  = tk.BooleanVar(value=bool(_cfg.get("export_word_vtt", False)))
        self.var_export_ass  = tk.BooleanVar(value=bool(_cfg.get("export_word_ass", False)))
        self.var_export_html = tk.BooleanVar(value=bool(_cfg.get("export_word_html", False)))
        self.var_export_lrc  = tk.BooleanVar(value=bool(_cfg.get("export_word_lrc", False)))
        self.var_export_ass_plain = tk.BooleanVar(value=bool(_cfg.get("export_ass_plain", False)))
        ttk.Label(opts, text="Word-level exports:").pack(anchor="w", pady=(6,0))
        ttk.Checkbutton(opts, text="Word-level VTT", variable=self.var_export_vtt).pack(anchor="w")
        ttk.Checkbutton(opts, text="Word-level ASS (VLC)", variable=self.var_export_ass).pack(anchor="w")
        ttk.Checkbutton(opts, text="Write HTML word player", variable=self.var_export_html).pack(anchor="w")
        ttk.Checkbutton(opts, text="Word-level LRC (CapCut)", variable=self.var_export_lrc).pack(anchor="w")
        ttk.Checkbutton(opts, text="ASS (plain, no karaoke)", variable=self.var_export_ass_plain).pack(anchor="w")
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(8,0))
        ttk.Button(btns, text="Apply", command=self.on_apply).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        viewer = ttk.Frame(right)
        viewer.pack(fill="both", expand=True)
        toolbar = ttk.Frame(viewer)
        toolbar.pack(fill="x", pady=(0,4))
        ttk.Label(toolbar, text="Find:").pack(side="left")
        self.find_var = tk.StringVar(value="SPEAKER_00")
        find_entry = ttk.Entry(toolbar, textvariable=self.find_var, width=24)
        find_entry.pack(side="left", padx=(4,6))
        self.find_speaker_var = tk.StringVar(value=self.speakers[0] if self.speakers else "")
        find_spk = ttk.Combobox(toolbar, textvariable=self.find_speaker_var, values=self.speakers, width=14, state="readonly")
        find_spk.pack(side="left", padx=(0,6))
        ttk.Button(toolbar, text="Find next", command=self.find_next).pack(side="left")
        ttk.Button(toolbar, text="Find speaker tag", command=self.find_speaker_tag).pack(side="left", padx=(6,0))
        ttk.Button(toolbar, text="Open video at hit", command=self.open_video_at_query).pack(side="left", padx=(6,0))
        ttk.Button(toolbar, text="Open externally", command=self.open_txt_external).pack(side="right")
        self.text = tk.Text(viewer, wrap="word")
        yscroll = ttk.Scrollbar(viewer, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=yscroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.txt_path, txt_content = self._load_transcript()
        self.text.insert("1.0", txt_content)
        self.text.edit_reset()
        self._reset_highlight()
        initial = "SPEAKER_00"
        if initial not in txt_content and self.speakers:
            initial = self.speakers[0]
        if "SPEAKER_" not in initial:
            initial = self.speakers[0] if self.speakers else "SPEAKER_00"
        self.find_var.set(initial)
        self._highlight_query(initial)
        if self.var_autofill2.get() and not (getattr(self, "saved_names", None) and len(self.saved_names)>0):
            self._prefill_first_two(per_spk_counts, global_counts, title_counts)

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

    def find_next(self):
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
        tag = self.find_speaker_var.get().strip()
        if not tag:
            return
        self.find_var.set(tag)
        self._highlight_query(tag)

    def open_txt_external(self):
        if self.txt_path and self.txt_path.exists():
            os.startfile(str(self.txt_path))
        else:
            messagebox.showinfo("No .txt file", "Transcript .txt not found on disk; showing generated preview only.")

    def open_video_at_query(self):
        query = (self.find_var.get() if hasattr(self, "find_var") else "").strip()
        if not query:
            messagebox.showinfo("Jump by SRT", "Type something in the Find box first, then try again.")
            return
        out_dir = self.speakers_json.parent
        srt_path = out_dir / f"{self.title_name}.srt"
        if not srt_path.exists():
            messagebox.showinfo("Jump by SRT", f"SRT not found:\n{srt_path.name}\n\nRun transcription first or enable SRT output.")
            return
        try:
            segs = parse_srt_segments(srt_path)
        except Exception as e:
            messagebox.showerror("Jump by SRT", f"Could not read SRT:\n{e}")
            return
        hits = find_segments_matching_query(segs, query)
        if not hits:
            messagebox.showinfo("Jump by SRT", f"No SRT lines matched:\n“{query}”")
            return
        if len(hits) > 1:
            dlg = _SrtHitsDialog(self, hits)
            self.wait_window(dlg)
            if not dlg.result:
                return
            chosen = dlg.result
        else:
            chosen = hits[0]
        try:
            start = max(0.0, float(chosen["start"]) - 0.8)
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
            messagebox.showerror("Jump by SRT", f"Failed to open player:\n{e}")
            return

    def _prefill_first_two(self, per_spk_counts: dict, global_counts: list, title_counts: list):
        order = []
        seen = set()
        for seg in self.segments:
            sp = seg.get("speaker")
            if not sp or sp in seen: continue
            seen.add(sp); order.append(sp)
            if len(order) >= 2: break
        for sp in order:
            cb = self.inputs.get(sp)
            if not cb: continue
            candidates = _best_names_list(per_spk_counts.get(sp, Counter()), global_counts, title_counts, limit=8)
            if candidates:
                cb.set(candidates[0])

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

    def on_apply(self):
        mapping = {spk: self.inputs[spk].get().strip() for spk in self.speakers}
        mapping = {k: v for k, v in mapping.items() if v}
        try:
            _spk = json.loads(self.speakers_json.read_text(encoding='utf-8'))
            _spk['names'] = mapping
            self.speakers_json.write_text(json.dumps(_spk, ensure_ascii=False, indent=2), encoding='utf-8')
        except:
            pass
        if not mapping:
            messagebox.showwarning("Nothing to apply", "Please enter at least one name.")
            return
        seg_data = json.loads(self.segments_json.read_text(encoding="utf-8"))
        segments = seg_data.get("segments") or []
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
        atomic_write_yaml(names_yaml, {"speaker_names": mapping})
        if self.var_rename_audio.get():
            try:
                self._rename_tree(out_dir, mapping)
            except Exception as e:
                messagebox.showwarning("Rename issue", f"Some files could not be renamed:\n{e}")
        messagebox.showinfo("Done", "Updated files:\n" + txt_tmp.name + "\n" + srt_tmp.name + ("\n\nExports:\n" + "\n".join(export_created) if export_created else "") + "\n\nSaved mapping: " + names_yaml.name)
        self.destroy()

class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.master = master
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.proc = None
        self.queue = queue.Queue()
        self.input_files = []
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
        self._build_ui()
        self._load_conf_to_ui()
        self._update_title_with_conf_path()
        self.after(120, self._poll_queue)

    def _build_ui(self):
        r = 0
        ttk.Button(self, text="Run", command=self.on_run).grid(row=r, column=0, sticky="w")
        ttk.Button(self, text="Stop", command=self.on_stop).grid(row=r, column=1, sticky="w", padx=2) # <--- STOP BUTTON
        ttk.Button(self, text="Save config", command=self.on_save).grid(row=r, column=2, sticky="w", padx=6)
        ttk.Button(self, text="Select Files...", command=self.select_input_files).grid(row=r, column=3, sticky="w", padx=6)
        ttk.Button(self, text="Open output folder", command=self.open_output_folder).grid(row=r, column=4, sticky="w", padx=6)
        ttk.Button(self, text="Clear output folder", command=self.on_clear_output).grid(row=r, column=5, sticky="w", padx=6)
        ttk.Button(self, text="Check HF token", command=self.on_check_hf).grid(row=r, column=6, sticky="w", padx=6)
        ttk.Button(self, text="Copy log", command=self.copy_log).grid(row=r, column=7, sticky="w", padx=6)
        ttk.Button(self, text="Clear log", command=self.clear_log).grid(row=r, column=8, sticky="w", padx=6)
        ttk.Button(self, text="About", command=self.on_about).grid(row=r, column=9, sticky="w", padx=6)
        ttk.Button(self, text="Name speakers…", command=self.on_name_speakers).grid(row=r, column=10, sticky="w", padx=6)
        r += 1
        ttk.Label(self, text="Model:").grid(row=r, column=0, sticky="e")
        self.cmb_model = ttk.Combobox(self, textvariable=self.var_model, values=_MODEL_CHOICES, width=16, state="readonly")
        self.cmb_model.grid(row=r, column=1, sticky="w")
        self.cmb_model.bind("<<ComboboxSelected>>", self._on_model_changed)
        ttk.Label(self, text="Language:").grid(row=r, column=2, sticky="e")
        ttk.Entry(self, textvariable=self.var_lang, width=8).grid(row=r, column=3, sticky="w", padx=6)
        ttk.Label(self, text="Output:").grid(row=r, column=4, sticky="e")
        ttk.Radiobutton(self, text="Both", variable=self.var_output, value="both").grid(row=r, column=5, sticky="w")
        ttk.Radiobutton(self, text="SRT", variable=self.var_output, value="srt").grid(row=r, column=6, sticky="w")
        ttk.Radiobutton(self, text="TXT", variable=self.var_output, value="txt").grid(row=r, column=7, sticky="w")
        r += 1
        ttk.Checkbutton(self, text="Diarize", variable=self.var_diar).grid(row=r, column=0, sticky="w")
        ttk.Checkbutton(self, text="Slice audio", variable=self.var_slice).grid(row=r, column=1, sticky="w")
        ttk.Checkbutton(self, text="Slice video", variable=self.var_slice_video).grid(row=r, column=2, sticky="w")
        ttk.Checkbutton(self, text="Fast cut (stream copy)", variable=self.var_fast_cut).grid(row=r, column=3, sticky="w")
        ttk.Checkbutton(self, text="One folder (merge)", variable=self.var_onefolder).grid(row=r, column=4, sticky="w")
        ttk.Checkbutton(self, text="Speaker tags in .txt", variable=self.var_tags).grid(row=r, column=5, sticky="w")
        ttk.Label(self, text="Compute type:").grid(row=r, column=6, sticky="e")
        self.cmb_comp = ttk.Combobox(self, textvariable=self.var_compute, values=_COMPUTE_CHOICES, width=10, state="readonly")
        self.cmb_comp.grid(row=r, column=7, sticky="w")
        ttk.Label(self, text="TF32:").grid(row=r, column=8, sticky="e")
        self.cmb_tf32 = ttk.Combobox(self, textvariable=self.var_tf32, values=_TF32_CHOICES, width=6, state="readonly")
        self.cmb_tf32.grid(row=r, column=9, sticky="w")
        r += 1
        ttk.Label(self, text="Padding (s)").grid(row=r, column=0, sticky="e")
        ttk.Spinbox(self, from_=0.0, to=3.0, increment=0.05, textvariable=self.var_pad, width=6).grid(row=r, column=1, sticky="w")
        ttk.Label(self, text="HF token").grid(row=r, column=2, sticky="e")
        ttk.Entry(self, textvariable=self.var_hf, width=30, show="*").grid(row=r, column=3, columnspan=2, sticky="we", padx=6)
        
        ttk.Label(self, text="Workers:").grid(row=r, column=5, sticky="e") # <--- WORKER LABEL
        ttk.Spinbox(self, from_=1, to=16, textvariable=self.var_workers, width=4).grid(row=r, column=6, sticky="w") # <--- WORKER SPINBOX

        ttk.Label(self, text="Video Player:").grid(row=r, column=7, sticky="e")
        ttk.Entry(self, textvariable=self.var_player, width=20).grid(row=r, column=8, columnspan=2, sticky="we")
        ttk.Button(self, text="Browse", command=self.browse_player).grid(row=r, column=10, sticky="w", padx=6)
        
        r += 1
        self.lbl_conf = ttk.Label(self, text=str(conf_path()), foreground="#666")
        self.lbl_conf.grid(row=r, column=0, columnspan=11, sticky="w", pady=(6, 2))
        r += 1
        self.txt = tk.Text(self, height=16, wrap="word")
        self.txt.grid(row=r, column=0, columnspan=11, sticky="nsew", pady=(6, 0))
        self.rowconfigure(r, weight=1)
        for c in range(11): self.columnconfigure(c, weight=1)

    def log(self, s: str):
        self.txt.insert("end", s.rstrip() + "\n")
        self.txt.see("end")

    def _update_title_with_conf_path(self):
        self.master.title(f"Audio Splitter — GPU Control Panel {APP_VER} (build {app_build_id()}) | conf: {conf_path()}")
        self.lbl_conf.configure(text=str(conf_path()))

    def _load_conf_to_ui(self):
        cfg = read_yaml(conf_path())
        if not cfg: return
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
        self.var_compute.set(cfg.get("compute_type", self.var_compute.get()))
        self.var_tf32.set(cfg.get("tf32", self.var_tf32.get()))
        self.var_pad.set(float(cfg.get("padding_seconds", self.var_pad.get())))
        self.var_hf.set(cfg.get("hf_token", self.var_hf.get()))
        self.var_player.set(cfg.get("video_player_path", self.var_player.get()))
        self.var_workers.set(int(cfg.get("parallel_workers", self.var_workers.get()))) # <--- LOAD

    def _collect_ui_to_conf(self) -> dict:
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
            "compute_type": self.var_compute.get().strip(),
            "tf32": self.var_tf32.get().strip(),
            "padding_seconds": float(self.var_pad.get()),
            "hf_token": self.var_hf.get().strip(),
        }

    def on_save(self):
        cfg = self._collect_ui_to_conf()
        try:
            atomic_write_yaml(conf_path(), cfg)
            self.log(f"Saved {conf_path()}")
        except Exception as e:
            messagebox.showerror("Save failed", f"{e}")
            self.log(f"ERROR saving conf: {e}")
        self._update_title_with_conf_path()

    def _on_model_changed(self, *_):
        model = self.var_model.get().strip()
        self.log(f"[cfg] Model set to: {model}")
        try:
            cfg = read_yaml(conf_path())
            cfg = cfg or {}
            cfg["model"] = model
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

    def on_run(self):
        self.on_save()
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
            messagebox.showerror("Run failed", "split_audio.py not found in program folder.")
            return
        threading.Thread(target=self._reader, daemon=True).start()

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str.strip())
        return p if p.is_absolute() else (program_root() / p)

    def _reader(self):
        if self.proc and self.proc.stdout:
            for line in self.proc.stdout:
                msg = line.rstrip()
                self.log(msg)
                if msg.startswith("[speakers-json]"):
                    try:
                        path_str = msg.split("]", 1)[1].strip()
                        spk_path = self._resolve_path(path_str)
                        seg_path = spk_path.parent / "segments.json"
                        if spk_path.exists() and seg_path.exists():
                            self.after(0, lambda p=spk_path, s=seg_path: NamingDialog(self.master, p, s))
                    except Exception:
                        pass
        self.log("[process finished]")

    def _poll_queue(self):
        try:
            while True:
                self.log(self.queue.get_nowait().rstrip())
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
        win.title("About — Audio Splitter")
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

    def on_name_speakers(self):
        out = output_root()
        if not out.exists():
            messagebox.showinfo("No output", f"No output folder {out}")
            return
        candidates = []
        for child in out.iterdir():
            if child.is_dir() and (child / "speakers.json").exists() and (child / "segments.json").exists():
                candidates.append((child.stat().st_mtime, child))
        if not candidates:
            messagebox.showinfo("Nothing to name", "No speakers.json found in output folders.")
            return
        latest = sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]
        default_engine = _NER_SETTINGS.get("engine","auto")
        dlg = NERSelectDialog(self.master, initial=default_engine)
        self.wait_window(dlg)
        engine = dlg.result or default_engine
        if engine not in _NER_CHOICES:
            engine = default_engine
        _set_ner_settings(engine=engine)
        self.log(f"[ner] Engine set to: {engine}  |  {_ner_device_info(engine)}")
        NamingDialog(self.master, latest / "speakers.json", latest / "segments.json")

def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    app = App(root)
    root.geometry("1140x620")
    root.title(f"Audio Splitter — GPU Control Panel {APP_VER} (build {app_build_id()}) | conf: {conf_path()}")
    root.mainloop()

if __name__ == "__main__":
    main()