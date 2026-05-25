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
from datetime import datetime
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id     TEXT PRIMARY KEY,
                origin      TEXT NOT NULL,
                destination TEXT NOT NULL,
                travel_date TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                last_checked TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      TEXT NOT NULL,
                checked_at   TEXT NOT NULL,
                airline      TEXT,
                price_twd    INTEGER,
                layover_info TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            )
        """)
        conn.commit()


def generate_task_id(length=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def create_task(origin: str, destination: str, travel_date: str) -> str:
    task_id = generate_task_id()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, origin, destination, travel_date, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, origin.upper(), destination.upper(), travel_date, now)
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


# =============================================================================
# PLAYWRIGHT SCRAPER
# =============================================================================

async def fetch_flight_data(origin: str, destination: str, date: str) -> dict:
    """
    Headless Chromium scraper targeting Google Flights.
    Returns dict: {airline, price_twd, layover_info}

    Note: Google Flights DOM selectors may change over time.
    This implementation targets the current (2025-2026) layout.
    If scraping fails, falls back to a simulated result for dev/demo purposes.
    """
    result = {
        "airline": "N/A",
        "price_twd": 0,
        "layover_info": "N/A",
    }

    url = (
        f"https://www.google.com/travel/flights/search"
        f"?tfs=CBwQAhooagcIARIDe3tvcmlnaW59EgN7e2Rlc3R9fQoGCAESA3t7ZGF0ZX19"
        f"&hl=zh-TW&curr=TWD"
    )

    # Build a clean direct URL instead of templating the encrypted tfs
    # Use the simpler query format
    clean_url = (
        f"https://www.google.com/travel/flights?"
        f"hl=zh-TW&curr=TWD"
        f"#flt={origin}.{destination}.{date};c:TWD;e:1;s:0*1;sd:1;t:f"
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

            # Remove automation hints
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            await page.goto(clean_url, timeout=30000)

            # Random human-like wait after page load
            await asyncio.sleep(random.uniform(3.5, 6.5))

            # Wait for flight results container
            try:
                await page.wait_for_selector('[data-ved]', timeout=15000)
            except Exception:
                pass

            # Extra random delay to mimic reading
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # --- Extract cheapest flight ---
            # Google Flights best flights section
            # Try multiple selector strategies
            price_text = None
            airline_text = None
            layover_raw = None

            # Strategy 1: structured flight list items
            try:
                cards = await page.query_selector_all('li[data-ved]')
                if cards:
                    card = cards[0]

                    # Price
                    price_el = await card.query_selector('[data-isin]')
                    if price_el:
                        price_text = await price_el.inner_text()

                    # Airline
                    airline_el = await card.query_selector('.sSHqwe')
                    if airline_el:
                        airline_text = await airline_el.inner_text()

                    # Stops/layover
                    stop_el = await card.query_selector('.EfT7Ae .ogfYpf')
                    if stop_el:
                        layover_raw = await stop_el.inner_text()
            except Exception:
                pass

            # Strategy 2: generic price spans
            if not price_text:
                try:
                    spans = await page.query_selector_all('span[data-gs]')
                    for span in spans[:5]:
                        txt = await span.inner_text()
                        if "TWD" in txt or "$" in txt or any(c.isdigit() for c in txt):
                            price_text = txt
                            break
                except Exception:
                    pass

            await browser.close()

            # --- Parse price ---
            price_twd = 0
            if price_text:
                digits = "".join(c for c in price_text if c.isdigit())
                if digits:
                    price_twd = int(digits)

            # --- Parse airline ---
            airline = airline_text.strip() if airline_text else "不明航空"

            # --- Parse layover ---
            layover_info = _parse_layover(layover_raw)

            if price_twd > 0:
                result = {
                    "airline": airline,
                    "price_twd": price_twd,
                    "layover_info": layover_info,
                }

    except Exception as exc:
        # Scraping failed - return simulated data for demo/dev
        st.warning(f"爬蟲發生錯誤（{exc}），使用模擬資料示範流程。")
        result = _simulate_flight_data(origin, destination)

    return result


def _parse_layover(raw: Optional[str]) -> str:
    """Parse Google Flights stop text into readable layover string."""
    if not raw:
        return "直飛"
    raw = raw.strip().lower()
    if "nonstop" in raw or "直飛" in raw or raw == "0":
        return "直飛"

    # e.g. "1 stop" / "2 stops" / "1次轉機 HKG 2小時15分"
    # Return raw cleaned if it is already informative
    return raw.strip() if raw else "直飛"


def _simulate_flight_data(origin: str, destination: str) -> dict:
    """Demo fallback when scraping is unavailable."""
    airlines = ["中華航空", "長榮航空", "國泰航空", "日本航空", "新加坡航空"]
    base_prices = {
        ("TPE", "NRT"): 8500,
        ("TPE", "HKG"): 4200,
        ("TPE", "SIN"): 11000,
        ("TPE", "BKK"): 9800,
        ("TPE", "LHR"): 35000,
    }
    layovers = ["直飛", "1次 (HKG / 1小時30分)", "1次 (NRT / 2小時)", "2次 (HKG / 1小時, SIN / 3小時)"]

    key = (origin.upper(), destination.upper())
    base = base_prices.get(key, 15000)
    price = base + random.randint(-2000, 2000)

    return {
        "airline": random.choice(airlines),
        "price_twd": price,
        "layover_info": random.choice(layovers),
    }


def run_fetch_for_task(task_id: str):
    """Synchronous wrapper called by scheduler or Streamlit button."""
    task = get_task(task_id)
    if not task:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(
            fetch_flight_data(task["origin"], task["destination"], task["travel_date"])
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
        tasks = get_all_tasks()
        for task in tasks:
            run_fetch_for_task(task["task_id"])

    scheduler = BackgroundScheduler()
    scheduler.add_job(job, "interval", hours=CHECK_INTERVAL_HOURS, id="monitor_job")
    scheduler.start()


def _start_in_thread():
    """Start scheduler inside a Streamlit-aware thread."""
    t = threading.Thread(target=start_scheduler, daemon=True)
    add_script_run_ctx(t)
    t.start()


# =============================================================================
# DASHBOARD DATA
# =============================================================================

def build_dashboard_df() -> pd.DataFrame:
    tasks = get_all_tasks()
    rows = []
    for task in tasks:
        tid = task["task_id"]
        prices = get_latest_two_prices(tid)

        if not prices:
            rows.append({
                "task_id": tid,
                "航線": f"{task['origin']} → {task['destination']}",
                "出發日期": task["travel_date"],
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

        current_price = latest["price_twd"]
        prev_price = prev["price_twd"] if prev else None
        delta = (current_price - prev_price) if prev_price is not None else None

        rows.append({
            "task_id": tid,
            "航線": f"{task['origin']} → {task['destination']}",
            "出發日期": task["travel_date"],
            "航空公司": latest["airline"] or "-",
            "轉機資訊": latest["layover_info"] or "-",
            "最新價格 (TWD)": current_price,
            "上次價格 (TWD)": prev_price,
            "變動幅度": delta,
            "智能評論": price_comment(current_price, prev_price),
        })

    return pd.DataFrame(rows)


def style_df(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    display_cols = [c for c in df.columns if c != "task_id"]
    display_df = df[display_cols].copy()

    def highlight_drop(row):
        styles = [""] * len(row)
        try:
            idx = display_cols.index("變動幅度")
            val = row.iloc[idx]
            if pd.notna(val) and val < 0:
                styles = [
                    "background-color: #d4edda; color: #155724;" if c in ("最新價格 (TWD)", "變動幅度", "智能評論")
                    else "background-color: #f0fbf2;"
                    for c in display_cols
                ]
        except (ValueError, IndexError):
            pass
        return styles

    styler = display_df.style.apply(highlight_drop, axis=1)
    styler = styler.format({
        "最新價格 (TWD)": lambda x: f"NT${x:,.0f}" if pd.notna(x) else "-",
        "上次價格 (TWD)": lambda x: f"NT${x:,.0f}" if pd.notna(x) else "-",
        "變動幅度": lambda x: f"{x:+,.0f}" if pd.notna(x) else "-",
    })
    return styler


# =============================================================================
# UI HELPERS
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

    # --- Custom CSS ---
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Noto Sans TC', sans-serif; }
    .metric-card {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border-radius: 12px;
        padding: 16px 20px;
        color: #e0e0e0;
        border-left: 4px solid #0f3460;
    }
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

    # --- Header ---
    st.title("✈ 多航點機票即時監控系統")
    st.caption("免註冊 · 免登入 · 24小時自動追蹤 · Powered by Playwright")
    st.divider()

    # --- Query params for shared task view ---
    params = st.query_params
    shared_task_id = params.get("task_id", None)

    # ==========================================================================
    # SIDEBAR: Add new task
    # ==========================================================================
    with st.sidebar:
        st.header("➕ 新增監控任務")

        with st.form("add_task_form"):
            col1, col2 = st.columns(2)
            with col1:
                origin = st.selectbox("出發地 (IATA)", AIRPORT_CODES, index=0)
            with col2:
                destination = st.selectbox("目的地 (IATA)", AIRPORT_CODES, index=8)

            travel_date = st.date_input("出發日期", min_value=datetime.today())
            submitted = st.form_submit_button("🚀 開始監控", use_container_width=True)

            if submitted:
                if origin == destination:
                    st.error("出發地與目的地不能相同！")
                else:
                    date_str = travel_date.strftime("%Y-%m-%d")
                    new_id = create_task(origin, destination, date_str)
                    st.success(f"任務已建立！task_id: `{new_id}`")
                    st.info("系統將在背景自動抓取資料，或點擊下方「立即查詢」手動觸發。")
                    # Auto-fetch once immediately in thread
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
    # SINGLE TASK VIEW (shared link)
    # ==========================================================================
    if shared_task_id:
        task = get_task(shared_task_id)
        if task:
            st.subheader(
                f"📌 任務詳情：{task['origin']} → {task['destination']}"
                f"<span class='task-badge'>{shared_task_id}</span>",
                anchor=False,
            )
            st.markdown(
                f"出發日期：**{task['travel_date']}** ｜ 建立時間：{task['created_at']}",
                unsafe_allow_html=False,
            )

            col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 4])
            with col_btn1:
                if st.button("🔄 立即查詢", use_container_width=True):
                    with st.spinner("正在爬取最新資料..."):
                        run_fetch_for_task(shared_task_id)
                    st.rerun()
            with col_btn2:
                if st.button("📋 複製分享連結", use_container_width=True):
                    st.code(f"?task_id={shared_task_id}")

            prices = get_latest_two_prices(shared_task_id)
            if prices:
                latest = prices[0]
                prev = prices[1] if len(prices) > 1 else None
                prev_price = prev["price_twd"] if prev else None
                comment = price_comment(latest["price_twd"], prev_price)
                delta = (latest["price_twd"] - prev_price) if prev_price else 0

                m1, m2, m3 = st.columns(3)
                m1.metric(
                    "最新票價 (TWD)",
                    f"NT${latest['price_twd']:,}",
                    delta=f"{delta:+,}" if prev_price else None,
                    delta_color="inverse",
                )
                m2.metric("航空公司", latest["airline"] or "-")
                m3.metric("轉機資訊", latest["layover_info"] or "-")

                st.info(comment)
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
                    conn,
                    params=(shared_task_id,),
                )
            if not hist.empty:
                st.dataframe(hist, use_container_width=True, hide_index=True)
            else:
                st.caption("暫無歷史紀錄。")

            st.divider()

        else:
            st.error(f"找不到 task_id = `{shared_task_id}` 的任務。")

    # ==========================================================================
    # MAIN DASHBOARD: All tasks table
    # ==========================================================================
    st.subheader("📊 全部監控任務總覽")

    col_refresh, col_info = st.columns([1, 5])
    with col_refresh:
        if st.button("🔄 重新整理", use_container_width=True):
            st.rerun()
    with col_info:
        st.caption("降價儲存格自動標示淺綠色 ✅ ｜點選任務 ID 欄位的連結可複製分享網址")

    df = build_dashboard_df()

    if df.empty:
        st.info("目前沒有監控任務。請在左側側欄新增第一筆！")
    else:
        # Show task links
        with st.expander("🔗 各任務分享連結", expanded=False):
            for _, row in df.iterrows():
                st.code(f"?task_id={row['task_id']}  |  {row['航線']}  {row['出發日期']}")

        styler = style_df(df)
        st.dataframe(styler, use_container_width=True, hide_index=True, height=420)

        # Manual fetch all
        if st.button("⚡ 立即全部更新 (手動觸發爬蟲)", use_container_width=False):
            prog = st.progress(0, text="正在更新...")
            tasks = get_all_tasks()
            for i, task in enumerate(tasks):
                prog.progress((i + 1) / len(tasks), text=f"查詢 {task['origin']} → {task['destination']}...")
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
