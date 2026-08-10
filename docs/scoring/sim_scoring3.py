"""Sim v3: partial seed coverage, wash-trading attack, parameter sweep.

Services: 10 real (reliability 100% -> 25%); seed reviews only 3 of them
(indices 0, 4, 9). Plus a "scam" service (25% reliable) wash-traded by 5
pumper wallets that always rate 5 and review nothing else.

Reviewers: 1 seed (honest, weight pinned 1.0, seeded services only),
3 honest (review everything), 10 inverters, 10 randoms, 5 pumpers.
Influence cap as in prod: one bucket per wallet/service/round (UTC day).

Trust rule: raw trust in [RAW_FLOOR, cap]; per review, p is implied by the
LEAVE-ONE-OUT score (own reviews excluded -- you cannot earn trust from a
consensus you created); update = eta * log2(likelihood / 0.5);
weight = raw if raw >= thresh else 0.

Sweep eta x cap x thresh x kappa; metrics: score MAE, honest flicker
(rounds a crossed honest wallet falls back to zero weight), max adversary
weight ever reached, final scam score.
"""

import math
import random
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MU0 = 3.0
ROUNDS = 100
P_CLAMP = (0.05, 0.95)
RAW_FLOOR = -5.0

N_REAL = 10
RELIABILITY = [1.0 - 0.75 * i / (N_REAL - 1) for i in range(N_REAL)] + [0.25]
SCAM = N_REAL  # index of the scam service
SEEDED = {0, 4, 9}
TARGETS = [1 + 4 * p for p in RELIABILITY]

REVIEWERS = ([("seed", "honest")]
             + [(f"hon{i}", "honest") for i in range(3)]
             + [(f"inv{i}", "inverter") for i in range(10)]
             + [(f"rand{i}", "random") for i in range(10)]
             + [(f"pump{i}", "pumper") for i in range(5)])


def services_for(name, kind):
    if name == "seed":
        return sorted(SEEDED)
    if kind == "pumper":
        return [SCAM]
    return list(range(len(RELIABILITY)))


def rating_for(kind, works, rng):
    if kind == "honest":
        return 5 if works else 1
    if kind == "inverter":
        return 1 if works else 5
    if kind == "pumper":
        return 5
    return rng.choice([1, 5])


def run(eta, cap, thresh, kappa, record=False):
    rng = random.Random(42)
    raw = {name: (1.0 if name == "seed" else 0.0) for name, _ in REVIEWERS}

    def weight(name):
        if name == "seed":
            return 1.0
        return min(cap, raw[name]) if raw[name] >= thresh else 0.0

    # per service: author -> [sum_of_ratings, count]
    stats = [dict() for _ in RELIABILITY]

    def score_parts(s):
        num, den = kappa * MU0, kappa
        for a, (tot, cnt) in stats[s].items():
            w = weight(a)
            num += w * tot
            den += w * cnt
        return num, den

    def loo_score(s, author):
        num, den = score_parts(s)
        if author in stats[s]:
            w = weight(author)
            tot, cnt = stats[s][author]
            num -= w * tot
            den -= w * cnt
        return num / den

    trust_hist = {name: [] for name, _ in REVIEWERS}
    score_hist = [[] for _ in RELIABILITY]
    crossed = {}          # honest wallet -> round it first got weight
    flicker = 0
    max_adv_w = 0.0

    for rnd_i in range(ROUNDS):
        for name, kind in REVIEWERS:
            for s in services_for(name, kind):
                works = rng.random() < RELIABILITY[s]
                r = rating_for(kind, works, rng)
                if name != "seed":
                    before = loo_score(s, name)
                    p = min(P_CLAMP[1], max(P_CLAMP[0], (before - 1) / 4))
                    likelihood = p if r >= 4 else 1 - p
                    raw[name] = min(cap, max(RAW_FLOOR,
                                             raw[name] + eta * math.log2(likelihood / 0.5)))
                tot, cnt = stats[s].get(name, (0, 0))
                stats[s][name] = (tot + r, cnt + 1)

        for name, kind in REVIEWERS:
            w = weight(name)
            if record:
                trust_hist[name].append(w)
            if kind == "honest" and name != "seed":
                if w > 0 and name not in crossed:
                    crossed[name] = rnd_i
                elif w == 0 and name in crossed:
                    flicker += 1
            if kind in ("inverter", "random", "pumper"):
                max_adv_w = max(max_adv_w, w)
        if record:
            for s in range(len(RELIABILITY)):
                num, den = score_parts(s)
                score_hist[s].append(num / den)

    finals = []
    for s in range(len(RELIABILITY)):
        num, den = score_parts(s)
        finals.append(num / den)
    mae = sum(abs(finals[s] - TARGETS[s]) for s in range(len(RELIABILITY))) / len(RELIABILITY)
    n_crossed = len(crossed)
    return dict(mae=mae, flicker=flicker, max_adv_w=max_adv_w,
                scam=finals[SCAM], finals=finals, n_crossed=n_crossed,
                trust_hist=trust_hist, score_hist=score_hist)


SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"


def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_all(res, cfg_label, outdir):
    th, sh = res["trust_hist"], res["score_hist"]
    x = list(range(1, ROUNDS + 1))

    fig, ax = plt.subplots(figsize=(9, 5), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax)
    cohorts = [
        ("seed (1)", ["seed"], SERIES[0], 2.0, 1.0, 0),
        ("honest (3)", [f"hon{i}" for i in range(3)], SERIES[1], 1.6, 0.8, 8),
        ("inverters (10)", [f"inv{i}" for i in range(10)], SERIES[2], 1.2, 0.55, 8),
        ("randoms (10)", [f"rand{i}" for i in range(10)], SERIES[3], 1.2, 0.55, -8),
        ("pumpers (5)", [f"pump{i}" for i in range(5)], SERIES[4], 1.2, 0.55, -22),
    ]
    for label, names, color, lw, alpha, off in cohorts:
        for j, n in enumerate(names):
            ax.plot(x, th[n], color=color, linewidth=lw, alpha=alpha,
                    label=label if j == 0 else None)
        last = sum(th[n][-1] for n in names) / len(names)
        ax.annotate(label, xy=(x[-1], last), xytext=(6, off),
                    textcoords="offset points", va="center", fontsize=9, color=INK)
    ax.set_ylim(-0.05, 1.08)
    ax.set_xlim(1, ROUNDS + 24)
    ax.set_xlabel("round", color=MUTED, fontsize=10)
    ax.set_ylabel("trust (effective weight)", color=MUTED, fontsize=10)
    ax.set_title(f"Reviewer reputation — seed covers 3/11 services  ({cfg_label})",
                 color=INK, fontsize=12, loc="left", pad=12)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK, loc="center left")
    fig.tight_layout()
    fig.savefig(f"{outdir}/reviewer_reputation_v3.png", facecolor=SURFACE)
    plt.close(fig)

    show = [(0, "A — 100%, seeded"), (1, "B — 92%, unseeded"),
            (6, "G — 50%, unseeded"), (9, "J — 25%, seeded"),
            (SCAM, "SCAM — 25%, 5 pumpers")]
    fig, ax = plt.subplots(figsize=(9, 5), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax)
    drawn = set()
    for i, (s, label) in enumerate(show):
        t = round(TARGETS[s], 2)
        if t not in drawn:
            drawn.add(t)
            ax.axhline(t, color=GRID, linewidth=1, linestyle=(0, (4, 3)))
            ax.annotate(f"true {t:.2f}", xy=(ROUNDS + 1, t), xytext=(6, 0),
                        textcoords="offset points", va="center",
                        fontsize=8, color=MUTED)
    for i, (s, label) in enumerate(show):
        ax.plot(x, sh[s], color=SERIES[i], linewidth=2, label=label)
    ax.set_ylim(1, 5.2)
    ax.set_xlim(1, ROUNDS + 16)
    ax.set_xlabel("round", color=MUTED, fontsize=10)
    ax.set_ylabel("score", color=MUTED, fontsize=10)
    ax.set_title(f"Service scores — seed covers 3/11, scam wash-traded  ({cfg_label})",
                 color=INK, fontsize=12, loc="left", pad=12)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK, loc="lower left")
    fig.tight_layout()
    fig.savefig(f"{outdir}/service_scores_v3.png", facecolor=SURFACE)
    plt.close(fig)


def main():
    import os
    outdir = os.path.dirname(os.path.abspath(__file__))

    print(f"{'eta':>5} {'cap':>4} {'thr':>4} {'kap':>4} | "
          f"{'MAE':>5} {'flick':>5} {'crossed':>7} {'advW':>5} {'scam':>5}")
    results = []
    for eta in (0.02, 0.05, 0.1):
        for cap in (0.5, 1.0):
            for thresh in (0.1, 0.2):
                for kappa in (2.0, 5.0):
                    r = run(eta, cap, thresh, kappa)
                    results.append((eta, cap, thresh, kappa, r))
                    print(f"{eta:>5} {cap:>4} {thresh:>4} {kappa:>4} | "
                          f"{r['mae']:>5.2f} {r['flicker']:>5} {r['n_crossed']:>5}/3 "
                          f"{r['max_adv_w']:>5.2f} {r['scam']:>5.2f}")

    def key(item):
        eta, cap, thresh, kappa, r = item
        return (r["flicker"], r["max_adv_w"], r["mae"])
    eta, cap, thresh, kappa, _ = min(results, key=key)
    print(f"\nbest: eta={eta} cap={cap} thresh={thresh} kappa={kappa}")

    best = run(eta, cap, thresh, kappa, record=True)
    cfg_label = f"η={eta}, cap={cap}, θ={thresh}, κ={kappa}"
    plot_all(best, cfg_label, outdir)

    print("\nfinal scores (best config):")
    for s in range(len(RELIABILITY)):
        tag = "seeded" if s in SEEDED else ("SCAM" if s == SCAM else "unseeded")
        print(f"  svc{s:>2} {tag:>8} rel={RELIABILITY[s]:.2f}: "
              f"score {best['finals'][s]:.2f} (true {TARGETS[s]:.2f})")
    print(f"MAE={best['mae']:.2f}  flicker={best['flicker']}  "
          f"maxAdvW={best['max_adv_w']:.2f}  scam={best['scam']:.2f}")


if __name__ == "__main__":
    main()
