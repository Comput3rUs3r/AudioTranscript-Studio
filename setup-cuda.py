import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "venv"

DATA_DIRS = [
    ROOT / "data" / "input",
    ROOT / "data" / "output",
    ROOT / "data" / "wav_files",
]


def run(cmd, check=True):
    print()
    print("Running:")
    print(" ".join(str(x) for x in cmd))
    print()
    return subprocess.run(cmd, check=check)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def create_virtual_environment():
    if VENV_DIR.exists():
        print("[ok] venv already exists. Reusing it.")
        return

    print("[setup] Creating virtual environment: venv")
    try:
        venv.create(str(VENV_DIR), with_pip=True)
    except Exception as e:
        print(f"[fatal] Failed to create virtual environment: {e}")
        sys.exit(1)


def upgrade_pip():
    py = venv_python()
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools<81"])


def install_requirements():
    py = venv_python()
    req = ROOT / "requirements-cuda.txt"

    if not req.exists():
        print("[fatal] requirements-cuda.txt not found.")
        sys.exit(1)

    run([str(py), "-m", "pip", "install", "-r", str(req)])


def create_project_folders():
    print()
    print("[setup] Creating data folders...")
    for folder in DATA_DIRS:
        folder.mkdir(parents=True, exist_ok=True)
        keep = folder / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
        print(f"[ok] {folder}")


def create_conf_if_missing():
    conf = ROOT / "conf.yaml"
    example = ROOT / "conf.example.yaml"

    if conf.exists():
        print("[ok] conf.yaml already exists.")
        return

    if example.exists():
        shutil.copyfile(example, conf)
        print("[ok] Created conf.yaml from conf.example.yaml.")
        print("[note] Open conf.yaml and add your Hugging Face token if you want diarization.")
    else:
        print("[warn] conf.example.yaml not found. Could not create conf.yaml automatically.")


def check_cuda():
    py = venv_python()
    code = (
        "import torch; "
        "print('PyTorch:', torch.__version__); "
        "print('CUDA available:', torch.cuda.is_available()); "
        "print('CUDA version:', torch.version.cuda); "
        "print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no cuda'); "
        "raise SystemExit(0 if torch.cuda.is_available() else 1)"
    )

    print()
    print("[check] Verifying CUDA PyTorch...")
    result = subprocess.run([str(py), "-c", code])

    if result.returncode != 0:
        print()
        print("[fatal] CUDA is not available in this venv.")
        print("This app needs the CUDA install for GPU transcription.")
        print("Check your NVIDIA driver and requirements-cuda.txt.")
        sys.exit(1)

    print("[ok] CUDA PyTorch is working.")


def check_packages():
    py = venv_python()
    code = (
        "import importlib.metadata as md; "
        "pkgs=['whisperx','pyannote.audio','ctranslate2']; "
        "print('Installed app packages:'); "
        "[print(f'  {p}: {md.version(p)}') for p in pkgs]"
    )

    print()
    print("[check] Checking package versions...")
    run([str(py), "-c", code])


def _command_exists(cmd):
    try:
        result = subprocess.run(
            [cmd, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def check_ffmpeg():
    print()
    print("[check] Checking FFmpeg...")

    local_ffmpeg = ROOT / "ffmpeg.exe"
    local_ffprobe = ROOT / "ffprobe.exe"

    if local_ffmpeg.exists() and local_ffprobe.exists():
        ffmpeg = str(local_ffmpeg)
        ffprobe = str(local_ffprobe)
    else:
        ffmpeg = "ffmpeg"
        ffprobe = "ffprobe"

    ffmpeg_ok = _command_exists(ffmpeg)
    ffprobe_ok = _command_exists(ffprobe)

    if ffmpeg_ok and ffprobe_ok:
        print("[ok] FFmpeg and FFprobe are available.")
        return

    print("[warn] FFmpeg or FFprobe was not found.")
    print("Install FFmpeg before processing videos/audio.")
    print("Recommended Windows command:")
    print("  winget install Gyan.FFmpeg")

def main():
    print("AudioTranscript Studio CUDA installer")
    print("------------------------------------")

    create_virtual_environment()
    upgrade_pip()
    install_requirements()
    create_project_folders()
    create_conf_if_missing()
    check_cuda()
    check_packages()
    check_ffmpeg()

    print()
    print("Install complete.")
    print("Next steps:")
    print("1. Open conf.yaml and add your Hugging Face token if you want speaker diarization.")
    print("2. Run run-gui.bat to start AudioTranscript Studio.")


if __name__ == "__main__":
    main()
