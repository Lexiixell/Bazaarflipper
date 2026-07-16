import colorsys
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import zipfile
from tkinter import ttk, messagebox, colorchooser

import requests

BAZAAR_URL = "https://api.hypixel.net/v2/skyblock/bazaar"
ITEMS_URL = "https://api.hypixel.net/resources/skyblock/items"
BAZAAR_TAX = 0.0125          # 1.25% sell-side bazaar tax (default rate)
MIN_DAILY_COIN_VOLUME = 2_000_000  # min trailing daily $ turnover to count as "alive"
                                    # (coin-based, not unit-based, so expensive items
                                    # with low unit counts aren't unfairly excluded)
EXTREME_MARGIN_THRESHOLD = 150.0  # margins above this get flagged as suspicious
ALL_CATEGORIES = "All"
DEFAULT_SLEEP_HOURS = 8
DEFAULT_SPREAD_N = 12
FULL_LIST_PAGE_SIZE = 40     # items rendered per "page" in Full List (avoids
                              # freezing the window building hundreds at once)

# ---- Auto-refresh ---------------------------------------------------------
DEFAULT_AUTO_REFRESH_ENABLED = True
DEFAULT_AUTO_REFRESH_MINUTES = 2
MIN_AUTO_REFRESH_MINUTES = 1  # floor, so it can't be set to hammer the API

# ---- Update checker ---------------------------------------------------------
# Uses GitHub's "latest release" API (has the downloadable asset attached,
# unlike a bare tag/commit).
APP_VERSION = "1.0.0"  # bump with each GitHub release
GITHUB_REPO = "Lexiixell/Bazaarflipper"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# ---- Price-history / manipulation detection --------------------------------
# Hypixel's API has no historical-price endpoint, so this app builds its own
# local 7-day price history each refresh to sanity-check the current price
# against. Fails open until enough samples exist (no false-flagging on day 1).
PRICE_HISTORY_MAX_AGE_DAYS = 7
PRICE_HISTORY_MIN_SAMPLES = 5        # min snapshots before trusting the average
PRICE_DEVIATION_THRESHOLD_PCT = 25.0 # % off own 7d avg to flag as suspect

# ---- Snapshot staleness -----------------------------------------------------
# lastUpdated is Hypixel's own capture time, not our fetch time - surfaced so
# you know how stale the book might be (esp. on thin/manipulated items).
STALE_DATA_WARNING_SECONDS = 90

# ---- Buy-order buffer -------------------------------------------------------
# The snapshot is already a few seconds old and other buy orders queue up
# fast, so bidding the raw top price often misses the real current order.
# This buffer nudges the buy price up: % of price (scales naturally), with a
# flat coin floor for cheap items and a % ceiling for expensive ones. It also
# scales up for unusually wide raw margins, since those are the items most
# likely to have a thin/stale/contested book.
DEFAULT_BUY_BUFFER_PCT = 1.0        # base % (set from Settings)
BUY_BUFFER_MIN_COINS = 5            # flat floor for cheap items
BUY_BUFFER_MAX_PCT = 20.0           # ceiling on total buffer %
BUY_BUFFER_MARGIN_SCALE_START_PCT = 30.0   # raw margin above this starts scaling buffer up
BUY_BUFFER_MARGIN_SCALE_PER_100 = 3.0      # extra buffer % per 100pts of margin above that
DEFAULT_PLAN_MIN_DAILY_VOLUME = MIN_DAILY_COIN_VOLUME * 4  # Overnight Plan is
    # unattended so it uses a stricter volume floor than the general list.

# ---- Skyblock calendar -----------------------------------------------------
# No "current date" API field exists - derived from wall-clock time against a
# fixed epoch, since SB time runs at a constant rate (20 real min/SB day).
SKYBLOCK_EPOCH_SECONDS = 1560275700
SKYBLOCK_DAY_SECONDS = 1200
SKYBLOCK_DAYS_PER_MONTH = 31
SKYBLOCK_MONTHS_PER_YEAR = 12
SKYBLOCK_MONTH_NAMES = [
    "Early Spring", "Spring", "Late Spring",
    "Early Summer", "Summer", "Late Summer",
    "Early Autumn", "Autumn", "Late Autumn",
    "Early Winter", "Winter", "Late Winter",
]
LATE_WINTER_MONTH_INDEX = 11
JERRY_WORKSHOP_WINDOW_SECONDS = 10 * 3600  # opens for first 10 real hrs of Late Winter


def get_skyblock_date(timestamp=None):
    """Unix timestamp -> (year, month_index, month_name, day) SB calendar coords."""
    if timestamp is None:
        timestamp = time.time()
    elapsed = max(0, timestamp - SKYBLOCK_EPOCH_SECONDS)
    total_sb_days = int(elapsed // SKYBLOCK_DAY_SECONDS)
    days_per_year = SKYBLOCK_DAYS_PER_MONTH * SKYBLOCK_MONTHS_PER_YEAR
    year = total_sb_days // days_per_year + 1
    day_in_year = total_sb_days % days_per_year
    month_index = day_in_year // SKYBLOCK_DAYS_PER_MONTH
    day = day_in_year % SKYBLOCK_DAYS_PER_MONTH + 1
    return year, month_index, SKYBLOCK_MONTH_NAMES[month_index], day


def jerry_workshop_status(now=None):
    """Whether Jerry's Workshop is open: either the SB-calendar Late Winter
    opening window, or real-life December (checked against local clock)."""
    if now is None:
        now = time.time()
    year, month_index, month_name, day = get_skyblock_date(now)
    elapsed = max(0, now - SKYBLOCK_EPOCH_SECONDS)
    total_sb_days = int(elapsed // SKYBLOCK_DAY_SECONDS)
    seconds_into_current_day = elapsed - total_sb_days * SKYBLOCK_DAY_SECONDS
    seconds_into_month = (day - 1) * SKYBLOCK_DAY_SECONDS + seconds_into_current_day

    sb_window_open = (month_index == LATE_WINTER_MONTH_INDEX
                       and seconds_into_month < JERRY_WORKSHOP_WINDOW_SECONDS)
    real_december_open = (time.localtime(now).tm_mon == 12)

    reasons = []
    if sb_window_open:
        reasons.append(f"SkyBlock Late Winter opening window (Year {year}, day {day}/31)")
    if real_december_open:
        reasons.append("real-life December")

    return {
        "active": sb_window_open or real_december_open,
        "reasons": reasons,
        "skyblock_year": year,
        "skyblock_month": month_name,
        "skyblock_day": day,
    }


# ---- Mayor / election API + seasonal event windows -------------------------
# Both the elected mayor's perks and the runner-up minister's single perk can
# trigger a festival, so both are checked.
MAYOR_URL = "https://api.hypixel.net/v2/resources/skyblock/election"

FESTIVAL_PERK_EVENTS = {
    "Fishing Festival": "fishing_festival",        # Marina
    "Mining Fiesta": "mining_fiesta",               # Cole
    "Mythological Ritual": "mythological_ritual",   # Diana
}
PAUL_DUNGEON_DISCOUNT_PERK = "Marauder"  # 20% off dungeon NPC costs
PAUL_DUNGEON_DISCOUNT_PCT = 20.0

# In-month windows (start_day, end_day), 1-indexed inclusive. Mythological
# Ritual has no sub-window - it's active for Diana's whole term.
FISHING_FESTIVAL_DAYS = (1, 3)                 # first 3 days of every month
MINING_FIESTA_DAYS = (1, 7)                    # days 1-7 of specific months
MINING_FIESTA_MONTH_INDICES = {4, 5, 6, 7, 8}  # Summer .. Late Autumn


def fetch_mayor_info():
    """Fetch current mayor + minister perks. Raises on failure - caller
    should treat this as non-fatal supplementary context."""
    response = requests.get(MAYOR_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        return {}

    mayor = data.get("mayor") or {}
    perks = [p.get("name") for p in (mayor.get("perks") or []) if p.get("name")]
    minister = mayor.get("minister") or {}
    minister_perk = (minister.get("perk") or {}).get("name")

    return {
        "name": mayor.get("name"),
        "perks": perks,
        "minister_name": minister.get("name"),
        "minister_perk": minister_perk,
    }


def compute_active_festivals(info, now=None):
    """Which perk-gated festivals are live right now, given fetch_mayor_info()'s
    output. "active_now" = actually in its window, not just "this mayor's term
    makes it possible"."""
    if not info:
        return []
    if now is None:
        now = time.time()
    _, month_index, _, day = get_skyblock_date(now)

    candidates = []
    for name in info.get("perks", []) or []:
        candidates.append((name, info.get("name")))
    if info.get("minister_perk"):
        candidates.append((info["minister_perk"], info.get("minister_name")))

    active = []
    seen = set()
    for perk_name, source in candidates:
        event_key = FESTIVAL_PERK_EVENTS.get(perk_name)
        if not event_key or perk_name in seen:
            continue
        seen.add(perk_name)

        window = None
        active_now = True
        if event_key == "fishing_festival":
            window = FISHING_FESTIVAL_DAYS
            active_now = window[0] <= day <= window[1]
        elif event_key == "mining_fiesta":
            window = MINING_FIESTA_DAYS
            active_now = (month_index in MINING_FIESTA_MONTH_INDICES
                           and window[0] <= day <= window[1])

        active.append({
            "label": perk_name,
            "event_key": event_key,
            "source": source,
            "active_now": active_now,
            "window": window,
        })
    return active


def paul_dungeon_discount_active(info):
    """True if mayor or minister currently holds Paul's Marauder perk."""
    if not info:
        return False
    if PAUL_DUNGEON_DISCOUNT_PERK in (info.get("perks") or []):
        return True
    return info.get("minister_perk") == PAUL_DUNGEON_DISCOUNT_PERK


# Keyword tagging so relevant items can be badged with the event affecting
# their supply/demand. Keyword-based (not an exhaustive ID list) so it stays
# useful as Hypixel adds new event items, at the cost of occasional
# mismatches - fine for an informational badge.
EVENT_ITEM_KEYWORDS = {
    "mining_fiesta": ["REFINED_MINERAL", "GLOSSY_GEMSTONE", "MINERAL", "GEMSTONE",
                       "MITHRIL_ORE", "TITANIUM_ORE"],
    "fishing_festival": ["SHARK", "PRISMARINE", "MAGMA_FISH"],
    "mythological_ritual": ["GRIFFIN", "ANCIENT_CLAW", "MYTHOLOGICAL", "PANDORA",
                             "DAEDALUS", "MEDUSA", "TITAN", "SIREN", "MINOS"],
    "jerry_workshop": ["GIFT", "NORTH_STAR", "SNOW", "ICE_", "JERRY", "GINGERBREAD",
                        "CANDY_CANE", "FROZEN", "WALNUT", "STOCKING"],
    "spooky_festival": ["CANDY_CORN", "MUTANT_ENDERMAN"],
    "dungeon_supply": ["ESSENCE_", "RECOMBOBULATOR", "FUMING_POTATO_BOOK", "SHADOW_FURY",
                        "NECRON", "SCYLLA", "STORM_", "GOLDOR", "MAXOR"],
}


def tag_event_relevance(product_id):
    """event_keys whose keywords appear in this product id."""
    return [key for key, keywords in EVENT_ITEM_KEYWORDS.items()
            if any(kw in product_id for kw in keywords)]


# ---- Fill-time / sell-time estimates ---------------------------------------
def compute_fill_sell_hours(flip, units):
    """Rough hours to fill `units` on the buy side (sellers matching your buy
    order) and sell side (buyers matching your sell offer), from trailing 7d
    volume. None if that side has 0 volume (no basis to estimate from)."""
    sell_daily = flip.get("sell_moving_week", 0) / 7
    buy_daily = flip.get("buy_moving_week", 0) / 7
    fill_hours = (units / sell_daily * 24) if sell_daily > 0 else None
    sell_hours = (units / buy_daily * 24) if buy_daily > 0 else None
    return fill_hours, sell_hours


def fmt_hours(hours):
    """Minutes under 1h, hours to 1dp under a day, else days. Em dash if unknown."""
    if hours is None:
        return "\u2014"
    hours = max(0, hours)
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


# ---- Persistent app-data folder --------------------------------------------
def get_app_data_dir():
    """Per-user data folder, works the same as .py or frozen .exe."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    app_dir = os.path.join(base, "BazaarFlipper")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


APP_DATA_DIR = get_app_data_dir()
OVERRIDES_PATH = os.path.join(APP_DATA_DIR, "category_overrides.json")
SETTINGS_PATH = os.path.join(APP_DATA_DIR, "settings.json")
CUSTOM_CATEGORIES_PATH = os.path.join(APP_DATA_DIR, "custom_categories.json")
PRICE_HISTORY_PATH = os.path.join(APP_DATA_DIR, "price_history.json")
BLACKLIST_PATH = os.path.join(APP_DATA_DIR, "blacklist.json")
MAYOR_CACHE_PATH = os.path.join(APP_DATA_DIR, "mayor_cache.json")  # last-known
    # mayor info, so a failed election fetch falls back to it instead of
    # blanking out event badges.


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except OSError:
        pass  # non-fatal - just won't persist this run


# ---- Theme ------------------------------------------------------------------
BG_DARK = "#181920"
BG_PANEL = "#22232f"
BG_PANEL_RAISED = "#282a3a"
BG_CARD_HOVER = "#2a2c3d"
BG_INPUT = "#2f3143"
BORDER_SUBTLE = "#34364a"

ACCENT = "#b085f5"           # Hypixel-ish purple accent
ACCENT_HOVER = "#c9a3ff"
ACCENT_DIM = "#4b3f72"
ACCENT_SOFT = "#3a2f57"
ACCENT_GREEN = "#4ade80"
ACCENT_YELLOW = "#facc15"
ACCENT_RED = "#f87171"
ACCENT_BLUE = "#60a5fa"
TEXT_MAIN = "#eceaf6"
TEXT_DIM = "#9694ac"
TEXT_FAINT = "#65637a"

FONT_MAIN = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_HEAD = ("Segoe UI", 14, "bold")
FONT_SUBHEAD = ("Segoe UI", 10, "bold")
FONT_PILL = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 11, "bold")

# Deterministic per-category chip color, stable across restarts.
CHIP_PALETTE = [
    "#b085f5", "#60a5fa", "#4ade80", "#facc15",
    "#f87171", "#38bdf8", "#f472b6", "#a3e635",
    "#fb923c", "#2dd4bf",
]

ACCENT_COLOR_PRESETS = [
    "#b085f5", "#60a5fa", "#4ade80", "#facc15", "#f87171",
    "#38bdf8", "#f472b6", "#fb923c", "#2dd4bf", "#a3e635",
]

# ---- User-customizable theme ------------------------------------------------
# Only the accent color is user-facing; hover/dim/soft variants are derived
# from it in HLS space. Applied once at startup (not live) - avoids needing to
# reconfigure every already-built widget.
def _hex_to_rgb01(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb01_to_hex(rgb):
    r, g, b = (max(0, min(1, c)) for c in rgb)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def derive_accent_shades(accent_hex):
    """Derive hover/dim/soft variants of a base accent color via HLS."""
    r, g, b = _hex_to_rgb01(accent_hex)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    hover = colorsys.hls_to_rgb(h, min(0.88, l + 0.12), s)
    dim = colorsys.hls_to_rgb(h, max(0.15, l - 0.42), min(1.0, s * 0.85))
    soft = colorsys.hls_to_rgb(h, max(0.12, l - 0.48), min(1.0, s * 0.75))
    return {
        "hover": _rgb01_to_hex(hover),
        "dim": _rgb01_to_hex(dim),
        "soft": _rgb01_to_hex(soft),
    }


def apply_saved_theme(settings):
    """Override default ACCENT palette from settings.json. Must run before
    _setup_style()/_build_widgets()."""
    global ACCENT, ACCENT_HOVER, ACCENT_DIM, ACCENT_SOFT
    saved = settings.get("accent_color")
    if isinstance(saved, str) and len(saved) == 7 and saved.startswith("#"):
        shades = derive_accent_shades(saved)
        ACCENT = saved
        ACCENT_HOVER = shades["hover"]
        ACCENT_DIM = shades["dim"]
        ACCENT_SOFT = shades["soft"]


# Fields shown inside an expanded item box (name/category shown in header).
DETAIL_FIELDS = [
    ("buy_order_at",  "Buy At"),
    ("buy_buffer",    "  \u21b3 buffer included"),
    ("sell_offer_at", "Sell At"),
    ("profit",        "Profit/Item"),
    ("margin",        "Margin %"),
    ("volume",        "Daily Volume"),
    ("weekly_volume", "Weekly Volume (7d)"),
]

# Badge text/color per event_key, shown when the event is CURRENTLY relevant.
EVENT_BADGE_STYLE = {
    "mining_fiesta":       ("\u26cf Mining Fiesta", ACCENT_YELLOW),
    "fishing_festival":    ("\U0001F41F Fishing Festival", ACCENT_BLUE),
    "mythological_ritual": ("\u2666 Mythological Ritual", ACCENT_GREEN),
    "jerry_workshop":      ("\u2744 Jerry's Workshop", ACCENT_BLUE),
    "spooky_festival":     ("\U0001F383 Spooky Festival", ACCENT_YELLOW),
    "dungeon_supply":      ("\u2694 Paul -20% Chests", ACCENT_RED),
}

SORT_OPTIONS = [
    ("Profit/hr (Purse)", "profit_hr"),
    ("Profit/Item",       "profit"),
    ("Margin %",           "margin"),
    ("Daily Volume",       "volume"),
    ("Buy At",             "buy_order_at"),
    ("Sell At",             "sell_offer_at"),
    ("Item",                "item"),
    ("Category",             "category"),
    ("Weekly Sales",       "weekly_volume")
]


def format_category(raw):
    if not raw:
        return None
    return raw.replace("_", " ").title()


def chip_color(category_name):
    """Deterministic color for a category name."""
    h = sum(ord(c) for c in category_name)
    return CHIP_PALETTE[h % len(CHIP_PALETTE)]


# Hypixel's category field doesn't cover every bazaar item (essence, runes,
# some reforge stones). Fallback fills in well-known ID prefixes. Manual
# overrides take priority over both this and Hypixel's own data.
ID_PREFIX_FALLBACKS = [
    ("ENCHANTMENT_", "Enchanted Books"),
    ("RUNE_", "Runes"),
]


def infer_category_from_id(product_id):
    for prefix, category in ID_PREFIX_FALLBACKS:
        if product_id.startswith(prefix):
            return category
    return None


def _parse_version(v):
    """'v1.4.2'/'1.4.2' -> (1,4,2) for numeric compare (avoids '1.10' < '1.9')."""
    v = v.strip().lstrip("vV")
    parts = []
    for chunk in v.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def check_for_update():
    """Compare GitHub's latest release tag against APP_VERSION. Returns
    (update_available, latest_version, release_url, asset_url) - asset_url is
    the .zip attached to the release (None if not found; GitHub's own
    auto-generated source zips are deliberately excluded). Raises on
    network/HTTP failure."""
    response = requests.get(GITHUB_RELEASES_API, timeout=10,
                             headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    data = response.json()
    latest_tag = data.get("tag_name", "")
    release_url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest"
    update_available = _parse_version(latest_tag) > _parse_version(APP_VERSION)

    asset_url = None
    for asset in data.get("assets", []):
        if asset.get("name", "").lower().endswith(".zip"):
            asset_url = asset.get("browser_download_url")
            break

    return update_available, latest_tag, release_url, asset_url


def fetch_item_categories():
    """Fetch item metadata -> {item_id: formatted_category}."""
    response = requests.get(ITEMS_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        return {}

    mapping = {}
    for item in data.get("items", []):
        item_id = item.get("id")
        formatted = format_category(item.get("category"))
        if item_id and formatted:
            mapping[item_id] = formatted
    return mapping


def fetch_bazaar_data():
    """Raw bazaar API response (products + Hypixel's lastUpdated timestamp)."""
    response = requests.get(BAZAAR_URL, timeout=15)
    response.raise_for_status()

    data = response.json()
    if not data.get("success"):
        raise ValueError("Hypixel API reported failure (success=false)")
    return data


def fetch_flips(category_map, overrides, bazaar_data):
    """Raw bazaar response -> every profitable flip (list of dicts)."""
    products = bazaar_data.get("products", {})
    flips = []

    for product_id, info in products.items():
        quick_status = info.get("quick_status", {})

        buy_price = quick_status.get("buyPrice", 0)     # instant-buy price (top sell offer)
        sell_price = quick_status.get("sellPrice", 0)    # instant-sell price (top buy order)

        # buyVolume/sellVolume = order-book DEPTH (can be spoofed/stale), not
        # trade flow. buyMovingWeek/sellMovingWeek = units actually
        # transacted over 7d - the real liquidity metric, used below.
        buy_moving_week = quick_status.get("buyMovingWeek", 0)
        sell_moving_week = quick_status.get("sellMovingWeek", 0)
        avg_daily_volume = (buy_moving_week + sell_moving_week) / 7

        avg_daily_coin_volume = avg_daily_volume * sell_price  # coin-value turnover

        if sell_price <= 0 or buy_price <= 0 or avg_daily_coin_volume < MIN_DAILY_COIN_VOLUME:
            continue

        # Strategy: buy order at sell_price, instantly liquidate at buy_price minus tax.
        cost_per_item = sell_price
        post_tax_earnings = buy_price * (1 - BAZAAR_TAX)
        raw_profit = post_tax_earnings - cost_per_item
        if raw_profit <= 0:
            continue

        margin_percent = (raw_profit / cost_per_item) * 100

        # Bottleneck liquidity: need turnover on BOTH sides, so use the
        # thinner side's weekly volume, spread over a full week (168h).
        bottleneck_weekly_volume = min(buy_moving_week, sell_moving_week)
        hourly_volume = bottleneck_weekly_volume / (7 * 24)

        # Priority: manual override > Hypixel category > ID-pattern guess > Uncategorized.
        category = (
            overrides.get(product_id)
            or category_map.get(product_id)
            or infer_category_from_id(product_id)
            or "Uncategorized"
        )

        event_tags = tag_event_relevance(product_id)

        flips.append({
            "id": product_id,
            "item": product_id.replace("_", " ").title(),
            "category": category,
            "buy_order_at": round(cost_per_item, 1),
            "sell_offer_at": round(buy_price, 1),
            "profit": round(raw_profit, 1),
            "margin": round(margin_percent, 1),
            "volume": round(avg_daily_volume),
            "hourly_volume": hourly_volume,
            "cost_per_item": cost_per_item,
            "raw_buy_target": sell_price,   # top current buy order
            "raw_sell_target": buy_price,   # top current sell offer
            "extreme_margin": margin_percent >= EXTREME_MARGIN_THRESHOLD,
            "weekly_volume": round(bottleneck_weekly_volume),  # thinner-side 7d, the real risk signal
            "daily_coin_volume": round(avg_daily_coin_volume),
            "buy_moving_week": buy_moving_week,     # raw 7d totals, for fill/sell estimates
            "sell_moving_week": sell_moving_week,
            "event_tags": event_tags,               # keyword-matched seasonal-event tags
        })

    flips.sort(key=lambda x: x["profit"], reverse=True)
    return flips  # no cap - list is scrollable


# ---- Local 7-day price history (manipulation detection) --------------------
def load_price_history():
    return load_json(PRICE_HISTORY_PATH, {})


def record_and_prune_price_history(history, flips):
    """Append this fetch's raw prices to each item's local history, drop
    samples older than PRICE_HISTORY_MAX_AGE_DAYS, save, return updated history."""
    now = time.time()
    cutoff = now - PRICE_HISTORY_MAX_AGE_DAYS * 86400

    for f in flips:
        pid = f["id"]
        samples = history.get(pid, [])
        samples.append([now, f["raw_buy_target"], f["raw_sell_target"]])
        samples = [s for s in samples if s[0] >= cutoff]
        history[pid] = samples

    # Also prune items no longer live (e.g. dropped below volume floor).
    live_ids = {f["id"] for f in flips}
    for pid in list(history.keys()):
        if pid not in live_ids:
            trimmed = [s for s in history[pid] if s[0] >= cutoff]
            if trimmed:
                history[pid] = trimmed
            else:
                del history[pid]

    save_json(PRICE_HISTORY_PATH, history)
    return history


def apply_price_deviation_flags(flips, history):
    """Flag items whose current price has drifted far from their own local
    7d average - catches manipulation extreme_margin alone can miss. Fails
    open until PRICE_HISTORY_MIN_SAMPLES samples exist."""
    for f in flips:
        samples = history.get(f["id"], [])
        f["price_manipulation_suspect"] = False
        f["price_deviation_pct"] = None

        if len(samples) < PRICE_HISTORY_MIN_SAMPLES:
            continue

        avg_buy_target = sum(s[1] for s in samples) / len(samples)
        avg_sell_target = sum(s[2] for s in samples) / len(samples)

        dev_buy = (abs(f["raw_buy_target"] - avg_buy_target) / avg_buy_target * 100
                   if avg_buy_target > 0 else 0)
        dev_sell = (abs(f["raw_sell_target"] - avg_sell_target) / avg_sell_target * 100
                    if avg_sell_target > 0 else 0)
        worst_dev = max(dev_buy, dev_sell)

        f["price_deviation_pct"] = round(worst_dev, 1)
        if worst_dev >= PRICE_DEVIATION_THRESHOLD_PCT:
            f["price_manipulation_suspect"] = True
    return flips


def compute_buy_buffer_amount(raw_buy_target, raw_margin_percent, base_pct):
    """Coin amount added on top of the raw top-buy-order price (see
    DEFAULT_BUY_BUFFER_PCT comment above)."""
    margin_over = max(0.0, raw_margin_percent - BUY_BUFFER_MARGIN_SCALE_START_PCT)
    margin_bonus_pct = margin_over / 100.0 * BUY_BUFFER_MARGIN_SCALE_PER_100
    effective_pct = min(BUY_BUFFER_MAX_PCT, base_pct + margin_bonus_pct)
    pct_amount = raw_buy_target * (effective_pct / 100.0)
    return max(BUY_BUFFER_MIN_COINS, pct_amount)


def apply_buy_buffer(flips, buffer_pct):
    """Recompute buy_order_at/profit/margin with the buffer added. Drops
    items no longer profitable after buffering."""
    buffer_pct = max(0.0, buffer_pct)
    adjusted = []
    for f in flips:
        raw_buy_target = f["raw_buy_target"]
        raw_sell_target = f["raw_sell_target"]

        raw_post_tax = raw_sell_target * (1 - BAZAAR_TAX)
        raw_margin_percent = ((raw_post_tax - raw_buy_target) / raw_buy_target) * 100 \
            if raw_buy_target > 0 else 0.0

        buffer_amount = compute_buy_buffer_amount(raw_buy_target, raw_margin_percent, buffer_pct)
        cost_per_item = raw_buy_target + buffer_amount

        raw_profit = raw_post_tax - cost_per_item
        if raw_profit <= 0:
            continue
        margin_percent = (raw_profit / cost_per_item) * 100

        g = dict(f)
        g["buy_order_at"] = round(cost_per_item, 1)
        g["buy_buffer"] = round(buffer_amount, 1)
        g["cost_per_item"] = cost_per_item
        g["profit"] = round(raw_profit, 1)
        g["margin"] = round(margin_percent, 1)
        g["extreme_margin"] = margin_percent >= EXTREME_MARGIN_THRESHOLD
        adjusted.append(g)
    return adjusted


def compute_purse_metrics(flips, purse):
    """Attach purse-limited achievable units & hourly profit to each flip.
    "If you went all-in on this ONE item" figure for the Full List - NOT
    what the Overnight Plan allocates (see compute_portfolio)."""
    for f in flips:
        max_affordable = int(purse // f["cost_per_item"]) if f["cost_per_item"] > 0 else 0
        achievable_units = max(0, min(max_affordable, int(f["hourly_volume"])))
        f["achievable_units"] = achievable_units
        f["profit_hr"] = round(achievable_units * f["profit"], 1)
    return flips


def compute_portfolio(flips, purse, sleep_hours, target_n, min_daily_coin_volume):
    """Spread `purse` across up to `target_n` flips, sized to be fillable
    within `sleep_hours` (instead of one all-in pick).

    Filters out extreme-margin, price-deviation-suspect, and low-turnover
    items first (this plan runs unattended, so it's stricter than the
    general list). Remaining candidates are ranked by profit achievable
    within the sleep window under an even per-slot budget, then water-filled
    so a thin item can't hog a slot's worth of coins it can't actually place.

    Returns (portfolio_list, leftover_purse, risk_excluded_count). Each
    portfolio item gets "units", "coins", "profit_window" added.
    """
    sleep_hours = max(0.1, sleep_hours)
    target_n = max(1, int(target_n))
    min_daily_coin_volume = max(0, min_daily_coin_volume)

    base_pool = [f for f in flips if f.get("cost_per_item", 0) > 0 and f.get("hourly_volume", 0) > 0]
    candidates = [
        f for f in base_pool
        if not f.get("extreme_margin")
        and not f.get("price_manipulation_suspect")
        and f.get("daily_coin_volume", 0) >= min_daily_coin_volume
    ]
    risk_excluded_count = len(base_pool) - len(candidates)

    avg_slot_budget = purse / target_n if target_n > 0 else purse
    for f in candidates:
        window_units = int(f["hourly_volume"] * sleep_hours)
        budget_units = int(avg_slot_budget // f["cost_per_item"])
        f["_window_units"] = window_units
        f["_score_units"] = min(window_units, budget_units)
    candidates = [f for f in candidates if f["_score_units"] > 0]
    candidates.sort(key=lambda f: f["_score_units"] * f["profit"], reverse=True)

    portfolio = []
    remaining_purse = purse

    for f in candidates:
        if len(portfolio) >= target_n:
            break
        if remaining_purse < f["cost_per_item"]:
            continue

        remaining_slots = target_n - len(portfolio)
        liquidity_units = f["_window_units"]

        slot_budget = remaining_purse / remaining_slots
        target_coins = min(slot_budget, liquidity_units * f["cost_per_item"], remaining_purse)
        units = int(target_coins // f["cost_per_item"])
        units = min(units, liquidity_units)
        if units <= 0:
            continue

        coins = units * f["cost_per_item"]
        f["units"] = units
        f["coins"] = coins
        f["profit_window"] = round(units * f["profit"], 1)

        fill_hours, sell_hours = compute_fill_sell_hours(f, units)
        f["fill_hours"] = fill_hours
        f["sell_hours"] = sell_hours

        portfolio.append(f)
        remaining_purse -= coins

    return portfolio, remaining_purse, risk_excluded_count


def fmt_num(n):
    return f"{n:,.1f}"


def fmt_int(n):
    return f"{n:,}"


class VerticalScrollFrame(ttk.Frame):
    """Vertically-scrollable frame with mousewheel support, for the item list."""
    def __init__(self, parent, bg=BG_DARK):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.canvas.configure(yscrollcommand=self.vbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

    def _on_inner_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.window_id, width=event.width)

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

    def _unbind_wheel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class HorizontalScrollFrame(ttk.Frame):
    """Horizontally-scrollable frame, for the category pill bar."""
    def __init__(self, parent, bg=BG_DARK, height=44):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=bg, height=height, highlightthickness=0)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.hbar.set)

        self.canvas.pack(side="top", fill="x")
        self.hbar.pack(side="top", fill="x")
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

    def _on_inner_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.window_id, height=event.height)

    def _bind_wheel(self):
        self.canvas.bind_all("<Shift-MouseWheel>", self._on_mousewheel)

    def _unbind_wheel(self):
        self.canvas.unbind_all("<Shift-MouseWheel>")

    def _on_mousewheel(self, event):
        self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")


def hoverable(widget, base_bg, hover_bg, fg=None, hover_fg=None):
    """Simple hover-color behavior for a plain tk widget (Button/Label)."""
    def on_enter(_e):
        widget.configure(bg=hover_bg)
        if hover_fg is not None:
            widget.configure(fg=hover_fg)

    def on_leave(_e):
        widget.configure(bg=base_bg)
        if fg is not None:
            widget.configure(fg=fg)

    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)


class FlipCard(tk.Frame):
    """Collapsible box for one item. Header shows a quick summary; tapping
    expands a detail grid with every field."""
    def __init__(self, parent, flip, mode, on_set_category, sleep_hours=None, on_blacklist=None,
                 market_context=None):
        super().__init__(parent, bg=BORDER_SUBTLE)
        self.flip = flip
        self.mode = mode
        self.expanded = False
        self.on_set_category = on_set_category
        self.on_blacklist = on_blacklist
        # {"active_event_keys": set(...), "paul_discount_active": bool} - badges
        # against what's ACTUALLY live now, not just keyword matches.
        self.market_context = market_context or {}

        inner = tk.Frame(self, bg=BG_PANEL)
        inner.pack(fill="both", expand=True)

        stripe = tk.Frame(inner, bg=chip_color(flip["category"]), width=4)
        stripe.pack(side="left", fill="y")

        body_wrap = tk.Frame(inner, bg=BG_PANEL)
        body_wrap.pack(side="left", fill="both", expand=True)

        header = tk.Frame(body_wrap, bg=BG_PANEL, cursor="hand2")
        header.pack(fill="x")

        margin = flip["margin"]
        if flip.get("extreme_margin") or flip.get("price_manipulation_suspect"):
            badge_color = ACCENT_RED
        elif margin >= 50:
            badge_color = ACCENT_GREEN
        elif margin >= 15:
            badge_color = ACCENT_YELLOW
        else:
            badge_color = TEXT_DIM

        warn_flag = flip.get("extreme_margin") or flip.get("price_manipulation_suspect")
        name_text = ("\u26a0 " if warn_flag else "") + flip["item"]
        name_lbl = tk.Label(header, text=name_text, font=FONT_SUBHEAD, bg=BG_PANEL, fg=TEXT_MAIN)
        name_lbl.pack(side="left", padx=(10, 0), pady=5)
        cat_lbl = tk.Label(header, text=flip["category"], font=FONT_PILL, bg=BG_PANEL, fg=TEXT_DIM)
        cat_lbl.pack(side="left", padx=(8, 0), pady=5)

        # Only badge tags that are actually live now; other keyword matches
        # are informational-only and shown in the detail view instead.
        active_keys = self.market_context.get("active_event_keys", set())
        badge_widgets = []
        for key in flip.get("event_tags", []):
            if key == "dungeon_supply":
                show = self.market_context.get("paul_discount_active", False)
            else:
                show = key in active_keys
            if not show:
                continue
            badge_text, badge_color2 = EVENT_BADGE_STYLE.get(key, (key, ACCENT))
            badge_lbl = tk.Label(header, text=badge_text, font=FONT_PILL, bg=BG_PANEL, fg=badge_color2)
            badge_lbl.pack(side="left", padx=(8, 0), pady=5)
            badge_widgets.append(badge_lbl)

        self.arrow_lbl = tk.Label(header, text="\u25b6", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_FAINT)
        self.arrow_lbl.pack(side="right", padx=(0, 10), pady=5)

        if mode == "portfolio":
            quick_text = (f"{flip['units']:,} units \u00b7 {flip['coins']:,.0f} coins \u00b7 "
                           f"+{flip['profit_window']:,.0f} expected")
        else:
            quick_text = f"{margin:.1f}% margin \u00b7 {fmt_num(flip.get('profit_hr', 0))}/hr"
        quick_lbl = tk.Label(header, text=quick_text, font=FONT_MAIN, bg=BG_PANEL, fg=badge_color)
        quick_lbl.pack(side="right", padx=(0, 10), pady=5)


        self.detail = None
        self._sleep_hours_for_detail = sleep_hours

        toggle_widgets = [header, name_lbl, cat_lbl, quick_lbl, self.arrow_lbl] + badge_widgets
        for w in toggle_widgets:
            w.bind("<Button-1>", self.toggle)

        def on_enter(_e):
            for w in toggle_widgets:
                w.configure(bg=BG_CARD_HOVER)

        def on_leave(_e):
            for w in toggle_widgets:
                w.configure(bg=BG_PANEL)

        header.bind("<Enter>", on_enter)
        header.bind("<Leave>", on_leave)

    def _build_detail(self, sleep_hours):
        flip = self.flip
        margin = flip["margin"]

        rows = []
        for key, label in DETAIL_FIELDS:
            value = flip[key]
            if key == "margin":
                text = f"{value:.1f}%"
            elif key in ("volume", "weekly_volume"):
                text = fmt_int(value)
            else:
                text = fmt_num(value)
            rows.append((label, text))

        if flip.get("price_deviation_pct") is not None:
            rows.append(("7d Price Deviation", f"{flip['price_deviation_pct']:.1f}%"))

        if self.mode == "portfolio":
            rows.append(("Units to Buy", f"{flip['units']:,}"))
            rows.append(("Coins to Invest", f"{flip['coins']:,.0f}"))
            rows.append((f"Expected Profit ({sleep_hours:g}h)", f"{flip['profit_window']:,.0f}"))
            rows.append(("Est. Time to Fill Buy Order", fmt_hours(flip.get("fill_hours"))))
            rows.append(("Est. Time to Sell", fmt_hours(flip.get("sell_hours"))))
        else:
            rows.append(("Achievable Units (this purse)", f"{flip.get('achievable_units', 0):,}"))
            rows.append(("Profit/hr (Purse)", fmt_num(flip.get("profit_hr", 0))))
            fh, sh = compute_fill_sell_hours(flip, max(1, flip.get("achievable_units", 0)))
            rows.append(("Est. Time to Fill Buy Order", fmt_hours(fh)))
            rows.append(("Est. Time to Sell", fmt_hours(sh)))

        grid = tk.Frame(self.detail, bg=BG_PANEL_RAISED)
        grid.pack(fill="x", padx=16, pady=(8, 2))
        for i, (label, text) in enumerate(rows):
            r, c = divmod(i, 2)
            cell = tk.Frame(grid, bg=BG_PANEL_RAISED)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 28), pady=2)
            tk.Label(cell, text=label + ":", font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_DIM).pack(side="left")
            tk.Label(cell, text=" " + text, font=FONT_BOLD, bg=BG_PANEL_RAISED, fg=TEXT_MAIN).pack(side="left")

        if flip.get("extreme_margin"):
            tk.Label(self.detail,
                     text=("\u26a0 Unusually high margin - often a thin/stale order book that's "
                           "already moved by the time you check in-game. Verify before trusting."),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=ACCENT_RED,
                     wraplength=900, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        if flip.get("price_manipulation_suspect"):
            tk.Label(self.detail,
                     text=(f"\u26a0 Current price is {flip['price_deviation_pct']:.0f}% off its own "
                           f"7-day local average - possible manipulation or a stale/spoofed order, "
                           f"verify in-game before trusting."),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=ACCENT_RED,
                     wraplength=900, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        # Show why an item is tagged even if the event isn't live right now,
        # for planning ahead (header badge only shows currently-live tags).
        if flip.get("event_tags"):
            active_keys = self.market_context.get("active_event_keys", set())
            lines = []
            for key in flip["event_tags"]:
                badge_text, _ = EVENT_BADGE_STYLE.get(key, (key, ACCENT))
                if key == "dungeon_supply":
                    live = self.market_context.get("paul_discount_active", False)
                    note = ("Paul's Marauder perk is active - dungeon chests 20% cheaper" if live
                            else "not currently affected - Paul/Marauder isn't in office")
                else:
                    live = key in active_keys
                    note = "currently live" if live else "not currently running"
                lines.append(f"{badge_text}: {note}")
            tk.Label(self.detail, text="Seasonal relevance:\n" + "\n".join(lines),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_DIM,
                     wraplength=900, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        btn_row = tk.Frame(self.detail, bg=BG_PANEL_RAISED)
        btn_row.pack(anchor="w", padx=16, pady=(2, 12))
        set_cat_btn = tk.Button(btn_row, text="Set Category", font=FONT_PILL, bg=BG_INPUT,
                                 fg=TEXT_MAIN, relief="flat", bd=0, padx=9, pady=4, cursor="hand2",
                                 command=lambda: self.on_set_category(flip["id"]))
        set_cat_btn.pack(side="left")
        hoverable(set_cat_btn, BG_INPUT, ACCENT_SOFT)

        if self.on_blacklist:
            bl_btn = tk.Button(btn_row, text="Blacklist Item", font=FONT_PILL, bg=BG_INPUT,
                                fg=ACCENT_RED, relief="flat", bd=0, padx=9, pady=4, cursor="hand2",
                                command=lambda: self.on_blacklist(flip["id"]))
            bl_btn.pack(side="left", padx=(8, 0))
            hoverable(bl_btn, BG_INPUT, "#4a2a2a")

    def toggle(self, _event=None):
        self.expanded = not self.expanded
        if self.expanded:
            if self.detail is None:
                self.detail = tk.Frame(self, bg=BG_PANEL_RAISED)
                self.detail.pack(fill="x")
                self._build_detail(self._sleep_hours_for_detail)
            else:
                self.detail.pack(fill="x")
            self.arrow_lbl.configure(text="\u25bc")
        else:
            if self.detail is not None:
                self.detail.pack_forget()
            self.arrow_lbl.configure(text="\u25b6")


class CategoryDialog(tk.Toplevel):
    """Small modal to manually set/reset an item's category."""
    def __init__(self, parent, item_name, current_category, existing_categories, on_save, on_reset):
        super().__init__(parent)
        self.title("Set Category")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        pad = {"padx": 18, "pady": 6}

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        tk.Label(self, text=item_name, font=FONT_HEAD, bg=BG_PANEL, fg=ACCENT).pack(anchor="w", **pad)
        tk.Label(self, text=f"Current category: {current_category}", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_DIM).pack(anchor="w", padx=18)

        tk.Label(self, text="New category (pick existing or type a new one):",
                 font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_MAIN).pack(anchor="w", **pad)

        self.value_var = tk.StringVar(value=current_category)
        combo = ttk.Combobox(self, textvariable=self.value_var,
                              values=sorted(existing_categories), width=30)
        combo.pack(padx=18, pady=(0, 14))
        combo.focus_set()

        btn_row = tk.Frame(self, bg=BG_PANEL)
        btn_row.pack(pady=(0, 16), padx=18, fill="x")

        def do_save():
            value = self.value_var.get().strip()
            if value:
                on_save(value)
            self.destroy()

        def do_reset():
            on_reset()
            self.destroy()

        ttk.Button(btn_row, text="Save", command=do_save).pack(side="left")
        ttk.Button(btn_row, text="Reset to Auto-Detected", command=do_reset).pack(side="left", padx=8)
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="right")


class AddCategoryDialog(tk.Toplevel):
    """Tiny modal for typing a brand-new category name."""
    def __init__(self, parent, on_add):
        super().__init__(parent)
        self.title("New Category")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        tk.Label(self, text="New category name", font=FONT_HEAD, bg=BG_PANEL, fg=ACCENT).pack(
            anchor="w", padx=18, pady=(12, 6))

        self.value_var = tk.StringVar()
        entry = ttk.Entry(self, textvariable=self.value_var, width=28)
        entry.pack(padx=18, pady=(0, 14))
        entry.focus_set()

        btn_row = tk.Frame(self, bg=BG_PANEL)
        btn_row.pack(pady=(0, 16), padx=18, fill="x")

        def do_add():
            value = self.value_var.get().strip()
            if value:
                on_add(value)
            self.destroy()

        entry.bind("<Return>", lambda e: do_add())
        ttk.Button(btn_row, text="Add", command=do_add).pack(side="left")
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="right")


class ManageCategoriesDialog(tk.Toplevel):
    """List/rename/delete/add categories, all from one place."""
    def __init__(self, parent, categories, item_counts, on_add, on_rename, on_delete):
        super().__init__(parent)
        self.title("Manage Categories")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.on_add = on_add
        self.on_rename = on_rename
        self.on_delete = on_delete
        self.item_counts = item_counts

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        header = tk.Frame(self, bg=BG_PANEL)
        header.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(header, text="Manage Categories", font=FONT_HEAD, bg=BG_PANEL, fg=ACCENT).pack(side="left")

        add_row = tk.Frame(self, bg=BG_PANEL)
        add_row.pack(fill="x", padx=18, pady=(0, 10))
        self.new_var = tk.StringVar()
        entry = ttk.Entry(add_row, textvariable=self.new_var, width=24)
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self._add())
        ttk.Button(add_row, text="+ Add", command=self._add).pack(side="left", padx=6)

        list_wrap = tk.Frame(self, bg=BG_PANEL_RAISED, highlightbackground=BORDER_SUBTLE,
                              highlightthickness=1)
        list_wrap.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        canvas = tk.Canvas(list_wrap, bg=BG_PANEL_RAISED, highlightthickness=0,
                            width=360, height=280)
        scrollbar = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
        self.rows_frame = tk.Frame(canvas, bg=BG_PANEL_RAISED)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.window_id = canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(self.window_id, width=e.width))

        self._populate(categories)

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 16))

    def _add(self):
        value = self.new_var.get().strip()
        if value:
            self.on_add(value)
        self.destroy()

    def _populate(self, categories):
        for i, cat in enumerate(sorted(categories)):
            row_bg = BG_PANEL if i % 2 == 0 else BG_PANEL_RAISED
            row = tk.Frame(self.rows_frame, bg=row_bg)
            row.pack(fill="x")

            dot = tk.Canvas(row, width=10, height=10, bg=row_bg, highlightthickness=0)
            dot.create_oval(1, 1, 9, 9, fill=chip_color(cat), outline="")
            dot.pack(side="left", padx=(10, 8), pady=8)

            count = self.item_counts.get(cat, 0)
            label_text = f"{cat}  ({count} item{'s' if count != 1 else ''})"
            tk.Label(row, text=label_text, font=FONT_MAIN, bg=row_bg, fg=TEXT_MAIN).pack(
                side="left", pady=8)

            btn_frame = tk.Frame(row, bg=row_bg)
            btn_frame.pack(side="right", padx=8, pady=6)

            rename_btn = tk.Button(btn_frame, text="Rename", font=FONT_PILL, bg=BG_INPUT,
                                    fg=TEXT_MAIN, relief="flat", bd=0, padx=8, pady=3,
                                    cursor="hand2", command=lambda c=cat: self._rename(c))
            rename_btn.pack(side="left", padx=(0, 4))
            hoverable(rename_btn, BG_INPUT, ACCENT_SOFT)

            if cat != "Uncategorized":
                del_btn = tk.Button(btn_frame, text="Delete", font=FONT_PILL, bg=BG_INPUT,
                                     fg=ACCENT_RED, relief="flat", bd=0, padx=8, pady=3,
                                     cursor="hand2", command=lambda c=cat: self._delete(c))
                del_btn.pack(side="left")
                hoverable(del_btn, BG_INPUT, "#4a2a2a")

    def _rename(self, cat):
        def on_add(new_name):
            self.on_rename(cat, new_name)
            self.destroy()
        AddCategoryDialog(self, on_add)

    def _delete(self, cat):
        if messagebox.askyesno(
                "Delete category",
                f'Delete "{cat}"? Items in it will move to "Uncategorized".'):
            self.on_delete(cat)
            self.destroy()


class SettingsDialog(tk.Toplevel):
    """Trading parameters, theme accent color, auto-refresh cadence, and the
    item blacklist. Trading fields bind directly to the app's own StringVars."""
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        outer = tk.Frame(self, bg=BG_PANEL)
        outer.pack(fill="both", expand=True, padx=20, pady=16)

        tk.Label(outer, text="Settings", font=FONT_HEAD, bg=BG_PANEL, fg=ACCENT).pack(anchor="w", pady=(0, 12))

        # --- Trading Parameters ---
        self._section(outer, "Trading Parameters")
        params = tk.Frame(outer, bg=BG_PANEL)
        params.pack(fill="x", pady=(0, 14))
        self._param_row(params, "Purse (coins):", app.purse_var)
        self._param_row(params, "Sleep Hours:", app.sleep_hours_var)
        self._param_row(params, "Spread (# items):", app.spread_var)
        self._param_row(params, "Min $Vol/day:", app.risk_floor_var)
        self._param_row(params, "Buy Buffer %:", app.buy_buffer_var)

        # --- Appearance ---
        self._section(outer, "Appearance")
        appear = tk.Frame(outer, bg=BG_PANEL)
        appear.pack(fill="x", pady=(0, 14))
        self.accent_var = tk.StringVar(value=app.settings.get("accent_color", ACCENT))

        tk.Label(appear, text="Accent color:", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM).pack(anchor="w")
        swatch_row = tk.Frame(appear, bg=BG_PANEL)
        swatch_row.pack(anchor="w", pady=(4, 6))
        for preset in ACCENT_COLOR_PRESETS:
            sw = tk.Label(swatch_row, bg=preset, width=2, height=1, relief="flat", cursor="hand2",
                          highlightthickness=2, highlightbackground=BG_PANEL)
            sw.pack(side="left", padx=2)
            sw.bind("<Button-1>", lambda e, c=preset: self._pick_accent(c))

        current_row = tk.Frame(appear, bg=BG_PANEL)
        current_row.pack(anchor="w")
        self.current_swatch = tk.Label(current_row, text="  Current  ", bg=self.accent_var.get(),
                                        fg="#191a24", font=FONT_PILL)
        self.current_swatch.pack(side="left")
        custom_btn = tk.Button(current_row, text="Custom Color \u2022 Color Wheel...", font=FONT_PILL,
                                bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", bd=0, padx=8, pady=4,
                                cursor="hand2", command=self._pick_custom_accent)
        custom_btn.pack(side="left", padx=(8, 0))
        hoverable(custom_btn, BG_INPUT, ACCENT_SOFT)
        tk.Label(appear, text="Theme changes apply the next time you start the app.",
                 font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_FAINT).pack(anchor="w", pady=(6, 0))

        # --- Auto-Refresh ---
        self._section(outer, "Auto-Refresh")
        auto = tk.Frame(outer, bg=BG_PANEL)
        auto.pack(fill="x", pady=(0, 14))
        self.auto_enabled_var = tk.BooleanVar(value=app.auto_refresh_enabled)
        chk = tk.Checkbutton(auto, text="Automatically refresh market data", variable=self.auto_enabled_var,
                              font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_MAIN, selectcolor=BG_INPUT,
                              activebackground=BG_PANEL, activeforeground=TEXT_MAIN)
        chk.pack(anchor="w")
        interval_row = tk.Frame(auto, bg=BG_PANEL)
        interval_row.pack(anchor="w", pady=(4, 0))
        tk.Label(interval_row, text="Every", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_MAIN).pack(side="left")
        self.auto_minutes_var = tk.StringVar(value=str(app.auto_refresh_minutes))
        ttk.Entry(interval_row, textvariable=self.auto_minutes_var, width=5).pack(side="left", padx=6)
        tk.Label(interval_row, text=f"minute(s)  (minimum {MIN_AUTO_REFRESH_MINUTES})", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_MAIN).pack(side="left")

        # --- Blacklist ---
        self._section(outer, "Blacklisted Items")
        bl = tk.Frame(outer, bg=BG_PANEL)
        bl.pack(fill="x", pady=(0, 14))
        tk.Label(bl, text="Excluded from the Overnight Plan and Full List entirely - use this for "
                           "manipulated items the automatic flags haven't caught, or low-volume "
                           "items you just don't want to see.", font=FONT_MAIN, bg=BG_PANEL,
                 fg=TEXT_DIM, wraplength=420, justify="left").pack(anchor="w", pady=(0, 6))

        list_wrap = tk.Frame(bl, bg=BG_PANEL_RAISED, highlightbackground=BORDER_SUBTLE, highlightthickness=1)
        list_wrap.pack(fill="x")
        self.bl_listbox = tk.Listbox(list_wrap, bg=BG_PANEL_RAISED, fg=TEXT_MAIN, font=FONT_MAIN,
                                      height=6, selectbackground=ACCENT_SOFT, relief="flat",
                                      highlightthickness=0)
        self.bl_listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        bl_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.bl_listbox.yview)
        bl_scroll.pack(side="right", fill="y")
        self.bl_listbox.configure(yscrollcommand=bl_scroll.set)

        self._id_by_label = {}
        for pid in sorted(app.blacklist):
            item = next((f for f in app.all_flips if f["id"] == pid), None)
            label = item["item"] if item else pid.replace("_", " ").title()
            self._id_by_label[label] = pid
            self.bl_listbox.insert("end", label)

        bl_btn_row = tk.Frame(bl, bg=BG_PANEL)
        bl_btn_row.pack(fill="x", pady=(6, 0))
        remove_btn = tk.Button(bl_btn_row, text="Remove Selected", font=FONT_PILL, bg=BG_INPUT,
                                fg=ACCENT_RED, relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                                command=self._remove_selected)
        remove_btn.pack(side="left")
        hoverable(remove_btn, BG_INPUT, "#4a2a2a")

        add_row = tk.Frame(bl, bg=BG_PANEL)
        add_row.pack(fill="x", pady=(8, 0))
        tk.Label(add_row, text="Add item:", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_MAIN).pack(side="left")
        self.add_item_var = tk.StringVar()
        item_names = sorted({f["item"] for f in app.all_flips})
        add_combo = ttk.Combobox(add_row, textvariable=self.add_item_var, values=item_names, width=28)
        add_combo.pack(side="left", padx=6)
        add_btn = ttk.Button(add_row, text="+ Add", style="Secondary.TButton", command=self._add_item)
        add_btn.pack(side="left")

        # --- Updates ---
        self._section(outer, "Updates")
        upd = tk.Frame(outer, bg=BG_PANEL)
        upd.pack(fill="x", pady=(0, 14))
        tk.Label(upd, text=f"Current version: v{APP_VERSION}", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_DIM).pack(anchor="w")
        check_btn = tk.Button(upd, text="Check for Updates", font=FONT_PILL, bg=BG_INPUT,
                               fg=TEXT_MAIN, relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                               command=lambda: self.app._check_for_update_async(silent=False))
        check_btn.pack(anchor="w", pady=(4, 0))
        hoverable(check_btn, BG_INPUT, ACCENT_SOFT)

        # --- Save/Cancel ---
        btn_row = tk.Frame(outer, bg=BG_PANEL)
        btn_row.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_row, text="Save", command=self._save).pack(side="left")
        ttk.Button(btn_row, text="Cancel", style="Secondary.TButton", command=self.destroy).pack(side="left", padx=8)

    def _section(self, parent, text):
        tk.Label(parent, text=text, font=FONT_SUBHEAD, bg=BG_PANEL, fg=TEXT_MAIN).pack(anchor="w", pady=(2, 6))

    def _param_row(self, parent, label, var):
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM, width=16, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=16).pack(side="left")

    def _pick_accent(self, hex_color):
        self.accent_var.set(hex_color)
        self.current_swatch.configure(bg=hex_color)

    def _pick_custom_accent(self):
        _rgb, hex_color = colorchooser.askcolor(color=self.accent_var.get(),
                                                 title="Pick accent color", parent=self)
        if hex_color:
            self._pick_accent(hex_color)

    def _remove_selected(self):
        for i in reversed(self.bl_listbox.curselection()):
            self.bl_listbox.delete(i)

    def _add_item(self):
        name = self.add_item_var.get().strip()
        if not name:
            return
        match = next((f for f in self.app.all_flips if f["item"].lower() == name.lower()), None)
        pid = match["id"] if match else name.upper().replace(" ", "_")
        label = match["item"] if match else name
        if label not in self._id_by_label:
            self._id_by_label[label] = pid
            self.bl_listbox.insert("end", label)
        self.add_item_var.set("")

    def _save(self):
        try:
            minutes = max(MIN_AUTO_REFRESH_MINUTES, float(self.auto_minutes_var.get()))
        except ValueError:
            messagebox.showwarning("Invalid interval", "Enter a number for the auto-refresh interval.")
            return

        blacklist_ids = {self._id_by_label[self.bl_listbox.get(i)] for i in range(self.bl_listbox.size())}

        self.app.auto_refresh_enabled = self.auto_enabled_var.get()
        self.app.auto_refresh_minutes = minutes
        self.app.blacklist = blacklist_ids
        save_json(BLACKLIST_PATH, sorted(blacklist_ids))

        self.app.settings.update({
            "accent_color": self.accent_var.get(),
            "auto_refresh_enabled": self.app.auto_refresh_enabled,
            "auto_refresh_minutes": self.app.auto_refresh_minutes,
        })
        save_json(SETTINGS_PATH, self.app.settings)

        self.app._schedule_auto_refresh()
        self.app.recompute_and_render()
        self.destroy()


class BazaarFlipperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hypixel Skyblock Live Bazaar Tracker")
        self.geometry("1240x700")
        self.minsize(900, 480)
        self.configure(bg=BG_DARK)

        self.all_flips = []
        self.category_map = {}
        self.overrides = load_json(OVERRIDES_PATH, {})
        self.settings = load_json(SETTINGS_PATH, {})
        self.custom_categories = set(load_json(CUSTOM_CATEGORIES_PATH, []))
        self.price_history = load_price_history()
        self.blacklist = set(load_json(BLACKLIST_PATH, []))

        # Mayor/election context, seeded from the last-known cache so the UI
        # has something to show before the first live fetch completes.
        self.mayor_info = load_json(MAYOR_CACHE_PATH, {})
        self.active_festivals = compute_active_festivals(self.mayor_info)
        self.paul_discount_active = paul_dungeon_discount_active(self.mayor_info)
        self.jerry_status = jerry_workshop_status()
        self._recompute_active_event_keys()

        self.auto_refresh_enabled = bool(self.settings.get("auto_refresh_enabled", DEFAULT_AUTO_REFRESH_ENABLED))
        try:
            self.auto_refresh_minutes = max(MIN_AUTO_REFRESH_MINUTES,
                                             float(self.settings.get("auto_refresh_minutes", DEFAULT_AUTO_REFRESH_MINUTES)))
        except (TypeError, ValueError):
            self.auto_refresh_minutes = DEFAULT_AUTO_REFRESH_MINUTES
        self._auto_refresh_after_id = None

        self.sort_key = "profit_hr"
        self.sort_reverse = True
        self.view_mode = "dashboard"      # "dashboard" (Overnight Plan) or "full"
        self.category_var = tk.StringVar(value=ALL_CATEGORIES)
        self.category_buttons = {}

        self._search_after_id = None
        self._full_list_page_size = FULL_LIST_PAGE_SIZE
        self._full_list_shown = FULL_LIST_PAGE_SIZE
        self._full_list_cache_key = None

        self._setup_style()
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()
        self._schedule_auto_refresh()
        self._check_for_update_async(silent=True)

    # -- setup ----------------------------------------------------------
    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=BG_DARK)
        style.configure("Panel.TFrame", background=BG_PANEL_RAISED)
        style.configure("TopBar.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG_DARK, foreground=TEXT_MAIN, font=FONT_MAIN)
        style.configure("Panel.TLabel", background=BG_PANEL_RAISED, foreground=TEXT_MAIN, font=FONT_MAIN)
        style.configure("TopBar.TLabel", background=BG_PANEL, foreground=TEXT_MAIN, font=FONT_MAIN)
        style.configure("Dim.TLabel", background=BG_DARK, foreground=TEXT_DIM, font=FONT_MAIN)
        style.configure("TopBarDim.TLabel", background=BG_PANEL, foreground=TEXT_DIM, font=FONT_MAIN)
        style.configure("Head.TLabel", background=BG_PANEL_RAISED, foreground=ACCENT, font=FONT_HEAD)

        style.configure("TButton", background=ACCENT, foreground="#191a24",
                         font=FONT_BOLD, padding=9, borderwidth=0)
        style.map("TButton", background=[("active", ACCENT_HOVER), ("disabled", "#4b4b5a")])

        style.configure("Secondary.TButton", background=BG_INPUT, foreground=TEXT_MAIN,
                         font=FONT_BOLD, padding=9, borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#3d3f5c")])

        style.configure("View.TButton", background="#2b2d3e", foreground=TEXT_MAIN,
                         font=FONT_BOLD, padding=(14, 9), borderwidth=0)
        style.map("View.TButton", background=[("active", "#3d3f5c")])
        style.configure("ViewActive.TButton", background=ACCENT, foreground="#191a24",
                         font=FONT_BOLD, padding=(14, 9), borderwidth=0)
        style.map("ViewActive.TButton", background=[("active", ACCENT_HOVER)])

        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=TEXT_MAIN,
                         insertcolor=TEXT_MAIN, padding=7, borderwidth=0)
        style.configure("TCombobox", fieldbackground=BG_INPUT, background=BG_INPUT,
                         foreground=TEXT_MAIN, arrowcolor=ACCENT, padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)])
        self.option_add("*TCombobox*Listbox.background", BG_INPUT)
        self.option_add("*TCombobox*Listbox.foreground", TEXT_MAIN)

        style.configure("Vertical.TScrollbar", background=BG_PANEL, troughcolor=BG_DARK,
                         arrowcolor=TEXT_DIM, borderwidth=0)
        style.configure("Horizontal.TScrollbar", background=BG_PANEL, troughcolor=BG_DARK,
                         arrowcolor=TEXT_DIM, borderwidth=0)

    # -- widgets ----------------------------------------------------------
    def _build_widgets(self):
        # Row 1: refresh + settings + manage categories + view toggle.
        # Trading-parameter fields live in the Settings dialog, not here -
        # keeps this row short enough to never overflow the window.
        top_bar_wrap = tk.Frame(self, bg=BG_DARK)
        top_bar_wrap.pack(fill="x")
        top_bar = ttk.Frame(top_bar_wrap, padding=(14, 14, 14, 14), style="TopBar.TFrame")
        top_bar.pack(fill="x")
        tk.Frame(top_bar_wrap, bg=ACCENT, height=2).pack(fill="x")
        self.top_bar_wrap = top_bar_wrap

        self.purse_var = tk.StringVar(value=self.settings.get("purse", "10000000"))
        self.sleep_hours_var = tk.StringVar(value=self.settings.get("sleep_hours", str(DEFAULT_SLEEP_HOURS)))
        self.spread_var = tk.StringVar(value=self.settings.get("spread_n", str(DEFAULT_SPREAD_N)))
        self.risk_floor_var = tk.StringVar(value=self.settings.get("risk_floor", str(DEFAULT_PLAN_MIN_DAILY_VOLUME)))
        self.buy_buffer_var = tk.StringVar(value=self.settings.get("buy_buffer_pct", str(DEFAULT_BUY_BUFFER_PCT)))

        self.refresh_btn = ttk.Button(top_bar, text="\u21bb  Refresh Market Data", command=self.refresh)
        self.refresh_btn.pack(side="left")

        ttk.Button(top_bar, text="\u2699 Settings", style="Secondary.TButton",
                   command=self.open_settings).pack(side="left", padx=(10, 0))

        ttk.Button(top_bar, text="\U0001F3F7 Manage Categories", style="Secondary.TButton",
                   command=self.open_manage_categories).pack(side="left", padx=(10, 0))

        view_frame = ttk.Frame(top_bar, style="TopBar.TFrame")
        view_frame.pack(side="right")
        self.dashboard_btn = ttk.Button(view_frame, text="\U0001F319 Overnight Plan",
                                         command=lambda: self.set_view("dashboard"))
        self.dashboard_btn.pack(side="left", padx=(0, 4))
        self.fulllist_btn = ttk.Button(view_frame, text="\u2261 Full List",
                                        command=lambda: self.set_view("full"))
        self.fulllist_btn.pack(side="left")

        # Row 2: search + sort (Full List) / category pills - Full List view only
        self.category_bar_wrap = ttk.Frame(self, padding=(14, 10, 14, 6))

        filter_row = ttk.Frame(self.category_bar_wrap)
        filter_row.pack(fill="x", pady=(0, 6))
        ttk.Label(filter_row, text="Search:").pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(filter_row, textvariable=self.search_var, width=18)
        search_entry.pack(side="left")
        search_entry.bind("<KeyRelease>", lambda e: self._on_search_key())

        ttk.Label(filter_row, text="Sort by:").pack(side="left", padx=(16, 6))
        self.sort_var = tk.StringVar(value=SORT_OPTIONS[0][0])
        sort_combo = ttk.Combobox(filter_row, textvariable=self.sort_var, state="readonly",
                                   values=[label for label, _ in SORT_OPTIONS], width=20)
        sort_combo.pack(side="left")
        sort_combo.bind("<<ComboboxSelected>>", lambda e: self._on_sort_change())

        self.sort_dir_btn = ttk.Button(filter_row, text="\u25bc Desc", style="Secondary.TButton",
                                        command=self._toggle_sort_dir)
        self.sort_dir_btn.pack(side="left", padx=6)

        self.category_scroll = HorizontalScrollFrame(self.category_bar_wrap)
        self.category_scroll.pack(fill="x")

        # Row 3: status
        status_bar = ttk.Frame(self, padding=(14, 0, 14, 8))
        status_bar.pack(fill="x")
        self.status_var = tk.StringVar(value="Loading...")
        ttk.Label(status_bar, textvariable=self.status_var, style="Dim.TLabel").pack(side="left")
        self.snapshot_age_var = tk.StringVar(value="")
        self.snapshot_age_lbl = ttk.Label(status_bar, textvariable=self.snapshot_age_var, style="Dim.TLabel")
        self.snapshot_age_lbl.pack(side="left", padx=(14, 0))

        self.events_var = tk.StringVar(value=self._events_status_text())
        ttk.Label(status_bar, textvariable=self.events_var, style="Dim.TLabel").pack(side="left", padx=(14, 0))

        self.update_available_var = tk.StringVar(value="")
        self.update_lbl = tk.Label(status_bar, textvariable=self.update_available_var, font=FONT_BOLD,
                                    bg=BG_DARK, fg=ACCENT_GREEN, cursor="hand2")
        self.update_lbl.bind("<Button-1>", self._on_update_click)
        # not packed until an update is actually found - see _show_update_available

        self.count_var = tk.StringVar(value="")
        ttk.Label(status_bar, textvariable=self.count_var, style="Dim.TLabel").pack(side="right")
        ttk.Label(status_bar, text="Tip: tap any item box below to expand its full details",
                  style="Dim.TLabel").pack(side="right", padx=16)

        # Summary card
        card_wrap = tk.Frame(self, bg=BG_DARK)
        card_wrap.pack(fill="x", padx=14, pady=(0, 12))
        tk.Frame(card_wrap, bg=ACCENT, width=4).pack(side="left", fill="y")
        self.card = ttk.Frame(card_wrap, style="Panel.TFrame", padding=16)
        self.card.pack(side="left", fill="both", expand=True)
        self.card_title = ttk.Label(self.card, text="Overnight Plan", style="Head.TLabel")
        self.card_title.pack(anchor="w")
        self.card_body = ttk.Label(self.card, text="Loading market data...", style="Panel.TLabel",
                                    wraplength=1100, justify="left")
        self.card_body.pack(anchor="w", pady=(6, 0))

        # Item box list
        list_frame = ttk.Frame(self, padding=(14, 0, 14, 14))
        list_frame.pack(fill="both", expand=True)
        self.cards_scroll = VerticalScrollFrame(list_frame)
        self.cards_scroll.pack(fill="both", expand=True)

        self._refresh_view_buttons()

    # -- view switching ----------------------------------------------------------
    def set_view(self, mode):
        self.view_mode = mode
        self._refresh_view_buttons()
        if mode == "full":
            self.category_bar_wrap.pack(fill="x", after=self.top_bar_wrap)
        else:
            self.category_bar_wrap.pack_forget()
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _refresh_view_buttons(self):
        self.dashboard_btn.configure(style="ViewActive.TButton" if self.view_mode == "dashboard" else "View.TButton")
        self.fulllist_btn.configure(style="ViewActive.TButton" if self.view_mode == "full" else "View.TButton")

    def get_all_categories(self):
        """Union of categories in use + custom ones (may have no items yet)."""
        in_use = {f["category"] for f in self.all_flips}
        return sorted(in_use | self.custom_categories)

    def _rebuild_category_bar(self):
        for child in self.category_scroll.inner.winfo_children():
            child.destroy()
        self.category_buttons = {}

        all_cats = [ALL_CATEGORIES] + self.get_all_categories()
        for cat in all_cats:
            active = (cat == self.category_var.get())
            base_bg = ACCENT if active else BG_INPUT
            fg = "#191a24" if active else TEXT_MAIN

            pill = tk.Frame(self.category_scroll.inner, bg=base_bg)
            pill.pack(side="left", padx=4, pady=4)

            inner_pad = tk.Frame(pill, bg=base_bg, cursor="hand2")
            inner_pad.pack()

            if cat != ALL_CATEGORIES:
                dot = tk.Canvas(inner_pad, width=8, height=8, bg=base_bg, highlightthickness=0)
                dot.create_oval(0, 0, 8, 8, fill=chip_color(cat), outline="")
                dot.pack(side="left", padx=(10, 6), pady=7)
                left_pad = 0
            else:
                left_pad = 12

            label = tk.Label(inner_pad, text=cat, font=FONT_PILL, bg=base_bg, fg=fg,
                              padx=left_pad if cat == ALL_CATEGORIES else 0, pady=7, cursor="hand2")
            label.pack(side="left", padx=(0, 12))

            for widget in (pill, inner_pad, label):
                widget.bind("<Button-1>", lambda e, c=cat: self.select_category(c))
            if not active:
                hoverable(label, base_bg, "#3a3c52", fg=fg, hover_fg=TEXT_MAIN)
                hoverable(inner_pad, base_bg, "#3a3c52")
                hoverable(pill, base_bg, "#3a3c52")

            self.category_buttons[cat] = pill

        add_pill = tk.Label(self.category_scroll.inner, text="+ New", font=FONT_PILL,
                             bg=BG_DARK, fg=ACCENT, padx=12, pady=7, cursor="hand2",
                             highlightbackground=ACCENT, highlightthickness=1)
        add_pill.pack(side="left", padx=(8, 4), pady=4)
        add_pill.bind("<Button-1>", lambda e: self.open_add_category())
        hoverable(add_pill, BG_DARK, ACCENT_SOFT, fg=ACCENT, hover_fg=ACCENT_HOVER)

    def select_category(self, cat):
        self.category_var.set(cat)
        self._rebuild_category_bar()
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    # -- settings -----------------------------------------------------------
    def open_settings(self):
        SettingsDialog(self)

    def _schedule_auto_refresh(self):
        """(Re)schedule the next auto-refresh; always cancels any pending
        one first so an interval/toggle change can't leave a stray timer."""
        if self._auto_refresh_after_id is not None:
            self.after_cancel(self._auto_refresh_after_id)
            self._auto_refresh_after_id = None
        if self.auto_refresh_enabled:
            interval_ms = int(max(MIN_AUTO_REFRESH_MINUTES, self.auto_refresh_minutes) * 60 * 1000)
            self._auto_refresh_after_id = self.after(interval_ms, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self.refresh()
        self._schedule_auto_refresh()

    # -- update checker -------------------------------------------------
    def _check_for_update_async(self, silent=False):
        """Background GitHub check so a slow/offline check never freezes the
        UI. silent=True (startup) says nothing unless a real update exists;
        silent=False (Settings button) always reports back."""
        def worker():
            try:
                available, latest, url, asset_url = check_for_update()
            except Exception:
                if not silent:
                    self.after(0, lambda: messagebox.showinfo(
                        "Update Check",
                        "Couldn't check for updates - check your connection, or that "
                        "GITHUB_REPO in the script points at a real repo with a published release."))
                return
            if available:
                self.after(0, lambda: self._show_update_available(latest, url, asset_url))
            elif not silent:
                self.after(0, lambda: messagebox.showinfo(
                    "Update Check", f"You're on the latest version (v{APP_VERSION})."))
        threading.Thread(target=worker, daemon=True).start()

    def _show_update_available(self, latest_version, release_url, asset_url):
        """Light up the clickable status-bar banner. If a .exe asset was
        found, clicking auto-installs it; otherwise opens the release page."""
        self._update_release_url = release_url
        self._update_asset_url = asset_url
        if asset_url:
            self.update_available_var.set(f"\u2b06 Update available: {latest_version} (click to install)")
        else:
            self.update_available_var.set(f"\u2b06 Update available: {latest_version} (click to download)")
        self.update_lbl.pack(side="left", padx=(14, 0))

    def _on_update_click(self, _event=None):
        """Prefer the auto-install path; fall back to opening the release page."""
        if getattr(self, "_update_asset_url", None):
            self._apply_update(self._update_asset_url)
        elif getattr(self, "_update_release_url", None):
            webbrowser.open(self._update_release_url)

    def _apply_update(self, download_url):
        """Download the release zip, extract to %TEMP%, then hand off to a
        batch script (see _launch_update_and_restart) that waits for this
        process to exit, mirrors the update over the install folder, and
        relaunches - a running .exe can't overwrite its own files while open.

        Only applies when frozen (packaged .exe); running the raw .py just
        opens the browser instead, since there's no installed folder."""
        if not getattr(sys, "frozen", False):
            webbrowser.open(self._update_release_url)
            return

        self.update_lbl.unbind("<Button-1>")
        self.update_available_var.set("\u2b07 Downloading update...")

        def worker():
            current_exe = sys.executable
            exe_name = os.path.basename(current_exe)

            work_dir = os.path.join(tempfile.gettempdir(), "BazaarFlipperUpdate")
            zip_path = os.path.join(work_dir, "update.zip")
            extract_dir = os.path.join(work_dir, "extracted")

            try:
                if os.path.exists(work_dir):
                    shutil.rmtree(work_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)

                with requests.get(download_url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    expected_size = int(r.headers.get("Content-Length", 0))
                    written = 0
                    with open(zip_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=262144):
                            if chunk:
                                f.write(chunk)
                                written += len(chunk)
                if expected_size and written != expected_size:
                    raise IOError(f"Incomplete download: got {written} of {expected_size} bytes")

                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(extract_dir)

                # zip may nest the app folder one level deep - search for
                # wherever the exe actually ended up.
                source_dir = None
                for root, _dirs, files in os.walk(extract_dir):
                    if exe_name in files:
                        source_dir = root
                        break
                if source_dir is None:
                    raise FileNotFoundError(
                        f"Couldn't find {exe_name} anywhere inside the downloaded update - "
                        f"the release zip may not contain a valid build.")
            except Exception as exc:
                shutil.rmtree(work_dir, ignore_errors=True)
                self.after(0, lambda: self._on_update_download_failed(exc))
                return
            self.after(0, lambda: self._launch_update_and_restart(current_exe, source_dir, work_dir))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_download_failed(self, exc):
        self.update_available_var.set("\u2b06 Update available (download failed, click to retry)")
        self.update_lbl.bind("<Button-1>", self._on_update_click)
        messagebox.showerror("Update failed", f"Couldn't download the update:\n{exc}")

    def _launch_update_and_restart(self, current_exe, source_dir, work_dir):
        """Write a batch script (in %TEMP%, outside the install folder) that
        waits for this exe to exit, robocopy /MIR's the update over the
        install folder, relaunches, and cleans up. Launched console-less so
        it survives after this process exits. Windows-only."""
        exe_dir = os.path.dirname(current_exe)
        exe_name = os.path.basename(current_exe)
        bat_path = os.path.join(work_dir, "_apply_update.bat")

        bat_contents = (
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /fi "imagename eq {exe_name}" | find /i "{exe_name}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "    timeout /t 1 /nobreak >nul\r\n"
            "    goto wait\r\n"
            ")\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'robocopy "{source_dir}" "{exe_dir}" /E /MIR /R:3 /W:1 >nul\r\n'
            f'start "" "{current_exe}"\r\n'
            f'rmdir /s /q "{work_dir}"\r\n'
            'del "%~f0"\r\n'
        )
        with open(bat_path, "w") as f:
            f.write(bat_contents)

        subprocess.Popen(["cmd", "/c", bat_path],
                          creationflags=subprocess.CREATE_NO_WINDOW,
                          close_fds=True)

        if self._auto_refresh_after_id is not None:
            self.after_cancel(self._auto_refresh_after_id)
        self.destroy()
        sys.exit(0)

    # -- category management (add / rename / delete) ------------------------
    def open_add_category(self):
        def on_add(name):
            self.custom_categories.add(name)
            save_json(CUSTOM_CATEGORIES_PATH, sorted(self.custom_categories))
            self._rebuild_category_bar()
        AddCategoryDialog(self, on_add)

    def open_manage_categories(self):
        counts = {}
        for f in self.all_flips:
            counts[f["category"]] = counts.get(f["category"], 0) + 1

        def on_add(name):
            self.custom_categories.add(name)
            save_json(CUSTOM_CATEGORIES_PATH, sorted(self.custom_categories))
            self._rebuild_category_bar()
            self.recompute_and_render()

        def on_rename(old_name, new_name):
            if old_name == new_name:
                return
            self.custom_categories.discard(old_name)
            self.custom_categories.add(new_name)
            save_json(CUSTOM_CATEGORIES_PATH, sorted(self.custom_categories))
            for product_id, cat in list(self.overrides.items()):
                if cat == old_name:
                    self.overrides[product_id] = new_name
            for f in self.all_flips:
                if f["category"] == old_name:
                    f["category"] = new_name
                    self.overrides[f["id"]] = new_name
            save_json(OVERRIDES_PATH, self.overrides)
            if self.category_var.get() == old_name:
                self.category_var.set(new_name)
            self._rebuild_category_bar()
            self.recompute_and_render()

        def on_delete(name):
            self.custom_categories.discard(name)
            save_json(CUSTOM_CATEGORIES_PATH, sorted(self.custom_categories))
            for product_id, cat in list(self.overrides.items()):
                if cat == name:
                    self.overrides[product_id] = "Uncategorized"
            for f in self.all_flips:
                if f["category"] == name:
                    f["category"] = "Uncategorized"
                    self.overrides[f["id"]] = "Uncategorized"
            save_json(OVERRIDES_PATH, self.overrides)
            if self.category_var.get() == name:
                self.category_var.set(ALL_CATEGORIES)
            self._rebuild_category_bar()
            self.recompute_and_render()

        ManageCategoriesDialog(self, self.get_all_categories(), counts, on_add, on_rename, on_delete)

    # -- manual categorization (per-item, via "Set Category" in a box) --
    def open_category_dialog(self, product_id):
        flip = next((f for f in self.all_flips if f["id"] == product_id), None)
        if not flip:
            return
        existing_categories = sorted(set(self.get_all_categories()))

        def on_save(new_category):
            self.overrides[product_id] = new_category
            save_json(OVERRIDES_PATH, self.overrides)
            self._set_flip_category(product_id, new_category)
            self._rebuild_category_bar()
            self.recompute_and_render()

        def on_reset():
            self.overrides.pop(product_id, None)
            save_json(OVERRIDES_PATH, self.overrides)
            fallback = (self.category_map.get(product_id)
                        or infer_category_from_id(product_id)
                        or "Uncategorized")
            self._set_flip_category(product_id, fallback)
            self._rebuild_category_bar()
            self.recompute_and_render()

        CategoryDialog(self, flip["item"], flip["category"], existing_categories, on_save, on_reset)

    def _set_flip_category(self, product_id, category):
        for f in self.all_flips:
            if f["id"] == product_id:
                f["category"] = category
                break

    # -- blacklist ------------------------------------------------------
    def blacklist_item(self, product_id):
        self.blacklist.add(product_id)
        save_json(BLACKLIST_PATH, sorted(self.blacklist))
        self.recompute_and_render()

    # -- seasonal events / mayor -----------------------------------------
    def _recompute_active_event_keys(self):
        """Set of event_keys that are ACTUALLY live right now - what
        FlipCard badges/filters against. dungeon_supply is excluded here;
        it's driven directly by paul_discount_active, not a festival window."""
        keys = {f["event_key"] for f in self.active_festivals if f["active_now"]}
        if self.jerry_status.get("active"):
            keys.add("jerry_workshop")
        self.active_event_keys = keys

    def _market_context(self):
        return {
            "active_event_keys": getattr(self, "active_event_keys", set()),
            "paul_discount_active": self.paul_discount_active,
        }

    def _events_status_text(self):
        """Human-readable "what's live now" summary for the status bar."""
        parts = []
        mayor_name = self.mayor_info.get("name")
        if mayor_name:
            parts.append(f"Mayor: {mayor_name}")
        live_labels = [f["label"] for f in self.active_festivals if f["active_now"]]
        if self.jerry_status.get("active"):
            live_labels.append("Jerry's Workshop")
        if self.paul_discount_active:
            live_labels.append("Paul \u201320% Chests")
        if live_labels:
            parts.append("Active: " + ", ".join(live_labels))
        return "  \u00b7  ".join(parts)

    # -- data flow ----------------------------------------------------------
    def refresh(self):
        self.refresh_btn.state(["disabled"])
        self.status_var.set("Fetching live bazaar data...")
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            category_map = fetch_item_categories()
            bazaar_data = fetch_bazaar_data()
            flips = fetch_flips(category_map, self.overrides, bazaar_data)
            self.price_history = record_and_prune_price_history(self.price_history, flips)
            flips = apply_price_deviation_flags(flips, self.price_history)
            snapshot_ms = bazaar_data.get("lastUpdated", 0)
        except Exception as exc:
            self.after(0, self._on_fetch_error, exc)
            return

        # Election data is a separate endpoint and shouldn't block the
        # bazaar refresh everything else depends on if it fails.
        mayor_info = None
        try:
            mayor_info = fetch_mayor_info()
            save_json(MAYOR_CACHE_PATH, mayor_info)
        except Exception:
            pass

        self.after(0, self._on_fetch_success, category_map, flips, snapshot_ms, mayor_info)

    def _on_fetch_success(self, category_map, flips, snapshot_ms, mayor_info=None):
        self.category_map = category_map
        self.all_flips = flips
        self.status_var.set("Bazaar data loaded successfully")
        self.refresh_btn.state(["!disabled"])

        if mayor_info:
            self.mayor_info = mayor_info
        self.active_festivals = compute_active_festivals(self.mayor_info)
        self.paul_discount_active = paul_dungeon_discount_active(self.mayor_info)
        self.jerry_status = jerry_workshop_status()
        self._recompute_active_event_keys()
        self.events_var.set(self._events_status_text())

        # snapshot_ms is Hypixel's own capture time, not our fetch time.
        self.last_snapshot_ms = snapshot_ms
        self.last_snapshot_local_ref = time.time()
        self._tick_snapshot_age()

        self._rebuild_category_bar()
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _tick_snapshot_age(self):
        """Keep the 'snapshot: Xs old' label live between fetches."""
        if getattr(self, "last_snapshot_ms", 0):
            elapsed_since_capture = time.time() - (self.last_snapshot_ms / 1000.0)
            elapsed_since_capture = max(0, elapsed_since_capture)
            if elapsed_since_capture < 90:
                age_text = f"Hypixel snapshot: {elapsed_since_capture:.0f}s old"
            else:
                age_text = f"Hypixel snapshot: {elapsed_since_capture / 60:.1f}m old"
            color = ACCENT_RED if elapsed_since_capture >= STALE_DATA_WARNING_SECONDS else TEXT_DIM
            self.snapshot_age_var.set(age_text)
            self.snapshot_age_lbl.configure(foreground=color)
        self.after(1000, self._tick_snapshot_age)

    def _on_fetch_error(self, exc):
        self.status_var.set("Error fetching data")
        self.refresh_btn.state(["!disabled"])
        messagebox.showerror("Fetch failed", str(exc))

    def _get_purse(self):
        try:
            return max(0.0, float(self.purse_var.get().replace(",", "")))
        except ValueError:
            messagebox.showwarning("Invalid purse", "Enter a number for your purse (no letters).")
            return 0.0

    def _get_sleep_hours(self):
        try:
            return max(0.5, float(self.sleep_hours_var.get().replace(",", "")))
        except ValueError:
            messagebox.showwarning("Invalid sleep hours", "Enter a number for sleep hours.")
            return DEFAULT_SLEEP_HOURS

    def _get_spread_n(self):
        try:
            return max(1, int(float(self.spread_var.get().replace(",", ""))))
        except ValueError:
            messagebox.showwarning("Invalid spread", "Enter a whole number for how many items to spread across.")
            return DEFAULT_SPREAD_N

    def _get_risk_floor(self):
        try:
            return max(0.0, float(self.risk_floor_var.get().replace(",", "")))
        except ValueError:
            messagebox.showwarning("Invalid Min $Vol/day",
                                    "Enter a number for the minimum daily coin volume.")
            return DEFAULT_PLAN_MIN_DAILY_VOLUME

    def _get_buy_buffer_pct(self):
        try:
            return max(0.0, float(self.buy_buffer_var.get().replace(",", "").replace("%", "")))
        except ValueError:
            messagebox.showwarning("Invalid Buy Buffer %",
                                    "Enter a number for the buy-order buffer percentage.")
            return DEFAULT_BUY_BUFFER_PCT

    def _filtered_flips(self):
        # Blacklisted items are excluded everywhere.
        flips = [f for f in self.all_flips if f["id"] not in self.blacklist]

        if self.view_mode == "full":
            selected = self.category_var.get()
            if selected not in (ALL_CATEGORIES, "", None):
                flips = [f for f in flips if f["category"] == selected]

        query = self.search_var.get().strip().lower()
        if query:
            flips = [f for f in flips if query in f["item"].lower() or query in f["category"].lower()]

        return flips

    # -- search debounce -----------------------------------------------------
    def _on_search_key(self):
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._full_list_shown = self._full_list_page_size
        self._search_after_id = self.after(250, self._run_debounced_search)

    def _run_debounced_search(self):
        self._search_after_id = None
        self.recompute_and_render()

    def recompute_and_render(self):
        if not self.all_flips:
            return
        purse = self._get_purse()
        buffer_pct = self._get_buy_buffer_pct()
        buffered = apply_buy_buffer(self._filtered_flips(), buffer_pct)
        flips = compute_purse_metrics(buffered, purse)
        self._render_cards(flips, purse)

    # -- sorting (Full List only) --------------------------------------------
    def _on_sort_change(self):
        label = self.sort_var.get()
        key = next((k for l, k in SORT_OPTIONS if l == label), "profit_hr")
        self.sort_key = key
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _toggle_sort_dir(self):
        self.sort_reverse = not self.sort_reverse
        self.sort_dir_btn.configure(text="\u25bc Desc" if self.sort_reverse else "\u25b2 Asc")
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _show_more_full_list(self):
        self._full_list_shown += self._full_list_page_size
        self.recompute_and_render()

    # -- rendering ----------------------------------------------------------
    def _render_cards(self, flips, purse):
        for child in self.cards_scroll.inner.winfo_children():
            child.destroy()

        if self.view_mode == "dashboard":
            sleep_hours = self._get_sleep_hours()
            target_n = self._get_spread_n()
            risk_floor = self._get_risk_floor()
            portfolio, leftover, risk_excluded = compute_portfolio(
                list(flips), purse, sleep_hours, target_n, risk_floor)
            rows = portfolio
            visible_rows = rows
            more_remaining = 0
            mode = "portfolio"

            total_invested = sum(f["coins"] for f in portfolio)
            total_profit = sum(f["profit_window"] for f in portfolio)
            self.card_title.configure(
                text=f"Overnight Plan \u2014 {len(portfolio)} item(s), {sleep_hours:g}h horizon")

            risk_note = (
                f" Skipped {risk_excluded} item(s) flagged as extreme-margin, price-deviation "
                f"suspects, or under {risk_floor:,.0f} coins/day in turnover, to keep this plan "
                f"safer to leave unattended."
                if risk_excluded else ""
            )
            if portfolio:
                self.card_body.configure(text=(
                    f"Spreading {total_invested:,.0f} of your {purse:,.0f} coin purse across "
                    f"{len(portfolio)} liquid flips ({leftover:,.0f} coins left uninvested \u2014 not "
                    f"enough safe liquidity to place elsewhere right now).\n"
                    f"Estimated profit over the next {sleep_hours:g}h: ~{total_profit:,.0f} coins "
                    f"(~{total_profit / sleep_hours:,.0f} coins/hr average), buy prices already include "
                    f"a {self._get_buy_buffer_pct():g}% buffer above the current top buy order. Tap any "
                    f"item below for the full breakdown. Based on real trailing-week turnover \u2014 not "
                    f"a guarantee, the market moves in real time.{risk_note}"
                ))
            else:
                self.card_body.configure(text=(
                    f"Nothing affordable/liquid/safe enough with a purse of {purse:,.0f} coins to build "
                    f"a plan. Try a bigger purse, a smaller spread count, or a lower Min $Vol/day "
                    f"floor.{risk_note}"
                ))
        else:
            sleep_hours = None
            key = self.sort_key
            if key in ("item", "category"):
                rows = sorted(flips, key=lambda x: x.get(key, ""), reverse=self.sort_reverse)
            else:
                rows = sorted(flips, key=lambda x: x.get(key, 0), reverse=self.sort_reverse)
            mode = "full"
            visible_rows = rows[: self._full_list_shown]
            more_remaining = max(0, len(rows) - len(visible_rows))

            self.card_title.configure(text="Full List")
            self.card_body.configure(
                text=f"{len(rows)} flip(s) match your filters. Showing {len(visible_rows)}. "
                     f"Buy prices include a {self._get_buy_buffer_pct():g}% buffer above the current "
                     f"top buy order (tune it in Settings) so estimates reflect what you can "
                     f"realistically get filled at. Items marked \u26a0 are flagged as extreme-margin "
                     f"or off their own 7-day local average price - verify before trusting, or "
                     f"blacklist them from the item's detail view. Tap an item for its full details.")

        market_context = self._market_context()
        for f in visible_rows:
            card = FlipCard(self.cards_scroll.inner, f, mode, self.open_category_dialog,
                             sleep_hours=sleep_hours, on_blacklist=self.blacklist_item,
                             market_context=market_context)
            card.pack(fill="x", pady=1)

        if self.view_mode == "full" and more_remaining > 0:
            more_wrap = tk.Frame(self.cards_scroll.inner, bg=BG_DARK)
            more_wrap.pack(fill="x", pady=(8, 4))
            more_btn = tk.Button(
                more_wrap, text=f"\u25bc Show {min(more_remaining, self._full_list_page_size)} more "
                                f"({more_remaining} left)",
                font=FONT_BOLD, bg=BG_INPUT, fg=TEXT_MAIN, relief="flat", bd=0,
                padx=14, pady=10, cursor="hand2", command=self._show_more_full_list)
            more_btn.pack()
            hoverable(more_btn, BG_INPUT, ACCENT_SOFT)

        self.count_var.set(f"Showing {len(visible_rows)} of {len(rows)} flip(s)")

    # -- persistence on close ----------------------------------------------------------
    def on_close(self):
        if self._auto_refresh_after_id is not None:
            self.after_cancel(self._auto_refresh_after_id)
        self.settings.update({
            "purse": self.purse_var.get(),
            "sleep_hours": self.sleep_hours_var.get(),
            "spread_n": self.spread_var.get(),
            "risk_floor": self.risk_floor_var.get(),
            "buy_buffer_pct": self.buy_buffer_var.get(),
            "auto_refresh_enabled": self.auto_refresh_enabled,
            "auto_refresh_minutes": self.auto_refresh_minutes,
        })
        save_json(SETTINGS_PATH, self.settings)
        save_json(CUSTOM_CATEGORIES_PATH, sorted(self.custom_categories))
        self.destroy()


if __name__ == "__main__":
    settings = load_json(SETTINGS_PATH, {})
    apply_saved_theme(settings)
    app = BazaarFlipperApp()
    app.mainloop()
