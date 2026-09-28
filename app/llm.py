"""Provider layer: Groq, Cerebras, OpenRouter (all OpenAI-compatible) and Gemini behind one
interface, with automatic fallback to the next configured provider when one is busy or failing."""
import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

from . import config

log = logging.getLogger("uvicorn.error")


class LLMError(Exception):
    pass


_VALID_ESCAPES = set('"\\/bfnrtu')


def _fix_invalid_escapes(text: str) -> str:
    r"""Double backslashes that don't start a valid JSON escape (e.g. LaTeX "\\end" written as
    "\end"), leaving valid escapes such as \\n, \\" and \\\\ untouched."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            if text[i + 1] in _VALID_ESCAPES:
                out.append(text[i : i + 2])
            else:
                out.append("\\\\" + text[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    # Some models wrap JSON in ```json fences even in JSON mode.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        if "escape" not in str(e):
            raise LLMError(f"Model did not return valid JSON: {e}") from e
        try:
            data = json.loads(_fix_invalid_escapes(text))
        except json.JSONDecodeError:
            raise LLMError(f"Model did not return valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("Model returned JSON that is not an object")
    return data


# With fallback available, don't sit on a busy provider for long — move on to the next one.
MAX_RATE_LIMIT_WAIT = 8
BUSY_RETRY_DELAYS = [2, 4]


def _error_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except ValueError:
        msg = None
    return (msg or r.text or "")[:200]


# ---------------------------------------------------------------- OpenAI-compatible


@dataclass
class OpenAICompatible:
    label: str
    base_url: str
    key: str
    model: str
    extra_headers: dict | None = None

    async def __call__(self, system: str, user: str) -> dict:
        models = [m.strip() for m in self.model.split(",") if m.strip()]
        payload = {
            "model": models[0],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if len(models) > 1:
            # OpenRouter model routing: it tries these in order when one is down or rate-limited,
            # and it still counts as a single request.
            payload["models"] = models
        if "gpt-oss" in models[0]:
            # Copying/editing text needs little hidden reasoning; this roughly halves token use.
            payload["reasoning_effort"] = "low"
        headers = {"Authorization": f"Bearer {self.key}", **(self.extra_headers or {})}

        for attempt in range(len(BUSY_RETRY_DELAYS) + 1):
            last = attempt == len(BUSY_RETRY_DELAYS)
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)

            if r.status_code == 200:
                try:
                    body = r.json()
                    content = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError, ValueError):
                    body, content = locals().get("body") or {}, None
                if content:
                    return _parse_json(content)
                # OpenRouter occasionally returns 200 with an error or an empty answer when an
                # upstream model hiccups; treat that like "busy" and retry.
                detail = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else None
                if not last:
                    await asyncio.sleep(BUSY_RETRY_DELAYS[attempt])
                    continue
                raise LLMError(f"{self.label}: empty response" + (f" ({detail[:120]})" if detail else ""))

            if r.status_code == 429 or r.status_code >= 500:
                wait = BUSY_RETRY_DELAYS[min(attempt, len(BUSY_RETRY_DELAYS) - 1)]
                if r.status_code == 429:
                    try:
                        wait = float(r.headers.get("retry-after") or wait)
                    except ValueError:
                        pass
                if not last and wait <= MAX_RATE_LIMIT_WAIT:
                    await asyncio.sleep(wait + 0.3)
                    continue
                kind = "rate limit reached" if r.status_code == 429 else "busy / unavailable"
                raise LLMError(f"{self.label} {kind}")

            # Groq's JSON-mode check sometimes rejects output that is actually fine; try it ourselves.
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            if isinstance(err, dict) and err.get("code") == "json_validate_failed":
                try:
                    return _parse_json(err.get("failed_generation", ""))
                except LLMError:
                    if not last:
                        continue
            raise LLMError(f"{self.label} error {r.status_code}: {_error_message(r)}")
        raise LLMError(f"{self.label} did not return valid JSON")


# ---------------------------------------------------------------- Gemini


async def _gemini(system: str, user: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    for attempt in range(len(BUSY_RETRY_DELAYS) + 1):
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                url,
                headers={"x-goog-api-key": config.GEMINI_API_KEY},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
            )
        if r.status_code not in (429, 500, 503) or attempt == len(BUSY_RETRY_DELAYS):
            break
        await asyncio.sleep(BUSY_RETRY_DELAYS[attempt])
    if r.status_code in (500, 503):
        raise LLMError("Gemini busy / unavailable")
    if r.status_code == 429:
        raise LLMError("Gemini rate limit reached")
    if r.status_code != 200:
        raise LLMError(f"Gemini error {r.status_code}: {_error_message(r)}")
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError("Gemini: unexpected response (possibly blocked)") from e
    return _parse_json(text)


# ---------------------------------------------------------------- registry + fallback

LABELS = {"groq": "Groq", "cerebras": "Cerebras", "gemini": "Gemini", "openrouter": "OpenRouter"}


def _providers() -> dict:
    """Configured providers only (those with an API key), in the fallback order."""
    all_ = {
        "groq": config.GROQ_API_KEY
        and OpenAICompatible("Groq", "https://api.groq.com/openai/v1", config.GROQ_API_KEY, config.GROQ_MODEL),
        "cerebras": config.CEREBRAS_API_KEY
        and OpenAICompatible("Cerebras", "https://api.cerebras.ai/v1", config.CEREBRAS_API_KEY, config.CEREBRAS_MODEL),
        "gemini": config.GEMINI_API_KEY and _gemini,
        "openrouter": config.OPENROUTER_API_KEY
        and OpenAICompatible(
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            config.OPENROUTER_API_KEY,
            config.OPENROUTER_MODEL,
            {"X-Title": "Resume Editor"},
        ),
    }
    order = [p for p in config.PROVIDER_ORDER if p in all_] + [p for p in all_ if p not in config.PROVIDER_ORDER]
    return {name: all_[name] for name in order if all_[name]}


def available_providers() -> list[str]:
    return list(_providers())


async def complete_json_with_provider(system: str, user: str, provider: str | None = None) -> tuple[dict, str]:
    """Try the preferred provider first, then every other configured one. Returns (data, provider)."""
    providers = _providers()
    if not providers:
        raise LLMError("No AI provider is configured. Add an API key to .env and restart the server.")
    order = list(providers)
    if provider and provider in providers:
        order.remove(provider)
        order.insert(0, provider)

    failures = []
    for name in order:
        try:
            return await providers[name](system, user), name
        except (LLMError, httpx.HTTPError) as e:
            msg = str(e) or type(e).__name__
            log.warning("LLM provider %s failed: %s", name, msg)
            failures.append(msg if isinstance(e, LLMError) else f"{LABELS[name]}: {msg}")

    if len(failures) == 1:
        raise LLMError(f"{failures[0]}. Please try again in a minute.")
    raise LLMError("All AI providers failed — " + "; ".join(failures) + ". Please try again in a minute.")


async def complete_json(system: str, user: str, provider: str | None = None) -> dict:
    data, _ = await complete_json_with_provider(system, user, provider)
    return data
