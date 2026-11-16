"""Quick check that your API keys work. Usage: python -m src.check_keys openai:gpt-4o-mini gemini:gemini-2.5-flash"""
import sys

from dotenv import load_dotenv

from . import providers
from .data import ROOT


def main():
    load_dotenv(ROOT / ".env")
    specs = sys.argv[1:] or ["openai:gpt-4o-mini", "gemini:gemini-2.5-flash"]
    for spec in specs:
        try:
            reply = providers.call(spec, "Reply in one word.", "Say OK.", retries=1)
            print(f"{spec}: OK ({reply.text.strip()!r}, {reply.latency_ms:.0f} ms)")
        except Exception as exc:
            print(f"{spec}: FAILED ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    main()
