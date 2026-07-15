from collections.abc import Callable

import polars as pl
from dash import dcc, html


def make_table_content(
    df: pl.DataFrame,
    report_date: str | None,
    table_factory: Callable,
):
    if not report_date:
        return html.Div("尚無資料。請先上傳 Excel 報表。", className="empty-state")
    if df.is_empty():
        return html.Div("所選日期沒有資料。", className="empty-state")
    return table_factory(df, table_id="daily-holdings-table")


def layout(
    dates: list[str],
    holdings_loader: Callable[[str | None], pl.DataFrame],
    table_factory: Callable,
) -> html.Div:
    selected_date = dates[-1] if dates else None
    holdings = holdings_loader(selected_date)
    return html.Div(
        children=[
            html.Header(
                className="hero daily-hero",
                children=[
                    html.H1("單日淨值資料"),
                    html.P(
                        "單日的所有基金與全委帳戶的資料",
                        className="subtitle",
                    ),
                ],
            ),
            html.Section(
                className="date-picker-panel",
                children=[
                    html.Div(
                        [
                            html.Strong("資料日期"),
                            html.Span("選擇要檢視的單日資料"),
                        ],
                        className="date-picker-copy",
                    ),
                    dcc.DatePickerSingle(
                        id="daily-date-picker",
                        date=selected_date,
                        min_date_allowed=dates[0] if dates else None,
                        max_date_allowed=dates[-1] if dates else None,
                        initial_visible_month=selected_date,
                        display_format="YYYY / MM / DD",
                        className="date-picker-control",
                    ),
                ],
            ),
            dcc.Loading(
                type="circle",
                children=html.Section(
                    id="daily-table-container",
                    className="table-card daily-table-card",
                    children=make_table_content(
                        holdings, selected_date, table_factory
                    ),
                ),
            ),
        ]
    )
