from dataclasses import dataclass

import plotly.graph_objects as go
import polars as pl
from dash import dcc, html

from src.database import (
    ACCOUNT_NAME_COLUMN,
    ASSET_RATIO_COLUMN,
    DATE_COLUMN,
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
)
from src.pages import daily

NAV_COLUMN = "基金淨值/ETF收盤價"
DATE_DISPLAY_FORMAT = "%Y/%m/%d"
TRACE_HOVER_TEMPLATE = (
    "<b>%{fullData.name}</b><br>"
    f"%{{x|{DATE_DISPLAY_FORMAT}}}<br>"
    "%{y:,.4f}<extra></extra>"
)


@dataclass(frozen=True)
class Metric:
    label: str
    reduction: str
    summary: str
    decimals: int = 2


METRICS = {
    daily.TARGET_MODE: {
        daily.UNITS_COLUMN: Metric("各專戶庫存單位數", "sum", "總計", 3),
        daily.TARGET_VALUE_COLUMN: Metric("各專戶持有市值(標的幣別)", "sum", "總計"),
        NAV_COLUMN: Metric(
            "基金淨值 / ETF 收盤價",
            "mode",
            "眾數（所有專戶資料；並列時取最小值）",
            4,
        ),
        ISSUE_SIZE_COLUMN: Metric(
            "標的發行規模或流通股數",
            "mode",
            "眾數（所有專戶資料；並列時取最小值）",
            3,
        ),
        ASSET_RATIO_COLUMN: Metric("佔標的資產或單位數比重(%)", "sum", "總計", 3),
    },
    daily.ACCOUNT_MODE: {
        daily.ACCOUNT_VALUE_COLUMN: Metric("持有市值(帳戶幣別)", "sum", "總計"),
        daily.UNITS_COLUMN: Metric("庫存單位數", "sum", "總計", 3),
        NAV_RATIO_COLUMN: Metric("佔淨資產比重(%)", "sum", "總計", 3),
    },
}


def metric_options(mode: str) -> list[dict[str, str]]:
    return [
        {"label": item.label, "value": value} for value, item in METRICS[mode].items()
    ]


def default_metric(mode: str) -> str:
    return (
        daily.TARGET_VALUE_COLUMN
        if mode == daily.TARGET_MODE
        else daily.ACCOUNT_VALUE_COLUMN
    )


def normalize_dates(
    start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    if start and end and start > end:
        return end, start
    return start, end


def filter_dates(df: pl.DataFrame, start: str | None, end: str | None) -> pl.DataFrame:
    if DATE_COLUMN not in df.columns:
        return pl.DataFrame()
    start, end = normalize_dates(start, end)
    result = df
    if start:
        result = result.filter(pl.col(DATE_COLUMN).cast(pl.String) >= start)
    if end:
        result = result.filter(pl.col(DATE_COLUMN).cast(pl.String) <= end)
    return result


def selector_options(
    df: pl.DataFrame, mode: str, start=None, end=None
) -> list[dict[str, str]]:
    return daily.selector_options(filter_dates(df, start, end), mode)


def resolve_controls(df, mode, start, end, selection=None, metric=None):
    mode = mode if mode in METRICS else daily.TARGET_MODE
    selections = selector_options(df, mode, start, end)
    allowed_selections = {option["value"] for option in selections}
    resolved_selection = (
        selection
        if selection in allowed_selections
        else (selections[0]["value"] if selections else None)
    )
    metrics = metric_options(mode)
    allowed_metrics = {option["value"] for option in metrics}
    resolved_metric = metric if metric in allowed_metrics else default_metric(mode)
    return (
        "標的名稱" if mode == daily.TARGET_MODE else "專戶名稱",
        selections,
        resolved_selection,
        metrics,
        resolved_metric,
    )


def empty_figure(message="所選條件沒有可顯示的歷史資料。"):
    figure = daily.empty_figure(message)
    figure.update_layout(xaxis={"rangeslider": {"visible": False}})
    return figure


def make_figure(df, mode, selection, metric, start=None, end=None):
    if mode not in METRICS or metric not in METRICS[mode]:
        return empty_figure(), "歷史趨勢", "請選擇有效的資料指標"
    selection_column = (
        daily.TARGET_NAME_COLUMN if mode == daily.TARGET_MODE else ACCOUNT_NAME_COLUMN
    )
    component_column = (
        ACCOUNT_NAME_COLUMN if mode == daily.TARGET_MODE else daily.TARGET_NAME_COLUMN
    )
    required = {DATE_COLUMN, selection_column, component_column, metric}
    if not selection or not required.issubset(df.columns):
        return empty_figure(), "歷史趨勢", "尚無可用資料"
    data = filter_dates(df, start, end).filter(pl.col(selection_column) == selection)
    data = data.filter(pl.col(metric).is_not_null()).with_columns(
        pl.col(metric).cast(pl.Float64)
    )
    if data.is_empty():
        return empty_figure(), f"{selection} 歷史趨勢", METRICS[mode][metric].label

    definition = METRICS[mode][metric]
    reducer = (
        pl.col(metric).sum()
        if definition.reduction == "sum"
        else pl.col(metric).mode().min()
    )
    summary = data.group_by(DATE_COLUMN).agg(reducer.alias("value")).sort(DATE_COLUMN)
    figure = go.Figure()
    components = data.group_by([DATE_COLUMN, component_column]).agg(
        reducer.alias("value")
    )
    for index, component in enumerate(
        sorted(components[component_column].drop_nulls().cast(pl.String).unique())
    ):
        points = components.filter(
            pl.col(component_column).cast(pl.String) == component
        ).sort(DATE_COLUMN)
        figure.add_trace(
            go.Scatter(
                x=points[DATE_COLUMN].to_list(),
                y=points["value"].to_list(),
                mode="lines+markers",
                name=component,
                line={
                    "width": 1.7,
                    "color": daily.CHART_COLORS[index % len(daily.CHART_COLORS)],
                },
                marker={"size": 5},
                hovertemplate=TRACE_HOVER_TEMPLATE,
            )
        )
    figure.add_trace(
        go.Scatter(
            x=summary[DATE_COLUMN].to_list(),
            y=summary["value"].to_list(),
            mode="lines+markers",
            name=definition.summary,
            line={"width": 4, "color": "#172033"},
            marker={"size": 7},
            hovertemplate=TRACE_HOVER_TEMPLATE,
        )
    )
    figure = daily.style_figure(figure)
    figure.update_layout(
        hovermode="x unified",
        margin={"l": 58, "r": 32, "t": 18, "b": 48},
        legend={"orientation": "h", "y": 1.08, "x": 0, "groupclick": "toggleitem"},
    )
    figure.update_xaxes(
        title="檢查日期",
        gridcolor="#eef2f7",
        tickformat=DATE_DISPLAY_FORMAT,
        hoverformat=DATE_DISPLAY_FORMAT,
        rangeslider={"visible": True},
    )
    figure.update_yaxes(title=definition.label, gridcolor="#eef2f7", zeroline=False)
    return figure, f"{selection} 歷史趨勢", definition.label


def layout(df: pl.DataFrame) -> html.Div:
    dates = (
        sorted(df[DATE_COLUMN].drop_nulls().cast(pl.String).unique())
        if DATE_COLUMN in df.columns
        else []
    )
    start, end = (dates[0], dates[-1]) if dates else (None, None)
    label, options, selection, metrics, metric = resolve_controls(
        df, daily.TARGET_MODE, start, end
    )
    figure, title, subtitle = make_figure(
        df, daily.TARGET_MODE, selection, metric, start, end
    )
    return html.Div(
        children=[
            html.Header(
                className="hero history-hero",
                children=[
                    html.H1("歷史資料"),
                    html.P(
                        "跨日期檢視所有標的與全委帳戶的資料趨勢", className="subtitle"
                    ),
                ],
            ),
            html.Section(
                className="history-date-controls",
                children=[
                    html.Label(
                        [
                            html.Span("開始日期"),
                            dcc.DatePickerSingle(
                                id="history-start-date",
                                date=start,
                                min_date_allowed=start,
                                max_date_allowed=end,
                                display_format="YYYY / MM / DD",
                            ),
                        ]
                    ),
                    html.Label(
                        [
                            html.Span("結束日期"),
                            dcc.DatePickerSingle(
                                id="history-end-date",
                                date=end,
                                min_date_allowed=start,
                                max_date_allowed=end,
                                display_format="YYYY / MM / DD",
                            ),
                        ]
                    ),
                ],
            ),
            html.Section(
                className="daily-view-controls history-view-controls",
                children=[
                    html.Div(
                        [
                            html.Strong("資料檢視", className="view-mode-label"),
                            dcc.RadioItems(
                                id="history-view-mode",
                                options=daily.MODE_OPTIONS,
                                value=daily.TARGET_MODE,
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
                                    html.Span(label, id="history-selection-label"),
                                    dcc.Dropdown(
                                        id="history-selection-dropdown",
                                        options=options,
                                        value=selection,
                                        clearable=False,
                                    ),
                                ],
                                className="daily-view-field daily-selection-field",
                            ),
                            html.Label(
                                [
                                    html.Span("資料指標"),
                                    dcc.Dropdown(
                                        id="history-metric-dropdown",
                                        options=metrics,
                                        value=metric,
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
                className="daily-visualization-card history-visualization-card",
                children=[
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Strong(title, id="history-chart-title"),
                                    html.Span(subtitle, id="history-chart-subtitle"),
                                ],
                                className="daily-chart-heading",
                            )
                        ],
                        className="daily-chart-header",
                    ),
                    dcc.Graph(
                        id="history-chart",
                        figure=figure,
                        config={"displayModeBar": False},
                        responsive=True,
                        style={"width": "100%", "height": "520px"},
                        className="history-chart",
                    ),
                ],
            ),
        ]
    )
