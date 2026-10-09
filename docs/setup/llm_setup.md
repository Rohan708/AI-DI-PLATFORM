# Setup: an LLM for rule proposals (Gemini)

AI is used only to **propose** business rules; checks never call it. Without a key, everything else works.

## 1. Get a key
1. Open [Google AI Studio → API keys](https://aistudio.google.com/apikey) and sign in.
2. Click **Create API key** and copy it.

## 2. Put it in `.env` (never in code or chat)
```
AIDE_LLM_PROVIDER=gemini
AIDE_GEMINI_API_KEY=<your key>
# optional, empty = gemini-2.5-flash
AIDE_LLM_MODEL=
```
`.env` is git-ignored, and the key is read as a secret (it never appears in logs or error messages).

## 3. Try it on the lab
```
python -m ai_data_engineer rules propose shopco3
python -m ai_data_engineer rules list shopco3
python -m ai_data_engineer rule show <id>
python -m ai_data_engineer rule approve <id> --note "makes sense"
python -m ai_data_engineer rules check shopco3
python -m ai_data_engineer lab score --data-source shopco3
```

## What is sent
Only structure and statistics: names, types, keys, row counts, null rates, distinct counts, numeric/date min-max, and known relationships. **No row values.** See [`docs/design/ai_rules.md`](../design/ai_rules.md) §5.

## Troubleshooting
| Message | Meaning |
|---|---|
| `AI features are off` | `AIDE_LLM_PROVIDER` isn't set (or is `none`) |
| `AIDE_GEMINI_API_KEY is not set` | provider is gemini but the key is missing from `.env` |
| `Gemini returned HTTP 400/403` | invalid key, or the API isn't enabled for it |
| `Gemini returned HTTP 404` | the model name in `AIDE_LLM_MODEL` doesn't exist; leave it empty or use a current model |
| `Gemini returned HTTP 429` | free-tier rate limit; wait a minute and retry |
