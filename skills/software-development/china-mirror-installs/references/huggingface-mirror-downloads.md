# Downloading HuggingFace models on a China network

Depth for the "Model weights" row of the main table. **ModelScope is not the only option, and it is not
always the right one**: when the checkpoint only exists on the Hub, mirror the Hub itself.

## The asymmetry that decides the whole approach

On a China network the Hub behaves in two different ways depending on which endpoint you touch. Probe
both before concluding anything:

| Endpoint | Typical result | What it is good for |
|---|---|---|
| `huggingface.co/api/models/<repo>?blobs=true` | **reachable** | listing files, sizes, tags, likes — everything except the bytes |
| `huggingface.co/<repo>/resolve/main/<file>` | **connect timeout** (≈21 s to port 443) | nothing |
| `hf-mirror.com/<repo>/resolve/main/<file>` | **200, full speed** | the actual download |

Consequence: **you can always enumerate a repo, and you can rarely fetch it, from the same host.** So
start by getting the authoritative file list from the API, then fetch exactly those paths from the
mirror. Do not guess filenames.

```bash
# authoritative file list + sizes (this call works)
curl -s "https://huggingface.co/api/models/<owner>/<repo>?blobs=true" \
  | python -c "import sys,json;[print(round((s['size'] or 0)/1048576,2),'MB',s['rfilename']) for s in json.load(sys.stdin)['siblings']]"

# same file from the mirror -> real bytes
curl -sSL -o "D:/tmp/config.json" "https://hf-mirror.com/<owner>/<repo>/resolve/main/config.json"
```

## `huggingface_hub.snapshot_download()` will fail — do not fight it

Setting `HF_ENDPOINT=https://hf-mirror.com` is **not enough**. `huggingface_hub` 1.x validates that the
response looks like the real Hub and raises
`FileMetadataError: Distant resource does not seem to be on huggingface.co` when the mirror's response
headers do not match its expectations — the mirror is fine, the client refuses it anyway. The same
wrapper surfaces as `LocalEntryNotFoundError` at the end of the traceback, which misreads as "no network".

**Do not spend time debugging the client.** Drop `snapshot_download`/`hf_hub_download` for mirrored
downloads and fetch the paths yourself: `urllib.request` (or curl) against
`hf-mirror.com/<repo>/resolve/main/<rfilename>`, writing straight to the destination directory. This is
the reliable form and it is ~10 lines.

## Non-flat repository layouts

The `rfilename` values are **relative paths, not filenames** — many repos put the encoder and tokenizer in
subdirectories (`encoder/config.json`, `tokenizer/tokenizer.json`). `os.makedirs(os.path.dirname(out))`
per file, or the small config files fail while the huge weights succeed and you only notice at load time.

Also filter deliberately rather than mirroring everything: `allow_patterns`-style selection is what keeps
a weights-only checkout small. Mirroring the whole repo pulls formats you will never load
(`*.onnx`, `*.gguf`, `*.msgpack` duplicates of the same weights).

## Order of operations

1. Resolve the destination paths and **put them on a drive with room** (a 600 MB–2 GB checkpoint plus
   several quantized variants adds up fast). Set `HF_HOME` to that drive too if a client library is doing
   any caching.
2. Download the **small files first** (configs, tokenizer, index) and the big `*.safetensors` last. A
   failure then costs seconds, and you learn the layout is right before paying for the weights.
3. **Report the measured throughput and the byte total**, not "done": e.g. *646.8 MB in 28 s (21.6 MB/s)*.
   It is the number that tells the user whether a larger checkpoint is feasible.
4. Verify by walking the directory and summing sizes, then compare against the API's `size` fields. Report
   files + MB.
5. For reuse, set the offline flags so later runs never touch the network:
   `HF_HUB_OFFLINE=1` (and `TRANSFORMERS_OFFLINE=1`) once the files are local.

## Local environment for running the model

- **Create the venv on a pure-ASCII path.** If the project directory contains non-ASCII characters, a
  venv or a source checkout *inside* it can break interpreter startup (`site.py` reads a `.pth` containing
  the non-ASCII path with the wrong codec). Keep venv, model cache, and download scratch on `D:/<ascii>`
  and only the *documents* inside the non-ASCII project tree.
- **`--system-site-packages` to reuse weights you already have.** A machine that already carries a CUDA
  `torch` should not download another 2 GB: `py -3.12 -m venv --system-site-packages "D:/proj/venv"`, then
  confirm the inherited stack before installing anything (`python -c "import torch;print(torch.__version__)"`).
  Install only the new package; verify it did **not** churn `torch`/`transformers` versions.
- **`USE_TF=0`** (and often `USE_FLAX=0`) before importing `transformers`: TensorFlow's abseil runtime can
  deadlock model construction, which presents as a load that hangs rather than an error.
- A first load that takes ~2 minutes is normal (import + weight map + device transfer). Distinguish it
  from a hang by measuring, and tell the user the expected first-call vs steady-state latency
  separately — a 2.3 s first inference next to 170 ms steady state is expected, not a bug.

## Windows path traps specific to this workflow

- **Native tools do not understand MSYS paths.** `py -3.12 -m venv /d/proj/venv` fails with
  *No such file or directory*; write `D:/proj/venv`. Same class of error as the curl `-o /dev/null` trap in
  the main skill — any argument that is a *path*, handed to a native binary, must be a native path.
- Use forward slashes in native paths (`D:/x/y`) — they work in both shells and avoid escape mangling.
- Write download scratch files to a real directory on the target drive, never to `/tmp`, and never to
  `/dev/null`.
