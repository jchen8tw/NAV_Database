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
import dash_ag_grid as dag
from dash import html

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

    def test_format_aware_result_and_mixed_workbook_rejection(self):
        sheet = pl.DataFrame([["data"]], orient="row")
        canonical = pl.DataFrame(
            {database.DATE_COLUMN: ["2026-07-30"], "ISIN": ["F1"]}
        )
        with (
            patch(
                "src.reader.pl.read_excel",
                return_value={"one": sheet},
            ),
            patch(
                "src.reader._parse_sheet_with_format",
                return_value=(canonical, "ctbc"),
            ),
        ):
            result = reader.parse_excel_bytes_result(b"book")
        self.assertEqual(result.format, "ctbc")
        self.assertTrue(result.dataframe.equals(canonical))

        with (
            patch(
                "src.reader.pl.read_excel",
                return_value={"one": sheet, "two": sheet},
            ),
            patch(
                "src.reader._parse_sheet_with_format",
                side_effect=[(canonical, "cathay"), (canonical, "ctbc")],
            ),
            self.assertRaisesRegex(ValueError, "mixes 國泰世華 and 中信"),
        ):
            reader.parse_excel_bytes_result(b"mixed")

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

    def test_load_instrument_observations_joins_exact_isin_date_keys(self):
        data = self.holdings({
            "ISIN": ["F1", "F1", "F1", "F2"],
            database.DATE_COLUMN: [
                "2025-01-01",
                "2025-01-02",
                "2025-01-02",
                "2025-01-01",
            ],
            "標的名稱": ["Fund 1", "Fund 1", "Fund 1", "Fund 2"],
            "標的種類": ["基金"] * 4,
            "類型別": ["A"] * 4,
            "庫存單位數": [1.0] * 4,
            database.NAV_COLUMN: [10.0, 11.0, 11.5, 20.0],
            "持有市值(標的幣別)": [10.0, 11.0, 11.5, 20.0],
        }).with_columns(
            pl.Series(database.ACCOUNT_CODE_COLUMN, ["A1", "A1", "A2", "A1"])
        )
        database.store_dataframe(data, self.database)

        observations = database.load_instrument_observations(
            {
                ("F1", "2025-01-02"),
                *((f"missing-{index}", "2025-01-01") for index in range(401)),
            },
            self.database,
        )

        self.assertEqual(len(observations), 2)
        by_account = {
            row[database.ACCOUNT_CODE_COLUMN]: row for row in observations
        }
        self.assertEqual(set(by_account), {"A1", "A2"})
        self.assertEqual(by_account["A1"]["ISIN"], "F1")
        self.assertEqual(by_account["A1"][database.DATE_COLUMN], "2025-01-02")
        self.assertEqual(by_account["A1"][database.NAV_COLUMN], 11.0)
        self.assertEqual(by_account["A2"][database.NAV_COLUMN], 11.5)

        self.assertEqual(
            database.load_instrument_observations(set(), self.database),
            [],
        )

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
        uploader = find_component(upload.layout(), "excel-upload")
        self.assertTrue(uploader.multiple)
        self.assertEqual(uploader.accept, ".xlsx,.xls,.msg")
        modal = find_component(upload.layout(), "upload-confirmation-modal")
        self.assertIsNotNone(modal)
        self.assertIn("確認上傳檔案", text_content(modal))
        self.assertIn("套用日期至已選檔案", text_content(modal))
        self.assertIn("確認上傳", text_content(modal))
        self.assertIsNotNone(find_component(modal, "upload-staging-grid"))
        self.assertIsNotNone(find_component(modal, "upload-batch-date"))
        self.assertIsNotNone(find_component(modal, "upload-conflict-panel"))
        self.assertTrue(find_component(modal, "upload-confirm-button").disabled)
        self.assertIsNotNone(find_component(upload.layout(), "upload-staging-store"))

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

    def test_history_metric_matrix_and_mode_with_account_data(self):
        self.assertIn(database.ASSET_RATIO_COLUMN, history.METRICS[daily.TARGET_MODE])
        self.assertNotIn(database.NAV_RATIO_COLUMN, history.METRICS[daily.TARGET_MODE])
        self.assertIn(database.NAV_RATIO_COLUMN, history.METRICS[daily.ACCOUNT_MODE])
        self.assertNotIn(history.NAV_COLUMN, history.METRICS[daily.ACCOUNT_MODE])
        self.assertNotIn(database.ISSUE_SIZE_COLUMN, history.METRICS[daily.ACCOUNT_MODE])

        for metric in (history.NAV_COLUMN, database.ISSUE_SIZE_COLUMN):
            with self.subTest(metric=metric):
                observations = pl.DataFrame({
                    database.DATE_COLUMN: [
                        "2025-01-01",
                        "2025-01-01",
                        "2025-01-01",
                        "2025-01-01",
                        "2025-01-02",
                        "2025-01-02",
                    ],
                    database.ACCOUNT_NAME_COLUMN: [
                        "Alpha",
                        "Alpha",
                        "Beta",
                        "Beta",
                        "Alpha",
                        "Beta",
                    ],
                    "標的名稱": ["Fund 1"] * 6,
                    metric: [10.0, 14.0, 14.0, 14.0, 20.0, 22.0],
                })
                figure, _, _ = history.make_figure(
                    observations, daily.TARGET_MODE, "Fund 1", metric
                )
                self.assertEqual(
                    [trace.name for trace in figure.data],
                    ["Alpha", "Beta", "眾數（所有專戶資料；並列時取最小值）"],
                )
                self.assertEqual(list(figure.data[0].y), [10.0, 20.0])
                self.assertEqual(list(figure.data[1].y), [14.0, 22.0])
                self.assertEqual(list(figure.data[2].y), [14.0, 20.0])

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
        numeric_values = {
            "庫存單位數": [75_273, None],
            database.NAV_COLUMN: [1_350.393, 12.5],
            "持有市值(帳戶幣別)": [3_294_066.6, 999.25],
            "持有市值(標的幣別)": [1_001.0, None],
            database.ISSUE_SIZE_COLUMN: [1_000_000, 500],
            database.NAV_RATIO_COLUMN: [1.5, 0.25],
            database.ASSET_RATIO_COLUMN: [0.25, None],
        }
        table = main.make_table(
            pl.DataFrame(
                {
                    database.ACCOUNT_CODE_COLUMN: ["A001", "A002"],
                    database.ACCOUNT_NAME_COLUMN: ["Alpha Fund", "Beta Fund"],
                    "ISIN": ["F1", "F2"],
                    database.CURRENCY_COLUMN: ["USD", "TWD"],
                    **numeric_values,
                }
            ),
            "custom",
        )
        self.assertIsInstance(table, dag.AgGrid)
        self.assertEqual(table.id, "custom")
        self.assertEqual(
            main.NUMERIC_VALUE_FORMATTER,
            {"function": "formatNumber(params.value)"},
        )
        columns_by_field = {
            column["field"]: column for column in table.columnDefs
        }
        for name in main.NUMERIC_COLUMNS:
            with self.subTest(column=name):
                self.assertEqual(
                    columns_by_field[name]["filter"], "agNumberColumnFilter"
                )
                self.assertEqual(
                    columns_by_field[name]["valueFormatter"],
                    main.NUMERIC_VALUE_FORMATTER,
                )
        for name in (
            database.ACCOUNT_CODE_COLUMN,
            database.ACCOUNT_NAME_COLUMN,
            "ISIN",
            database.CURRENCY_COLUMN,
        ):
            with self.subTest(column=name):
                self.assertEqual(
                    columns_by_field[name]["filter"], "agTextColumnFilter"
                )
                self.assertNotIn("valueFormatter", columns_by_field[name])
        currency = next(
            column for column in table.columnDefs
            if column["field"] == database.CURRENCY_COLUMN
        )
        self.assertEqual(
            currency,
            {
                "headerName": "幣別",
                "field": database.CURRENCY_COLUMN,
                "filter": "agTextColumnFilter",
                "tooltipField": database.CURRENCY_COLUMN,
            },
        )
        self.assertEqual(table.rowData[0][database.ACCOUNT_CODE_COLUMN], "A001")
        for name, values in numeric_values.items():
            with self.subTest(row_data=name):
                self.assertEqual(
                    [row[name] for row in table.rowData], values
                )
        self.assertEqual(table.dashGridOptions["paginationPageSize"], 20)
        self.assertTrue(table.dashGridOptions["alwaysMultiSort"])
        self.assertTrue(table.defaultColDef["floatingFilter"])
        self.assertEqual(table.style["height"], "440px")

    def test_daily_csv_download_uses_virtual_rows_and_display_columns(self):
        columns = [
            {"headerName": "幣別", "field": database.CURRENCY_COLUMN},
            {"headerName": "標的名稱", "field": daily.TARGET_NAME_COLUMN},
            {"headerName": "庫存單位數", "field": daily.UNITS_COLUMN},
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
                {"headerName": "幣別", "field": database.CURRENCY_COLUMN},
                {"headerName": "標的名稱", "field": daily.TARGET_NAME_COLUMN},
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

    def test_previous_weekday_covers_monday_and_weekend_boundaries(self):
        self.assertEqual(
            main.previous_weekday(date(2026, 7, 30)), date(2026, 7, 29)
        )
        self.assertEqual(
            main.previous_weekday(date(2026, 8, 3)), date(2026, 7, 31)
        )
        self.assertEqual(
            main.previous_weekday(date(2026, 8, 2)), date(2026, 7, 31)
        )
        self.assertEqual(
            main.previous_weekday(date(2026, 8, 1)), date(2026, 7, 31)
        )

    def test_staging_classifies_dates_and_never_stores(self):
        cathay = reader.ParsedWorkbook(
            pl.DataFrame({database.DATE_COLUMN: ["2026-08-03"]}), "cathay"
        )
        ctbc = reader.ParsedWorkbook(
            pl.DataFrame({database.DATE_COLUMN: ["2026-08-02"]}), "ctbc"
        )
        with (
            patch(
                "main.parse_excel_result",
                side_effect=[cathay, ctbc, ValueError("bad workbook")],
            ),
            patch("main.store_dataframe") as store,
        ):
            staging = main.stage_uploaded_workbooks(
                ["one", "two", "bad"],
                ["cathay.xlsx", "ctbc.xls", "bad.xlsx"],
            )
        store.assert_not_called()
        self.assertEqual(
            [row["format_label"] for row in staging["rows"]],
            ["國泰世華", "中信", "無法辨識"],
        )
        self.assertEqual(
            [row["date"] for row in staging["rows"]],
            ["2026-07-31", "2026-08-02", None],
        )
        self.assertFalse(staging["rows"][2]["valid"])
        self.assertEqual(
            [row["id"] for row in staging["rows"]],
            [
                "upload-0-report-0",
                "upload-1-report-0",
                "upload-2-report-0",
            ],
        )

    def test_staging_expands_msg_and_exposes_invalid_rows(self):
        reports = [
            reader.ExtractedWorkbook(
                "國壽越權報表.zip › DC029_20260626.xlsx", b"one"
            ),
            reader.ExtractedWorkbook(
                "國壽越權報表.zip › DC030_20260626.xlsx",
                error="broken attachment",
            ),
        ]
        upload_data = "data:application/octet-stream;base64," + base64.b64encode(
            b"message"
        ).decode()
        with (
            patch("main.extract_msg_workbooks", return_value=reports),
            patch(
                "main.parse_excel_bytes_result",
                return_value=reader.ParsedWorkbook(
                    pl.DataFrame({database.DATE_COLUMN: ["2025-01-01"]}),
                    "cathay",
                ),
            ),
            patch("main.store_dataframe") as store,
        ):
            staging = main.stage_uploaded_workbooks(
                [upload_data], ["message.msg"]
            )
        store.assert_not_called()
        self.assertEqual(
            [row["filename"] for row in staging["rows"]],
            [
                "message.msg › DC029_20260626.xlsx",
                "message.msg › DC030_20260626.xlsx",
            ],
        )
        self.assertTrue(staging["rows"][0]["valid"])
        self.assertFalse(staging["rows"][1]["valid"])

    def test_upload_modal_initially_selects_every_valid_file(self):
        valid = reader.ParsedWorkbook(
            pl.DataFrame({database.DATE_COLUMN: ["2025-01-01"]}), "ctbc"
        )
        with (
            patch(
                "main.parse_excel_result",
                side_effect=[valid, ValueError("invalid"), valid],
            ),
            patch("main.load_instrument_observations") as load_observations,
        ):
            result = main.show_uploaded_workbooks(
                ["one", "bad", "two"],
                ["one.xlsx", "bad.xlsx", "two.xlsx"],
            )

        load_observations.assert_not_called()
        selected_rows = result[2]
        self.assertEqual(
            [row["filename"] for row in selected_rows],
            ["one.xlsx", "two.xlsx"],
        )
        self.assertTrue(all(row["valid"] for row in selected_rows))
        self.assertEqual(result[8], [])
        self.assertEqual(result[9], "確認上傳")

    def test_upload_reset_does_not_overwrite_existing_status(self):
        for contents in (None, []):
            with self.subTest(contents=contents):
                with self.assertRaises(main.PreventUpdate):
                    main.show_uploaded_workbooks(contents, None)

    def test_selection_and_mixed_date_editing_only_updates_targets(self):
        staging = {
            "review_phase": "confirmation",
            "conflicts": [],
            "rows": [
                {
                    "id": "one",
                    "valid": True,
                    "date": "2026-07-29",
                    "display_date": "2026/07/29",
                },
                {
                    "id": "two",
                    "valid": True,
                    "date": "2026-07-30",
                    "display_date": "2026/07/30",
                },
                {
                    "id": "three",
                    "valid": True,
                    "date": "2026-07-30",
                    "display_date": "2026/07/30",
                },
            ]
        }
        selected = [staging["rows"][0], staging["rows"][1]]
        picker_date, help_text = main.selected_date_state(selected)
        self.assertIsNone(picker_date)
        self.assertIn("不同日期", help_text)

        updated = main.apply_date_to_selected(
            staging, selected, "2026-08-01"
        )
        self.assertEqual(
            [row["date"] for row in updated["rows"]],
            ["2026-08-01", "2026-08-01", "2026-07-30"],
        )
        self.assertEqual(
            main.selected_date_state([updated["rows"][0]]),
            ("2026-08-01", "已選取 1 個檔案"),
        )

    def test_date_edit_does_not_check_conflicts_before_confirmation(self):
        staging = {
            "review_phase": "confirmation",
            "conflicts": [],
            "rows": [
                {
                    "id": "one",
                    "filename": "one.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "display_date": "2026/07/29",
                }
            ],
        }
        with patch("main.refresh_staging_conflicts") as refresh:
            result = main.update_staged_dates(
                "2026-08-01", [staging["rows"][0]], staging
            )

        refresh.assert_not_called()
        self.assertEqual(result[0]["rows"][0]["date"], "2026-08-01")
        self.assertEqual(result[2], [])
        self.assertEqual(result[4], "確認上傳")

    def test_conflicts_compare_same_date_nonblank_values_and_batch_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "conflicts.sqlite3"
            stored = DatabaseTests.holdings({
                "ISIN": ["F1", "F1"],
                database.DATE_COLUMN: ["2026-07-29", "2026-07-30"],
                "標的名稱": ["Fund 1", "Fund 1"],
                "標的種類": ["基金", "基金"],
                "類型別": ["A", "A"],
                "庫存單位數": [1.0, 1.0],
                database.NAV_COLUMN: [10.0, 99.0],
                "持有市值(標的幣別)": [10.0, 99.0],
            })
            database.store_dataframe(stored, database_path)
            staging = {
                "rows": [
                    {
                        "id": "one",
                        "filename": "one.xlsx",
                        "valid": True,
                        "date": "2026-07-29",
                        "instrument_values": [
                            {
                                "isin": "F1",
                                "account": "A002",
                                database.NAV_COLUMN: 12.0,
                                database.ISSUE_SIZE_COLUMN: 1_000_000,
                            },
                            {
                                "isin": "F2",
                                "account": "A002",
                                database.NAV_COLUMN: None,
                                database.ISSUE_SIZE_COLUMN: None,
                            },
                            {
                                "isin": "F3",
                                "account": "A002",
                                database.NAV_COLUMN: 1.0,
                                database.ISSUE_SIZE_COLUMN: 3_000,
                            },
                        ],
                    },
                    {
                        "id": "two",
                        "filename": "two.xlsx",
                        "valid": True,
                        "date": "2026-07-29",
                        "instrument_values": [
                            {
                                "isin": "F3",
                                "account": "A003",
                                database.NAV_COLUMN: 2.0,
                                database.ISSUE_SIZE_COLUMN: 3_000,
                            }
                        ],
                    },
                ]
            }

            conflicts = main.find_upload_conflicts(staging, database_path)

        self.assertEqual(len(conflicts), 3)
        self.assertEqual(
            [(item["isin"], item["field"]) for item in conflicts],
            [
                ("F1", database.NAV_COLUMN),
                ("F3", database.NAV_COLUMN),
                ("F3", database.NAV_COLUMN),
            ],
        )
        f1 = conflicts[0]
        self.assertEqual(f1["source_id"], "one")
        self.assertEqual(f1["existing_values"], [10.0])
        self.assertEqual(f1["existing_accounts"], ["A001"])
        self.assertNotIn(99.0, f1["existing_values"])

    def test_matching_nav_produces_no_conflict_even_when_issue_size_differs(self):
        staging = {
            "rows": [
                {
                    "id": "one",
                    "filename": "one.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "instrument_values": [
                        {
                            "isin": "F1",
                            "account": "A001",
                            database.NAV_COLUMN: 10.0,
                            database.ISSUE_SIZE_COLUMN: 1,
                        }
                    ],
                },
                {
                    "id": "two",
                    "filename": "two.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "instrument_values": [
                        {
                            "isin": "F1",
                            "account": "A003",
                            database.NAV_COLUMN: 10.0,
                            database.ISSUE_SIZE_COLUMN: 2,
                        }
                    ],
                },
            ]
        }
        with patch("main.load_instrument_observations", return_value=[
            {
                "ISIN": "F1",
                database.DATE_COLUMN: "2026-07-29",
                database.ACCOUNT_CODE_COLUMN: "A002",
                database.NAV_COLUMN: 10.0,
                database.ISSUE_SIZE_COLUMN: 999,
            }
        ]):
            conflicts = main.find_upload_conflicts(staging)
        self.assertEqual(conflicts, [])
        self.assertEqual(main.make_conflict_panel(conflicts), [])

    def test_conflict_check_excludes_unselected_batch_files(self):
        staging = {
            "rows": [
                {
                    "id": "one",
                    "filename": "one.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "instrument_values": [
                        {
                            "isin": "F1",
                            "account": "A001",
                            database.NAV_COLUMN: 10.0,
                        }
                    ],
                },
                {
                    "id": "two",
                    "filename": "two.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "instrument_values": [
                        {
                            "isin": "F1",
                            "account": "A002",
                            database.NAV_COLUMN: 12.0,
                        }
                    ],
                },
            ]
        }
        with patch("main.load_instrument_observations", return_value=[]):
            selected_only = main.find_upload_conflicts(
                staging, selected_ids={"one"}
            )
            both_selected = main.find_upload_conflicts(
                staging, selected_ids={"one", "two"}
            )

        self.assertEqual(selected_only, [])
        self.assertEqual(
            {conflict["filename"] for conflict in both_selected},
            {"one.xlsx", "two.xlsx"},
        )

    def test_selection_change_clears_review_and_disables_empty_upload(self):
        staging = {
            "rows": [
                {"id": "one", "valid": True},
                {"id": "two", "valid": True},
            ],
            "selected_ids": ["one"],
            "reviewed_selection": ["one"],
            "review_phase": "conflict_review",
            "conflicts": [{"id": "conflict"}],
        }

        result = main.update_staged_selection([], staging)

        self.assertEqual(result[2]["selected_ids"], [])
        self.assertEqual(result[2]["review_phase"], "confirmation")
        self.assertEqual(result[2]["conflicts"], [])
        self.assertEqual(result[5], "確認上傳")
        self.assertTrue(result[6])

    def test_conflict_panel_and_summary_expose_decision_details(self):
        conflict = {
            "id": "one:0:nav",
            "filename": "one.xlsx",
            "isin": "F1",
            "date": "2026-07-29",
            "field": database.NAV_COLUMN,
            "incoming_value": 12.0,
            "existing_values": [10.0],
            "existing_accounts": ["A001"],
            "sources": ["database"],
        }
        staging = {"rows": [{"valid": True}], "conflicts": [conflict]}

        panel_text = " ".join(text_content(main.make_conflict_panel([conflict])))

        self.assertIn("偵測到資料不一致", panel_text)
        self.assertIn("基金淨值", panel_text)
        self.assertIn("F1", panel_text)
        self.assertIn("12", panel_text)
        self.assertIn("A001", panel_text)
        self.assertEqual(
            main.upload_staging_summary(staging),
            "已選取 1 個有效檔案 · 1 個 ISIN 有資料差異",
        )

    def test_first_confirmation_opens_conflict_review_before_upload(self):
        selected = {"id": "one", "valid": True}
        staging = {
            "rows": [selected],
            "conflicts": [],
            "selected_ids": ["one"],
            "review_phase": "confirmation",
        }
        refreshed = {
            **staging,
            "review_phase": "conflict_review",
            "conflicts": [
                {
                    "id": "one:0:nav",
                    "source_id": "one",
                    "filename": "one.xlsx",
                    "isin": "F1",
                    "date": "2026-07-29",
                    "field": database.NAV_COLUMN,
                    "incoming_value": 12.0,
                    "existing_values": [10.0],
                    "existing_accounts": ["A001"],
                    "sources": ["database"],
                }
            ],
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch("main.refresh_staging_conflicts", return_value=refreshed),
            patch("main.process_staged_workbooks") as process,
        ):
            result = main.finish_upload(0, 1, staging, [selected])

        process.assert_not_called()
        self.assertEqual(result[0], "upload-modal upload-modal--open")
        self.assertEqual(result[1]["reviewed_selection"], ["one"])
        self.assertEqual(result[1]["conflicts"], refreshed["conflicts"])
        self.assertEqual(result[8], "")
        self.assertEqual(result[10], "仍要上傳")

    def test_first_confirmation_uploads_clean_files_and_reviews_only_conflicts(
        self,
    ):
        clean = {"id": "clean", "filename": "clean.xlsx", "valid": True}
        failed = {"id": "failed", "filename": "failed.xlsx", "valid": True}
        conflicted = {
            "id": "conflicted",
            "filename": "conflicted.xlsx",
            "valid": True,
        }
        staging = {
            "rows": [clean, failed, conflicted],
            "conflicts": [],
            "selected_ids": ["clean", "failed", "conflicted"],
            "review_phase": "confirmation",
            "upload_results": [],
        }
        conflict = {
            "id": "conflicted:0:nav",
            "source_id": "conflicted",
            "filename": "conflicted.xlsx",
            "isin": "F1",
            "date": "2026-07-29",
            "field": database.NAV_COLUMN,
            "incoming_value": 12.0,
            "existing_values": [10.0],
            "existing_accounts": ["A001"],
            "sources": ["database"],
        }
        refreshed = {**staging, "conflicts": [conflict]}
        clean_result = {
            "id": "clean",
            "filename": "clean.xlsx",
            "status": "完成",
            "detail": "done",
            "imported_rows": 1,
        }
        failed_result = {
            "id": "failed",
            "filename": "failed.xlsx",
            "status": "失敗",
            "detail": "write failed",
            "imported_rows": 0,
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch("main.refresh_staging_conflicts", return_value=refreshed),
            patch(
                "main.process_staged_workbooks",
                return_value=[clean_result, failed_result],
            ) as process,
        ):
            result = main.finish_upload(
                0, 1, staging, [clean, failed, conflicted]
            )

        process.assert_called_once_with(refreshed, {"clean", "failed"})
        self.assertEqual(result[0], "upload-modal upload-modal--open")
        self.assertEqual(
            [row["id"] for row in result[1]["rows"]], ["conflicted"]
        )
        self.assertEqual(result[1]["selected_ids"], ["conflicted"])
        self.assertEqual(result[1]["reviewed_selection"], ["conflicted"])
        self.assertEqual([row["id"] for row in result[3]], ["conflicted"])
        self.assertEqual(result[1]["conflicts"], [conflict])
        self.assertIn("1 個完成 · 1 個失敗", " ".join(text_content(result[6])))
        self.assertEqual(
            result[11],
            "已處理 2 個檔案 · 已選取 1 個有效檔案 · "
            "1 個 ISIN 有資料差異",
        )
        self.assertEqual(result[10], "仍要上傳")

    def test_first_confirmation_uploads_immediately_without_conflicts(self):
        staging = {
            "rows": [{"id": "one", "valid": True}],
            "conflicts": [],
            "review_phase": "confirmation",
            "selected_ids": ["one"],
            "upload_results": [],
        }
        completed = {
            "id": "one",
            "filename": "one.xlsx",
            "status": "完成",
            "detail": "done",
            "imported_rows": 1,
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch(
                "main.refresh_staging_conflicts",
                return_value={**staging, "conflicts": []},
            ),
            patch(
                "main.process_staged_workbooks",
                return_value=[completed],
            ) as process,
        ):
            result = main.finish_upload(0, 1, staging, [staging["rows"][0]])

        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[1], {"one"})
        self.assertEqual(result[0], "upload-modal")
        self.assertIn("1 個完成", " ".join(text_content(result[6])))

    def test_second_confirmation_uploads_after_unchanged_conflict_review(self):
        conflict = {
            "id": "old",
            "filename": "one.xlsx",
            "isin": "F1",
            "date": "2026-07-29",
            "field": database.NAV_COLUMN,
            "incoming_value": 12.0,
            "existing_values": [10.0],
            "existing_accounts": ["A001"],
            "sources": ["database"],
        }
        staging = {
            "rows": [{"id": "one", "valid": True}],
            "review_phase": "conflict_review",
            "conflicts": [conflict],
            "selected_ids": ["one"],
            "reviewed_selection": ["one"],
            "upload_results": [],
        }
        completed = {
            "id": "one",
            "filename": "one.xlsx",
            "status": "完成",
            "detail": "done",
            "imported_rows": 1,
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch("main.refresh_staging_conflicts", return_value=staging),
            patch(
                "main.process_staged_workbooks",
                return_value=[completed],
            ) as process,
        ):
            result = main.finish_upload(0, 2, staging, [staging["rows"][0]])

        process.assert_called_once()
        self.assertEqual(result[0], "upload-modal")
        self.assertIn("1 個完成", " ".join(text_content(result[6])))

    def test_conflict_review_reopens_when_database_conflicts_change(self):
        existing_conflict = {
            "id": "old",
            "filename": "one.xlsx",
            "isin": "F1",
            "date": "2026-07-29",
            "field": database.NAV_COLUMN,
            "incoming_value": 12.0,
            "existing_values": [10.0],
            "existing_accounts": ["A001"],
            "sources": ["database"],
        }
        staging = {
            "rows": [{"id": "one", "valid": True}],
            "review_phase": "conflict_review",
            "conflicts": [existing_conflict],
            "selected_ids": ["one"],
            "reviewed_selection": ["one"],
        }
        refreshed = {
            **staging,
            "conflicts": [{**existing_conflict, "existing_values": [11.0]}],
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch("main.refresh_staging_conflicts", return_value=refreshed),
            patch("main.process_staged_workbooks") as process,
        ):
            result = main.finish_upload(0, 2, staging, [staging["rows"][0]])

        process.assert_not_called()
        self.assertIn("衝突清單已更新", result[8])
        self.assertEqual(result[10], "仍要上傳")

    def test_partial_upload_keeps_unselected_files_and_cumulative_results(self):
        first = {"id": "one", "filename": "one.xlsx", "valid": True}
        second = {"id": "two", "filename": "two.xlsx", "valid": True}
        staging = {
            "rows": [first, second],
            "selected_ids": ["one"],
            "review_phase": "confirmation",
            "conflicts": [],
            "upload_results": [],
        }
        first_result = {
            "id": "one",
            "filename": "one.xlsx",
            "status": "完成",
            "detail": "first done",
            "imported_rows": 1,
        }
        second_result = {
            "id": "two",
            "filename": "two.xlsx",
            "status": "完成",
            "detail": "second done",
            "imported_rows": 2,
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch(
                "main.refresh_staging_conflicts",
                side_effect=lambda current, **_: current,
            ),
            patch(
                "main.process_staged_workbooks",
                side_effect=[[first_result], [second_result]],
            ),
        ):
            partial = main.finish_upload(0, 1, staging, [first])
            partial_row_ids = [
                row["id"] for row in partial[1]["rows"]
            ]
            second_staging = {
                **partial[1],
                "rows": list(partial[1]["rows"]),
                "upload_results": list(partial[1]["upload_results"]),
            }
            completed = main.finish_upload(
                0, 2, second_staging, [partial[2][0]]
            )

        self.assertEqual(partial[0], "upload-modal upload-modal--open")
        self.assertEqual(partial_row_ids, ["two"])
        self.assertEqual(partial[3], [])
        self.assertIn("1 個完成", " ".join(text_content(partial[6])))
        self.assertEqual(completed[0], "upload-modal")
        completed_text = " ".join(text_content(completed[6]))
        self.assertIn("已處理 2 個報表檔案", completed_text)
        self.assertIn("本批次共寫入 3 筆資料", completed_text)

    def test_failed_selected_file_remains_selected_for_retry(self):
        selected = {"id": "one", "filename": "one.xlsx", "valid": True}
        staging = {
            "rows": [selected],
            "selected_ids": ["one"],
            "review_phase": "confirmation",
            "conflicts": [],
            "upload_results": [],
        }
        failed = {
            "id": "one",
            "filename": "one.xlsx",
            "status": "失敗",
            "detail": "parse failed",
            "imported_rows": 0,
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch(
                "main.refresh_staging_conflicts",
                side_effect=lambda current, **_: current,
            ),
            patch("main.process_staged_workbooks", return_value=[failed]),
        ):
            result = main.finish_upload(0, 1, staging, [selected])

        self.assertEqual(result[0], "upload-modal upload-modal--open")
        self.assertEqual(result[1]["selected_ids"], ["one"])
        self.assertEqual([row["id"] for row in result[3]], ["one"])
        self.assertIn("1 個失敗", " ".join(text_content(result[6])))

    def test_invalid_rows_do_not_prevent_close_after_valid_uploads(self):
        selected = {"id": "one", "filename": "one.xlsx", "valid": True}
        invalid = {"id": "bad", "filename": "bad.xlsx", "valid": False}
        staging = {
            "rows": [selected, invalid],
            "selected_ids": ["one"],
            "review_phase": "confirmation",
            "conflicts": [],
            "upload_results": [],
        }
        completed = {
            "id": "one",
            "filename": "one.xlsx",
            "status": "完成",
            "detail": "done",
            "imported_rows": 1,
        }
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-confirm-button")),
            patch(
                "main.refresh_staging_conflicts",
                side_effect=lambda current, **_: current,
            ),
            patch(
                "main.process_staged_workbooks", return_value=[completed]
            ),
        ):
            result = main.finish_upload(0, 1, staging, [selected])

        self.assertEqual(result[0], "upload-modal")
        self.assertEqual(result[1], {"rows": []})

    def test_confirmation_overrides_dates_and_isolates_failures(self):
        staging = {
            "rows": [
                {
                    "id": "one",
                    "filename": "one.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "source_kind": "upload",
                    "source_contents": "one",
                },
                {
                    "id": "bad",
                    "filename": "bad.xlsx",
                    "valid": True,
                    "date": "2026-07-30",
                    "source_kind": "upload",
                    "source_contents": "bad",
                },
                {
                    "id": "invalid",
                    "filename": "invalid.xlsx",
                    "valid": False,
                    "date": None,
                    "error": "invalid format",
                    "source_kind": "upload",
                    "source_contents": "invalid",
                },
            ]
        }
        frame = pl.DataFrame(
            {database.DATE_COLUMN: ["2025-01-01"], "ISIN": ["F1"]}
        )
        with (
            patch(
                "main.parse_excel_result",
                side_effect=[
                    reader.ParsedWorkbook(frame, "cathay"),
                    ValueError("parse failed"),
                ],
            ),
            patch("main.store_dataframe", return_value=1) as store,
        ):
            result, class_name = main.confirm_staged_workbooks(staging)
        self.assertEqual(store.call_count, 1)
        self.assertEqual(
            store.call_args.args[0][database.DATE_COLUMN].to_list(),
            ["2026-07-29"],
        )
        self.assertEqual(class_name, "batch-status batch-status--mixed")
        self.assertEqual(
            [row["status"] for row in result.children[1].rowData],
            ["完成", "失敗", "失敗"],
        )
        self.assertIn("1 個完成 · 2 個失敗", " ".join(text_content(result)))

    def test_processing_uploads_only_selected_valid_files(self):
        staging = {
            "rows": [
                {
                    "id": "one",
                    "filename": "one.xlsx",
                    "valid": True,
                    "date": "2026-07-29",
                    "source_kind": "upload",
                    "source_contents": "one",
                },
                {
                    "id": "two",
                    "filename": "two.xlsx",
                    "valid": True,
                    "date": "2026-07-30",
                    "source_kind": "upload",
                    "source_contents": "two",
                },
            ]
        }
        frame = pl.DataFrame(
            {database.DATE_COLUMN: ["2025-01-01"], "ISIN": ["F1"]}
        )
        with (
            patch(
                "main.parse_excel_result",
                return_value=reader.ParsedWorkbook(frame, "ctbc"),
            ) as parse,
            patch("main.store_dataframe") as store,
        ):
            results = main.process_staged_workbooks(staging, {"two"})

        parse.assert_called_once_with("two")
        store.assert_called_once()
        self.assertEqual([row["id"] for row in results], ["two"])

    def test_retry_result_replaces_previous_file_result(self):
        failed = {
            "id": "one",
            "filename": "one.xlsx",
            "status": "失敗",
            "detail": "failed",
            "imported_rows": 0,
        }
        completed = {
            **failed,
            "status": "完成",
            "detail": "done",
            "imported_rows": 5,
        }

        merged = main.merge_upload_results([failed], [completed])

        self.assertEqual(merged, [completed])

    def test_cancel_clears_staging_without_database_writes(self):
        with (
            patch("main.ctx", MagicMock(triggered_id="upload-cancel-button")),
            patch("main.store_dataframe") as store,
        ):
            result = main.finish_upload(
                1, 0, {"rows": [{"valid": True}]}
            )
        store.assert_not_called()
        self.assertEqual(result[0], "upload-modal")
        self.assertEqual(result[1], {"rows": []})
        self.assertIsNone(result[4])

    def test_cancel_conflict_review_preserves_processed_upload_status(self):
        staging = {
            "rows": [{"id": "conflicted", "valid": True}],
            "review_phase": "conflict_review",
            "upload_results": [
                {
                    "id": "clean",
                    "filename": "clean.xlsx",
                    "status": "完成",
                    "detail": "done",
                    "imported_rows": 1,
                }
            ],
        }
        with patch(
            "main.ctx", MagicMock(triggered_id="upload-cancel-button")
        ):
            result = main.finish_upload(1, 0, staging)

        self.assertEqual(result[0], "upload-modal")
        self.assertEqual(result[1], {"rows": []})
        self.assertEqual(result[2], [])
        self.assertEqual(result[3], [])
        self.assertIsNone(result[4])
        self.assertIsNone(result[5])
        self.assertIs(result[6], main.no_update)
        self.assertIs(result[7], main.no_update)
        self.assertEqual(result[8], "")
        self.assertEqual(result[9], [])
        self.assertEqual(result[10], "確認上傳")
        self.assertEqual(result[11], "")

    def test_cancel_conflict_review_without_results_clears_upload_status(self):
        staging = {
            "rows": [{"id": "conflicted", "valid": True}],
            "review_phase": "conflict_review",
            "upload_results": [],
        }
        with patch(
            "main.ctx", MagicMock(triggered_id="upload-cancel-button")
        ):
            result = main.finish_upload(1, 0, staging)

        self.assertEqual(result[6], "")
        self.assertEqual(result[7], "batch-status")




if __name__ == "__main__":
    unittest.main()
