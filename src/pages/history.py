from collections.abc import Callable
from typing import Any

from dash import dcc, html


def format_range(history: list[dict[str, Any]]) -> str:
    dates = sorted({observation["report_date"] for observation in history})
    if not dates:
        return "尚無日期"
    if len(dates) == 1:
        return dates[0].replace("-", " / ")
    return f"{dates[0].replace('-', ' / ')} — {dates[-1].replace('-', ' / ')}"


def layout(history: list[dict[str, Any]], figure_factory: Callable) -> html.Div:
    fund_count = len({observation["isin"] for observation in history})
    date_count = len({observation["report_date"] for observation in history})
    return html.Div(
        children=[
            html.Header(
                className="hero history-hero",
                children=[
                    html.P("TIME SERIES", className="eyebrow"),
                    html.H1("基金歷史趨勢"),
                    html.P(
                        "所有已上傳共同基金的歷史淨值走勢",
                        className="subtitle",
                    ),
                ],
            ),
            html.Div(
                className="history-summary",
                children=[
                    html.Div([html.Strong(f"{fund_count:,}"), html.Span("基金")]),
                    html.Div([html.Strong(f"{date_count:,}"), html.Span("資料日期")]),
                ],
            ),
            html.Section(
                className="chart-card",
                children=[
                    html.Div(
                        className="chart-header",
                        children=[
                            html.Div(
                                [
                                    html.Strong("共同基金淨值"),
                                    html.Span("每條線代表一個 ISIN；跨帳戶的同日資料已合併"),
                                ],
                                className="chart-heading",
                            ),
                            html.Div(format_range(history), className="chart-date-range"),
                        ],
                    ),
                    dcc.Graph(
                        id="history-chart",
                        figure=figure_factory(history),
                        config={"displaylogo": False, "responsive": True},
                        className="history-chart",
                    ),
                ],
            ),
        ]
    )
