import base64
from datetime import date, timedelta
from pathlib import Path
import polars as pl
import dash_ag_grid as dag
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
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
    parse_excel_bytes_result,
    parse_excel_result,
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
FORMAT_LABELS = {"cathay": "國泰世華", "ctbc": "中信"}


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


def previous_weekday(value: date) -> date:
    """Return the nearest Monday-Friday date strictly before value."""
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _display_date(value: str | None) -> str:
    return value.replace("-", "/") if value else "—"


def _grid_rows(staging: dict | None) -> list[dict]:
    return [
        {
            key: value
            for key, value in row.items()
            if key not in {"source_contents", "source_kind"}
        }
        for row in (staging or {}).get("rows", [])
    ]


def stage_uploaded_workbooks(
    contents: list[str] | None,
    filenames: list[str] | None,
) -> dict:
    """Inspect uploads and build JSON-safe rows without writing to storage."""
    if not contents:
        return {"rows": []}

    filenames = filenames or []
    rows: list[dict] = []

    def add_row(
        upload_index: int,
        report_index: int,
        filename: str,
        source_kind: str,
        source_contents: str,
        parsed=None,
        error: str | None = None,
    ) -> None:
        workbook_format = parsed.format if parsed is not None else None
        parsed_date = (
            date.fromisoformat(parsed.dataframe[DATE_COLUMN].item(0))
            if parsed is not None
            else None
        )
        assigned_date = (
            previous_weekday(parsed_date).isoformat()
            if workbook_format == "cathay"
            else parsed_date.isoformat()
            if workbook_format == "ctbc"
            else None
        )
        rows.append(
            {
                "id": f"upload-{upload_index}-report-{report_index}",
                "filename": filename,
                "format": workbook_format,
                "format_label": FORMAT_LABELS.get(
                    workbook_format, "無法辨識"
                ),
                "date": assigned_date,
                "display_date": _display_date(assigned_date),
                "valid": parsed is not None and error is None,
                "error": error or "",
                "upload_index": upload_index,
                "report_index": report_index,
                "source_kind": source_kind,
                "source_contents": source_contents,
            }
        )

    for upload_index, file_contents in enumerate(contents):
        filename = (
            filenames[upload_index]
            if upload_index < len(filenames)
            else f"檔案 {upload_index + 1}"
        )
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".xls", ".msg"}:
            add_row(
                upload_index,
                0,
                filename,
                "upload",
                file_contents,
                error="不支援的格式；請上傳 .xlsx、.xls 或 .msg",
            )
            continue

        if suffix != ".msg":
            try:
                parsed = parse_excel_result(file_contents)
            except Exception as exc:
                add_row(
                    upload_index,
                    0,
                    filename,
                    "upload",
                    file_contents,
                    error=str(exc),
                )
            else:
                add_row(
                    upload_index,
                    0,
                    filename,
                    "upload",
                    file_contents,
                    parsed=parsed,
                )
            continue

        try:
            reports = extract_msg_workbooks(decode_upload(file_contents))
        except Exception as exc:
            reports = [ExtractedWorkbook(filename, error=str(exc))]
        for report_index, report in enumerate(reports):
            label = (
                filename
                if report.label == filename
                else f"{filename} › {report.label.split(' › ', 1)[-1]}"
            )
            encoded = (
                base64.b64encode(report.contents).decode()
                if report.contents is not None
                else ""
            )
            if report.error:
                add_row(
                    upload_index,
                    report_index,
                    label,
                    "bytes",
                    encoded,
                    error=report.error,
                )
                continue
            try:
                parsed = parse_excel_bytes_result(report.contents or b"")
            except Exception as exc:
                add_row(
                    upload_index,
                    report_index,
                    label,
                    "bytes",
                    encoded,
                    error=str(exc),
                )
            else:
                add_row(
                    upload_index,
                    report_index,
                    label,
                    "bytes",
                    encoded,
                    parsed=parsed,
                )
    return {"rows": rows}


def selected_date_state(
    selected_rows: list[dict] | None,
) -> tuple[str | None, str]:
    valid_rows = [row for row in (selected_rows or []) if row.get("valid")]
    if not valid_rows:
        return None, "請先勾選要批次設定日期的檔案"
    dates = {row.get("date") for row in valid_rows}
    if len(dates) > 1:
        return None, f"已選取 {len(valid_rows)} 個檔案，目前包含不同日期"
    return dates.pop(), f"已選取 {len(valid_rows)} 個檔案"


def apply_date_to_selected(
    staging: dict | None,
    selected_rows: list[dict] | None,
    selected_date: str | None,
) -> dict:
    if not staging or not selected_date:
        return staging or {"rows": []}
    selected_ids = {
        row["id"] for row in (selected_rows or []) if row.get("valid")
    }
    for row in staging.get("rows", []):
        if row["id"] in selected_ids and row.get("valid"):
            row["date"] = selected_date
            row["display_date"] = _display_date(selected_date)
    return staging


def _make_upload_result(rows: list[dict[str, str]]) -> tuple[html.Section, str]:
    completed = sum(row["status"] == "完成" for row in rows)
    imported_rows = sum(row.get("imported_rows", 0) for row in rows)
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


def confirm_staged_workbooks(staging: dict | None):
    """Reparse and independently store every valid staged workbook."""
    rows: list[dict] = []
    for staged in (staging or {}).get("rows", []):
        if not staged.get("valid"):
            rows.append(
                {
                    "filename": staged["filename"],
                    "status": "失敗",
                    "detail": staged.get("error") or "檔案格式無效",
                    "imported_rows": 0,
                }
            )
            continue
        try:
            if staged["source_kind"] == "upload":
                parsed = parse_excel_result(staged["source_contents"])
            else:
                parsed = parse_excel_bytes_result(
                    base64.b64decode(staged["source_contents"])
                )
            dataframe = parsed.dataframe.with_columns(
                pl.lit(staged["date"]).alias(DATE_COLUMN)
            )
            store_dataframe(dataframe)
        except Exception as exc:
            rows.append(
                {
                    "filename": staged["filename"],
                    "status": "失敗",
                    "detail": str(exc),
                    "imported_rows": 0,
                }
            )
            continue
        rows.append(
            {
                "filename": staged["filename"],
                "status": "完成",
                "detail": (
                    f"{dataframe.height:,} 列 · {dataframe.width:,} 欄 · "
                    f"資料日期 {staged['date']}"
                ),
                "imported_rows": dataframe.height,
            }
        )
    return _make_upload_result(rows)


@app.callback(
    Output("upload-staging-store", "data"),
    Output("upload-staging-grid", "rowData"),
    Output("upload-staging-grid", "selectedRows"),
    Output("upload-confirmation-modal", "className"),
    Output("upload-confirm-button", "disabled"),
    Output("upload-staging-summary", "children"),
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
        return (
            {"rows": []},
            [],
            [],
            "upload-modal",
            True,
            "",
            "未收到任何檔案。",
            "batch-status batch-status--error",
        )
    staging = stage_uploaded_workbooks(contents, filenames)
    grid_rows = _grid_rows(staging)
    valid_rows = [row for row in grid_rows if row["valid"]]
    invalid_count = len(grid_rows) - len(valid_rows)
    summary = (
        f"{len(valid_rows)} 個有效檔案將上傳"
        + (f" · {invalid_count} 個檔案需修正" if invalid_count else "")
    )
    return (
        staging,
        grid_rows,
        valid_rows,
        "upload-modal upload-modal--open",
        not valid_rows,
        summary,
        "",
        "batch-status",
    )


@app.callback(
    Output("upload-batch-date", "date"),
    Output("upload-date-help", "children"),
    Input("upload-staging-grid", "selectedRows"),
    prevent_initial_call=True,
)
def update_staged_selection(selected_rows):
    return selected_date_state(selected_rows)


@app.callback(
    Output("upload-staging-store", "data", allow_duplicate=True),
    Output("upload-staging-grid", "rowData", allow_duplicate=True),
    Input("upload-batch-date", "date"),
    State("upload-staging-grid", "selectedRows"),
    State("upload-staging-store", "data"),
    prevent_initial_call=True,
)
def update_staged_dates(selected_date, selected_rows, staging):
    updated = apply_date_to_selected(staging, selected_rows, selected_date)
    return updated, _grid_rows(updated)


@app.callback(
    Output("upload-confirmation-modal", "className", allow_duplicate=True),
    Output("upload-staging-store", "data", allow_duplicate=True),
    Output("upload-staging-grid", "rowData", allow_duplicate=True),
    Output("upload-staging-grid", "selectedRows", allow_duplicate=True),
    Output("excel-upload", "contents"),
    Output("excel-upload", "filename"),
    Output("upload-status", "children", allow_duplicate=True),
    Output("upload-status", "className", allow_duplicate=True),
    Output("upload-modal-error", "children"),
    Input("upload-cancel-button", "n_clicks"),
    Input("upload-confirm-button", "n_clicks"),
    State("upload-staging-store", "data"),
    prevent_initial_call=True,
    running=[(Output("upload-confirm-button", "disabled"), True, False)],
)
def finish_upload(cancel_clicks, confirm_clicks, staging):
    if ctx.triggered_id == "upload-cancel-button":
        return (
            "upload-modal",
            {"rows": []},
            [],
            [],
            None,
            None,
            "",
            "batch-status",
            "",
        )
    if ctx.triggered_id != "upload-confirm-button" or not confirm_clicks:
        raise PreventUpdate
    try:
        status, class_name = confirm_staged_workbooks(staging)
    except Exception as exc:
        return (
            "upload-modal upload-modal--open",
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            f"無法處理此批次：{exc}。請確認檔案後再試一次。",
        )
    return (
        "upload-modal",
        {"rows": []},
        [],
        [],
        None,
        None,
        status,
        class_name,
        "",
    )


if __name__ == "__main__":
    app.run(debug=True)
