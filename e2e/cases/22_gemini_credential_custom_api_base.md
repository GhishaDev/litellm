# Case 22 — Gemini provider with custom `api_base`

## Goal

End-to-end validation that the UI change in PR #24 (exposing `api_base`
on the Google AI Studio credential form) reaches the gemini provider
runtime correctly. Specifically:

1. A deployment with `litellm_params.model = gemini/...` plus a custom
   `api_base` pointing at a Gemini-compatible gateway resolves to the
   gemini provider code path (no ADC, no Vertex auth).
2. The outbound request hits `{api_base}/models/{model}:generateContent`
   with an `x-goog-api-key` header — matching what a self-hosted gateway
   like Raven Router or anispark's ai-router exposes.
3. The response surface (status, `x-litellm-model-api-base`,
   `x-litellm-model-group`) reflects the custom api_base, not Google's
   default `generativelanguage.googleapis.com`.

This case guards the runtime half of PR #24. The UI half is covered by
`tests/test_litellm/proxy/public_endpoints/test_public_endpoints.py::test_google_ai_studio_provider_fields_expose_api_base`.

## Background

Before this fix, the only LiteLLM UI options for "URL + API key Gemini"
were:

- **OpenAI credential** mislabeled (admin confusion, wrong logo/group in
  the dashboard).
- Raw admin API PATCH of `litellm_params.api_base` (no UI path).

The runtime gemini provider already supported custom `api_base` via
`litellm/llms/vertex_ai/vertex_llm_base.py:415` —
`_check_custom_proxy` builds `{api_base}/models/{model}:{endpoint}` and
attaches `x-goog-api-key: {gemini_api_key}` (line 484). The
`_ensure_access_token_async` ADC path is skipped because
`custom_llm_provider == "gemini"` (line 714). The UI just needed to
expose the `api_base` field — see `provider_create_fields.json` →
`Google_AI_Studio`.

## Preconditions

- `e2e/.env` has `GEMINI_API_KEY` and `GEMINI_API_BASE` set, pointing at
  a Gemini-compatible gateway. Examples:
  - `https://generativelanguage.googleapis.com/v1beta` (canonical Google
    AI Studio; works with an `AIza...` key)
  - `https://ai-router-hk.anispark.ai/v1beta` (anispark / Raven Router
    with their own sk-style key)
  - any self-hosted proxy that serves
    `POST /v1beta/models/{model}:generateContent` with
    `x-goog-api-key` auth.
- Optionally `MODEL_GEMINI` to override the upstream model id (default
  `gemini/gemini-3.1-pro-preview`).
- `e2e/tools/proxy status` reports `ready` against an image built AFTER
  this PR's branch (`e2e/tools/proxy build` if unsure — the rendered
  config gains a `gemini-custom-base` deployment when `GEMINI_API_KEY`
  is set).
- Without `GEMINI_API_KEY` the case exits 77 (SKIP) — the proxy doesn't
  render the deployment, so there's nothing to test.

## Steps

```bash
bash e2e/cases/data/22_gemini_credential_custom_api_base.sh
echo "exit=$?"
```

The fixture issues a single chat completion call against the
`gemini-custom-base` deployment and inspects the response headers:

- **C1** `POST /v1/chat/completions` model=`gemini-custom-base` →
  expects HTTP 200, body has `choices`, header
  `x-litellm-model-api-base` equals the value of `GEMINI_API_BASE` from
  `e2e/.env`, header `x-litellm-model-group == gemini-custom-base`.

## Expected — GREEN

```
PASS C1: HTTP 200, x-litellm-model-api-base=https://<gateway>/v1beta
PASS C1: x-litellm-model-group=gemini-custom-base
PASS C1: body has 'choices' array
```

## Failure modes

| Symptom | Likely cause |
|---|---|
| `SKIP: GEMINI_API_KEY not set` | Add `GEMINI_API_KEY` and `GEMINI_API_BASE` to `e2e/.env`, then `e2e/tools/proxy restart`. |
| `FAIL C1: HTTP 500 ... DefaultCredentialsError` | The deployment routed to `vertex_ai`/`vertex_ai_beta` instead of `gemini`. Verify `MODEL_GEMINI` does not have `vertex_ai/` prefix and the rendered config shows `model: gemini/...` for `gemini-custom-base`. |
| `FAIL C1: HTTP 200 but body is HTML (<!doctype html>)` | `api_base` is the gateway's UI root, not the API path. Add `/v1beta` (or the equivalent for your gateway) to `GEMINI_API_BASE`. |
| `FAIL C1: x-litellm-model-api-base != GEMINI_API_BASE` | LiteLLM is ignoring the custom api_base. Confirm the deployment block in the rendered config has `api_base: os.environ/GEMINI_API_BASE` and not the default Google endpoint. |
| `FAIL C1: 401 / 403` from upstream | `GEMINI_API_KEY` is wrong for this gateway, or the gateway expects a different auth header (some non-Google gateways want `Authorization: Bearer` instead of `x-goog-api-key`). Confirm with a direct curl to `{api_base}/models/{model}:generateContent`. |

## Cross-reference

- PR #24 (`fix(ui): expose api_base on Google AI Studio credential form`)
- `litellm/llms/vertex_ai/vertex_llm_base.py:415` —
  `_check_custom_proxy` URL construction for gemini provider
- `litellm/llms/vertex_ai/vertex_llm_base.py:714` — gemini provider
  skipping ADC
- `tests/test_litellm/proxy/public_endpoints/test_public_endpoints.py::test_google_ai_studio_provider_fields_expose_api_base` — UI field metadata assertion
