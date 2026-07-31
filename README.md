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

## Database location

By default, the application stores its SQLite database at
`<project-root>/nav_database.sqlite3`. The path is configured by the
`DATABASE_PATH` constant near the top of `src/database.py`:

```python
DATABASE_PATH = Path(__file__).resolve().parent.parent / "nav_database.sqlite3"
```

Maintainers can change the database location by updating this constant. The
current expression resolves the path relative to the project root, so it does
not depend on the directory from which the application is started.

Page-specific layouts and helpers are organized under `src/pages/` as
`upload.py`, `daily.py`, and `history.py`.

The dashboard includes:

- Drag-and-drop Excel uploads
- Polars-based parsing and cleanup for both supported custodian formats: the
  metadata-based workbook and the worksheet-per-account `越權檢核` workbook
- SQLite persistence in `nav_database.sqlite3`
- Dated inserts and updates using `(ISIN, 檢查日期)` as the composite primary key
- A single-day holdings table at `/daily` with a report-date picker
- A historical line chart at `/history`, with one series per mutual fund
- Shared navigation between Upload, By Day, and History pages
- A sortable, filterable, paginated data table

> **Note：**國泰世華銀行 Excel 中的「檢查日期」會比實際資料日期晚一個工作天。也就是說，若檢查日期為 D 日，庫存、淨值等資料實際上是 D-1 個工作天的資料，並於 D 日進行檢查。

## 使用者輸入與輸出流程

```mermaid
flowchart LR

    subgraph A[上傳資料]
        A1[拖曳或選取<br/>保管銀行的Excel越權報表<br/>（預計支援世華銀行與中信的格式）]
        A2[處理<br/>持倉資料]
        A3[(儲存至 SQLite<br/>資料庫)]
        A4[顯示上傳成功/失敗]
    end

    subgraph B[查看單日資料]
        B1[進入單日資料頁面]
        B2[選擇查看日期]
        B3-1[查看可排序、篩選<br/>與分頁的表格<br/>（格式同越權報表）]
        B3-2[查看各專戶在單一標的持倉的比例]
        B4-1[可匯出excel]
        B4-2[圓餅圖]
    end

    subgraph C[查看歷史趨勢]
        C1[進入歷史趨勢頁面]
        C2[下拉式選單選擇查看專戶或標的]
        C3-1[彙整標的的<br/>時間序列資料（淨值、標的發行股數、各專戶佔比等）]
        C3-2[彙整專戶的<br/>時間序列資料（標的市值佔比、庫存單位數等）]
        C4-1[折線圖<br/>（可選起訖日期）]
        C4-2[長條圖/折線圖<br/>（可選起訖日期）]
    end

    A1 --> A2 --> A3 --> A4
    B1 --> B2
    A3 --> B2
    B2 --> B3-1 --> B4-1
    B2 --> B3-2 --> B4-2
    A3 --> C2
    C1 --> C2 --> |選擇標的|C3-1 --> C4-1
    C2 --> |選擇專戶|C3-2 --> C4-2
```
