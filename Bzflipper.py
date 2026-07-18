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
import traceback
import webbrowser
import zipfile
from tkinter import ttk, messagebox, colorchooser


import requests

BAZAAR_URL = "https://api.hypixel.net/v2/skyblock/bazaar"
ITEMS_URL = "https://api.hypixel.net/resources/skyblock/items"
BAZAAR_TAX = 0.0125          # 1.25% standard sell-side tax (reducible via
                              # community upgrades, but 1.25% is the safe
                              # default to assume for estimates)
MIN_DAILY_COIN_VOLUME = 2_000_000  # filters out dead/illiquid items, measured
                                    # in COINS of turnover per day - not raw
                                    # unit count, since a flat unit threshold
                                    # unfairly excludes expensive items (a
                                    # 4m-coin item only needs a handful of
                                    # sales/day to represent serious money
                                    # moving, but would never clear a
                                    # units-based bar sized for cheap items)
EXTREME_MARGIN_THRESHOLD = 150.0  # margins above this get flagged as
                                   # "unusually high - verify before trusting"
                                   # rather than treated as simply "great"
ALL_CATEGORIES = "All"
EVENT_FILTER_ALL = "All Events"
DEFAULT_SLEEP_HOURS = 8
DEFAULT_SPREAD_N = 12
FULL_LIST_PAGE_SIZE = 40     # Full List renders this many item boxes at a
                              # time, with a "Show More" button for the
                              # rest - building hundreds of boxes at once
                              # synchronously is what used to freeze the
                              # window.

# ---- Auto-refresh --------------------------------------------------------
# Keeps the bazaar snapshot from going stale while you're not actively
# clicking Refresh yourself. Off by default would mean the "snapshot age"
# indicator just keeps climbing until you remember to click - the whole
# point of an unattended Overnight Plan is that nobody's there to remember.
DEFAULT_AUTO_REFRESH_ENABLED = True
DEFAULT_AUTO_REFRESH_MINUTES = 2
MIN_AUTO_REFRESH_MINUTES = 1  # floor - avoids hammering Hypixel's API if
                               # someone sets this to 0 or a fraction

# ---- Update checker -------------------------------------------------------
# Checks GitHub's Releases API for a newer published release than this
# build. Uses the "latest release" endpoint (not tags/commits) since a
# GitHub Release is the thing that actually has your .exe attached as a
# downloadable asset - a bare tag or commit wouldn't give the user
# anything to click through to.
#
APP_VERSION = "1.1.8"  # bump this string with each GitHub release you publish
GITHUB_REPO = "Lexiixell/Bazaarflipper"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# ---- Price-history / manipulation detection ----------------------------
# Hypixel's bazaar API has no historical-price endpoint - only the current
# snapshot and 7-day *volume* (buyMovingWeek/sellMovingWeek). To get a
# 7-day AVERAGE PRICE to sanity-check the current snapshot against, this
# app builds its own local history: every refresh, it records each item's
# current raw buy/sell prices with a timestamp, and prunes anything older
# than PRICE_HISTORY_MAX_AGE_DAYS. An item whose current price has drifted
# far from its own local rolling average is flagged as a manipulation
# suspect - a real, gradual market move doesn't look like a price that's
# suddenly a quarter (or 4x) its own week-long average.
#
# This needs to accumulate samples over real elapsed time to mean anything,
# so on a fresh install (or for a brand-new item with no history yet) the
# check simply doesn't flag anything until PRICE_HISTORY_MIN_SAMPLES is met
# - it fails open (no flag) rather than closed (false-flagging everything).
PRICE_HISTORY_MAX_AGE_DAYS = 7
PRICE_HISTORY_MIN_SAMPLES = 5        # need at least this many snapshots before
                                      # trusting the average enough to flag
                                      # anything off of it
PRICE_DEVIATION_THRESHOLD_PCT = 25.0 # current price this far off its own 7d
                                      # local average gets flagged - tune this
                                      # if it's too trigger-happy on normal
                                      # volatile items, or too lax to catch
                                      # real manipulation

# ---- Snapshot staleness ---------------------------------------------
# Hypixel's bazaar endpoint returns its own "lastUpdated" timestamp - the
# moment THEIR backend captured this order-book snapshot, not the moment
# you fetched it. On volatile/thinly-traded/manipulated items (Precursor
# Gear being a well-known repeat offender - expensive, thin book relative
# to its price, frequently targeted by large orders placed and pulled in
# quick succession) the real order book can already look completely
# different by the time you glance in-game, even a few seconds later.
# There's no calculation that fixes bad/old input data - the only real
# defense is knowing HOW OLD the snapshot you're trusting is, so this is
# surfaced directly instead of silently assumed fresh.
STALE_DATA_WARNING_SECONDS = 90  # snapshot age past this turns the age
                                  # indicator red as a "double-check before
                                  # trusting this" signal

# The bazaar snapshot this app fetches is already seconds old, and on
# high-volume items other players' buy orders queue up and outbid the
# top-of-book price shown in that snapshot almost immediately - so pricing
# your own buy order at the exact raw price (what earlier versions did)
# routinely under-bids the *real* current top order by the time you place
# it. This buffer nudges the assumed buy price up so the plan reflects a
# price you can realistically get filled at, not a price that was already
# gone by the time the API responded.
#   - It's percentage-based against the item's own raw price, so it scales
#     naturally instead of being a flat amount that's way too big for a
#     400-coin item and way too small for a 400k-coin one.
#   - There's a flat-coin FLOOR (not a ceiling) for cheap items, since a
#     percentage of a low price is often a fraction of a coin - not
#     "worth doing" as a percentage, as opposed to just adding a small
#     flat nudge instead.
#   - It scales UP automatically for items whose raw margin is already
#     unusually wide. A "normal" flip clears maybe 5-20%; a margin deep
#     into the hundreds of percent is much more likely to mean the order
#     book is thin/stale/being fought over, i.e. exactly the items where
#     the snapshot price is most likely to already be gone - so those get
#     bid up harder rather than trusted at face value, the same way the
#     Overnight Plan spreads risk across more items instead of trusting
#     one number.
#   - There's a ceiling on the *percentage* (not a flat coin ceiling) so
#     the scaling keeps working for expensive items instead of being
#     capped down to nothing, while still bounding how far this can go.
DEFAULT_BUY_BUFFER_PCT = 1.0        # base % you control from the top bar
BUY_BUFFER_MIN_COINS = 5            # flat floor for cheap items
BUY_BUFFER_MAX_PCT = 20.0           # ceiling on the total (base + margin-scaled) %
BUY_BUFFER_MARGIN_SCALE_START_PCT = 30.0   # raw margins above this start being
                                            # treated as "unusually good" and begin
                                            # ramping the buffer up
BUY_BUFFER_MARGIN_SCALE_PER_100 = 3.0      # extra buffer %, per 100 percentage
                                            # points of raw margin above the
                                            # scale-start line
DEFAULT_PLAN_MIN_DAILY_VOLUME = MIN_DAILY_COIN_VOLUME * 4  # the Overnight Plan
    # is unattended, so it applies a stricter volume floor than the general
    # list (which only needs to clear MIN_DAILY_COIN_VOLUME to show up at
    # all) - thin-but-technically-alive items are exactly the ones most
    # likely to stall out or get manipulated while you're asleep.
DEFAULT_MIN_WEEKLY_SALES = 10_000  # floor on UNITS, not coins - daily_coin_volume
    # blends buy-side and sell-side moving-week volume together, so an item
    # with heavy buy-order traffic but a thin trickle of actual sales can
    # still clear the coin-volume floor above while being genuinely slow to
    # offload. This floor is checked against weekly_volume, which is
    # already the thinner (bottleneck) side of buyMovingWeek/sellMovingWeek
    # - so it catches exactly the "great margin, nobody's actually trading
    # it" items the coin-volume floor alone lets through.

# ---- Skyblock calendar --------------------------------------------------
# Hypixel exposes no "current skyblock date" API field - the calendar is
# entirely derivable from wall-clock time against a fixed epoch, since
# SkyBlock time always runs at a constant rate (confirmed via Hypixel's
# own SkyBlock Time wiki page). Constants below are the documented values,
# not guesses:
#   - SKYBLOCK_EPOCH_SECONDS: unix time of "1st of Early Spring, Year 1,
#     00:00" - everything else is computed as an offset from this.
#   - a SkyBlock day is 20 real minutes (1200s); a month is 31 SkyBlock
#     days; a year is 12 months (372 SkyBlock days total).
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
# Index of "Late Winter" - the SkyBlock month Jerry's Workshop opens for
# (on top of the separate real-life-December override below).
LATE_WINTER_MONTH_INDEX = 11
# The SB-calendar trigger only holds the Workshop open for the first 10
# real hours of Late Winter (it recurs roughly every 5 days 4 hours since
# that's how long a full SB year takes) - it does NOT stay open the whole
# month via the SB-time path. The real-life-December path below is what
# covers the rest of that month for northern-hemisphere players.
JERRY_WORKSHOP_WINDOW_SECONDS = 10 * 3600


def get_skyblock_date(timestamp=None):
    """Converts a unix timestamp (default: now) into
    (year, month_index, month_name, day) SkyBlock calendar coordinates.
    year is 1-indexed, month_index is 0-indexed into SKYBLOCK_MONTH_NAMES,
    day is 1-indexed within the month (1..31)."""
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
    """Whether Jerry's Workshop (the Winter Island) is currently open, and
    why. It opens under EITHER of two independent conditions (per the
    Hypixel wiki):
      - the SkyBlock calendar is in Late Winter, for the first 10 real
        hours of that month, OR
      - it's real-life December (Hypixel keeps it open the whole month,
        separately from the SkyBlock-time trigger, so players get a full
        real-life month of it once a year on top of the recurring
        in-game window).
    Real-life-December is checked against the local machine's own clock -
    close enough for a "should I care about this today" flag without
    pulling in a timezone database dependency for a cosmetic edge case
    around midnight on Nov 30/Dec 1."""
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


def harvest_festival_status(now=None, mayor_info=None):
    """Whether the Harvest Festival is currently active. Calendar-gated:
    runs during Early Autumn, Autumn, Late Autumn every SkyBlock year.
    If Finnegan is mayor and has the Grand Feast perk, it lasts the
    ENTIRE SkyBlock year instead of just autumn."""
    if now is None:
        now = time.time()
    year, month_index, month_name, day = get_skyblock_date(now)

    finnegan_extends = False
    if mayor_info and mayor_info.get("name") == "Finnegan":
        perks = mayor_info.get("perks") or []
        if HARVEST_FESTIVAL_FINNEGAN_PERK in perks:
            finnegan_extends = True
    # Minister can also carry the perk
    if mayor_info and mayor_info.get("minister_perk") == HARVEST_FESTIVAL_FINNEGAN_PERK:
        finnegan_extends = True

    calendar_active = month_index in HARVEST_FESTIVAL_MONTH_INDICES
    active = finnegan_extends or calendar_active

    reasons = []
    if calendar_active:
        reasons.append(f"SkyBlock {month_name} (Year {year}, day {day})")
    if finnegan_extends and not calendar_active:
        reasons.append("Finnegan's Grand Feast extends Harvest Festival year-round")

    return {
        "active": active,
        "reasons": reasons,
        "finnegan_extended": finnegan_extends,
        "skyblock_year": year,
        "skyblock_month": month_name,
        "skyblock_day": day,
    }


def oringo_status(now=None):
    """Whether Oringo's Traveling Zoo is currently visiting, and which
    legendary pet is available (computed from the fixed rotation cycle).
    The pet index is auto-calculated but can be overridden by the user
    in Settings if the computed rotation has drifted."""
    if now is None:
        now = time.time()
    elapsed = now - ORINGO_EPOCH_SECONDS
    cycle_position = elapsed % ORINGO_CYCLE_SECONDS
    active = cycle_position < ORINGO_VISIT_SECONDS

    # Which visit number is this (or the most recent one)?
    visit_number = int(elapsed // ORINGO_CYCLE_SECONDS)
    pet_index = visit_number % len(ORINGO_LEGENDARY_ROTATION)
    current_pet = ORINGO_LEGENDARY_ROTATION[pet_index]

    # Time until next visit (if not active now)
    if active:
        time_remaining = ORINGO_VISIT_SECONDS - cycle_position
    else:
        time_remaining = ORINGO_CYCLE_SECONDS - cycle_position

    return {
        "active": active,
        "current_pet": current_pet,
        "pet_index": pet_index,
        "visit_number": visit_number,
        "time_remaining_seconds": int(time_remaining),
        "pet_materials": ORINGO_PET_MATERIALS.get(current_pet, []),
    }


def year_of_pig_status(now=None):
    """Whether it's currently the Year of the Pig in SkyBlock. Occurs
    once every 12 SkyBlock years (~62 real days)."""
    if now is None:
        now = time.time()
    year, month_index, month_name, day = get_skyblock_date(now)
    is_pig_year = (year % YEAR_OF_PIG_CYCLE) == YEAR_OF_PIG_OFFSET

    # Next pig year
    if is_pig_year:
        next_pig_year = year + YEAR_OF_PIG_CYCLE
    else:
        remainder = year % YEAR_OF_PIG_CYCLE
        if YEAR_OF_PIG_OFFSET > remainder:
            next_pig_year = year + (YEAR_OF_PIG_OFFSET - remainder)
        else:
            next_pig_year = year + (YEAR_OF_PIG_CYCLE - remainder + YEAR_OF_PIG_OFFSET)

    return {
        "active": is_pig_year,
        "skyblock_year": year,
        "skyblock_month": month_name,
        "skyblock_day": day,
        "next_pig_year": next_pig_year,
    }


# ---- Event forecasting (how far off is the NEXT occurrence?) --------------
# Everything above answers "is this event live right now?"; the bazaar's
# event-price engine (event_price_engine/) also wants "when does it NEXT
# start?", so it can flag an event as ~24h away and pre-position the items
# tied to it before the price actually moves. That's a pure function of the
# same fixed SkyBlock calendar the status helpers already use - no API for
# it, same as get_skyblock_date.
#
# Only the events whose next start is BOTH cleanly derivable from the
# calendar AND rare relative to the lead window get a forecast here:
#   - Oringo / Traveling Zoo (fixed 50h rotation cycle)
#   - Jerry's Workshop (SkyBlock Late Winter opening window, once per SB year)
#   - Harvest Festival (Early Autumn, once per SB year - unless Finnegan's
#     perk has extended it to run all year, in which case there's no discrete
#     "next start" worth a heads-up and it's skipped)
#   - Year of the Pig (once every 12 SB years)
# The perk-gated, fast-recurring events (Fishing Festival every SB month,
# Mining Fiesta days 1-7 of five months, Mythological Ritual for a whole
# term) are deliberately NOT forecast: they either recur far faster than a
# 24h lead window (so "within 24h" carries no signal) or depend on who wins
# an election weeks out. They still get real-time detection + item tracking
# exactly as before - this only adds the "coming soon" layer on top.
DEFAULT_EVENT_LEAD_HOURS = 24  # how far ahead of an event's start to start
                                # flagging it as "upcoming" / tracking its items;
                                # user-overridable in Settings (event_lead_hours).


def _next_skyblock_month_start_ts(now, month_index, day=1):
    """Unix timestamp of the next time the SkyBlock calendar reaches
    (month_index, day) at 00:00 SB time, strictly after `now`. month_index is
    0-indexed into SKYBLOCK_MONTH_NAMES; day is 1-indexed. Mirrors the exact
    day-counting get_skyblock_date() uses, run in reverse."""
    days_per_year = SKYBLOCK_DAYS_PER_MONTH * SKYBLOCK_MONTHS_PER_YEAR  # 372
    elapsed = max(0, now - SKYBLOCK_EPOCH_SECONDS)
    total_sb_days = int(elapsed // SKYBLOCK_DAY_SECONDS)
    current_year = total_sb_days // days_per_year  # 0-indexed SB year
    target_day_of_year = month_index * SKYBLOCK_DAYS_PER_MONTH + (day - 1)
    candidate_sb_day = current_year * days_per_year + target_day_of_year
    candidate_ts = SKYBLOCK_EPOCH_SECONDS + candidate_sb_day * SKYBLOCK_DAY_SECONDS
    if candidate_ts <= now:
        # This year's occurrence is already past (or exactly now) - the next
        # one is a full SB year later.
        candidate_ts += days_per_year * SKYBLOCK_DAY_SECONDS
    return int(candidate_ts)


def forecast_events(now=None, mayor_info=None):
    """Predict the next start time of each cleanly-forecastable event. Returns
    a list of dicts: {event_key, next_start_ts, recurrence_seconds, source}.
    recurrence_seconds is roughly how often the event recurs, used downstream
    to decide whether a fixed lead window (e.g. 24h) is even meaningful for
    it. Pure/deterministic - safe to call every tick."""
    if now is None:
        now = time.time()
    sb_year_seconds = SKYBLOCK_DAYS_PER_MONTH * SKYBLOCK_MONTHS_PER_YEAR * SKYBLOCK_DAY_SECONDS
    forecasts = []

    # Oringo / Traveling Zoo - fixed rotation. The next fresh visit start is
    # always one cycle-remainder away, whether or not it's visiting right now
    # (if it's here now, this points at the FOLLOWING visit, not "0s").
    cycle_position = (now - ORINGO_EPOCH_SECONDS) % ORINGO_CYCLE_SECONDS
    forecasts.append({
        "event_key": "oringo",
        "next_start_ts": int(now + (ORINGO_CYCLE_SECONDS - cycle_position)),
        "recurrence_seconds": ORINGO_CYCLE_SECONDS,
        "source": "Traveling Zoo rotation cycle",
    })

    # Jerry's Workshop - SkyBlock Late Winter opening window (once per SB year).
    forecasts.append({
        "event_key": "jerry_workshop",
        "next_start_ts": _next_skyblock_month_start_ts(now, LATE_WINTER_MONTH_INDEX, 1),
        "recurrence_seconds": sb_year_seconds,
        "source": "SkyBlock Late Winter calendar window",
    })

    # Harvest Festival - starts at Early Autumn (the first of the autumn
    # months) each SB year. If Finnegan's Grand Feast has extended it to run
    # the whole year, there's no discrete upcoming start to flag.
    harvest = harvest_festival_status(now, mayor_info)
    if not harvest.get("finnegan_extended"):
        early_autumn_index = min(HARVEST_FESTIVAL_MONTH_INDICES)
        forecasts.append({
            "event_key": "harvest_festival",
            "next_start_ts": _next_skyblock_month_start_ts(now, early_autumn_index, 1),
            "recurrence_seconds": sb_year_seconds,
            "source": "SkyBlock autumn calendar window",
        })

    # Year of the Pig - the whole SB year, once every 12 SB years.
    pig = year_of_pig_status(now)
    next_pig_year = pig.get("next_pig_year")
    if next_pig_year:
        pig_start_ts = SKYBLOCK_EPOCH_SECONDS + (next_pig_year - 1) * sb_year_seconds
        forecasts.append({
            "event_key": "year_of_pig",
            "next_start_ts": int(pig_start_ts),
            "recurrence_seconds": YEAR_OF_PIG_CYCLE * sb_year_seconds,
            "source": f"SkyBlock Year {next_pig_year} (12-year zodiac cycle)",
        })

    return forecasts


def upcoming_events_within(now=None, mayor_info=None,
                            lead_seconds=DEFAULT_EVENT_LEAD_HOURS * 3600,
                            active_event_keys=None):
    """Filter forecast_events() down to the events genuinely worth a heads-up
    right now: not already live, starting within `lead_seconds`, and recurring
    rarely enough that "within the lead window" actually means something (an
    event that recurs faster than the lead window is trivially always 'within
    24h' and carries no signal, so it's dropped). Each returned dict carries a
    live `seconds_until`, nearest first."""
    if now is None:
        now = time.time()
    active_event_keys = active_event_keys or set()
    upcoming = []
    for fc in forecast_events(now, mayor_info):
        if fc["recurrence_seconds"] <= lead_seconds:
            continue
        if fc["event_key"] in active_event_keys:
            continue  # it's happening now, not "coming up"
        seconds_until = fc["next_start_ts"] - now
        if 0 < seconds_until <= lead_seconds:
            upcoming.append({**fc, "seconds_until": int(seconds_until)})
    upcoming.sort(key=lambda x: x["seconds_until"])
    return upcoming


# ---- Mayor / election API + seasonal event windows -----------------------
# Hypixel's election endpoint returns the CURRENTLY ELECTED mayor plus
# their minister (a second candidate who lost the mayoral race but still
# grants their own single perk for the term) - both matter here, since
# either one can be carrying a festival-triggering perk.
MAYOR_URL = "https://api.hypixel.net/v2/resources/skyblock/election"

# Mayor/minister perks that turn on a recurring seasonal bazaar-relevant
# event, mapped to a normalized event key. Deliberately NOT tracking Year
# of the Pig / Scorpius (Foxy) / Marina's Seal Perk / Mayor Fear here -
# only the ones worth wiring into flip logic right now.
FESTIVAL_PERK_EVENTS = {
    "Fishing Festival": "fishing_festival",        # Marina
    "Mining Fiesta": "mining_fiesta",               # Cole
    "Mythological Ritual": "mythological_ritual",   # Diana
    "Grand Feast": "harvest_festival",              # Finnegan (extends to full year)
}
# Paul's signature perk: 20% off dungeon-related NPC costs. Doesn't touch
# bazaar order prices directly, but several bazaar-tradeable dungeon-drop
# materials have their effective "replacement cost" tied to an NPC-priced
# recipe, so this flag lets the UI note the discount wherever those items
# show up.
PAUL_DUNGEON_DISCOUNT_PERK = "Marauder"
PAUL_DUNGEON_DISCOUNT_PCT = 20.0

# Within-month scheduling for the two perk-triggered events that don't run
# for a mayor's whole term (start_day, end_day), 1-indexed inclusive:
#   - Fishing Festival: the first 3 days of every month, for as long as
#     Marina/her perk is active.
#   - Mining Fiesta: days 1-7 of five specific SB months per Cole's term
#     (Summer .. Late Autumn - indices 4-8 into SKYBLOCK_MONTH_NAMES).
# Mythological Ritual has no sub-window - Diana's perk runs for her whole
# term (a full SkyBlock year), so it's simply "active whenever she holds
# the perk" (see compute_active_festivals).
FISHING_FESTIVAL_DAYS = (1, 3)
MINING_FIESTA_DAYS = (1, 7)
MINING_FIESTA_MONTH_INDICES = {4, 5, 6, 7, 8}  # Summer .. Late Autumn

# ---- Harvest Festival (calendar-gated, Finnegan-extendable) ---------
# Runs every SkyBlock year during the autumn months. If Finnegan is mayor
# and has the Grand Feast perk, it lasts the ENTIRE SkyBlock year instead.
HARVEST_FESTIVAL_MONTH_INDICES = {6, 7, 8}  # Early Autumn, Autumn, Late Autumn
HARVEST_FESTIVAL_FINNEGAN_PERK = "Grand Feast"

# ---- Oringo / Traveling Zoo -----------------------------------------
# Oringo appears every 2 real days and 2 hours (50 hours). The legendary
# pet rotates through a fixed 6-pet cycle each visit.
ORINGO_CYCLE_SECONDS = 50 * 3600          # 180,000 seconds between visits
ORINGO_VISIT_SECONDS = 3600               # stays for ~1 real hour
# Reference epoch: a known Oringo appearance. Adjust if the computed
# rotation drifts from what you see in-game.
ORINGO_EPOCH_SECONDS = 1560275700 + 3600  # ~1h after SB epoch
ORINGO_LEGENDARY_ROTATION = [
    "Blue Whale", "Tiger", "Lion", "Monkey", "Elephant", "Giraffe",
]
# Bazaar materials associated with each legendary pet (items whose demand
# shifts when that pet is available from Oringo). Indexed by pet name.
ORINGO_PET_MATERIALS = {
    "Blue Whale": ["ENCHANTED_COOKED_FISH"],
    "Tiger":      ["ENCHANTED_RAW_CHICKEN"],
    "Lion":       ["ENCHANTED_RAW_BEEF"],
    "Monkey":     ["ENCHANTED_JUNGLE_LOG"],
    "Elephant":   ["ENCHANTED_DARK_OAK_LOG"],
    "Giraffe":    ["ENCHANTED_ACACIA_LOG"],
}

# ---- Year of the Pig ------------------------------------------------
# Occurs once every 12 SkyBlock years (the 12th animal in the zodiac
# cycle). Lasts for the entire SkyBlock year when it fires.
YEAR_OF_PIG_CYCLE = 12
# Which position in the 12-year cycle is "Pig" (0-indexed).
# year % 12 == this value means it's Year of the Pig.
YEAR_OF_PIG_OFFSET = 0  # SB years 12, 24, 36, ...


def fetch_mayor_info():
    """Fetch the current mayor + minister from Hypixel's election resource
    and normalize into a small dict. Raises on network/HTTP failure - this
    is supplementary market context, so callers should treat a failure as
    non-fatal (fall back to cached info) rather than blocking the core
    bazaar refresh everything else depends on."""
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
    """Given fetch_mayor_info()'s output, figures out which of the
    perk-gated seasonal events (Fishing Festival / Mining Fiesta /
    Mythological Ritual) are currently running - both WHO holds the
    triggering perk (mayor's own perks, or the minister's single perk)
    and, for the two that have an in-month schedule, WHEN in the current
    SkyBlock month it actually fires. A mayor merely holding the perk
    doesn't mean the festival is live at this exact moment; it means it's
    scheduled to recur, and only actually running during its window.

    Returns a list of dicts: {"label", "event_key", "source",
    "active_now", "window"}. "active_now" separates "this mayor's term
    makes this event possible" from "it's actually happening right now" -
    callers tagging bazaar items for right-now relevance should filter on
    active_now."""
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
        # mythological_ritual: no sub-window, active for the whole term.

        active.append({
            "label": perk_name,
            "event_key": event_key,
            "source": source,
            "active_now": active_now,
            "window": window,
        })
    return active


def paul_dungeon_discount_active(info):
    """True if the current mayor OR minister is carrying Paul's Marauder
    perk (20% cheaper dungeon NPC costs). Flagged as context on relevant
    bazaar items, not baked into the price math itself - the bazaar price
    already reflects whatever real supply change occurred; there's no
    reliable way to separately quantify "how much of today's price is
    because of Paul" from the snapshot alone."""
    if not info:
        return False
    if PAUL_DUNGEON_DISCOUNT_PERK in (info.get("perks") or []):
        return True
    return info.get("minister_perk") == PAUL_DUNGEON_DISCOUNT_PERK


# Lightweight keyword tagging so relevant bazaar items can be flagged with
# which seasonal event/mayor perk affects their supply/demand right now.
# Keyword-based rather than an exhaustive ID list, since Hypixel adds new
# event items periodically - this stays useful without needing to be
# hand-updated every content patch, at the cost of occasionally tagging
# something unrelated that happens to share a word (acceptable for an
# informational badge, not something the profit math depends on).
EVENT_ITEM_KEYWORDS = {
    "mining_fiesta": ["REFINED_MINERAL", "GLOSSY_GEMSTONE"],
    "fishing_festival": ["SHARK"],
    "mythological_ritual": ["ANCIENT_CLAW", "MINOS", "HARPY", "DAEDALUS", "MINOAUR", "CREATAN_BULL", "SPHINX" "ENCHANTMENT_ULTIMATE_CHIMERA"],
    "jerry_workshop": ["WHITE_GIFT","GREEN_GIFT","RED_GIFT", "HUNK_OF_BLUE_ICE", "HUNK_OF_ICE", "ENCHANTMENT_PROSPERITY", "WALNUT",],
    "spooky_festival": ["CANDY_CORN", "PURPLE_CANDY", "GREEN_CANDY",
                         "ECTOPLASM", "PUMPKING_GUTS", "SPOOKY_FRAGMENT", "WEREWOLF_SKIN",
                         "SOUL_FRAGMENT"],
    "dungeon_supply": ["ESSENCE_UNDEAD", "ESSENCE_WITHER", "RECOMBOBULATOR",
                        "FUMING_POTATO_BOOK", "HOT_POTATO_BOOK", "PRECURSOR_GEAR",
                        "IMPLOSION_SCROLL", "SHADOW_WARP_SCROLL", "WITHER_SHIELD_SCROLL"
                        "FIRST_MASTER_STAR","SECOND_MASTER_STAR","THIRD_MASTER_STAR","FOURTH_MASTER_STAR","FIFTH_MASTER_STAR","WITHERBLOOD"],
    "harvest_festival": ["CORNUCOPIA", "CARROT_ZEST", "DEEPFRIES", "AGGOURDIAN",
                          "CANE_KNOT", "MELON_JUICE", "CACTUS_FLOWER",
                          "DESIGNER_COFFEE_BEANS", "FEASTFUNGUS", "BOTROOT",
                          "SALTED_SUNFLOWER_SEEDS", "CRYSTALIZED_MOONLIGHT",
                          "FLORAL_GELATIN"],
    "oringo": ["ENCHANTED_COOKED_FISH", 
                "ENCHANTED_RAW_BEEF", 
                "ENCHANTED_RAW_CHICKEN",
                "ENCHANTED_JUNGLE_LOG", 
                "ENCHANTED_DARK_OAK_LOG", 
                "ENCHANTED_ACACIA_LOG",],
    "year_of_pig": ["FARMING_FOR_DUMMIES", "ENCHANTMENT_HARVESTING",
                     "POTATO_SPREADING"],
}



def tag_event_relevance(product_id):
    """Returns the list of event_keys (from EVENT_ITEM_KEYWORDS) whose
    keywords appear in this bazaar product id. Used to badge items in the
    UI that a currently-active (or currently-inactive) seasonal event or
    Paul's dungeon discount is likely to affect."""
    return [key for key, keywords in EVENT_ITEM_KEYWORDS.items()
            if any(kw in product_id for kw in keywords)]


# ---- Fill-time / sell-time estimates --------------------------------------
def compute_fill_sell_hours(flip, units):
    """Rough estimated real-world hours to (a) get `units` filled on the
    BUY side (your buy order being matched by sellers) and (b) get them
    filled on the SELL side (your sell offer being matched by buyers),
    based on the item's own trailing 7-day moving volume as a stand-in
    for its typical daily flow.

    This is the same trailing-week-volume basis the rest of the app
    already uses for liquidity (hourly_volume/daily_coin_volume), just
    expressed as "how long would MY order take" instead of "how much
    volume exists" - sellMovingWeek is the flow of players selling INTO
    the bazaar, which is what fills a buy order; buyMovingWeek is the
    flow of players buying FROM the bazaar, which is what fills a sell
    offer. Returns (fill_hours, sell_hours), either being None if that
    side's weekly volume is 0 (no basis to estimate from - fails open
    rather than reporting a misleading instant/infinite fill)."""
    sell_daily = flip.get("sell_moving_week", 0) / 7
    buy_daily = flip.get("buy_moving_week", 0) / 7
    fill_hours = (units / sell_daily * 24) if sell_daily > 0 else None
    sell_hours = (units / buy_daily * 24) if buy_daily > 0 else None
    return fill_hours, sell_hours


def fmt_hours(hours):
    """Formats an estimated hours figure the way a player actually thinks
    about it - minutes when it's under an hour, hours to 1 decimal under a
    day, days beyond that. Returns an em dash for unknown/None (no
    turnover data to estimate from) rather than a misleading "0"."""
    if hours is None:
        return "\u2014"
    hours = max(0, hours)
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


# ---- Persistent app-data folder ---------------------------------------
def get_app_data_dir():
    """Returns (and creates) a per-user folder to store this app's data.
    Works the same whether run as a .py script or a frozen PyInstaller .exe."""
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
MAYOR_CACHE_PATH = os.path.join(APP_DATA_DIR, "mayor_cache.json")  # last-known mayor/
    # election info, so a failed election-API fetch (it's a separate
    # endpoint from the bazaar, and can fail independently) falls back to
    # the last thing we successfully saw instead of blanking out every
    # event badge in the app.
STORAGE_PATH = os.path.join(APP_DATA_DIR, "storage.json")  # user's saved/pinned
    # flips + manual entries - see the Storage tab. Lives in the same
    # per-user APPDATA/AppSupport/XDG folder as every other *_PATH above,
    # which is what makes it (and everything else in this file) survive a
    # GitHub-release update: _apply_update()/robocopy only ever mirrors
    # files into the INSTALL folder (next to the .exe), and never touches
    # APP_DATA_DIR at all, so anything saved here is untouched by an
    # update no matter how the install folder's contents change.


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
        pass  # non-fatal - app still works, just won't persist this run


# ---- Theme -----------------------------------------------------------
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

# Palette a category chip color is deterministically picked from, so the
# same category always renders the same color across restarts.
CHIP_PALETTE = [
    "#b085f5", "#60a5fa", "#4ade80", "#facc15",
    "#f87171", "#38bdf8", "#f472b6", "#a3e635",
    "#fb923c", "#2dd4bf",
]

# Quick-pick swatches shown in the Settings > Appearance color wheel panel,
# in addition to the OS custom-color picker.
ACCENT_COLOR_PRESETS = [
    "#b085f5", "#60a5fa", "#4ade80", "#facc15", "#f87171",
    "#38bdf8", "#f472b6", "#fb923c", "#2dd4bf", "#a3e635",
]

# ---- User-customizable theme -------------------------------------------
# Only the accent color is user-facing (picked in Settings, via presets or
# the OS color wheel/picker) - its hover/dim/soft variants are DERIVED from
# it in HLS space rather than requiring 4 separate picks, so the whole
# palette stays visually coherent no matter what color someone chooses.
# Applied once at startup (see apply_saved_theme below), not live - a true
# live re-theme would mean tracking and reconfiguring every already-built
# widget (many bake their color in as a literal at creation time, e.g. the
# hover-color closures in `hoverable()`), which risks leaving parts of the
# UI half-updated. Restart-to-apply is simpler and can't do that.
def _hex_to_rgb01(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb01_to_hex(rgb):
    r, g, b = (max(0, min(1, c)) for c in rgb)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def derive_accent_shades(accent_hex):
    """Given a base accent color, derive the hover/dim/soft variants used
    throughout the UI by adjusting lightness/saturation in HLS space."""
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
    """Overrides the default ACCENT palette with a user-chosen color from
    settings.json, if one was saved. Must run before _setup_style() and
    _build_widgets() - both read these module-level constants directly."""
    global ACCENT, ACCENT_HOVER, ACCENT_DIM, ACCENT_SOFT
    saved = settings.get("accent_color")
    if isinstance(saved, str) and len(saved) == 7 and saved.startswith("#"):
        shades = derive_accent_shades(saved)
        ACCENT = saved
        ACCENT_HOVER = shades["hover"]
        ACCENT_DIM = shades["dim"]
        ACCENT_SOFT = shades["soft"]


# Fields shown inside an expanded item box, in order. "item"/"category"
# are shown in the box header instead, not repeated here.
DETAIL_FIELDS = [
    ("buy_order_at",  "Buy At"),
    ("buy_buffer",    "  \u21b3 buffer included"),
    ("sell_offer_at", "Sell At"),
    ("profit",        "Profit/Item"),
    ("margin",        "Margin %"),
    ("volume",        "Daily Volume"),
    ("weekly_volume", "Weekly Volume (7d)"),
]

# Short badge text + color for each event_key a flip might be tagged
# with, shown next to the category pill when that event is CURRENTLY
# relevant (either the seasonal window is open, or - for the standing
# Paul discount - his perk is currently active). Kept separate from
# EVENT_ITEM_KEYWORDS above since that table is about detection, this
# one's about display.
EVENT_BADGE_STYLE = {
    "mining_fiesta":       ("\u26cf Mining Fiesta", ACCENT_YELLOW),
    "fishing_festival":    ("\U0001F41F Fishing Festival", ACCENT_BLUE),
    "mythological_ritual": ("\u2666 Mythological Ritual", ACCENT_GREEN),
    "jerry_workshop":      ("\u2744 Jerry's Workshop", ACCENT_BLUE),
    "spooky_festival":     ("\U0001F383 Spooky Festival", ACCENT_YELLOW),
    "dungeon_supply":      ("\u2694 Paul -20% Chests", ACCENT_RED),
    "harvest_festival":    ("\U0001F33E Harvest Festival", ACCENT_GREEN),
    "oringo":              ("\U0001F981 Traveling Zoo", ACCENT_YELLOW),
    "year_of_pig":         ("\U0001F437 Year of the Pig", ACCENT_RED),
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
EVENT_ENGINE_SORT_OPTIONS = [
    ("Confidence", "confidence"),
    ("Expected movement", "movement"),
    ("Item", "item"),
    ("Recommendation", "action"),
]


def format_category(raw):
    if not raw:
        return None
    return raw.replace("_", " ").title()


def chip_color(category_name):
    """Deterministic color for a category name, so it stays stable."""
    h = sum(ord(c) for c in category_name)
    return CHIP_PALETTE[h % len(CHIP_PALETTE)]


# Hypixel's own /resources/skyblock/items "category" field is built mainly
# to support the reforge/enchant UI (which items are reforgeable, which are
# enchanted books) - it does NOT attempt to categorize every bazaar-tradeable
# material. Essence, runes, and some reforge stones commonly have no category
# at all in Hypixel's data. This fallback fills in a couple of well-known,
# unambiguous ID naming conventions. Manual overrides (right-click a row)
# take priority over both this and Hypixel's own data.
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
    """Turns 'v1.4.2' / '1.4.2' into (1, 4, 2) for numeric comparison -
    plain string comparison would wrongly say '1.10.0' < '1.9.0'."""
    v = v.strip().lstrip("vV")
    parts = []
    for chunk in v.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def check_for_update():
    """Hits GitHub's 'latest release' API for GITHUB_REPO and compares its
    tag against APP_VERSION. Returns (update_available, latest_version,
    release_url, asset_url). asset_url is the direct download link for the
    .zip attached to the release (a zipped copy of the --onedir build
    folder - the exe plus its DLLs/dependencies, since PyInstaller onedir
    builds are a folder, not a single file), or None if the release has no
    .zip asset uploaded (e.g. a release published without attaching the
    built app, or just GitHub's own auto-generated source-code zips, which
    this deliberately does NOT match - see the name filter below) - callers
    should fall back to opening the browser in that case, since there's
    nothing to silently download and swap in. Raises on network/HTTP
    failure - caller decides whether that's worth surfacing (silent on a
    background auto-check, shown on a manual 'Check for Updates' click)."""
    response = requests.get(GITHUB_RELEASES_API, timeout=10,
                             headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    data = response.json()
    latest_tag = data.get("tag_name", "")
    release_url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest"
    
    # --- START OF DEBUG LOGS ---
    print("\n" + "="*40)
    print("      UPDATER DEBUG LOGS      ")
    print("="*40)
    print(f"[VERSION CHECK]")
    print(f"  -> Local version string (APP_VERSION): '{APP_VERSION}'")
    print(f"  -> Local version parsed tuple:         {_parse_version(APP_VERSION)}")
    print(f"  -> GitHub latest tag found:            '{latest_tag}'")
    print(f"  -> GitHub tag parsed tuple:            {_parse_version(latest_tag)}")
    
    update_available = _parse_version(latest_tag) > _parse_version(APP_VERSION)
    print(f"  -> Does Remote > Local? Result:        {update_available}")
    print("\n[GITHUB ASSETS FOUND]")
    # --- END OF DEBUG LOGS ---

    # NOTE: data.get("assets", []) is the list of files YOU manually attach
    # to the release (uploaded_download_url etc.) - it does NOT include
    # GitHub's own auto-generated "Source code (zip)"/"Source code (tar.gz)"
    # links, since those live in a separate zipball_url/tarball_url field,
    # not in "assets". So this only ever matches a zip you actually uploaded.
    asset_url = None
    for asset in data.get("assets", []):
        asset_name = asset.get("name", "")
        # --- DEBUG LINE FOR ASSETS ---
        print(f"  -> Found file: '{asset_name}'")
        
        if asset_name.lower().endswith(".zip") or asset_name.lower().endswith(".rar"):
            asset_url = asset.get("browser_download_url")
            # --- DEBUG LINE FOR MATCH ---
            print(f"     MATCHED ASSET! URL: {asset_url}")
            break

    # --- FINAL DEBUG PRINT ---
    if not asset_url:
        print("  -> WARNING: No matching .zip or .rar asset was selected!")
    print("="*40 + "\n")
    # --------------------------

    return update_available, latest_tag, release_url, asset_url



def fetch_item_categories():
    """Fetch item metadata and return {item_id: formatted_category}."""
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
    """Fetch the raw bazaar API response (products + Hypixel's own
    lastUpdated timestamp for this snapshot). Split out from fetch_flips
    so the caller can read lastUpdated directly instead of it being
    silently discarded - see the STALE_DATA_WARNING_SECONDS comment
    above for why that timestamp matters."""
    response = requests.get(BAZAAR_URL, timeout=15)
    response.raise_for_status()

    data = response.json()
    if not data.get("success"):
        raise ValueError("Hypixel API reported failure (success=false)")
    return data


def fetch_flips(category_map, overrides, bazaar_data):
    """Turn a raw bazaar API response (from fetch_bazaar_data) into every
    profitable flip (list of dicts)."""
    products = bazaar_data.get("products", {})
    flips = []

    for product_id, info in products.items():
        quick_status = info.get("quick_status", {})

        buy_price = quick_status.get("buyPrice", 0)     # instant-buy price (top sell offer)
        sell_price = quick_status.get("sellPrice", 0)    # instant-sell price (top buy order)

        # NOTE ON VOLUME FIELDS (this was the source of the wildly-off
        # profit/hr numbers in earlier versions): Hypixel's own API docs
        # define quick_status.buyVolume/sellVolume as "the sum of item
        # amounts in all [open] orders" - that's order-BOOK DEPTH, a
        # snapshot of what's currently listed, not a trade-flow number.
        # A thinly-traded or manipulated item can pile up huge book depth
        # (one big spoofed order) while almost nothing actually changes
        # hands - which is exactly what produced things like a 3000%+
        # "flip" that was really just a stale, unfillable order sitting
        # in the book.
        #
        # The real turnover metric is buyMovingWeek/sellMovingWeek - units
        # actually transacted over the trailing 7 days. That's the field
        # every serious flipping tool bases hourly profit on, so that's
        # what we use here.
        buy_moving_week = quick_status.get("buyMovingWeek", 0)
        sell_moving_week = quick_status.get("sellMovingWeek", 0)
        avg_daily_volume = (buy_moving_week + sell_moving_week) / 7

        # Coin-value version of the same turnover - this is what actually
        # decides whether an item is "dead," not the raw unit count. Using
        # sell_price (what a unit costs you to acquire) keeps this on the
        # same basis as cost_per_item below.
        avg_daily_coin_volume = avg_daily_volume * sell_price

        if sell_price <= 0 or buy_price <= 0 or avg_daily_coin_volume < MIN_DAILY_COIN_VOLUME:
            continue

        # Strategy: acquire via buy order at sell_price, then instantly
        # liquidate to a sell offer at buy_price, minus tax on the sale.
        cost_per_item = sell_price
        post_tax_earnings = buy_price * (1 - BAZAAR_TAX)
        raw_profit = post_tax_earnings - cost_per_item
        if raw_profit <= 0:
            continue

        margin_percent = (raw_profit / cost_per_item) * 100

        # Bottleneck liquidity: you need turnover on BOTH sides to keep
        # flipping continuously (your buy order needs sellers filling it,
        # your sell offer needs buyers filling it), so use the thinner
        # side's weekly turnover - and spread it across a full week
        # (168h), not 24h, since that's the actual window this data
        # covers.
        bottleneck_weekly_volume = min(buy_moving_week, sell_moving_week)
        hourly_volume = bottleneck_weekly_volume / (7 * 24)

        # Priority: manual override > Hypixel's own category > ID-pattern
        # guess > "Uncategorized".
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
            "weekly_volume": round(bottleneck_weekly_volume),  # thinner side, 7d - the
                                                                # real risk signal, not
                                                                # the friendlier averaged
                                                                # "volume" figure above
            "daily_coin_volume": round(avg_daily_coin_volume),
            "buy_moving_week": buy_moving_week,     # raw 7d moving totals, kept for
            "sell_moving_week": sell_moving_week,   # fill/sell-time estimates
            "event_tags": event_tags,               # keyword-matched seasonal-event tags
        })

    flips.sort(key=lambda x: x["profit"], reverse=True)
    return flips  # no artificial cap - list is scrollable


# ---- Local 7-day price history (manipulation detection) ----------------
def load_price_history():
    return load_json(PRICE_HISTORY_PATH, {})


def record_and_prune_price_history(history, flips):
    """Append this fetch's raw prices to each item's local history, drop
    samples older than PRICE_HISTORY_MAX_AGE_DAYS, save, and return the
    updated history. This is the only source of "7-day average price" -
    Hypixel's API doesn't expose historical prices, only current snapshot
    + 7-day volume, so the average has to be built locally over time."""
    now = time.time()
    cutoff = now - PRICE_HISTORY_MAX_AGE_DAYS * 86400

    for f in flips:
        pid = f["id"]
        samples = history.get(pid, [])
        samples.append([now, f["raw_buy_target"], f["raw_sell_target"]])
        samples = [s for s in samples if s[0] >= cutoff]
        history[pid] = samples

    # Also prune history for items that no longer show up as live flips at
    # all (e.g. dropped below the volume floor), so the file doesn't grow
    # forever with stale entries.
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
    """Compare each flip's current raw buy/sell prices against its own
    local 7-day average and flag items that have drifted unusually far
    from their own history - a strong signal of order-book manipulation
    or a stale/spoofed quote, distinct from the extreme_margin check
    (which only looks at the CURRENT snapshot in isolation and has no way
    to tell "this item is always like this" from "this just moved a lot").

    Fails open: an item with fewer than PRICE_HISTORY_MIN_SAMPLES of local
    history (brand new to the app, or just started clearing the volume
    filter) is never flagged - there's not enough data yet to call
    something abnormal, and false-flagging everything on day one would
    make the flag meaningless."""
    for f in flips:
        samples = history.get(f["id"], [])
        f["price_manipulation_suspect"] = False
        f["price_deviation_pct"] = None

        if len(samples) < PRICE_HISTORY_MIN_SAMPLES:
            continue

        avg_buy_target = sum(s[1] for s in samples) / len(samples)   # avg of raw_buy_target (sell_price)
        avg_sell_target = sum(s[2] for s in samples) / len(samples)  # avg of raw_sell_target (buy_price)

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
    """The coin amount added on top of the raw top-buy-order price. See the
    DEFAULT_BUY_BUFFER_PCT comment above for the reasoning: percentage of
    price, scaled further for suspiciously wide raw margins, floored (not
    capped) in coins so cheap items still get a meaningful nudge."""
    margin_over = max(0.0, raw_margin_percent - BUY_BUFFER_MARGIN_SCALE_START_PCT)
    margin_bonus_pct = margin_over / 100.0 * BUY_BUFFER_MARGIN_SCALE_PER_100
    effective_pct = min(BUY_BUFFER_MAX_PCT, base_pct + margin_bonus_pct)
    pct_amount = raw_buy_target * (effective_pct / 100.0)
    return max(BUY_BUFFER_MIN_COINS, pct_amount)


def apply_buy_buffer(flips, buffer_pct):
    """Recompute buy_order_at/profit/margin using raw_buy_target + a buffer
    that scales with both the base % you set and the item's own raw
    margin (see compute_buy_buffer_amount). Returns a new list - items no
    longer profitable once the buffer is added are dropped, same as the
    dead/unprofitable filter in fetch_flips, just re-applied post-buffer."""
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
    This is the "if you went all-in on this ONE item" figure - useful for
    comparing items in the Full List, but NOT what the Overnight Plan
    actually allocates (see compute_portfolio for that)."""
    for f in flips:
        max_affordable = int(purse // f["cost_per_item"]) if f["cost_per_item"] > 0 else 0
        achievable_units = max(0, min(max_affordable, int(f["hourly_volume"])))
        f["achievable_units"] = achievable_units
        f["profit_hr"] = round(achievable_units * f["profit"], 1)
    return flips


def compute_portfolio(flips, purse, sleep_hours, target_n, min_daily_coin_volume, min_weekly_volume=0,
                       price_trends=None):
    """Spread `purse` across up to `target_n` of the best flips, sized so
    each item's slice is realistically fillable within `sleep_hours` -
    instead of one all-in pick.

    Four risk filters get applied before anything is ranked, since this
    plan runs unattended while you're away:
      - extreme-margin items are dropped entirely. A margin that wide is
        as likely to be a stale/manipulated order book as a real
        opportunity, and there's nobody watching to bail out if it's the
        former.
      - items whose CURRENT price has drifted far from their own local
        7-day average price are dropped, regardless of margin - see
        apply_price_deviation_flags. This catches manipulation that
        extreme_margin alone can miss (a manipulated book doesn't always
        produce a huge margin - sometimes both sides get pushed together).
      - items whose trailing-week coin turnover is below
        `min_daily_coin_volume` are dropped. They're technically alive
        (they already cleared the global dead-item filter) but thin
        enough that one unlucky order can leave you sitting on unsold
        stock all night.
      - items whose trailing-week UNIT sales (weekly_volume - already the
        thinner of buyMovingWeek/sellMovingWeek) are below
        `min_weekly_volume` are dropped. daily_coin_volume above blends
        both sides together, so a coin-heavy but rarely-traded item can
        still clear that floor while actually being slow to fill or
        offload - this catches those directly, on units rather than
        coins.

    What's left is ranked by profit potential WITHIN the sleep window
    itself (liquidity_units_over_horizon * profit), not by an
    hours-independent score - so a shorter window genuinely favors
    different (more immediately liquid) items than a longer one, instead
    of just buying less of the same fixed list. Each candidate then takes
    the smaller of an even per-slot share of what's left or what its own
    liquidity over the window can absorb (water-fill), so a thin item
    doesn't hog a slot's worth of coins it can't actually place.

    If price_trends is given ({product_id: {"direction", "pct_change"}}
    from BazaarFlipperApp.get_price_trend), each candidate's ranking score
    gets a small multiplier from its own 24h trend: a RISING item nudges
    up (you're buying in before the price likely climbs further, which is
    upside for the sell-side of the flip), a FALLING item nudges down
    (you're about to buy into a slide, which works against you on the
    buy-side and risks the sell-side dropping further before you offload).
    This only re-ranks candidates already cleared by the four risk filters
    above - it never overrides them, and an item with no trend data yet
    (price_trends.get returns None) is scored as neutral (1.0x), not
    penalized, since "unknown" isn't evidence of anything.
    """
    sleep_hours = max(0.1, sleep_hours)
    target_n = max(1, int(target_n))
    min_daily_coin_volume = max(0, min_daily_coin_volume)
    min_weekly_volume = max(0, min_weekly_volume)
    price_trends = price_trends or {}

    base_pool = [f for f in flips if f.get("cost_per_item", 0) > 0 and f.get("hourly_volume", 0) > 0]
    candidates = [
        f for f in base_pool
        if not f.get("extreme_margin")
        and not f.get("price_manipulation_suspect")
        and f.get("daily_coin_volume", 0) >= min_daily_coin_volume
        and f.get("weekly_volume", 0) >= min_weekly_volume
    ]
    risk_excluded_count = len(base_pool) - len(candidates)

    # Rank by profit achievable under an EVEN slot share of the purse, capped 
    avg_slot_budget = purse / target_n if target_n > 0 else purse
    for f in candidates:
        window_units = int(f["hourly_volume"] * sleep_hours)
        budget_units = int(avg_slot_budget // f["cost_per_item"])
        f["_window_units"] = window_units
        f["_score_units"] = min(window_units, budget_units)

        trend = price_trends.get(f["id"])
        if trend is None:
            trend_mult = 1.0
        elif trend["direction"] == "rising":
            trend_mult = 1.15
        elif trend["direction"] == "falling":
            trend_mult = 0.85
        else:
            trend_mult = 1.0
        f["_trend_mult"] = trend_mult
        f["price_trend"] = trend

    candidates = [f for f in candidates if f["_score_units"] > 0]
    candidates.sort(key=lambda f: f["_score_units"] * f["profit"] * f["_trend_mult"], reverse=True)

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
    """A frame whose contents can overflow vertically with a scrollbar +
    mousewheel support. Used for the item box list."""
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
    """A frame whose contents can overflow horizontally with a scrollbar."""
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
    """Attach simple hover-color behavior to a plain tk widget (Button/Label)."""
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
    """A collapsible box for one item. Header shows name/category/quick
    number; tapping it expands a detail grid with every field inside."""
    def __init__(self, parent, flip, mode, on_set_category, sleep_hours=None, on_blacklist=None,
                 market_context=None, on_add_storage=None, trend=None):
        super().__init__(parent, bg=BORDER_SUBTLE)
        self.flip = flip
        self.mode = mode
        self.expanded = False
        self.on_set_category = on_set_category
        self.on_blacklist = on_blacklist
        self.on_add_storage = on_add_storage
        self.trend = trend
        # market_context carries {"active_event_keys": set(...),
        # "paul_discount_active": bool} so the card can badge itself
        # against what's ACTUALLY live right now, not just what keywords
        # matched on the item id.
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

        # Event badge(s) - only shown for tags that are ACTUALLY live right
        # now per market_context, not just keyword-matched on the item id.
        # A keyword match with no live event is informational only and is
        # left for the detail view rather than cluttering every header.
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

        trend_lbl = None
        if self.trend is not None:
            arrow = {"rising": "\u2191", "falling": "\u2193", "flat": "\u2192"}.get(self.trend["direction"], "")
            trend_color = {"rising": ACCENT_GREEN, "falling": ACCENT_RED,
                           "flat": TEXT_DIM}.get(self.trend["direction"], TEXT_DIM)
            trend_lbl = tk.Label(header, text=f"{arrow} {self.trend['pct_change']:+.1f}%", font=FONT_PILL,
                                  bg=BG_PANEL, fg=trend_color)
            trend_lbl.pack(side="left", padx=(8, 0), pady=5)
            badge_widgets.append(trend_lbl)

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

        # click anywhere on the header to toggle
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

        if self.trend is not None:
            rows.append(("24h Price Trend",
                         f"{self.trend['direction'].capitalize()} ({self.trend['pct_change']:+.1f}%)"))

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
                     text=("\u26a0 Unusually high margin - this is often a volatile/thin order book "
                           "that's already moved by the time you look in-game (Precursor Gear and "
                           "similar expensive commodities are frequent repeat offenders). Always "
                           "check the live in-game price before committing coins, even right after "
                           "a fresh refresh."),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=ACCENT_RED,
                     wraplength=900, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        if flip.get("price_manipulation_suspect"):
            tk.Label(self.detail,
                     text=(f"\u26a0 Current price is {flip['price_deviation_pct']:.0f}% off its own "
                           f"7-day local average - possible manipulation or a stale/spoofed order, "
                           f"verify in-game before trusting."),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=ACCENT_RED,
                     wraplength=900, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        # Seasonal-event context: show WHY this item is tagged, even for
        # tags that aren't live right now (e.g. "Mining Fiesta affects this
        # item, but isn't running this SkyBlock month") - useful context
        # for planning ahead, distinct from the header badge which only
        # shows currently-live tags.
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

        if self.on_add_storage:
            storage_btn = tk.Button(btn_row, text="\U0001F4E6 Add to Storage", font=FONT_PILL, bg=BG_INPUT,
                                     fg=ACCENT, relief="flat", bd=0, padx=9, pady=4, cursor="hand2",
                                     command=lambda: self.on_add_storage(flip))
            storage_btn.pack(side="left", padx=(8, 0))
            hoverable(storage_btn, BG_INPUT, ACCENT_SOFT)

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
                # re-parent detail under body_wrap equivalent: since detail's
                # master must be a child of the same row, we place it as a
                # sibling frame packed after the row itself.
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
    """Small modal dialog to manually set/reset an item's category."""
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
    """Lists every category (custom + in-use). Lets you rename, delete,
    or add new ones, all from one place."""
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
    """One place for every configurable option except the three you tune
    most often (Purse, Spread, Run Time - those live on the main window
    now, see BazaarFlipperApp._build_widgets): the remaining trading
    parameters, the theme accent color (color wheel), auto-refresh
    cadence, and the item blacklist. Fields are bound directly to the SAME
    StringVars the app already reads via _get_purse()/_get_risk_floor()/
    etc. - this dialog is the only place those get edited, nothing
    downstream had to change.

    Resizable and scrollable (instead of a fixed-height fixed-size window)
    so the whole thing still fits - and is reachable - on a small/short
    screen; the Save/Cancel row stays pinned to the bottom of the window
    rather than scrolling out of view with everything else."""
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.configure(bg=BG_PANEL)
        self.transient(app)

        # Size to fit the screen instead of a fixed height that can exceed
        # a small/short display - the scrollable body below handles any
        # content that still doesn't fit.
        self.update_idletasks()
        screen_h = self.winfo_screenheight()
        height = max(360, min(640, screen_h - 120))
        self.geometry(f"480x{height}")
        self.minsize(360, 280)
        self.resizable(True, True)
        self.grab_set()

        tk.Frame(self, bg=ACCENT, height=3).pack(side="top", fill="x")

        # Save/Cancel pinned to the bottom, outside the scroll area, so
        # they're always reachable no matter how far the content scrolls.
        btn_row = tk.Frame(self, bg=BG_PANEL)
        btn_row.pack(side="bottom", fill="x", padx=20, pady=12)
        ttk.Button(btn_row, text="Save", command=self._save).pack(side="left")
        ttk.Button(btn_row, text="Cancel", style="Secondary.TButton", command=self.destroy).pack(side="left", padx=8)
        tk.Frame(self, bg=BORDER_SUBTLE, height=1).pack(side="bottom", fill="x")

        body_scroll = VerticalScrollFrame(self, bg=BG_PANEL)
        body_scroll.pack(side="top", fill="both", expand=True)
        outer = tk.Frame(body_scroll.inner, bg=BG_PANEL)
        outer.pack(fill="both", expand=True, padx=20, pady=16)

        tk.Label(outer, text="Settings", font=FONT_HEAD, bg=BG_PANEL, fg=ACCENT).pack(anchor="w", pady=(0, 12))

        # --- Trading Parameters ---
        # Purse, Spread, and Run Time live on the main window now - only
        # the less-frequently-touched risk parameters stay here.
        self._section(outer, "Trading Parameters")
        params = tk.Frame(outer, bg=BG_PANEL)
        params.pack(fill="x", pady=(0, 14))
        self._param_row(params, "Min $Vol/day:", app.risk_floor_var)
        self._param_row(params, "Min Weekly Sales:", app.min_weekly_sales_var)
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

        # --- Seasonal Event Timing ---
        self._section(outer, "Seasonal Events")
        events_frame = tk.Frame(outer, bg=BG_PANEL)
        events_frame.pack(fill="x", pady=(0, 14))
        tk.Label(events_frame,
                 text="How far ahead of a seasonal event's start to flag it as “upcoming” "
                      "and begin tracking its related items. Only events that recur rarely enough "
                      "for the lead time to matter are forecast (Traveling Zoo, Jerry's Workshop, "
                      "Harvest Festival, Year of the Pig).",
                 font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM, wraplength=420,
                 justify="left").pack(anchor="w", pady=(0, 6))
        lead_row = tk.Frame(events_frame, bg=BG_PANEL)
        lead_row.pack(fill="x", pady=2)
        tk.Label(lead_row, text="Lead time (hours):", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM,
                 width=16, anchor="w").pack(side="left")
        current_lead_hours = getattr(app, "event_lead_seconds",
                                     DEFAULT_EVENT_LEAD_HOURS * 3600) / 3600.0
        self.event_lead_var = tk.StringVar(value=f"{current_lead_hours:g}")
        ttk.Entry(lead_row, textvariable=self.event_lead_var, width=16).pack(side="left")

        # --- Oringo Pet Override ---
        self._section(outer, "Oringo (Traveling Zoo)")
        oringo_frame = tk.Frame(outer, bg=BG_PANEL)
        oringo_frame.pack(fill="x", pady=(0, 14))
        tk.Label(oringo_frame, text="The legendary pet is auto-calculated from the rotation cycle. "
                 "Override here if the auto-detection has drifted from what you see in-game.",
                 font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM, wraplength=420,
                 justify="left").pack(anchor="w", pady=(0, 6))
        pet_row = tk.Frame(oringo_frame, bg=BG_PANEL)
        pet_row.pack(fill="x", pady=2)
        tk.Label(pet_row, text="Current pet:", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM,
                 width=16, anchor="w").pack(side="left")
        self.oringo_pet_var = tk.StringVar(
            value=app.settings.get("oringo_pet_override", "Auto"))
        pet_values = ["Auto"] + ORINGO_LEGENDARY_ROTATION
        ttk.Combobox(pet_row, textvariable=self.oringo_pet_var, state="readonly",
                     values=pet_values, width=16).pack(side="left")
        # Show what auto-detection thinks right now
        auto_pet = getattr(app, "oringo_status_info", {}).get("current_pet", "?")
        tk.Label(oringo_frame, text=f"Auto-detected: {auto_pet}", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_FAINT).pack(anchor="w", pady=(2, 0))

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

        # Oringo pet override
        pet_choice = self.oringo_pet_var.get() if hasattr(self, "oringo_pet_var") else "Auto"
        self.app.settings["oringo_pet_override"] = pet_choice

        # Seasonal-event lead time. Invalid input falls back to the default
        # rather than blocking the rest of the save.
        try:
            lead_hours = max(1.0, float(self.event_lead_var.get()))
        except (ValueError, AttributeError):
            lead_hours = DEFAULT_EVENT_LEAD_HOURS
        self.app.event_lead_seconds = lead_hours * 3600.0
        self.app.settings["event_lead_hours"] = lead_hours

        self.app.settings.update({
            "accent_color": self.accent_var.get(),
            "auto_refresh_enabled": self.app.auto_refresh_enabled,
            "auto_refresh_minutes": self.app.auto_refresh_minutes,
        })
        save_json(SETTINGS_PATH, self.app.settings)

        # Recompute upcoming events immediately so a changed lead time shows in
        # the status bar / Event Engine without waiting for the next refresh.
        self.app._recompute_upcoming_forecasts()
        self.app.events_var.set(self.app._events_status_text())

        self.app._schedule_auto_refresh()
        self.app.recompute_and_render()
        self.destroy()


class ManualStorageDialog(tk.Toplevel):
    """Modal for adding a hand-typed entry to Storage - for items you want
    to track that didn't come from an existing flip card (e.g. something
    you spotted in-game, or a price you want to keep an eye on). Buy/Sell
    price are optional; profit/margin are only computed when both are
    given, otherwise the entry is stored as a plain note."""
    def __init__(self, parent, existing_categories, on_add):
        super().__init__(parent)
        self.title("Add Item to Storage")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self.transient(parent)
        self.on_add = on_add

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        outer = tk.Frame(self, bg=BG_PANEL)
        outer.pack(fill="both", expand=True, padx=20, pady=16)

        tk.Label(outer, text="Add Item to Storage", font=FONT_HEAD, bg=BG_PANEL, fg=ACCENT).pack(
            anchor="w", pady=(0, 4))
        tk.Label(outer, text="Manually track any item, price, or note - not just flips already "
                              "found by the scanner.", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM,
                 wraplength=380, justify="left").pack(anchor="w", pady=(0, 14))

        self.name_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Manual")
        self.buy_var = tk.StringVar()
        self.sell_var = tk.StringVar()
        self.qty_var = tk.StringVar(value="1")
        self.notes_var = tk.StringVar()

        # --- Item ---
        self._section(outer, "Item")
        item_grid = tk.Frame(outer, bg=BG_PANEL)
        item_grid.pack(fill="x", pady=(0, 14))
        item_grid.columnconfigure(1, weight=1)
        name_entry = self._grid_entry(item_grid, 0, "Item Name:", self.name_var)
        self._grid_entry(item_grid, 1, "Category:", self.category_var,
                          values=sorted(existing_categories))

        # --- Pricing ---
        self._section(outer, "Pricing (optional)")
        price_row = tk.Frame(outer, bg=BG_PANEL)
        price_row.pack(fill="x", pady=(0, 2))
        self._inline_field(price_row, "Buy:", self.buy_var, width=12)
        self._inline_field(price_row, "Sell:", self.sell_var, width=12, padx=(18, 0))
        tk.Label(outer, text="Leave both blank to store this as a plain note.",
                 font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_FAINT).pack(anchor="w", pady=(4, 14))

        # --- Details ---
        self._section(outer, "Details")
        details_grid = tk.Frame(outer, bg=BG_PANEL)
        details_grid.pack(fill="x", pady=(0, 4))
        details_grid.columnconfigure(1, weight=1)
        self._grid_entry(details_grid, 0, "Quantity:", self.qty_var)
        self._grid_entry(details_grid, 1, "Notes:", self.notes_var)

        name_entry.focus_set()

        btn_row = tk.Frame(outer, bg=BG_PANEL)
        btn_row.pack(fill="x", pady=(18, 0))

        def do_add():
            name = self.name_var.get().strip()
            if not name:
                messagebox.showwarning("Missing name", "Enter an item name.", parent=self)
                return

            def parse_optional_float(s):
                s = s.strip().replace(",", "")
                if not s:
                    return None
                try:
                    return float(s)
                except ValueError:
                    return None

            buy = parse_optional_float(self.buy_var.get())
            sell = parse_optional_float(self.sell_var.get())
            try:
                qty = max(1, int(float(self.qty_var.get().strip() or "1")))
            except ValueError:
                qty = 1

            self.on_add({
                "item": name,
                "category": self.category_var.get().strip() or "Manual",
                "buy_at": buy,
                "sell_at": sell,
                "quantity": qty,
                "notes": self.notes_var.get().strip(),
            })
            self.destroy()

        ttk.Button(btn_row, text="Add", command=do_add).pack(side="left")
        ttk.Button(btn_row, text="Cancel", style="Secondary.TButton", command=self.destroy).pack(side="left", padx=8)

        # Center over the parent window now that the final size is known.
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        self.grab_set()

    def _section(self, parent, text):
        tk.Label(parent, text=text, font=FONT_SUBHEAD, bg=BG_PANEL, fg=TEXT_MAIN).pack(anchor="w", pady=(0, 6))

    def _grid_entry(self, grid, row_idx, label, var, values=None):
        tk.Label(grid, text=label, font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM,
                 width=11, anchor="w").grid(row=row_idx, column=0, sticky="w", pady=4)
        if values is not None:
            widget = ttk.Combobox(grid, textvariable=var, values=values)
        else:
            widget = ttk.Entry(grid, textvariable=var)
        widget.grid(row=row_idx, column=1, sticky="ew", pady=4)
        return widget

    def _inline_field(self, parent, label, var, width=12, padx=(0, 0)):
        wrap = tk.Frame(parent, bg=BG_PANEL)
        wrap.pack(side="left", padx=padx)
        tk.Label(wrap, text=label, font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_DIM).pack(side="left", padx=(0, 6))
        ttk.Entry(wrap, textvariable=var, width=width).pack(side="left")


class StorageCard(tk.Frame):
    """A collapsible box for one saved Storage entry - either pulled in
    from a live flip (source == 'flip') or hand-typed (source ==
    'manual'). Mirrors FlipCard's look/collapse behavior so Storage feels
    like part of the same app, but its fields come from the stored entry
    dict itself rather than a live bazaar snapshot."""
    def __init__(self, parent, entry, on_remove, recommendation=None, trend=None):
        super().__init__(parent, bg=BORDER_SUBTLE)
        self.entry = entry
        self.expanded = False
        self.detail = None
        self.on_remove = on_remove
        self.recommendation = recommendation
        self.trend = trend

        inner = tk.Frame(self, bg=BG_PANEL)
        inner.pack(fill="both", expand=True)

        stripe_color = ACCENT if entry.get("source") == "manual" else chip_color(entry.get("category") or "Manual")
        tk.Frame(inner, bg=stripe_color, width=4).pack(side="left", fill="y")

        body_wrap = tk.Frame(inner, bg=BG_PANEL)
        body_wrap.pack(side="left", fill="both", expand=True)

        header = tk.Frame(body_wrap, bg=BG_PANEL, cursor="hand2")
        header.pack(fill="x")

        name_text = entry.get("item", "Unknown Item")
        name_lbl = tk.Label(header, text=name_text, font=FONT_SUBHEAD, bg=BG_PANEL, fg=TEXT_MAIN)
        name_lbl.pack(side="left", padx=(10, 0), pady=5)

        cat_lbl = tk.Label(header, text=entry.get("category") or "Manual", font=FONT_PILL,
                            bg=BG_PANEL, fg=TEXT_DIM)
        cat_lbl.pack(side="left", padx=(8, 0), pady=5)

        source_text = "\u270e Manual" if entry.get("source") == "manual" else "\U0001F517 From Flip"
        source_lbl = tk.Label(header, text=source_text, font=FONT_PILL, bg=BG_PANEL, fg=TEXT_FAINT)
        source_lbl.pack(side="left", padx=(8, 0), pady=5)

        rec_lbl = None
        if recommendation is not None:
            action = recommendation.action
            action_color = {"buy": ACCENT_GREEN, "sell": ACCENT_RED,
                            "hold": ACCENT_YELLOW}.get(action, TEXT_DIM)
            rec_lbl = tk.Label(header, text=f"\u2192 {action.upper()}", font=FONT_PILL,
                                bg=BG_PANEL, fg=action_color)
            rec_lbl.pack(side="left", padx=(8, 0), pady=5)

        trend_lbl = None
        if trend is not None:
            arrow = {"rising": "\u2191", "falling": "\u2193", "flat": "\u2192"}.get(trend["direction"], "")
            trend_color = {"rising": ACCENT_GREEN, "falling": ACCENT_RED,
                           "flat": TEXT_DIM}.get(trend["direction"], TEXT_DIM)
            trend_lbl = tk.Label(header, text=f"{arrow} {trend['pct_change']:+.1f}%", font=FONT_PILL,
                                  bg=BG_PANEL, fg=trend_color)
            trend_lbl.pack(side="left", padx=(8, 0), pady=5)

        self.arrow_lbl = tk.Label(header, text="\u25b6", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_FAINT)
        self.arrow_lbl.pack(side="right", padx=(0, 10), pady=5)

        margin = entry.get("margin")
        if margin is not None:
            quick_text = f"{margin:.1f}% margin \u00b7 {fmt_num(entry.get('profit') or 0)}/item"
            quick_color = ACCENT_GREEN if margin >= 50 else ACCENT_YELLOW if margin >= 15 else TEXT_DIM
        else:
            quick_text = "note"
            quick_color = TEXT_DIM
        quick_lbl = tk.Label(header, text=quick_text, font=FONT_MAIN, bg=BG_PANEL, fg=quick_color)
        quick_lbl.pack(side="right", padx=(0, 10), pady=5)

        toggle_widgets = [header, name_lbl, cat_lbl, source_lbl, quick_lbl, self.arrow_lbl]
        if rec_lbl is not None:
            toggle_widgets.append(rec_lbl)
        if trend_lbl is not None:
            toggle_widgets.append(trend_lbl)
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

    def _build_detail(self):
        entry = self.entry
        rows = []
        if entry.get("buy_at") is not None:
            rows.append(("Buy At", fmt_num(entry["buy_at"])))
        if entry.get("sell_at") is not None:
            rows.append(("Sell At", fmt_num(entry["sell_at"])))
        if entry.get("profit") is not None:
            rows.append(("Profit/Item", fmt_num(entry["profit"])))
        if entry.get("margin") is not None:
            rows.append(("Margin %", f"{entry['margin']:.1f}%"))
        rows.append(("Quantity", fmt_int(entry.get("quantity") or 1)))
        if entry.get("added_ts"):
            rows.append(("Added", time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["added_ts"]))))

        grid = tk.Frame(self.detail, bg=BG_PANEL_RAISED)
        grid.pack(fill="x", padx=16, pady=(8, 2))
        for i, (label, text) in enumerate(rows):
            r, c = divmod(i, 2)
            cell = tk.Frame(grid, bg=BG_PANEL_RAISED)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 28), pady=2)
            tk.Label(cell, text=label + ":", font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_DIM).pack(side="left")
            tk.Label(cell, text=" " + text, font=FONT_BOLD, bg=BG_PANEL_RAISED, fg=TEXT_MAIN).pack(side="left")

        if entry.get("notes"):
            tk.Label(self.detail, text="Notes: " + entry["notes"], font=FONT_MAIN, bg=BG_PANEL_RAISED,
                     fg=TEXT_DIM, wraplength=900, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        if self.recommendation is not None:
            rec = self.recommendation
            conf = max(rec.buy_confidence, rec.sell_confidence)
            tk.Label(self.detail,
                     text=(f"Event-engine signal: {rec.action.upper()} "
                           f"({conf:.0f}% confidence, {rec.expected_appreciation:+.1f}% expected)"),
                     font=FONT_SUBHEAD, bg=BG_PANEL_RAISED, fg=TEXT_MAIN).pack(
                anchor="w", padx=16, pady=(6, 2))
            if rec.explanation:
                reasoning_text = "\n".join(f"\u2022 {line}" for line in rec.explanation)
                tk.Label(self.detail, text=reasoning_text, font=FONT_MAIN, bg=BG_PANEL_RAISED,
                         fg=TEXT_DIM, wraplength=880, justify="left").pack(
                    anchor="w", padx=16, pady=(0, 6))
        elif self.entry.get("product_id"):
            tk.Label(self.detail,
                     text="No event-engine signal yet for this item (needs recorded event "
                          "history to compare against).",
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_FAINT,
                     wraplength=880, justify="left").pack(anchor="w", padx=16, pady=(2, 6))

        if self.trend is not None:
            trend_color = {"rising": ACCENT_GREEN, "falling": ACCENT_RED,
                           "flat": TEXT_DIM}.get(self.trend["direction"], TEXT_DIM)
            tk.Label(self.detail,
                     text=(f"Price trend (24h, own history): {self.trend['direction'].upper()} "
                           f"({self.trend['pct_change']:+.1f}%)"),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=trend_color).pack(
                anchor="w", padx=16, pady=(2, 6))

        btn_row = tk.Frame(self.detail, bg=BG_PANEL_RAISED)
        btn_row.pack(anchor="w", padx=16, pady=(2, 12))
        remove_btn = tk.Button(btn_row, text="Remove from Storage", font=FONT_PILL, bg=BG_INPUT,
                                fg=ACCENT_RED, relief="flat", bd=0, padx=9, pady=4, cursor="hand2",
                                command=lambda: self.on_remove(entry["entry_id"]))
        remove_btn.pack(side="left")
        hoverable(remove_btn, BG_INPUT, "#4a2a2a")

    def toggle(self, _event=None):
        self.expanded = not self.expanded
        if self.expanded:
            if self.detail is None:
                self.detail = tk.Frame(self, bg=BG_PANEL_RAISED)
                self.detail.pack(fill="x")
                self._build_detail()
            else:
                self.detail.pack(fill="x")
            self.arrow_lbl.configure(text="\u25bc")
        else:
            if self.detail is not None:
                self.detail.pack_forget()
            self.arrow_lbl.configure(text="\u25b6")


class EventItemCard(tk.Frame):
    """One item's event-driven Buy/Hold/Sell analysis, collapsible like
    FlipCard. Displays exactly what the event_price_engine's Recommendation
    (from scoring.py, produced via Pipeline.generate_recommendation) already
    computed - this card only formats it, it never re-derives a score."""
    def __init__(self, parent, item_id, recommendation):
        super().__init__(parent, bg=BORDER_SUBTLE)
        self.expanded = False
        self.detail = None
        self.rec = recommendation
        self.item_id = item_id

        inner = tk.Frame(self, bg=BG_PANEL)
        inner.pack(fill="both", expand=True)

        action = recommendation.action if recommendation else "hold"
        action_color = {"buy": ACCENT_GREEN, "sell": ACCENT_RED,
                        "hold": ACCENT_YELLOW}.get(action, TEXT_DIM)

        stripe = tk.Frame(inner, bg=action_color, width=4)
        stripe.pack(side="left", fill="y")

        body = tk.Frame(inner, bg=BG_PANEL)
        body.pack(side="left", fill="both", expand=True)

        header = tk.Frame(body, bg=BG_PANEL, cursor="hand2")
        header.pack(fill="x")

        name_text = item_id.replace("_", " ").title()
        name_lbl = tk.Label(header, text=name_text, font=FONT_SUBHEAD, bg=BG_PANEL, fg=TEXT_MAIN)
        name_lbl.pack(side="left", padx=(10, 8), pady=6)

        action_lbl = tk.Label(header, text=action.upper(), font=FONT_PILL, bg=BG_PANEL, fg=action_color)
        action_lbl.pack(side="left", pady=6)

        if recommendation:
            if action == "buy":
                conf = recommendation.buy_confidence
            elif action == "sell":
                conf = recommendation.sell_confidence
            else:
                conf = max(recommendation.buy_confidence, recommendation.sell_confidence)
            quick_text = f"{conf:.0f}% confidence \u00b7 {recommendation.expected_appreciation:+.1f}% expected"
            confidence_color = (ACCENT_GREEN if conf >= 75 else
                                ACCENT_YELLOW if conf >= 50 else ACCENT_RED)
        else:
            quick_text = "insufficient data yet"
            confidence_color = TEXT_FAINT
        quick_lbl = tk.Label(header, text=quick_text, font=FONT_MAIN, bg=BG_PANEL, fg=confidence_color)
        quick_lbl.pack(side="right", padx=(0, 10), pady=6)

        self.arrow_lbl = tk.Label(header, text="\u25b6", font=FONT_MAIN, bg=BG_PANEL, fg=TEXT_FAINT)
        self.arrow_lbl.pack(side="right", padx=(0, 6), pady=6)

        toggle_widgets = [header, name_lbl, action_lbl, quick_lbl, self.arrow_lbl]
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

    def toggle(self, _event=None):
        self.expanded = not self.expanded
        if self.expanded:
            if self.detail is None:
                self.detail = tk.Frame(self, bg=BG_PANEL_RAISED)
                self.detail.pack(fill="x")
                self._build_detail()
            else:
                self.detail.pack(fill="x")
            self.arrow_lbl.configure(text="\u25bc")
        else:
            if self.detail is not None:
                self.detail.pack_forget()
            self.arrow_lbl.configure(text="\u25b6")

    def _build_detail(self):
        rec = self.rec

        if rec is None:
            tk.Label(self.detail,
                     text=("No current Buy/Hold/Sell recommendation yet - this needs at least one "
                           "closed historical occurrence of this event (with price history recorded "
                           "during it) plus current price data for this item. It fills in "
                           "automatically as more real event occurrences pass."),
                     font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_DIM,
                     wraplength=880, justify="left").pack(anchor="w", padx=16, pady=(10, 12))
            return

        try:
            details = json.loads(rec.details_json)
        except (TypeError, ValueError):
            details = {}
        buy_window = details.get("buy_window")
        sell_window = details.get("sell_window")
        anchor = details.get("anchor")
        occurrences_used = details.get("occurrences_used")

        grid = tk.Frame(self.detail, bg=BG_PANEL_RAISED)
        grid.pack(fill="x", padx=16, pady=(10, 6))
        rows = [
            ("Recommendation", rec.action.upper()),
            ("Buy Confidence", f"{rec.buy_confidence:.1f}%"),
            ("Sell Confidence", f"{rec.sell_confidence:.1f}%"),
            ("Expected Profit/Deviation", f"{rec.expected_appreciation:+.1f}%"),
            ("Expected Holding Window", f"{rec.expected_holding_days:.1f} day(s)"),
        ]
        if buy_window:
            rows.append(("Best Historical Buy Window",
                          f"Day {buy_window[0]} to Day {buy_window[1]} "
                          f"(relative to event {anchor or 'start'})"))
        if sell_window:
            rows.append(("Best Historical Sell Window",
                          f"Day {sell_window[0]} to Day {sell_window[1]} "
                          f"(relative to event {anchor or 'start'})"))
        if occurrences_used is not None:
            rows.append(("Historical Occurrences Used", str(occurrences_used)))

        for i, (label, text) in enumerate(rows):
            r, c = divmod(i, 2)
            cell = tk.Frame(grid, bg=BG_PANEL_RAISED)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 28), pady=2)
            tk.Label(cell, text=label + ":", font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_DIM).pack(side="left")
            tk.Label(cell, text=" " + text, font=FONT_BOLD, bg=BG_PANEL_RAISED, fg=TEXT_MAIN).pack(side="left")

        if rec.explanation:
            tk.Label(self.detail, text="Historical Trend, Supporting Indicators & Reasoning:",
                     font=FONT_SUBHEAD, bg=BG_PANEL_RAISED, fg=TEXT_MAIN).pack(
                anchor="w", padx=16, pady=(8, 2))
            reasoning_text = "\n".join(f"\u2022 {line}" for line in rec.explanation)
            tk.Label(self.detail, text=reasoning_text, font=FONT_MAIN, bg=BG_PANEL_RAISED, fg=TEXT_DIM,
                     wraplength=880, justify="left").pack(anchor="w", padx=16, pady=(0, 12))


class LegacyEventRecommendationsView:
    """Browsable, per-event breakdown of the event-driven price-deviation
    analysis engine (event_price_engine): historical price trend, current
    Buy/Hold/Sell call, confidence score, expected profit/deviation, best
    historical buy/sell windows, and the indicators behind each call.

    This dialog does NOT compute any of that itself - it only calls the
    existing Pipeline/Database facade (event_price_engine) and formats
    what comes back, so there's exactly one copy of the scoring logic
    (scoring.py / indicators.py / event_study.py), same as bazaar_bridge.py
    already does for feeding data in. One dedicated, collapsible section
    per tracked seasonal event, so multiple events are easy to browse and
    compare side by side."""
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Event-Driven Price Recommendations")
        self.configure(bg=BG_DARK)
        self.geometry("1000x640")
        self.minsize(760, 420)
        self.transient(app)

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        header = tk.Frame(self, bg=BG_DARK)
        header.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(header, text="\U0001F4C8 Event-Driven Price Recommendations", font=FONT_HEAD,
                 bg=BG_DARK, fg=ACCENT).pack(side="left")
        refresh_btn = tk.Button(header, text="\u21bb Refresh", font=FONT_PILL, bg=BG_INPUT,
                                 fg=TEXT_MAIN, relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
                                 command=self._reload)
        refresh_btn.pack(side="right")
        hoverable(refresh_btn, BG_INPUT, ACCENT_SOFT)

        self.status_var = tk.StringVar(value="Loading event analysis...")
        tk.Label(self, textvariable=self.status_var, font=FONT_MAIN, bg=BG_DARK, fg=TEXT_DIM,
                 wraplength=960, justify="left").pack(anchor="w", padx=18, pady=(0, 8))

        self.scroll = VerticalScrollFrame(self, bg=BG_DARK)
        self.scroll.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.after(50, self._reload)

    def _reload(self):
        self.status_var.set("Loading event analysis...")
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        try:
            sections = self.app._gather_event_sections()
        except Exception as exc:
            self.status_var.set(f"Couldn't load event analysis: {exc}")
            return

        if not sections:
            self.status_var.set(
                "No seasonal events have been recorded yet. This fills in automatically as Mining "
                "Fiesta, Fishing Festival, Mythological Ritual, or Jerry's Workshop actually occur "
                "while the app is running - check back after the next one starts (or has run at "
                "least once since you started using this build).")
            return

        self.status_var.set(f"{len(sections)} tracked event type(s). Tap any item for its full breakdown.")
        for section in sections:
            self._render_section(section)

    def _render_section(self, section):
        event_type = section["event_type"]
        label, color = EVENT_BADGE_STYLE.get(event_type, (event_type, ACCENT))
        current = section["current_instance"]

        start_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(current.start_ts))
        if current.end_ts:
            end_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(current.end_ts))
            timeframe = f"{start_txt} \u2192 {end_txt} (ended)"
        else:
            timeframe = f"{start_txt} \u2192 ongoing"

        wrap = tk.Frame(self.scroll.inner, bg=BG_DARK)
        wrap.pack(fill="x", pady=(4, 10))

        head_row = tk.Frame(wrap, bg=BG_DARK)
        head_row.pack(fill="x")
        tk.Frame(head_row, bg=color, width=4, height=22).pack(side="left", padx=(0, 8))
        tk.Label(head_row, text=label, font=FONT_TITLE, bg=BG_DARK, fg=color).pack(side="left")
        tk.Label(head_row, text=f"   Most recent occurrence: {timeframe}", font=FONT_MAIN,
                 bg=BG_DARK, fg=TEXT_DIM).pack(side="left")
        tk.Label(head_row, text=f"{section['historical_count']} prior occurrence(s) on record",
                 font=FONT_MAIN, bg=BG_DARK, fg=TEXT_FAINT).pack(side="right", padx=10, pady=9)

        if not section["items"]:
            tk.Label(wrap, text="No items with a current Buy/Hold/Sell recommendation for this "
                                 "event yet (needs at least one closed prior occurrence to compare "
                                 "against).",
                     font=FONT_MAIN, bg=BG_DARK, fg=TEXT_FAINT, wraplength=940,
                     justify="left").pack(anchor="w", pady=(6, 0))
            return

        cards_frame = tk.Frame(wrap, bg=BG_DARK)
        cards_frame.pack(fill="x", pady=(6, 0))
        for row in section["items"]:
            card = EventItemCard(cards_frame, row["item_id"], row["recommendation"])
            card.pack(fill="x", pady=1)


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
        self.storage = load_json(STORAGE_PATH, [])
        self._event_pipeline = None
        self._data_ready = False

        # Mayor/election context - seeded from the last-known cache so the
        # UI has *something* to show before the first live election fetch
        # completes (or if that fetch ever fails - it's a separate
        # endpoint from the bazaar and can fail independently).
        self.mayor_info = load_json(MAYOR_CACHE_PATH, {})
        self.active_festivals = compute_active_festivals(self.mayor_info)
        self.paul_discount_active = paul_dungeon_discount_active(self.mayor_info)
        self.jerry_status = jerry_workshop_status()
        self.harvest_status = harvest_festival_status(mayor_info=self.mayor_info)
        self.oringo_status_info = oringo_status()
        self.year_of_pig_status_info = year_of_pig_status()
        self._recompute_active_event_keys()

        # How far ahead of a seasonal event's start to flag it as "upcoming"
        # and start tracking its related items (Settings-adjustable, default
        # 24h). Must be set before the first bridge_tick, which reads it.
        try:
            lead_hours = float(self.settings.get("event_lead_hours", DEFAULT_EVENT_LEAD_HOURS))
        except (TypeError, ValueError):
            lead_hours = DEFAULT_EVENT_LEAD_HOURS
        self.event_lead_seconds = max(1.0, lead_hours) * 3600.0
        self.upcoming_event_forecasts = []
        self._recompute_upcoming_forecasts()

        self.auto_refresh_enabled = bool(self.settings.get("auto_refresh_enabled", DEFAULT_AUTO_REFRESH_ENABLED))
        try:
            self.auto_refresh_minutes = max(MIN_AUTO_REFRESH_MINUTES,
                                             float(self.settings.get("auto_refresh_minutes", DEFAULT_AUTO_REFRESH_MINUTES)))
        except (TypeError, ValueError):
            self.auto_refresh_minutes = DEFAULT_AUTO_REFRESH_MINUTES
        self._auto_refresh_after_id = None

        self.sort_key = "profit_hr"
        self.sort_reverse = True
        self.event_sort_key = "confidence"
        self.event_sort_reverse = True
        self.view_mode = "dashboard"      # dashboard, full, event_engine, or storage
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
        self._check_previous_update_failure()
        self._check_previous_update_failure()
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
        # NOTE: this row used to also hold five separate Entry fields
        # (Purse/Sleep/Spread/MinVol/BuyBuffer) plus an Apply button, all
        # crammed into one row. On a non-maximized window those pushed the
        # row wider than the visible area - pack() doesn't wrap, so the
        # rightmost widgets (the Overnight Plan / Full List toggle) got
        # squeezed off past the edge of the window instead of actually
        # disappearing. Purse/Spread/Run Time are frequently-tuned enough
        # to earn a spot on the main window, so they're back - but on
        # their OWN row (params_bar, below) instead of sharing this one,
        # so this button row can never overflow again. The two
        # less-frequently-touched risk knobs (Min $Vol/day, Buy Buffer %)
        # plus the new Min Weekly Sales floor stay in the Settings dialog.
        top_bar_wrap = tk.Frame(self, bg=BG_DARK)
        top_bar_wrap.pack(fill="x")
        top_bar = ttk.Frame(top_bar_wrap, padding=(14, 14, 14, 14), style="TopBar.TFrame")
        top_bar.pack(fill="x")
        tk.Frame(top_bar_wrap, bg=ACCENT, height=2).pack(fill="x")
        self.top_bar_wrap = top_bar_wrap

        # StringVars for the trading parameters.
        self.purse_var = tk.StringVar(value=self.settings.get("purse", "10000000"))
        self.sleep_hours_var = tk.StringVar(value=self.settings.get("sleep_hours", str(DEFAULT_SLEEP_HOURS)))
        self.spread_var = tk.StringVar(value=self.settings.get("spread_n", str(DEFAULT_SPREAD_N)))
        self.risk_floor_var = tk.StringVar(value=self.settings.get("risk_floor", str(DEFAULT_PLAN_MIN_DAILY_VOLUME)))
        self.min_weekly_sales_var = tk.StringVar(
            value=self.settings.get("min_weekly_sales", str(DEFAULT_MIN_WEEKLY_SALES)))
        self.buy_buffer_var = tk.StringVar(value=self.settings.get("buy_buffer_pct", str(DEFAULT_BUY_BUFFER_PCT)))

        self.refresh_btn = ttk.Button(top_bar, text="\u21bb  Refresh Market Data", command=self.refresh)
        self.refresh_btn.pack(side="left")

        ttk.Button(top_bar, text="\u2699 Settings", style="Secondary.TButton",
                   command=self.open_settings).pack(side="left", padx=(10, 0))

        ttk.Button(top_bar, text="\U0001F3F7 Manage Categories", style="Secondary.TButton",
                   command=self.open_manage_categories).pack(side="left", padx=(10, 0))

        ttk.Button(top_bar, text="\U0001F4E6 + Add to Storage", style="Secondary.TButton",
                   command=self.open_manual_storage_dialog).pack(side="left", padx=(10, 0))

        view_frame = ttk.Frame(top_bar, style="TopBar.TFrame")
        view_frame.pack(side="right")
        self.dashboard_btn = ttk.Button(view_frame, text="\U0001F319 Overnight Plan",
                                         command=lambda: self.set_view("dashboard"))
        self.dashboard_btn.pack(side="left", padx=(0, 4))
        self.fulllist_btn = ttk.Button(view_frame, text="\u2261 Full List",
                                        command=lambda: self.set_view("full"))
        self.fulllist_btn.pack(side="left")
        self.event_engine_btn = ttk.Button(view_frame, text="\U0001F4C8 Event Engine",
                                            command=lambda: self.set_view("event_engine"))
        self.event_engine_btn.pack(side="left", padx=(4, 0))
        self.storage_btn = ttk.Button(view_frame, text="\U0001F4E6 Storage",
                                       command=lambda: self.set_view("storage"))
        self.storage_btn.pack(side="left", padx=(4, 0))

        # Row 1b: Purse / Spread / Run Time - its own row so it can never
        # crowd out the buttons above, and long enough on its own that a
        # small window just wraps naturally into extra vertical space
        # rather than clipping anything.
        params_bar = ttk.Frame(self, padding=(14, 0, 14, 10), style="TopBar.TFrame")
        params_bar.pack(fill="x")
        params_bar_divider = tk.Frame(self, bg=BORDER_SUBTLE, height=1)
        params_bar_divider.pack(fill="x")
        self.params_bar_wrap = params_bar_divider  # anchor for packing the category bar after this

        def _param_field(label_text, var):
            ttk.Label(params_bar, text=label_text, style="TopBarDim.TLabel").pack(side="left", padx=(0, 6))
            entry = ttk.Entry(params_bar, textvariable=var, width=12)
            entry.pack(side="left", padx=(0, 16))
            entry.bind("<Return>", lambda e: self.recompute_and_render())
            entry.bind("<FocusOut>", lambda e: self.recompute_and_render())
            return entry

        _param_field("Purse (coins):", self.purse_var)
        _param_field("Run Time (hrs):", self.sleep_hours_var)
        _param_field("Spread (# items):", self.spread_var)
        ttk.Button(params_bar, text="Apply", style="Secondary.TButton",
                   command=self.recompute_and_render).pack(side="left")

        # Row 2: shared search/filter controls for the browsable views.
        self.category_bar_wrap = ttk.Frame(self, padding=(14, 10, 14, 6))

        filter_row = ttk.Frame(self.category_bar_wrap)
        filter_row.pack(fill="x", pady=(0, 6))
        ttk.Label(filter_row, text="Search:").pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(filter_row, textvariable=self.search_var, width=18)
        search_entry.pack(side="left")
        search_entry.bind("<KeyRelease>", lambda e: self._on_search_key())

        self.sort_label = ttk.Label(filter_row, text="Sort by:")
        self.sort_label.pack(side="left", padx=(16, 6))
        self.sort_var = tk.StringVar(value=SORT_OPTIONS[0][0])
        self.sort_var_combo = ttk.Combobox(filter_row, textvariable=self.sort_var, state="readonly",
                                            values=[label for label, _ in SORT_OPTIONS], width=20)
        self.sort_var_combo.pack(side="left")
        self.sort_var_combo.bind("<<ComboboxSelected>>", lambda e: self._on_sort_change())

        self.sort_dir_btn = ttk.Button(filter_row, text="\u25bc Desc", style="Secondary.TButton",
                                        command=self._toggle_sort_dir)
        self.sort_dir_btn.pack(side="left", padx=6)

        # Event filter - lets you browse everything tagged for a given
        # seasonal event/Paul's discount, independent of whether that
        # event is live RIGHT NOW (that's what the header badge already
        # shows) - e.g. "what's affected by Mining Fiesta" even while
        # Cole isn't mayor, for planning ahead. Options are rebuilt from
        # whatever event_tags actually show up in the current flip data
        # (see _rebuild_event_filter_options), so it never offers an
        # event with zero matching items.
        self.event_filter_label = ttk.Label(filter_row, text="Event:")
        self.event_filter_label.pack(side="left", padx=(16, 6))
        self.event_filter_var = tk.StringVar(value=EVENT_FILTER_ALL)
        self.event_filter_combo = ttk.Combobox(filter_row, textvariable=self.event_filter_var,
                                                state="readonly", values=[EVENT_FILTER_ALL], width=20)
        self.event_filter_combo.pack(side="left")
        self.event_filter_combo.bind("<<ComboboxSelected>>", lambda e: self._on_event_filter_change())
        self._event_filter_label_to_key = {}

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

        # Summary card, with a colored accent stripe down the left edge
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
        if mode in ("full", "event_engine"):
            self.category_bar_wrap.pack(fill="x", after=self.params_bar_wrap)
        else:
            self.category_bar_wrap.pack_forget()
        is_event_engine = mode == "event_engine"
        self.category_scroll.pack_forget() if is_event_engine else self.category_scroll.pack(fill="x")
        self.event_filter_label.configure(text="Event type:" if is_event_engine else "Event:")
        event_sort_options = EVENT_ENGINE_SORT_OPTIONS if is_event_engine else SORT_OPTIONS
        current_key = self.event_sort_key if is_event_engine else self.sort_key
        current_label = next((label for label, key in event_sort_options if key == current_key),
                             event_sort_options[0][0])
        self.sort_var.set(current_label)
        self.sort_var_combo.configure(values=[label for label, _ in event_sort_options])
        reverse = self.event_sort_reverse if is_event_engine else self.sort_reverse
        self.sort_dir_btn.configure(text="\u25bc Desc" if reverse else "\u25b2 Asc")
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _refresh_view_buttons(self):
        self.dashboard_btn.configure(style="ViewActive.TButton" if self.view_mode == "dashboard" else "View.TButton")
        self.fulllist_btn.configure(style="ViewActive.TButton" if self.view_mode == "full" else "View.TButton")
        self.event_engine_btn.configure(style="ViewActive.TButton" if self.view_mode == "event_engine" else "View.TButton")
        self.storage_btn.configure(style="ViewActive.TButton" if self.view_mode == "storage" else "View.TButton")

    def get_all_categories(self):
        """Union of categories currently in use by flips + custom ones the
        user has created (which may not have any items yet)."""
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

        # "+ New" shortcut pill, visually distinct (outlined, dashed feel)
        add_pill = tk.Label(self.category_scroll.inner, text="+ New", font=FONT_PILL,
                             bg=BG_DARK, fg=ACCENT, padx=12, pady=7, cursor="hand2",
                             highlightbackground=ACCENT, highlightthickness=1)
        add_pill.pack(side="left", padx=(8, 4), pady=4)
        add_pill.bind("<Button-1>", lambda e: self.open_add_category())
        hoverable(add_pill, BG_DARK, ACCENT_SOFT, fg=ACCENT, hover_fg=ACCENT_HOVER)

    def _rebuild_event_filter_options(self):
        """Rebuilds the Event filter dropdown's options from whatever
        event_tags actually appear in the current flip data, so it only
        ever offers events with at least one matching item. If the
        previously-selected event no longer has any matches (e.g. after
        a refresh), falls back to "All Events" rather than silently
        filtering to nothing."""
        keys_present = set()
        for f in self.all_flips:
            keys_present.update(f.get("event_tags", []))
        # The Event Engine can have history even when today's live bazaar
        # tags do not include that event, so keep its event types filterable.
        keys_present.update(key for key in EVENT_BADGE_STYLE if key != "dungeon_supply")
        # Ensure new calendar-gated events are always filterable
        keys_present.update(["harvest_festival", "oringo", "year_of_pig"])

        label_to_key = {}
        for key in keys_present:
            label, _color = EVENT_BADGE_STYLE.get(key, (key, ACCENT))
            label_to_key[label] = key
        self._event_filter_label_to_key = label_to_key

        options = [EVENT_FILTER_ALL] + sorted(label_to_key)
        self.event_filter_combo.configure(values=options)
        if self.event_filter_var.get() not in options:
            self.event_filter_var.set(EVENT_FILTER_ALL)

    def _on_event_filter_change(self):
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def select_category(self, cat):
        self.category_var.set(cat)
        self._rebuild_category_bar()
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    # -- settings -----------------------------------------------------------
    def open_settings(self):
        SettingsDialog(self)

    def _schedule_auto_refresh(self):
        """(Re)schedules the next automatic refresh. Always cancels any
        pending one first, so changing the interval or toggling it off in
        Settings can't leave a stray timer still firing on the old
        cadence."""
        if self._auto_refresh_after_id is not None:
            self.after_cancel(self._auto_refresh_after_id)
            self._auto_refresh_after_id = None
        if self.auto_refresh_enabled:
            interval_ms = int(max(MIN_AUTO_REFRESH_MINUTES, self.auto_refresh_minutes) * 60 * 1000)
            self._auto_refresh_after_id = self.after(interval_ms, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self.refresh()
        self._schedule_auto_refresh()

    def _check_previous_update_failure(self):
        """If the last self-update attempt failed, the batch script leaves a
        log behind in the scratch folder - surface it once, then clean it up
        so it doesn't reappear on every future launch."""
        work_dir = os.path.join(tempfile.gettempdir(), "BazaarFlipperUpdate")
        log_path = os.path.join(work_dir, "update_failed.log")
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    message = f.read().strip()
            except OSError:
                message = "Unknown error (couldn't read log)."
            messagebox.showerror(
                "Update Failed",
                f"The last update attempt failed:\n\n{message}\n\n"
                f"The app has continued running your previous version. "
                f"Try 'Check for Updates' again, or update manually from GitHub.")
            shutil.rmtree(work_dir, ignore_errors=True)

    # -- update checker -------------------------------------------------
    def _check_for_update_async(self, silent=False):
        """Hits GitHub in a background thread (same pattern as the bazaar
        fetch) so a slow/offline check never freezes the UI. silent=True
        (used on startup) says nothing on failure or "already current" -
        only a real update pops the status-bar notice. silent=False (the
        Settings 'Check for Updates' button) always reports back."""
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
        """Stores what we found and lights up the clickable status-bar
        banner. If the release has a .exe asset attached, clicking it
        downloads and auto-installs the update (see _apply_update). If
        not (e.g. a release published without the built exe attached),
        clicking just opens the release page instead, same as before."""
        self._update_release_url = release_url
        self._update_asset_url = asset_url
        if asset_url:
            self.update_available_var.set(f"\u2b06 Update available: {latest_version} (click to install)")
        else:
            self.update_available_var.set(f"\u2b06 Update available: {latest_version} (click to download)")
        self.update_lbl.pack(side="left", padx=(14, 0))

    def _on_update_click(self, _event=None):
        """Click handler for the status-bar update banner. Prefers the
        one-click auto-install path when a downloadable .exe asset was
        found; otherwise falls back to just opening the release page."""
        if getattr(self, "_update_asset_url", None):
            self._apply_update(self._update_asset_url)
        elif getattr(self, "_update_release_url", None):
            webbrowser.open(self._update_release_url)

    def _apply_update(self, download_url):
        """Downloads the new release zip in the background, extracts it
        to a scratch folder under %TEMP%, then hands off to a small batch
        script that waits for this process to fully exit, mirrors the
        extracted folder over the current install folder, and relaunches
        the app - a running Windows exe (and its DLLs sitting next to it)
        can't be overwritten while still open, so this two-step handoff
        (external script does the copy after we exit) is the standard way
        self-updating apps work.

        This app is built with PyInstaller's --onedir mode, so "the app"
        is a whole folder (exe + its DLLs/dependencies), not a single
        file - that's why this downloads+extracts a .zip of that folder
        rather than swapping one .exe, which is what a --onefile build
        would do instead.

        Only applies when running as the packaged .exe (sys.frozen). If
        you're running the raw .py file there's no installed folder to
        replace, so this just falls back to opening the browser instead.

        NOTE: this only ever touches the INSTALL folder next to the exe
        (exe_dir below) - it never writes into APP_DATA_DIR, so
        settings/blacklist/category overrides/price history/Storage all
        survive an update untouched. See the STORAGE_PATH comment above."""
        if not getattr(sys, "frozen", False):
            webbrowser.open(self._update_release_url)
            return

        self.update_lbl.unbind("<Button-1>")
        self.update_available_var.set("\u2b07 Downloading update...")

        def worker():
            current_exe = sys.executable
            exe_name = os.path.basename(current_exe)

            # Scratch space under %TEMP% - deliberately NOT inside the
            # install folder, since we're about to need to fully replace
            # that folder's contents and don't want the scratch files
            # themselves to be part of what gets mirrored over.
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

                # The zip may contain the app folder directly, or nested
                # one level deep (e.g. if it was made by right-clicking
                # the dist folder and choosing "Send to > Compressed
                # folder", which wraps it in an extra folder of the same
                # name) - so search for wherever the exe actually ended
                # up rather than assuming a fixed layout.
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
        """Writes a tiny batch script (placed in %TEMP%, NOT inside the
        install folder, since that folder is about to be overwritten out
        from under it) that:
          1. polls (via tasklist) until THIS exe's process has actually
             exited, since we're about to close it a moment after
             launching this script and its files can't be overwritten
             while it's still open,
          2. mirrors the extracted update folder (source_dir) over the
             current install folder (robocopy /MIR - this is a whole
             --onedir folder of files, not a single exe, so a plain
             "move" of one file isn't enough; robocopy also removes any
             files an old version left behind that the new one no longer
             ships),
          3. relaunches the app from its original path,
          4. cleans up the temp download/extract folder and deletes
             itself.
        Launched detached/console-less (CREATE_NO_WINDOW) so no console
        flashes up, and so it keeps running after this Python process
        exits right after. Windows-only, matching the .exe packaging
        this whole update flow is built around."""
        exe_dir = os.path.dirname(current_exe)
        exe_name = os.path.basename(current_exe)
        bat_path = os.path.join(work_dir, "_apply_update.bat")

        bat_contents = [
            "@echo off\r\n",
            f'echo Starting update process... > "{work_dir}\\bat_debug.log"\r\n',
            ":wait\r\n",
            f'tasklist /fi "imagename eq {exe_name}" 2>nul | find /i "{exe_name}" >nul 2>&1\r\n',
            "if not errorlevel 1 (\r\n",
            "    timeout /t 1 /nobreak >nul 2>&1\r\n",
            "    goto wait\r\n",
            ")\r\n",
            f'echo Process exited, waiting 2s... >> "{work_dir}\\bat_debug.log"\r\n',
            "timeout /t 2 /nobreak >nul 2>&1\r\n",
            f'echo Running robocopy from "{source_dir}" to "{exe_dir}"... >> "{work_dir}\\bat_debug.log"\r\n',
            f'robocopy "{source_dir}" "{exe_dir}" /E /MIR /R:3 /W:1 >> "{work_dir}\\bat_debug.log" 2>&1\r\n',
            f'echo Robocopy exit code: %errorlevel% >> "{work_dir}\\bat_debug.log"\r\n',
            "if %errorlevel% geq 8 (\r\n",
            f'    echo Update failed - robocopy error %errorlevel% > "{work_dir}\\update_failed.log"\r\n',
            "    exit /b 1\r\n",
            ")\r\n",
            f'echo Starting new exe... >> "{work_dir}\\bat_debug.log"\r\n',
            "timeout /t 3 /nobreak >nul 2>&1\r\n",
            f'start "" "{current_exe}"\r\n',
]

        with open(bat_path, "w") as f:
            f.writelines(bat_contents)

        subprocess.Popen(
            ["cmd", "/c", bat_path],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )

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

    # -- event-driven recommendations (event_price_engine) -----------------
    def open_event_recommendations(self):
        """Compatibility entry point for older menu bindings.

        Event recommendations now live in the main Event Engine view rather
        than opening a separate Toplevel window.
        """
        self.set_view("event_engine")

    def _get_event_pipeline(self):
        """Lazily builds (and caches) a Pipeline facade from event_price_engine,
        pointed at the exact same DuckDB store bazaar_bridge.bridge_tick() has
        already been writing to every refresh (self._event_price_db, set
        there in _on_fetch_success) - reusing that already-resolved path
        instead of re-deriving it, so this can never end up pointed at a
        different DB file than the one actually being fed live data. Falls
        back to the same default path bazaar_bridge would use if no refresh
        has completed yet (e.g. dialog opened before the first fetch)."""
        from event_price_engine import Pipeline, bazaar_bridge as _bridge

        existing_db = getattr(self, "_event_price_db", None)
        db_path = (existing_db.path if existing_db is not None
                   else os.path.join(APP_DATA_DIR, _bridge.DB_FILENAME))

        if self._event_pipeline is None or self._event_pipeline.db.path != db_path:
            if self._event_pipeline is not None:
                self._event_pipeline.close()
            self._event_pipeline = Pipeline(db_path)
        return self._event_pipeline

    def _gather_event_sections(self):
        """Builds the data displayed by the Event Engine, one
        section per tracked seasonal event type. Calls only the existing
        event_price_engine facade (Pipeline.generate_recommendation +
        Database.load_event_instances/load_items_for_event_type) - all the
        actual scoring/indicator/event-study logic still lives exactly
        where it already did (scoring.py/indicators.py/event_study.py via
        pipeline.py); this only assembles and returns it for display.

        For each event type, the most recent recorded occurrence (open or
        closed) is used as the anchor instance, and every prior occurrence
        before it is what the recommendation is scored against - so a
        currently-live event gets a live recommendation compared to its
        own past occurrences, and a just-ended one still shows its last
        result until the next occurrence starts."""
        from event_price_engine import bazaar_bridge as _bridge

        pipeline_obj = self._get_event_pipeline()
        as_of_ts = int(time.time())
        event_types = sorted(_bridge.TRACKED_FESTIVAL_KEYS | {"jerry_workshop"})
        # Also include oringo/harvest_festival/year_of_pig even if
        # not in TRACKED_FESTIVAL_KEYS (they are, but belt-and-suspenders)
        event_types = sorted(set(event_types) | {"harvest_festival", "oringo", "year_of_pig"})

        sections = []
        for event_type in event_types:
            all_instances = pipeline_obj.db.load_event_instances(event_type)
            if not all_instances:
                continue  # never actually occurred yet - nothing to show

            current_instance = all_instances[-1]
            historical_count = max(0, len(all_instances) - 1)

            item_ids = pipeline_obj.db.load_items_for_event_type(event_type)
            item_rows = []
            for item_id in item_ids:
                try:
                    rec = pipeline_obj.generate_recommendation(item_id, current_instance, as_of_ts)
                except Exception:
                    rec = None
                if rec is not None:
                    item_rows.append({"item_id": item_id, "recommendation": rec})

            sections.append({
                "event_type": event_type,
                "current_instance": current_instance,
                "historical_count": historical_count,
                "items": item_rows,
            })
        return sections

    def _gather_upcoming_event_sections(self):
        """Pre-event sections for events starting within the lead window
        (self.upcoming_event_forecasts). For each, its tracked items are scored
        as a PRE-EVENT signal via the engine's synthetic-future-instance path
        (Pipeline.generate_recommendation_for_forecast) - "today" lands at a
        negative relative day, i.e. right where the historical buy window sits,
        so the same scorer that grades a live event grades "should I position
        for the one that's coming." Events with no prior recorded occurrence
        yet still appear (as a bare countdown) so the heads-up is visible even
        before there's any history to score against. Fails open to []."""
        upcoming = getattr(self, "upcoming_event_forecasts", [])
        if not upcoming:
            return []
        try:
            pipeline_obj = self._get_event_pipeline()
        except Exception:
            return []

        as_of_ts = int(time.time())
        sections = []
        for fc in upcoming:
            event_type = fc["event_key"]
            next_start_ts = fc["next_start_ts"]
            try:
                prior_instances = pipeline_obj.db.load_event_instances(event_type)
                item_ids = pipeline_obj.db.load_items_for_event_type(event_type)
            except Exception:
                prior_instances, item_ids = [], []

            item_rows = []
            for item_id in item_ids:
                try:
                    rec = pipeline_obj.generate_recommendation_for_forecast(
                        item_id, event_type, next_start_ts, as_of_ts)
                except Exception:
                    rec = None
                if rec is not None:
                    item_rows.append({"item_id": item_id, "recommendation": rec})

            sections.append({
                "event_type": event_type,
                "next_start_ts": next_start_ts,
                "seconds_until": fc["seconds_until"],
                "prior_count": len(prior_instances),
                "tracked_item_count": len(item_ids),
                "items": item_rows,
            })
        return sections

    def get_price_trend(self, product_id, lookback_hours=24):
        """Rising/Falling/Flat based purely on this item's own local price
        history (self.price_history) - independent of the event engine,
        so it works immediately with no event-occurrence requirement.
        Compares the oldest sample within the lookback window against the
        newest sample overall. Returns None if there isn't enough history
        yet to say anything."""
        if not product_id:
            return None
        samples = self.price_history.get(product_id, [])
        if len(samples) < 2:
            return None

        now = time.time()
        cutoff = now - lookback_hours * 3600
        in_window = [s for s in samples if s[0] >= cutoff]
        if len(in_window) < 2:
            in_window = samples[-2:]

        oldest = in_window[0]
        newest = samples[-1]

        old_price = (oldest[1] + oldest[2]) / 2
        new_price = (newest[1] + newest[2]) / 2
        if old_price <= 0:
            return None

        pct_change = ((new_price - old_price) / old_price) * 100
        if pct_change > 3:
            direction = "rising"
        elif pct_change < -3:
            direction = "falling"
        else:
            direction = "flat"

        return {"direction": direction, "pct_change": round(pct_change, 1)}

    def get_storage_recommendation(self, entry):
        """Looks up the current Buy/Hold/Sell recommendation for a Storage
        entry, if any. Only works for entries with a product_id (i.e. saved
        from a live flip card, or a manual entry that matched a known item
        name) - manual entries with no resolvable product_id return None
        rather than a guess. If the item is tagged relevant to more than
        one seasonal event, returns whichever has the higher confidence;
        does not average or combine them."""
        product_id = entry.get("product_id")
        if not product_id:
            return None
        event_keys = tag_event_relevance(product_id)
        if not event_keys:
            return None

        try:
            pipeline_obj = self._get_event_pipeline()
        except Exception:
            return None

        best = None
        as_of_ts = int(time.time())
        for event_type in event_keys:
            try:
                instances = pipeline_obj.db.load_event_instances(event_type)
                if not instances:
                    continue
                rec = pipeline_obj.generate_recommendation(product_id, instances[-1], as_of_ts)
            except Exception:
                rec = None
            if rec is None:
                continue
            if best is None or max(rec.buy_confidence, rec.sell_confidence) > \
                    max(best.buy_confidence, best.sell_confidence):
                best = rec
        return best

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

    # -- manual categorization (per-item, via the "Set Category" button in a box) --
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

    # -- storage ----------------------------------------------------------
    # Stored entirely under APP_DATA_DIR (see STORAGE_PATH above), the same
    # per-user folder every other *_PATH file already lives in - so it
    # persists across both ordinary app restarts AND the built-in
    # self-updater (_apply_update/robocopy only ever mirrors files into
    # the install folder next to the .exe, never into APP_DATA_DIR).
    def _next_storage_entry_id(self):
        """A collision-safe id for a new storage entry - millisecond
        timestamp plus the current list length as a tiebreaker, so two
        entries added within the same millisecond (e.g. rapid clicking)
        still get distinct ids."""
        return f"{int(time.time() * 1000)}_{len(self.storage)}"

    def save_storage(self):
        save_json(STORAGE_PATH, self.storage)

    def add_flip_to_storage(self, flip):
        """Snapshots the CURRENT numbers off a live flip card into a
        standalone Storage entry. This is a copy, not a live link - the
        bazaar keeps moving, so what you saved is what you saw at the
        moment you clicked "Add to Storage," not an auto-updating quote.

        Quantity mirrors whatever the card was actually showing: the
        Overnight Plan's planned buy quantity ("units") in portfolio mode,
        or the purse-limited achievable quantity ("achievable_units") in
        Full List mode - not a flat 1."""
        quantity = flip.get("units")
        if not quantity:
            quantity = flip.get("achievable_units")
        quantity = max(1, int(quantity or 1))

        entry = {
            "entry_id": self._next_storage_entry_id(),
            "source": "flip",
            "product_id": flip.get("id"),
            "item": flip.get("item"),
            "category": flip.get("category"),
            "buy_at": flip.get("buy_order_at"),
            "sell_at": flip.get("sell_offer_at"),
            "profit": flip.get("profit"),
            "margin": flip.get("margin"),
            "quantity": quantity,
            "notes": "",
            "added_ts": time.time(),
        }
        self.storage.append(entry)
        self.save_storage()
        if self.view_mode == "storage":
            self.recompute_and_render()

    def add_manual_storage_entry(self, data):
        """Adds a hand-typed Storage entry (see ManualStorageDialog).
        Profit/margin are only computed when both a buy and sell price
        were given - otherwise this is stored as a plain note."""
        buy_at = data.get("buy_at")
        sell_at = data.get("sell_at")
        profit = None
        margin = None
        if buy_at is not None and sell_at is not None and buy_at > 0:
            post_tax = sell_at * (1 - BAZAAR_TAX)
            profit = round(post_tax - buy_at, 1)
            margin = round((profit / buy_at) * 100, 1)

        # Best-effort match against known bazaar items so a manually-typed
        # entry can still get an event-engine recommendation later. Only
        # matches on exact (case-insensitive) display name - doesn't guess
        # on partial/fuzzy matches, since a wrong match would silently show
        # the wrong item's signal.
        typed_name = data.get("item", "").strip().lower()
        match = next((f for f in self.all_flips if f["item"].lower() == typed_name), None)
        product_id = match["id"] if match else None

        entry = {
            "entry_id": self._next_storage_entry_id(),
            "source": "manual",
            "product_id": product_id,
            "item": data.get("item", "Unknown Item"),
            "category": data.get("category") or "Manual",
            "buy_at": buy_at,
            "sell_at": sell_at,
            "profit": profit,
            "margin": margin,
            "quantity": data.get("quantity", 1),
            "notes": data.get("notes", ""),
            "added_ts": time.time(),
        }
        self.storage.append(entry)
        self.save_storage()
        if self.view_mode == "storage":
            self.recompute_and_render()

    def remove_from_storage(self, entry_id):
        self.storage = [e for e in self.storage if e.get("entry_id") != entry_id]
        self.save_storage()
        self.recompute_and_render()

    def open_manual_storage_dialog(self):
        ManualStorageDialog(self, self.get_all_categories(), self.add_manual_storage_entry)

    # -- seasonal events / mayor -----------------------------------------
    def _recompute_active_event_keys(self):
        """Rebuilds the set of event_keys that are ACTUALLY live right now
        (as opposed to merely possible under the current mayor's term) -
        this is what FlipCard badges/filters against. dungeon_supply is
        deliberately excluded here since it's driven by paul_discount_active
        directly, not a festival window."""
        keys = {f["event_key"] for f in self.active_festivals if f["active_now"]}
        if self.jerry_status.get("active"):
            keys.add("jerry_workshop")
        if getattr(self, "harvest_status", {}).get("active"):
            keys.add("harvest_festival")
        if getattr(self, "oringo_status_info", {}).get("active"):
            keys.add("oringo")
        if getattr(self, "year_of_pig_status_info", {}).get("active"):
            keys.add("year_of_pig")
        self.active_event_keys = keys

    def _recompute_upcoming_forecasts(self):
        """Refresh the list of events starting within the lead window (default
        24h) and not already live - drives both the status-bar heads-up and
        the Event Engine's Upcoming section. Fails open to an empty list so a
        forecasting hiccup never blanks the rest of the UI."""
        try:
            self.upcoming_event_forecasts = upcoming_events_within(
                mayor_info=self.mayor_info,
                lead_seconds=getattr(self, "event_lead_seconds", DEFAULT_EVENT_LEAD_HOURS * 3600),
                active_event_keys=getattr(self, "active_event_keys", set()),
            )
        except Exception:
            traceback.print_exc()
            self.upcoming_event_forecasts = []

    def _market_context(self):
        return {
            "active_event_keys": getattr(self, "active_event_keys", set()),
            "paul_discount_active": self.paul_discount_active,
        }

    def _events_status_text(self):
        """Short human-readable summary of what's currently live, shown in
        the status bar so the Overnight Plan's event badges don't come out
        of nowhere."""
        parts = []
        mayor_name = self.mayor_info.get("name")
        if mayor_name:
            parts.append(f"Mayor: {mayor_name}")
        live_labels = [f["label"] for f in self.active_festivals if f["active_now"]]
        if self.jerry_status.get("active"):
            live_labels.append("Jerry's Workshop")
        if self.paul_discount_active:
            live_labels.append("Paul \u201320% Chests")
        if getattr(self, "harvest_status", {}).get("active"):
            live_labels.append("Harvest Festival")
        if getattr(self, "oringo_status_info", {}).get("active"):
            pet = self.oringo_status_info.get("current_pet", "?")
            live_labels.append(f"Traveling Zoo ({pet})")
        if getattr(self, "year_of_pig_status_info", {}).get("active"):
            live_labels.append("Year of the Pig")
        if live_labels:
            parts.append("Active: " + ", ".join(live_labels))

        # Heads-up for events crossing into the lead window (~24h out by
        # default) but not yet live, so their related items can be pre-tracked.
        upcoming = getattr(self, "upcoming_event_forecasts", [])
        if upcoming:
            up_labels = []
            for fc in upcoming:
                label, _color = EVENT_BADGE_STYLE.get(fc["event_key"], (fc["event_key"], ACCENT))
                up_labels.append(f"{label} in {fmt_hours(fc['seconds_until'] / 3600.0)}")
            parts.append("\u23f3 Upcoming: " + ", ".join(up_labels))
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

        # Election data is a separate endpoint from the bazaar and changes
        # far less often (only when a new mayor is elected, every ~5 real
        # days) - a failure here shouldn't block the bazaar refresh that
        # everything else depends on, so it's fetched in its own try/except
        # and just falls back to whatever was last cached on disk.
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
        self._data_ready = True
        self.status_var.set("Bazaar data loaded successfully")
        self.refresh_btn.state(["!disabled"])

        # snapshot_ms is Hypixel's OWN capture time for this data (ms since
        # epoch), not our fetch time - the two can differ if their backend
        # served a cached response. snapshot_local_ref is our local clock
        # at the moment we received it, so the ticking "Xs old" label below
        # stays accurate between refreshes without re-hitting the API. This
        # only needs snapshot_ms (already in hand above), so it's pulled up
        # here rather than sitting after the enrichment block below.
        self.last_snapshot_ms = snapshot_ms
        self.last_snapshot_local_ref = time.time()
        self._tick_snapshot_age()

        # Everything below is enrichment on top of the flip data already
        # stored above - mayor/festival context, the event_price_engine
        # bridge, category/event-filter rebuilds. This used to run
        # unguarded, so an exception anywhere in here (e.g. bridge_tick
        # hitting the event_price_engine DB) aborted the rest of this
        # method BEFORE reaching the after_idle(recompute_and_render) call
        # at the bottom. Tk swallows exceptions raised inside an after()
        # callback silently (just prints to stderr, no dialog), so the
        # Overnight Plan was left sitting on "Loading live bazaar data..."
        # forever with no visible error - only "fixed" by switching to
        # another view and back, since that calls recompute_and_render
        # directly via set_view() and never goes through this method at
        # all. Wrapping it means a failure here can no longer swallow the
        # render that's supposed to happen on every refresh.
        try:
            if mayor_info:
                self.mayor_info = mayor_info
            self.active_festivals = compute_active_festivals(self.mayor_info)
            self.paul_discount_active = paul_dungeon_discount_active(self.mayor_info)
            self.jerry_status = jerry_workshop_status()
            self.harvest_status = harvest_festival_status(mayor_info=self.mayor_info)
            self.oringo_status_info = oringo_status()
            self.year_of_pig_status_info = year_of_pig_status()
            self._recompute_active_event_keys()
            self._recompute_upcoming_forecasts()
            self.events_var.set(self._events_status_text())
        except Exception:
            traceback.print_exc()

        try:
            from event_price_engine import bazaar_bridge
            bazaar_bridge.bridge_tick(self)
        except Exception:
            traceback.print_exc()

        try:
            self._rebuild_category_bar()
            self._rebuild_event_filter_options()
        except Exception:
            traceback.print_exc()

        self._full_list_shown = self._full_list_page_size
        # Let Tk finish laying out the initial view before drawing cards.
        # This prevents the first Overnight Plan render from racing the
        # asynchronous fetch/layout sequence on startup. Runs
        # unconditionally (outside the try above) so it can never again be
        # silently skipped.
        self.after_idle(self.recompute_and_render)

    def _tick_snapshot_age(self):
        """Keeps the 'Hypixel snapshot: Xs old' label live between fetches,
        so staleness is visible even if you sit on the same screen for a
        while rather than only right after a refresh."""
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

    def _get_min_weekly_sales(self):
        try:
            return max(0.0, float(self.min_weekly_sales_var.get().replace(",", "")))
        except ValueError:
            messagebox.showwarning("Invalid Min Weekly Sales",
                                    "Enter a number for the minimum weekly unit sales.")
            return DEFAULT_MIN_WEEKLY_SALES

    def _get_buy_buffer_pct(self):
        try:
            return max(0.0, float(self.buy_buffer_var.get().replace(",", "").replace("%", "")))
        except ValueError:
            messagebox.showwarning("Invalid Buy Buffer %",
                                    "Enter a number for the buy-order buffer percentage.")
            return DEFAULT_BUY_BUFFER_PCT

    def _filtered_flips(self):
        # Blacklisted items are excluded everywhere - both the Overnight
        # Plan and the Full List are built from this same filtered set.
        flips = [f for f in self.all_flips if f["id"] not in self.blacklist]

        if self.view_mode == "full":
            selected = self.category_var.get()
            if selected not in (ALL_CATEGORIES, "", None):
                flips = [f for f in flips if f["category"] == selected]

            selected_event_label = self.event_filter_var.get()
            if selected_event_label != EVENT_FILTER_ALL:
                event_key = self._event_filter_label_to_key.get(selected_event_label)
                if event_key:
                    flips = [f for f in flips if event_key in f.get("event_tags", [])]

        if self.view_mode == "full":
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
        if not self._data_ready:
            self._render_loading_state()
            return
        if self.view_mode == "event_engine":
            self._render_event_engine()
            return
        if self.view_mode == "storage":
            self._render_storage()
            return
        purse = self._get_purse()
        buffer_pct = self._get_buy_buffer_pct()
        buffered = apply_buy_buffer(self._filtered_flips(), buffer_pct)
        flips = compute_purse_metrics(buffered, purse)
        if self.view_mode == "dashboard":
            self._render_overnight_plan(flips, purse)
        else:
            self._render_full_list(flips, purse)

    # -- sorting (Full List only) --------------------------------------------
    def _on_sort_change(self):
        label = self.sort_var.get()
        options = EVENT_ENGINE_SORT_OPTIONS if self.view_mode == "event_engine" else SORT_OPTIONS
        key = next((k for l, k in options if l == label), options[0][1])
        if self.view_mode == "event_engine":
            self.event_sort_key = key
        else:
            self.sort_key = key
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _toggle_sort_dir(self):
        if self.view_mode == "event_engine":
            self.event_sort_reverse = not self.event_sort_reverse
            reverse = self.event_sort_reverse
        else:
            self.sort_reverse = not self.sort_reverse
            reverse = self.sort_reverse
        self.sort_dir_btn.configure(text="\u25bc Desc" if reverse else "\u25b2 Asc")
        self._full_list_shown = self._full_list_page_size
        self.recompute_and_render()

    def _show_more_full_list(self):
        self._full_list_shown += self._full_list_page_size
        self.recompute_and_render()

    # -- rendering ----------------------------------------------------------
    def _clear_cards(self):
        for child in self.cards_scroll.inner.winfo_children():
            child.destroy()

    def _render_loading_state(self):
        """Render a real initial state instead of leaving an empty list while
        the first background market request is still in flight."""
        if not hasattr(self, "cards_scroll"):
            return
        self._clear_cards()
        self.card_title.configure(text="Overnight Plan")
        self.card_body.configure(text="Loading live bazaar data. Your first plan will appear automatically.")
        self.count_var.set("")

    def _render_storage(self):
        """Renders the Storage tab: everything saved via "Add to Storage"
        on a flip card, plus every manually-typed entry, newest first.
        Search (from the shared filter row, when visible) matches on item
        name/category/notes."""
        self._clear_cards()
        self.card_title.configure(text=f"Storage \u2014 {len(self.storage)} item(s)")
        self.card_body.configure(
            text="Items you've pinned for later, either saved straight off a flip card or typed in "
                 "by hand. These are a snapshot of the numbers at the time they were added, not a "
                 "live quote - refresh the Full List and re-add if you want the current price. "
                 "Where available, a Buy/Hold/Sell badge from the Event Engine is shown next to "
                 "each item - tap it for the reasoning. Saved to your local user data folder, so "
                 "this list survives app restarts and updates.")

        query = self.search_var.get().strip().lower()
        entries = list(self.storage)
        if query:
            entries = [e for e in entries
                       if query in (e.get("item") or "").lower()
                       or query in (e.get("category") or "").lower()
                       or query in (e.get("notes") or "").lower()]
        entries.sort(key=lambda e: e.get("added_ts", 0), reverse=True)

        if not entries:
            empty_text = ("Nothing in Storage yet. Tap \"Add to Storage\" on any item in the "
                           "Overnight Plan or Full List, or use \"+ Add to Storage\" up top for a "
                           "manual entry." if not self.storage else
                           "No storage items match your search.")
            tk.Label(self.cards_scroll.inner, text=empty_text, font=FONT_MAIN, bg=BG_DARK,
                     fg=TEXT_DIM, wraplength=1000, justify="left").pack(anchor="w", padx=4, pady=12)
        else:
            for entry in entries:
                rec = self.get_storage_recommendation(entry)
                trend = self.get_price_trend(entry.get("product_id"))
                card = StorageCard(self.cards_scroll.inner, entry, self.remove_from_storage,
                                    recommendation=rec, trend=trend)
                card.pack(fill="x", pady=1)

        self.count_var.set(f"Showing {len(entries)} of {len(self.storage)} storage item(s)")

    def _render_event_engine(self):
        """Render the event engine inside the shared content frame.

        The engine remains the single source of recommendation/scoring data;
        this view only filters, sorts, and presents those recommendations.
        """
        self._clear_cards()
        self.card_title.configure(text="Event Engine")
        self.card_body.configure(
            text="Event-driven Buy / Hold / Sell signals built from recorded seasonal occurrences. "
                 "Events starting within your lead window (Settings, default 24h) show first as "
                 "⏳ Upcoming, with pre-event signals for their tracked items. "
                 "Confidence is green at 75%+, amber at 50–74%, and red below 50%. "
                 "Use search, event type, and sorting to focus the list.")
        try:
            sections = self._gather_event_sections()
        except Exception as exc:
            self.count_var.set("Event analysis unavailable")
            tk.Label(self.cards_scroll.inner, text=f"Couldn't load event analysis: {exc}",
                     font=FONT_MAIN, bg=BG_DARK, fg=ACCENT_RED).pack(anchor="w", padx=4, pady=12)
            return

        selected_label = self.event_filter_var.get()
        selected_key = self._event_filter_label_to_key.get(selected_label)
        query = self.search_var.get().strip().lower()
        if selected_key:
            sections = [s for s in sections if s["event_type"] == selected_key]

        # Upcoming (pre-event) sections render first - events starting within
        # the lead window, with pre-event signals for their tracked items. The
        # header (countdown) shows even when an event has no rows yet, so the
        # heads-up is visible before any history exists to score.
        upcoming_sections = self._gather_upcoming_event_sections()
        if selected_key:
            upcoming_sections = [s for s in upcoming_sections if s["event_type"] == selected_key]
        upcoming_shown = 0
        for section in upcoming_sections:
            rows = [row for row in section["items"] if not query or query in row["item_id"].lower()]
            rows.sort(key=self._event_row_sort_value, reverse=self.event_sort_reverse)
            self._render_upcoming_event_section(section, rows)
            upcoming_shown += 1

        shown = 0
        for section in sections:
            rows = [row for row in section["items"] if not query or query in row["item_id"].lower()]
            if not rows:
                continue
            rows.sort(key=self._event_row_sort_value, reverse=self.event_sort_reverse)
            self._render_event_section(section, rows)
            shown += len(rows)

        if not shown and not upcoming_shown:
            tk.Label(self.cards_scroll.inner,
                     text="No event recommendations match the current search or filter. "
                          "Historical signals appear after an event has recorded enough price data.",
                     font=FONT_MAIN, bg=BG_DARK, fg=TEXT_DIM, wraplength=1000,
                     justify="left").pack(anchor="w", padx=4, pady=12)
        count_text = f"Showing {shown} event recommendation(s)"
        if upcoming_shown:
            count_text += f" · {upcoming_shown} upcoming"
        self.count_var.set(count_text)

    def _event_row_sort_value(self, row):
        rec = row["recommendation"]
        if self.event_sort_key == "item":
            return row["item_id"].lower()
        if self.event_sort_key == "action":
            return rec.action if rec else "hold"
        if self.event_sort_key == "movement":
            return rec.expected_appreciation if rec else 0.0
        if rec is None:
            return 0.0
        return max(rec.buy_confidence, rec.sell_confidence)

    def _render_upcoming_event_section(self, section, rows):
        """Header + pre-event item signals for an event that hasn't started yet
        but is inside the lead window. Visually distinct from
        _render_event_section (live/past occurrences) by its countdown and
        amber "upcoming" accent."""
        event_type = section["event_type"]
        label, color = EVENT_BADGE_STYLE.get(event_type, (event_type, ACCENT))
        starts_in = fmt_hours(section["seconds_until"] / 3600.0)
        start_local = time.strftime("%Y-%m-%d %H:%M", time.localtime(section["next_start_ts"]))

        wrap = tk.Frame(self.cards_scroll.inner, bg=BG_DARK)
        wrap.pack(fill="x", pady=(4, 12))
        heading = tk.Frame(wrap, bg=BG_PANEL)
        heading.pack(fill="x")
        tk.Frame(heading, bg=ACCENT_YELLOW, width=4).pack(side="left", fill="y")
        tk.Label(heading, text=f"⏳ {label}", font=FONT_SUBHEAD, bg=BG_PANEL,
                 fg=color).pack(side="left", padx=(10, 6), pady=9)
        tk.Label(heading, text=f"starts in ~{starts_in}  ·  {start_local}", font=FONT_MAIN,
                 bg=BG_PANEL, fg=ACCENT_YELLOW).pack(side="left", pady=9)
        tk.Label(heading, text=f"tracking {section['tracked_item_count']} item(s) · "
                 f"{section['prior_count']} prior occurrence(s)", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_FAINT).pack(side="right", padx=10, pady=9)

        if not rows:
            note = ("Pre-event signals appear once this event has at least one recorded past "
                    "occurrence to compare against - its items are being tracked in the meantime."
                    if section["prior_count"] == 0 else
                    "No pre-event signals match the current search.")
            tk.Label(wrap, text=f"    {note}", font=FONT_MAIN, bg=BG_DARK, fg=TEXT_DIM,
                     wraplength=1000, justify="left").pack(anchor="w", pady=(2, 4))
        else:
            for row in rows:
                EventItemCard(wrap, row["item_id"], row["recommendation"]).pack(fill="x", pady=(1, 0))

    def _render_event_section(self, section, rows):
        event_type = section["event_type"]
        label, color = EVENT_BADGE_STYLE.get(event_type, (event_type, ACCENT))
        current = section["current_instance"]
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(current.start_ts))
        state = "ongoing" if not current.end_ts else "ended"

        wrap = tk.Frame(self.cards_scroll.inner, bg=BG_DARK)
        wrap.pack(fill="x", pady=(4, 12))
        heading = tk.Frame(wrap, bg=BG_PANEL)
        heading.pack(fill="x")
        tk.Frame(heading, bg=color, width=4).pack(side="left", fill="y")
        tk.Label(heading, text=label, font=FONT_SUBHEAD, bg=BG_PANEL, fg=color).pack(side="left", padx=(10, 6), pady=9)
        tk.Label(heading, text=f"Started {started} · {state}", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_DIM).pack(side="left", pady=9)
        tk.Label(heading, text=f"{section['historical_count']} prior occurrence(s)", font=FONT_MAIN,
                 bg=BG_PANEL, fg=TEXT_FAINT).pack(side="right", padx=10, pady=9)
        # Show current Oringo pet for oringo sections
        if event_type == "oringo":
            oringo_info = getattr(self, "oringo_status_info", {})
            pet_name = oringo_info.get("current_pet", "Unknown")
            override = self.settings.get("oringo_pet_override", "Auto")
            if override != "Auto":
                pet_name = f"{override} (manual override)"
            pet_lbl = tk.Label(wrap, text=f"    Current legendary pet: {pet_name}",
                               font=FONT_MAIN, bg=BG_DARK, fg=ACCENT_YELLOW)
            pet_lbl.pack(anchor="w", pady=(2, 4))
        for row in rows:
            EventItemCard(wrap, row["item_id"], row["recommendation"]).pack(fill="x", pady=(1, 0))

    def _render_overnight_plan(self, flips, purse):
        """Render the Overnight Plan view in the shared scroll frame."""
        self._render_cards(flips, purse)

    def _render_full_list(self, flips, purse):
        """Render the Full List view in the shared scroll frame."""
        self._render_cards(flips, purse)

    def _render_cards(self, flips, purse):
        self._clear_cards()

        if self.view_mode == "dashboard":
            sleep_hours = self._get_sleep_hours()
            target_n = self._get_spread_n()
            risk_floor = self._get_risk_floor()
            min_weekly_sales = self._get_min_weekly_sales()
            price_trends = {f["id"]: self.get_price_trend(f["id"]) for f in flips}
            portfolio, leftover, risk_excluded = compute_portfolio(
                list(flips), purse, sleep_hours, target_n, risk_floor, min_weekly_sales,
                price_trends=price_trends)
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
                f"suspects, under {risk_floor:,.0f} coins/day in turnover, or under "
                f"{min_weekly_sales:,.0f} units/week in actual trailing sales, to keep this plan "
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
                    f"a {self._get_buy_buffer_pct():g}% buffer above the current top buy order. Ranking "
                    f"also weighs each item's own 24h price trend - rising items nudged up, falling "
                    f"items nudged down. Tap any "
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
            trend = f.get("price_trend") if "price_trend" in f else self.get_price_trend(f["id"])
            card = FlipCard(self.cards_scroll.inner, f, mode, self.open_category_dialog,
                             sleep_hours=sleep_hours, on_blacklist=self.blacklist_item,
                             market_context=market_context, on_add_storage=self.add_flip_to_storage,
                             trend=trend)
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
        try:
            from event_price_engine import bazaar_bridge
            bazaar_bridge.bridge_close(self)
        except Exception:
            traceback.print_exc()
        if self._event_pipeline is not None:
            self._event_pipeline.close()
        self.settings.update({
            "purse": self.purse_var.get(),
            "sleep_hours": self.sleep_hours_var.get(),
            "spread_n": self.spread_var.get(),
            "risk_floor": self.risk_floor_var.get(),
            "min_weekly_sales": self.min_weekly_sales_var.get(),
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
