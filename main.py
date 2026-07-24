from pathlib import Path
import polars as pl
import dash_ag_grid as dag
from dash import Dash, Input, Output, State, dcc, html
from dash.exceptions import PreventUpdate

from src.pages import daily, history, upload
from src.database import (
    ASSET_RATIO_COLUMN,
    CURRENCY_COLUMN,
    DATE_COLUMN,
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
    load_available_dates,
    load_holdings_by_date,
    load_holdings_history,
    store_dataframe,
)
from src.reader import (
    ExtractedWorkbook,
    decode_upload,
    extract_msg_workbooks,
    parse_excel,
    parse_excel_bytes,
)

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
) -> dag.AgGrid:
    numeric_columns = set(NUMERIC_COLUMNS)
    columns = [
        {
            "headerName": "幣別" if name == CURRENCY_COLUMN else name,
            "field": name,
            "filter": (
                "agNumberColumnFilter"
                if name in numeric_columns
                else "agTextColumnFilter"
            ),
            "tooltipField": name,
        }
        for name in df.columns
    ]

    return dag.AgGrid(
        id=table_id,
        columnDefs=columns,
        rowData=df.to_dicts(),
        defaultColDef={
            "sortable": True,
            "resizable": True,
            "floatingFilter": True,
            "minWidth": 100,
            "maxWidth": 260,
        },
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": 20,
            "paginationPageSizeSelector": False,
            "alwaysMultiSort": True,
            "tooltipShowDelay": 400,
            "tooltipHideDelay": 0,
            "theme": {
                "function": (
                    "themeQuartz.withParams({"
                    "fontFamily: 'Inter, system-ui, sans-serif',"
                    "fontSize: 13,"
                    "headerBackgroundColor: '#eef2ff',"
                    "headerTextColor: '#27326b',"
                    "headerFontWeight: 700,"
                    "headerColumnBorder: { color: '#c7d2fe', width: 1 },"
                    "rowBorder: { color: '#e2e8f0', width: 1 },"
                    "spacing: 8"
                    "})"
                )
            },
        },
        getRowStyle={
            "styleConditions": [
                {
                    "condition": "params.node.rowIndex % 2 === 1",
                    "style": {"backgroundColor": "#f8fafc"},
                }
            ]
        },
        style={"height": "440px", "width": "100%", "overflowX": "auto"},
    )


def make_upload_status_table(rows: list[dict[str, str]]) -> dag.AgGrid:
    """Render a compact, paginated status list for a batch of workbooks."""
    return dag.AgGrid(
        id="upload-status-table",
        columnDefs=[
            {
                "headerName": "檔案",
                "field": "filename",
                "flex": 4,
                "cellStyle": {
                    "fontFamily": "Geist Mono, ui-monospace, monospace",
                    "fontWeight": 600,
                },
            },
            {
                "headerName": "狀態",
                "field": "status",
                "flex": 1.2,
                "cellStyle": {
                    "styleConditions": [
                        {
                            "condition": "params.value === '完成'",
                            "style": {"color": "#166534"},
                        },
                        {
                            "condition": "params.value === '失敗'",
                            "style": {"color": "#b91c1c"},
                        },
                    ],
                    "defaultStyle": {"fontWeight": 700},
                },
            },
            {
                "headerName": "詳細資訊",
                "field": "detail",
                "flex": 4.8,
                "cellStyle": {"color": "#64748b"},
            },
        ],
        rowData=rows,
        defaultColDef={"sortable": True, "resizable": True, "minWidth": 90},
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": 6,
            "paginationPageSizeSelector": False,
            "domLayout": "autoHeight",
            "theme": {
                "function": (
                    "themeQuartz.withParams({"
                    "fontFamily: 'Inter, system-ui, sans-serif',"
                    "fontSize: 12,"
                    "headerBackgroundColor: '#f8fafc',"
                    "headerTextColor: '#64748b',"
                    "headerFontWeight: 700,"
                    "rowBorder: { color: '#e2e8f0', width: 1 },"
                    "spacing: 8"
                    "})"
                )
            },
        },
        getRowStyle={
            "styleConditions": [
                {
                    "condition": "params.node.rowIndex % 2 === 1",
                    "style": {"backgroundColor": "#fafbfd"},
                }
            ]
        },
        style={"width": "100%", "overflowX": "auto"},
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
    Output("daily-export-button", "disabled"),
    Input("daily-date-picker", "date"),
)
def update_daily_table(report_date: str | None):
    holdings = load_holdings_by_date(report_date)
    return (
        daily.make_table_content(holdings, report_date, make_table),
        holdings.is_empty(),
    )


@app.callback(
    Output("daily-csv-download", "data"),
    Input("daily-export-button", "n_clicks"),
    State("daily-holdings-table", "virtualRowData"),
    State("daily-holdings-table", "columnDefs"),
    State("daily-date-picker", "date"),
    prevent_initial_call=True,
)
def download_daily_csv(n_clicks, rows, columns, report_date):
    if not n_clicks or not report_date:
        raise PreventUpdate
    return daily.make_csv_download(rows, columns, report_date)


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
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".xls", ".msg"}:
            rows.append(
                {
                    "filename": filename,
                    "status": "失敗",
                    "detail": "不支援的格式；請上傳 .xlsx、.xls 或 .msg",
                }
            )
            continue

        if suffix == ".msg":
            try:
                reports = extract_msg_workbooks(decode_upload(file_contents))
            except Exception as exc:
                reports = [ExtractedWorkbook(filename, error=str(exc))]
        else:
            reports = [ExtractedWorkbook(filename)]

        for report in reports:
            if report.error:
                report_label = (
                    f"{filename} › {report.label}"
                    if report.label != filename
                    else filename
                )
                rows.append(
                    {
                        "filename": report_label,
                        "status": "失敗",
                        "detail": report.error,
                    }
                )
                continue
            label = (
                filename
                if suffix != ".msg"
                else f"{filename} › {report.label.split(' › ', 1)[-1]}"
            )
            try:
                df = (
                    parse_excel(file_contents)
                    if suffix != ".msg"
                    else parse_excel_bytes(report.contents or b"")
                )
                store_dataframe(df)
            except Exception as exc:
                rows.append({"filename": label, "status": "失敗", "detail": str(exc)})
                continue

            completed += 1
            imported_rows += df.height
            rows.append(
                {
                    "filename": label,
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
                            html.Strong(f"已處理 {len(rows)} 個報表檔案"),
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
