"""Compute metrics with 95% Wilson confidence intervals and draw the charts.

Usage: python -m src.analyse [--results results/trials.jsonl]
Writes results/summary.md and three PNG charts to results/.
"""
import argparse
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .data import ROOT

DEFENSE_ORDER = ["none", "spotlight", "sandwich", "monitor"]
COLORS = ["#2a6f97", "#c8553d", "#6a994e", "#8d6a9f"]


def wilson(k, n, z=1.96):
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def rate_table(df, col, by):
    rows = []
    for keys, g in df.groupby(by, sort=False):
        keys = keys if isinstance(keys, tuple) else (keys,)
        k, n = int(g[col].sum()), len(g)
        p, lo, hi = wilson(k, n)
        rows.append({**dict(zip(by, keys)), "k": k, "n": n, "rate": p, "lo": lo, "hi": hi})
    return pd.DataFrame(rows)


def md_table(df, cols, headers):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def pct(r):
    return f'{r["rate"]:.1%} [{r["lo"]:.1%}, {r["hi"]:.1%}] ({r["k"]}/{r["n"]})'


def order_defenses(df):
    df["defense"] = pd.Categorical(df["defense"], [d for d in DEFENSE_ORDER if d in set(df["defense"])], ordered=True)
    return df.sort_values(["model", "defense"])


def grouped_bars(table, group_col, path, title, xlabel):
    models = list(dict.fromkeys(table["model"]))
    groups = list(dict.fromkeys(table[group_col]))
    width = 0.8 / len(models)
    fig, ax = plt.subplots(figsize=(max(7, len(groups) * 1.4), 4.5))
    for i, m in enumerate(models):
        sub = table[table["model"] == m].set_index(group_col).reindex(groups)
        x = np.arange(len(groups)) + i * width - 0.4 + width / 2
        err = [(sub["rate"] - sub["lo"]).clip(lower=0), (sub["hi"] - sub["rate"]).clip(lower=0)]
        ax.bar(x, sub["rate"], width, yerr=err, capsize=3, label=m, color=COLORS[i % len(COLORS)])
    ax.set_xticks(np.arange(len(groups)), groups)
    ax.set_ylim(0, 1)
    ax.set_ylabel("Attack success rate")
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--results", default=str(ROOT / "results" / "trials.jsonl"))
    args = p.parse_args()
    out_dir = Path(args.results).parent
    df = pd.read_json(args.results, lines=True)
    df["total_latency_ms"] = df["latency_ms"] + df["monitor_latency_ms"]
    attacked = df[df["attack"] != "clean"].copy()
    clean = df[df["attack"] == "clean"].copy()

    asr = order_defenses(rate_table(attacked, "attack_success", ["model", "defense"]))
    asr_type = rate_table(attacked, "attack_success", ["model", "defense", "attack"])
    asr_pos = rate_table(attacked, "attack_success", ["model", "defense", "position"])
    acc = order_defenses(rate_table(clean, "correct", ["model", "defense"]))
    acc_att = order_defenses(rate_table(attacked, "correct", ["model", "defense"]))

    lat = df.groupby(["model", "defense"], observed=True)["total_latency_ms"].median().rename("median_ms").reset_index()
    base = lat[lat["defense"] == "none"].set_index("model")["median_ms"]
    lat["overhead_ms"] = lat.apply(lambda r: r["median_ms"] - base.get(r["model"], float("nan")), axis=1)
    tok = df.groupby(["model", "defense"], observed=True)[["in_tokens", "monitor_calls"]].mean().reset_index()
    lat = order_defenses(lat.merge(tok, on=["model", "defense"]))

    retrieval = rate_table(attacked[attacked["defense"] == "none"], "target_retrieved", ["model", "position"])
    mon = attacked[attacked["defense"] == "monitor"]
    mon_clean = clean[clean["defense"] == "monitor"]

    for t in (asr, acc, acc_att, asr_type, asr_pos, retrieval):
        t["summary"] = t.apply(pct, axis=1)
    lat["median_ms"] = lat["median_ms"].round(0)
    lat["overhead_ms"] = lat["overhead_ms"].round(0)
    lat["in_tokens"] = lat["in_tokens"].round(0)
    lat["monitor_calls"] = lat["monitor_calls"].round(2)

    pivot_type = asr_type.pivot_table(index=["model", "attack"], columns="defense", values="summary", aggfunc="first", observed=True)
    pivot_type = pivot_type[[d for d in DEFENSE_ORDER if d in pivot_type.columns]].reset_index()

    md = ["# Results summary", "", "Rates show the estimate, the 95% Wilson interval in brackets, and the raw count.", ""]
    md += ["## Attack success rate by defense", "", md_table(asr, ["model", "defense", "summary"], ["Model", "Defense", "Attack success"]), ""]
    md += ["## Clean accuracy (no attack) by defense", "", md_table(acc, ["model", "defense", "summary"], ["Model", "Defense", "Correct"]), ""]
    md += ["## Accuracy while under attack", "", md_table(acc_att, ["model", "defense", "summary"], ["Model", "Defense", "Correct"]), ""]
    md += ["## Attack success by attack type", "", md_table(pivot_type, list(pivot_type.columns), list(pivot_type.columns)), ""]
    md += ["## Cost of each defense", "", md_table(lat, ["model", "defense", "median_ms", "overhead_ms", "in_tokens", "monitor_calls"],
                                                 ["Model", "Defense", "Median latency (ms)", "Overhead vs none (ms)", "Avg input tokens", "Avg new monitor calls"]), ""]
    md += ["## Was the poisoned document retrieved? (no defense)", "", md_table(retrieval, ["model", "position", "summary"], ["Model", "Injection position", "Target retrieved"]), ""]
    if len(mon):
        md += ["## Monitor behaviour", "",
               f'- Attacked trials where the monitor dropped the poisoned document: {mon["monitor_dropped_target"].mean():.1%} ({int(mon["monitor_dropped_target"].sum())}/{len(mon)})',
               f'- Clean trials where the monitor wrongly dropped the answer document: {mon_clean["monitor_dropped_target"].mean():.1%} ({int(mon_clean["monitor_dropped_target"].sum())}/{len(mon_clean)})' if len(mon_clean) else "",
               ""]
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")

    grouped_bars(asr.astype({"defense": str}), "defense", out_dir / "asr_by_defense.png", "Attack success rate by defense", "Defense")
    none_type = asr_type[asr_type["defense"] == "none"]
    if len(none_type):
        grouped_bars(none_type, "attack", out_dir / "asr_by_attack_no_defense.png", "Attack success by attack type (no defense)", "Attack type")

    merged = asr.astype({"defense": str}).merge(lat.astype({"defense": str}), on=["model", "defense"])
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for i, m in enumerate(dict.fromkeys(merged["model"])):
        sub = merged[merged["model"] == m]
        ax.scatter(sub["overhead_ms"], sub["rate"], s=60, color=COLORS[i % len(COLORS)], label=m)
        for _, r in sub.iterrows():
            ax.annotate(r["defense"], (r["overhead_ms"], r["rate"]), textcoords="offset points", xytext=(6, 4), fontsize=9)
    ax.set_xlabel("Median latency overhead vs no defense (ms)")
    ax.set_ylabel("Attack success rate")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_title("Safety vs cost of each defense")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(out_dir / "tradeoff.png", dpi=150)
    plt.close(fig)
    print(f"Wrote {out_dir / 'summary.md'} and charts to {out_dir}")


if __name__ == "__main__":
    main()
