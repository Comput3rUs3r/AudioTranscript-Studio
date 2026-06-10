# AudioTranscript Studio

AudioTranscript Studio is a Windows GUI tool for WhisperX transcription, speaker diarization, speaker naming, subtitle export, and audio segment extraction.

It is designed for users who want to turn audio or video files into organized transcripts, subtitles, speaker-labeled text, and separated audio clips.

This project began as a fork of `JarodMica/audiosplitter_whisper` and has been expanded with a larger GUI workflow, Hugging Face token handling, speaker naming tools, subtitle export options, and improved setup utilities.

---

## Features

* GUI control panel for running the transcription pipeline
* WhisperX transcription
* Speaker diarization with Hugging Face / pyannote
* Speaker naming after transcription
* SRT subtitle export
* TXT transcript export
* Audio segment extraction by speaker
* Optional video segment cutting
* Word-level export options
* Hugging Face environment check tool
* CUDA and CPU setup scripts
* Local configuration through `conf.yaml`

---

## Current Status

This project is currently in early public-release preparation.

The program works on the developer's Windows/NVIDIA setup, but installation instructions and compatibility testing are still being improved.

Tested working environment:

```text
Windows 11
Python 3.11
NVIDIA RTX GPU
PyTorch 2.5.1+cu124
WhisperX 3.4.2
pyannote.audio 3.3.2
ctranslate2 4.4.0
```

CPU mode may work, but it will be much slower and is not the main target.

---

## Requirements

Recommended:

* Windows 10 or Windows 11
* Python 3.11
* NVIDIA GPU with CUDA support
* FFmpeg
* Git
* Hugging Face account
* Hugging Face access token
* Accepted pyannote model terms on Hugging Face

Optional but recommended:

* Visual Studio Code

---

## Important Hugging Face Notice

Speaker diarization requires access to Hugging Face models.

You need your own Hugging Face token. Do **not** use someone else's token, and do **not** upload your token to GitHub.

Before running diarization, you must:

1. Create a Hugging Face account.
2. Accept the model terms for `pyannote/speaker-diarization-3.1`.
3. Accept the model terms for `pyannote/segmentation-3.0`.
4. Create a Hugging Face access token.
5. Add that token to your local `conf.yaml` file.

Your private `conf.yaml` file should never be uploaded to GitHub.

---

## Installation

Clone the repository:

```powershell
git clone https://github.com/Comput3rUs3r/AudioTranscript-Studio.git
cd AudioTranscript-Studio
```

Create or install the environment.

For NVIDIA CUDA users:

```powershell
python setup-cuda.py
```

For CPU users:

```powershell
python setup-cpu.py
```

Activate the virtual environment:

```powershell
venv\Scripts\activate
```

Install requirements manually if needed:

```powershell
pip install -r requirements-cuda.txt
```

or:

```powershell
pip install -r requirements-cpu.txt
```

---

## Configuration

The repo includes:

```text
conf.example.yaml
```

Copy it and rename the copy to:

```text
conf.yaml
```

Then open `conf.yaml` and add your Hugging Face token:

```yaml
hf_token: "YOUR_HUGGING_FACE_TOKEN_HERE"
```

Do not upload `conf.yaml` to GitHub.

---

## Folder Structure

Expected working folders:

```text
data/
├── input/
├── output/
└── wav_files/
```

Put your audio or video files in:

```text
data/input/
```

Generated files will be saved in:

```text
data/output/
```

Temporary converted WAV files may be stored in:

```text
data/wav_files/
```

---

## Running the GUI

After activating the virtual environment, run:

```powershell
python split_audio_gui.py
```

The GUI allows you to:

* Choose a WhisperX model
* Select language
* Enable or disable diarization
* Select output type
* Choose compute type
* Set TF32 behavior
* Select files manually
* Open the output folder
* Name speakers after processing

---

## Basic Workflow

1. Place an audio or video file in `data/input`, or use the GUI's file selection button.
2. Start the GUI:

```powershell
python split_audio_gui.py
```

3. Choose your model and settings.
4. Click `Run`.
5. Wait for transcription, alignment, and diarization.
6. Open the output folder.
7. Use `Name speakers` to replace speaker labels like `SPEAKER_00` with real names.
8. Export or use the generated `.srt`, `.txt`, `.json`, and audio segment files.

---

## Output Files

For each processed file, AudioTranscript Studio may create:

```text
segments.json
speakers.json
names.yaml
transcript.txt
subtitles.srt
speaker folders
audio clips
```

The exact output depends on the settings you choose.

---

## Notes About SRT and Styled Subtitles

SRT subtitles are plain and widely supported.

Future versions may include improved ASS subtitle export for styled subtitles, including colored speaker names.

---

## Troubleshooting

### The program says no files were found

Make sure your audio or video file is inside:

```text
data/input/
```

or select the file manually through the GUI.

---

### Diarization does not work

Check that:

* Your Hugging Face token is valid.
* Your token is saved in `conf.yaml`.
* You accepted the pyannote model terms.
* You have internet access the first time models are downloaded.

---

### CUDA or cuDNN errors

Make sure:

* You are using the correct virtual environment.
* PyTorch CUDA is installed correctly.
* Your NVIDIA drivers are installed.
* You are running from the activated `venv`.

Activate the environment:

```powershell
venv\Scripts\activate
```

Then run:

```powershell
python split_audio_gui.py
```

---

### VS Code Play button does not work correctly

The safest way to run the project is from an activated terminal:

```powershell
venv\Scripts\activate
python split_audio_gui.py
```

A dedicated launcher script may be added in a future version.

---

## Files That Should Not Be Uploaded

Do not upload:

```text
conf.yaml
venv/
__pycache__/
data/input/*
data/output/*
data/wav_files/*
*.bak
old local backup files
```

The repo should include `conf.example.yaml`, not your private `conf.yaml`.

---

## Credits

AudioTranscript Studio began as a fork of JarodMica/audiosplitter_whisper.

Original project copyright:
Copyright (c) 2023 Jarod Mica

Modifications copyright:
Copyright (c) 2026 Comput3rUs3r

This project is licensed under the MIT License.

This version has been expanded with a larger GUI workflow, Hugging Face token handling, speaker naming tools, SRT/TXT output, word-level export options, and improved setup utilities.

---

## License

This project uses the MIT License.

See the `LICENSE` file for details.
