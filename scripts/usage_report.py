#!/usr/bin/env python3
"""Generate a visual usage report for Claude Code sessions.

Produces a multi-panel dashboard image showing:
- Daily token usage by model
- Per-project cost distribution
- Hourly activity heatmap
- Session timeline

Usage:
    python scripts/usage_report.py [--since YYYYMMDD] [--until YYYYMMDD] [-o output.png]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Matplotlib setup (must be before any pyplot import)
# ---------------------------------------------------------------------------
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.gridspec import GridSpec

# Try to use a CJK-capable font; fall back gracefully
_FONT_FAMILY = "sans-serif"
for _candidate in ("Noto Serif CJK SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei"):
    try:
        from matplotlib.font_manager import FontProperties

        _fp = FontProperties(family=_candidate)
        if _fp.get_name() != _candidate:
            continue
        _FONT_FAMILY = _candidate
        break
    except Exception:
        continue

plt.rcParams.update(
    {
        "font.family": _FONT_FAMILY,
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "figure.facecolor": "#ffffff",
        "axes.facecolor": "#f8f9fa",
        "text.color": "#2d3436",
        "axes.labelcolor": "#2d3436",
        "xtick.color": "#636e72",
        "ytick.color": "#636e72",
        "axes.edgecolor": "#dfe6e9",
        "grid.color": "#dfe6e9",
        "grid.alpha": 0.7,
    }
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HKT = timezone(timedelta(hours=8))
MODEL_COLORS = {
    "claude-opus-4-6": "#6c5ce7",
    "claude-opus-4-5-20251101": "#a29bfe",
    "claude-sonnet-4-6": "#0984e3",
    "claude-haiku-4-5-20251001": "#00b894",
}
MODEL_LABELS = {
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-5-20251101": "Opus 4.5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
}
PROJECT_COLORS = [
    "#6c5ce7",
    "#0984e3",
    "#00b894",
    "#fdcb6e",
    "#e17055",
    "#a29bfe",
    "#74b9ff",
]

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def run_ccusage(subcmd: str, since: str, until: str, extra_flags: list[str] | None = None) -> dict:
    """Run ccusage CLI and return parsed JSON."""
    cmd = ["ccusage", subcmd, "--since", since, "--until", until, "--json"]
    if extra_flags:
        cmd.extend(extra_flags)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"ccusage error: {result.stderr}", file=sys.stderr)
        return {}
    return json.loads(result.stdout)


def collect_session_timestamps(since_date: str, until_date: str) -> dict:
    """Parse JSONL session files to extract message timestamps by type.

    Returns dict with keys: user_turns, assistant_turns, by_hour, sessions.
    """
    target_dates = set()
    dt = datetime.strptime(since_date, "%Y%m%d")
    dt_end = datetime.strptime(until_date, "%Y%m%d")
    while dt < dt_end:
        target_dates.add(dt.strftime("%Y-%m-%d"))
        dt += timedelta(days=1)

    hourly_user = defaultdict(int)
    hourly_assistant = defaultdict(int)
    daily_user: dict[str, list[datetime]] = defaultdict(list)
    daily_all: dict[str, list[datetime]] = defaultdict(list)

    # Scan all project directories for JSONL files
    if not CLAUDE_PROJECTS_DIR.exists():
        return {"hourly_user": hourly_user, "hourly_assistant": hourly_assistant,
                "daily_user": daily_user, "daily_all": daily_all, "sessions": []}

    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for fpath in project_dir.glob("*.jsonl"):
            try:
                with open(fpath) as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            ts = entry.get("timestamp")
                            if not ts:
                                continue
                            dt_parsed = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            )
                            dt_hkt = dt_parsed.astimezone(HKT)
                            date_str = dt_hkt.strftime("%Y-%m-%d")
                            if date_str not in target_dates:
                                continue

                            etype = entry.get("type", "")
                            if etype == "user":
                                hourly_user[(date_str, dt_hkt.hour)] += 1
                                daily_user[date_str].append(dt_hkt)
                            elif etype == "assistant":
                                hourly_assistant[(date_str, dt_hkt.hour)] += 1

                            if etype in ("user", "assistant"):
                                daily_all[date_str].append(dt_hkt)
                        except (json.JSONDecodeError, KeyError, ValueError):
                            continue
            except (FileNotFoundError, PermissionError):
                continue

    # Compute sessions (gap > 30 min = new session)
    sessions: list[dict] = []
    for date in sorted(daily_user.keys()):
        msgs = sorted(daily_user[date])
        if not msgs:
            continue
        session_start = msgs[0]
        prev = msgs[0]
        for m in msgs[1:]:
            gap = (m - prev).total_seconds() / 60
            if gap > 30:
                sessions.append({
                    "date": date,
                    "start": session_start,
                    "end": prev,
                    "duration_min": (prev - session_start).total_seconds() / 60 + 5,
                })
                session_start = m
            prev = m
        sessions.append({
            "date": date,
            "start": session_start,
            "end": prev,
            "duration_min": (prev - session_start).total_seconds() / 60 + 5,
        })

    return {
        "hourly_user": dict(hourly_user),
        "hourly_assistant": dict(hourly_assistant),
        "daily_user": {k: v for k, v in daily_user.items()},
        "daily_all": {k: v for k, v in daily_all.items()},
        "sessions": sessions,
    }


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------


def render_report(
    daily_data: dict,
    blocks_data: dict,
    instances_data: dict,
    ts_data: dict,
    since: str,
    until: str,
    output_path: str,
) -> str:
    """Render multi-panel dashboard and save to output_path."""
    fig = plt.figure(figsize=(16, 24))
    gs = GridSpec(5, 2, figure=fig, hspace=0.35, wspace=0.3,
                  height_ratios=[1, 1, 0.7, 1.2, 1.2])
    fig.suptitle(
        f"Claude Code Usage Report\n{since[:4]}-{since[4:6]}-{since[6:]} to "
        f"{until[:4]}-{until[4:6]}-{until[6:]}",
        fontsize=18,
        fontweight="bold",
        color="#2d3436",
        y=0.98,
    )

    # ---- Panel 1: Daily token usage by model (stacked bar) ----
    ax1 = fig.add_subplot(gs[0, 0])
    _draw_daily_tokens(ax1, daily_data)

    # ---- Panel 2: Daily cost by model (stacked bar) ----
    ax2 = fig.add_subplot(gs[0, 1])
    _draw_daily_cost(ax2, daily_data)

    # ---- Panel 3: Per-project cost pie ----
    ax3 = fig.add_subplot(gs[1, 0])
    _draw_project_pie(ax3, instances_data)

    # ---- Panel 4: Model cost distribution pie ----
    ax4 = fig.add_subplot(gs[1, 1])
    _draw_model_pie(ax4, daily_data)

    # ---- Panel 5: Daily summary stats (full width) ----
    ax_stats = fig.add_subplot(gs[2, :])
    _draw_daily_summary(ax_stats, ts_data)

    # ---- Panel 6: Active/Idle timeline (full width) ----
    ax5 = fig.add_subplot(gs[3, :])
    _draw_active_idle_timeline(ax5, ts_data)

    # ---- Panel 7: Hourly token heatmap (full width) ----
    ax6 = fig.add_subplot(gs[4, :])
    _draw_hourly_heatmap(ax6, ts_data)

    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path


def _draw_daily_tokens(ax: plt.Axes, daily_data: dict) -> None:
    """Stacked bar chart of daily tokens by model."""
    days = daily_data.get("daily", [])
    if not days:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    dates = [d["date"] for d in days]
    x = range(len(dates))

    # Collect all models
    all_models = set()
    for d in days:
        for mb in d.get("modelBreakdowns", []):
            all_models.add(mb["modelName"])

    bottom = [0.0] * len(dates)
    for model in sorted(all_models):
        values = []
        for d in days:
            val = 0
            for mb in d.get("modelBreakdowns", []):
                if mb["modelName"] == model:
                    val = mb.get("cacheReadTokens", 0) + mb.get("cacheCreationTokens", 0) + mb.get("inputTokens", 0) + mb.get("outputTokens", 0)
            values.append(val / 1e6)

        color = MODEL_COLORS.get(model, "#888888")
        label = MODEL_LABELS.get(model, model.split("-")[1].title())
        ax.bar(x, values, bottom=bottom, color=color, label=label, width=0.6)
        bottom = [b + v for b, v in zip(bottom, values)]

    ax.set_xticks(list(x))
    ax.set_xticklabels([d[-5:] for d in dates], rotation=0)
    ax.set_ylabel("Tokens (millions)")
    ax.set_title("Daily Token Usage by Model")
    ax.legend(fontsize=9, loc="upper right")
    ax.grid(axis="y", alpha=0.3)

    # Add total labels on top
    for i, total in enumerate(bottom):
        ax.text(i, total + total * 0.02, f"{total:.0f}M", ha="center", va="bottom",
                fontsize=9, color="#2d3436")


def _draw_daily_cost(ax: plt.Axes, daily_data: dict) -> None:
    """Stacked bar chart of daily cost by model."""
    days = daily_data.get("daily", [])
    if not days:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    dates = [d["date"] for d in days]
    x = range(len(dates))

    all_models = set()
    for d in days:
        for mb in d.get("modelBreakdowns", []):
            all_models.add(mb["modelName"])

    bottom = [0.0] * len(dates)
    for model in sorted(all_models):
        values = []
        for d in days:
            val = 0
            for mb in d.get("modelBreakdowns", []):
                if mb["modelName"] == model:
                    val = mb.get("cost", 0)
            values.append(val)

        color = MODEL_COLORS.get(model, "#888888")
        label = MODEL_LABELS.get(model, model.split("-")[1].title())
        ax.bar(x, values, bottom=bottom, color=color, label=label, width=0.6)
        bottom = [b + v for b, v in zip(bottom, values)]

    ax.set_xticks(list(x))
    ax.set_xticklabels([d[-5:] for d in dates], rotation=0)
    ax.set_ylabel("Cost (USD)")
    ax.set_title("Daily Cost by Model")
    ax.legend(fontsize=9, loc="upper right")
    ax.grid(axis="y", alpha=0.3)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("$%.0f"))

    for i, total in enumerate(bottom):
        ax.text(i, total + total * 0.02, f"${total:.0f}", ha="center", va="bottom",
                fontsize=9, color="#2d3436")


def _draw_project_pie(ax: plt.Axes, instances_data: dict) -> None:
    """Pie chart of cost by project."""
    projects_raw = instances_data.get("projects", {})
    if not projects_raw:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    home_slug = f"-home-{os.environ.get('USER', 'user')}-"
    project_costs: dict[str, float] = {}
    for proj_id, days in projects_raw.items():
        name = proj_id.replace(f"{home_slug}Desktop-", "").replace(home_slug, "~/")
        total = sum(d.get("totalCost", 0) for d in days)
        if total > 0.1:
            project_costs[name] = total

    if not project_costs:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    # Sort by cost descending
    sorted_projects = sorted(project_costs.items(), key=lambda x: -x[1])
    labels = [p[0] for p in sorted_projects]
    values = [p[1] for p in sorted_projects]
    colors = PROJECT_COLORS[: len(labels)]

    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct=lambda pct: f"${sum(values)*pct/100:.0f}\n({pct:.0f}%)" if pct > 3 else "",
        colors=colors,
        startangle=90,
        textprops={"color": "#2d3436", "fontsize": 9},
    )
    ax.legend(
        wedges, labels, loc="center left", bbox_to_anchor=(0.85, 0.5),
        fontsize=9, framealpha=0.3,
    )
    ax.set_title("Cost by Project")


def _draw_model_pie(ax: plt.Axes, daily_data: dict) -> None:
    """Pie chart of total cost by model."""
    days = daily_data.get("daily", [])
    model_totals: dict[str, float] = defaultdict(float)
    for d in days:
        for mb in d.get("modelBreakdowns", []):
            model_totals[mb["modelName"]] += mb.get("cost", 0)

    if not model_totals:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    sorted_models = sorted(model_totals.items(), key=lambda x: -x[1])
    labels = [MODEL_LABELS.get(m, m) for m, _ in sorted_models]
    values = [v for _, v in sorted_models]
    colors = [MODEL_COLORS.get(m, "#888888") for m, _ in sorted_models]

    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct=lambda pct: f"${sum(values)*pct/100:.0f}\n({pct:.0f}%)" if pct > 2 else "",
        colors=colors,
        startangle=90,
        textprops={"color": "#2d3436", "fontsize": 9},
    )
    ax.legend(
        wedges, labels, loc="center left", bbox_to_anchor=(0.85, 0.5),
        fontsize=9, framealpha=0.3,
    )
    ax.set_title("Cost by Model (Total)")


def _draw_daily_summary(ax: plt.Axes, ts_data: dict) -> None:
    """Summary stats table: active hours, idle hours, sessions per day."""
    ax.axis("off")
    sessions = ts_data.get("sessions", [])
    daily_user = ts_data.get("daily_user", {})
    if not sessions:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    dates = sorted(set(s["date"] for s in sessions))

    # Table data
    headers = ["Date", "Sessions", "Active Time", "Idle Time", "User Turns", "Longest Session"]
    rows = []
    for date in dates:
        day_sessions = [s for s in sessions if s["date"] == date]
        active_min = sum(s["duration_min"] for s in day_sessions)
        idle_min = 24 * 60 - active_min
        user_turns = len(daily_user.get(date, []))
        longest = max((s["duration_min"] for s in day_sessions), default=0)
        rows.append([
            date,
            str(len(day_sessions)),
            f"{active_min / 60:.1f}h ({active_min:.0f}m)",
            f"{idle_min / 60:.1f}h",
            str(user_turns),
            f"{longest:.0f}m ({longest / 60:.1f}h)",
        ])

    table = ax.table(
        cellText=rows,
        colLabels=headers,
        cellLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.6)

    # Style header
    for j in range(len(headers)):
        cell = table[0, j]
        cell.set_facecolor("#6c5ce7")
        cell.set_text_props(color="#ffffff", fontweight="bold")
    # Style rows
    for i in range(1, len(rows) + 1):
        for j in range(len(headers)):
            cell = table[i, j]
            cell.set_facecolor("#f0f0f5" if i % 2 == 0 else "#ffffff")
            cell.set_edgecolor("#dfe6e9")
            # Highlight active time column in green
            if j == 2:
                cell.set_text_props(color="#00b894", fontweight="bold")
            # Highlight idle time in gray
            elif j == 3:
                cell.set_text_props(color="#b2bec3")

    ax.set_title("Daily Collaboration Summary", fontsize=13, fontweight="bold", pad=15)


def _draw_active_idle_timeline(ax: plt.Axes, ts_data: dict) -> None:
    """24h timeline per day showing active (colored) vs idle (gray) periods."""
    import numpy as np

    hourly = ts_data.get("hourly_user", {})
    sessions = ts_data.get("sessions", [])
    if not hourly and not sessions:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    dates = sorted(set(d for d, _ in hourly.keys()))
    if not dates:
        dates = sorted(set(s["date"] for s in sessions))

    bar_height = 0.6
    idle_color = "#ecf0f1"
    active_color = "#6c5ce7"
    active_high = "#e17055"

    for row_idx, date in enumerate(reversed(dates)):
        y = row_idx

        # Draw full 24h idle background
        ax.barh(y, 24, left=0, height=bar_height, color=idle_color,
                edgecolor="#dfe6e9", linewidth=0.5)

        # Overlay active hours with intensity-based color
        max_turns = max((hourly.get((date, h), 0) for h in range(24)), default=1) or 1
        active_hours = 0
        for h in range(24):
            turns = hourly.get((date, h), 0)
            if turns > 0:
                active_hours += 1
                # Color intensity based on turns
                intensity = min(turns / max_turns, 1.0)
                # Blend from active_color to active_high
                r1, g1, b1 = 0.424, 0.361, 0.906  # #6c5ce7
                r2, g2, b2 = 0.882, 0.439, 0.333  # #e17055
                r = r1 + (r2 - r1) * intensity
                g = g1 + (g2 - g1) * intensity
                b = b1 + (b2 - b1) * intensity
                ax.barh(y, 1, left=h, height=bar_height, color=(r, g, b), alpha=0.9)

                # Show turn count for high-activity hours
                if turns >= 50:
                    ax.text(h + 0.5, y, str(turns), ha="center", va="center",
                            fontsize=6, color="#ffffff", fontweight="bold")

        # Day label and stats on right
        day_sessions = [s for s in sessions if s["date"] == date]
        active_min = sum(s["duration_min"] for s in day_sessions)
        idle_hours = 24 - active_hours
        ax.text(24.5, y + 0.12, f"{active_hours}h active", ha="left", va="center",
                fontsize=10, color="#6c5ce7", fontweight="bold")
        ax.text(24.5, y - 0.12, f"{idle_hours}h idle", ha="left", va="center",
                fontsize=9, color="#b2bec3")

    ax.set_yticks(range(len(dates)))
    ax.set_yticklabels(list(reversed(dates)), fontsize=10)
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 25)], fontsize=8)
    ax.set_xlabel("Hour (HKT)")
    ax.set_title("Active vs Idle Timeline  (gray = no activity, purple/red = active)", fontsize=13, fontweight="bold")
    ax.grid(axis="x", alpha=0.2, linewidth=0.5)
    ax.set_axisbelow(True)

    # Add legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=idle_color, edgecolor="#dfe6e9", label="Idle (no tokens)"),
        Patch(facecolor=active_color, label="Active (low)"),
        Patch(facecolor=active_high, label="Active (high)"),
    ]
    ax.legend(handles=legend_elements, loc="upper right", fontsize=9, framealpha=0.8)


def _draw_hourly_heatmap(ax: plt.Axes, ts_data: dict) -> None:
    """Heatmap of hourly activity (user turns) with clear zero distinction."""
    import numpy as np

    hourly = ts_data.get("hourly_user", {})
    if not hourly:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    dates = sorted(set(d for d, _ in hourly.keys()))
    hours = list(range(24))

    # Build matrix
    matrix = []
    for date in dates:
        row = [hourly.get((date, h), 0) for h in hours]
        matrix.append(row)

    data = np.array(matrix, dtype=float)

    # Use masked array so zeros appear as distinct background
    masked_data = np.ma.masked_where(data == 0, data)

    # Custom colormap: white for zero, then gradient for activity
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list(
        "activity", ["#dfe6e9", "#6c5ce7", "#e17055", "#d63031"], N=256
    )
    cmap.set_bad(color="#f5f6fa")  # Masked (zero) cells

    im = ax.imshow(
        masked_data,
        aspect="auto",
        cmap=cmap,
        interpolation="nearest",
        vmin=1,
    )

    ax.set_yticks(range(len(dates)))
    ax.set_yticklabels(dates)
    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h:02d}" for h in hours], fontsize=8)
    ax.set_xlabel("Hour (HKT)")
    ax.set_title("Hourly Activity Heatmap  (white = idle, colored = active)")

    # Add value annotations
    for i in range(len(dates)):
        for j in range(24):
            val = data[i, j]
            if val > 0:
                color = "#2d3436" if val < data.max() * 0.5 else "#ffffff"
                ax.text(j, i, str(int(val)), ha="center", va="center",
                        fontsize=7, color=color)
            else:
                ax.text(j, i, "-", ha="center", va="center",
                        fontsize=7, color="#b2bec3")

    cbar = plt.colorbar(im, ax=ax, shrink=0.6, pad=0.02)
    cbar.ax.tick_params(labelsize=8)
    cbar.set_label("User Turns", fontsize=9)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Claude Code usage report")
    parser.add_argument(
        "--since",
        default=(datetime.now(HKT) - timedelta(days=2)).strftime("%Y%m%d"),
        help="Start date (YYYYMMDD)",
    )
    parser.add_argument(
        "--until",
        default=(datetime.now(HKT) + timedelta(days=1)).strftime("%Y%m%d"),
        help="End date (YYYYMMDD, exclusive)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output image path (default: /tmp/usage_report_YYYYMMDD.png)",
    )
    args = parser.parse_args()

    if args.output is None:
        args.output = f"/tmp/usage_report_{args.since}_{args.until}.png"

    print(f"Collecting data from {args.since} to {args.until}...")

    # Collect all data
    daily_data = run_ccusage("daily", args.since, args.until, ["--breakdown"])
    blocks_data = run_ccusage("blocks", args.since, args.until)
    instances_data = run_ccusage("daily", args.since, args.until, ["--instances"])

    print("Scanning session files for timestamps...")
    ts_data = collect_session_timestamps(args.since, args.until)

    user_dates = ts_data.get("daily_user", {})
    for date in sorted(user_dates.keys()):
        msgs = user_dates[date]
        sessions_for_date = [s for s in ts_data["sessions"] if s["date"] == date]
        total_min = sum(s["duration_min"] for s in sessions_for_date)
        print(f"  {date}: {len(msgs)} user turns, {len(sessions_for_date)} sessions, ~{total_min:.0f}min active")

    print("Rendering report...")
    output = render_report(daily_data, blocks_data, instances_data, ts_data,
                           args.since, args.until, args.output)
    print(f"Report saved to: {output}")


if __name__ == "__main__":
    main()
