import base64
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import polars as pl
from dash import dash_table, html

import main
from src import database, reader
from src.pages import daily, history, upload


def text_content(component) -> list[str]:
    if isinstance(component, str):
        return [component]
    if isinstance(component, (list, tuple)):
        return [text for child in component for text in text_content(child)]
    return text_content(getattr(component, "children", []))


class WorkbookParsingTests(unittest.TestCase):
    def test_find_report_date_accepts_supported_values(self):
        for value, expected in (
            (datetime(2025, 2, 3, 12, 30), "2025-02-03"),
            (date(2025, 2, 3), "2025-02-03"),
            ("2025/02/03", "2025-02-03"),
            ("20250203", "2025-02-03"),
        ):
            with self.subTest(value=value):
                sheet = pl.DataFrame([["檢查日期：", value]], orient="row")
                self.assertEqual(reader.find_report_date(sheet), expected)

    def test_find_report_date_rejects_missing_or_invalid_date(self):
        for sheet in (
            pl.DataFrame([["other", "value"]], orient="row"),
            pl.DataFrame([["檢查日期", "03-02-2025"]], orient="row"),
        ):
            with self.subTest(sheet=sheet.to_dicts()):
                with self.assertRaisesRegex(ValueError, "Invalid or missing 檢查日期"):
                    reader.find_report_date(sheet)

    def test_parse_excel_decodes_cleans_and_types_holdings(self):
        headers = [
            "ISIN", "庫存單位數", "基金淨值/ETF收盤價", "持有市值(帳戶幣別)",
            "持有市值(標的幣別)", "標的發行規模（標的幣別）或流通股數",
            "佔淨資產比重(%)", "佔標的資產或單位數比重(%)",
        ]
        rows = [
            ["專戶代號：", " A001 ", "專戶名稱:", " Alpha Fund ", *([""] * 4)],
            ["檢查日期", "2025/02/03", *([""] * 6)],
            *([["metadata", *([""] * 7)]] * 4),
            headers,
            [" TW0001 ", "1,200", "10.5", "2,400", "2,500", "3,000", "12.5%", "4%"],
            ["footer", *([""] * 7)],
        ]
        worksheet = pl.DataFrame(rows, orient="row", schema=[f"column_{i}" for i in range(8)])
        contents = "data:application/octet-stream;base64," + base64.b64encode(b"workbook").decode()

        with patch("src.reader.pl.read_excel", return_value=worksheet) as read_excel:
            result = reader.parse_excel(contents)

        read_excel.assert_called_once_with(unittest.mock.ANY, has_header=False)
        self.assertEqual(result.height, 1)
        self.assertEqual(result["ISIN"].item(), "TW0001")
        self.assertEqual(result["庫存單位數"].item(), 1200.0)
        self.assertEqual(result["佔淨資產比重(%)"].item(), 12.5)
        self.assertEqual(result[database.DATE_COLUMN].item(), "2025-02-03")
        self.assertEqual(result[reader.ACCOUNT_CODE_COLUMN].item(), "A001")
        self.assertEqual(result[reader.ACCOUNT_NAME_COLUMN].item(), "Alpha Fund")

    def test_parse_excel_rejects_missing_account_metadata(self):
        worksheet = pl.DataFrame(
            [["檢查日期", "2025/02/03"], *([["metadata", ""]] * 6)],
            orient="row",
        )
        contents = "data:application/octet-stream;base64," + base64.b64encode(b"workbook").decode()

        with patch("src.reader.pl.read_excel", return_value=worksheet):
            with self.assertRaisesRegex(ValueError, "Invalid or missing 專戶代號"):
                reader.parse_excel(contents)

    def test_parse_excel_rejects_invalid_base64(self):
        with self.assertRaisesRegex(ValueError, "could not be decoded"):
            reader.parse_excel("data:application/octet-stream;base64,not-base64!")


class DataHelpersTests(unittest.TestCase):
    def test_identifier_type_and_value_helpers(self):
        self.assertEqual(database.quote_identifier('a"b'), '"a""b"')
        self.assertEqual(database.sqlite_type(pl.Int64), "INTEGER")
        self.assertEqual(database.sqlite_type(pl.Float32), "REAL")
        self.assertEqual(database.sqlite_type(pl.Binary), "BLOB")
        self.assertEqual(database.sqlite_type(pl.String), "TEXT")
        self.assertEqual(database.sqlite_value(date(2025, 1, 2)), "2025-01-02")

    def test_find_isin_column_prefers_exact_normalized_match(self):
        df = pl.DataFrame({"related isin code": ["x"], " ISIN ": ["y"]})
        self.assertEqual(database.find_isin_column(df), " ISIN ")
        with self.assertRaisesRegex(ValueError, "ISIN column"):
            database.find_isin_column(pl.DataFrame({"name": ["x"]}))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "test.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def holdings(rows):
        row_count = len(next(iter(rows.values())))
        rows = {
            **rows,
            database.ACCOUNT_NAME_COLUMN: ["Alpha Fund"] * row_count,
            database.ISSUE_SIZE_COLUMN: [1_000_000] * row_count,
            database.NAV_RATIO_COLUMN: [1.5] * row_count,
            database.ASSET_RATIO_COLUMN: [0.25] * row_count,
        }
        return pl.DataFrame(
            rows,
            schema={
                "ISIN": pl.String,
                database.DATE_COLUMN: pl.String,
                "標的名稱": pl.String,
                "標的種類": pl.String,
                "類型別": pl.String,
                "庫存單位數": pl.Float64,
                "基金淨值/ETF收盤價": pl.Float64,
                "持有市值(標的幣別)": pl.Float64,
                database.ACCOUNT_NAME_COLUMN: pl.String,
                database.ISSUE_SIZE_COLUMN: pl.Int64,
                database.NAV_RATIO_COLUMN: pl.Float64,
                database.ASSET_RATIO_COLUMN: pl.Float64,
            },
        ).with_columns(pl.lit("A001").alias(database.ACCOUNT_CODE_COLUMN))

    def test_store_upserts_by_isin_date_and_account_and_adds_columns(self):
        original = self.holdings({
            "ISIN": ["F1"], database.DATE_COLUMN: ["2025-01-01"], "標的名稱": ["Fund"],
            "標的種類": ["基金"], "類型別": ["A"], "庫存單位數": [2.0],
            "基金淨值/ETF收盤價": [10.0], "持有市值(標的幣別)": [20.0],
        })
        self.assertEqual(database.store_dataframe(original, self.database), 1)
        updated = original.with_columns(pl.lit(11.5).alias("基金淨值/ETF收盤價"), pl.lit("new").alias("備註"))
        self.assertEqual(database.store_dataframe(updated, self.database), 1)

        with sqlite3.connect(self.database) as connection:
            row = connection.execute('SELECT "基金淨值/ETF收盤價", "備註" FROM holdings').fetchone()
        self.assertEqual(row, (11.5, "new"))

        other_account = original.with_columns(
            pl.lit("A002").alias(database.ACCOUNT_CODE_COLUMN)
        )
        self.assertEqual(database.store_dataframe(other_account, self.database), 2)

    def test_store_adds_account_metadata_columns(self):
        holdings = self.holdings({
            "ISIN": ["F1"], database.DATE_COLUMN: ["2025-01-01"], "標的名稱": ["Fund"],
            "標的種類": ["基金"], "類型別": ["A"], "庫存單位數": [2.0],
            "基金淨值/ETF收盤價": [10.0], "持有市值(標的幣別)": [20.0],
        }).with_columns(
            pl.lit("A001").alias(reader.ACCOUNT_CODE_COLUMN),
            pl.lit("Alpha Fund").alias(reader.ACCOUNT_NAME_COLUMN),
        )

        database.store_dataframe(holdings, self.database)

        with sqlite3.connect(self.database) as connection:
            table_info = connection.execute("PRAGMA table_info(holdings)").fetchall()
            columns = {row[1] for row in table_info}
            primary_key = [
                row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]
            ]
            values = connection.execute('SELECT "專戶代號", "專戶名稱" FROM holdings').fetchone()
        self.assertTrue({"專戶代號", "專戶名稱"}.issubset(columns))
        self.assertEqual(primary_key, ["ISIN", "檢查日期", "專戶代號"])
        self.assertEqual(values, ("A001", "Alpha Fund"))

    def test_store_rejects_empty_blank_and_duplicate_keys(self):
        with self.assertRaisesRegex(ValueError, "no data rows"):
            database.store_dataframe(
                pl.DataFrame(
                    schema={
                        "ISIN": pl.String,
                        database.DATE_COLUMN: pl.String,
                        database.ACCOUNT_CODE_COLUMN: pl.String,
                    }
                ),
                self.database,
            )
        for values, message in ((["", "F2"], "cannot be blank"), (["F1", "F1"], "duplicate")):
            df = pl.DataFrame({
                "ISIN": values,
                database.DATE_COLUMN: ["2025-01-01", "2025-01-01"],
                database.ACCOUNT_CODE_COLUMN: ["A001", "A001"],
            })
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                database.store_dataframe(df, self.database)

    def test_loaders_filter_sort_and_aggregate(self):
        data = self.holdings({
            "ISIN": ["F1", "F1", "E1"],
            database.DATE_COLUMN: ["2025-01-01", "2025-01-02", "2025-01-02"],
            "標的名稱": ["Z Fund", "Z Fund", "A ETF"],
            "標的種類": ["基金", "基金", "ETF"], "類型別": ["A", "A", "B"],
            "庫存單位數": [1.0, 2.0, 3.0], "基金淨值/ETF收盤價": [10.0, 12.0, 20.0],
            "持有市值(標的幣別)": [10.0, 24.0, 60.0],
        })
        database.store_dataframe(data, self.database)

        self.assertTrue(database.holdings_table_is_ready(self.database))
        self.assertEqual(database.load_available_dates(self.database), ["2025-01-01", "2025-01-02"])
        daily_holdings = database.load_holdings_by_date("2025-01-02", self.database)
        self.assertEqual(daily_holdings.columns, database.HOLDINGS_COLUMNS)
        self.assertEqual(daily_holdings.height, 2)
        self.assertEqual(daily_holdings[database.ACCOUNT_CODE_COLUMN].to_list(), ["A001", "A001"])
        self.assertEqual(daily_holdings[database.ACCOUNT_NAME_COLUMN].to_list(), ["Alpha Fund", "Alpha Fund"])
        self.assertEqual(daily_holdings[database.ISSUE_SIZE_COLUMN].to_list(), [1_000_000, 1_000_000])
        self.assertEqual(daily_holdings[database.NAV_RATIO_COLUMN].to_list(), [1.5, 1.5])
        self.assertEqual(daily_holdings[database.ASSET_RATIO_COLUMN].to_list(), [0.25, 0.25])
        history_rows = database.load_nav_history(self.database)
        self.assertEqual([(row["report_date"], row["nav"]) for row in history_rows], [("2025-01-01", 10.0), ("2025-01-02", 12.0)])

    def test_nav_history_uses_median_across_accounts(self):
        data = self.holdings({
            "ISIN": ["F1", "F1", "F1"],
            database.DATE_COLUMN: ["2025-01-02"] * 3,
            "標的名稱": ["Fund"] * 3,
            "標的種類": ["基金"] * 3,
            "類型別": ["A"] * 3,
            "庫存單位數": [1.0] * 3,
            "基金淨值/ETF收盤價": [12.0, 12.1, 999.0],
            "持有市值(標的幣別)": [12.0, 12.1, 999.0],
        }).with_columns(
            pl.Series(database.ACCOUNT_CODE_COLUMN, ["A001", "A002", "A003"])
        )
        database.store_dataframe(data, self.database)

        self.assertEqual(database.load_nav_history(self.database)[0]["nav"], 12.1)

    def test_loaders_return_empty_results_without_ready_database(self):
        self.assertFalse(database.holdings_table_is_ready(self.database))
        self.assertEqual(database.load_available_dates(self.database), [])
        self.assertEqual(database.load_nav_history(self.database), [])
        self.assertTrue(database.load_holdings_by_date(None, self.database).is_empty())


class PresentationTests(unittest.TestCase):
    def test_application_copy_matches_holdings_database_design(self):
        self.assertEqual(main.app.title, "Holdings Database")
        self.assertIn("Holdings Database", text_content(main.app.layout))

        upload_copy = text_content(upload.layout())
        self.assertIn("投資組合資料庫", upload_copy)
        self.assertIn("上傳excel以存入資料庫", upload_copy)
        self.assertIn("Drop Excel files here", upload_copy)
        self.assertIn("or click to choose files", upload_copy)

        daily_copy = text_content(daily.layout([], lambda _: pl.DataFrame(), main.make_table))
        self.assertIn("單日的所有基金與全委帳戶的資料", daily_copy)
        self.assertIn("選擇要檢視的單日資料", daily_copy)

        history_copy = text_content(history.layout([], main.make_history_figure))
        self.assertIn("每條線代表一個 ISIN；跨帳戶的同日資料已合併", history_copy)

    def test_history_figure_groups_series_and_handles_empty_state(self):
        observations = [
            {"report_date": "2025-01-01", "isin": "F1", "fund_name": "Fund 1", "nav": 10.0},
            {"report_date": "2025-01-02", "isin": "F1", "fund_name": "Fund 1", "nav": 11.0},
            {"report_date": "2025-01-01", "isin": "F2", "fund_name": "Fund 2", "nav": 20.0},
        ]
        figure = main.make_history_figure(observations)
        self.assertEqual(len(figure.data), 2)
        self.assertEqual(list(figure.data[0].x), ["2025-01-01", "2025-01-02"])
        self.assertTrue(figure.layout.xaxis.rangeslider.visible)
        empty = main.make_history_figure([])
        self.assertEqual(len(empty.layout.annotations), 1)
        self.assertFalse(empty.layout.xaxis.visible)

    def test_make_table_marks_numeric_columns(self):
        table = main.make_table(
            pl.DataFrame(
                {
                    database.ACCOUNT_CODE_COLUMN: ["A001"],
                    database.ACCOUNT_NAME_COLUMN: ["Alpha Fund"],
                    "ISIN": ["F1"],
                    database.ISSUE_SIZE_COLUMN: [1_000_000],
                    database.NAV_RATIO_COLUMN: [1.5],
                    database.ASSET_RATIO_COLUMN: [0.25],
                }
            ),
            "custom",
        )
        self.assertIsInstance(table, dash_table.DataTable)
        self.assertEqual(table.id, "custom")
        self.assertEqual(
            [column["type"] for column in table.columns],
            ["text", "text", "text", "numeric", "numeric", "numeric"],
        )
        self.assertEqual(table.style_table["overflowX"], "auto")
        self.assertEqual(table.style_header["whiteSpace"], "normal")

    def test_page_helpers_cover_empty_and_date_ranges(self):
        empty = daily.make_table_content(pl.DataFrame(), None, main.make_table)
        self.assertIsInstance(empty, html.Div)
        self.assertIn("尚無資料", empty.children)
        no_rows = daily.make_table_content(pl.DataFrame(), "2025-01-01", main.make_table)
        self.assertIn("所選日期", no_rows.children)
        self.assertEqual(history.format_range([]), "尚無日期")
        self.assertEqual(history.format_range([{"report_date": "2025-01-01"}]), "2025 / 01 / 01")
        self.assertEqual(history.format_range([{"report_date": "2025-02-01"}, {"report_date": "2025-01-01"}]), "2025 / 01 / 01 — 2025 / 02 / 01")

    def test_upload_callback_validates_input(self):
        self.assertEqual(main.show_uploaded_workbook(None, None), ("No file was received.", "status status--error", None))
        message, class_name, table = main.show_uploaded_workbook("data:text/plain;base64,eA==", "report.txt")
        self.assertIn("Excel file", message)
        self.assertEqual(class_name, "status status--error")
        self.assertIsNone(table)


if __name__ == "__main__":
    unittest.main()
