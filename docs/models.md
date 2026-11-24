# Choosing a cleanup model

Cleanup is one short request per dictation (about 2,000 tokens of instructions and context, a few hundred back). What matters is time to the last word and how literally the model follows the rules.

## Recommendation

| Order | Provider | Why |
|---|---|---|
| 1 | **Groq** `qwen/qwen3.8-27b` (free key) | Fastest and most literal in our tests: 687 ms median on real dictations, with paragraph breaks and the fewest restored words. |
| 2 | **Groq** with a second key (`groq2`) | Groq's free tier limits input tokens per minute; several quick dictations in a row can hit it. |
| 3 | **Cerebras** `qwen-3.8-27b` (free key) | The same model, very fast inference; a natural second provider. |
| 4 | **OpenRouter** free models | Works, but free models are often busy; last resort. |
| offline | **Ollama** `qwen3:4b` | Private and free, weaker; shares GPU memory with speech recognition. |

```sh
PP_ORDER="groq groq2 cerebras openrouter"
```

Measured 2026-11-23 on 10 real dictations (GPT-OSS 120B and 20B on Groq were slower, 0.9–1.0 s, and broke sentences more often).

Not usable from Shuush: **OpenCode Zen's free models** (they answer only inside the OpenCode app), and subscription quotas of coding tools (they are meant for those apps, and their models are tuned for agent work, not sub-second rewrites).

## Compare models on your own dictations

```sh
BENCH_SLEEP=14 bash tests/bench_models.sh 10 groq:qwen/qwen3.8-27b groq:openai/gpt-oss-20b zen:<model>
```

It builds the exact request Shuush would send for your last 10 dictations (no screen or clipboard context), sends it to each model, runs each answer through the same gate, and prints latency, rejections and the outputs. `BENCH_SLEEP` paces the calls for Groq's free tier.
