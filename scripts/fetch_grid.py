"""
fetch_grid.py
-------------
Polls free grid APIs every 5 minutes via GitHub Actions cron.
Writes normalised price + renewables data to Supabase.

APIs used (all free, no payment required):
  - CAISO OASIS    : pubcrawldata.caiso.com  (no key)
  - ERCOT          : pubcrawldata.ercot.com  (free account)
  - EIA Open Data  : api.eia.gov             (free key)
  - ElectricityMaps: api.electricitymap.org  (free tier)
"""

import os
import json
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Supabase client ──────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Optional API keys ────────────────────────────────────────────────────────
EIA_KEY     = os.environ.get("EIA_API_KEY", "")
EMAP_KEY    = os.environ.get("ELECTRICITYMAP_KEY", "")

NOW_UTC = datetime.now(timezone.utc)
NOW_STR = NOW_UTC.strftime("%Y%m%dT%H%MZ")


# ── CAISO OASIS ──────────────────────────────────────────────────────────────
# Docs: http://oasis.caiso.com/mrioasis/logon.do
# LMP = Locational Marginal Price in $/MWh
CAISO_ZONES = {
    "CAISO_NP15": "TH_NP15_GEN-APND",
    "CAISO_SP15": "TH_SP15_GEN-APND",
    "CAISO_ZP26": "TH_ZP26_GEN-APND",
}

def fetch_caiso() -> list[dict]:
    results = []
    start = NOW_UTC.strftime("%Y%m%dT%H:00-0000")
    end   = NOW_UTC.strftime("%Y%m%dT%H:59-0000")

    url = (
        "http://oasis.caiso.com/oasisapi/SingleZip"
        f"?queryname=PRC_LMP&startdatetime={start}&enddatetime={end}"
        "&version=1&market_run_id=RTM&resultformat=6"
    )
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"o": "http://www.caiso.com/soa/OASISReport_v1.xsd"}

        for report in root.findall(".//o:REPORT_DATA", ns):
            node  = report.findtext("o:NODE", namespaces=ns)
            lmp   = report.findtext("o:MW", namespaces=ns)
            if node and lmp:
                for zone_id, zone_node in CAISO_ZONES.items():
                    if zone_node in (node or ""):
                        results.append({
                            "zone_id": zone_id,
                            "source": "CAISO",
                            "lmp_dollars_per_mwh": float(lmp),
                            "fetched_at": NOW_UTC.isoformat(),
                        })
    except Exception as e:
        log.warning("CAISO fetch failed: %s", e)
        # Fallback: return None so caller can skip without crashing
    return results


# ── ERCOT ────────────────────────────────────────────────────────────────────
# Docs: https://www.ercot.com/services/mdt/prodDesc/SPPHLZ
ERCOT_HUBS = {
    "ERCOT_HB_NORTH": "HB_NORTH",
    "ERCOT_HB_WEST":  "HB_WEST",
    "ERCOT_HB_SOUTH": "HB_SOUTH",
    "ERCOT_HB_BUSAVG":"HB_BUSAVG",
}

def fetch_ercot() -> list[dict]:
    results = []
    # ERCOT real-time SPP (Settlement Point Price) — public endpoint
    url = "https://www.ercot.com/api/1/services/read/dashboards/real-time-system-conditions.json"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # Real-time price is at data["current"]["prices"]["RTLMP"]
        rt_price = float(data.get("current", {}).get("prices", {}).get("RTLMP", 0))
        for zone_id in ERCOT_HUBS:
            results.append({
                "zone_id": zone_id,
                "source": "ERCOT",
                "lmp_dollars_per_mwh": rt_price,
                "fetched_at": NOW_UTC.isoformat(),
            })
    except Exception as e:
        log.warning("ERCOT fetch failed: %s", e)
    return results


# ── EIA Open Data ─────────────────────────────────────────────────────────────
# Docs: https://api.eia.gov/bulk/
# Series: EBA.US48-ALL.NG.SUN.H (solar), EBA.US48-ALL.NG.WND.H (wind)
EIA_SERIES = {
    "solar_mwh": "EBA.US48-ALL.NG.SUN.H",
    "wind_mwh":  "EBA.US48-ALL.NG.WND.H",
    "total_mwh": "EBA.US48-ALL.NG.H",
}

def fetch_eia_renewables() -> dict | None:
    if not EIA_KEY:
        log.info("No EIA key — skipping")
        return None
    out = {}
    base = "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
    params = {
        "api_key": EIA_KEY,
        "frequency": "hourly",
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 1,
    }
    for key, facet in [("solar_mwh","SUN"),("wind_mwh","WND"),("total_mwh","ALL")]:
        try:
            p = {**params}
            if facet != "ALL":
                p["facets[fueltype][]"] = facet
            resp = requests.get(base, params=p, timeout=15)
            data = resp.json()
            val = data["response"]["data"][0]["value"]
            out[key] = float(val)
        except Exception as e:
            log.warning("EIA series %s failed: %s", key, e)
    return out or None


def compute_renewables_pct(eia: dict | None) -> float | None:
    """Returns renewable % of total US generation, or None if data unavailable."""
    if not eia:
        return None
    total = eia.get("total_mwh", 0)
    if total <= 0:
        return None
    renewable = eia.get("solar_mwh", 0) + eia.get("wind_mwh", 0)
    return round(renewable / total * 100, 1)


# ── ElectricityMaps ───────────────────────────────────────────────────────────
# Docs: https://docs.electricitymaps.com/
# Free tier: 5 zones, 5-min data

EMAP_ZONES = {
    "US-CAL-CISO": "CAISO",     # California
    "US-TEX-ERCO": "ERCOT",     # Texas
    "US-MIDA-PJM": "PJM",       # Mid-Atlantic
}

def fetch_electricitymap() -> list[dict]:
    if not EMAP_KEY:
        log.info("No ElectricityMaps key — skipping")
        return []
    results = []
    for zone, label in EMAP_ZONES.items():
        try:
            resp = requests.get(
                f"https://api.electricitymap.org/v3/carbon-intensity/latest?zone={zone}",
                headers={"auth-token": EMAP_KEY},
                timeout=15,
            )
            data = resp.json()
            results.append({
                "zone": zone,
                "label": label,
                "carbon_intensity_gco2_per_kwh": data.get("carbonIntensity"),
                "renewable_pct": data.get("fossilFuelPercentage") and (100 - data["fossilFuelPercentage"]),
                "fetched_at": NOW_UTC.isoformat(),
            })
        except Exception as e:
            log.warning("ElectricityMaps zone %s failed: %s", zone, e)
    return results


# ── Persist to Supabase ───────────────────────────────────────────────────────

def upsert_grid_prices(rows: list[dict]):
    if not rows:
        return
    supabase.table("grid_prices").upsert(rows, on_conflict="zone_id,fetched_at").execute()
    log.info("Upserted %d grid_prices rows", len(rows))


def upsert_renewables(rows: list[dict]):
    if not rows:
        return
    supabase.table("grid_renewables").upsert(rows, on_conflict="zone,fetched_at").execute()
    log.info("Upserted %d grid_renewables rows", len(rows))


def log_run(status: str, details: dict):
    supabase.table("pipeline_runs").insert({
        "ran_at": NOW_UTC.isoformat(),
        "status": status,
        "details": details,
    }).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== fetch_grid.py starting %s ===", NOW_STR)
    summary = {}

    # 1. Grid prices
    price_rows = []
    price_rows.extend(fetch_caiso())
    price_rows.extend(fetch_ercot())
    upsert_grid_prices(price_rows)
    summary["price_zones_fetched"] = len(price_rows)

    # 2. Renewables %
    eia = fetch_eia_renewables()
    renewables_pct = compute_renewables_pct(eia)
    summary["renewables_pct"] = renewables_pct

    emap_rows = fetch_electricitymap()
    upsert_renewables(emap_rows)
    summary["emap_zones"] = len(emap_rows)

    # 3. Attach renewables % to each price row for alert_engine downstream
    if renewables_pct is not None:
        supabase.table("grid_meta").upsert({
            "key": "us48_renewables_pct",
            "value": str(renewables_pct),
            "updated_at": NOW_UTC.isoformat(),
        }, on_conflict="key").execute()

    log.info("Run complete: %s", summary)
    log_run("success", summary)


if __name__ == "__main__":
    main()
