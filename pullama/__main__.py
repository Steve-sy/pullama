#!/usr/bin/env python3
"""
Pullama — Resumable Ollama Model Downloader & Installer
https://github.com/yourusername/pullama
---------------------------------------------------
Fix for ollama pull TLS handshake timeouts and disconnects.
Downloads models with resume support and installs them directly into Ollama.

  pullama pull tinyllama:latest
  pullama get gemma2:2b
  pullama install --model gemma2:2b --blobsPath ./downloads
  pullama list
"""

import argparse
import sys
import os
import json
import urllib.request
import urllib.error
import shutil
import hashlib
import platform
import time
import subprocess
import datetime

# Constants
DEFAULT_REGISTRY = "registry.ollama.ai"
BLOBS_PATTERN = "blobs"
PULLAMA_DIR = os.path.expanduser("~/.pullama")
STATE_FILE = os.path.join(PULLAMA_DIR, "state.json")
VERSION = "1.0.0"


# ─── Colors ───────────────────────────────────────────────────────────────────

class Colors:
    HEADER  = '\033[95m'
    OKBLUE  = '\033[94m'
    OKCYAN  = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL    = '\033[91m'
    ENDC    = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'

def print_success(msg):
    print(f"{Colors.OKGREEN}✔ {msg}{Colors.ENDC}")

def print_info(msg):
    print(f"{Colors.OKCYAN}ℹ {msg}{Colors.ENDC}")

def print_warning(msg):
    print(f"{Colors.WARNING}⚠ {msg}{Colors.ENDC}")

def print_error(msg):
    print(f"{Colors.FAIL}✖ {msg}{Colors.ENDC}", file=sys.stderr)


# ─── State ────────────────────────────────────────────────────────────────────

def ensure_pullama_dir():
    os.makedirs(PULLAMA_DIR, exist_ok=True)

def load_state():
    ensure_pullama_dir()
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    ensure_pullama_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def update_model_state(state, model_key, **kwargs):
    if model_key not in state:
        state[model_key] = {}
    state[model_key].update(kwargs)


# ─── Utilities ────────────────────────────────────────────────────────────────

def format_size(size_in_bytes):
    if not isinstance(size_in_bytes, (int, float)) or size_in_bytes < 0:
        return "?"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            if unit == 'B':
                return f"{int(size_in_bytes)} B"
            return f"{size_in_bytes:.1f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.1f} PB"

def format_eta(seconds):
    if seconds < 0:
        return "--:--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m:02d}m"

def parse_model_name(model_name_input):
    tag = "latest"
    if ":" in model_name_input:
        base, tag = model_name_input.split(":", 1)
    else:
        base = model_name_input

    if "/" in base:
        namespace, model = base.split("/", 1)
    else:
        namespace = "library"
        model = base

    return namespace, model, tag

def get_default_models_path():
    """Auto-detect where Ollama actually stores models."""
    # Priority 1: explicit env var
    env_path = os.environ.get("OLLAMA_MODELS")
    if env_path:
        return os.path.expanduser(env_path)

    system = platform.system().lower()
    if system == "windows":
        base = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        candidates = [os.path.join(base, ".ollama", "models")]
    else:
        candidates = [
            "/usr/share/ollama/.ollama/models",  # system service (Linux official install)
            "/var/lib/ollama/.ollama/models",     # some Linux distros
            os.path.expanduser("~/.ollama/models"),  # user install / macOS
        ]

    # Return the first path that already has Ollama's directory structure
    for path in candidates:
        if os.path.isdir(os.path.join(path, "blobs")) or \
           os.path.isdir(os.path.join(path, "manifests")):
            return path

    # Fall back to user home path
    if system == "windows":
        return candidates[0]
    return os.path.expanduser("~/.ollama/models")

def verify_ollama_sees_model(model_key):
    """Check if the Ollama service can see the installed model via its API."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            names = [m.get("name", "") for m in data.get("models", [])]
            # model_key may be "gemma2:2b" — check if it appears in any listed name
            return any(model_key.split(":")[0] in n for n in names)
    except Exception:
        return None  # Ollama not running or unreachable

def get_models_path(explicit_path=None):
    if explicit_path:
        return os.path.expanduser(explicit_path)
    return get_default_models_path()

def get_file_hash(filepath):
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

def verify_blob(filepath, expected_digest):
    """Verify SHA256 of a downloaded blob. expected_digest is like 'sha256:abc123...'"""
    expected_hash = expected_digest.replace("sha256:", "")
    actual_hash = get_file_hash(filepath)
    return actual_hash == expected_hash


# ─── Download Engine ──────────────────────────────────────────────────────────

def check_aria2():
    return shutil.which("aria2c") is not None

def print_aria2_hint():
    system = platform.system().lower()
    if system == "linux":
        install_cmd = "sudo apt install aria2"
    elif system == "darwin":
        install_cmd = "brew install aria2"
    else:
        install_cmd = "winget install aria2"
    print(f"{Colors.DIM}⚡ Tip: Install aria2 for faster downloads: {install_cmd}{Colors.ENDC}\n")

def _render_progress(label, downloaded, total, speed_bps, elapsed):
    try:
        cols = os.get_terminal_size().columns
    except OSError:
        cols = 80

    pct = downloaded / total if total > 0 else 0

    # Build fixed right side: "  165/261 MB  2.6 MB/s  ETA 58s"
    size_str  = f"{format_size(downloaded)}/{format_size(total)}"
    speed_str = f"{format_size(speed_bps)}/s" if speed_bps > 0 else ""
    remaining = total - downloaded
    eta_str   = f"ETA {format_eta(remaining / speed_bps)}" if speed_bps > 0 else ""
    right = f"  {size_str}  {speed_str}  {eta_str}"

    # Calculate how much space is left for label + bar
    # Layout: "  {label}  {bar}{right}"  — prefix=2, sep=2
    bar_width = 18
    prefix = "  "
    sep    = "  "
    available = cols - len(prefix) - len(sep) - bar_width - len(right) - 1
    label_display = label[:max(0, available)]

    # Shrink bar if terminal is very narrow
    if available < 0:
        bar_width = max(5, cols - len(prefix) - len(right) - 2)
        label_display = ""

    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    # Build plain version to measure true visible length
    if label_display:
        plain = f"{prefix}{label_display}{sep}{bar}{right}"
    else:
        plain = f"{prefix}{bar}{right}"

    trailing = " " * max(0, cols - len(plain) - 1)

    # Build colored version (same structure, ANSI codes don't affect visible width)
    if label_display:
        colored = (
            f"{prefix}{Colors.DIM}{label_display}{Colors.ENDC}{sep}"
            f"{Colors.OKCYAN}{bar}{Colors.ENDC}"
            f"{Colors.BOLD}{right}{Colors.ENDC}"
        )
    else:
        colored = (
            f"{prefix}{Colors.OKCYAN}{bar}{Colors.ENDC}"
            f"{Colors.BOLD}{right}{Colors.ENDC}"
        )

    print(f"\r{colored}{trailing}", end="", flush=True)

def download_with_urllib(url, dest_path, expected_size, label=""):
    """Download with resume support using HTTP Range requests."""
    existing = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0

    if existing >= expected_size:
        return True  # already complete

    headers = {}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"

    req = urllib.request.Request(url, headers=headers)

    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            # Range not satisfiable — file already fully downloaded
            return True
        print_error(f"HTTP {e.code} downloading {label}")
        return False
    except urllib.error.URLError as e:
        print_error(f"Connection error: {e.reason}")
        return False

    chunk_size = 65536  # 64 KB
    downloaded = existing
    start_time = time.time()
    last_print = start_time

    try:
        with open(dest_path, "ab" if existing > 0 else "wb") as f:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                elapsed = now - start_time
                if now - last_print >= 0.3:
                    speed = (downloaded - existing) / elapsed if elapsed > 0 else 0
                    _render_progress(label, downloaded, expected_size, speed, elapsed)
                    last_print = now
    except KeyboardInterrupt:
        print()
        raise
    except Exception as e:
        print()
        print_error(f"Download interrupted: {e}")
        return False

    # Final progress line
    elapsed = time.time() - start_time
    speed = (downloaded - existing) / elapsed if elapsed > 0 else 0
    _render_progress(label, downloaded, expected_size, speed, elapsed)
    print()
    return downloaded >= expected_size

def download_with_aria2(url, dest_path, expected_size, label=""):
    """Download using aria2c for maximum reliability on slow connections."""
    dest_dir = os.path.dirname(dest_path)
    dest_file = os.path.basename(dest_path)

    cmd = [
        "aria2c",
        "--continue=true",
        "--max-connection-per-server=4",
        "--split=4",
        "--min-split-size=1M",
        "--timeout=60",
        "--retry-wait=5",
        "--max-tries=10",
        "--dir", dest_dir,
        "--out", dest_file,
        url
    ]

    print(f"  {Colors.DIM}{label}{Colors.ENDC}  {Colors.OKCYAN}[aria2]{Colors.ENDC} downloading...", flush=True)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"\r  {Colors.DIM}{label}{Colors.ENDC}  {Colors.OKGREEN}✔{Colors.ENDC}{' ' * 60}")
            return True
        else:
            print_error(f"aria2 failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        print_error(f"aria2 error: {e}")
        return False

def download_blob(url, dest_path, expected_size, label="", use_aria2=False):
    """Download a blob, skipping if already complete."""
    existing = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
    if existing >= expected_size:
        print(f"  {Colors.DIM}{label}{Colors.ENDC}  {Colors.OKGREEN}✔ already complete{Colors.ENDC}")
        return True

    if existing > 0:
        print_info(f"Resuming {label} from {format_size(existing)} / {format_size(expected_size)}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    if use_aria2:
        return download_with_aria2(url, dest_path, expected_size, label)
    else:
        return download_with_urllib(url, dest_path, expected_size, label)


# ─── Manifest Fetching ────────────────────────────────────────────────────────

def fetch_manifest(namespace, model, tag):
    url = f"https://{DEFAULT_REGISTRY}/v2/{namespace}/{model}/manifests/{tag}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.docker.distribution.manifest.v2+json"
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw), raw, url
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print_error(f"Model not found on registry.")
        else:
            print_error(f"HTTP {e.code} fetching manifest.")
        sys.exit(1)
    except urllib.error.URLError as e:
        print_error(f"Connection error: {e.reason}")
        sys.exit(1)


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_get(args):
    namespace, model, tag = parse_model_name(args.model)
    model_key = f"{namespace}/{model}:{tag}" if namespace != "library" else f"{model}:{tag}"

    print(f"\n{Colors.BOLD}Fetching {Colors.OKCYAN}{args.model}{Colors.ENDC}{Colors.BOLD} from registry...{Colors.ENDC}")

    data, raw, manifest_url = fetch_manifest(namespace, model, tag)

    layers = data.get("layers", [])
    config = data.get("config")
    if config:
        layers.append(config)

    total_size = sum(l.get("size", 0) for l in layers if l.get("digest"))

    print(f"\n  {Colors.BOLD}Model:{Colors.ENDC}  {Colors.OKCYAN}{args.model}{Colors.ENDC}")
    print(f"  {Colors.BOLD}Files:{Colors.ENDC}  {len(layers)} blobs  •  {Colors.BOLD}Total:{Colors.ENDC} {format_size(total_size)}")
    print(f"\n  {Colors.BOLD}Manifest URL:{Colors.ENDC}")
    print(f"  {Colors.DIM}{manifest_url}{Colors.ENDC}")
    print(f"\n  {Colors.BOLD}Download URLs:{Colors.ENDC}")

    for i, layer in enumerate(layers, 1):
        digest = layer.get("digest")
        size = layer.get("size", 0)
        if digest:
            blob_url = f"https://{DEFAULT_REGISTRY}/v2/{namespace}/{model}/blobs/{digest}"
            out_name = digest.replace(":", "-")
            print(f"  [{i}] {Colors.DIM}{format_size(size):>10}{Colors.ENDC}  {blob_url}")

    print(f"\n  {Colors.BOLD}Curl commands:{Colors.ENDC}")
    print(f"  {Colors.DIM}curl -L \"{manifest_url}\" -o \"manifest\"{Colors.ENDC}")
    for layer in layers:
        digest = layer.get("digest")
        if digest:
            blob_url = f"https://{DEFAULT_REGISTRY}/v2/{namespace}/{model}/blobs/{digest}"
            out_name = digest.replace(":", "-")
            print(f"  {Colors.DIM}curl -L \"{blob_url}\" -o \"{out_name}\"{Colors.ENDC}")

    print(f"\n  {Colors.OKGREEN}Tip:{Colors.ENDC} Run {Colors.BOLD}pullama pull {args.model}{Colors.ENDC} to download & install automatically.\n")


def cmd_pull(args):
    namespace, model, tag = parse_model_name(args.model)
    model_key = f"{namespace}/{model}:{tag}" if namespace != "library" else f"{model}:{tag}"
    models_path = get_models_path(getattr(args, 'modelsPath', None))

    # Check write permission before doing anything
    blobs_dir = os.path.join(models_path, "blobs")
    os.makedirs(blobs_dir, exist_ok=True) if os.path.isdir(models_path) else None
    test_path = models_path if not os.path.isdir(blobs_dir) else blobs_dir
    if not os.access(test_path, os.W_OK):
        print_error(f"No write permission to: {models_path}")
        print(f"\n  Ollama's models folder requires elevated permissions.")
        if platform.system().lower() == "windows":
            print(f"  Re-run this terminal as Administrator, then:")
            print(f"\n    {Colors.BOLD}pullama pull {args.model}{Colors.ENDC}\n")
        else:
            print(f"  Re-run with sudo:\n")
            print(f"    {Colors.BOLD}sudo pullama pull {args.model}{Colors.ENDC}\n")
        sys.exit(1)

    use_aria2 = check_aria2()
    if not use_aria2:
        print_aria2_hint()
        print()

    print(f"\n{Colors.BOLD}Pulling {Colors.OKCYAN}{args.model}{Colors.ENDC}{Colors.BOLD}...{Colors.ENDC}\n")

    # Fetch manifest
    print(f"  Fetching manifest...", end="", flush=True)
    data, raw_manifest, manifest_url = fetch_manifest(namespace, model, tag)
    print(f"\r  Fetching manifest...{' ' * 20}  {Colors.OKGREEN}✔{Colors.ENDC}")

    layers = data.get("layers", [])
    config = data.get("config")
    if config:
        layers.append(config)

    total_size = sum(l.get("size", 0) for l in layers if l.get("digest"))
    blobs_count = len([l for l in layers if l.get("digest")])

    print(f"  {Colors.DIM}{blobs_count} files  •  {format_size(total_size)} total{Colors.ENDC}\n")

    # Save state
    state = load_state()
    update_model_state(state, model_key,
        namespace=namespace,
        model=model,
        tag=tag,
        total_size=total_size,
        manifest_url=manifest_url,
        installed=False,
        started_at=datetime.datetime.now().isoformat(),
        blobs=[{"digest": l["digest"], "size": l.get("size", 0)} for l in layers if l.get("digest")]
    )
    save_state(state)

    # Download blobs
    blobs_dir = os.path.join(models_path, "blobs")
    os.makedirs(blobs_dir, exist_ok=True)

    for i, layer in enumerate(layers, 1):
        digest = layer.get("digest")
        size = layer.get("size", 0)
        if not digest:
            continue

        blob_url = f"https://{DEFAULT_REGISTRY}/v2/{namespace}/{model}/blobs/{digest}"
        out_name = digest.replace(":", "-")
        dest_path = os.path.join(blobs_dir, out_name)
        label = f"[{i}/{blobs_count}] {out_name[:20]}..."

        success = download_blob(blob_url, dest_path, size, label=label, use_aria2=use_aria2)

        if not success:
            print_error(f"Failed to download blob {digest}.")
            print_info(f"Run the same command again to resume: pullama pull {args.model}")
            sys.exit(1)

        # Verify SHA256
        print(f"  {Colors.DIM}Verifying...{Colors.ENDC}", end="", flush=True)
        if not verify_blob(dest_path, digest):
            print(f"\r  {Colors.FAIL}✖ Verification failed for {out_name[:30]}{Colors.ENDC}")
            os.remove(dest_path)
            print_error("Corrupted file removed. Run the command again to re-download.")
            sys.exit(1)
        print(f"\r{' ' * 40}\r", end="")

    # Write manifest (last step — Ollama sees model only after this)
    print(f"\n  Installing into Ollama...", end="", flush=True)
    manifest_dir = os.path.join(models_path, "manifests", DEFAULT_REGISTRY, namespace, model)
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_dest = os.path.join(manifest_dir, tag)
    with open(manifest_dest, "w", encoding="utf-8") as f:
        f.write(raw_manifest)
    print(f"\r  Installing into Ollama...{' ' * 10}  {Colors.OKGREEN}✔{Colors.ENDC}")

    # Update state
    state = load_state()
    update_model_state(state, model_key, installed=True, models_path=models_path)
    save_state(state)

    # Verify Ollama can actually see the model
    seen = verify_ollama_sees_model(model_key)
    if seen:
        print(f"\n{Colors.OKGREEN}{Colors.BOLD}✔ {args.model} is ready!{Colors.ENDC}")
        print(f"  {Colors.DIM}Installed to: {models_path}{Colors.ENDC}")
        print(f"  Run: {Colors.BOLD}ollama run {args.model}{Colors.ENDC}\n")
    elif seen is False:
        # Ollama is running but can't see the model — wrong path
        print(f"\n{Colors.WARNING}⚠ Installed to: {models_path}{Colors.ENDC}")
        print(f"  But Ollama can't see the model — it may use a different models directory.")
        print(f"  Find the correct path with:")
        print(f"    {Colors.BOLD}ls /usr/share/ollama/.ollama/models{Colors.ENDC}  (common on Linux)")
        print(f"  Then re-run with:")
        print(f"    {Colors.BOLD}pullama pull {args.model} --modelsPath <correct-path>{Colors.ENDC}\n")
    else:
        # Ollama not running — can't verify
        print(f"\n{Colors.OKGREEN}{Colors.BOLD}✔ {args.model} is ready!{Colors.ENDC}")
        print(f"  {Colors.DIM}Installed to: {models_path}{Colors.ENDC}")
        print(f"  Run: {Colors.BOLD}ollama run {args.model}{Colors.ENDC}\n")


def cmd_list(args):
    state = load_state()

    if not state:
        print_info("No models tracked yet. Run: pullama pull <model>")
        return

    models_path = get_default_models_path()

    col_model     = 26
    col_size      = 10
    col_dl        = 18
    col_installed = 12

    header = (
        f"  {'Model':<{col_model}}"
        f"{'Size':>{col_size}}"
        f"  {'Downloaded':<{col_dl}}"
        f"{'Installed':<{col_installed}}"
    )
    sep = "  " + "─" * (col_model + col_size + col_dl + col_installed + 2)

    print(f"\n{Colors.BOLD}{header}{Colors.ENDC}")
    print(sep)

    for model_key, info in sorted(state.items()):
        total_size  = info.get("total_size", 0)
        blobs       = info.get("blobs", [])
        namespace   = info.get("namespace", "library")
        model_name  = info.get("model", "")
        tag         = info.get("tag", "latest")

        # Calculate actual downloaded bytes from disk
        blobs_dir = os.path.join(models_path, "blobs")
        downloaded = 0
        for blob in blobs:
            digest = blob.get("digest", "")
            expected = blob.get("size", 0)
            out_name = digest.replace(":", "-")
            fpath = os.path.join(blobs_dir, out_name)
            if os.path.exists(fpath):
                downloaded += min(os.path.getsize(fpath), expected)

        # Check if actually installed (manifest exists on disk)
        manifest_path = os.path.join(models_path, "manifests", DEFAULT_REGISTRY, namespace, model_name, tag)
        is_installed = os.path.exists(manifest_path)

        # Format columns
        if total_size > 0:
            pct = downloaded / total_size * 100
            if downloaded >= total_size:
                dl_str = f"{format_size(downloaded)} {Colors.OKGREEN}✔{Colors.ENDC}"
            else:
                dl_str = f"{format_size(downloaded)} {Colors.WARNING}{pct:.0f}%{Colors.ENDC}"
        else:
            dl_str = Colors.DIM + "?" + Colors.ENDC

        inst_str = f"{Colors.OKGREEN}✔ yes{Colors.ENDC}" if is_installed else f"{Colors.FAIL}✗ no{Colors.ENDC}"
        size_str = format_size(total_size)

        print(
            f"  {Colors.BOLD}{model_key:<{col_model}}{Colors.ENDC}"
            f"{size_str:>{col_size}}"
            f"  {dl_str:<{col_dl + 10}}"  # extra for ANSI codes
            f"{inst_str}"
        )

    print()
    incomplete = [k for k, v in state.items() if not v.get("installed")]
    if incomplete:
        print(f"  {Colors.DIM}Tip: Resume incomplete downloads with: pullama pull <model>{Colors.ENDC}\n")


def cmd_install(args):
    namespace, model, tag = parse_model_name(args.model)
    model_key = f"{namespace}/{model}:{tag}" if namespace != "library" else f"{model}:{tag}"
    blobs_path = os.path.expanduser(args.blobsPath)

    print_info(f"Installing model: {Colors.BOLD}{args.model}{Colors.ENDC}")

    if not os.path.exists(blobs_path):
        print_error(f"Path '{blobs_path}' does not exist.")
        sys.exit(1)

    manifest_source = os.path.join(blobs_path, "manifest")
    if not os.path.isfile(manifest_source):
        print_error(f"No 'manifest' file found in '{blobs_path}'.")
        sys.exit(1)

    models_path = get_models_path(getattr(args, 'modelsPath', None))

    # Check if we need elevated permissions (system path)
    if not models_path.startswith(os.path.expanduser("~")):
        system = platform.system().lower()
        if system != "windows" and os.geteuid() != 0:
            print_error("System models path requires sudo. Re-run with: sudo pullama install ...")
            sys.exit(1)

    # Copy manifest
    manifest_dest_dir = os.path.join(models_path, "manifests", DEFAULT_REGISTRY, namespace, model)
    os.makedirs(manifest_dest_dir, exist_ok=True)
    manifest_dest = os.path.join(manifest_dest_dir, tag)

    if os.path.exists(manifest_dest):
        print_warning("Model already installed. Overwrite? (Y/n) ", end="")
        choice = input("").strip().upper()
        if choice not in ("Y", ""):
            print_error("Installation aborted.")
            sys.exit(1)

    shutil.copy2(manifest_source, manifest_dest)
    print_success("Manifest copied.")

    # Copy blobs
    blobs_dest_dir = os.path.join(models_path, "blobs")
    os.makedirs(blobs_dest_dir, exist_ok=True)

    print_info("Copying blobs (this may take a while)...")
    for filename in os.listdir(blobs_path):
        if filename == "manifest" or os.path.isdir(os.path.join(blobs_path, filename)):
            continue

        file_source = os.path.join(blobs_path, filename)

        if "sha256" not in filename:
            print_info(f"Computing SHA256 for {filename}...")
            hashed_name = "sha256-" + get_file_hash(file_source)
        elif filename.startswith("sha256-"):
            hashed_name = filename
        elif filename.startswith("sha256:"):
            hashed_name = filename.replace("sha256:", "sha256-", 1)
        else:
            hashed_name = filename

        file_dest = os.path.join(blobs_dest_dir, hashed_name)
        print_info(f"  {filename} → {hashed_name}")
        shutil.copy2(file_source, file_dest)

    # Update state
    state = load_state()
    update_model_state(state, model_key, installed=True,
                       namespace=namespace, model=model, tag=tag)
    save_state(state)

    print_success(f"Model installed successfully!")
    print_info(f"Run: {Colors.BOLD}ollama run {args.model}{Colors.ENDC}")


# ─── Help Banner ──────────────────────────────────────────────────────────────

def print_main_help():
    print(f"""
{Colors.BOLD}{Colors.HEADER}╔══════════════════════════════════════╗
║      Pullama 🦙  v{VERSION}            ║
║  Resumable Ollama Model Downloader   ║
╚══════════════════════════════════════╝{Colors.ENDC}

{Colors.DIM}Fix for: ollama pull TLS handshake timeout, disconnects, slow connections{Colors.ENDC}

{Colors.BOLD}USAGE{Colors.ENDC}
  pullama <command> [options]

{Colors.BOLD}COMMANDS{Colors.ENDC}
  {Colors.OKGREEN}pull <model>{Colors.ENDC}        Download & install a model (resume supported)
  {Colors.OKGREEN}get <model>{Colors.ENDC}         Print direct download URLs only
  {Colors.OKGREEN}install{Colors.ENDC} [options]   Install from manually downloaded files
  {Colors.OKGREEN}list{Colors.ENDC}                Show all tracked models and their status

{Colors.BOLD}QUICK START{Colors.ENDC}
  {Colors.OKCYAN}pullama pull tinyllama:latest{Colors.ENDC}
  {Colors.OKCYAN}pullama pull gemma2:2b{Colors.ENDC}
  {Colors.OKCYAN}pullama pull deepseek-r1:7b{Colors.ENDC}

{Colors.BOLD}RESUME{Colors.ENDC}
  Just run the same pull command again — it resumes automatically.

{Colors.BOLD}MODEL NAME FORMAT{Colors.ENDC}
  <model>:<tag>                  Official  → gemma2:2b
  <namespace>/<model>:<tag>      Community → huihui_ai/deepseek-r1:8b
""")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) == 1 or sys.argv[1] in ("-h", "--help"):
        print_main_help()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog="pullama",
        description="Pullama — Resumable Ollama Model Downloader",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    # pull
    parser_pull = subparsers.add_parser("pull", help="Download & install a model")
    parser_pull.add_argument("model", help="Model name, e.g. tinyllama:latest")
    parser_pull.add_argument("--modelsPath", default=None, metavar="PATH",
                             help="Override Ollama models directory")
    parser_pull.set_defaults(func=cmd_pull)

    # get
    parser_get = subparsers.add_parser("get", help="Print direct download URLs")
    parser_get.add_argument("model", help="Model name, e.g. gemma2:2b")
    parser_get.set_defaults(func=cmd_get)

    # install
    parser_install = subparsers.add_parser("install", help="Install from downloaded files")
    parser_install.add_argument("--model", required=True, metavar="MODEL")
    parser_install.add_argument("--blobsPath", required=True, metavar="PATH",
                                help="Folder with manifest + blob files")
    parser_install.add_argument("--modelsPath", default=None, metavar="PATH",
                                help="Override Ollama models directory")
    parser_install.set_defaults(func=cmd_install)

    # list
    parser_list = subparsers.add_parser("list", help="Show all tracked models")
    parser_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print(f"\n\n{Colors.WARNING}⏸  Download paused.{Colors.ENDC}")
        print(f"  Run the same command again to resume.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
