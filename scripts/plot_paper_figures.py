#!/usr/bin/env python3
"""Generate publication figures from the frozen paper source-data tables.

The layout follows the production principles in Alto-R/nature-figure-craft:
final-size geometry, editable vector text, semantic colours, quiet scaffolding,
and PDF/SVG/PNG triple export.  Run with the Anaconda Python available on the
experiment workstation::

    /home/lzh/anaconda3/bin/python scripts/plot_paper_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIGURES = ROOT / "figures"
MM = 1 / 25.4
DOUBLE_COLUMN_MM = 180

# One meaning, one colour across the paper.
CONTROL = "#4C72B0"
PHYSICAL = "#DDAA33"
NETWORK = "#8172B3"
NEUTRAL = "#62666A"
LIGHT_NEUTRAL = "#E9EAEC"

BASELINE_PHYSICS = Path("/media/lzh/新加卷/论文/数据/baseline/physics.csv")
BASELINE_ACTUATORS = Path("/media/lzh/新加卷/论文/数据/baseline/actuator_state.csv")
EPYNET_PHYSICS = Path(
    "/home/lzh/MASTER/CODE/hydro-cps-sim/examples/c_town/baseline/csv/physics.csv"
)
EPYNET_ACTUATORS = Path(
    "/home/lzh/MASTER/CODE/hydro-cps-sim/examples/c_town/baseline/csv/actuator_state.csv"
)
ATTACK_ANALYSIS = Path(
    "/home/lzh/MASTER/CODE/output/advisor_experiments_20260817_cross_layer/06_analysis"
)

# A single gold/orange root encodes increasing attack intensity in trajectory
# figures; line style and marker shape provide a second, grayscale-safe channel.
ATTACK_LIGHT = "#E8C978"
ATTACK_MID = PHYSICAL
ATTACK_DARK = "#9B6A12"


def apply_style() -> None:
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "Nimbus Sans"],
            "font.size": 9.0,
            "axes.labelsize": 9.0,
            "axes.titlesize": 9.0,
            "xtick.labelsize": 9.0,
            "ytick.labelsize": 9.0,
            "legend.fontsize": 9.0,
            "axes.linewidth": 0.6,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "lines.linewidth": 1.2,
            "lines.markersize": 3.8,
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "axes.unicode_minus": False,
            "mathtext.fontset": "custom",
            "mathtext.rm": "Arial",
            "mathtext.it": "Arial:italic",
            "mathtext.bf": "Arial:bold",
            "mathtext.default": "regular",
        }
    )


def clean_axes(ax: mpl.axes.Axes, grid_axis: str = "y") -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)
    ax.grid(axis=grid_axis, linestyle="--", linewidth=0.45, alpha=0.25, color=NEUTRAL)
    ax.set_axisbelow(True)


def panel_letter(ax: mpl.axes.Axes, letter: str) -> None:
    ax.text(
        0.0,
        1.035,
        letter,
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=10.5,
        fontweight="bold",
    )


def save_all(fig: mpl.figure.Figure, stem: str) -> None:
    for kind in ("pdf", "svg", "png"):
        out_dir = FIGURES / kind
        out_dir.mkdir(parents=True, exist_ok=True)
        kwargs = {"dpi": 300} if kind == "png" else {}
        fig.savefig(out_dir / f"{stem}.{kind}", format=kind, **kwargs)


def validate_sources(attacks: pd.DataFrame, scale: pd.DataFrame) -> None:
    expected = {"mitm": 39, "dos": 10, "plc_logic": 35}
    observed = attacks.groupby("attack_type").size().to_dict()
    assert observed == expected, f"Unexpected attack matrix: {observed}"
    assert len(scale) == 8 and set(scale["plc_count"]) == {2, 4, 6, 8, 16, 32, 64, 128}
    assert np.allclose(scale["end_to_end_correct_rate"], 1.0)
    assert int(scale["pipeline_observed_records"].sum()) == 3900

    mitm = attacks.query("attack_type == 'mitm'").set_index("strength_value")
    assert mitm.loc[0.0, "modified_frame_count"] == 41
    assert mitm.loc[0.0, "control_mismatch_rate"] == 0
    plateau = mitm.loc[[3.3, 3.4, 3.5, 4.0, 4.5, 5.0]]
    assert plateau["control_mismatch_rate"].nunique() == 1
    assert plateau["tank_peak_absolute_deviation"].nunique() == 1

    dos = attacks.query("attack_type == 'dos'").set_index("strength_value")
    assert dos.loc[1.5, "communication_timeout_count"] == 3
    assert dos.loc[1.5, "control_mismatch_rate"] == 0
    assert dos.loc[2.0, "control_first_deviation_iteration"] == 47
    assert dos.loc[2.0, "pressure_first_deviation_iteration"] == 48

    plc = attacks.query("attack_type == 'plc_logic'").set_index("strength_value")
    assert plc.loc[0.0, "control_mismatch_rate"] == 0
    assert plc.loc[-3.0, "control_first_deviation_iteration"] == 20
    assert plc.loc[-3.0, "pressure_first_deviation_iteration"] == 21
    assert plc.loc[-0.1, "control_first_deviation_iteration"] == 54


def load_and_validate_baseline() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load the archived reference and platform baseline and prove exact equality."""
    for path in (BASELINE_PHYSICS, BASELINE_ACTUATORS, EPYNET_PHYSICS, EPYNET_ACTUATORS):
        assert path.is_file(), f"Missing baseline source: {path}"

    baseline_physics = pd.read_csv(BASELINE_PHYSICS).query("iteration >= 1")
    epynet_physics = pd.read_csv(EPYNET_PHYSICS).query("iteration >= 1")
    baseline_actuators = pd.read_csv(BASELINE_ACTUATORS)
    epynet_actuators = pd.read_csv(EPYNET_ACTUATORS)

    tanks = [f"T{i}" for i in range(1, 8)]
    actuators = ["PU1", "PU2", "PU4", "PU5", "PU6", "PU7", "PU8", "PU10", "PU11", "V2"]
    assert len(baseline_physics) == len(epynet_physics) == 100
    assert len(baseline_actuators) == len(epynet_actuators) == 99
    assert baseline_physics[["iteration", *tanks]].equals(epynet_physics[["iteration", *tanks]])
    assert baseline_actuators[["iteration", *actuators]].equals(
        epynet_actuators[["iteration", *actuators]]
    )
    return baseline_physics, epynet_physics, baseline_actuators, epynet_actuators


def baseline_legend_handles() -> list[Line2D]:
    return [
        Line2D([0], [0], color=CONTROL, linewidth=1.3, label="Baseline verification"),
        Line2D(
            [0],
            [0],
            color=NEUTRAL,
            marker="o",
            markerfacecolor="white",
            markeredgewidth=0.8,
            linewidth=0,
            label="Only EPYNET Python",
        ),
    ]


def plot_baseline_consistency() -> None:
    baseline_p, epynet_p, baseline_a, epynet_a = load_and_validate_baseline()
    tanks = [f"T{i}" for i in range(1, 8)]
    actuators = ["PU1", "PU2", "PU4", "PU5", "PU6", "PU7", "PU8", "PU10", "PU11", "V2"]

    fig, axes = plt.subplots(
        4,
        2,
        figsize=(DOUBLE_COLUMN_MM * MM, 132 * MM),
        sharex=True,
        layout="constrained",
    )
    fig.get_layout_engine().set(
        rect=(0, 0, 1, 0.94),
        w_pad=2 / 25.4,
        h_pad=1.5 / 25.4,
        wspace=0.06,
        hspace=0.04,
    )
    for ax, tank in zip(axes.flat, tanks):
        ax.plot(baseline_p["iteration"], baseline_p[tank], color=CONTROL, linewidth=1.3)
        ax.plot(
            epynet_p["iteration"],
            epynet_p[tank],
            color=NEUTRAL,
            marker="o",
            markerfacecolor="white",
            markeredgewidth=0.75,
            linewidth=0,
            markevery=10,
            zorder=3,
        )
        ax.set_title(f"{tank}  |  RMSE = 0 m", loc="left", pad=2)
        clean_axes(ax)
    axes[-1, -1].axis("off")
    axes[-1, -1].legend(
        handles=baseline_legend_handles(),
        frameon=False,
        loc="center",
        handlelength=2.2,
    )
    for ax in axes[-1, :1]:
        ax.set_xlabel("Hydraulic iteration")
    fig.supylabel("Tank level (m)")
    fig.suptitle(
        "Only EPYNET Python vs Baseline Verification C-Town Key Tanks",
        fontsize=10.5,
        fontweight="bold",
    )
    save_all(fig, "baseline_tank_consistency")
    plt.close(fig)

    fig, axes = plt.subplots(
        5,
        2,
        figsize=(DOUBLE_COLUMN_MM * MM, 142 * MM),
        sharex=True,
        sharey=True,
        layout="constrained",
    )
    fig.get_layout_engine().set(
        rect=(0, 0, 1, 0.88),
        w_pad=2 / 25.4,
        h_pad=1.5 / 25.4,
        wspace=0.04,
        hspace=0.04,
    )
    for ax, actuator in zip(axes.flat, actuators):
        baseline_state = baseline_a[actuator].astype(int)
        epynet_state = epynet_a[actuator].astype(int)
        ax.step(
            baseline_a["iteration"],
            baseline_state,
            where="post",
            color=CONTROL,
            linewidth=1.3,
        )
        ax.plot(
            epynet_a["iteration"],
            epynet_state,
            color=NEUTRAL,
            marker="o",
            markerfacecolor="white",
            markeredgewidth=0.75,
            linewidth=0,
            markevery=10,
            zorder=3,
        )
        ax.set_title(f"{actuator}  |  mismatch = 0", loc="left", pad=2)
        ax.set_ylim(-0.12, 1.12)
        ax.set_yticks([0, 1], ["Off", "On"])
        clean_axes(ax)
    for ax in axes[-1, :]:
        ax.set_xlabel("Control iteration")
    fig.suptitle(
        "Only EPYNET Python vs Baseline Verification C-Town Key Pumps and Valves",
        fontsize=10.5,
        fontweight="bold",
    )
    fig.legend(
        handles=baseline_legend_handles(),
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.94),
        ncol=2,
        handlelength=2.2,
        columnspacing=1.5,
    )
    save_all(fig, "baseline_actuator_consistency")
    plt.close(fig)


def plot_attack_effects() -> None:
    """Draw compact representative trajectories for the three attack families."""
    specifications = [
        {
            "source": "mitm_t7_water_level_series.csv",
            "title": "MITM Effect on T7 Water Level",
            "ylabel": "T7 water level (m)",
            "rows": 80,
            "series": [
                ("delta_x_0", r"Baseline, $\Delta x=0$", NEUTRAL, "--", None),
                ("delta_x_0p1", r"$\Delta x=0.1$", ATTACK_LIGHT, ":", "o"),
                ("delta_x_2", r"$\Delta x=2.0$", ATTACK_MID, "-.", "^"),
                (
                    "delta_x_3p3_3p4_3p5_4_4p5_5",
                    r"$\Delta x=3.3$--$5.0$",
                    ATTACK_DARK,
                    "-",
                    "s",
                ),
            ],
        },
        {
            "source": "dos_t4_scada_observed_series.csv",
            "title": "DoS Effect on SCADA-observed T4 Water Level",
            "ylabel": "SCADA-observed T4 level (m)",
            "rows": 79,
            "series": [
                ("rho_0_0p5_1", r"Baseline, $\rho=0$--$1$", NEUTRAL, "--", None),
                ("rho_1p5", r"$\rho=1.5$", ATTACK_LIGHT, ":", "o"),
                ("rho_2", r"$\rho=2.0$", ATTACK_MID, "-.", "^"),
                ("rho_5", r"$\rho=5.0$", ATTACK_DARK, "-", "s"),
            ],
        },
        {
            "source": "plc_logic_t7_series.csv",
            "title": "PLC Logic Injection Effect on T7 Water Level",
            "ylabel": "T7 water level (m)",
            "rows": 80,
            "series": [
                ("delta_h_0", r"Baseline, $\Delta h=0$", NEUTRAL, "--", None),
                ("delta_h_m0p1", r"$\Delta h=-0.1$ m", ATTACK_LIGHT, ":", "o"),
                ("delta_h_m1p5", r"$\Delta h=-1.5$ m", ATTACK_MID, "-.", "^"),
                ("delta_h_m3", r"$\Delta h=-3.0$ m", ATTACK_DARK, "-", "s"),
            ],
        },
    ]

    for item in specifications:
        source = ATTACK_ANALYSIS / item["source"]
        assert source.is_file(), f"Missing attack trajectory source: {source}"
        frame = pd.read_csv(source)
        assert len(frame) == item["rows"]
        assert frame["iteration"].tolist() == list(range(1, item["rows"] + 1))
        assert set(column for column, *_ in item["series"]).issubset(frame.columns)

        fig, ax = plt.subplots(figsize=(DOUBLE_COLUMN_MM * MM, 80 * MM))
        fig.subplots_adjust(left=0.09, right=0.985, bottom=0.18, top=0.72)
        ax.axvspan(15, 55, color=LIGHT_NEUTRAL, alpha=0.72, zorder=-3)
        ax.axvline(15, color=NEUTRAL, linestyle=":", linewidth=0.7, zorder=-1)
        ax.axvline(55, color=NEUTRAL, linestyle=":", linewidth=0.7, zorder=-1)

        handles = []
        labels = []
        for index, (column, label, color, linestyle, marker) in enumerate(item["series"]):
            (line,) = ax.plot(
                frame["iteration"],
                frame[column],
                color=color,
                linestyle=linestyle,
                linewidth=1.55 if index == 0 else 1.25,
                marker=marker,
                markerfacecolor="white" if marker else color,
                markeredgewidth=0.75,
                markevery=10,
                zorder=5 if index == 0 else 2 + index,
            )
            handles.append(line)
            labels.append(label)

        ax.text(
            35,
            5.12,
            "Attack window: iterations 15--55",
            ha="center",
            va="top",
            fontsize=8.5,
            color=NEUTRAL,
        )
        ax.set(
            xlabel="Hydraulic iteration",
            ylabel=item["ylabel"],
            xlim=(1, 80),
            ylim=(-0.08, 5.28),
        )
        ax.set_xticks([10, 20, 30, 40, 50, 60, 70, 80])
        ax.set_yticks([0, 1, 2, 3, 4, 5])
        clean_axes(ax)
        fig.suptitle(item["title"], y=0.965, fontsize=10.5, fontweight="bold")
        fig.legend(
            handles,
            labels,
            frameon=False,
            loc="upper center",
            bbox_to_anchor=(0.5, 0.89),
            ncol=4,
            handlelength=2.5,
            columnspacing=1.4,
        )
        save_all(fig, Path(item["source"]).stem)
        plt.close(fig)


def plot_propagation_timeline(attacks: pd.DataFrame) -> None:
    indexed = attacks.set_index("experiment_id")
    rows = [
        ("MITM, $\\Delta x=5$", "mitm_bias_5", 15),
        ("DoS, $\\rho=5$", "dos_rho_5", 15),
        ("PLC logic, $\\Delta h=-3$", "plc_threshold_shift_m3", None),
    ]
    timeline = []
    for label, experiment_id, communication_iteration in rows:
        row = indexed.loc[experiment_id]
        attack = int(row["attack_start_iteration"])
        control = int(row["control_first_deviation_iteration"])
        physical = int(
            np.nanmin(
                [row["tank_first_deviation_iteration"], row["pressure_first_deviation_iteration"]]
            )
        )
        communication = communication_iteration if communication_iteration is not None else np.nan
        timeline.append(
            {
                "label": label,
                "attack": attack,
                "communication": communication,
                "control": control,
                "physical": physical,
                "pre_control": control - attack,
                "physical_delay": physical - control,
            }
        )
    frame = pd.DataFrame(timeline)

    fig, ax = plt.subplots(figsize=(DOUBLE_COLUMN_MM * MM, 58 * MM), layout="constrained")
    fig.get_layout_engine().set(w_pad=2 / 25.4, h_pad=2 / 25.4)
    y = np.arange(len(frame))[::-1]
    ax.barh(y, frame["pre_control"], left=0, height=0.42, color=CONTROL, edgecolor="white", linewidth=0.5)
    ax.barh(
        y,
        frame["physical_delay"],
        left=frame["pre_control"],
        height=0.42,
        color=PHYSICAL,
        edgecolor="white",
        linewidth=0.5,
    )
    ax.scatter(np.zeros(len(y)), y, marker="D", s=22, color=NEUTRAL, zorder=4)
    ax.scatter(frame["pre_control"], y, marker="o", s=25, color=CONTROL, edgecolor="white", linewidth=0.5, zorder=4)
    ax.scatter(
        frame["pre_control"] + frame["physical_delay"],
        y,
        marker="s",
        s=25,
        color=PHYSICAL,
        edgecolor="white",
        linewidth=0.5,
        zorder=4,
    )
    ax.set_yticks(y, frame["label"])
    ax.set_xlabel("Hydraulic iterations since attack onset")
    ax.set_xlim(-0.8, 34.5)
    ax.set_xticks([0, 5, 10, 15, 20, 25, 30, 33])
    clean_axes(ax, grid_axis="x")
    handles = [
        Line2D([0], [0], marker="D", color="none", markerfacecolor=NEUTRAL, markeredgecolor=NEUTRAL, label="Attack onset"),
        Line2D([0], [0], marker="o", color=CONTROL, markerfacecolor=CONTROL, label="First control deviation"),
        Line2D([0], [0], marker="s", color=PHYSICAL, markerfacecolor=PHYSICAL, label="First physical deviation"),
    ]
    ax.legend(handles=handles, frameon=False, loc="upper right", ncol=3, handlelength=1.6, columnspacing=1.2)
    save_all(fig, "propagation_timeline")
    plt.close(fig)


def plot_scalability(scale: pd.DataFrame) -> None:
    scale = scale.sort_values("plc_count")
    x = scale["plc_count"]
    fig, axes = plt.subplots(
        2,
        2,
        figsize=(DOUBLE_COLUMN_MM * MM, 103 * MM),
        layout="constrained",
    )
    fig.get_layout_engine().set(w_pad=2 / 25.4, h_pad=2 / 25.4, wspace=0.08, hspace=0.08)

    ax = axes[0, 0]
    ax.plot(x, scale["mean_cycle_time_sec"], color=CONTROL, marker="o")
    ax.set(ylabel="Mean cycle time (s)", ylim=(0, 0.56))
    clean_axes(ax)
    panel_letter(ax, "a")

    ax = axes[0, 1]
    ax.plot(x, scale["pair_updates_per_sec"], color=NETWORK, marker="o")
    ax.set(ylabel=r"Pair updates (s$^{-1}$)", ylim=(0, 132))
    clean_axes(ax)
    panel_letter(ax, "b")

    ax = axes[1, 0]
    ax.plot(x, scale["peak_aggregate_memory_mb"] / 1000, color=PHYSICAL, marker="s")
    ax.set(xlabel="PLC instances", ylabel="Peak RSS (GB)", ylim=(0, 10.8))
    clean_axes(ax)
    panel_letter(ax, "c")

    ax = axes[1, 1]
    ax.plot(x, scale["startup_time_sec"], color=CONTROL, marker="o")
    ax.set(xlabel="PLC instances", ylabel="Startup time (s)", ylim=(0, 400))
    clean_axes(ax)
    panel_letter(ax, "d")

    for ax in axes.flat:
        ax.set_xscale("log", base=2)
        ax.set_xticks(x, [str(v) for v in x])
    for ax in axes[0, :]:
        ax.tick_params(labelbottom=False)

    save_all(fig, "scalability_response")
    plt.close(fig)


def main() -> None:
    apply_style()
    attacks = pd.read_csv(DATA / "attack_intensity_summary.csv")
    scale = pd.read_csv(DATA / "scalability_summary.csv")
    validate_sources(attacks, scale)
    plot_baseline_consistency()
    plot_attack_effects()
    plot_propagation_timeline(attacks)
    plot_scalability(scale)
    print("Validated 84 attack points and 8 scalability points.")
    print("Validated 700 tank values and 990 selected pump/valve states against EPYNET Python.")
    print("Generated 7 figures in PDF, SVG, and 300-dpi PNG at 180 mm final width.")


if __name__ == "__main__":
    main()
