"""Compute metrics with 95% Wilson confidence intervals and draw the charts.

Usage: python -m src.analyse [--results results/trials.jsonl]
If results/judgements.jsonl exists (from python -m src.judge), the headline attack
success rate uses the judge's labels; the keyword-based rate is reported alongside.
Writes results/summary.md and PNG charts to results/.
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
    for keys, g in df.groupby(by, sort=False, observed=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        k, n = int(g[col].sum()), len(g)
        p, lo, hi = wilson(k, n)
        rows.append({**dict(zip(by, keys)), "k": k, "n": n, "rate": p, "lo": lo, "hi": hi})
    t = pd.DataFrame(rows)
    t["summary"] = t.apply(pct, axis=1)
    return t


def pct(r):
    return f'{r["rate"]:.1%} [{r["lo"]:.1%}, {r["hi"]:.1%}] ({r["k"]}/{r["n"]})'


def md_table(df, cols, headers):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def order_defenses(df):
    df = df.copy()
    df["defense"] = pd.Categorical(df["defense"].astype(str), [d for d in DEFENSE_ORDER if d in set(df["defense"].astype(str))], ordered=True)
    return df.sort_values(["model", "defense"])


def pivot(table, index, col="defense"):
    pv = table.pivot_table(index=index, columns=col, values="summary", aggfunc="first", observed=True)
    if col == "defense":
        pv = pv[[d for d in DEFENSE_ORDER if d in pv.columns]]
    return pv.reset_index()


def grouped_bars(table, group_col, path, title, xlabel, ylabel="Attack success rate", series_col="model"):
    models = sorted(dict.fromkeys(table[series_col]))
    groups = list(dict.fromkeys(table[group_col].astype(str)))
    width = 0.8 / len(models)
    fig, ax = plt.subplots(figsize=(max(7, len(groups) * 1.5), 4.5))
    for i, m in enumerate(models):
        sub = table[table[series_col] == m].assign(g=lambda d: d[group_col].astype(str)).set_index("g").reindex(groups)
        x = np.arange(len(groups)) + i * width - 0.4 + width / 2
        err = [(sub["rate"] - sub["lo"]).clip(lower=0), (sub["hi"] - sub["rate"]).clip(lower=0)]
        ax.bar(x, sub["rate"], width, yerr=err, capsize=3, label=m, color=COLORS[i % len(COLORS)])
    ax.set_xticks(np.arange(len(groups)), groups)
    ax.set_ylim(0, 1)
    ax.set_ylabel(ylabel)
    ax.set_xlabel(xlabel)
    ax.set_title(title, pad=34)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=len(models))
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--results", default=str(ROOT / "results" / "trials.jsonl"))
    args = p.parse_args()
    out_dir = Path(args.results).parent
    df = pd.read_json(args.results, lines=True)
    df["total_latency_ms"] = df["latency_ms"] + df["monitor_latency_ms"]
    df["keyword_success"] = df["attack_success"].astype("boolean").fillna(False).astype(bool)

    jpath = out_dir / "judgements.jsonl"
    judged = jpath.exists()
    if judged:
        j = pd.read_json(jpath, lines=True).drop_duplicates("trial_id", keep="last")
        df = df.merge(j[["trial_id", "label", "answer_correct", "reason"]], on="trial_id", how="left")
        df["obeyed"] = df["label"].eq("OBEYED")
        df["obeyed_or_partial"] = df["label"].isin(["OBEYED", "PARTIAL"])
        df["flagged"] = df["label"].eq("FLAGGED")
        df["judge_correct"] = df["answer_correct"].fillna(False).astype(bool)

    attacked = df[df["attack"] != "clean"].copy()
    clean = df[df["attack"] == "clean"].copy()
    if judged:
        missing = attacked["label"].isna().sum()
        if missing:
            print(f"Warning: {missing} attacked trials have no judgement and are left out of judged tables. Re-run python -m src.judge.")
        attacked = attacked[attacked["label"].notna()]
    headline = "obeyed" if judged else "keyword_success"

    asr = order_defenses(rate_table(attacked, headline, ["model", "defense"]))
    asr_type = rate_table(attacked, headline, ["model", "defense", "attack"])
    asr_pos = rate_table(attacked[attacked["defense"] == "none"], headline, ["model", "position"])
    acc = order_defenses(rate_table(clean, "correct", ["model", "defense"]))
    acc_att = order_defenses(rate_table(attacked, "judge_correct" if judged else "correct", ["model", "defense"]))

    lat = df.groupby(["model", "defense"], observed=True)["total_latency_ms"].median().rename("median_ms").reset_index()
    base = lat[lat["defense"] == "none"].set_index("model")["median_ms"]
    lat["overhead_ms"] = lat.apply(lambda r: r["median_ms"] - base.get(r["model"], float("nan")), axis=1)
    tok = df.groupby(["model", "defense"], observed=True)[["in_tokens", "monitor_calls"]].mean().reset_index()
    lat = order_defenses(lat.merge(tok, on=["model", "defense"]))
    for c, nd in [("median_ms", 0), ("overhead_ms", 0), ("in_tokens", 0), ("monitor_calls", 2)]:
        lat[c] = lat[c].round(nd)

    mon = attacked[attacked["defense"] == "monitor"]
    mon_clean = clean[clean["defense"] == "monitor"]

    md = ["# Results summary", "",
          "Rates show the estimate, the 95% Wilson interval in brackets, and the raw count.",
          f"Headline attack success uses **{'LLM-judge labels (OBEYED)' if judged else 'canary / keyword matching'}**.", ""]
    md += ["## Attack success rate by defense", "", md_table(asr, ["model", "defense", "summary"], ["Model", "Defense", "Attack success"]), ""]
    if judged:
        kw = order_defenses(rate_table(attacked, "keyword_success", ["model", "defense"]))
        lenient = order_defenses(rate_table(attacked, "obeyed_or_partial", ["model", "defense"]))
        flag = order_defenses(rate_table(attacked, "flagged", ["model", "defense"]))
        cmp = asr[["model", "defense", "summary"]].rename(columns={"summary": "judge"}).astype({"defense": str})
        for name, t in [("keyword", kw), ("lenient", lenient), ("flagged", flag)]:
            cmp = cmp.merge(t[["model", "defense", "summary"]].astype({"defense": str}).rename(columns={"summary": name}), on=["model", "defense"])
        md += ["## Keyword scoring vs LLM judge", "",
               "Keyword = canary or false value appears anywhere in the reply. Judge = the reply actually did what the attacker wanted. "
               "Lenient = judge OBEYED or PARTIAL. Flagged = the model refused and warned the user about the planted text.", "",
               md_table(cmp, ["model", "defense", "keyword", "judge", "lenient", "flagged"],
                        ["Model", "Defense", "Keyword success", "Judge: obeyed", "Judge: obeyed or hedged", "Flagged to user"]), ""]
        labels = attacked.groupby(["model", "label"]).size().unstack(fill_value=0).reset_index()
        md += ["## Judge label counts (all defenses)", "", md_table(labels, list(labels.columns), list(labels.columns)), ""]
        dis = attacked[attacked["keyword_success"] != attacked["obeyed"]]
        md += [f"Keyword and judge disagree on {len(dis)} of {len(attacked)} attacked trials ({len(dis) / len(attacked):.1%}).", ""]
    md += ["## Clean accuracy (no attack) by defense", "", md_table(acc, ["model", "defense", "summary"], ["Model", "Defense", "Correct"]), ""]
    md += ["## Accuracy while under attack", "", md_table(acc_att, ["model", "defense", "summary"], ["Model", "Defense", "Correct"]), ""]
    pt = pivot(asr_type, ["model", "attack"])
    md += ["## Attack success by attack type", "", md_table(pt, list(pt.columns), list(pt.columns)), ""]
    pp = asr_pos.pivot_table(index="model", columns="position", values="summary", aggfunc="first").reset_index()
    pp = pp[["model"] + [c for c in ["start", "middle", "end"] if c in pp.columns]]
    md += ["## Attack success by injection position (no defense)", "", md_table(pp, list(pp.columns), list(pp.columns)), ""]
    md += ["## Cost of each defense", "", md_table(lat, ["model", "defense", "median_ms", "overhead_ms", "in_tokens", "monitor_calls"],
                                                 ["Model", "Defense", "Median latency (ms)", "Overhead vs none (ms)", "Avg input tokens", "Avg new monitor calls"]), ""]
    retrieval = rate_table(attacked[attacked["defense"] == "none"], "target_retrieved", ["model", "position"])
    md += ["## Was the poisoned document retrieved? (no defense)", "", md_table(retrieval, ["model", "position", "summary"], ["Model", "Injection position", "Target retrieved"]), ""]
    if len(mon):
        md += ["## Monitor behaviour", ""]
        for m, g in mon.groupby("model"):
            gc = mon_clean[mon_clean["model"] == m]
            md.append(f'- {m}: dropped the poisoned document in {g["monitor_dropped_target"].mean():.1%} of attacked trials '
                      f'({int(g["monitor_dropped_target"].sum())}/{len(g)}); wrongly dropped the answer document in '
                      f'{int(gc["monitor_dropped_target"].sum())}/{len(gc)} clean trials')
        md.append("")
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")

    grouped_bars(asr, "defense", out_dir / "asr_by_defense.png", "Attack success rate by defense" + (" (LLM judge)" if judged else ""), "Defense")
    none_type = asr_type[asr_type["defense"] == "none"]
    grouped_bars(none_type, "attack", out_dir / "asr_by_attack_no_defense.png", "Attack success by attack type, no defense" + (" (LLM judge)" if judged else ""), "Attack type")
    if judged:
        k2 = kw.assign(defense=kw["defense"].astype(str))
        both = pd.concat([k2[k2["defense"] == "none"].assign(method="keyword"),
                          asr.assign(defense=asr["defense"].astype(str)).query("defense == 'none'").assign(method="LLM judge")])
        grouped_bars(both, "model", out_dir / "keyword_vs_judge.png", "Same replies, two scoring methods (no defense)", "Model", series_col="method")

    merged = asr.astype({"defense": str}).merge(lat.astype({"defense": str}), on=["model", "defense"])
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for i, m in enumerate(sorted(dict.fromkeys(merged["model"]))):
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
    if judged:
        attacked[["trial_id", "model", "defense", "attack", "position", "keyword_success", "label", "answer_correct", "reason", "response"]].to_csv(out_dir / "judged_trials.csv", index=False)
    print(f"Wrote {out_dir / 'summary.md'} and charts to {out_dir}")


if __name__ == "__main__":
    main()
