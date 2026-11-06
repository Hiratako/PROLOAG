from __future__ import annotations

from collections.abc import Callable, Sequence
from math import ceil, isfinite
from pathlib import Path
from typing import Any

import matplotlib
import matplotlib.dates as mdates
from matplotlib import font_manager


_CONFIGURED = False
_FONT_FAMILY: str | None = None


_LIGHT_SERIES = (
    {"color": "#005A9C", "linestyle": "-", "marker": "o"},
    {"color": "#5B5BD6", "linestyle": "--", "marker": "^"},
    {"color": "#007A5E", "linestyle": "-.", "marker": "D"},
    {"color": "#9A6700", "linestyle": ":", "marker": "s"},
    {"color": "#C2410C", "linestyle": (0, (5, 1)), "marker": "X"},
    {"color": "#9D3C8C", "linestyle": (0, (3, 1, 1, 1)), "marker": "P"},
)

_DARK_SERIES = (
    {"color": "#39D5FF", "linestyle": "-", "marker": "o"},
    {"color": "#9DA7FF", "linestyle": "--", "marker": "^"},
    {"color": "#56F0A6", "linestyle": "-.", "marker": "D"},
    {"color": "#FFD166", "linestyle": ":", "marker": "s"},
    {"color": "#FF8A5B", "linestyle": (0, (5, 1)), "marker": "X"},
    {"color": "#F08BC3", "linestyle": (0, (3, 1, 1, 1)), "marker": "P"},
)


def configure_japanese_font() -> None:
    """Configure Matplotlib to render Japanese labels on local Windows PCs."""
    global _CONFIGURED, _FONT_FAMILY
    if _CONFIGURED:
        if _FONT_FAMILY:
            matplotlib.rcParams["font.family"] = _FONT_FAMILY
        matplotlib.rcParams["axes.unicode_minus"] = False
        return

    try:
        import japanize_matplotlib  # noqa: F401
    except ImportError:
        pass

    font_files = [
        ("Yu Gothic", Path("C:/Windows/Fonts/YuGothR.ttc")),
        ("Meiryo", Path("C:/Windows/Fonts/meiryo.ttc")),
        ("MS Gothic", Path("C:/Windows/Fonts/msgothic.ttc")),
    ]
    for family, path in font_files:
        if path.exists():
            try:
                font_manager.fontManager.addfont(str(path))
                matplotlib.rcParams["font.family"] = family
                _FONT_FAMILY = family
                break
            except RuntimeError:
                continue
    else:
        available = {font.name for font in font_manager.fontManager.ttflist}
        for family in ("IPAexGothic", "Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP"):
            if family in available:
                matplotlib.rcParams["font.family"] = family
                _FONT_FAMILY = family
                break

    matplotlib.rcParams["axes.unicode_minus"] = False
    _CONFIGURED = True


def configure_plot_theme(dark: bool) -> None:
    """Apply the active UI theme to Matplotlib defaults for the current rerun."""
    if dark:
        colors = {
            "figure": "#06111F",
            "axes": "#0B1728",
            "text": "#EAF6FF",
            "muted": "#A8C0D4",
            "edge": "#29445D",
            "grid": "#29445D",
        }
    else:
        colors = {
            "figure": "#FFFFFF",
            "axes": "#FFFFFF",
            "text": "#152238",
            "muted": "#667085",
            "edge": "#D9E2EC",
            "grid": "#D5DEE8",
        }
    matplotlib.rcParams.update(
        {
            "figure.facecolor": colors["figure"],
            "savefig.facecolor": colors["figure"],
            "axes.facecolor": colors["axes"],
            "axes.edgecolor": colors["edge"],
            "axes.labelcolor": colors["text"],
            "axes.titlecolor": colors["text"],
            "text.color": colors["text"],
            "xtick.color": colors["muted"],
            "ytick.color": colors["muted"],
            "grid.color": colors["grid"],
            "legend.facecolor": colors["axes"],
            "legend.edgecolor": colors["edge"],
            "legend.labelcolor": colors["text"],
            "axes.unicode_minus": False,
        }
    )


def chart_series_style(palette: dict[str, Any], index: int) -> dict[str, Any]:
    """Return a theme-aware line style with redundant visual encoding."""
    styles = _DARK_SERIES if palette.get("mode") == "dark" else _LIGHT_SERIES
    return dict(styles[index % len(styles)])


def adaptive_markevery(values: Sequence[Any] | int, max_markers: int = 18) -> int:
    """Keep markers legible when a time series contains many observations."""
    count = values if isinstance(values, int) else len(values)
    return max(1, ceil(max(count, 1) / max(max_markers, 1)))


def plot_styled_line(
    ax: Any,
    x: Sequence[Any],
    y: Sequence[Any],
    *,
    palette: dict[str, Any],
    label: str | None = None,
    style_index: int = 0,
    color: str | None = None,
    linestyle: Any | None = None,
    marker: str | None = None,
    show_markers: bool = True,
    max_markers: int = 18,
    linewidth: float = 2.0,
    markersize: float = 5.0,
    alpha: float = 1.0,
    zorder: int = 3,
) -> Any:
    """Draw the shared PROLOAG line treatment on an existing axes."""
    style = chart_series_style(palette, style_index)
    selected_marker = marker if marker is not None else style["marker"]
    if not show_markers:
        selected_marker = None
    line = ax.plot(
        x,
        y,
        label=label,
        color=color or style["color"],
        linestyle=linestyle if linestyle is not None else style["linestyle"],
        linewidth=linewidth,
        marker=selected_marker,
        markersize=markersize if selected_marker else 0,
        markeredgecolor=palette.get("panel", palette.get("card", "#FFFFFF")),
        markeredgewidth=1.0,
        markevery=adaptive_markevery(x, max_markers) if selected_marker else None,
        alpha=alpha,
        zorder=zorder,
    )[0]
    return line


def style_chart_axis(
    ax: Any,
    palette: dict[str, Any],
    *,
    title: str | None = None,
    title_color: str | None = None,
    ylabel: str | None = None,
    grid_axis: str = "y",
    score_range: bool = False,
    reference_value: float | None = None,
) -> None:
    """Apply the common lightweight axes treatment without changing data semantics."""
    if title is not None:
        ax.set_title(
            title,
            loc="left",
            fontsize=10.5,
            fontweight="bold",
            color=title_color or palette.get("ink"),
        )
    if ylabel is not None:
        ax.set_ylabel(ylabel)
    if score_range:
        ax.set_ylim(0.75, 5.25)
        ax.set_yticks([1, 2, 3, 4, 5])
        if reference_value is None:
            reference_value = 3
    if reference_value is not None:
        ax.axhline(
            reference_value,
            color=palette.get("grid", "#D5DEE8"),
            linewidth=1,
            linestyle=(0, (3, 3)),
            zorder=0,
        )
    ax.set_axisbelow(True)
    ax.grid(False)
    ax.grid(
        axis=grid_axis,
        color=palette.get("grid", "#D5DEE8"),
        linewidth=0.8,
        alpha=0.78,
    )
    ax.tick_params(axis="both", which="both", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)


def style_date_axis(ax: Any, *, minticks: int = 4, maxticks: int = 8) -> None:
    """Use a compact, locale-neutral date axis and a small horizontal margin."""
    locator = mdates.AutoDateLocator(minticks=minticks, maxticks=maxticks)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.margins(x=0.02)


def style_legend(
    ax: Any,
    *,
    location: str = "upper left",
    columns: int = 1,
    outside: bool = False,
) -> Any | None:
    """Render a compact frameless legend only when labelled artists exist."""
    handles, labels = ax.get_legend_handles_labels()
    if not labels:
        return None
    kwargs: dict[str, Any] = {
        "loc": location,
        "ncol": columns,
        "frameon": False,
        "fontsize": 8.5,
        "handlelength": 2.4,
        "handletextpad": 0.5,
        "columnspacing": 1.2,
    }
    if outside:
        kwargs.update({"loc": "lower right", "bbox_to_anchor": (1.0, 1.01), "borderaxespad": 0})
    return ax.legend(**kwargs)


def plot_time_bars(
    ax: Any,
    x: Sequence[Any],
    values: Sequence[float],
    *,
    palette: dict[str, Any],
    color: str,
    label: str | None = None,
    width: float = 0.72,
    alpha: float = 0.84,
    accent_color: str | None = None,
    highlight_indices: Sequence[int] = (),
) -> Any:
    """Draw dense time bars with theme-aware edges and optional sparse highlights."""
    colors = [color] * len(values)
    if accent_color:
        for index in highlight_indices:
            if -len(colors) <= index < len(colors):
                colors[index] = accent_color
    container = ax.bar(
        x,
        values,
        width=width,
        color=colors,
        alpha=alpha,
        edgecolor=palette.get("panel", palette.get("card", "#FFFFFF")),
        linewidth=0.8,
        label=label,
        zorder=2,
    )
    return container


def add_bar_value_labels(
    ax: Any,
    container: Any,
    *,
    palette: dict[str, Any],
    formatter: str | Callable[[float], str] = "{:.1f}",
    indices: Sequence[int] | None = None,
    orientation: str = "vertical",
    padding: int = 3,
) -> None:
    """Add selective direct labels to a Matplotlib bar container."""
    selected = set(indices) if indices is not None else set(range(len(container.patches)))
    labels: list[str] = []
    for index, patch in enumerate(container.patches):
        value = patch.get_height() if orientation == "vertical" else patch.get_width()
        if index not in selected:
            labels.append("")
        elif callable(formatter):
            labels.append(formatter(float(value)))
        else:
            labels.append(formatter.format(float(value)))
    ax.bar_label(
        container,
        labels=labels,
        padding=padding,
        fontsize=8,
        color=palette.get("muted", palette.get("ink")),
    )


def plot_rounded_category_bars(
    ax: Any,
    labels: Sequence[str],
    values: Sequence[float],
    *,
    palette: dict[str, Any],
    colors: str | Sequence[str],
    orientation: str = "horizontal",
    linewidth: float = 22,
    formatter: str | Callable[[float], str] | None = None,
    missing_label: str = "データなし",
    alpha: float = 0.9,
) -> list[Any | None]:
    """Draw sparse category bars with rounded caps using screen-space line widths."""
    positions = list(range(len(labels)))
    value_list = [float(value) for value in values]
    color_list = [colors] * len(labels) if isinstance(colors, str) else list(colors)
    artists: list[Any | None] = []
    if orientation == "vertical":
        for position, value, color in zip(positions, value_list, color_list):
            artists.append(
                ax.vlines(position, 0, value, color=color, linewidth=linewidth, capstyle="round", alpha=alpha, zorder=3)
                if isfinite(value)
                else None
            )
        ax.set_xticks(positions, labels)
    else:
        for position, value, color in zip(positions, value_list, color_list):
            artists.append(
                ax.hlines(position, 0, value, color=color, linewidth=linewidth, capstyle="round", alpha=alpha, zorder=3)
                if isfinite(value)
                else None
            )
        ax.set_yticks(positions, labels)
        ax.invert_yaxis()

    if formatter is not None and value_list:
        finite_values = [value for value in value_list if isfinite(value)]
        span = (
            max(max(finite_values) - min(finite_values), max(abs(value) for value in finite_values), 1.0)
            if finite_values
            else 1.0
        )
        pad = span * 0.02
        for position, value in zip(positions, value_list):
            if not isfinite(value):
                if orientation == "vertical":
                    ax.text(position, pad, missing_label, ha="center", va="bottom", fontsize=8, color=palette.get("muted"))
                else:
                    ax.text(pad, position, missing_label, ha="left", va="center", fontsize=8, color=palette.get("muted"))
                continue
            text = formatter(value) if callable(formatter) else formatter.format(value)
            if orientation == "vertical":
                ax.text(position, value + pad, text, ha="center", va="bottom", fontsize=8, color=palette.get("muted"))
            else:
                positive = value >= 0
                ax.text(
                    value + (pad if positive else -pad),
                    position,
                    text,
                    ha="left" if positive else "right",
                    va="center",
                    fontsize=8,
                    color=palette.get("muted"),
                )
    return artists
