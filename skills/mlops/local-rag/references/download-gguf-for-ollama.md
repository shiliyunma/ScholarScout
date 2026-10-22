# Download GGUF Models via hf-mirror for Ollama

Mirrors for Qwen2.5 GGUF models when registry.ollama.ai is blocked in China.

## hf-mirror download URLs (verified working from China)

These are direct GGUF file URLs on hf-mirror.com. Append `?download=true` for curl to follow redirect correctly.

### Qwen2.5-7B-Instruct-GGUF

| Quant | File | Size | curl Command |
|-------|------|------|-------------|
| Q4_K_M | qwen2.5-7b-instruct-q4_k_m.gguf | ~4.5GB | `curl -L -o qwen2.5-7b-instruct-q4_k_m.gguf "https://hf-mirror.com/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf?download=true"` |
| Q5_K_M | qwen2.5-7b-instruct-q5_k_m.gguf | ~5.2GB | Same URL pattern, replace filename |
| Q8_0 | qwen2.5-7b-instruct-q8_0.gguf | ~7.7GB | Same URL pattern, replace filename |

### Qwen2.5-14B-Instruct-GGUF

| Quant | Size | curl Command |
|-------|------|-------------|
| Q4_K_M | ~9GB | `curl -L -o qwen2.5-14b-instruct-q4_k_m.gguf "https://hf-mirror.com/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m.gguf?download=true"` |
| Q5_K_M | ~11GB | Same URL pattern, replace filename |

### Qwen2.5-1.5B-Instruct-GGUF

| Quant | Size | curl Command |
|-------|------|-------------|
| Q4_K_M | ~1GB | `curl -L -o qwen2.5-1.5b-instruct-q4_k_m.gguf "https://hf-mirror.com/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true"` |
| Q8_0 | ~1.6GB | Same URL pattern, replace filename |

### Other models

| Model | hf-mirror URL |
|-------|---------------|
| DeepSeek-R1-Distill-Qwen-7B | https://hf-mirror.com/deepseek-ai/DeepSeek-R1-Distill-Qwen-7B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf?download=true |
| Qwen2.5-Coder-7B | https://hf-mirror.com/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/qwen2.5-coder-7b-instruct-q4_k_m.gguf?download=true |

## Modelfile Template

```dockerfile
FROM ./qwen2.5-7b-instruct-q4_k_m.gguf

TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}<|im_start|>user
{{ .Prompt }}<|im_end|>
<|im_start|>assistant
"""

PARAMETER temperature 0.7
PARAMETER top_p 0.8
PARAMETER stop "<|im_start|>"
PARAMETER stop "<|im_end|>"
```

Then import:
```bash
ollama create qwen2.5-7b -f Modelfile
ollama run qwen2.5-7b
```

## ModelScope alternative (may be faster inside China)

Some models are also available on ModelScope:
```bash
# Qwen2.5-7B from ModelScope
curl -L -o qwen2.5-7b-instruct-q4_k_m.gguf \
  "https://modelscope.cn/api/v1/models/qwen/Qwen2.5-7B-Instruct-GGUF/repo?Revision=master&FilePath=qwen2.5-7b-instruct-q4_k_m.gguf"
```

## Verification

Check the file downloaded correctly:
```bash
ls -sh qwen2.5-7b-instruct-q4_k_m.gguf
# Should show ~4.5G for Q4_K_M variant
```

To verify sha256 (if available from the model page):
```bash
# Windows (PowerShell)
Get-FileHash qwen2.5-7b-instruct-q4_k_m.gguf -Algorithm SHA256

# Linux / git-bash
sha256sum qwen2.5-7b-instruct-q4_k_m.gguf
```
