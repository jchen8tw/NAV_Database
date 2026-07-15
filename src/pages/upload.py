from dash import dcc, html


def layout() -> html.Div:
    return html.Div(
        children=[
            html.Header(
                className="hero",
                children=[
                    html.H1("投資組合資料庫"),
                    html.P("上傳excel以存入資料庫", className="subtitle"),
                ],
            ),
            dcc.Loading(
                type="circle",
                children=html.Div(
                    id="upload-status", className="status", role="status"
                ),
            ),
            dcc.Upload(
                id="excel-upload",
                className="upload-box",
                className_active="upload-box upload-box--active",
                accept=".xlsx,.xls",
                multiple=False,
                children=html.Div(
                    [
                        html.Div("↑", className="upload-icon"),
                        html.Strong("Drop Excel files here"),
                        html.Span("or click to choose files", className="upload-hint"),
                        html.Span(
                            "Accepted formats: .xlsx, .xls", className="file-types"
                        ),
                    ]
                ),
            ),
            dcc.Loading(
                type="circle",
                children=html.Section(
                    id="table-container", className="table-card upload-preview"
                ),
            ),
        ]
    )
