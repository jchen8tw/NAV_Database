# Holdings Database

A small Dash application for uploading, storing, and exploring Excel holdings reports.

## Run

```bash
uv sync
uv run python main.py
```

Open the local URL printed by Dash (normally <http://127.0.0.1:8050>).

## Test

Run the unit tests from the project root:

```bash
UV_CACHE_DIR=/tmp/holdings-uv-cache uv run python -m unittest discover -v
```

The temporary cache setting avoids local `uv` cache-permission issues.

Page-specific layouts and helpers are organized under `src/pages/` as
`upload.py`, `daily.py`, and `history.py`.

The dashboard includes:

- Drag-and-drop Excel uploads
- Polars-based report parsing and cleanup, including `檢查日期` from workbook metadata
- SQLite persistence in `nav_database.sqlite3`
- Dated inserts and updates using `(ISIN, 檢查日期)` as the composite primary key
- A single-day holdings table at `/daily` with a report-date picker
- A historical line chart at `/history`, with one series per mutual fund
- Shared navigation between Upload, By Day, and History pages
- A sortable, filterable, paginated data table
