# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python CLI (`consulta_nacionalidade.py`) that checks the status of a Portuguese nationality case on the IRN "Meu Registo" portal. It drives a Camoufox browser, gets past the site's hCaptcha with `hcaptcha-challenger` (backed by Gemini), and sends the query from inside the page. Code identifiers, comments, user-facing messages and the README are in Brazilian Portuguese. Keep new code and messages in the same language and style.

## Commands

The project uses `uv` with Python 3.13 (`.python-version`; `pyproject.toml` allows `>=3.12,<3.15`).

```bash
uv sync                                          # install dependencies
uv run camoufox fetch                            # download the Camoufox browser (first run does this too)
uv run consulta_nacionalidade.py                 # query using SENHA_ACESSO from .env
uv run consulta_nacionalidade.py 1234-5678-9012  # query a specific access code (dashes optional)
uv run consulta_nacionalidade.py --headless      # --no-headless overrides HEADLESS=true
```

The project has no test suite, linter config or build step. Every real run hits the live government site (it has a WAF, so don't run queries back to back) and uses Gemini API quota.

Exit codes: `0` success, `1` query or browser error, `2` config or argument error (including a model Gemini rejects), `130` interrupted.

## Configuration

Settings live in `.env` (template in `.env.example`); values already set in the environment win over `.env`. Required: `GEMINI_API_KEY`. Optional: `SENHA_ACESSO`, `HEADLESS`, `GEMINI_MODELOS` (comma-separated fallback chain, default `gemini-3.8-flash,gemini-3.1-flash-lite`). `main()` copies the first model into the hcaptcha-challenger per-task model vars (`CHALLENGE_CLASSIFIER_MODEL` etc.) unless they're already set. The `gemini-2.5-*` defaults that ship with hcaptcha-challenger no longer accept new API keys.

## Architecture (flow in `main()` → `executar()`)

1. **Preflight**: `verificar_gemini` sends a tiny request to every configured model before the browser opens, so a rejected model fails fast (exit 2). A 429 only prints a warning, and 5xx errors are ignored.
2. **Browser**: `AsyncCamoufox` with a persistent profile in `tmp/camoufox` (keeps cookies between runs), `os=` set to the host OS so the faked fingerprint matches the real fonts and screen scale. `AgentV` must be created **before** `page.goto` because it listens to hCaptcha API responses.
3. **Dynamic action discovery** (`descobrir_acao`): the OutSystems action URL and the `moduleVersion`/`apiVersion` are read live from `/Pedidos/moduleservices/moduleversioninfo`, `moduleinfo` and the screen's `.mvc.js` (regex on `callServerAction("GetPedidoData_New", ...)`). Never hardcode them: they change every time the site is republished, and a stale version comes back as `hasApiVersionChanged`.
4. **hCaptcha** (`resolver_hcaptcha`): clicks the checkbox, then races two tasks: polling the `h-captcha-response` DOM field (covers instant approval with no challenge, which the agent alone would wait on forever, and manual solving in the visible window) and `agente.wait_for_challenge()`. Camoufox runs `page.evaluate` in an isolated world, so `window.hcaptcha` isn't reachable and the token is read from the DOM.
5. **Query** (`consultar`): the POST to `ActionGetPedidoData_New` runs through `fetch` inside the page (`POST_NA_PAGINA`), so it carries the browser's cookies. `X-CSRFToken` comes from the `crf` value in the `nr2*` cookie, falling back to the OutSystems anonymous token `CSRF_ANONIMO`. `Pedidos.NotAllowed` means the hCaptcha token was invalid or expired.
6. **Output**: the raw `data` goes to `saida/<senha>_<YYYYmmdd-HHMMSS>.json`. Before writing, the latest previous file for the same access code is loaded and `imprimir_comparativo` prints a `rich` table (current vs previous). `campos_alterados` diffs the whole flattened response, including fields the table doesn't show. OutSystems lists arrive as `{"List": [...]}` (see `itens`).

### Monkeypatches and import-order constraints

- `matplotlib.use("Agg")` must run **before** `hcaptcha_challenger` is imported. The macOS backend renders at 2x on Retina screens and breaks the package with "cannot reshape array".
- `GeminiProvider.generate_with_images` is replaced by `_gerar_trocando_de_modelo`. It wraps each call in a `TEMPO_MAXIMO_CHAMADA` timeout and, on a tenacity `RetryError` from a Gemini `APIError` or on a timeout, moves that provider to the next model in the module-level `modelos_gemini` list (filled in by `main()`).
- `AgentConfig(DISABLE_BEZIER_TRAJECTORY=True)`: Camoufox's `humanize` already handles mouse movement.

## Git-ignored paths

`.env`, `tmp/` (browser profile, hcaptcha-challenger logs and screenshots), `saida/` (query history used for comparisons), `.venv/`.
