# Pullama 🦙

**Fix for `ollama pull` disconnecting, TLS handshake timeouts, and slow internet.**

If you've seen any of these errors, Pullama is for you:

```
net/http: TLS handshake timeout
context deadline exceeded
download interrupted, starting from scratch
```

Pullama replaces `ollama pull` with a resumable downloader — if your connection drops, just run the same command again and it picks up exactly where it stopped. Works on slow connections, unstable Wi-Fi, mobile data, and VPNs.

---

## Install

```bash
pip install pullama
```

**Boost download speed with aria2 (optional but recommended):**

```bash
# Linux (Debian/Ubuntu)
sudo apt install aria2

# macOS
brew install aria2

# Windows
winget install aria2
```

---

## Usage

### Download & install a model in one command

```bash
pullama pull tinyllama:latest
pullama pull gemma2:2b
pullama pull deepseek-r1:7b
pullama pull huihui_ai/deepseek-r1-abliterated:8b
```

That's it. Pullama downloads the model and installs it into Ollama automatically. Then run it:

```bash
ollama run tinyllama:latest
```

### Resume an interrupted download

Just run the exact same command again:

```bash
pullama pull gemma2:2b
# ... connection drops at 60% ...

pullama pull gemma2:2b
# ℹ Resuming from 1.1 GB / 1.7 GB
```

No flags needed. Pullama detects the partial download and continues from where it stopped — even after days, power outages, or switching networks.

### See what you've downloaded

```bash
pullama list
```

```
  Model                  Size       Downloaded       Installed
  ────────────────────────────────────────────────────────────
  tinyllama:latest        608 MB    608/608 MB  ✔   ✔ yes
  gemma2:2b               1.7 GB    856 MB/1.7 GB   ✗ no
```

### Get direct download URLs (for wget, IDM, or other tools)

```bash
pullama get gemma2:2b
```

Prints direct URLs and ready-to-use curl commands. Useful if you want to download with your own tool.

### Install from manually downloaded files

```bash
pullama install --model gemma2:2b --blobsPath ./downloads
```

---

## How it works

Ollama models are stored as blobs (SHA256-named files). Pullama auto-detects where Ollama keeps its models (handles both user installs at `~/.ollama/models` and system service installs at `/usr/share/ollama/.ollama/models`), downloads each blob directly there, and writes the manifest file last — so Ollama only sees the model once everything is verified complete. If a download is interrupted, the partial blob stays on disk and is resumed via HTTP `Range` requests on the next run.

**With aria2 installed**, each file is split into 4 parallel chunks for significantly faster downloads — especially useful when the server throttles single connections.

**Without aria2**, Pullama uses Python's built-in HTTP client with the same resume support.

---

## Model name format

```
tinyllama:latest                    # official model, explicit tag
gemma2:2b                           # official model
deepseek-r1:7b                      # official model
huihui_ai/deepseek-r1:8b            # community model (namespace/model:tag)
```

---

## Platform support

| Platform | Supported |
|---|---|
| Linux | ✔ |
| macOS | ✔ |
| Windows | ✔ |

---

## Why not just use `ollama pull`?

`ollama pull` streams the entire model in one HTTP connection. On unstable or slow connections this means:

- Any interruption restarts from zero
- TLS handshakes time out on high-latency connections
- No way to resume or track progress across sessions

Pullama solves all three.

---

## License

MIT
