#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transcript Studio + WhisperX pipeline (v1.6.0)
- OPTIMIZATION: Configurable worker count via CLI.
- ROBUSTNESS: Better error logging from FFmpeg (captures stderr).
- PARALLEL PROCESSING: Cuts multiple segments at once.
- INPUT PRIORITY: Prefers original video files.
- NVENC UPGRADE: Uses NVIDIA GPU for precise cutting.
"""
from __future__ import annotations

import os, sys, math, time, shlex, yaml, json, subprocess, hashlib, datetime, concurrent.futures, argparse, tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PIPELINE_VERSION = "v1.6.0"

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
INPUT_DIR = DATA_DIR / "input"
WAV_DIR = DATA_DIR / "wav_files"
OUT_DIR = DATA_DIR / "output"
SPEAKER_NAMES_DIR = DATA_DIR / "speaker_names"

FFMPEG_BIN  = str((ROOT / "ffmpeg.exe").resolve())  if (ROOT / "ffmpeg.exe").exists()  else "ffmpeg"
FFPROBE_BIN = str((ROOT / "ffprobe.exe").resolve()) if (ROOT / "ffprobe.exe").exists() else "ffprobe"

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
SOURCE_IDENTITY_FIELDS = ("path", "size", "mtime_ns", "st_dev", "st_ino")
PROGRESS_PREFIX = "@@ATS_PROGRESS@@"


def emit_progress(
    phase: str,
    label: str,
    file_percent: float,
    file_index: int,
    file_total: int,
) -> None:
    payload = {
        "phase": phase,
        "label": label,
        "file_percent": file_percent,
        "file_index": file_index,
        "file_total": file_total,
    }
    print(PROGRESS_PREFIX + json.dumps(payload, separators=(",", ":")), flush=True)

def normalized_source_path(path: Path | str) -> str:
    resolved = Path(path).expanduser().resolve(strict=True)
    return os.path.normcase(os.path.normpath(str(resolved)))

def build_source_identity(path: Path | str | None) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        source_stat = resolved.stat()
        return {
            "path": os.path.normcase(os.path.normpath(str(resolved))),
            "size": int(source_stat.st_size),
            "mtime_ns": int(source_stat.st_mtime_ns),
            "st_dev": int(source_stat.st_dev),
            "st_ino": int(source_stat.st_ino),
        }
    except (OSError, TypeError, ValueError):
        return None

def is_valid_source_identity(identity: Any) -> bool:
    if not isinstance(identity, dict):
        return False
    if set(SOURCE_IDENTITY_FIELDS) - set(identity):
        return False
    identity_path = identity.get("path")
    if not isinstance(identity_path, str) or not identity_path:
        return False
    for field in SOURCE_IDENTITY_FIELDS[1:]:
        value = identity.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    return True

def source_identities_match(saved: Any, current: Any) -> bool:
    if not is_valid_source_identity(saved) or not is_valid_source_identity(current):
        return False
    if os.path.normcase(os.path.normpath(saved["path"])) != os.path.normcase(os.path.normpath(current["path"])):
        return False
    for field in SOURCE_IDENTITY_FIELDS[1:]:
        if saved[field] != current[field]:
            return False
    return True

def load_verified_speaker_names(names_path: Path, source_path: Path) -> Tuple[Dict[str, str], str]:
    if not names_path.is_file():
        return {}, "missing"
    try:
        raw = yaml.safe_load(names_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, "malformed"
    if not isinstance(raw, dict):
        return {}, "malformed"
    if "source_identity" not in raw:
        return {}, "legacy"
    if not is_valid_source_identity(raw.get("source_identity")):
        return {}, "malformed"
    current_identity = build_source_identity(source_path)
    if current_identity is None:
        return {}, "source_unavailable"
    if not source_identities_match(raw.get("source_identity"), current_identity):
        return {}, "identity_mismatch"
    speaker_names = raw.get("speaker_names")
    if not isinstance(speaker_names, dict):
        return {}, "malformed"
    verified = {
        str(speaker): str(name).strip()
        for speaker, name in speaker_names.items()
        if isinstance(speaker, str) and isinstance(name, str) and name.strip()
    }
    return verified, "verified"

def _speaker_name_record_path_from_identity(identity: Dict[str, Any]) -> Path:
    path_hash = hashlib.sha256(identity["path"].encode("utf-8", errors="surrogatepass")).hexdigest()
    return SPEAKER_NAMES_DIR / f"{path_hash}.yaml"

def speaker_name_record_path(source_path: Path | str | None) -> Optional[Path]:
    identity = build_source_identity(source_path)
    return _speaker_name_record_path_from_identity(identity) if identity is not None else None

def write_speaker_name_record(
    source_path: Path | str | None,
    speaker_names: Dict[str, str],
    *,
    overwrite: bool = True,
) -> Optional[Path]:
    identity = build_source_identity(source_path)
    if identity is None:
        return None
    record_path = _speaker_name_record_path_from_identity(identity)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_data = {
        "source_identity": identity,
        "speaker_names": {
            str(speaker): str(name).strip()
            for speaker, name in speaker_names.items()
            if isinstance(speaker, str) and isinstance(name, str) and name.strip()
        },
    }

    if not overwrite:
        with record_path.open("x", encoding="utf-8", newline="\n") as record_file:
            yaml.safe_dump(record_data, record_file, sort_keys=False, allow_unicode=True)
            record_file.flush()
            os.fsync(record_file.fileno())
        return record_path

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=record_path.parent,
            prefix=f".{record_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            yaml.safe_dump(record_data, temp_file, sort_keys=False, allow_unicode=True)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, record_path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return record_path

def load_speaker_names_with_migration(
    source_path: Path,
    output_names_path: Path,
) -> Tuple[Dict[str, str], str]:
    persistent_path = speaker_name_record_path(source_path)
    if persistent_path is None:
        return {}, "source_unavailable"

    if persistent_path.exists():
        names, status = load_verified_speaker_names(persistent_path, source_path)
        return names, f"persistent_{status}"

    output_names, output_status = load_verified_speaker_names(output_names_path, source_path)
    if output_status != "verified":
        return {}, f"output_{output_status}"

    try:
        write_speaker_name_record(source_path, output_names, overwrite=False)
    except FileExistsError:
        names, status = load_verified_speaker_names(persistent_path, source_path)
        return names, f"persistent_{status}"
    except Exception:
        return output_names, "migration_failed"
    return output_names, "migrated"

def names_for_current_speakers(saved_names: Dict[str, str], speakers: List[str]) -> Dict[str, str]:
    current_speakers = set(speakers)
    return {
        speaker: name
        for speaker, name in saved_names.items()
        if speaker.startswith("SPEAKER_")
        and speaker.removeprefix("SPEAKER_").isdigit()
        and speaker in current_speakers
    }

def build_id() -> str:
    try:
        p = Path(__file__).resolve()
        h = hashlib.sha256(p.read_bytes()).hexdigest()[:8]
        ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d")
        return f"sha{h}-{ts}"
    except Exception:
        return "unknown"

def print_version() -> None:
    print(f"Transcript Studio pipeline {PIPELINE_VERSION} (build {build_id()})")
    try:
        import importlib.metadata as md
    except Exception:
        md = None
    try:
        import torch
        torch_line = f"PyTorch {getattr(torch, '__version__', '?')} | CUDA available: {torch.cuda.is_available()} | CUDA: {getattr(torch.version, 'cuda', None)}"
    except Exception:
        torch_line = "PyTorch: (not importable)"
    details = [torch_line]
    for dist in ("whisperx", "pyannote.audio", "ctranslate2"):
        try:
            ver = md.version(dist) if md else "(unknown)"
            details.append(f"{dist}: {ver}")
        except Exception:
            details.append(f"{dist}: (not installed)")
    print("\n".join(details))

def print_rel_or_abs(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except Exception:
        return str(p)

def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    # Capture output for debugging
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr

def run_ffmpeg(args: List[str]) -> None:
    cmd = [FFMPEG_BIN] + args
    rc, out, err = run_cmd(cmd)
    if rc != 0:
        # Optimization: Return the actual error message
        clean_err = "\n".join(line for line in err.splitlines() if "Error" in line or "Invalid" in line or "failed" in line)
        raise RuntimeError(f"ffmpeg failed (rc={rc}): {clean_err or 'Check log for details'}")

def hhmmss(secs: float) -> str:
    secs = max(0, int(secs))
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def probe_duration_seconds(path: Path) -> Optional[float]:
    try:
        rc, out, err = run_cmd([
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ])
        if rc == 0:
            return float(out.strip())
    except Exception:
        pass
    return None

@dataclass
class Conf:
    language: Optional[str] = "en"
    model: str = "large-v3"
    diarize: bool = True
    output_format: str = "both"
    padding_seconds: float = 0.25
    tf32: str = "on"
    allow_tf32_matmul: Optional[bool] = None
    allow_tf32_cudnn: Optional[bool] = None
    slice_audio: bool = True
    slice_video: bool = False
    fast_cut_video: bool = False
    merge_all_segments_into_one_folder: bool = False
    txt_speaker_tags: bool = True
    compute_type_cuda: str = "float16"
    HF_token: Optional[str] = None
    parallel_workers: int = 4
    diarization_speaker_mode: str = "auto"
    min_speakers: int = 2
    max_speakers: int = 2
    transcription_backend: str = "whisperx"
    crisperwhisper: Dict[str, Any] = field(default_factory=dict)

def load_conf(path: Path) -> Tuple[Conf, Optional[str]]:
    if not path.exists():
        print(f"[!] conf.yaml not found at {print_rel_or_abs(path)}; using defaults.")
        return Conf(), None
    print("Config loaded from: conf.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if "pad_seconds" in data and "padding_seconds" not in data:
        data["padding_seconds"] = data["pad_seconds"]
    if "one_folder" in data and "merge_all_segments_into_one_folder" not in data:
        data["merge_all_segments_into_one_folder"] = bool(data["one_folder"])
    if "compute_type" in data and "compute_type_cuda" not in data:
        data["compute_type_cuda"] = data["compute_type"]

    for bool_key in ("diarize", "slice_audio", "slice_video", "fast_cut_video", 
                     "merge_all_segments_into_one_folder", "txt_speaker_tags"):
        if bool_key in data:
            data[bool_key] = bool(data[bool_key])

    tval = str(data.get("tf32", "on")).strip().lower()
    if tval in ("enable", "enabled", "true", "yes", "1", "on"):
        data["tf32"] = "on"
    elif tval in ("disable", "disabled", "false", "no", "0", "off"):
        data["tf32"] = "off"
    elif tval not in ("on", "off", "auto"):
        data["tf32"] = "on"

    hf_token = (
        data.get("HF_token") or data.get("hf_token") or data.get("HF_TOKEN") or
        data.get("HUGGINGFACE_TOKEN") or data.get("HUGGING_FACE_HUB_TOKEN") or data.get("HUGGINGFACE_HUB_TOKEN")
    )

    base = {}
    for k in asdict(Conf()).keys():
        if k in data:
            base[k] = data[k]
    cfg = Conf(**base)
    return cfg, hf_token

def diarization_speaker_kwargs(cfg: Conf) -> Dict[str, int]:
    mode = str(cfg.diarization_speaker_mode or "auto").strip().lower()
    if mode == "auto":
        return {}

    def positive_int(value, label: str) -> int:
        text = str(value).strip()
        if not text.isdigit():
            raise ValueError(f"{label} must be a positive integer.") from None
        number = int(text)
        if number <= 0:
            raise ValueError(f"{label} must be a positive integer.")
        return number

    if mode == "exact":
        number = positive_int(cfg.min_speakers, "Speaker count")
        return {"min_speakers": number, "max_speakers": number}
    if mode == "range":
        minimum = positive_int(cfg.min_speakers, "Minimum speaker count")
        maximum = positive_int(cfg.max_speakers, "Maximum speaker count")
        if minimum > maximum:
            raise ValueError("Minimum speaker count cannot be greater than maximum speaker count.")
        return {"min_speakers": minimum, "max_speakers": maximum}
    raise ValueError("Diarization speaker mode must be auto, exact, or range.")

def setup_device(cfg: Conf) -> str:
    import torch
    if not torch.cuda.is_available():
        print("[!] CUDA is not available. Exiting to avoid CPU fallback.")
        sys.exit(1)
    if cfg.allow_tf32_matmul is not None:
        torch.backends.cuda.matmul.allow_tf32 = bool(cfg.allow_tf32_matmul)
    if cfg.allow_tf32_cudnn is not None:
        torch.backends.cudnn.allow_tf32 = bool(cfg.allow_tf32_cudnn)
    if cfg.allow_tf32_matmul is None and cfg.allow_tf32_cudnn is None:
        if cfg.tf32 == "on":
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        elif cfg.tf32 == "off":
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
    print(f"TF32 settings: matmul={torch.backends.cuda.matmul.allow_tf32} cudnn={torch.backends.cudnn.allow_tf32}")
    try:
        name = torch.cuda.get_device_name(0)
        print(f"CUDA device: {name}")
    except Exception:
        print("CUDA device: <unknown>")
    return "cuda"

def discover_sources(explicit_files: List[str] = None) -> List[Path]:
    if explicit_files:
        chosen = []
        for f in explicit_files:
            p = Path(f).resolve()
            if p.exists() and p.is_file():
                chosen.append(p)
            else:
                print(f"[warn] Explicit file not found or invalid: {f}")
        return sorted(list(set(chosen)), key=lambda x: str(x).lower())

    media: List[Path] = []
    wavs: List[Path] = []
    
    if INPUT_DIR.exists():
        for p in INPUT_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
                media.append(p.resolve())
    
    if WAV_DIR.exists():
        for p in WAV_DIR.rglob("*.wav"):
            if p.is_file():
                wavs.append(p.resolve())
    
    def stemkey(p: Path) -> str: return p.stem.lower()
    input_keys = {stemkey(m) for m in media}
    
    chosen: List[Path] = []
    chosen.extend(media)
    for w in wavs:
        if stemkey(w) not in input_keys: chosen.append(w.resolve())
    chosen = sorted(set(chosen), key=lambda p: (p.suffix.lower(), str(p).lower()))
    return chosen

def to_safe_title(path: Path) -> str:
    name = path.stem
    return "".join(ch if ch.isalnum() or ch in (" ", "_", "-") else "_" for ch in name).strip("_ ")

def convert_to_wav16k(src: Path, dst_dir: Path) -> Path:
    safe_mkdir(dst_dir)
    out = dst_dir / (src.stem + ".wav")
    if out.exists() and out.stat().st_size > 0:
        print(f"Using existing WAV: {out.name}")
        return out
    print(f"Converting to WAV: {src.name}")
    run_ffmpeg([ "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out) ])
    return out

def load_asr_model(device: str, cfg: Conf):
    import whisperx
    compute_type = cfg.compute_type_cuda
    print(f"Loading WhisperX model='{cfg.model}' on {device} (compute_type={compute_type}) ...")
    return whisperx.load_model(cfg.model, device, compute_type=compute_type, language=cfg.language)

def transcribe_whisperx(
    model,
    audio_path: Path,
    device: str,
    cfg: Conf,
    progress_callback: Optional[Callable[[str, str, float], None]] = None,
) -> Dict[str, Any]:
    import whisperx
    if progress_callback:
        progress_callback("transcribing", "Transcribing", 25)
    print(">>Performing transcription...")
    result = model.transcribe(str(audio_path))
    if cfg.language:
        if progress_callback:
            progress_callback("aligning", "Aligning", 55)
        print(">>Performing alignment...")
        model_a, metadata = whisperx.load_align_model(language_code=cfg.language, device=device)
        result = whisperx.align(result["segments"], model_a, metadata, str(audio_path), device)
        print(">>Performed alignment.")
    return apply_diarization(result, audio_path, device, cfg, progress_callback)

def apply_diarization(
    result: Dict[str, Any],
    audio_path: Path,
    device: str,
    cfg: Conf,
    progress_callback: Optional[Callable[[str, str, float], None]] = None,
) -> Dict[str, Any]:
    """Apply the existing WhisperX/pyannote speaker assignment to timed words."""

    diar_ok = False
    if cfg.diarize:
        import whisperx
        speaker_kwargs = diarization_speaker_kwargs(cfg)
        if not speaker_kwargs:
            print("[diarization] Speaker count: automatic")
        elif speaker_kwargs["min_speakers"] == speaker_kwargs["max_speakers"]:
            print(f"[diarization] Speaker count: exactly {speaker_kwargs['min_speakers']}")
        else:
            print(
                f"[diarization] Speaker count range: "
                f"{speaker_kwargs['min_speakers']}–{speaker_kwargs['max_speakers']}"
            )
        hf_token = cfg.HF_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")                        or os.environ.get("HUGGING_FACE_HUB_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        if hf_token:
            os.environ.setdefault("HF_TOKEN", hf_token)
            os.environ.setdefault("HUGGINGFACE_TOKEN", hf_token)
            os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", hf_token)
            os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", hf_token)
        try:
            if progress_callback:
                progress_callback("identifying_speakers", "Identifying speakers", 68)
            print(">>Performing diarization...")
            try:
                from whisperx.diarize import DiarizationPipeline  # type: ignore
                try:
                    diarize_model = DiarizationPipeline(token=hf_token, device=device)
                except TypeError:
                    diarize_model = DiarizationPipeline(use_auth_token=hf_token, device=device)
                diarize_segments = diarize_model(str(audio_path), **speaker_kwargs)
            except Exception:
                from pyannote.audio import Pipeline  # type: ignore
                try:
                    diarize_model = Pipeline.from_pretrained(
                        "pyannote/speaker-diarization-3.1",
                        token=hf_token
                    )
                except TypeError:
                    diarize_model = Pipeline.from_pretrained(
                        "pyannote/speaker-diarization-3.1",
                        use_auth_token=hf_token
                    )
                try:
                    diarize_model.to(device)
                except Exception:
                    pass
                diarize_segments = diarize_model(str(audio_path), **speaker_kwargs)
            result = whisperx.assign_word_speakers(diarize_segments, result)
            diar_ok = True
        except Exception as e:
            print(f"[!] Diarization failed ({e}). Continuing without diarization.")
            diar_ok = False
    result["__diar_ok__"] = diar_ok
    return result

def transcribe_configured_backend(
    audio_path: Path,
    device: str,
    cfg: Conf,
    progress_callback: Optional[Callable[[str, str, float], None]] = None,
    *,
    crisper_backend=None,
    crisper_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Transcribe one WAV through the explicitly selected backend."""

    from crisperwhisper_backend import (
        CrisperWhisperBackend,
        normalize_backend_name,
        normalize_worker_result,
        resolve_crisperwhisper_settings,
    )

    backend_name = normalize_backend_name(cfg.transcription_backend)
    if backend_name == "whisperx":
        if progress_callback:
            progress_callback("loading_model", "Loading WhisperX model", 12)
        model = load_asr_model(device, cfg)
        return transcribe_whisperx(
            model,
            audio_path,
            device,
            cfg,
            progress_callback=progress_callback,
        ), None

    settings = crisper_settings or resolve_crisperwhisper_settings(
        cfg.crisperwhisper,
        cfg.language,
    )
    family = settings["model"]["family"]
    mode = settings["transcription"]["mode"]
    if progress_callback:
        progress_callback(
            "loading_model",
            f"Loading CrisperWhisper {family}",
            12,
        )

    adapter = crisper_backend or CrisperWhisperBackend(ROOT)

    def worker_status(stage: str, _message: str) -> None:
        if not progress_callback:
            return
        if stage == "loading_model":
            progress_callback(
                "loading_model",
                f"Loading CrisperWhisper {family}",
                12,
            )
        elif stage == "transcribing":
            progress_callback(
                "transcribing",
                f"Transcribing with CrisperWhisper {family}, {mode}",
                25,
            )

    worker_response = adapter.transcribe(
        audio_path,
        settings,
        status_callback=worker_status,
    )
    normalized = normalize_worker_result(worker_response)
    result = {"segments": normalized["segments"]}
    result = apply_diarization(
        result,
        audio_path,
        device,
        cfg,
        progress_callback=progress_callback,
    )
    return result, normalized["transcription"]

def srt_timestamp(t: float) -> str:
    if t < 0: t = 0.0
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - math.floor(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def write_srt(segments, out_path: Path) -> None:
    print(f"Created SRT: {out_path.name}")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for i, seg in enumerate(segments, 1):
            start = float(seg.get("start", 0.0)); end = float(seg.get("end", start))
            text = str(seg.get("text", "")).strip()
            spk = seg.get("speaker")
            line = f"{spk}: {text}" if spk else text
            f.write(str(i)); f.write("\n")
            f.write(f"{srt_timestamp(start)} --> {srt_timestamp(end)}"); f.write("\n")
            f.write(line); f.write("\n\n")

def write_txt(segments, out_path: Path, diarized: bool, include_speakers: bool) -> None:
    print(f"Created TXT: {out_path.name}")
    import os as _os
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        if diarized and include_speakers:
            last_spk: Optional[str] = None; buffer: List[str] = []
            def flush():
                if not buffer or last_spk is None: return
                text = " ".join(buffer).strip()
                if text: f.write(f"{last_spk}: {text}" + _os.linesep)
                buffer.clear()
            for seg in segments:
                text = str(seg.get("text", "")).strip()
                if not text: continue
                spk = seg.get("speaker") or "SPEAKER_00"
                if spk != last_spk and last_spk is not None: flush()
                last_spk = spk; buffer.append(text)
            flush()
        else:
            joined = " ".join(str(seg.get("text", "")).strip() for seg in segments if seg.get("text")).strip()
            if joined: f.write(joined + _os.linesep)

def _worker_ffmpeg_wrapper(cmd: List[str]):
    try:
        run_ffmpeg(cmd)
    except Exception as e:
        print(f"[!] Worker Error: {e}")

def cut_segments_to_wavs(audio_path: Path, segments, out_dir: Path, padding: float = 0.25, merge_all: bool = False, workers: int = 4) -> None:
    safe_mkdir(out_dir)
    try:
        rc, out, err = run_cmd([ FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path) ])
        dur = float(out.strip()) if rc == 0 else None
    except Exception: dur = None
    
    tasks = []
    print(f"Cutting audio segments (x{workers} parallel)...")
    for idx, seg in enumerate(segments, 1):
        spk = (seg.get("speaker") or "SPEAKER_00").replace(" ", "_")
        start = max(0.0, float(seg.get("start", 0.0)) - padding)
        end = float(seg.get("end", start))
        end = min(end + padding, dur) if dur is not None else end + padding
        target_dir = out_dir if merge_all else (out_dir / spk)
        safe_mkdir(target_dir)
        filename = f"{spk}_{idx:04d}.wav" if merge_all else f"{idx:04d}.wav"
        out_path = target_dir / filename
        cmd = [ "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(audio_path), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_path)]
        tasks.append(cmd)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_worker_ffmpeg_wrapper, tasks))

def cut_segments_to_video(video_path: Path, segments, out_dir: Path, padding: float = 0.25, merge_all: bool = False, fast_cut: bool = False, workers: int = 4) -> None:
    safe_mkdir(out_dir)
    try:
        rc, out, err = run_cmd([ FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path) ])
        dur = float(out.strip()) if rc == 0 else None
    except Exception: dur = None
    
    mode = "FAST (stream copy)" if fast_cut else "PRECISE (h264_nvenc)"
    print(f"Cutting video segments (x{workers} parallel) [{mode}]...")
    
    tasks = []
    for idx, seg in enumerate(segments, 1):
        spk = (seg.get("speaker") or "SPEAKER_00").replace(" ", "_")
        start = max(0.0, float(seg.get("start", 0.0)) - padding)
        end = float(seg.get("end", start))
        end = min(end + padding, dur) if dur is not None else end + padding
        target_dir = out_dir if merge_all else (out_dir / spk)
        safe_mkdir(target_dir)
        filename = f"{spk}_{idx:04d}.mp4" if merge_all else f"{idx:04d}.mp4"
        out_path = target_dir / filename
        
        if fast_cut:
            cmd = [ "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video_path), "-c", "copy", "-map", "0", str(out_path)]
        else:
            # Use NVIDIA GPU for encoding (p1 is fastest preset)
            cmd = [ "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video_path), 
                   "-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23", 
                   "-c:a", "aac", "-b:a", "192k", str(out_path)]
        tasks.append(cmd)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_worker_ffmpeg_wrapper, tasks))

def run_pipeline(explicit_files: List[str] = None, explicit_workers: int = None) -> None:
    print("Starting processing...")
    pipeline_start = time.perf_counter()
    cfg, hf_token = load_conf(ROOT / "conf.yaml")
    from crisperwhisper_backend import (
        CrisperWhisperBackend,
        normalize_backend_name,
        resolve_crisperwhisper_settings,
    )
    backend_name = normalize_backend_name(cfg.transcription_backend)
    if cfg.diarize:
        try:
            diarization_speaker_kwargs(cfg)
        except ValueError as e:
            print(f"[!] Invalid diarization speaker-count settings: {e}")
            raise SystemExit(2)
    
    # CLI override for workers
    workers = explicit_workers if explicit_workers else cfg.parallel_workers
    
    if hf_token:
        os.environ["HF_TOKEN"] = hf_token
        os.environ.setdefault("HUGGINGFACE_TOKEN", hf_token)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", hf_token)
        os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", hf_token)
    device = setup_device(cfg)
    
    sources = discover_sources(explicit_files)
    
    if not sources:
        if explicit_files:
            print("[!] No valid files found from the selection.")
        else:
            print("[!] No files found in 'data/input' or 'data/wav_files'.")
        return

    print(f"Found {len(sources)} file(s) to process:")
    for p in sources: print(f" - {print_rel_or_abs(p)}")

    crisper_backend = None
    crisper_settings = None
    if backend_name == "crisperwhisper":
        crisper_settings = resolve_crisperwhisper_settings(cfg.crisperwhisper, cfg.language)
        crisper_backend = CrisperWhisperBackend(ROOT)
        crisper_backend.probe()
        print("[crisper] Isolated CrisperWhisper runtime is ready.")
    
    file_total = len(sources)
    for idx, src in enumerate(sources, 1):
        def file_progress(phase: str, label: str, file_percent: float) -> None:
            emit_progress(phase, label, file_percent, idx, file_total)

        print(f"\nProcessing {idx}/{file_total}: {print_rel_or_abs(src)}")
        file_progress("preparing_input", "Preparing input", 0)
        start_file = time.perf_counter()
        title = to_safe_title(src); title_dir = OUT_DIR / title; safe_mkdir(title_dir)
        saved_names, saved_names_status = load_speaker_names_with_migration(src, title_dir / "names.yaml")
        if saved_names_status == "migrated":
            print("[names] Migrated verified output-local speaker names to persistent storage.")
        elif saved_names_status == "migration_failed":
            print("[names] Verified output-local names restored, but persistent migration failed.")
        elif saved_names_status.endswith("identity_mismatch"):
            print("[names] Saved names not restored: source identity differs.")
        elif saved_names_status.endswith("legacy"):
            print("[names] Ignored legacy names.yaml without source identity.")
        elif saved_names_status.endswith("malformed"):
            print("[names] Ignored malformed names.yaml.")
        elif saved_names_status == "source_unavailable":
            print("[names] Saved names not restored: current source identity is unavailable.")
        
        wav_path = src
        if src.suffix.lower() != ".wav" or src.parent != WAV_DIR:
            wav_path = convert_to_wav16k(src, WAV_DIR)
            
        dur = probe_duration_seconds(wav_path)
        if dur is not None: print(f"  Media duration: {hhmmss(int(dur))} ({dur:.2f} s)")
        
        result, transcription_metadata = transcribe_configured_backend(
            wav_path,
            device,
            cfg,
            progress_callback=file_progress,
            crisper_backend=crisper_backend,
            crisper_settings=crisper_settings,
        )
        segments = result.get("segments") or []
        diar_ok = bool(result.get("__diar_ok__", False)) or any((s.get("speaker") or "") for s in segments)
        speakers = sorted({(s.get("speaker") or "SPEAKER_00") for s in segments if (s.get("speaker") or diar_ok)})
        restored_names = names_for_current_speakers(saved_names, speakers)
        if restored_names:
            print(f"[names] Restored {len(restored_names)} speaker name(s).")
        elif saved_names:
            print("[names] No saved names matched the current speaker IDs.")
        
        writing_json_label = (
            "Writing CrisperWhisper outputs"
            if backend_name == "crisperwhisper"
            else "Writing JSON outputs"
        )
        file_progress("writing_json", writing_json_label, 80)
        seg_json = {"title": title, "segments": segments, "source_path": str(src.resolve())}
        if transcription_metadata is not None:
            seg_json["transcription"] = transcription_metadata
        spk_json = {"title": title, "diarization": diar_ok, "speakers": speakers}
        if restored_names:
            spk_json["names"] = restored_names
        (title_dir / "segments.json").write_text(json.dumps(seg_json, ensure_ascii=False, indent=2), encoding="utf-8")
        (title_dir / "speakers.json").write_text(json.dumps(spk_json, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[segments-json] {print_rel_or_abs(title_dir / 'segments.json')}")
        print(f"[speakers-json] {print_rel_or_abs(title_dir / 'speakers.json')}")
        
        writing_transcripts_label = (
            "Writing CrisperWhisper outputs"
            if backend_name == "crisperwhisper"
            else "Writing SRT/TXT"
        )
        file_progress("writing_transcripts", writing_transcripts_label, 84)
        fmt = (cfg.output_format or "both").lower().strip()
        srt_path = title_dir / f"{title}.srt"; txt_path = title_dir / f"{title}.txt"
        if fmt in ("srt", "both"): write_srt(segments, srt_path)
        if fmt in ("txt", "both"): write_txt(segments, txt_path, diarized=diar_ok, include_speakers=cfg.txt_speaker_tags)
        
        if cfg.slice_audio:
            file_progress("cutting_audio", "Cutting audio", 90)
            cut_segments_to_wavs(wav_path, segments, title_dir, padding=cfg.padding_seconds,
                                 merge_all=cfg.merge_all_segments_into_one_folder, workers=workers)
        
        if cfg.slice_video and src.suffix.lower() in VIDEO_EXTS:
            file_progress("cutting_video", "Cutting video", 95)
            cut_segments_to_video(src, segments, title_dir, padding=cfg.padding_seconds,
                                  merge_all=cfg.merge_all_segments_into_one_folder,
                                  fast_cut=cfg.fast_cut_video, workers=workers)

        file_time = time.perf_counter() - start_file
        rtf = (dur / file_time) if (dur and file_time > 0) else None
        print(f"Finished {src.name} in {hhmmss(file_time)}" + (f"  |  RTF: {rtf:.2f}x" if rtf else ""))
        file_progress("file_complete", "File complete", 100)
    total = time.perf_counter() - pipeline_start
    print("Done."); print(f"Total elapsed: {hhmmss(total)}")

if __name__ == "__main__":
    if any(a in sys.argv for a in ("--version", "-V")):
        print_version(); sys.exit(0)
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", help="List of input files to process")
    parser.add_argument("--workers", type=int, default=None, help="Number of parallel workers")
    args, unknown = parser.parse_known_args()
    
    try:
        run_pipeline(explicit_files=args.inputs, explicit_workers=args.workers)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user."); sys.exit(1)
    except Exception as e:
        print(f"[FATAL] {e}"); sys.exit(1)
