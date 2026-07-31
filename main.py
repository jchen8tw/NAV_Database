import base64
from datetime import date, timedelta
import math
from pathlib import Path
from typing import Any

import polars as pl
import dash_ag_grid as dag
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from src.pages import daily, history, upload
from src.database import (
    ACCOUNT_CODE_COLUMN,
    ASSET_RATIO_COLUMN,
    CURRENCY_COLUMN,
    DATABASE_PATH,
    DATE_COLUMN,
    ISSUE_SIZE_COLUMN,
    NAV_COLUMN,
    NAV_RATIO_COLUMN,
    load_available_dates,
    load_holdings_by_date,
    load_holdings_history,
    load_instrument_observations,
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
    NAV_COLUMN,
    "持有市值(帳戶幣別)",
    "持有市值(標的幣別)",
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
    ASSET_RATIO_COLUMN,
)
FORMAT_LABELS = {"cathay": "國泰世華", "ctbc": "中信"}
NUMERIC_VALUE_FORMATTER = {
    "function": "formatNumber(params.value)"
}


def make_table(
    df: pl.DataFrame, table_id: str = "holdings-table"
) -> dag.AgGrid:
    numeric_columns = set(NUMERIC_COLUMNS)
    columns = []
    for name in df.columns:
        column = {
            "headerName": "幣別" if name == CURRENCY_COLUMN else name,
            "field": name,
            "filter": (
                "agNumberColumnFilter"
                if name in numeric_columns
                else "agTextColumnFilter"
            ),
            "tooltipField": name,
        }
        if name in numeric_columns:
            column["valueFormatter"] = NUMERIC_VALUE_FORMATTER
        columns.append(column)

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
            if key not in {"instrument_values", "source_contents", "source_kind"}
        }
        for row in (staging or {}).get("rows", [])
    ]


def _comparison_value(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _instrument_values(dataframe: pl.DataFrame) -> list[dict[str, Any]]:
    required = {"ISIN", NAV_COLUMN}
    if not required.issubset(dataframe.columns):
        return []
    columns = [
        "ISIN",
        *(
            [ACCOUNT_CODE_COLUMN]
            if ACCOUNT_CODE_COLUMN in dataframe.columns
            else []
        ),
        NAV_COLUMN,
    ]
    values = []
    for row in dataframe.select(columns).iter_rows(named=True):
        isin = str(row.get("ISIN") or "").strip()
        if not isin:
            continue
        values.append(
            {
                "isin": isin,
                "account": str(row.get(ACCOUNT_CODE_COLUMN) or "").strip(),
                NAV_COLUMN: _comparison_value(row.get(NAV_COLUMN)),
            }
        )
    return values


def find_upload_conflicts(
    staging: dict | None,
    database_path: Path = DATABASE_PATH,
    selected_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compare selected staged values with storage and the selected batch."""
    incoming: list[dict[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    for staged in (staging or {}).get("rows", []):
        report_date = staged.get("date")
        if (
            not staged.get("valid")
            or not report_date
            or (
                selected_ids is not None
                and staged.get("id") not in selected_ids
            )
        ):
            continue
        for index, values in enumerate(staged.get("instrument_values", [])):
            isin = str(values.get("isin") or "").strip()
            if not isin:
                continue
            keys.add((isin, report_date))
            # Only 基金淨值 is compared: 發行規模/流通股數 is ambiguous across sources.
            value = _comparison_value(values.get(NAV_COLUMN))
            if value is None:
                continue
            incoming.append(
                {
                    "observation_id": f"{staged['id']}:{index}:{NAV_COLUMN}",
                    "source_id": staged["id"],
                    "filename": staged["filename"],
                    "isin": isin,
                    "date": report_date,
                    "account": str(values.get("account") or "").strip(),
                    "field": NAV_COLUMN,
                    "value": value,
                }
            )

    stored = load_instrument_observations(keys, database_path)
    by_key_field: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for observation in incoming:
        key = (
            observation["isin"],
            observation["date"],
            observation["field"],
        )
        by_key_field.setdefault(key, []).append(observation)

    stored_by_key_field: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = {}
    for observation in stored:
        value = _comparison_value(observation.get(NAV_COLUMN))
        if value is None:
            continue
        key = (
            str(observation["ISIN"]),
            str(observation[DATE_COLUMN]),
            NAV_COLUMN,
        )
        stored_by_key_field.setdefault(key, []).append(
            {
                "value": value,
                "account": str(
                    observation.get(ACCOUNT_CODE_COLUMN) or ""
                ).strip(),
                "source": "database",
            }
        )

    conflicts = []
    for observation in incoming:
        key = (
            observation["isin"],
            observation["date"],
            observation["field"],
        )
        counterparts = [
            candidate
            for candidate in stored_by_key_field.get(key, [])
            if candidate["value"] != observation["value"]
        ]
        counterparts.extend(
            {
                "value": candidate["value"],
                "account": candidate["account"],
                "source": candidate["filename"],
            }
            for candidate in by_key_field.get(key, [])
            if candidate["observation_id"] != observation["observation_id"]
            and candidate["value"] != observation["value"]
        )
        if not counterparts:
            continue

        distinct_counterparts = {
            (
                candidate["value"],
                candidate["account"],
                candidate["source"],
            )
            for candidate in counterparts
        }
        ordered = sorted(
            distinct_counterparts,
            key=lambda item: (str(item[0]), item[1], item[2]),
        )
        conflicts.append(
            {
                "id": observation["observation_id"],
                "source_id": observation["source_id"],
                "filename": observation["filename"],
                "isin": observation["isin"],
                "date": observation["date"],
                "field": observation["field"],
                "incoming_value": observation["value"],
                "existing_values": list(dict.fromkeys(item[0] for item in ordered)),
                "existing_accounts": list(
                    dict.fromkeys(item[1] for item in ordered if item[1])
                ),
                "sources": list(
                    dict.fromkeys(item[2] for item in ordered if item[2])
                ),
            }
        )
    return sorted(
        conflicts,
        key=lambda conflict: (
            conflict["date"],
            conflict["isin"],
            conflict["field"],
            conflict["filename"],
        ),
    )


def refresh_staging_conflicts(
    staging: dict | None,
    database_path: Path = DATABASE_PATH,
    selected_ids: set[str] | None = None,
) -> dict:
    staging = staging or {"rows": []}
    staging["conflicts"] = find_upload_conflicts(
        staging, database_path, selected_ids
    )
    return staging


def _conflict_signature(conflicts: list[dict[str, Any]]) -> tuple:
    return tuple(
        (
            conflict["id"],
            conflict["date"],
            conflict["incoming_value"],
            tuple(conflict["existing_values"]),
            tuple(conflict["existing_accounts"]),
            tuple(conflict["sources"]),
        )
        for conflict in conflicts
    )


def conflict_source_ids(conflicts: list[dict[str, Any]] | None) -> set[str]:
    """Return the staged file IDs represented by conflict records."""
    source_ids = set()
    for conflict in conflicts or []:
        source_id = conflict.get("source_id")
        if source_id:
            source_ids.add(source_id)
            continue
        conflict_id = str(conflict.get("id") or "")
        if conflict_id.count(":") >= 2:
            source_ids.add(conflict_id.rsplit(":", 2)[0])
    return source_ids


def _format_conflict_value(value: int | float) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.12g}"


def make_conflict_panel(conflicts: list[dict[str, Any]] | None):
    conflicts = conflicts or []
    if not conflicts:
        return []
    affected_isins = len({conflict["isin"] for conflict in conflicts})
    rows = []
    for conflict in conflicts:
        existing = "、".join(
            _format_conflict_value(value)
            for value in conflict["existing_values"]
        )
        accounts = "、".join(conflict["existing_accounts"]) or "—"
        rows.append(
            html.Tr(
                [
                    html.Td(
                        [
                            html.Strong(conflict["isin"]),
                            html.Small(
                                f"{_display_date(conflict['date'])} · "
                                f"{conflict['filename']}"
                            ),
                        ]
                    ),
                    html.Td(conflict["field"]),
                    html.Td(
                        _format_conflict_value(conflict["incoming_value"]),
                        className="conflict-value conflict-value--incoming",
                    ),
                    html.Td(existing, className="conflict-value"),
                    html.Td(accounts),
                ]
            )
        )
    return [
        html.Div(
            [
                html.Div("!", className="upload-conflict-icon"),
                html.Div(
                    [
                        html.Strong("偵測到資料不一致"),
                        html.Span(
                            "下列標的當日的基金淨值"
                            "與資料庫或本批次其他檔案不同（空值不列入衝突）。"
                        ),
                        html.Span(
                            "請勾選對應檔案調整套用日期或是取消本次上傳。"
                        ),
                    ],
                    className="upload-conflict-copy",
                ),
                html.Span(
                    f"{affected_isins} 標的 · {len(conflicts)} 項差異",
                    className="upload-conflict-count",
                ),
            ],
            className="upload-conflict-banner",
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.Strong("衝突資料"),
                        html.Span("比對規則：同 ISIN ＋ 同資料日期"),
                    ],
                    className="upload-conflict-table-heading",
                ),
                html.Div(
                    html.Table(
                        [
                            html.Thead(
                                html.Tr(
                                    [
                                        html.Th("ISIN / 日期 / 檔案"),
                                        html.Th("衝突欄位"),
                                        html.Th("本次上傳值"),
                                        html.Th("既有值"),
                                        html.Th("既有專戶"),
                                    ]
                                )
                            ),
                            html.Tbody(rows),
                        ]
                    ),
                    className="upload-conflict-table-scroll",
                ),
            ],
            className="upload-conflict-details",
        ),
    ]


def upload_staging_summary(staging: dict | None) -> str:
    rows = (staging or {}).get("rows", [])
    selected_ids = (
        set(staging["selected_ids"])
        if staging is not None and "selected_ids" in staging
        else {row.get("id") for row in rows if row.get("valid")}
    )
    valid_count = sum(
        bool(row.get("valid")) and row.get("id") in selected_ids
        for row in rows
    )
    invalid_count = len(rows) - valid_count
    conflicts = (staging or {}).get("conflicts", [])
    affected_isins = len({conflict["isin"] for conflict in conflicts})
    parts = []
    processed_count = len((staging or {}).get("upload_results", []))
    if (
        (staging or {}).get("review_phase") == "conflict_review"
        and processed_count
    ):
        parts.append(f"已處理 {processed_count} 個檔案")
    parts.append(f"已選取 {valid_count} 個有效檔案")
    if invalid_count:
        remaining_valid = sum(bool(row.get("valid")) for row in rows) - valid_count
        if remaining_valid:
            parts.append(f"{remaining_valid} 個有效檔案未選取")
        invalid_rows = sum(not row.get("valid") for row in rows)
        if invalid_rows:
            parts.append(f"{invalid_rows} 個檔案需修正")
    if affected_isins:
        parts.append(f"{affected_isins} 個 ISIN 有資料差異")
    return " · ".join(parts)


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
                "instrument_values": (
                    _instrument_values(parsed.dataframe)
                    if parsed is not None
                    else []
                ),
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
    return {
        "rows": rows,
        "conflicts": [],
        "review_phase": "confirmation",
        "reviewed_selection": [],
        "selected_ids": [row["id"] for row in rows if row.get("valid")],
        "upload_results": [],
    }


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


def selected_row_ids(selected_rows: list[dict] | None) -> set[str]:
    return {
        row["id"]
        for row in (selected_rows or [])
        if row.get("valid") and row.get("id")
    }


def update_staging_selection(
    staging: dict | None,
    selected_rows: list[dict] | None,
) -> dict:
    staging = staging or {"rows": []}
    selected_ids = selected_row_ids(selected_rows)
    previous_ids = set(staging.get("selected_ids", []))
    staging["selected_ids"] = sorted(selected_ids)
    if selected_ids != previous_ids:
        staging["review_phase"] = "confirmation"
        staging["reviewed_selection"] = []
        staging["conflicts"] = []
    return staging


def selected_grid_rows(staging: dict | None) -> list[dict]:
    selected_ids = set((staging or {}).get("selected_ids", []))
    return [
        row
        for row in _grid_rows(staging)
        if row.get("valid") and row.get("id") in selected_ids
    ]


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


def _make_upload_result(rows: list[dict]) -> tuple[html.Section, str]:
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


def process_staged_workbooks(
    staging: dict | None,
    selected_ids: set[str] | None = None,
) -> list[dict]:
    """Reparse and independently store the requested staged workbooks."""
    rows: list[dict] = []
    for staged in (staging or {}).get("rows", []):
        if selected_ids is not None and staged.get("id") not in selected_ids:
            continue
        if not staged.get("valid"):
            rows.append(
                {
                    "id": staged.get("id"),
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
                    "id": staged.get("id"),
                    "filename": staged["filename"],
                    "status": "失敗",
                    "detail": str(exc),
                    "imported_rows": 0,
                }
            )
            continue
        rows.append(
            {
                "id": staged.get("id"),
                "filename": staged["filename"],
                "status": "完成",
                "detail": (
                    f"{dataframe.height:,} 列 · {dataframe.width:,} 欄 · "
                    f"資料日期 {staged['date']}"
                ),
                "imported_rows": dataframe.height,
            }
        )
    return rows


def merge_upload_results(
    existing: list[dict] | None,
    latest: list[dict],
) -> list[dict]:
    merged = list(existing or [])
    positions = {row.get("id"): index for index, row in enumerate(merged)}
    for row in latest:
        row_id = row.get("id")
        if row_id in positions:
            merged[positions[row_id]] = row
        else:
            positions[row_id] = len(merged)
            merged.append(row)
    return merged


def confirm_staged_workbooks(
    staging: dict | None,
    selected_ids: set[str] | None = None,
):
    """Store requested staged workbooks and render their result."""
    return _make_upload_result(
        process_staged_workbooks(staging, selected_ids)
    )


@app.callback(
    Output("upload-staging-store", "data"),
    Output("upload-staging-grid", "rowData"),
    Output("upload-staging-grid", "selectedRows"),
    Output("upload-confirmation-modal", "className"),
    Output("upload-confirm-button", "disabled"),
    Output("upload-staging-summary", "children"),
    Output("upload-status", "children"),
    Output("upload-status", "className"),
    Output("upload-conflict-panel", "children"),
    Output("upload-confirm-button", "children"),
    Input("excel-upload", "contents"),
    State("excel-upload", "filename"),
    prevent_initial_call=True,
)
def show_uploaded_workbooks(
    contents: list[str] | None, filenames: list[str] | None
):
    if not contents:
        raise PreventUpdate
    staging = stage_uploaded_workbooks(contents, filenames)
    grid_rows = _grid_rows(staging)
    valid_rows = selected_grid_rows(staging)
    conflicts = staging.get("conflicts", [])
    return (
        staging,
        grid_rows,
        valid_rows,
        "upload-modal upload-modal--open",
        not valid_rows,
        upload_staging_summary(staging),
        "",
        "batch-status",
        make_conflict_panel(conflicts),
        "仍要上傳" if conflicts else "確認上傳",
    )


@app.callback(
    Output("upload-batch-date", "date"),
    Output("upload-date-help", "children"),
    Output("upload-staging-store", "data", allow_duplicate=True),
    Output("upload-conflict-panel", "children", allow_duplicate=True),
    Output("upload-staging-summary", "children", allow_duplicate=True),
    Output("upload-confirm-button", "children", allow_duplicate=True),
    Output("upload-confirm-button", "disabled", allow_duplicate=True),
    Input("upload-staging-grid", "selectedRows"),
    State("upload-staging-store", "data"),
    prevent_initial_call=True,
)
def update_staged_selection(selected_rows, staging):
    updated = update_staging_selection(staging, selected_rows)
    picker_date, help_text = selected_date_state(selected_rows)
    conflicts = updated.get("conflicts", [])
    return (
        picker_date,
        help_text,
        updated,
        make_conflict_panel(conflicts),
        upload_staging_summary(updated),
        "仍要上傳" if conflicts else "確認上傳",
        not selected_row_ids(selected_rows),
    )


@app.callback(
    Output("upload-staging-store", "data", allow_duplicate=True),
    Output("upload-staging-grid", "rowData", allow_duplicate=True),
    Output("upload-conflict-panel", "children", allow_duplicate=True),
    Output("upload-staging-summary", "children", allow_duplicate=True),
    Output("upload-confirm-button", "children", allow_duplicate=True),
    Input("upload-batch-date", "date"),
    State("upload-staging-grid", "selectedRows"),
    State("upload-staging-store", "data"),
    prevent_initial_call=True,
)
def update_staged_dates(selected_date, selected_rows, staging):
    updated = apply_date_to_selected(staging, selected_rows, selected_date)
    if updated.get("review_phase") == "conflict_review":
        updated = refresh_staging_conflicts(
            updated, selected_ids=set(updated.get("selected_ids", []))
        )
    else:
        updated["conflicts"] = []
    conflicts = updated.get("conflicts", [])
    return (
        updated,
        _grid_rows(updated),
        make_conflict_panel(conflicts),
        upload_staging_summary(updated),
        "仍要上傳" if conflicts else "確認上傳",
    )


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
    Output("upload-conflict-panel", "children", allow_duplicate=True),
    Output("upload-confirm-button", "children", allow_duplicate=True),
    Output("upload-staging-summary", "children", allow_duplicate=True),
    Input("upload-cancel-button", "n_clicks"),
    Input("upload-confirm-button", "n_clicks"),
    State("upload-staging-store", "data"),
    State("upload-staging-grid", "selectedRows"),
    prevent_initial_call=True,
    running=[(Output("upload-confirm-button", "disabled"), True, False)],
)
def finish_upload(cancel_clicks, confirm_clicks, staging, selected_rows=None):
    if ctx.triggered_id == "upload-cancel-button":
        preserve_upload_status = (
            (staging or {}).get("review_phase") == "conflict_review"
            and bool((staging or {}).get("upload_results"))
        )
        return (
            "upload-modal",
            {"rows": []},
            [],
            [],
            None,
            None,
            no_update if preserve_upload_status else "",
            no_update if preserve_upload_status else "batch-status",
            "",
            [],
            "確認上傳",
            "",
        )
    if ctx.triggered_id != "upload-confirm-button" or not confirm_clicks:
        raise PreventUpdate
    try:
        staging = staging or {"rows": []}
        staging = update_staging_selection(staging, selected_rows)
        selected_ids = set(staging.get("selected_ids", []))
        if not selected_ids:
            return (
                "upload-modal upload-modal--open",
                staging,
                _grid_rows(staging),
                [],
                no_update,
                no_update,
                no_update,
                no_update,
                "請先勾選要檢查並上傳的檔案。",
                [],
                "確認上傳",
                upload_staging_summary(staging),
            )
        reviewing_conflicts = (
            staging.get("review_phase") == "conflict_review"
            and set(staging.get("reviewed_selection", [])) == selected_ids
        )
        previous_signature = _conflict_signature(staging.get("conflicts", []))
        staging = refresh_staging_conflicts(
            staging, selected_ids=selected_ids
        )
        staging["review_phase"] = "conflict_review"
        staging["reviewed_selection"] = sorted(selected_ids)
        current_conflicts = staging.get("conflicts", [])
        current_signature = _conflict_signature(current_conflicts)
        if current_conflicts and not reviewing_conflicts:
            conflicted_ids = conflict_source_ids(current_conflicts)
            clean_ids = selected_ids - conflicted_ids
            if clean_ids:
                clean_results = process_staged_workbooks(staging, clean_ids)
                staging["upload_results"] = merge_upload_results(
                    staging.get("upload_results"), clean_results
                )
                staging["rows"] = [
                    row
                    for row in staging.get("rows", [])
                    if row.get("id") not in clean_ids
                ]
            staging["selected_ids"] = sorted(conflicted_ids)
            staging["reviewed_selection"] = sorted(conflicted_ids)
            status = no_update
            class_name = no_update
            if clean_ids:
                status, class_name = _make_upload_result(
                    staging["upload_results"]
                )
            return (
                "upload-modal upload-modal--open",
                staging,
                _grid_rows(staging),
                selected_grid_rows(staging),
                no_update,
                no_update,
                status,
                class_name,
                "",
                make_conflict_panel(current_conflicts),
                "仍要上傳",
                upload_staging_summary(staging),
            )
        if reviewing_conflicts and current_signature != previous_signature:
            return (
                "upload-modal upload-modal--open",
                staging,
                _grid_rows(staging),
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                "資料庫內容在確認前已變更，衝突清單已更新。請重新檢視後再確認。",
                make_conflict_panel(current_conflicts),
                "仍要上傳" if current_conflicts else "確認上傳",
                upload_staging_summary(staging),
            )
        latest_results = process_staged_workbooks(staging, selected_ids)
        staging["upload_results"] = merge_upload_results(
            staging.get("upload_results"), latest_results
        )
        successful_ids = {
            row["id"]
            for row in latest_results
            if row["status"] == "完成"
        }
        staging["rows"] = [
            row
            for row in staging.get("rows", [])
            if row.get("id") not in successful_ids
        ]
        staging["selected_ids"] = sorted(selected_ids - successful_ids)
        staging["review_phase"] = "confirmation"
        staging["reviewed_selection"] = []
        staging["conflicts"] = []
        status, class_name = _make_upload_result(staging["upload_results"])
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
            no_update,
            no_update,
            no_update,
        )
    valid_rows_remain = any(
        row.get("valid") for row in staging.get("rows", [])
    )
    if valid_rows_remain:
        return (
            "upload-modal upload-modal--open",
            staging,
            _grid_rows(staging),
            selected_grid_rows(staging),
            no_update,
            no_update,
            status,
            class_name,
            "",
            [],
            "確認上傳",
            upload_staging_summary(staging),
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
        [],
        "確認上傳",
        "",
    )


if __name__ == "__main__":
    app.run(debug=True)
