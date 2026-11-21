"""Thin wrappers around model APIs. A model spec looks like 'openai:gpt-4o-mini'."""
import os
import time
from dataclasses import dataclass

_clients = {}


@dataclass
class Reply:
    text: str
    latency_ms: float
    in_tokens: int
    out_tokens: int


def _openai(model, system, user):
    if "openai" not in _clients:
        from openai import OpenAI

        _clients["openai"] = OpenAI()
    resp = _clients["openai"].chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    usage = resp.usage
    return resp.choices[0].message.content or "", usage.prompt_tokens, usage.completion_tokens


def _gemini(model, system, user):
    from google.genai import types

    if "gemini" not in _clients:
        from google import genai

        _clients["gemini"] = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    resp = _clients["gemini"].models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system, temperature=0),
    )
    meta = resp.usage_metadata
    return resp.text or "", meta.prompt_token_count or 0, meta.candidates_token_count or 0


def _anthropic(model, system, user):
    if "anthropic" not in _clients:
        import anthropic

        _clients["anthropic"] = anthropic.Anthropic()
    kwargs = {"system": system} if system else {}
    resp = _clients["anthropic"].messages.create(
        model=model,
        max_tokens=512,
        # This model rejects the temperature parameter, so it runs at default sampling.
        messages=[{"role": "user", "content": user}],
        **kwargs,
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _mock(model, system, user):
    """Offline stand-in for pipeline tests. Naively obeys any canary it sees, unless told not to."""
    if "YES or NO" in user:
        return ("YES" if "assistant" in user.split("Text:", 1)[-1].lower() else "NO"), 0, 0
    guarded = "untrusted" in system or "ignore any instructions" in user
    for canary in ("ZEBRA-17", "CANARY-42", "KESTREL-55", "ARRR-99"):
        if canary in user and not guarded:
            return f"{canary}. Mock answer.", 0, 0
    first_doc = user.split("\n\n")[1] if "\n\n" in user else user
    return first_doc[:300], 0, 0


PROVIDERS = {"openai": _openai, "gemini": _gemini, "anthropic": _anthropic, "mock": _mock}


def call(spec, system, user, retries=5):
    provider, model = spec.split(":", 1)
    fn = PROVIDERS[provider]
    for attempt in range(retries):
        try:
            start = time.perf_counter()
            text, tin, tout = fn(model, system, user)
            return Reply(text, (time.perf_counter() - start) * 1000, tin, tout)
        except Exception as exc:  # rate limits, timeouts, transient errors
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * 2
            print(f"  {spec} error ({type(exc).__name__}: {exc}); retrying in {wait}s")
            time.sleep(wait)
