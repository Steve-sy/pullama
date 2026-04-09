#!/usr/bin/env python3
"""
Export and simplified install commands for Pullama.
"""

import argparse
import sys
import os
import json
import shutil


def cmd_export(args):
    """Export an installed Ollama model to a shareable folder."""
    # Import here to avoid circular imports
    from .__main__ import (
        Colors, print_success, print_info, print_error, format_size,
        parse_model_name, get_models_path, DEFAULT_REGISTRY, STATE_FILE,
        load_state, save_state, update_model_state
    )

    model_input = args.model
    namespace, model, tag = parse_model_name(model_input)
    model_key = f"{namespace}/{model}:{tag}" if namespace != "library" else f"{model}:{tag}"

    # Resolve Ollama models path
    models_path = get_models_path()

    # Locate manifest on disk
    manifest_dir = os.path.join(models_path, "manifests", DEFAULT_REGISTRY, namespace, model)
    manifest_path = os.path.join(manifest_dir, tag)

    if not os.path.isfile(manifest_path):
        print_error(f"Model '{model_input}' not found in Ollama.")
        print(f"  Checked: {manifest_path}")
        print(f"  Run: pullama pull {model_input} to download it first.")
        sys.exit(1)

    # Read manifest to discover required blobs
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception as e:
        print_error(f"Failed to read manifest: {e}")
        sys.exit(1)

    layers = manifest_data.get("layers", [])
    config = manifest_data.get("config")
    if config:
        layers.append(config)

    # Extract digests
    digests = [layer["digest"] for layer in layers if layer.get("digest")]

    # Determine output directory
    if args.output:
        export_dir = os.path.expanduser(args.output)
    else:
        safe_name = model_key.replace(":", "-")
        export_dir = os.path.join(os.getcwd(), f"{safe_name}-export")

    if os.path.exists(export_dir):
        if not os.path.isdir(export_dir):
            print_error(f"Export path exists and is not a directory: {export_dir}")
            sys.exit(1)
    else:
        os.makedirs(export_dir, exist_ok=True)

    print(f"\n{Colors.BOLD}Exporting {Colors.OKCYAN}{model_input}{Colors.ENDC}...\n")
    print(f"  Source: {models_path}")
    print(f"  Target: {export_dir}")
    print()

    # Copy manifest as 'manifest'
    manifest_dest = os.path.join(export_dir, "manifest")
    try:
        shutil.copy2(manifest_path, manifest_dest)
        print(f"  {Colors.DIM}✔ manifest{Colors.ENDC}")
    except Exception as e:
        print_error(f"Failed to copy manifest: {e}")
        sys.exit(1)

    # Copy blobs
    blobs_src_dir = os.path.join(models_path, "blobs")
    missing = []
    for digest in digests:
        blob_name = digest.replace(":", "-")  # storage format: sha256-abc...
        src = os.path.join(blobs_src_dir, blob_name)
        dst = os.path.join(export_dir, blob_name)

        if not os.path.exists(src):
            missing.append(blob_name)
            continue

        try:
            shutil.copy2(src, dst)
            print(f"  {Colors.DIM}✔ {blob_name[:30]}...{Colors.ENDC}")
        except Exception as e:
            print_error(f"Failed to copy {blob_name}: {e}")
            sys.exit(1)

    if missing:
        print_error(f"Missing blobs: {', '.join(missing)}")
        print("The model installation is incomplete. Re-run 'pullama pull' first.")
        sys.exit(1)

    print(f"\n{Colors.OKGREEN}{Colors.BOLD}✔ Exported successfully!{Colors.ENDC}")
    print(f"\n  Share the folder: {Colors.BOLD}{export_dir}{Colors.ENDC}")
    print(f"\n  On another machine run:")
    print(f"    {Colors.BOLD}pullama install {export_dir}{Colors.ENDC}\n")


def cmd_install(args):
    """Install from an exported model folder (simplified interface)."""
    # Backward compat: handle old --blobsPath flag
    from .__main__ import (
        Colors, print_success, print_info, print_error, print_warning,
        get_file_hash, get_models_path, parse_model_name,
        DEFAULT_REGISTRY, STATE_FILE, load_state, save_state, update_model_state
    )

    blobs_path = getattr(args, 'blobsPath', None)
    folder = getattr(args, 'folder', None)

    if not blobs_path and not folder:
        print_error("Missing folder argument. Usage: pullama install <folder>")
        print(f"  Example: {Colors.BOLD}pullama install ./qwen2.5-7b-export/{Colors.ENDC}")
        sys.exit(1)

    blobs_path = blobs_path or folder
    blobs_path = os.path.expanduser(blobs_path)

    if not os.path.exists(blobs_path):
        print_error(f"Folder does not exist: {blobs_path}")
        sys.exit(1)

    manifest_source = os.path.join(blobs_path, "manifest")
    if not os.path.isfile(manifest_source):
        print_error(f"No 'manifest' file found in: {blobs_path}")
        sys.exit(1)

    # Read manifest to parse model metadata
    try:
        with open(manifest_source, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception as e:
        print_error(f"Failed to read manifest: {e}")
        sys.exit(1)

    # Derive namespace/model/tag from manifest URL or fallback to args/inference
    manifest_url = manifest_data.get("config", {}).get("digest")  # not helpful; better: look at URL in manifest?
    # Actually we can't reliably get namespace/model/tag from manifest alone without the URL.
    # Better: if --model is given use it; else try infer from folder name.
    if args.model:
        namespace, model, tag = parse_model_name(args.model)
    else:
        # Infer from folder basename: e.g., "qwen2.5-7b-export" → "qwen2.5:7b"
        base = os.path.basename(os.path.abspath(blobs_path.rstrip("/")))
        if base.endswith("-export"):
            base = base[:-7]  # strip "-export" (7 chars)
        # Try to parse as: model:tag format
        if ":" in base:
            namespace, model, tag = parse_model_name(base)
        else:
            # Default to library namespace and 'latest' tag if none
            namespace, model, tag = "library", base, "latest"

    models_path = get_models_path(getattr(args, 'modelsPath', None))

    # Check permissions for system path
    if not models_path.startswith(os.path.expanduser("~")):
        import platform
        system = platform.system().lower()
        if system != "windows" and os.geteuid() != 0:
            print_error("System models path requires elevated permissions.")
            actual_path = shutil.which("pullama") or os.path.abspath(sys.argv[0])
            symlink_exists = os.path.exists("/usr/local/bin/pullama")
            if not symlink_exists:
                print(f"\n  One-time setup — make pullama available to sudo:\n")
                print(f"    {Colors.BOLD}sudo ln -s {actual_path} /usr/local/bin/pullama{Colors.ENDC}\n")
                print(f"  Then re-run:\n")
            else:
                print(f"  Re-run with sudo:\n")
            print(f"    {Colors.BOLD}sudo pullama install {blobs_path}{Colors.ENDC}\n")
            sys.exit(1)

    print_info(f"Installing model: {Colors.BOLD}{namespace}/{model}:{tag}{Colors.ENDC}")

    # Copy manifest to Ollama directory
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

        # Normalize blob name to sha256-<hash> format (same logic as original install)
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
    model_key = f"{namespace}/{model}:{tag}" if namespace != "library" else f"{model}:{tag}"
    update_model_state(state, model_key, installed=True,
                       namespace=namespace, model=model, tag=tag)
    save_state(state)

    print_success(f"Model installed successfully!")
    print_info(f"Run: {Colors.BOLD}ollama run {args.model or f'{namespace}/{model}:{tag}'}{Colors.ENDC}")
