"""STEP 3 - Grade answers, apply temperature scaling, compute metrics and draw charts.

Reads  results/raw.jsonl (+ run_meta.json)
Writes results/summary.json, results/per_question.csv, results/charts/*.png

  python analyze.py
"""
import argparse
import csv
import json
import math
import os

import numpy as np

from common import DATASET_LABELS, is_correct, same_answer

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
EPS = 1e-6
HIGH_CONF = 0.90  # "confidently wrong" threshold


def ece(conf, correct, bins=10):
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(conf, edges[1:-1], right=False), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            total += m.mean() * abs(conf[m].mean() - correct[m].mean())
    return float(total)


def reliability_bins(conf, correct, bins=10):
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(conf, edges[1:-1], right=False), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        out.append({"lo": float(edges[b]), "hi": float(edges[b + 1]), "count": int(m.sum()),
                    "mean_conf": float(conf[m].mean()) if m.any() else None,
                    "accuracy": float(correct[m].mean()) if m.any() else None})
    return out


def brier(conf, correct):
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    return float(np.mean((conf - correct) ** 2))


def auroc(conf, correct):
    """Probability that a random correct answer gets higher confidence than a random wrong one."""
    conf, correct = np.asarray(conf, float), np.asarray(correct, bool)
    pos, neg = conf[correct], conf[~correct]
    if len(pos) == 0 or len(neg) == 0:
        return None
    # rank-based (Mann-Whitney U) with tie handling
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    allv = np.concatenate([pos, neg])[order]
    ranks = np.empty(len(allv))
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[i:j + 1] = (i + j) / 2 + 1
        i = j + 1
    r = np.empty(len(allv))
    r[order] = ranks
    u = r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2
    return float(u / (len(pos) * len(neg)))


def risk_coverage(conf, correct):
    """Sort by confidence (high first); risk = error rate among the answers we keep."""
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    order = np.argsort(-conf, kind="mergesort")
    c = correct[order]
    n = len(c)
    cov = np.arange(1, n + 1) / n
    risk = 1 - np.cumsum(c) / np.arange(1, n + 1)
    return cov, risk


def aurc(conf, correct):
    cov, risk = risk_coverage(conf, correct)
    return float(np.mean(risk))


def acc_at_coverage(conf, correct, coverage=0.8):
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    k = max(1, int(round(coverage * len(conf))))
    order = np.argsort(-conf, kind="mergesort")[:k]
    return float(correct[order].mean())


def all_metrics(conf, correct):
    conf, correct = np.asarray(conf, float), np.asarray(correct, bool)
    wrong = ~correct
    return {
        "n": int(len(conf)),
        "accuracy": float(correct.mean()),
        "mean_conf": float(conf.mean()),
        "overconfidence_gap": float(conf.mean() - correct.mean()),
        "ece": ece(conf, correct),
        "brier": brier(conf, correct),
        "auroc": auroc(conf, correct),
        "aurc": aurc(conf, correct),
        "acc_at_80cov": acc_at_coverage(conf, correct, 0.8),
        "confidently_wrong_rate": float(np.mean(wrong & (conf >= HIGH_CONF))),
        "wrong_with_high_conf_share": float(np.mean(conf[wrong] >= HIGH_CONF)) if wrong.any() else None,
        "mean_conf_when_wrong": float(conf[wrong].mean()) if wrong.any() else None,
        "mean_conf_when_right": float(conf[correct].mean()) if correct.any() else None,
    }


# ---------------------------------------------------------------------------
# Temperature scaling on the confidence logit, fitted with K-fold cross-fitting
# (each question's calibrated score comes from a T fitted on the OTHER folds,
#  so we never evaluate on the data we calibrated on)
# ---------------------------------------------------------------------------

def _logit(p):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def _nll(T, z, y):
    p = 1 / (1 + np.exp(-z / T))
    p = np.clip(p, EPS, 1 - EPS)
    return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))


def fit_temperature(conf, correct):
    z, y = _logit(conf), np.asarray(correct, float)
    grid = np.exp(np.linspace(np.log(0.05), np.log(50), 400))
    losses = [_nll(T, z, y) for T in grid]
    return float(grid[int(np.argmin(losses))])


def temperature_scale_cv(conf, correct, folds=5, seed=7):
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    rng = np.random.default_rng(seed)
    fold_id = rng.permutation(len(conf)) % folds
    out = np.empty(len(conf))
    temps = []
    for f in range(folds):
        test = fold_id == f
        T = fit_temperature(conf[~test], correct[~test])
        temps.append(T)
        out[test] = 1 / (1 + np.exp(-_logit(conf[test]) / T))
    return out, float(np.median(temps))


# ---------------------------------------------------------------------------
# Charts (dark theme matching the slide deck)
# ---------------------------------------------------------------------------
BG, PANEL, GRID, INK, MUTED = "#0E1828", "#14223A", "#2F415C", "#F5F8FC", "#B0BED2"
METHOD_COLORS = {  # fixed per method across every chart
    "Token probability": "#3987E5",
    "Token prob. + TS": "#199E70",
    "Verbalized confidence": "#D95926",
    "Verbalized + TS": "#C98500",
    "Self-consistency (k={k})": "#D55181",
}
RIGHT, WRONG = "#5CC98B", "#EB6868"


def _style(plt):
    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
        "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
        "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.alpha": 0.6,
        "font.size": 11, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.spines.top": False,
        "axes.spines.right": False, "legend.frameon": False,
    })


def chart_reliability(methods, path):
    import matplotlib.pyplot as plt

    _style(plt)
    names = list(methods)
    fig, axes = plt.subplots(1, len(names), figsize=(2.75 * len(names), 3.3), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, name in zip(axes, names):
        m = methods[name]
        color = m["color"]
        bins = m["reliability"]
        ax.plot([0, 1], [0, 1], ls="--", lw=1, color=MUTED, alpha=0.8)
        for b in bins:
            if b["count"] == 0:
                continue
            w = b["hi"] - b["lo"]
            ax.bar(b["lo"] + w / 2, b["accuracy"], width=w * 0.86, color=color, alpha=0.9, linewidth=0)
        ax.set_title(name, fontsize=11, color=INK)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1.02)
        ax.set_xticks([0, 0.5, 1])
        ax.set_xlabel("Stated confidence")
        ax.text(0.04, 0.93, f"ECE {m['metrics']['ece']:.3f}", fontsize=10, color=INK, transform=ax.transAxes)
    axes[0].set_ylabel("Actual accuracy")
    fig.tight_layout(w_pad=1.2)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def chart_wrong_hist(conf, correct, path, label):
    import matplotlib.pyplot as plt

    _style(plt)
    conf, correct = np.asarray(conf, float), np.asarray(correct, bool)
    bins = np.linspace(0, 1, 11)
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    h_r, _ = np.histogram(conf[correct], bins)
    h_w, _ = np.histogram(conf[~correct], bins)
    x = (bins[:-1] + bins[1:]) / 2
    ax.bar(x - 0.022, h_r, width=0.04, color=RIGHT, label=f"Correct answers ({correct.sum()})")
    ax.bar(x + 0.022, h_w, width=0.04, color=WRONG, label=f"Wrong answers ({(~correct).sum()})")
    ax.set_xticks(bins)
    ax.set_xticklabels([f"{int(b*100)}" for b in bins])
    ax.set_xlabel(f"{label} (%)")
    ax.set_ylabel("Number of questions")
    ax.legend(loc="upper left", labelcolor=INK)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def chart_risk_coverage(curves, path, triage_point=None):
    import matplotlib.pyplot as plt

    _style(plt)
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    for name, (cov, risk, color) in curves.items():
        ax.plot(cov * 100, risk * 100, lw=2, color=color, label=name)
    if triage_point:
        cx, cy = triage_point
        ax.scatter([cx * 100], [cy * 100], s=70, color=INK, zorder=5, edgecolor=BG, linewidth=2)
        ax.annotate("triage threshold", (cx * 100, cy * 100), xytext=(-110, 18), textcoords="offset points",
                    color=INK, fontsize=10, arrowprops=dict(arrowstyle="-", color=MUTED, lw=1))
    ax.set_xlabel("Coverage: % of questions answered (most confident first)")
    ax.set_ylabel("Error rate among answered (%)")
    ax.set_xlim(0, 100)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", labelcolor=INK, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results")
    ap.add_argument("--tau", type=float, default=0.8, help="triage: accept if this share of samples agree")
    args = ap.parse_args()

    raw_path = os.path.join(args.out, "raw.jsonl")
    with open(raw_path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f if line.strip()]
    recs = list({r["id"]: r for r in recs}.values())  # de-duplicate on restarts
    meta = {}
    if os.path.exists(os.path.join(args.out, "run_meta.json")):
        with open(os.path.join(args.out, "run_meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    k = meta.get("k", len(recs[0]["samples"]))
    print(f"Loaded {len(recs)} questions")

    # ---- grade + raw confidence signals
    g_correct, v_correct, agree, seqp, vconf = [], [], [], [], []
    verbal_missing = 0
    for r in recs:
        r["greedy_correct"] = is_correct(r["greedy_answer"], r["aliases"])
        r["verbal_correct"] = is_correct(r["verbal_answer"], r["aliases"])
        s = r["samples"]
        r["agreement"] = sum(same_answer(x, r["greedy_answer"]) for x in s) / max(1, len(s))
        if r.get("verbal_conf") is None:
            verbal_missing += 1
        g_correct.append(r["greedy_correct"])
        v_correct.append(r["verbal_correct"])
        agree.append(r["agreement"])
        seqp.append(r.get("greedy_seq_prob"))
        vconf.append(r["verbal_conf"] if r.get("verbal_conf") is not None else 0.5)

    g_correct, v_correct = np.array(g_correct), np.array(v_correct)
    agree, vconf = np.array(agree, float), np.array(vconf, float)
    have_logprobs = all(p is not None for p in seqp)

    sc_name = f"Self-consistency (k={k})"
    colors = {n.replace("{k}", str(k)): c for n, c in METHOD_COLORS.items()}
    methods, temps = {}, {}
    if have_logprobs:
        seqp = np.array(seqp, float)
        ts, T = temperature_scale_cv(seqp, g_correct)
        temps["token"] = T
        methods["Token probability"] = (seqp, g_correct, "Probability the model assigns to its own answer tokens (greedy decoding)", 1)
        methods["Token prob. + TS"] = (ts, g_correct, f"Token probability rescaled by temperature scaling (median T = {T:.2f}, 5-fold CV)", 1)
    vts, Tv = temperature_scale_cv(vconf, v_correct)
    temps["verbal"] = Tv
    methods["Verbalized confidence"] = (vconf, v_correct, "Model states 'Confidence: NN%' with its answer", 1)
    methods["Verbalized + TS"] = (vts, v_correct, f"Verbalized confidence rescaled by temperature scaling (median T = {Tv:.2f}, 5-fold CV)", 1)
    methods[sc_name] = (agree, g_correct, f"Share of {k} samples (temperature 1.0) that agree with the greedy answer", 1 + k)

    out_methods = {}
    for name, (conf, corr, desc, calls) in methods.items():
        out_methods[name] = {
            "description": desc, "api_calls_per_question": calls, "color": colors[name],
            "metrics": all_metrics(conf, corr), "reliability": reliability_bins(conf, corr),
        }

    # ---- triage preview (Stage 1 of our proposed pipeline = self-consistency filter)
    triage = []
    for tau in [0.4, 0.6, 0.8, 1.0]:
        accept = agree >= tau - 1e-9
        esc = ~accept
        n_err = int((~g_correct).sum())
        triage.append({
            "tau": tau,
            "accepted_share": float(accept.mean()),
            "escalated_share": float(esc.mean()),
            "accuracy_accepted": float(g_correct[accept].mean()) if accept.any() else None,
            "error_rate_escalated": float((~g_correct[esc]).mean()) if esc.any() else None,
            "errors_caught_share": float((~g_correct[esc]).sum() / n_err) if n_err else None,
        })
    main_triage = min(triage, key=lambda t: abs(t["tau"] - args.tau))

    # ---- confidently-wrong examples (verbalized), most confident first
    examples = []
    for r in sorted(recs, key=lambda r: -(r.get("verbal_conf") or 0)):
        if not r["verbal_correct"] and (r.get("verbal_conf") or 0) >= HIGH_CONF:
            examples.append({"question": r["question"], "model_answer": r["verbal_answer"],
                             "gold": r["aliases"][0], "confidence": r["verbal_conf"],
                             "agreement": r["agreement"]})
    # ---- charts
    chart_dir = os.path.join(args.out, "charts")
    os.makedirs(chart_dir, exist_ok=True)
    rel_show = [m for m in ["Token probability", "Verbalized confidence", "Verbalized + TS", sc_name] if m in out_methods]
    chart_reliability({m: out_methods[m] for m in rel_show}, os.path.join(chart_dir, "reliability.png"))
    chart_wrong_hist(vconf, v_correct, os.path.join(chart_dir, "verbal_conf_hist.png"), "Verbalized confidence")
    curves = {}
    for name in out_methods:
        if name.endswith("+ TS"):
            continue  # TS is monotonic -> identical ranking -> identical curve
        conf, corr = methods[name][0], methods[name][1]
        cov, risk = risk_coverage(conf, corr)
        curves[name] = (cov, risk, colors[name])
    tp = None
    if main_triage["accuracy_accepted"] is not None:
        tp = (main_triage["accepted_share"], 1 - main_triage["accuracy_accepted"])
    chart_risk_coverage(curves, os.path.join(chart_dir, "risk_coverage.png"), tp)

    # ---- per-question CSV (handy for spotting grading mistakes)
    with open(os.path.join(args.out, "per_question.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "question", "gold", "greedy_answer", "greedy_correct", "token_prob", "agreement",
                    "verbal_answer", "verbal_correct", "verbal_conf"])
        for r in recs:
            w.writerow([r["id"], r["question"], " | ".join(r["aliases"][:5]), r["greedy_answer"], int(r["greedy_correct"]),
                        "" if r.get("greedy_seq_prob") is None else round(r["greedy_seq_prob"], 4), r["agreement"],
                        r["verbal_answer"], int(r["verbal_correct"]), r.get("verbal_conf")])

    summary = {
        "run": {**meta, "n_questions": len(recs), "dataset_label": DATASET_LABELS.get(meta.get("dataset", ""), meta.get("dataset", "")),
                "verbal_parse_failures": verbal_missing, "logprobs_available": have_logprobs},
        "high_conf_threshold": HIGH_CONF,
        "temperatures": temps,
        "methods": out_methods,
        "triage": triage,
        "triage_main": main_triage,
        "confidently_wrong_examples": examples[:8],
        "n_confidently_wrong_verbal": len(examples),
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # ---- console report
    print(f"\n{'Method':28s} {'Acc':>6s} {'Conf':>6s} {'ECE':>6s} {'Brier':>6s} {'AUROC':>6s} {'Wrong@>=90%':>12s}")
    for name, m in out_methods.items():
        mm = m["metrics"]
        au = f"{mm['auroc']:.3f}" if mm["auroc"] is not None else "  n/a"
        wh = f"{mm['wrong_with_high_conf_share']:.0%}" if mm["wrong_with_high_conf_share"] is not None else "n/a"
        print(f"{name:28s} {mm['accuracy']:6.1%} {mm['mean_conf']:6.1%} {mm['ece']:6.3f} {mm['brier']:6.3f} {au:>6s} {wh:>12s}")
    t = main_triage
    print(f"\nTriage preview (accept if >= {t['tau']:.0%} of samples agree): escalate {t['escalated_share']:.0%} of questions, "
          f"which contain {t['errors_caught_share'] or 0:.0%} of all errors.")
    if verbal_missing:
        print(f"Note: {verbal_missing} verbalized answers had no parsable confidence (counted as 50%).")
    print(f"\nWrote {args.out}/summary.json, per_question.csv and charts/*.png")
    print("CHECK per_question.csv: skim ~20 rows marked wrong to make sure the auto-grader is fair.")


if __name__ == "__main__":
    main()
