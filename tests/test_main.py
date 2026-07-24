import base64
import csv
from io import BytesIO, StringIO
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def find_component(component, component_id):
    if getattr(component, "id", None) == component_id:
        return component
    children = getattr(component, "children", [])
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        found = find_component(child, component_id)
        if found is not None:
            return found
    return None


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
            "ISIN", "庫存單位數", "基金淨值/ETF收盤價", "商品幣別", "持有市值(帳戶幣別)",
            "持有市值(標的幣別)", "標的發行規模（標的幣別）或流通股數",
            "佔淨資產比重(%)", "佔標的資產或單位數比重(%)",
        ]
        rows = [
            ["專戶代號：", " A001 ", "專戶名稱:", " Alpha Fund ", *([""] * 5)],
            ["檢查日期", "2025/02/03", *([""] * 7)],
            *([["metadata", *([""] * 8)]] * 4),
            headers,
            [" TW0001 ", "1,200", "10.5", " USD ", "2,400", "2,500", "3,000", "12.5%", "4%"],
            ["footer", *([""] * 8)],
        ]
        worksheet = pl.DataFrame(rows, orient="row", schema=[f"column_{i}" for i in range(9)])
        contents = "data:application/octet-stream;base64," + base64.b64encode(b"workbook").decode()

        with patch("src.reader.pl.read_excel", return_value=worksheet) as read_excel:
            result = reader.parse_excel(contents)

        read_excel.assert_called_once_with(
            unittest.mock.ANY,
            sheet_id=0,
            has_header=False,
            raise_if_empty=False,
            drop_empty_rows=False,
            drop_empty_cols=False,
            infer_schema_length=None,
        )
        self.assertEqual(result.height, 1)
        self.assertEqual(result["ISIN"].item(), "TW0001")
        self.assertEqual(result["庫存單位數"].item(), 1200.0)
        self.assertEqual(result[database.CURRENCY_COLUMN].item(), "USD")
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

    def test_parse_excel_bytes_uses_workbook_bytes_directly(self):
        worksheet = pl.DataFrame([["summary"]], orient="row")
        with (
            patch("src.reader.pl.read_excel", return_value=worksheet) as read_excel,
            self.assertRaisesRegex(ValueError, "recognized holdings table"),
        ):
            reader.parse_excel_bytes(b"workbook")
        self.assertEqual(read_excel.call_args.args[0].read(), b"workbook")

    def test_parse_supplied_legacy_workbook(self):
        contents = "data:application/vnd.ms-excel;base64," + base64.b64encode(
            (Path("sample") / "越權檢核115-07-08.xls").read_bytes()
        ).decode()

        result = reader.parse_excel(contents)

        self.assertEqual(result.height, 19)
        self.assertEqual(result[database.ACCOUNT_CODE_COLUMN].unique().to_list(), ["S000000002001"])
        self.assertEqual(result[database.DATE_COLUMN].unique().to_list(), ["2026-07-08"])
        self.assertEqual(result[database.ACCOUNT_NAME_COLUMN].item(0), "國泰人壽委託聯博投信投資帳戶-多元守護")
        self.assertEqual(result["ISIN"].item(0), "US4642874329")
        self.assertEqual(result["標的名稱"].item(0), "ISHARES 20+ YEAR TREASURY BOND ETF")
        self.assertEqual(result["庫存單位數"].item(0), 75273.0)
        self.assertEqual(result["基金淨值/ETF收盤價"].item(0), 84.36)
        self.assertEqual(result[database.CURRENCY_COLUMN].item(0), "USD")
        self.assertEqual(result["持有市值(帳戶幣別)"].item(0), 6350030.28)
        self.assertEqual(result[database.ISSUE_SIZE_COLUMN].dtype, pl.Int64)
        self.assertEqual(result[database.ISSUE_SIZE_COLUMN].item(16), 45223366556666)
        self.assertEqual(result[database.NAV_RATIO_COLUMN].item(0), 3.75)
        self.assertEqual(result[database.ASSET_RATIO_COLUMN].item(0), 0.0313)
        self.assertIsNone(result["標的種類"].item(0))
        self.assertIsNone(result["類型別"].item(0))
        self.assertEqual(result["持有市值(標的幣別)"].item(0), 6350030.28)

    def test_parse_combines_recognized_sheets_and_rejects_unrecognized_workbook(self):
        headers = [*reader.SECOND_FORMAT_COLUMNS.values(), "市值(EUR)"]
        def sheet(isin):
            values = ["Account", "115-07-08", isin, "Fund", "1,000", "10", "EUR", "3,000", "4%", "0.5%", "2,000"]
            return pl.DataFrame([headers, values], orient="row")
        contents = "data:x;base64," + base64.b64encode(b"book").decode()
        with patch("src.reader.pl.read_excel", return_value={"A001": sheet("F1"), "empty": pl.DataFrame(), "A002": sheet("F2")}):
            result = reader.parse_excel(contents)
        self.assertEqual(result[database.ACCOUNT_CODE_COLUMN].to_list(), ["A001", "A002"])
        self.assertEqual(result["持有市值(帳戶幣別)"].to_list(), [2000.0, 2000.0])
        self.assertEqual(result["持有市值(標的幣別)"].to_list(), [2000.0, 2000.0])
        with patch("src.reader.pl.read_excel", return_value={"summary": pl.DataFrame([["summary"]], orient="row")}):
            with self.assertRaisesRegex(ValueError, "recognized holdings table"):
                reader.parse_excel(contents)

    def test_second_format_leaves_target_market_value_empty_for_other_currency(self):
        headers = [*reader.SECOND_FORMAT_COLUMNS.values(), "市值（ USD ）"]
        values = ["Account", "115-07-08", "F1", "Fund", "1", "10", "JPY", "3,000", "4%", "0.5%", "2,000"]
        contents = "data:x;base64," + base64.b64encode(b"book").decode()
        with patch(
            "src.reader.pl.read_excel",
            return_value={"A001": pl.DataFrame([headers, values], orient="row")},
        ):
            result = reader.parse_excel(contents)
        self.assertEqual(result["持有市值(帳戶幣別)"].item(), 2000.0)
        self.assertIsNone(result["持有市值(標的幣別)"].item())

    def test_msg_extraction_selects_all_qualifying_zip_attachments(self):
        cathay_zip = self._zip_bytes(
            {
                "DC029_20260626.xlsx": b"cathay",
                "nested/DC030_20260626.xlsx": b"nested",
                "ignore.txt": b"ignore",
            }
        )
        dated_zip = self._zip_bytes(
            {
                "越權檢核115-7-8.xls": b"dated",
                "DC029_20260626.xlsx": b"wrong family",
            }
        )
        attachments = [
            self._attachment("國壽越權報表_20260626.zip", cathay_zip),
            self._attachment("20260708_reports.zip", dated_zip),
            self._attachment("other.zip", cathay_zip),
        ]
        message = MagicMock()
        message.__enter__.return_value.attachments = attachments
        with patch("src.reader.extract_msg.openMsg", return_value=message) as open_msg:
            results = reader.extract_msg_workbooks(b"message")

        open_msg.assert_called_once_with(b"message")
        self.assertEqual([result.contents for result in results], [b"cathay", b"dated"])
        self.assertEqual(
            [result.label for result in results],
            [
                "國壽越權報表_20260626.zip › DC029_20260626.xlsx",
                "20260708_reports.zip › 越權檢核115-7-8.xls",
            ],
        )

    def test_msg_extraction_reports_invalid_msg_missing_zip_and_corrupt_archive(self):
        with patch("src.reader.extract_msg.openMsg", side_effect=OSError("bad data")):
            with self.assertRaisesRegex(ValueError, "無法讀取 Outlook MSG"):
                reader.extract_msg_workbooks(b"bad")

        message = MagicMock()
        message.__enter__.return_value.attachments = [
            self._attachment("unrelated.zip", b"not a zip")
        ]
        with patch("src.reader.extract_msg.openMsg", return_value=message):
            with self.assertRaisesRegex(ValueError, "找不到符合命名規則"):
                reader.extract_msg_workbooks(b"message")

        message.__enter__.return_value.attachments = [
            self._attachment("國壽越權報表.zip", b"not a zip")
        ]
        with patch("src.reader.extract_msg.openMsg", return_value=message):
            results = reader.extract_msg_workbooks(b"message")
        self.assertIn("已損毀", results[0].error)

    def test_archive_read_uses_password_and_reports_wrong_password(self):
        info = MagicMock()
        info.filename = "DC029_20260626.xlsx"
        info.flag_bits = 0x800
        info.is_dir.return_value = False
        archive = MagicMock()
        archive.__enter__.return_value.infolist.return_value = [info]
        archive.__enter__.return_value.read.side_effect = RuntimeError(
            "Bad password for file"
        )
        message = MagicMock()
        message.__enter__.return_value.attachments = [
            self._attachment("國壽越權報表.zip", b"archive")
        ]
        with (
            patch("src.reader.extract_msg.openMsg", return_value=message),
            patch("src.reader.zipfile.ZipFile", return_value=archive),
        ):
            results = reader.extract_msg_workbooks(b"message")
        archive.__enter__.return_value.read.assert_called_once_with(
            info, pwd=reader.MSG_ZIP_PASSWORD
        )
        self.assertIn("密碼不正確", results[0].error)

    def test_cp950_zip_filename_recovery(self):
        expected = "越權檢核115-7-8.xls"
        info = MagicMock()
        info.filename = expected.encode("cp950").decode("cp437")
        info.flag_bits = 0
        self.assertEqual(reader._decode_zip_filename(info), expected)

    @staticmethod
    def _zip_bytes(files):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for filename, contents in files.items():
                archive.writestr(filename, contents)
        return output.getvalue()

    @staticmethod
    def _attachment(name, contents):
        attachment = MagicMock()
        attachment.getFilename.return_value = name
        attachment.data = contents
        return attachment


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
            database.CURRENCY_COLUMN: ["USD"] * row_count,
            database.ACCOUNT_NAME_COLUMN: ["Alpha Fund"] * row_count,
            database.ISSUE_SIZE_COLUMN: [1_000_000] * row_count,
            database.NAV_RATIO_COLUMN: [1.5] * row_count,
            database.ASSET_RATIO_COLUMN: [0.25] * row_count,
            daily.ACCOUNT_VALUE_COLUMN: [500.0] * row_count,
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
                database.CURRENCY_COLUMN: pl.String,
                daily.ACCOUNT_VALUE_COLUMN: pl.Float64,
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

    def test_store_backfills_each_missing_classification_from_newest_history(self):
        old = self.holdings({
            "ISIN": ["F1", "F1"], database.DATE_COLUMN: ["2025-01-01", "2025-02-01"],
            "標的名稱": ["Fund", "Fund"], "標的種類": ["舊種類", "新種類"],
            "類型別": ["唯一類型", ""], "庫存單位數": [1.0, 1.0],
            "基金淨值/ETF收盤價": [10.0, 10.0], "持有市值(標的幣別)": [10.0, 10.0],
        })
        database.store_dataframe(old, self.database)
        incoming = self.holdings({
            "ISIN": ["F1", "F2", "F1"], database.DATE_COLUMN: ["2025-03-01"] * 3,
            "標的名稱": ["Fund", "Other", "Fund"], "標的種類": [None, None, "工作簿種類"],
            "類型別": [None, None, "工作簿類型"], "庫存單位數": [1.0] * 3,
            "基金淨值/ETF收盤價": [10.0] * 3, "持有市值(標的幣別)": [10.0] * 3,
        }).with_columns(pl.Series(database.ACCOUNT_CODE_COLUMN, ["A001", "A001", "A002"]))
        database.store_dataframe(incoming, self.database)
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute(
                'SELECT "ISIN", "標的種類", "類型別", "專戶代號" FROM holdings WHERE "檢查日期" = ? ORDER BY "專戶代號", "ISIN"',
                ("2025-03-01",),
            ).fetchall()
        self.assertEqual(rows, [("F1", "新種類", "唯一類型", "A001"), ("F2", None, None, "A001"), ("F1", "工作簿種類", "工作簿類型", "A002")])

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
        self.assertEqual(daily_holdings[database.CURRENCY_COLUMN].to_list(), ["USD", "USD"])
        self.assertEqual(daily_holdings[database.ISSUE_SIZE_COLUMN].to_list(), [1_000_000, 1_000_000])
        self.assertEqual(daily_holdings[database.NAV_RATIO_COLUMN].to_list(), [1.5, 1.5])
        self.assertEqual(daily_holdings[database.ASSET_RATIO_COLUMN].to_list(), [0.25, 0.25])
        history_rows = database.load_holdings_history(self.database)
        self.assertEqual(history_rows.columns, [database.DATE_COLUMN, *database.HOLDINGS_COLUMNS])
        self.assertEqual(history_rows.height, 3)
        self.assertEqual(history_rows[database.CURRENCY_COLUMN].to_list(), ["USD"] * 3)

    def test_holdings_history_preserves_accounts(self):
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

        self.assertEqual(database.load_holdings_history(self.database).height, 3)

    def test_loaders_return_empty_results_without_ready_database(self):
        self.assertFalse(database.holdings_table_is_ready(self.database))
        self.assertEqual(database.load_available_dates(self.database), [])
        self.assertTrue(database.load_holdings_history(self.database).is_empty())
        self.assertTrue(database.load_holdings_by_date(None, self.database).is_empty())


class PresentationTests(unittest.TestCase):
    def test_application_copy_matches_holdings_database_design(self):
        self.assertEqual(main.app.title, "Holdings Database")
        self.assertIn("Holdings Database", text_content(main.app.layout))

        upload_copy = text_content(upload.layout())
        self.assertIn("投資組合資料庫", upload_copy)
        self.assertIn("請將保管銀行的越權報表上傳，支援世華銀行與中信銀行格式", upload_copy)
        self.assertIn("將 Excel 或 Outlook 訊息檔拖曳到這裡", upload_copy)
        self.assertIn("或點擊選擇報表檔案（可一次選取多個）", upload_copy)
        uploader = upload.layout().children[1]
        self.assertTrue(uploader.multiple)
        self.assertEqual(uploader.accept, ".xlsx,.xls,.msg")

        daily_copy = text_content(daily.layout([], lambda _: pl.DataFrame(), main.make_table))
        self.assertIn("單日的所有基金與全委帳戶的資料", daily_copy)
        self.assertIn("選擇要檢視的單日資料", daily_copy)
        self.assertIn("資料檢視", daily_copy)
        self.assertIn("輸出成csv", daily_copy)
        daily_layout = daily.layout([], lambda _: pl.DataFrame(), main.make_table)
        export_button = find_component(daily_layout, "daily-export-button")
        self.assertIsNotNone(export_button)
        self.assertTrue(export_button.disabled)
        self.assertIsNotNone(find_component(daily_layout, "daily-csv-download"))
        self.assertEqual(
            [option["label"] for option in daily_layout.children[3].children[0].children[1].options],
            ["查看標的", "查看專戶"],
        )
        daily_graph = daily_layout.children[4].children[1]
        self.assertEqual(daily_graph.style, {"width": "100%", "height": "440px"})
        self.assertTrue(daily_graph.responsive)
        self.assertNotIn("responsive", daily_graph.config)

        history_copy = text_content(history.layout(pl.DataFrame()))
        self.assertIn("歷史資料", history_copy)
        self.assertIn("跨日期檢視所有標的與全委帳戶的資料趨勢", history_copy)

    def test_history_figure_components_summary_and_empty_state(self):
        observations = pl.DataFrame({
            database.DATE_COLUMN: ["2025-01-01", "2025-01-01", "2025-01-02"],
            database.ACCOUNT_NAME_COLUMN: ["Alpha", "Beta", "Alpha"],
            "標的名稱": ["Fund 1"] * 3,
            daily.TARGET_VALUE_COLUMN: [10.0, 20.0, 15.0],
        })
        figure, _, _ = history.make_figure(observations, daily.TARGET_MODE, "Fund 1", daily.TARGET_VALUE_COLUMN)
        self.assertEqual([trace.name for trace in figure.data], ["Alpha", "Beta", "總計"])
        self.assertEqual(list(figure.data[-1].y), [30.0, 15.0])
        self.assertTrue(figure.layout.xaxis.rangeslider.visible)
        self.assertEqual(figure.layout.xaxis.tickformat, "%Y/%m/%d")
        self.assertEqual(figure.layout.xaxis.hoverformat, "%Y/%m/%d")
        for trace in figure.data:
            self.assertIn("%{x|%Y/%m/%d}", trace.hovertemplate)
            self.assertNotIn("<br>%{x}<br>", trace.hovertemplate)
        empty, _, _ = history.make_figure(pl.DataFrame(), daily.TARGET_MODE, None, daily.TARGET_VALUE_COLUMN)
        self.assertEqual(len(empty.layout.annotations), 1)
        self.assertFalse(empty.layout.xaxis.visible)

    def test_history_metric_matrix_and_representative_median(self):
        self.assertIn(database.ASSET_RATIO_COLUMN, history.METRICS[daily.TARGET_MODE])
        self.assertNotIn(database.NAV_RATIO_COLUMN, history.METRICS[daily.TARGET_MODE])
        self.assertIn(database.NAV_RATIO_COLUMN, history.METRICS[daily.ACCOUNT_MODE])
        self.assertNotIn(history.NAV_COLUMN, history.METRICS[daily.ACCOUNT_MODE])
        self.assertNotIn(database.ISSUE_SIZE_COLUMN, history.METRICS[daily.ACCOUNT_MODE])

        for metric in (history.NAV_COLUMN, database.ISSUE_SIZE_COLUMN):
            with self.subTest(metric=metric):
                observations = pl.DataFrame({
                    database.DATE_COLUMN: ["2025-01-01", "2025-01-01"],
                    database.ACCOUNT_NAME_COLUMN: ["Alpha", "Beta"],
                    "標的名稱": ["Fund 1", "Fund 1"],
                    metric: [10.0, 14.0],
                })
                figure, _, _ = history.make_figure(
                    observations, daily.TARGET_MODE, "Fund 1", metric
                )
                self.assertEqual(
                    [trace.name for trace in figure.data],
                    ["代表值（所有標的資料的中位數）"],
                )
                self.assertEqual(list(figure.data[0].y), [12.0])

    def test_history_controls_filter_range_and_reset_stale_values(self):
        observations = pl.DataFrame({
            database.DATE_COLUMN: ["2025-01-01", "2025-02-01"],
            database.ACCOUNT_NAME_COLUMN: ["Alpha", "Beta"],
            "標的名稱": ["Old Fund", "New Fund"],
        })
        label, options, selection, metrics, metric = history.resolve_controls(
            observations,
            daily.ACCOUNT_MODE,
            "2025-02-01",
            "2025-02-01",
            "Alpha",
            history.NAV_COLUMN,
        )
        self.assertEqual(label, "專戶名稱")
        self.assertEqual([option["value"] for option in options], ["Beta"])
        self.assertEqual(selection, "Beta")
        self.assertNotIn(history.NAV_COLUMN, [option["value"] for option in metrics])
        self.assertEqual(metric, daily.ACCOUNT_VALUE_COLUMN)

    def test_make_table_marks_numeric_columns(self):
        table = main.make_table(
            pl.DataFrame(
                {
                    database.ACCOUNT_CODE_COLUMN: ["A001"],
                    database.ACCOUNT_NAME_COLUMN: ["Alpha Fund"],
                    "ISIN": ["F1"],
                    database.CURRENCY_COLUMN: ["USD"],
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
            ["text", "text", "text", "text", "numeric", "numeric", "numeric"],
        )
        currency = next(
            column for column in table.columns
            if column["id"] == database.CURRENCY_COLUMN
        )
        self.assertEqual(
            currency,
            {"name": "幣別", "id": database.CURRENCY_COLUMN, "type": "text"},
        )
        self.assertEqual(table.style_table["overflowX"], "auto")
        self.assertEqual(table.style_header["whiteSpace"], "normal")

    def test_daily_csv_download_uses_virtual_rows_and_display_columns(self):
        columns = [
            {"name": "幣別", "id": database.CURRENCY_COLUMN},
            {"name": "標的名稱", "id": daily.TARGET_NAME_COLUMN},
            {"name": "庫存單位數", "id": daily.UNITS_COLUMN},
        ]
        filtered_and_sorted_rows = [
            {
                database.CURRENCY_COLUMN: "TWD",
                daily.TARGET_NAME_COLUMN: "基金乙",
                daily.UNITS_COLUMN: 20.5,
            },
            {
                database.CURRENCY_COLUMN: "USD",
                daily.TARGET_NAME_COLUMN: "基金甲",
                daily.UNITS_COLUMN: 10,
            },
        ]

        download = main.download_daily_csv(
            1, filtered_and_sorted_rows, columns, "2025-01-02"
        )

        self.assertEqual(download["filename"], "daily_holdings_2025-01-02.csv")
        self.assertEqual(download["type"], "text/csv;charset=utf-8")
        self.assertTrue(download["content"].startswith("\ufeff"))
        parsed = list(csv.reader(StringIO(download["content"].lstrip("\ufeff"))))
        self.assertEqual(parsed[0], ["幣別", "標的名稱", "庫存單位數"])
        self.assertEqual(parsed[1], ["TWD", "基金乙", "20.5"])
        self.assertEqual(parsed[2], ["USD", "基金甲", "10"])

    def test_daily_csv_download_with_zero_filtered_rows_keeps_headers(self):
        download = daily.make_csv_download(
            [],
            [
                {"name": "幣別", "id": database.CURRENCY_COLUMN},
                {"name": "標的名稱", "id": daily.TARGET_NAME_COLUMN},
            ],
            "2025-01-02",
        )

        parsed = list(csv.reader(StringIO(download["content"].lstrip("\ufeff"))))
        self.assertEqual(parsed, [["幣別", "標的名稱"]])

    def test_page_helpers_cover_empty_and_date_ranges(self):
        empty = daily.make_table_content(pl.DataFrame(), None, main.make_table)
        self.assertIsInstance(empty, html.Div)
        self.assertIn("尚無資料", empty.children)
        no_rows = daily.make_table_content(pl.DataFrame(), "2025-01-01", main.make_table)
        self.assertIn("所選日期", no_rows.children)
        self.assertEqual(history.normalize_dates("2025-02-01", "2025-01-01"), ("2025-01-01", "2025-02-01"))

    def test_daily_visualization_options_and_chart_types(self):
        holdings = pl.DataFrame(
            {
                database.ACCOUNT_NAME_COLUMN: ["Alpha", "Beta", "Alpha"],
                "標的名稱": ["Fund A", "Fund A", "Fund B"],
                "庫存單位數": [10.0, 30.0, 5.0],
                "持有市值(帳戶幣別)": [100.0, 300.0, 50.0],
                "持有市值(標的幣別)": [120.0, 360.0, 60.0],
            }
        )
        self.assertEqual(
            [option["value"] for option in daily.selector_options(holdings, daily.TARGET_MODE)],
            ["Fund A", "Fund B"],
        )
        self.assertEqual(
            [option["value"] for option in daily.selector_options(holdings, daily.ACCOUNT_MODE)],
            ["Alpha", "Beta"],
        )

        target_figure, title, _, total = daily.make_visualization(
            holdings, daily.TARGET_MODE, "Fund A", daily.TARGET_VALUE_COLUMN
        )
        self.assertEqual(target_figure.data[0].type, "pie")
        self.assertEqual(tuple(target_figure.data[0].domain.x), (0.04, 0.46))
        self.assertEqual(tuple(target_figure.data[0].domain.y), (0.14, 0.86))
        self.assertIsNone(target_figure.layout.height)
        self.assertEqual(target_figure.layout.legend.maxheight, 0.86)
        self.assertIn("Fund A", title)
        self.assertEqual(total, "總計 480.00")

        account_figure, _, _, total = daily.make_visualization(
            holdings, daily.ACCOUNT_MODE, "Alpha", daily.UNITS_COLUMN
        )
        self.assertEqual(account_figure.data[0].type, "bar")
        self.assertEqual(account_figure.data[0].orientation, "h")
        self.assertEqual(account_figure.data[0].width, 0.9)
        self.assertEqual(account_figure.layout.bargap, 0.08)
        self.assertIsNone(account_figure.layout.height)
        self.assertEqual(account_figure.layout.xaxis.tickfont.size, 14)
        self.assertEqual(account_figure.layout.yaxis.tickfont.size, 14)
        self.assertEqual(account_figure.layout.margin.l, 360)
        self.assertEqual(account_figure.layout.margin.r, 32)
        self.assertFalse(account_figure.layout.margin.autoexpand)
        self.assertFalse(account_figure.layout.showlegend)
        self.assertFalse(account_figure.layout.yaxis.automargin)
        self.assertEqual(total, "總計 15.000")

    def test_upload_callback_validates_input(self):
        self.assertEqual(
            main.show_uploaded_workbooks(None, None),
            ("未收到任何檔案。", "batch-status batch-status--error"),
        )
        message, class_name = main.show_uploaded_workbooks(
            ["data:text/plain;base64,eA=="], ["report.txt"]
        )
        self.assertEqual(class_name, "batch-status batch-status--mixed")
        self.assertIn("1 個失敗", " ".join(text_content(message)))
        status_table = message.children[1]
        self.assertEqual(status_table.data[0]["status"], "失敗")
        self.assertIn(".xlsx", status_table.data[0]["detail"])

    def test_upload_callback_processes_each_workbook_independently(self):
        frames = [
            pl.DataFrame({database.DATE_COLUMN: ["2025-01-01"], "ISIN": ["F1"]}),
            pl.DataFrame({database.DATE_COLUMN: ["2025-01-02"], "ISIN": ["F2"]}),
        ]
        with (
            patch("main.parse_excel", side_effect=[frames[0], ValueError("bad workbook"), frames[1]]),
            patch("main.store_dataframe", side_effect=[1, 2]) as store,
        ):
            result, class_name = main.show_uploaded_workbooks(
                ["one", "bad", "two"], ["one.xlsx", "bad.xlsx", "two.xls"]
            )

        self.assertEqual(class_name, "batch-status batch-status--mixed")
        self.assertEqual(store.call_count, 2)
        self.assertIn("2 個完成 · 1 個失敗", " ".join(text_content(result)))
        self.assertEqual(
            [row["status"] for row in result.children[1].data],
            ["完成", "失敗", "完成"],
        )
        self.assertEqual(result.children[1].page_size, 6)

    def test_upload_callback_processes_msg_workbooks_independently(self):
        frames = [
            pl.DataFrame({database.DATE_COLUMN: ["2026-06-26"], "ISIN": ["F1"]}),
            pl.DataFrame({database.DATE_COLUMN: ["2026-06-26"], "ISIN": ["F2"]}),
        ]
        reports = [
            reader.ExtractedWorkbook(
                "國壽越權報表.zip › DC029_20260626.xlsx", b"one"
            ),
            reader.ExtractedWorkbook(
                "國壽越權報表.zip › DC030_20260626.xlsx", b"bad"
            ),
            reader.ExtractedWorkbook(
                "國壽越權報表.zip › DC031_20260626.xlsx", b"two"
            ),
        ]
        upload_data = "data:application/octet-stream;base64," + base64.b64encode(
            b"message"
        ).decode()
        with (
            patch("main.extract_msg_workbooks", return_value=reports),
            patch(
                "main.parse_excel_bytes",
                side_effect=[frames[0], ValueError("bad workbook"), frames[1]],
            ),
            patch("main.store_dataframe", side_effect=[1, 2]) as store,
        ):
            result, class_name = main.show_uploaded_workbooks(
                [upload_data], ["message.msg"]
            )

        self.assertEqual(class_name, "batch-status batch-status--mixed")
        self.assertEqual(store.call_count, 2)
        self.assertEqual(
            [row["filename"] for row in result.children[1].data],
            [
                "message.msg › DC029_20260626.xlsx",
                "message.msg › DC030_20260626.xlsx",
                "message.msg › DC031_20260626.xlsx",
            ],
        )
        self.assertIn("3 個報表檔案", " ".join(text_content(result)))




if __name__ == "__main__":
    unittest.main()
