# Download Embedding Models from hf-mirror.com

When HuggingFace is blocked, use hf-mirror.com (verified working in China).
Download via curl and point the code to the local path.

## bge-small-zh-v1.5 (~91MB, recommended for CPU)

```bash
BASE="https://hf-mirror.com/BAAI/bge-small-zh-v1.5/resolve/main"
mkdir -p models/bge-small-zh-v1.5/1_Pooling
cd models/bge-small-zh-v1.5

# Config + tokenizer files (~540KB total)
for f in config.json config_sentence_transformers.json modules.json \
         sentence_bert_config.json special_tokens_map.json tokenizer.json \
         tokenizer_config.json vocab.txt; do
  curl -sOL "$BASE/$f"
done
curl -sL "$BASE/1_Pooling/config.json" -o 1_Pooling/config.json

# Model weights (~91MB)
curl -L -o model.safetensors "$BASE/model.safetensors"
```

## bge-base-zh-v1.5 (~400MB, more accurate, needs more RAM)

```bash
BASE="https://hf-mirror.com/BAAI/bge-base-zh-v1.5/resolve/main"
mkdir -p models/bge-base-zh-v1.5/1_Pooling
cd models/bge-base-zh-v1.5

for f in config.json config_sentence_transformers.json modules.json \
         sentence_bert_config.json special_tokens_map.json tokenizer.json \
         tokenizer_config.json vocab.txt; do
  curl -sOL "$BASE/$f"
done
curl -sL "$BASE/1_Pooling/config.json" -o 1_Pooling/config.json

curl -L -o model.safetensors "$BASE/model.safetensors"
```

## Verify download

```bash
ls -sh models/bge-small-zh-v1.5/
# Should see: model.safetensors (~91MB), config.json, tokenizer.json, etc.
```
