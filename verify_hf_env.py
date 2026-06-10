
#!/usr/bin/env python3
import os, yaml
from pathlib import Path

cfg_path = Path("conf.yaml")
if cfg_path.exists():
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
else:
    cfg = {}

print("conf.yaml HF_token:", (cfg.get("HF_token") or cfg.get("hf_token")) is not None)
print("Env HF_TOKEN set:", bool(os.getenv("HF_TOKEN")))
print("Env HUGGINGFACE_HUB_TOKEN set:", bool(os.getenv("HUGGINGFACE_HUB_TOKEN")))
