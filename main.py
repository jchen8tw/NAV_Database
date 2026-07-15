from pathlib import Path
from typing import Any

import polars as pl
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dash_table, dcc, html

from src.pages import daily, history, upload
from src.database import (
    ASSET_RATIO_COLUMN,
    DATE_COLUMN,
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
    load_available_dates,
    load_holdings_by_date,
    load_nav_history,
    store_dataframe,
)
from src.reader import parse_excel

NUMERIC_COLUMNS = (
    "庫存單位數",
    "基金淨值/ETF收盤價",
    "持有市值(帳戶幣別)",
    "持有市值(標的幣別)",
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
    ASSET_RATIO_COLUMN,
)


def make_history_figure(history: list[dict[str, Any]]) -> go.Figure:
    """Build a line plot containing every mutual fund in the history table."""
    figure = go.Figure()
    colors = ["#4F46E5", "#0891B2", "#F59E0B", "#DB2777", "#16A34A"]
    series: dict[str, list[dict[str, Any]]] = {}
    for observation in history:
        series.setdefault(observation["isin"], []).append(observation)

    for index, (isin, observations) in enumerate(series.items()):
        fund_name = observations[-1]["fund_name"]
        figure.add_trace(
            go.Scatter(
                x=[observation["report_date"] for observation in observations],
                y=[observation["nav"] for observation in observations],
                mode="lines+markers",
                name=fund_name,
                line={"width": 2.4, "color": colors[index % len(colors)]},
                marker={"size": 6},
                customdata=[isin] * len(observations),
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>"
                    "ISIN: %{customdata}<br>"
                    "Date: %{x|%Y-%m-%d}<br>"
                    "NAV: %{y:,.4f}<extra></extra>"
                ),
            )
        )

    figure.update_layout(
        margin={"l": 58, "r": 300, "t": 24, "b": 52},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="closest",
        legend={
            "title": {"text": "共同基金"},
            "groupclick": "toggleitem",
            "x": 1.02,
            "y": 1,
            "xanchor": "left",
            "yanchor": "top",
            "bgcolor": "#f8fafc",
            "bordercolor": "#e2e8f0",
            "borderwidth": 1,
            "font": {"size": 11},
        },
        xaxis={
            "title": "檢查日期",
            "showgrid": True,
            "gridcolor": "#eef2f7",
            "rangeslider": {"visible": bool(history)},
        },
        yaxis={"title": "淨值", "showgrid": True, "gridcolor": "#eef2f7"},
        font={"family": "Inter, system-ui, sans-serif", "color": "#172033"},
    )
    if not history:
        figure.add_annotation(
            text="上傳報表後即可建立基金歷史趨勢。",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"size": 16, "color": "#64748b"},
        )
        figure.update_xaxes(visible=False)
        figure.update_yaxes(visible=False)
    return figure


def make_table(
    df: pl.DataFrame, table_id: str = "holdings-table"
) -> dash_table.DataTable:
    numeric_columns = set(NUMERIC_COLUMNS)
    columns = [
        {
            "name": name,
            "id": name,
            "type": "numeric" if name in numeric_columns else "text",
        }
        for name in df.columns
    ]

    return dash_table.DataTable(
        id=table_id,
        columns=columns,  # type: ignore[arg-type] -- Dash's generated stub is invariant.
        data=df.to_dicts(),  # type: ignore[arg-type] -- Polars values are JSON-compatible.
        page_size=20,
        page_action="native",
        sort_action="native",
        sort_mode="multi",
        filter_action="native",
        fixed_rows={"headers": True},
        style_table={"overflowX": "auto", "maxHeight": "440px"},
        style_cell={
            "fontFamily": "Inter, system-ui, sans-serif",
            "fontSize": 13,
            "padding": "10px 12px",
            "textAlign": "left",
            "minWidth": "100px",
            "maxWidth": "260px",
            "overflow": "hidden",
            "textOverflow": "ellipsis",
        },
        style_header={
            "backgroundColor": "#eef2ff",
            "color": "#27326b",
            "fontWeight": 700,
            "borderBottom": "2px solid #c7d2fe",
            "whiteSpace": "normal",
            "height": "auto",
        },
        style_filter={"backgroundColor": "#f8fafc"},
        style_data_conditional=[
            {
                "if": {"row_index": "odd"},
                "backgroundColor": "#f8fafc",
            }
        ],
        tooltip_delay=400,
        tooltip_duration=None,
    )


app = Dash(__name__, suppress_callback_exceptions=True)
app.title = "Holdings Database"

app.layout = html.Div(
    children=[
        dcc.Location(id="url"),
        html.Nav(
            className="site-nav",
            children=[
                dcc.Link("Holdings Database", href="/", className="brand"),
                html.Div(
                    className="nav-links",
                    children=[
                        dcc.Link("上傳", href="/", id="upload-nav-link"),
                        dcc.Link("單日資料", href="/daily", id="daily-nav-link"),
                        dcc.Link(
                            "歷史趨勢", href="/history", id="history-nav-link"
                        ),
                    ],
                ),
            ],
        ),
        html.Main(id="page-content", className="app-shell"),
    ],
)


@app.callback(
    Output("page-content", "children"),
    Output("upload-nav-link", "className"),
    Output("daily-nav-link", "className"),
    Output("history-nav-link", "className"),
    Input("url", "pathname"),
)
def render_page(pathname: str | None):
    if pathname == "/daily":
        return (
            daily.layout(load_available_dates(), load_holdings_by_date, make_table),
            "",
            "active",
            "",
        )
    if pathname == "/history":
        return history.layout(load_nav_history(), make_history_figure), "", "", "active"
    return upload.layout(), "active", "", ""


@app.callback(
    Output("daily-table-container", "children"),
    Input("daily-date-picker", "date"),
)
def update_daily_table(report_date: str | None):
    return daily.make_table_content(
        load_holdings_by_date(report_date), report_date, make_table
    )


@app.callback(
    Output("upload-status", "children"),
    Output("upload-status", "className"),
    Output("table-container", "children"),
    Input("excel-upload", "contents"),
    State("excel-upload", "filename"),
    prevent_initial_call=True,
)
def show_uploaded_workbook(contents: str | None, filename: str | None):
    if not contents:
        return "No file was received.", "status status--error", None

    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        return (
            "Please upload an Excel file (.xlsx or .xls).",
            "status status--error",
            None,
        )

    try:
        df = parse_excel(contents)
        database_rows = store_dataframe(df)
    except Exception as exc:
        return (
            f"Could not process {filename or 'the uploaded file'}: {exc}",
            "status status--error",
            None,
        )

    status = html.Div(
        [
            html.Strong(filename or "Uploaded workbook"),
            html.Span(
                f"{df.height:,} rows · {df.width:,} columns · "
                f"{database_rows:,} records in SQLite · "
                f"{df.get_column(DATE_COLUMN).item(0)}"
            ),
        ],
        className="file-summary",
    )
    return status, "status status--success", make_table(df)


if __name__ == "__main__":
    app.run(debug=True)
