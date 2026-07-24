from collections.abc import Callable
import csv
from io import StringIO

import polars as pl
import plotly.graph_objects as go
from dash import dcc, html

from src.database import ACCOUNT_NAME_COLUMN

TARGET_MODE = "target"
ACCOUNT_MODE = "account"
TARGET_NAME_COLUMN = "標的名稱"
UNITS_COLUMN = "庫存單位數"
ACCOUNT_VALUE_COLUMN = "持有市值(帳戶幣別)"
TARGET_VALUE_COLUMN = "持有市值(標的幣別)"
MODE_OPTIONS = [
    {"label": "查看標的", "value": TARGET_MODE},
    {"label": "查看專戶", "value": ACCOUNT_MODE},
]
METRIC_OPTIONS = {
    TARGET_MODE: [
        {"label": "各專戶庫存單位數", "value": UNITS_COLUMN},
        {"label": "各專戶持有市值(標的幣別)", "value": TARGET_VALUE_COLUMN},
    ],
    ACCOUNT_MODE: [
        {"label": "持有市值(帳戶幣別)", "value": ACCOUNT_VALUE_COLUMN},
        {"label": "庫存單位數", "value": UNITS_COLUMN},
    ],
}
CHART_COLORS = ["#4F46E5", "#06B6D4", "#F59E0B", "#DB2777", "#16A34A", "#94A3B8"]


def make_csv_download(
    rows: list[dict] | None,
    columns: list[dict] | None,
    report_date: str,
) -> dict[str, str]:
    columns = columns or []
    output = StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        " ".join(column["headerName"])
        if isinstance(column["headerName"], list)
        else column["headerName"]
        for column in columns
    ])
    for row in rows or []:
        writer.writerow([row.get(column["field"]) for column in columns])
    return {
        "content": output.getvalue(),
        "filename": f"daily_holdings_{report_date[:10]}.csv",
        "type": "text/csv;charset=utf-8",
    }


def selector_options(df: pl.DataFrame, mode: str) -> list[dict[str, str]]:
    column = TARGET_NAME_COLUMN if mode == TARGET_MODE else ACCOUNT_NAME_COLUMN
    if column not in df.columns or df.is_empty():
        return []
    values = sorted(
        {
            str(value).strip()
            for value in df.get_column(column).drop_nulls().to_list()
            if str(value).strip()
        }
    )
    return [{"label": value, "value": value} for value in values]


def default_metric(mode: str) -> str:
    return TARGET_VALUE_COLUMN if mode == TARGET_MODE else ACCOUNT_VALUE_COLUMN


def empty_figure(message: str = "所選條件沒有可顯示的資料。") -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(
        text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
        font={"size": 15, "color": "#64748b"},
    )
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    return style_figure(figure)


def style_figure(figure: go.Figure) -> go.Figure:
    figure.update_layout(
        autosize=True,
        margin={"l": 32, "r": 32, "t": 18, "b": 32},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, system-ui, sans-serif", "color": "#172033"},
        showlegend=True,
    )
    return figure


def make_visualization(
    df: pl.DataFrame, mode: str, selection: str | None, metric: str | None
) -> tuple[go.Figure, str, str, str]:
    metric = metric or default_metric(mode)
    selection_column = TARGET_NAME_COLUMN if mode == TARGET_MODE else ACCOUNT_NAME_COLUMN
    group_column = ACCOUNT_NAME_COLUMN if mode == TARGET_MODE else TARGET_NAME_COLUMN
    if (
        not selection
        or df.is_empty()
        or any(column not in df.columns for column in (selection_column, group_column, metric))
    ):
        return empty_figure(), "資料分布", "尚無可用資料", "總計 —"

    grouped = (
        df.filter(pl.col(selection_column) == selection)
        .filter(pl.col(metric).is_not_null())
        .group_by(group_column)
        .agg(pl.col(metric).sum().alias("value"))
        .filter(pl.col("value") > 0)
        .sort("value", descending=True)
    )
    if grouped.is_empty():
        return empty_figure(), f"{selection} 資料分布", metric, "總計 0"

    labels = grouped.get_column(group_column).cast(pl.String).to_list()
    values = grouped.get_column("value").cast(pl.Float64).to_list()
    is_bar = mode == ACCOUNT_MODE and metric == UNITS_COLUMN
    if is_bar:
        labels = labels[::-1]
        values = values[::-1]
        axis_labels = [
            label if len(label) <= 48 else f"{label[:45]}…" for label in labels
        ]
        figure = go.Figure(
            go.Bar(
                x=values,
                y=axis_labels,
                customdata=labels,
                orientation="h",
                width=0.9,
                showlegend=False,
                marker={"color": "#4F46E5", "line": {"color": "#3730A3", "width": 0.5}},
                hovertemplate="<b>%{customdata}</b><br>庫存單位數：%{x:,.3f}<extra></extra>",
            )
        )
        figure.update_layout(bargap=0.08)
        figure.update_xaxes(
            title={"text": "庫存單位數", "font": {"size": 17}},
            tickfont={"size": 14},
            gridcolor="#eef2f7",
            zeroline=False,
        )
        figure.update_yaxes(
            title=None,
            tickfont={"size": 14},
            showgrid=False,
            automargin=False,
        )
    else:
        figure = go.Figure(
            go.Pie(
                labels=labels,
                values=values,
                sort=False,
                textinfo="none",
                marker={"colors": [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(labels))]},
                hovertemplate="<b>%{label}</b><br>%{value:,.2f} · %{percent}<extra></extra>",
            )
        )
        figure.update_layout(
            legend={
                "x": 0.52,
                "y": 0.5,
                "xanchor": "left",
                "yanchor": "middle",
                "xref": "container",
                "yref": "container",
                "maxheight": 0.86,
                "font": {"size": 12},
            },
        )
        figure.update_traces(domain={"x": [0.04, 0.46], "y": [0.14, 0.86]})

    noun = "各專戶" if mode == TARGET_MODE else "各標的"
    title = f"{selection} {noun}{'庫存單位數' if metric == UNITS_COLUMN else '持有市值'}分布"
    currency = "・帳戶幣別" if metric == ACCOUNT_VALUE_COLUMN else ("・標的幣別" if metric == TARGET_VALUE_COLUMN else "")
    subtitle = next(option["label"] for option in METRIC_OPTIONS[mode] if option["value"] == metric) + currency
    total = sum(values)
    total_text = f"總計 {total:,.3f}" if metric == UNITS_COLUMN else f"總計 {total:,.2f}"
    figure = style_figure(figure)
    if is_bar:
        figure.update_layout(
            showlegend=False,
            margin={"l": 360, "r": 32, "t": 18, "b": 48, "autoexpand": False},
        )
    return figure, title, subtitle, total_text


def make_table_content(
    df: pl.DataFrame,
    report_date: str | None,
    table_factory: Callable,
):
    if not report_date:
        return html.Div("尚無資料。請先上傳 Excel 報表。", className="empty-state")
    if df.is_empty():
        return html.Div("所選日期沒有資料。", className="empty-state")
    return table_factory(df, table_id="daily-holdings-table")


def layout(
    dates: list[str],
    holdings_loader: Callable[[str | None], pl.DataFrame],
    table_factory: Callable,
) -> html.Div:
    selected_date = dates[-1] if dates else None
    holdings = holdings_loader(selected_date)
    initial_options = selector_options(holdings, TARGET_MODE)
    initial_selection = initial_options[0]["value"] if initial_options else None
    initial_figure, initial_title, initial_subtitle, initial_total = make_visualization(
        holdings, TARGET_MODE, initial_selection, TARGET_VALUE_COLUMN
    )
    return html.Div(
        children=[
            html.Header(
                className="hero daily-hero",
                children=[
                    html.H1("單日資料"),
                    html.P(
                        "單日的所有基金與全委帳戶的資料",
                        className="subtitle",
                    ),
                ],
            ),
            html.Section(
                className="date-picker-panel",
                children=[
                    html.Div(
                        [
                            html.Strong("資料日期"),
                            html.Span("選擇要檢視的單日資料"),
                        ],
                        className="date-picker-copy",
                    ),
                    html.Div(
                        [
                            dcc.DatePickerSingle(
                                id="daily-date-picker",
                                date=selected_date,
                                min_date_allowed=dates[0] if dates else None,
                                max_date_allowed=dates[-1] if dates else None,
                                initial_visible_month=selected_date,
                                display_format="YYYY / MM / DD",
                                className="date-picker-control",
                            ),
                            html.Button(
                                [
                                    html.Span("↓", className="download-icon", **{"aria-hidden": "true"}),
                                    "輸出成csv",
                                ],
                                id="daily-export-button",
                                className="daily-export-button",
                                disabled=holdings.is_empty(),
                            ),
                            dcc.Download(id="daily-csv-download"),
                        ],
                        className="daily-date-actions",
                    ),
                ],
            ),
            dcc.Loading(
                type="circle",
                children=html.Section(
                    id="daily-table-container",
                    className="table-card daily-table-card",
                    children=make_table_content(
                        holdings, selected_date, table_factory
                    ),
                ),
            ),
            html.Section(
                className="daily-view-controls",
                children=[
                    html.Div(
                        [
                            html.Strong("資料檢視", className="view-mode-label"),
                            dcc.RadioItems(
                                id="daily-view-mode",
                                options=MODE_OPTIONS,
                                value=TARGET_MODE,
                                inline=True,
                                className="view-mode-radio",
                            ),
                        ],
                        className="view-mode-group",
                    ),
                    html.Div(
                        [
                            html.Label(
                                [
                                    html.Span("標的名稱", id="daily-selection-label"),
                                    dcc.Dropdown(
                                        id="daily-selection-dropdown",
                                        options=initial_options,
                                        value=initial_selection,
                                        clearable=False,
                                    ),
                                ],
                                className="daily-view-field daily-selection-field",
                            ),
                            html.Label(
                                [
                                    html.Span("資料指標"),
                                    dcc.Dropdown(
                                        id="daily-metric-dropdown",
                                        options=METRIC_OPTIONS[TARGET_MODE],
                                        value=TARGET_VALUE_COLUMN,
                                        clearable=False,
                                    ),
                                ],
                                className="daily-view-field daily-metric-field",
                            ),
                        ],
                        className="daily-view-selectors",
                    ),
                ],
            ),
            html.Section(
                className="daily-visualization-card",
                children=[
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Strong(initial_title, id="daily-chart-title"),
                                    html.Span(initial_subtitle, id="daily-chart-subtitle"),
                                ],
                                className="daily-chart-heading",
                            ),
                            html.Strong(initial_total, id="daily-chart-total", className="daily-chart-total"),
                        ],
                        className="daily-chart-header",
                    ),
                    dcc.Graph(
                        id="daily-visualization",
                        figure=initial_figure,
                        config={"displayModeBar": False},
                        responsive=True,
                        style={"width": "100%", "height": "440px"},
                        className="daily-chart",
                    ),
                ],
            ),
        ]
    )
