import base64
import binascii
from datetime import date, datetime
from io import BytesIO
from typing import Any

import polars as pl
import polars.selectors as cs

from src.database import ACCOUNT_CODE_COLUMN, ACCOUNT_NAME_COLUMN, DATE_COLUMN



def parse_excel(contents: str) -> pl.DataFrame:
    """Decode a workbook and return holdings with its report date attached."""
    try:
        _, encoded = contents.split(",", 1)
        workbook = BytesIO(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("The uploaded file could not be decoded.") from exc

    worksheet = pl.read_excel(workbook, has_header=False)
    if worksheet.height < 7:
        raise ValueError("The workbook does not contain holdings rows.")

    report_date = find_report_date(worksheet)
    account_code = find_metadata_value(worksheet, ACCOUNT_CODE_COLUMN)
    account_name = find_metadata_value(worksheet, ACCOUNT_NAME_COLUMN)
    try:
        header_index = next(
            index
            for index, row in enumerate(worksheet.iter_rows())
            if str(row[0] or "").strip().casefold() == "isin"
        )
    except StopIteration as exc:
        raise ValueError("The workbook does not contain a holdings header row.") from exc
    headers = worksheet.row(header_index)
    if any(not header for header in headers):
        raise ValueError("The workbook contains a blank holdings column header.")

    return (
        worksheet.slice(header_index + 1, worksheet.height - header_index - 2)
        .rename(dict(zip(worksheet.columns, headers, strict=True)))
        .with_columns(cs.string().str.strip_chars())
        .with_columns(cs.string().str.replace_all(r"[,%]+", ""))
        .with_columns(
            pl.col("庫存單位數").cast(pl.Float32),
            pl.col("基金淨值/ETF收盤價").cast(pl.Float32),
            pl.col("持有市值(帳戶幣別)").cast(pl.Float32),
            pl.col("持有市值(標的幣別)").cast(pl.Float32),
            pl.col("標的發行規模（標的幣別）或流通股數").cast(
                pl.Int32, strict=False
            ),
            pl.col("佔淨資產比重(%)").cast(pl.Float32),
            pl.col("佔標的資產或單位數比重(%)").cast(pl.Float32),
        )
        .with_columns(
            pl.lit(report_date).alias(DATE_COLUMN),
            pl.lit(account_code).alias(ACCOUNT_CODE_COLUMN),
            pl.lit(account_name).alias(ACCOUNT_NAME_COLUMN),
        )
    )


def find_metadata_value(worksheet: pl.DataFrame, column: str) -> str:
    """Find a label/value pair in the workbook metadata rows."""
    for row in worksheet.head(12).iter_rows():
        for index, value in enumerate(row[:-1]):
            label = str(value or "").replace("：", "").replace(":", "").strip()
            if label == column:
                result = str(row[index + 1] or "").strip()
                if result:
                    return result
                break
    raise ValueError(f"Invalid or missing {column}: missing.")


def find_report_date(worksheet: pl.DataFrame) -> str:
    """Find 檢查日期 in workbook metadata and normalize it to ISO format."""
    raw_date: Any = None
    for row in worksheet.head(12).iter_rows():
        for index, value in enumerate(row[:-1]):
            label = str(value or "").replace("：", "").replace(":", "").strip()
            if label == DATE_COLUMN:
                raw_date = row[index + 1]
                break
        if raw_date is not None:
            break

    if isinstance(raw_date, datetime):
        return raw_date.date().isoformat()
    if isinstance(raw_date, date):
        return raw_date.isoformat()

    date_text = str(raw_date or "").strip()
    for date_format in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(date_text, date_format).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Invalid or missing {DATE_COLUMN}: {date_text or 'missing'}.")
