# Competition YAML Config Troubleshooting

## Indentation mismatch (ScannerError)

### Symptom
```
ScannerError: mapping values are not allowed here
  in "<unicode string>", line 7, column 14:
          api_key: sk-5df...2c1a
                 ^
```

### Root cause
Keys at the same YAML level have **different indentation**. Example:

```yaml
agent:
  model: deepseek-chat         # 2 spaces
  api_base: https://...        # 2 spaces
      api_key: sk-xxx          # 6 spaces ← WRONG
```

The `api_key` with 6 spaces makes YAML treat it as a sub-key of `api_base` instead of a sibling of `model`. Since `api_base` already has a scalar value (`https://...`), YAML rejects the attempt to start a mapping under it.

### Fix
Align all sibling keys to the same indentation:

```yaml
agent:
  model: deepseek-chat         # 2 spaces
  api_base: https://...        # 2 spaces
  api_key: sk-xxx              # 2 spaces ← CORRECT
```

### How it happens
- Copy-pasting from a partially hidden/redacted config
- Mixed editor tab widths (tabs vs spaces)
- Manual editing in Notepad or other non-YAML-aware editors

## Authentication failures

### DeepSeek
```bash
# List available models — verify your model name exists
curl -s https://api.deepseek.com/v1/models \
  -H "Authorization: Bearer *** --connect-timeout 10 --max-time 15
# Available: deepseek-v4-flash, deepseek-v4-pro
# ⚠️ 'deepseek-chat' no longer exists — using it causes silent hangs
```

```txt
Error code: 401
{'error': {'message': 'Authentication Fails, Your api key: ****xxx is invalid', ...}}
```
**Fix**: Replace with a valid DeepSeek API key from platform.deepseek.com.

### OpenAI
```
Error code: 401
{'error': {'message': 'Incorrect API key provided: ...'}}
```
**Fix**: Replace with a valid OpenAI API key.

### General pattern
If the error message shows `****xxx`, the system received the key but it was rejected by the provider. Check:
1. The key hasn't expired
2. The key has sufficient credits/quota
3. The `api_base` URL matches the provider's endpoint

## YAML config checklist

- [ ] All sibling keys use the same indentation (spaces only, no tabs)
- [ ] `api_key` is the full real key (not `sk-xxx...yyy` with ellipsis)
- [ ] `api_key` has no trailing comment (`# comment` gets included in the value)
- [ ] `api_base` matches the provider (e.g. `https://api.deepseek.com`, `https://api.openai.com/v1`)
- [ ] Config file is `*.local.yaml` (gitignored) not `*.example.yaml`
- [ ] Config file passes `python -c "import yaml; yaml.safe_load(open('path/to/config.yaml'))"`
