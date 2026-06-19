# Providers & Models

TigerLiteCode supports **3 providers**, talking to each one directly over its
OpenAI-compatible API — no proxy, no format translation. Pick one per session
with `/model` (in the TUI) or `--provider` / `--model` (on the CLI).

## Supported providers

**1. DeepSeek**, **2. OpenCode** (both the *Zen* and *Go* endpoints are
supported), and **3. OpenAI** (plus any OpenAI-compatible endpoint).

| Provider key | Base URL | API key variable |
|--------------|----------|------------------|
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_TIGER_KEY` |
| `opencode-zen` | `https://opencode.ai/zen/v1` | `OPENCODE_API_KEY` |
| `opencode-go` | `https://opencode.ai/zen/go/v1` | `OPENCODE_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |

> OpenCode is one provider with two interchangeable endpoints — `opencode-zen`
> and `opencode-go` — both authenticated by the same `OPENCODE_API_KEY`.

The default is **`deepseek`** with model **`deepseek-v4-pro`**.

> Each provider has its **own** key variable — they are not interchangeable, and
> they are not all DeepSeek keys. Only **DeepSeek** uses the special
> `DEEPSEEK_API_TIGER_KEY` name (not `DEEPSEEK_API_KEY`), on purpose, so
> TigerLiteCode never accidentally picks up an unrelated key already in your
> environment. OpenCode and OpenAI use their standard `OPENCODE_API_KEY` /
> `OPENAI_API_KEY` names.

## Setting keys

**Environment variables (recommended for CI and shared machines):**

```bash
export DEEPSEEK_API_TIGER_KEY="sk-..."
export OPENCODE_API_KEY="oc-..."
export OPENAI_API_KEY="sk-..."
```

**Interactively:** run `/model` in the TUI, choose a provider, and paste the
key. It's stored in `~/.config/tigercli/auth.json` (or `./.tigercli/auth.json`
for a single project).

## Choosing a model

```bash
# CLI
tigerlitecode run --provider opencode-go --model deepseek-v4-pro "refactor this"

# TUI
/model     # then pick provider → model → thinking → effort
```

Recently used models are remembered and offered first the next time you open the
picker.

## OpenAI-compatible endpoints

Any service that speaks the OpenAI chat-completions format works through the
`openai` provider. Point it at your endpoint by setting a custom base URL in
`auth.json`:

```json
{
  "openai": {
    "api_key": "sk-...",
    "base_url": "https://my-gateway.example.com/v1"
  }
}
```

Then select it with `--provider openai --model <your-model>`.

## Thinking & effort

Models that support reasoning can be run in "thinking" mode with an effort
level:

```bash
tigerlitecode run --thinking --effort max "design a migration plan"
```

Effort levels are `low`, `medium`, `high` (default), and `max`. Higher effort
means more reasoning tokens — and more cost — so reach for `max` only on genuinely
hard problems.
