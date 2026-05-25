# =============================================================================
# 多航點機票即時監控系統 - Flight Price Monitor
# =============================================================================
# 安裝步驟：
#   pip install streamlit playwright pandas apscheduler
#   playwright install chromium
# =============================================================================

import streamlit as st
import sqlite3
import asyncio
import threading
import random
import string
import time
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
from playwright.async_api import async_playwright
from apscheduler.schedulers.background import BackgroundScheduler
from streamlit.runtime.scriptrunner import add_script_run_ctx

# =============================================================================
# CONFIG
# =============================================================================
DB_PATH = "flight_monitor.db"
CHECK_INTERVAL_HOURS = 4
PAGE_TITLE = "✈ 機票價格監控"

# =============================================================================
# DATABASE
# =============================================================================

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        # ------------------------------------------------------------------
        # tasks table: drop and recreate when schema is missing any column
        # ------------------------------------------------------------------
        task_col_rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
        task_cols = {r[1] for r in task_col_rows}
        required = {"task_id", "origin", "destination", "depart_date",
                    "return_date", "trip_type", "created_at", "last_checked"}

        if task_cols and not required.issubset(task_cols):
            # Incompatible old schema -- drop and rebuild cleanly.
            conn.execute("DROP TABLE IF EXISTS tasks")
            task_cols = set()

        if not task_cols:
            conn.execute("""
                CREATE TABLE tasks (
                    task_id      TEXT PRIMARY KEY,
                    origin       TEXT NOT NULL,
                    destination  TEXT NOT NULL,
                    depart_date  TEXT NOT NULL DEFAULT '',
                    return_date  TEXT,
                    trip_type    TEXT DEFAULT 'oneway',
                    created_at   TEXT NOT NULL DEFAULT '',
                    last_checked TEXT
                )
            """)

        # ------------------------------------------------------------------
        # price_history table: schema unchanged, safe to keep
        # ------------------------------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      TEXT NOT NULL,
                checked_at   TEXT NOT NULL,
                airline      TEXT,
                price_twd    INTEGER,
                layover_info TEXT
            )
        """)
        conn.commit()
def generate_task_id(length=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def create_task(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str],
    trip_type: str,
) -> str:
    task_id = generate_task_id()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tasks
               (task_id, origin, destination, depart_date, return_date, trip_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, origin.upper(), destination.upper(),
             depart_date, return_date, trip_type, now)
        )
        conn.commit()
    return task_id


def save_price(task_id: str, airline: str, price_twd: int, layover_info: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_history (task_id, checked_at, airline, price_twd, layover_info) VALUES (?, ?, ?, ?, ?)",
            (task_id, now, airline, price_twd, layover_info)
        )
        conn.execute(
            "UPDATE tasks SET last_checked = ? WHERE task_id = ?",
            (now, task_id)
        )
        conn.commit()


def get_all_tasks():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()


def get_latest_two_prices(task_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM price_history WHERE task_id = ? ORDER BY checked_at DESC LIMIT 2",
            (task_id,)
        ).fetchall()
    return rows


def get_task(task_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()


# =============================================================================
# PRICE COMMENT
# =============================================================================

def price_comment(current: int, previous: Optional[int]) -> str:
    if previous is None:
        return "🆕 首次建立監控，正在累積歷史數據。"
    diff = current - previous
    if diff < 0:
        return f"📉 比上次查詢降了 {abs(diff):,} 元！下手時機變好！"
    elif diff > 0:
        return f"📈 比上次變貴了 {diff:,} 元，建議再等等。"
    else:
        return "➡ 價格與上次相同，持續觀望中。"


def format_date_range(task) -> str:
    """Format depart / return dates for display."""
    try:
        depart = task["depart_date"]
        trip_type = task["trip_type"] if task["trip_type"] else "oneway"
        if trip_type == "roundtrip" and task["return_date"]:
            return f"{depart} ~ {task['return_date']}"
        return depart
    except Exception:
        return "-"


# =============================================================================
# PLAYWRIGHT SCRAPER (simulation fallback)
# =============================================================================

async def fetch_flight_data(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    trip_type: str = "oneway",
) -> dict:
    """
    Headless Chromium scraper targeting Google Flights.
    Falls back to simulated data if scraping fails.
    """
    result = {"airline": "N/A", "price_twd": 0, "layover_info": "N/A"}

    if trip_type == "roundtrip" and return_date:
        clean_url = (
            f"https://www.google.com/travel/flights?"
            f"hl=zh-TW&curr=TWD"
            f"#flt={origin}.{destination}.{depart_date}*{destination}.{origin}.{return_date}"
            f";c:TWD;e:1;s:0*1;sd:1;t:r"
        )
    else:
        clean_url = (
            f"https://www.google.com/travel/flights?"
            f"hl=zh-TW&curr=TWD"
            f"#flt={origin}.{destination}.{depart_date};c:TWD;e:1;s:0*1;sd:1;t:f"
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="zh-TW",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            await page.goto(clean_url, timeout=30000)
            await asyncio.sleep(random.uniform(3.5, 6.5))

            try:
                await page.wait_for_selector('[data-ved]', timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(1.5, 3.0))

            price_text = None
            airline_text = None
            layover_raw = None

            try:
                cards = await page.query_selector_all('li[data-ved]')
                if cards:
                    card = cards[0]
                    price_el = await card.query_selector('[data-isin]')
                    if price_el:
                        price_text = await price_el.inner_text()
                    airline_el = await card.query_selector('.sSHqwe')
                    if airline_el:
                        airline_text = await airline_el.inner_text()
                    stop_el = await card.query_selector('.EfT7Ae .ogfYpf')
                    if stop_el:
                        layover_raw = await stop_el.inner_text()
            except Exception:
                pass

            if not price_text:
                try:
                    spans = await page.query_selector_all('span[data-gs]')
                    for span in spans[:5]:
                        txt = await span.inner_text()
                        if any(c.isdigit() for c in txt):
                            price_text = txt
                            break
                except Exception:
                    pass

            await browser.close()

            price_twd = 0
            if price_text:
                digits = "".join(c for c in price_text if c.isdigit())
                if digits:
                    price_twd = int(digits)

            layover_info = _parse_layover(layover_raw)
            airline = airline_text.strip() if airline_text else "不明航空"

            if price_twd > 0:
                result = {
                    "airline": airline,
                    "price_twd": price_twd,
                    "layover_info": layover_info,
                }

    except Exception as exc:
        st.warning(f"爬蟲發生錯誤（{exc}），使用模擬資料示範流程。")
        result = _simulate_flight_data(origin, destination, trip_type)

    return result


def _parse_layover(raw: Optional[str]) -> str:
    if not raw:
        return "直飛"
    raw = raw.strip().lower()
    if "nonstop" in raw or "直飛" in raw or raw == "0":
        return "直飛"
    return raw.strip()


def _simulate_flight_data(origin: str, destination: str, trip_type: str = "oneway") -> dict:
    airlines = ["中華航空", "長榮航空", "國泰航空", "日本航空", "新加坡航空"]
    base_prices = {
        ("TPE", "NRT"): 8500,  ("TPE", "HKG"): 4200,
        ("TPE", "SIN"): 11000, ("TPE", "BKK"): 9800,
        ("TPE", "LHR"): 35000, ("TPE", "ICN"): 6500,
    }
    layovers = [
        "直飛",
        "1次 (HKG / 1小時30分)",
        "1次 (NRT / 2小時)",
        "2次 (HKG / 1小時, SIN / 3小時)",
    ]
    key = (origin.upper(), destination.upper())
    base = base_prices.get(key, 15000)
    # Round-trip price roughly doubles with small discount
    multiplier = 1.85 if trip_type == "roundtrip" else 1.0
    price = int((base + random.randint(-2000, 2000)) * multiplier)
    return {
        "airline": random.choice(airlines),
        "price_twd": price,
        "layover_info": random.choice(layovers),
    }


def run_fetch_for_task(task_id: str):
    task = get_task(task_id)
    if not task:
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Support both old (travel_date) and new (depart_date) schema
        depart = task["depart_date"] if task["depart_date"] else task["travel_date"] if "travel_date" in task.keys() else ""
        return_d = task["return_date"] if task["return_date"] else None
        trip_type = task["trip_type"] if task["trip_type"] else "oneway"
        data = loop.run_until_complete(
            fetch_flight_data(task["origin"], task["destination"],
                              depart, return_d, trip_type)
        )
    finally:
        loop.close()
    if data and data["price_twd"] > 0:
        save_price(task_id, data["airline"], data["price_twd"], data["layover_info"])


# =============================================================================
# BACKGROUND SCHEDULER
# =============================================================================

_scheduler_started = False


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def job():
        for task in get_all_tasks():
            run_fetch_for_task(task["task_id"])

    scheduler = BackgroundScheduler()
    scheduler.add_job(job, "interval", hours=CHECK_INTERVAL_HOURS, id="monitor_job")
    scheduler.start()


def _start_in_thread():
    t = threading.Thread(target=start_scheduler, daemon=True)
    add_script_run_ctx(t)
    t.start()


# =============================================================================
# DASHBOARD DATA
# =============================================================================

def build_dashboard_df() -> pd.DataFrame:
    rows = []
    for task in get_all_tasks():
        tid = task["task_id"]
        prices = get_latest_two_prices(tid)
        trip_type = task["trip_type"] if task["trip_type"] else "oneway"
        trip_label = "來回" if trip_type == "roundtrip" else "單程"

        if not prices:
            rows.append({
                "task_id": tid,
                "航線": f"{task['origin']} → {task['destination']}",
                "行程類型": trip_label,
                "出發日期": task["depart_date"] or "-",
                "回程日期": task["return_date"] or "-",
                "航空公司": "-",
                "轉機資訊": "-",
                "最新價格 (TWD)": None,
                "上次價格 (TWD)": None,
                "變動幅度": None,
                "智能評論": "🆕 首次建立監控，正在累積歷史數據。",
            })
            continue

        latest = prices[0]
        prev = prices[1] if len(prices) > 1 else None
        prev_price = prev["price_twd"] if prev else None
        delta = (latest["price_twd"] - prev_price) if prev_price is not None else None

        rows.append({
            "task_id": tid,
            "航線": f"{task['origin']} → {task['destination']}",
            "行程類型": trip_label,
            "出發日期": task["depart_date"] or "-",
            "回程日期": task["return_date"] or "-",
            "航空公司": latest["airline"] or "-",
            "轉機資訊": latest["layover_info"] or "-",
            "最新價格 (TWD)": latest["price_twd"],
            "上次價格 (TWD)": prev_price,
            "變動幅度": delta,
            "智能評論": price_comment(latest["price_twd"], prev_price),
        })

    return pd.DataFrame(rows)


def style_df(df: pd.DataFrame):
    display_cols = [c for c in df.columns if c != "task_id"]
    display_df = df[display_cols].copy()

    def highlight_drop(row):
        try:
            idx = display_cols.index("變動幅度")
            val = row.iloc[idx]
            if pd.notna(val) and val < 0:
                return [
                    "background-color: #d4edda; color: #155724;"
                    if c in ("最新價格 (TWD)", "變動幅度", "智能評論")
                    else "background-color: #f0fbf2;"
                    for c in display_cols
                ]
        except (ValueError, IndexError):
            pass
        return [""] * len(display_cols)

    return (
        display_df.style
        .apply(highlight_drop, axis=1)
        .format({
            "最新價格 (TWD)": lambda x: f"NT${x:,.0f}" if pd.notna(x) else "-",
            "上次價格 (TWD)": lambda x: f"NT${x:,.0f}" if pd.notna(x) else "-",
            "變動幅度": lambda x: f"{x:+,.0f}" if pd.notna(x) else "-",
        })
    )


# =============================================================================
# AIRPORT LIST
# =============================================================================

AIRPORT_CODES = [
    "TPE", "TSA", "RMQ", "KHH", "HUN",
    "NRT", "HND", "KIX", "ITM", "NGO", "CTS", "OKA",
    "HKG", "MFM",
    "ICN", "GMP", "PUS",
    "SIN", "BKK", "DMK", "KUL", "MNL", "SGN",
    "LHR", "CDG", "FRA", "AMS", "FCO", "BCN", "IST",
    "JFK", "LAX", "SFO", "ORD", "SEA",
    "SYD", "MEL",
]


# =============================================================================
# STREAMLIT APP
# =============================================================================

def main():
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon="✈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_db()
    _start_in_thread()

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Noto Sans TC', sans-serif; }
    .task-badge {
        display: inline-block;
        background: #0f3460;
        color: #e0e0e0;
        padding: 2px 10px;
        border-radius: 20px;
        font-size: 0.8em;
        font-family: monospace;
        margin-left: 8px;
    }
    div[data-testid="stDataFrame"] table { font-size: 0.88rem; }
    </style>
    """, unsafe_allow_html=True)

    st.title("✈ 多航點機票即時監控系統")
    st.caption("免註冊 · 免登入 · 24小時自動追蹤 · Powered by Playwright")
    st.divider()

    params = st.query_params
    shared_task_id = params.get("task_id", None)

    # ==========================================================================
    # SIDEBAR
    # ==========================================================================
    with st.sidebar:
        st.header("➕ 新增監控任務")

        with st.form("add_task_form"):
            col1, col2 = st.columns(2)
            with col1:
                origin = st.selectbox("出發地 (IATA)", AIRPORT_CODES, index=0)
            with col2:
                destination = st.selectbox("目的地 (IATA)", AIRPORT_CODES, index=8)

            trip_type = st.radio(
                "行程類型",
                options=["oneway", "roundtrip"],
                format_func=lambda x: "單程" if x == "oneway" else "來回",
                horizontal=True,
            )

            today = date.today()
            col_d1, col_d2 = st.columns(2)
            with col_d1:
                depart_date = st.date_input(
                    "出發日期",
                    value=today + timedelta(days=30),
                    min_value=today,
                )
            with col_d2:
                return_date = st.date_input(
                    "回程日期",
                    value=today + timedelta(days=37),
                    min_value=today,
                    disabled=(trip_type == "oneway"),
                    help="單程票不需填寫",
                )

            submitted = st.form_submit_button("🚀 開始監控", use_container_width=True)

            if submitted:
                if origin == destination:
                    st.error("出發地與目的地不能相同！")
                elif trip_type == "roundtrip" and return_date <= depart_date:
                    st.error("回程日期必須晚於出發日期！")
                else:
                    d_str = depart_date.strftime("%Y-%m-%d")
                    r_str = return_date.strftime("%Y-%m-%d") if trip_type == "roundtrip" else None
                    new_id = create_task(origin, destination, d_str, r_str, trip_type)
                    st.success(f"任務已建立！task_id: `{new_id}`")
                    t = threading.Thread(target=run_fetch_for_task, args=(new_id,), daemon=True)
                    add_script_run_ctx(t)
                    t.start()
                    time.sleep(0.5)
                    st.query_params["task_id"] = new_id
                    st.rerun()

        st.divider()
        st.caption(f"背景自動更新頻率：每 {CHECK_INTERVAL_HOURS} 小時")
        st.caption("分享網址格式：?task_id=xxxxxx")

    # ==========================================================================
    # SINGLE TASK VIEW
    # ==========================================================================
    if shared_task_id:
        task = get_task(shared_task_id)
        if task:
            trip_type = task["trip_type"] if task["trip_type"] else "oneway"
            trip_label = "來回" if trip_type == "roundtrip" else "單程"
            depart = task["depart_date"] or "-"
            ret = task["return_date"] or "-"
            date_display = f"{depart} ~ {ret}" if trip_type == "roundtrip" else depart

            st.subheader(
                f"📌 {trip_label} | {task['origin']} ⇄ {task['destination']} "
                f"<span class='task-badge'>{shared_task_id}</span>",
                anchor=False,
            )
            st.markdown(f"日期：**{date_display}** ｜ 建立時間：{task['created_at']}")

            col_btn1, col_btn2, _ = st.columns([1, 1, 4])
            with col_btn1:
                if st.button("🔄 立即查詢", use_container_width=True):
                    with st.spinner("正在抓取最新資料..."):
                        run_fetch_for_task(shared_task_id)
                    st.rerun()
            with col_btn2:
                if st.button("📋 分享連結", use_container_width=True):
                    st.code(f"?task_id={shared_task_id}")

            prices = get_latest_two_prices(shared_task_id)
            if prices:
                latest = prices[0]
                prev = prices[1] if len(prices) > 1 else None
                prev_price = prev["price_twd"] if prev else None
                delta = (latest["price_twd"] - prev_price) if prev_price else 0

                m1, m2, m3, m4 = st.columns(4)
                m1.metric(
                    "最新票價 (TWD)",
                    f"NT${latest['price_twd']:,}",
                    delta=f"{delta:+,}" if prev_price else None,
                    delta_color="inverse",
                )
                m2.metric("行程類型", trip_label)
                m3.metric("航空公司", latest["airline"] or "-")
                m4.metric("轉機資訊", latest["layover_info"] or "-")

                st.info(price_comment(latest["price_twd"], prev_price))
                st.caption(f"查詢時間：{latest['checked_at']}")
            else:
                st.warning("尚無查詢資料，點擊「立即查詢」開始抓取。")

            st.divider()
            st.subheader("📈 歷史價格紀錄")
            with get_conn() as conn:
                hist = pd.read_sql(
                    "SELECT checked_at AS '查詢時間', airline AS '航空公司', "
                    "price_twd AS '票價 (TWD)', layover_info AS '轉機資訊' "
                    "FROM price_history WHERE task_id = ? ORDER BY checked_at DESC",
                    conn, params=(shared_task_id,),
                )
            if not hist.empty:
                st.dataframe(hist, use_container_width=True, hide_index=True)
            else:
                st.caption("暫無歷史紀錄。")
            st.divider()

        else:
            st.error(f"找不到 task_id = `{shared_task_id}` 的任務。")

    # ==========================================================================
    # MAIN DASHBOARD
    # ==========================================================================
    st.subheader("📊 全部監控任務總覽")

    col_ref, col_info = st.columns([1, 5])
    with col_ref:
        if st.button("🔄 重新整理", use_container_width=True):
            st.rerun()
    with col_info:
        st.caption("降價列自動標示淺綠色 ✅ ｜ 來回票價為去程＋回程合計")

    df = build_dashboard_df()

    if df.empty:
        st.info("目前沒有監控任務。請在左側側欄新增第一筆！")
    else:
        with st.expander("🔗 各任務分享連結", expanded=False):
            for _, row in df.iterrows():
                label = f"{row['航線']}  {row['出發日期']}"
                if row["回程日期"] != "-":
                    label += f" ~ {row['回程日期']}"
                st.code(f"?task_id={row['task_id']}  |  {label}")

        st.dataframe(style_df(df), use_container_width=True, hide_index=True, height=420)

        if st.button("⚡ 立即全部更新"):
            prog = st.progress(0, text="正在更新...")
            tasks = get_all_tasks()
            for i, task in enumerate(tasks):
                prog.progress(
                    (i + 1) / len(tasks),
                    text=f"查詢 {task['origin']} → {task['destination']}..."
                )
                run_fetch_for_task(task["task_id"])
            prog.empty()
            st.success("全部更新完成！")
            st.rerun()

    st.divider()
    st.caption(
        "⚠ 本系統使用 Playwright 爬取公開票價資訊，僅供個人參考，"
        "不保證資料即時性與準確性。請以航空公司官網價格為準。"
    )


if __name__ == "__main__":
    main()
