"""
alert_engine.py
---------------
Runs after fetch_grid.py (chained in GitHub Actions workflow).
Detects negative LMP windows, finds nearest open EV stations,
composes affiliate-linked SMS, and sends via Twilio.

Key logic:
  1. Read latest grid prices from Supabase
  2. Flag zones where LMP < NEGATIVE_THRESHOLD
  3. For each triggered zone, look up subscribers in that region
  4. Find nearest available charging station via NREL AFDC API
  5. Compose SMS with affiliate deep-link
  6. Send via Twilio, log result, update affiliate click tracker
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_KEY"]
TWILIO_SID       = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH      = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM      = os.environ["TWILIO_PHONE_NUMBER"]   # e.g. "+15105550100"
NREL_KEY         = os.environ["NREL_API_KEY"]          # free at developer.nlr.gov

NEGATIVE_THRESHOLD   = float(os.environ.get("NEGATIVE_THRESHOLD_DOLLARS", "-1.0"))
MIN_RENEWABLES_PCT   = float(os.environ.get("MIN_RENEWABLES_PCT", "70.0"))
ALERT_COOLDOWN_MINS  = int(os.environ.get("ALERT_COOLDOWN_MINS", "30"))  # don't spam
STATION_RADIUS_MILES = float(os.environ.get("STATION_RADIUS_MILES", "3.0"))

NOW_UTC = datetime.now(timezone.utc)

supabase: Client  = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio: TwilioClient = TwilioClient(TWILIO_SID, TWILIO_AUTH)

# ── Affiliate link registry ───────────────────────────────────────────────────
# Replace these with your real tracked links from Impact / PartnerStack / Bitly
AFFILIATE_LINKS = {
    "EVgo":        "https://bit.ly/evgo-gs-aff",
    "ChargePoint": "https://bit.ly/cp-gs-aff",
    "Blink":       "https://bit.ly/blink-gs-aff",
    "Tesla":       "https://bit.ly/tsla-gs-aff",
    "default":     "https://bit.ly/gs-charge-aff",
}

# Zone → approximate center lat/lng for station radius search
ZONE_CENTERS = {
    "CAISO_NP15": (37.77, -122.42),   # SF Bay Area
    "CAISO_SP15": (34.05, -118.24),   # LA
    "CAISO_ZP26": (36.74, -119.78),   # Fresno
    "ERCOT_HB_NORTH": (32.78, -96.80),# Dallas
    "ERCOT_HB_WEST":  (31.84, -102.37),# Midland
    "ERCOT_HB_SOUTH": (29.76, -95.37), # Houston
    "ERCOT_HB_BUSAVG":(30.27, -97.74), # Austin
}


# ── Step 1: Read latest prices ────────────────────────────────────────────────

def get_negative_zones() -> list[dict]:
    """Return all zones where the most recent LMP < NEGATIVE_THRESHOLD."""
    cutoff = (NOW_UTC - timedelta(minutes=10)).isoformat()
    resp = (
        supabase.table("grid_prices")
        .select("zone_id, lmp_dollars_per_mwh, source, fetched_at")
        .gte("fetched_at", cutoff)
        .lt("lmp_dollars_per_mwh", NEGATIVE_THRESHOLD)
        .execute()
    )
    zones = resp.data or []
    log.info("Negative zones found: %s", [z["zone_id"] for z in zones])
    return zones


def get_renewables_pct() -> float:
    """Fetch cached renewables % from grid_meta."""
    try:
        resp = supabase.table("grid_meta").select("value").eq("key", "us48_renewables_pct").execute()
        return float(resp.data[0]["value"]) if resp.data else 50.0
    except Exception:
        return 50.0


# ── Step 2: Subscribers in zone ───────────────────────────────────────────────

# Simple mapping: CAISO zones → California zip prefixes, ERCOT → Texas prefixes
ZONE_ZIP_PREFIXES = {
    "CAISO_NP15": ("94", "95"),
    "CAISO_SP15": ("90", "91", "92"),
    "CAISO_ZP26": ("93",),
    "ERCOT_HB_NORTH": ("75", "76"),
    "ERCOT_HB_WEST":  ("79",),
    "ERCOT_HB_SOUTH": ("77",),
    "ERCOT_HB_BUSAVG":("78",),
}

def get_subscribers_for_zone(zone_id: str) -> list[dict]:
    prefixes = ZONE_ZIP_PREFIXES.get(zone_id, ())
    if not prefixes:
        return []

    resp = supabase.table("subscribers").select("*").eq("active", True).execute()
    all_subs = resp.data or []

    matched = [
        s for s in all_subs
        if any(s.get("zip_code", "").startswith(p) for p in prefixes)
    ]
    log.info("Zone %s matched %d subscribers", zone_id, len(matched))
    return matched


def was_recently_alerted(subscriber_id: str, zone_id: str) -> bool:
    """Prevent alert spam — skip if subscriber got an alert for this zone recently."""
    cutoff = (NOW_UTC - timedelta(minutes=ALERT_COOLDOWN_MINS)).isoformat()
    resp = (
        supabase.table("alerts_sent")
        .select("id")
        .eq("subscriber_id", subscriber_id)
        .eq("zone_id", zone_id)
        .gte("sent_at", cutoff)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


# ── Step 3: Find nearest EV station ──────────────────────────────────────────

def find_nearest_stations(lat: float, lng: float, count: int = 3) -> list[dict]:
    """
    NREL Alternative Fuels Station Locator API — completely free.
    Docs: https://developer.nrel.gov/docs/transportation/alt-fuel-stations-v1/nearest/
    """
    url = "https://developer.nrel.gov/api/alt-fuel-stations/v1/nearest.json"
    params = {
        "api_key": NREL_KEY,
        "fuel_type": "ELEC",
        "latitude": lat,
        "longitude": lng,
        "radius": STATION_RADIUS_MILES,
        "limit": count,
        "status": "E",          # E = open
        "access": "public",
        "ev_connector_types": "J1772,CHADEMO,J1772COMBO",  # exclude Tesla-only
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        stations = resp.json().get("alt_fuel_stations", [])
        return [{
            "id":       s.get("id"),
            "name":     s.get("station_name"),
            "address":  f"{s.get('street_address')}, {s.get('city')} {s.get('state')}",
            "network":  s.get("ev_network", "Unknown"),
            "phone":    s.get("station_phone"),
            "ev_ports": s.get("ev_level2_evse_num", 0) + s.get("ev_dc_fast_num", 0),
            "lat":      s.get("latitude"),
            "lng":      s.get("longitude"),
        } for s in stations]
    except Exception as e:
        log.error("NREL station fetch failed: %s", e)
        return []


# ── Step 4: Compose SMS ───────────────────────────────────────────────────────

def compose_sms(
    subscriber: dict,
    zone: dict,
    station: dict,
    renewables_pct: float,
    minutes_remaining: int,
) -> str:
    lmp      = zone["lmp_dollars_per_mwh"]
    network  = station.get("network", "").split()[0]  # "EVgo Network" → "EVgo"
    aff_link = AFFILIATE_LINKS.get(network, AFFILIATE_LINKS["default"])
    savings  = abs(lmp) * 0.065  # rough: 65 kWh avg EV charge * $/kWh equiv

    is_green = renewables_pct >= MIN_RENEWABLES_PCT
    green_tag = f" · {int(renewables_pct)}% 🌱 renewable" if is_green else ""

    # Plan-based message differentiation
    plan = subscriber.get("plan", "free")
    if plan == "premium":
        header = f"⚡ GridSniper PREMIUM — negative price window now!"
    else:
        header = f"⚡ GridSniper alert!"

    sms = (
        f"{header}\n"
        f"Plug in at {station['name']}\n"
        f"({station['address']})\n"
        f"Price: ${lmp:.1f}/MWh{green_tag}\n"
        f"Est. savings: ~${savings:.2f} vs peak\n"
        f"Window: ~{minutes_remaining} min left\n"
        f"→ {aff_link}"
    )

    # SMS hard limit 160 chars per segment; keep under 320 (2 segments)
    if len(sms) > 320:
        sms = sms[:316] + "..."

    return sms


# ── Step 5: Send & log ────────────────────────────────────────────────────────

def send_sms(to_phone: str, body: str) -> str | None:
    """Returns Twilio message SID or None on failure."""
    try:
        msg = twilio.messages.create(
            body=body,
            from_=TWILIO_FROM,
            to=to_phone,
        )
        log.info("SMS sent to %s — SID %s", to_phone[:7] + "****", msg.sid)
        return msg.sid
    except Exception as e:
        log.error("Twilio error sending to %s: %s", to_phone[:7] + "****", e)
        return None


def log_alert(subscriber: dict, zone: dict, station: dict, sms_sid: str | None, sms_body: str):
    supabase.table("alerts_sent").insert({
        "subscriber_id":       subscriber["id"],
        "zone_id":             zone["zone_id"],
        "lmp_dollars_per_mwh": zone["lmp_dollars_per_mwh"],
        "station_id":          station.get("id"),
        "station_name":        station.get("name"),
        "network":             station.get("network"),
        "sms_body":            sms_body,
        "twilio_sid":          sms_sid,
        "sent_at":             NOW_UTC.isoformat(),
        "delivered":           sms_sid is not None,
    }).execute()


def increment_affiliate_click(network: str):
    """Track that we sent a link for this network — for monetization reporting."""
    supabase.rpc("increment_affiliate_clicks", {"p_network": network}).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== alert_engine.py starting %s ===", NOW_UTC.isoformat())

    negative_zones = get_negative_zones()
    if not negative_zones:
        log.info("No negative price zones — nothing to do.")
        return

    renewables_pct = get_renewables_pct()
    log.info("Current renewables: %.1f%%", renewables_pct)

    alerts_sent  = 0
    alerts_skipped = 0

    for zone in negative_zones:
        zone_id = zone["zone_id"]
        center  = ZONE_CENTERS.get(zone_id)
        if not center:
            log.warning("No center coordinates for zone %s — skipping", zone_id)
            continue

        stations = find_nearest_stations(*center, count=3)
        if not stations:
            log.warning("No stations found near zone %s", zone_id)
            continue

        # Pick the station with the most ports (best availability proxy)
        station = max(stations, key=lambda s: s.get("ev_ports") or 0)

        subscribers = get_subscribers_for_zone(zone_id)

        # Estimate window: negative prices typically last 30–90 min in CAISO
        # Use a conservative 25 min so subscribers feel urgency
        minutes_remaining = 25

        for sub in subscribers:
            if was_recently_alerted(sub["id"], zone_id):
                log.debug("Subscriber %s already alerted for %s — skip", sub["id"], zone_id)
                alerts_skipped += 1
                continue

            sms_body = compose_sms(sub, zone, station, renewables_pct, minutes_remaining)
            sms_sid  = send_sms(sub["phone"], sms_body)
            log_alert(sub, zone, station, sms_sid, sms_body)

            network = station.get("network", "default").split()[0]
            increment_affiliate_click(network)

            if sms_sid:
                alerts_sent += 1

    log.info("Done. Sent: %d  Skipped (cooldown): %d", alerts_sent, alerts_skipped)

    # Log pipeline run
    supabase.table("pipeline_runs").insert({
        "ran_at":       NOW_UTC.isoformat(),
        "status":       "success",
        "details": {
            "negative_zones":   len(negative_zones),
            "alerts_sent":      alerts_sent,
            "alerts_skipped":   alerts_skipped,
            "renewables_pct":   renewables_pct,
        },
    }).execute()


if __name__ == "__main__":
    main()
