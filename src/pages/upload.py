from dash import dcc, html


def layout() -> html.Div:
    return html.Div(
        children=[
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
                accept=".xlsx,.xls",
                multiple=True,
                children=html.Div(
                    [
                        html.Div("↑", className="upload-icon"),
                        html.Strong("將多個 Excel 檔案拖曳到這裡"),
                        html.Span(
                            "或點擊選擇檔案（可一次選取多個）",
                            className="upload-hint",
                        ),
                        html.Span(
                            "支援 .xlsx、.xls",
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
        ]
    )
