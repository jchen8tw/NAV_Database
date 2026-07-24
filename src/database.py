import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl


DATABASE_PATH = Path(__file__).resolve().parent.parent / "nav_database.sqlite3"
TABLE_NAME = "holdings"
DATE_COLUMN = "檢查日期"
ACCOUNT_CODE_COLUMN = "專戶代號"
ACCOUNT_NAME_COLUMN = "專戶名稱"
ISSUE_SIZE_COLUMN = "標的發行規模（標的幣別）或流通股數"
NAV_RATIO_COLUMN = "佔淨資產比重(%)"
ASSET_RATIO_COLUMN = "佔標的資產或單位數比重(%)"
CURRENCY_COLUMN = "商品幣別"
HOLDINGS_COLUMNS = [
    ACCOUNT_CODE_COLUMN,
    ACCOUNT_NAME_COLUMN,
    "ISIN",
    "標的名稱",
    "標的種類",
    "類型別",
    "庫存單位數",
    "基金淨值/ETF收盤價",
    CURRENCY_COLUMN,
    "持有市值(帳戶幣別)",
    "持有市值(標的幣別)",
    ISSUE_SIZE_COLUMN,
    NAV_RATIO_COLUMN,
    ASSET_RATIO_COLUMN,
]


def find_isin_column(df: pl.DataFrame) -> str:
    """Return the report's ISIN column, allowing minor header variations."""
    candidates = [column for column in df.columns if "isin" in column.casefold()]
    if not candidates:
        raise ValueError("The workbook does not contain an ISIN column.")
    exact_matches = [
        column
        for column in candidates
        if "".join(character for character in column.casefold() if character.isalnum())
        == "isin"
    ]
    return exact_matches[0] if exact_matches else candidates[0]


def quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def sqlite_type(dtype: pl.DataType) -> str:
    if dtype.is_integer() or dtype == pl.Boolean:
        return "INTEGER"
    if dtype.is_float() or dtype.is_decimal():
        return "REAL"
    if dtype == pl.Binary:
        return "BLOB"
    return "TEXT"


def sqlite_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def store_dataframe(df: pl.DataFrame, database_path: Path = DATABASE_PATH) -> int:
    """Upsert holdings using (ISIN, 檢查日期, 專戶代號) as the primary key."""
    if df.is_empty():
        raise ValueError("The workbook contains no data rows.")
    if DATE_COLUMN not in df.columns:
        raise ValueError(f"The dataframe does not contain '{DATE_COLUMN}'.")
    if ACCOUNT_CODE_COLUMN not in df.columns:
        raise ValueError(f"The dataframe does not contain '{ACCOUNT_CODE_COLUMN}'.")

    isin_column = find_isin_column(df)
    primary_key_columns = [isin_column, DATE_COLUMN, ACCOUNT_CODE_COLUMN]
    normalized_keys = [
        tuple(str(value or "").strip() for value in row)
        for row in df.select(primary_key_columns).iter_rows()
    ]
    if any(not all(key) for key in normalized_keys):
        raise ValueError(
            f"Columns '{isin_column}', '{DATE_COLUMN}', and "
            f"'{ACCOUNT_CODE_COLUMN}' cannot be blank."
        )
    if len(normalized_keys) != len(set(normalized_keys)):
        raise ValueError(
            f"The workbook contains duplicate "
            f"({isin_column}, {DATE_COLUMN}, {ACCOUNT_CODE_COLUMN}) values."
        )

    quoted_table = quote_identifier(TABLE_NAME)
    column_definitions = [
        f"{quote_identifier(column)} {sqlite_type(dtype)}"
        + (" NOT NULL" if column in primary_key_columns else "")
        for column, dtype in df.schema.items()
    ]
    primary_key = ", ".join(quote_identifier(column) for column in primary_key_columns)

    def create_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"CREATE TABLE {quoted_table} "
            f"({', '.join(column_definitions)}, PRIMARY KEY ({primary_key}))"
        )

    with sqlite3.connect(database_path) as connection:
        existing_columns = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        if not existing_columns:
            create_table(connection)
        else:
            primary_keys = [
                row[1]
                for row in sorted(existing_columns, key=lambda row: row[5])
                if row[5] > 0
            ]
            if primary_keys != primary_key_columns:
                legacy_name = datetime.now().strftime("holdings_legacy_%Y%m%d_%H%M%S")
                quoted_legacy = quote_identifier(legacy_name)
                connection.execute(f"ALTER TABLE {quoted_table} RENAME TO {quoted_legacy}")
                create_table(connection)
                legacy_names = {row[1] for row in existing_columns}
                common_columns = [column for column in df.columns if column in legacy_names]
                if all(column in common_columns for column in primary_key_columns):
                    quoted_common = ", ".join(quote_identifier(column) for column in common_columns)
                    connection.execute(
                        f"INSERT OR IGNORE INTO {quoted_table} ({quoted_common}) "
                        f"SELECT {quoted_common} FROM {quoted_legacy} "
                        f"WHERE {quote_identifier(DATE_COLUMN)} IS NOT NULL"
                    )
                existing_columns = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()

            existing_names = {row[1] for row in existing_columns}
            for column, dtype in df.schema.items():
                if column not in existing_names:
                    connection.execute(
                        f"ALTER TABLE {quoted_table} ADD COLUMN "
                        f"{quote_identifier(column)} {sqlite_type(dtype)}"
                    )

        df = backfill_classifications(df, connection)

        quoted_columns = [quote_identifier(column) for column in df.columns]
        placeholders = ", ".join("?" for _ in df.columns)
        update_columns = [column for column in df.columns if column not in primary_key_columns]
        conflict_action = (
            "DO UPDATE SET "
            + ", ".join(
                f"{quote_identifier(column)} = excluded.{quote_identifier(column)}"
                for column in update_columns
            )
            if update_columns
            else "DO NOTHING"
        )
        statement = (
            f"INSERT INTO {quoted_table} ({', '.join(quoted_columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT ({primary_key}) {conflict_action}"
        )
        rows = [tuple(sqlite_value(value) for value in row) for row in df.iter_rows()]
        connection.executemany(statement, rows)
        total_rows = connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()
    return int(total_rows[0])


def backfill_classifications(
    df: pl.DataFrame, connection: sqlite3.Connection
) -> pl.DataFrame:
    """Fill missing classifications from the newest stored row for each ISIN."""
    classification_columns = ["標的種類", "類型別"]
    if not all(column in df.columns for column in classification_columns):
        return df
    isin_column = find_isin_column(df)
    historical: dict[str, dict[str, str]] = {
        column: {} for column in classification_columns
    }
    for column in classification_columns:
        rows = connection.execute(
            f"SELECT {quote_identifier(isin_column)}, {quote_identifier(column)} "
            f"FROM {quote_identifier(TABLE_NAME)} "
            f"WHERE {quote_identifier(column)} IS NOT NULL "
            f"AND TRIM(CAST({quote_identifier(column)} AS TEXT)) <> '' "
            f"ORDER BY {quote_identifier(DATE_COLUMN)} DESC"
        ).fetchall()
        for isin, value in rows:
            historical[column].setdefault(str(isin).strip(), str(value).strip())

    expressions = []
    for column in classification_columns:
        fallback = pl.col(isin_column).cast(pl.String).str.strip_chars().replace_strict(
            historical[column], default=None, return_dtype=pl.String
        )
        expressions.append(
            pl.when(
                pl.col(column).is_null()
                | (pl.col(column).cast(pl.String).str.strip_chars() == "")
            )
            .then(fallback)
            .otherwise(pl.col(column))
            .alias(column)
        )
    return df.with_columns(expressions)


def load_holdings_history(database_path: Path = DATABASE_PATH) -> pl.DataFrame:
    """Return every stored holding observation using a stable history schema."""
    columns = [DATE_COLUMN, *HOLDINGS_COLUMNS]
    empty = pl.DataFrame(schema={column: pl.String for column in columns})
    if not holdings_table_is_ready(database_path):
        return empty
    quoted_columns = ", ".join(quote_identifier(column) for column in columns)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            f"SELECT {quoted_columns} FROM {quote_identifier(TABLE_NAME)} "
            f"ORDER BY {quote_identifier(DATE_COLUMN)}, "
            f"{quote_identifier('標的名稱')}, {quote_identifier(ACCOUNT_CODE_COLUMN)}"
        ).fetchall()
    return pl.DataFrame(rows, schema=columns, orient="row") if rows else empty


def holdings_table_is_ready(database_path: Path = DATABASE_PATH) -> bool:
    if not database_path.exists():
        return False
    with sqlite3.connect(database_path) as connection:
        columns = connection.execute(
            f"PRAGMA table_info({quote_identifier(TABLE_NAME)})"
        ).fetchall()
    return {DATE_COLUMN, *HOLDINGS_COLUMNS}.issubset({row[1] for row in columns})


def load_available_dates(database_path: Path = DATABASE_PATH) -> list[str]:
    if not holdings_table_is_ready(database_path):
        return []
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            f"SELECT DISTINCT {quote_identifier(DATE_COLUMN)} "
            f"FROM {quote_identifier(TABLE_NAME)} ORDER BY {quote_identifier(DATE_COLUMN)}"
        ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def load_holdings_by_date(
    report_date: str | None, database_path: Path = DATABASE_PATH
) -> pl.DataFrame:
    empty = pl.DataFrame(schema={column: pl.String for column in HOLDINGS_COLUMNS})
    if not report_date or not holdings_table_is_ready(database_path):
        return empty
    quoted_columns = ", ".join(
        quote_identifier(column) for column in HOLDINGS_COLUMNS
    )
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            f"SELECT {quoted_columns} FROM {quote_identifier(TABLE_NAME)} "
            f"WHERE {quote_identifier(DATE_COLUMN)} = ? "
            f"ORDER BY {quote_identifier('標的名稱')}",
            (report_date,),
        ).fetchall()
    return (
        pl.DataFrame(rows, schema=HOLDINGS_COLUMNS, orient="row")
        if rows
        else empty
    )
