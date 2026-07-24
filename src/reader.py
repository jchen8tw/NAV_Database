import base64
import binascii
import re
from datetime import date, datetime
from io import BytesIO
from typing import Any

import polars as pl

from src.database import (
    ACCOUNT_CODE_COLUMN,
    ACCOUNT_NAME_COLUMN,
    ASSET_RATIO_COLUMN,
    CURRENCY_COLUMN,
    DATE_COLUMN,
    HOLDINGS_COLUMNS,
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
)


SECOND_FORMAT_COLUMNS = {
    ACCOUNT_NAME_COLUMN: "代操帳戶名稱",
    DATE_COLUMN: "庫存日期",
    "ISIN": "ISIN CODE",
    "標的名稱": "證券名稱",
    "庫存單位數": "庫存股數",
    "基金淨值/ETF收盤價": "收盤價",
    CURRENCY_COLUMN: "證券幣別",
    ISSUE_SIZE_COLUMN: "流通在外單位數",
    NAV_RATIO_COLUMN: "占淨資產",
    ASSET_RATIO_COLUMN: "占發行單位數",
}
NUMERIC_COLUMNS = {
    "庫存單位數": pl.Float64,
    "基金淨值/ETF收盤價": pl.Float64,
    "持有市值(帳戶幣別)": pl.Float64,
    "持有市值(標的幣別)": pl.Float64,
    ISSUE_SIZE_COLUMN: pl.Int64,
    NAV_RATIO_COLUMN: pl.Float64,
    ASSET_RATIO_COLUMN: pl.Float64,
}


def parse_excel(contents: str) -> pl.DataFrame:
    """Decode all recognized holdings worksheets into the canonical schema."""
    try:
        _, encoded = contents.split(",", 1)
        workbook = BytesIO(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("The uploaded file could not be decoded.") from exc

    sheets = pl.read_excel(
        workbook,
        sheet_id=0,
        has_header=False,
        raise_if_empty=False,
        drop_empty_rows=False,
        drop_empty_cols=False,
        infer_schema_length=None,
    )
    if isinstance(sheets, pl.DataFrame):
        sheets = {"Sheet1": sheets}

    holdings = []
    errors = []
    for sheet_name, worksheet in sheets.items():
        if worksheet.is_empty():
            continue
        try:
            parsed = _parse_sheet(worksheet, sheet_name)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if parsed is not None and not parsed.is_empty():
            holdings.append(parsed)

    if not holdings:
        detail = f" ({errors[0]})" if errors else ""
        raise ValueError(f"The workbook does not contain a recognized holdings table.{detail}")
    return pl.concat(holdings, how="vertical_relaxed")


def _parse_sheet(worksheet: pl.DataFrame, sheet_name: str) -> pl.DataFrame | None:
    rows = list(worksheet.iter_rows())
    for header_index, row in enumerate(rows):
        headers = [_clean_header(value) for value in row]
        if (
            "ISIN CODE" in headers
            and set(SECOND_FORMAT_COLUMNS.values()).issubset(headers)
            and _find_market_value_header(headers) is not None
        ):
            return _parse_second_format(worksheet, sheet_name, header_index, headers)
        if "ISIN" in headers:
            return _parse_original_format(worksheet, header_index, headers)
    if any(DATE_COLUMN in [_clean_header(value).replace("：", "").replace(":", "") for value in row] for row in rows[:12]):
        find_metadata_value(worksheet, ACCOUNT_CODE_COLUMN)
    return None


def _parse_original_format(
    worksheet: pl.DataFrame, header_index: int, headers: list[str]
) -> pl.DataFrame:
    if any(not header for header in headers):
        raise ValueError("The workbook contains a blank holdings column header.")
    report_date = find_report_date(worksheet)
    account_code = find_metadata_value(worksheet, ACCOUNT_CODE_COLUMN)
    account_name = find_metadata_value(worksheet, ACCOUNT_NAME_COLUMN)
    data = worksheet.slice(
        header_index + 1, max(worksheet.height - header_index - 2, 0)
    ).rename(dict(zip(worksheet.columns, headers, strict=True)))
    data = data.filter(_nonempty("ISIN"))
    data = _ensure_canonical_columns(data).with_columns(
        pl.lit(report_date).alias(DATE_COLUMN),
        pl.lit(account_code).alias(ACCOUNT_CODE_COLUMN),
        pl.lit(account_name).alias(ACCOUNT_NAME_COLUMN),
    )
    return _normalize_canonical(data)


def _parse_second_format(
    worksheet: pl.DataFrame,
    sheet_name: str,
    header_index: int,
    headers: list[str],
) -> pl.DataFrame:
    market_value_header, account_currency = _find_market_value_header(headers) or (
        None,
        None,
    )
    if market_value_header is None or account_currency is None:
        raise ValueError("The second-format sheet has no currency market-value column.")
    data = _with_headers(worksheet, header_index, headers)
    data = data.filter(_nonempty("ISIN CODE"))
    selected = data.select(
        [pl.col(source).alias(target) for target, source in SECOND_FORMAT_COLUMNS.items()]
        + [
            pl.col(market_value_header).alias("持有市值(帳戶幣別)"),
            pl.when(
                pl.col("證券幣別")
                .cast(pl.String, strict=False)
                .str.strip_chars()
                .str.to_uppercase()
                == account_currency.upper()
            )
            .then(pl.col(market_value_header))
            .otherwise(None)
            .alias("持有市值(標的幣別)"),
        ]
    ).with_columns(pl.lit(sheet_name.strip()).alias(ACCOUNT_CODE_COLUMN))
    return _normalize_canonical(_ensure_canonical_columns(selected))


def _with_headers(
    worksheet: pl.DataFrame, header_index: int, headers: list[str]
) -> pl.DataFrame:
    return worksheet.slice(header_index + 1).rename(
        dict(zip(worksheet.columns, headers, strict=True))
    )


def _ensure_canonical_columns(data: pl.DataFrame) -> pl.DataFrame:
    missing = [column for column in [DATE_COLUMN, *HOLDINGS_COLUMNS] if column not in data.columns]
    if missing:
        data = data.with_columns(pl.lit(None).cast(pl.String).alias(column) for column in missing)
    return data.select([DATE_COLUMN, *HOLDINGS_COLUMNS])


def _normalize_canonical(data: pl.DataFrame) -> pl.DataFrame:
    string_columns = [column for column in data.columns if column not in NUMERIC_COLUMNS]
    return data.with_columns(
        [
            pl.col(column)
            .cast(pl.String, strict=False)
            .str.strip_chars()
            .replace("", None)
            for column in string_columns
        ]
        + [
            pl.col(column)
            .cast(pl.String, strict=False)
            .str.strip_chars()
            .str.replace_all(r"[,％%\s]", "")
            .str.replace_all("（", "(")
            .str.replace_all("）", ")")
            .str.replace(r"^\((.*)\)$", r"-$1")
            .cast(pl.Float64, strict=False)
            .cast(dtype, strict=False)
            for column, dtype in NUMERIC_COLUMNS.items()
        ]
    ).with_columns(
        pl.col(DATE_COLUMN).map_elements(normalize_report_date, return_dtype=pl.String)
    )


def _clean_header(value: Any) -> str:
    return str(value or "").strip()


def _find_market_value_header(headers: list[str]) -> tuple[str, str] | None:
    for header in headers:
        match = re.fullmatch(r"市值\s*[\(（]\s*([^()（）]+?)\s*[\)）]", header)
        if match:
            return header, match.group(1).strip()
    return None


def _nonempty(column: str) -> pl.Expr:
    return pl.col(column).cast(pl.String, strict=False).fill_null("").str.strip_chars() != ""


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


def normalize_report_date(raw_date: Any) -> str | None:
    if raw_date is None:
        return None
    if isinstance(raw_date, datetime):
        return raw_date.date().isoformat()
    if isinstance(raw_date, date):
        return raw_date.isoformat()
    date_text = str(raw_date).strip()
    for date_format in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(date_text, date_format).date().isoformat()
        except ValueError:
            pass
    for separator in ("-", "/"):
        parts = date_text.split(separator)
        if len(parts) == 3 and parts[0].isdigit() and len(parts[0]) <= 3:
            try:
                return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2])).isoformat()
            except ValueError:
                pass
    return None


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
    normalized = normalize_report_date(raw_date)
    if normalized:
        return normalized
    date_text = str(raw_date or "").strip()
    raise ValueError(f"Invalid or missing {DATE_COLUMN}: {date_text or 'missing'}.")
