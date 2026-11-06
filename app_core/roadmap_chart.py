"""Plotly timelines: calendar spans and quantity progress stay distinct."""

from datetime import date, datetime, timedelta
from hashlib import sha256
from html import escape

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from app_core.roadmap_service import parse_day, progress_percent, schedule_label
from prolog_ui.theme import STANDARD_PALETTE


def build_roadmap_figure(
    items: list,
    milestones: list,
    start: date,
    end: date,
    *,
    today: date,
    names: dict | None = None,
    actuals: dict | None = None,
    palette: dict | None = None,
    show_baseline=True,
    show_actuals=True,
):
    palette = palette or STANDARD_PALETTE
    names, actuals = names or {}, actuals or {}
    colors = [palette[key] for key in ("cyan", "green", "purple", "amber", "primary")]
    labels, rows = {}, []
    for item in items:
        group = item.get("subject_id") or item.get("project_id") or "unassigned"
        color = colors[int(sha256(group.encode()).hexdigest()[:8], 16) % len(colors)]
        label = item["title"][:65]
        parent = names.get(item.get("subject_id")) or names.get(item.get("project_id"))
        labels[item["id"]] = escape((parent + " / " if parent else "") + label)
        rows.append(
            {
                "id": item["id"],
                "start": parse_day(item["start_date"]),
                "finish": parse_day(item["end_date"]) + timedelta(days=1),
                "group": group,
                "color": color,
            }
        )
    if rows:
        frame = pd.DataFrame(rows)
        figure = px.timeline(
            frame,
            x_start="start",
            x_end="finish",
            y="id",
            color="group",
            color_discrete_map={row["group"]: row["color"] for row in rows},
            custom_data=["id"],
        )
        figure.update_traces(
            opacity=0.25, showlegend=False, hoverinfo="skip", marker_line_width=0
        )
    else:
        figure = go.Figure()
    for item, row in zip(items, rows):
        start_dt = datetime.combine(row["start"], datetime.min.time())
        duration = (row["finish"] - row["start"]).days * 86400000
        pct = progress_percent(item)
        text = f"{pct:.0f}% · {schedule_label(item, today)}"
        planned = f"<br>予定 {item['planned_minutes'] / 60:g}h"
        if show_actuals:
            planned += f" / 実績 {actuals.get(item['id'], 0) / 3600:.2f}h"
        hover = (
            f"{escape(item['title'])}<br>{item['start_date']} 〜 {item['end_date']}（終了日を含む）"
            f"<br>進捗 {item['completed']:g}/{item['total']:g} {escape(item['unit'])}（{pct:.1f}%）"
            f"{planned}"
            f"<br>{schedule_label(item, today)}"
        )
        if pct:
            figure.add_trace(
                go.Bar(
                    y=[item["id"]],
                    x=[duration * pct / 100],
                    base=[start_dt.isoformat()],
                    orientation="h",
                    marker_color=row["color"],
                    marker_opacity=0.55,
                    customdata=[[item["id"]]],
                    hovertemplate=hover + "<extra></extra>",
                    showlegend=False,
                    name="学習範囲の進捗",
                )
            )
        # Last overlay keeps the label and full-span selection target at 0% too.
        figure.add_trace(
            go.Bar(
                y=[item["id"]],
                x=[duration],
                base=[start_dt.isoformat()],
                orientation="h",
                marker_color=row["color"],
                marker_opacity=0.05,
                customdata=[[item["id"]]],
                text=[text],
                textposition="inside",
                insidetextanchor="middle",
                textfont_color=palette["ink"],
                hovertemplate=hover + "<extra></extra>",
                showlegend=False,
                name="予定",
            )
        )
        if show_baseline and (item["start_date"], item["end_date"]) != (
            item["baseline_start_date"],
            item["baseline_end_date"],
        ):
            finish = parse_day(item["baseline_end_date"]) + timedelta(days=1)
            figure.add_trace(
                go.Scatter(
                    x=[item["baseline_start_date"], finish.isoformat()],
                    y=[item["id"], item["id"]],
                    mode="lines",
                    line={"color": palette["muted"], "width": 3, "dash": "dot"},
                    showlegend=False,
                    hovertemplate="当初予定<extra></extra>",
                    customdata=[[item["id"]], [item["id"]]],
                )
            )
        if item.get("deadline"):
            figure.add_trace(
                go.Scatter(
                    x=[item["deadline"]],
                    y=[item["id"]],
                    mode="markers",
                    marker={
                        "symbol": "diamond-open",
                        "size": 12,
                        "color": palette["red"],
                    },
                    showlegend=False,
                    customdata=[[item["id"]]],
                    hovertemplate="確定期限 " + item["deadline"] + "<extra></extra>",
                )
            )
    visible_items = {item["id"]: item for item in items}
    for item in items:
        for parent_id in item.get("predecessor_ids", []):
            if parent_id not in visible_items:
                continue
            parent = visible_items[parent_id]
            finish = datetime.combine(
                parse_day(parent["end_date"]) + timedelta(days=1), datetime.min.time()
            )
            begins = datetime.combine(
                parse_day(item["start_date"]), datetime.min.time()
            )
            middle = finish + (begins - finish) / 2
            figure.add_trace(
                go.Scatter(
                    x=[
                        finish.isoformat(),
                        middle.isoformat(),
                        middle.isoformat(),
                        begins.isoformat(),
                    ],
                    y=[parent_id, parent_id, item["id"], item["id"]],
                    mode="lines+markers",
                    line={"color": palette["muted"], "width": 1.5},
                    marker={
                        "size": [0, 0, 0, 8],
                        "symbol": ["circle", "circle", "circle", "triangle-right"],
                        "color": palette["muted"],
                    },
                    customdata=[["edge:" + item["id"]]] * 4,
                    hovertemplate=escape(parent["title"])
                    + " → "
                    + escape(item["title"])
                    + "<extra></extra>",
                    showlegend=False,
                )
            )
    ordered = [item["id"] for item in items]
    if milestones:
        ordered.insert(0, "milestones")
        labels["milestones"] = "試験・節目"
        figure.add_trace(
            go.Scatter(
                x=[row["date"] for row in milestones],
                y=["milestones"] * len(milestones),
                mode="markers",
                marker={"symbol": "diamond", "size": 13, "color": palette["purple"]},
                customdata=[["milestone:" + row["id"]] for row in milestones],
                text=[escape(row["title"]) for row in milestones],
                hovertemplate="%{text}<br>%{x|%Y/%m/%d}<extra></extra>",
                showlegend=False,
            )
        )
    if start <= today <= end:
        figure.add_shape(
            type="line",
            x0=today.isoformat(),
            x1=today.isoformat(),
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            line={"color": palette["red"], "width": 1.5, "dash": "dash"},
        )
        figure.add_annotation(
            x=today.isoformat(),
            y=1,
            xref="x",
            yref="paper",
            text="今日",
            showarrow=False,
            yshift=16,
            font={"color": palette["red"]},
        )
    figure.update_layout(
        barmode="overlay",
        bargap=0.35,
        dragmode="pan",
        clickmode="event+select",
        height=max(320, 90 + 48 * len(ordered)),
        margin={"l": 12, "r": 24, "t": 38, "b": 50},
        paper_bgcolor=palette["card"],
        plot_bgcolor=palette["card"],
        font={"color": palette["ink"]},
        showlegend=False,
        uirevision=f"{start}:{end}:{','.join(ordered)}",
        hoverlabel={"bgcolor": palette["card"], "font_color": palette["ink"]},
    )
    figure.update_xaxes(
        type="date",
        range=[start.isoformat(), (end + timedelta(days=1)).isoformat()],
        title="日付",
        gridcolor=palette["line"],
        tickformat="%m/%d",
        dtick=(1 if (end - start).days < 15 else 7) * 86400000,
    )
    figure.update_yaxes(
        title=None,
        categoryorder="array",
        categoryarray=list(reversed(ordered)),
        tickvals=ordered,
        ticktext=[labels[key] for key in ordered],
        automargin=True,
    )
    return figure
