import dash_ag_grid as dag
from dash import dcc, html


def staging_grid() -> dag.AgGrid:
    return dag.AgGrid(
        id="upload-staging-grid",
        className="upload-staging-grid",
        columnDefs=[
            {
                "headerName": "檔案名稱",
                "field": "filename",
                "flex": 3,
                "minWidth": 260,
                "tooltipField": "filename",
            },
            {
                "headerName": "格式",
                "field": "format_label",
                "flex": 1,
                "minWidth": 120,
                "cellStyle": {
                    "styleConditions": [
                        {
                            "condition": "params.value === '國泰世華'",
                            "style": {
                                "color": "#007a66",
                                "fontWeight": 700,
                            },
                        },
                        {
                            "condition": "params.value === '中信'",
                            "style": {
                                "color": "#0057a8",
                                "fontWeight": 700,
                            },
                        },
                        {
                            "condition": "!params.data.valid",
                            "style": {
                                "color": "#b91c1c",
                                "fontWeight": 700,
                            },
                        },
                    ]
                },
            },
            {
                "headerName": "設定日期",
                "field": "display_date",
                "flex": 1,
                "minWidth": 140,
            },
            {
                "headerName": "狀態",
                "field": "error",
                "flex": 2,
                "minWidth": 220,
                "cellStyle": {"color": "#b91c1c"},
            },
        ],
        rowData=[],
        selectedRows=[],
        defaultColDef={"resizable": True, "sortable": True},
        dashGridOptions={
            "rowSelection": {
                "mode": "multiRow",
                "checkboxes": True,
                "headerCheckbox": True,
                "enableClickSelection": True,
                "isRowSelectable": {
                    "function": "params.data && params.data.valid"
                },
            },
            "getRowId": {"function": "params.data.id"},
            "suppressRowClickSelection": False,
            "tooltipShowDelay": 300,
            "theme": {
                "function": (
                    "themeQuartz.withParams({"
                    "fontFamily: 'Inter, system-ui, sans-serif',"
                    "fontSize: 13,"
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
                    "condition": "!params.data.valid",
                    "style": {
                        "backgroundColor": "#fff7f7",
                        "opacity": "0.68",
                    },
                },
                {
                    "condition": "params.node.rowIndex % 2 === 1",
                    "style": {"backgroundColor": "#fafbfd"},
                },
            ]
        },
        style={"height": "360px", "width": "100%"},
    )


def layout() -> html.Div:
    return html.Div(
        children=[
            dcc.Store(id="upload-staging-store"),
            html.Header(
                className="hero",
                children=[
                    html.H1("投資組合資料庫"),
                    html.P(
                        "請將保管銀行的越權報表上傳，支援世華銀行與中信銀行格式",
                        className="subtitle",
                    ),
                ],
            ),
            dcc.Upload(
                id="excel-upload",
                className="upload-box",
                className_active="upload-box upload-box--active",
                accept=".xlsx,.xls,.msg",
                multiple=True,
                children=html.Div(
                    [
                        html.Div("↑", className="upload-icon"),
                        html.Strong("將 Excel 或 Outlook 訊息檔拖曳到這裡"),
                        html.Span(
                            "或點擊選擇報表檔案（可一次選取多個）",
                            className="upload-hint",
                        ),
                        html.Span(
                            "支援 .xlsx、.xls、.msg",
                            className="file-types",
                        ),
                    ]
                ),
            ),
            dcc.Loading(
                type="circle",
                children=html.Div(
                    id="upload-status",
                    className="batch-status",
                    role="status",
                    **{"aria-live": "polite"},
                ),
            ),
            html.Div(
                id="upload-confirmation-modal",
                className="upload-modal",
                role="dialog",
                **{
                    "aria-modal": "true",
                    "aria-labelledby": "upload-modal-title",
                },
                children=[
                    html.Div(className="upload-modal-backdrop"),
                    html.Section(
                        className="upload-modal-dialog",
                        children=[
                            html.Header(
                                className="upload-modal-header",
                                children=[
                                    html.H2(
                                        "確認上傳檔案",
                                        id="upload-modal-title",
                                    ),
                                    html.P(
                                        "勾選的列用於批次設定日期；所有格式有效的"
                                        "檔案仍會在確認後上傳。"
                                    ),
                                ],
                            ),
                            html.Div(
                                className="upload-modal-body",
                                children=[
                                    html.Div(
                                        className="upload-date-editor",
                                        children=[
                                            html.Div(
                                                className="upload-date-copy",
                                                children=[
                                                    html.Strong(
                                                        "套用日期至已選檔案"
                                                    ),
                                                    html.Span(
                                                        id="upload-date-help"
                                                    ),
                                                ],
                                            ),
                                            dcc.DatePickerSingle(
                                                id="upload-batch-date",
                                                display_format="YYYY/MM/DD",
                                                clearable=True,
                                                className=(
                                                    "date-picker-control "
                                                    "upload-date-picker"
                                                ),
                                            ),
                                        ],
                                    ),
                                    staging_grid(),
                                    html.Div(
                                        id="upload-modal-error",
                                        className="upload-modal-error",
                                        role="alert",
                                    ),
                                ],
                            ),
                            html.Footer(
                                className="upload-modal-footer",
                                children=[
                                    html.Span(id="upload-staging-summary"),
                                    html.Div(
                                        className="upload-modal-actions",
                                        children=[
                                            html.Button(
                                                "取消",
                                                id="upload-cancel-button",
                                                className=(
                                                    "upload-modal-button "
                                                    "upload-modal-button--secondary"
                                                ),
                                                n_clicks=0,
                                            ),
                                            html.Button(
                                                "確認上傳",
                                                id="upload-confirm-button",
                                                className=(
                                                    "upload-modal-button "
                                                    "upload-modal-button--primary"
                                                ),
                                                disabled=True,
                                                n_clicks=0,
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
        ]
    )
