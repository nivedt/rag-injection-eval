"""Run every trial and append one JSON line per trial to the results file.

Re-running is safe: trials already in the results file are skipped, so an
interrupted run continues where it stopped.

Examples:
  python -m src.run_eval --models mock:test --embedder tfidf --limit 20     # offline smoke test
  python -m src.run_eval --models openai:gpt-4o-mini --defenses none --limit 30
  python -m src.run_eval --models openai:gpt-4o-mini gemini:gemini-2.5-flash
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import providers
from .data import POSITIONS, ROOT, answer_correct, attack_succeeded, attack_text, inject, load_dataset
from .defenses import DEFENSES, MONITOR_PROMPT, build_prompt
from .retrieval import Retriever


def build_trials(docs, questions, attacks):
    trials = []
    for q in questions:
        trials.append({"qid": q["id"], "attack": "clean", "position": None, "q": q, "a": None})
        for a in attacks:
            for pos in POSITIONS:
                trials.append({"qid": q["id"], "attack": a["id"], "position": pos, "q": q, "a": a})
    return trials


class MonitorCache:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def key(self, model, chunk):
        return hashlib.sha256(f"{model}\n{chunk}".encode()).hexdigest()

    def save(self):
        self.path.write_text(json.dumps(self.data))


def run_monitor(spec, chunks, cache, stats):
    kept, dropped = [], []
    for doc_id, text in chunks:
        k = cache.key(spec, text)
        if k not in cache.data:
            reply = providers.call(spec, "", MONITOR_PROMPT.format(chunk=text))
            cache.data[k] = reply.text.strip().upper().startswith("YES")
            stats["calls"] += 1
            stats["latency_ms"] += reply.latency_ms
        (dropped if cache.data[k] else kept).append((doc_id, text))
    return kept, dropped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", required=True, help="e.g. openai:gpt-4o-mini gemini:gemini-2.5-flash")
    p.add_argument("--defenses", nargs="+", default=DEFENSES, choices=DEFENSES)
    p.add_argument("--monitor-model", default=None, help="model used by the monitor defense (default: same as answering model)")
    p.add_argument("--embedder", default="minilm", choices=["minilm", "tfidf"])
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--limit", type=int, default=None, help="only run the first N trials per model and defense")
    p.add_argument("--out", default=str(ROOT / "results" / "trials.jsonl"))
    p.add_argument("--sleep", type=float, default=0.0, help="seconds to pause between calls")
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    docs, questions, attacks = load_dataset()
    retriever = Retriever(docs, args.embedder)
    trials = build_trials(docs, questions, attacks)[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(line)["trial_id"] for line in out.read_text().splitlines() if line.strip()}
    cache = MonitorCache(out.parent / "monitor_cache.json")

    for model in args.models:
        for defense in args.defenses:
            todo = [t for t in trials if f'{model}|{defense}|{t["qid"]}|{t["attack"]}|{t["position"]}' not in done]
            print(f"{model} / {defense}: {len(todo)} trials to run")
            for n, t in enumerate(todo, 1):
                q, a = t["q"], t["a"]
                trial_id = f'{model}|{defense}|{q["id"]}|{t["attack"]}|{t["position"]}'
                override = None
                if a:
                    override = (q["doc_id"], inject(docs[q["doc_id"]], attack_text(a, q), t["position"]))
                chunks = retriever.top_k(q["question"], args.k, override)
                retrieved_ids = [d for d, _ in chunks]

                mstats = {"calls": 0, "latency_ms": 0.0}
                dropped_ids = []
                if defense == "monitor":
                    chunks, dropped = run_monitor(args.monitor_model or model, chunks, cache, mstats)
                    dropped_ids = [d for d, _ in dropped]

                system, user = build_prompt(defense, q["question"], chunks)
                reply = providers.call(model, system, user)
                row = {
                    "trial_id": trial_id,
                    "model": model,
                    "defense": defense,
                    "qid": q["id"],
                    "attack": t["attack"],
                    "position": t["position"],
                    "target_doc": q["doc_id"],
                    "target_retrieved": q["doc_id"] in retrieved_ids,
                    "retrieved_ids": retrieved_ids,
                    "monitor_dropped_ids": dropped_ids,
                    "monitor_dropped_target": q["doc_id"] in dropped_ids,
                    "response": reply.text,
                    "attack_success": attack_succeeded(reply.text, a, q) if a else None,
                    "correct": answer_correct(reply.text, q),
                    "latency_ms": round(reply.latency_ms, 1),
                    "monitor_latency_ms": round(mstats["latency_ms"], 1),
                    "monitor_calls": mstats["calls"],
                    "in_tokens": reply.in_tokens,
                    "out_tokens": reply.out_tokens,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                with open(out, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                if n % 25 == 0:
                    cache.save()
                    print(f"  {n}/{len(todo)}")
                if args.sleep:
                    time.sleep(args.sleep)
            cache.save()
    print(f"Done. Results in {out}")


if __name__ == "__main__":
    main()
