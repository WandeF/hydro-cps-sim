#!/usr/bin/env python3
"""Render the dense network-parameter traceability figure from measured data."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from plot_paper_figures import (
    CONTROL,
    DOUBLE_COLUMN_MM,
    MM,
    NEUTRAL,
    apply_style,
    clean_axes,
    panel_letter,
    save_all,
)


ROOT = Path(__file__).resolve().parents[1]
DELAY_CSV = ROOT / "data/network_delay_traceability.csv"
LOSS_CSV = ROOT / "data/network_loss_traceability.csv"


def validate(delay: pd.DataFrame, loss: pd.DataFrame) -> None:
    expected_delay = list(range(0, 101, 5))
    expected_loss = list(range(10, 51, 2))
    assert delay["configured_delay_ms"].tolist() == expected_delay
    assert loss["configured_loss_percent"].tolist() == expected_loss
    assert delay["target_link"].eq("r0-r_scada").all()
    assert loss["target_link"].eq("r0-r_scada").all()
    assert delay["delay_samples"].gt(0).all()
    assert loss["tx_packets"].gt(0).all()
    assert loss["error_model_drop_packets"].le(loss["tx_packets"]).all()
    assert np.isfinite(delay["measured_delay_ms"]).all()
    assert np.isfinite(loss["measured_loss_percent"]).all()


def main() -> None:
    apply_style()
    delay = pd.read_csv(DELAY_CSV).sort_values("configured_delay_ms")
    loss = pd.read_csv(LOSS_CSV).sort_values("configured_loss_percent")
    validate(delay, loss)

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(DOUBLE_COLUMN_MM * MM, 72 * MM),
        layout="constrained",
    )
    fig.get_layout_engine().set(w_pad=2 / 25.4, h_pad=2 / 25.4, wspace=0.08)

    ax = axes[0]
    x = delay["configured_delay_ms"]
    ax.plot(x, x, color=NEUTRAL, linestyle="--", linewidth=1.0, label="Configured")
    ax.plot(
        x,
        delay["measured_delay_ms"],
        color=CONTROL,
        marker="o",
        markerfacecolor="white",
        markeredgewidth=0.8,
        label=r"Measured $r0$--$r_{scada}$",
    )
    ax.set(
        xlabel="Configured delay (ms)",
        ylabel="Link delay (ms)",
        xlim=(-3, 103),
        ylim=(-3, 103),
    )
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    clean_axes(ax)
    panel_letter(ax, "a")
    ax.legend(frameon=False, loc="upper left", handlelength=2.2)

    ax = axes[1]
    x = loss["configured_loss_percent"]
    ax.plot(x, x, color=NEUTRAL, linestyle="--", linewidth=1.0, label="Configured")
    ax.plot(
        x,
        loss["measured_loss_percent"],
        color=CONTROL,
        marker="o",
        markerfacecolor="white",
        markeredgewidth=0.8,
        label=r"Measured $r0$--$r_{scada}$",
    )
    ax.set(
        xlabel="Configured packet loss (%)",
        ylabel="Measured packet loss (%)",
        xlim=(8, 52),
        ylim=(8, 52),
    )
    ax.set_xticks([10, 20, 30, 40, 50])
    ax.set_yticks([10, 20, 30, 40, 50])
    clean_axes(ax)
    panel_letter(ax, "b")
    ax.legend(frameon=False, loc="upper left", handlelength=2.2)

    save_all(fig, "network_parameter_traceability")
    plt.close(fig)
    print("Generated network traceability figure from 21 delay and 21 loss levels.")


if __name__ == "__main__":
    main()
