from pathlib import Path
import polars as pl
from dash import Dash, Input, Output, State, dash_table, dcc, html

from src.pages import daily, history, upload
from src.database import (
    ASSET_RATIO_COLUMN,
    DATE_COLUMN,
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
    load_available_dates,
    load_holdings_by_date,
    load_holdings_history,
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


def make_upload_status_table(rows: list[dict[str, str]]) -> dash_table.DataTable:
    """Render a compact, paginated status list for a batch of workbooks."""
    return dash_table.DataTable(
        id="upload-status-table",
        columns=[
            {"name": "檔案", "id": "filename"},
            {"name": "狀態", "id": "status"},
            {"name": "詳細資訊", "id": "detail"},
        ],
        data=rows,
        page_size=6,
        page_action="native",
        sort_action="native",
        style_table={"overflowX": "auto"},
        style_cell={
            "fontFamily": "Inter, system-ui, sans-serif",
            "fontSize": 12,
            "padding": "11px 16px",
            "textAlign": "left",
            "border": "none",
            "borderBottom": "1px solid #e2e8f0",
        },
        style_cell_conditional=[
            {
                "if": {"column_id": "filename"},
                "fontFamily": "Geist Mono, ui-monospace, monospace",
                "fontWeight": 600,
                "width": "40%",
            },
            {"if": {"column_id": "status"}, "fontWeight": 700, "width": "12%"},
            {"if": {"column_id": "detail"}, "color": "#64748b"},
        ],
        style_header={
            "backgroundColor": "#f8fafc",
            "color": "#64748b",
            "fontWeight": 700,
            "border": "none",
            "borderBottom": "1px solid #e2e8f0",
        },
        style_data_conditional=[
            {
                "if": {"filter_query": '{status} = "完成"', "column_id": "status"},
                "color": "#166534",
            },
            {
                "if": {"filter_query": '{status} = "失敗"', "column_id": "status"},
                "color": "#b91c1c",
            },
            {"if": {"row_index": "odd"}, "backgroundColor": "#fafbfd"},
        ],
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
                            "歷史資料", href="/history", id="history-nav-link"
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
        return history.layout(load_holdings_history()), "", "", "active"
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
    Output("daily-selection-label", "children"),
    Output("daily-selection-dropdown", "options"),
    Output("daily-selection-dropdown", "value"),
    Output("daily-metric-dropdown", "options"),
    Output("daily-metric-dropdown", "value"),
    Input("daily-date-picker", "date"),
    Input("daily-view-mode", "value"),
)
def update_daily_visualization_controls(report_date: str | None, mode: str):
    holdings = load_holdings_by_date(report_date)
    options = daily.selector_options(holdings, mode)
    selection = options[0]["value"] if options else None
    metric_options = daily.METRIC_OPTIONS[mode]
    return (
        "標的名稱" if mode == daily.TARGET_MODE else "專戶名稱",
        options,
        selection,
        metric_options,
        daily.default_metric(mode),
    )


@app.callback(
    Output("daily-visualization", "figure"),
    Output("daily-chart-title", "children"),
    Output("daily-chart-subtitle", "children"),
    Output("daily-chart-total", "children"),
    Input("daily-date-picker", "date"),
    Input("daily-view-mode", "value"),
    Input("daily-selection-dropdown", "value"),
    Input("daily-metric-dropdown", "value"),
)
def update_daily_visualization(
    report_date: str | None, mode: str, selection: str | None, metric: str | None
):
    return daily.make_visualization(
        load_holdings_by_date(report_date), mode, selection, metric
    )


@app.callback(
    Output("history-selection-label", "children"),
    Output("history-selection-dropdown", "options"),
    Output("history-selection-dropdown", "value"),
    Output("history-metric-dropdown", "options"),
    Output("history-metric-dropdown", "value"),
    Input("history-view-mode", "value"),
    Input("history-start-date", "date"),
    Input("history-end-date", "date"),
    State("history-selection-dropdown", "value"),
    State("history-metric-dropdown", "value"),
)
def update_history_controls(mode, start, end, selection, metric):
    return history.resolve_controls(
        load_holdings_history(), mode, start, end, selection, metric
    )


@app.callback(
    Output("history-chart", "figure"),
    Output("history-chart-title", "children"),
    Output("history-chart-subtitle", "children"),
    Input("history-view-mode", "value"),
    Input("history-selection-dropdown", "value"),
    Input("history-metric-dropdown", "value"),
    Input("history-start-date", "date"),
    Input("history-end-date", "date"),
)
def update_history_figure(mode, selection, metric, start, end):
    return history.make_figure(
        load_holdings_history(), mode, selection, metric, start, end
    )


@app.callback(
    Output("upload-status", "children"),
    Output("upload-status", "className"),
    Input("excel-upload", "contents"),
    State("excel-upload", "filename"),
    prevent_initial_call=True,
)
def show_uploaded_workbooks(
    contents: list[str] | None, filenames: list[str] | None
):
    if not contents:
        return "未收到任何檔案。", "batch-status batch-status--error"

    filenames = filenames or []

    rows: list[dict[str, str]] = []
    completed = 0
    imported_rows = 0
    for index, file_contents in enumerate(contents):
        filename = filenames[index] if index < len(filenames) else f"檔案 {index + 1}"
        if Path(filename).suffix.lower() not in {".xlsx", ".xls"}:
            rows.append(
                {
                    "filename": filename,
                    "status": "失敗",
                    "detail": "不支援的格式；請上傳 .xlsx 或 .xls",
                }
            )
            continue

        try:
            df = parse_excel(file_contents)
            store_dataframe(df)
        except Exception as exc:
            rows.append(
                {"filename": filename, "status": "失敗", "detail": str(exc)}
            )
            continue

        completed += 1
        imported_rows += df.height
        rows.append(
            {
                "filename": filename,
                "status": "完成",
                "detail": (
                    f"{df.height:,} 列 · {df.width:,} 欄 · "
                    f"資料日期 {df.get_column(DATE_COLUMN).item(0)}"
                ),
            }
        )

    failed = len(rows) - completed
    status = html.Section(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Strong(f"已處理 {len(rows)} 個 Excel 檔案"),
                            html.Span(
                                f"{completed} 個完成 · {failed} 個失敗 · "
                                f"本批次共寫入 {imported_rows:,} 筆資料"
                            ),
                        ],
                        className="batch-summary-copy",
                    ),
                    html.Div(
                        [
                            html.Span("完成率"),
                            html.Strong(f"{completed / len(rows):.0%}"),
                        ],
                        className="batch-completion",
                    ),
                ],
                className="batch-summary",
            ),
            make_upload_status_table(rows),
        ],
        className="batch-card",
    )
    modifier = "batch-status--success" if not failed else "batch-status--mixed"
    return status, f"batch-status {modifier}"


if __name__ == "__main__":
    app.run(debug=True)
