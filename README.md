# ⚡ GridSniper — EV Negative Price Alert Engine

Text subscribers when solar overproduction causes negative electricity prices,
with the nearest open charger and an affiliate deep-link in every message.

**Total infra cost: $0/month** (until ~2,000 SMS/month)

---

## Files

```
ev-grid-sniper/
├── .github/workflows/pipeline.yml  ← GitHub Actions cron (runs every 5 min)
├── scripts/
│   ├── fetch_grid.py               ← Polls CAISO, ERCOT, EIA, ElectricityMaps
│   ├── alert_engine.py             ← Detects negatives → finds station → sends SMS
│   └── schema.sql                  ← Paste into Supabase SQL editor
├── signup.html                     ← Landing page (deploy on Carrd / Netlify)
└── requirements.txt
```

---

## 5-Step Deploy

### 1. Supabase (free, 5 min)
1. Create project at supabase.com
2. SQL editor → paste `scripts/schema.sql` → Run
3. Copy: Project URL + Service Role Key + Anon Key

### 2. API Keys (all free)
| Service | URL | Notes |
|---|---|---|
| EIA | api.eia.gov/signup | Free, 5,000 req/day |
| NREL | developer.nrel.gov/signup | Free |
| ElectricityMaps | electricitymaps.com/free-tier | Free tier: 5 zones |
| CAISO | No key needed | Public REST API |
| ERCOT | pubcrawldata.ercot.com | Free account |

### 3. Twilio (free trial = ~1,900 SMS)
1. twilio.com → create account
2. Get a phone number (free trial)
3. Note: Account SID, Auth Token, Phone Number

### 4. Affiliate links
| Network | Program |
|---|---|
| EVgo | impact.com → search EVgo |
| Blink | blinkcharging.com/affiliates |
| ChargePoint | partnerstack.com → ChargePoint |

Replace the `AFFILIATE_LINKS` dict in `alert_engine.py` with your real tracked URLs.

### 5. GitHub Actions secrets
In your repo → Settings → Secrets → Actions, add:

```
SUPABASE_URL
SUPABASE_SERVICE_KEY
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER      # E.164 format: +15105550000
EIA_API_KEY
ELECTRICITYMAP_KEY
NREL_API_KEY
```

Push to main → Actions tab → pipeline runs every 5 minutes automatically.

---

## Landing page deploy
1. Open `signup.html`
2. Replace `YOUR_PROJECT.supabase.co` and `YOUR_ANON_KEY` with your values
3. Deploy: drag into Netlify Drop (netlify.com/drop) — instant free hosting
   Or paste into a Carrd page as an HTML embed block

---

## Monetization

| Stream | Rate | At 500 subs |
|---|---|---|
| EVgo affiliate signup | $4.50/signup | ~$20/mo |
| Blink affiliate signup | $5.00/signup | ~$25/mo |
| ChargePoint affiliate | $3.00/signup | ~$15/mo |
| Session commissions | $0.20–0.30/session | ~$35/mo |
| Premium SMS ($4.99/mo) | per sub | ~$250/mo (50 premium) |
| Fleet B2B API | $99–499/mo | next unlock |
| **Total** | | **~$345–715/mo** |

**Infra cost at 500 subs:** GitHub Actions free · Supabase free · ~$0.40/mo SMS

---

## Thresholds (tune in `.github/workflows/pipeline.yml`)

| Var | Default | Meaning |
|---|---|---|
| `NEGATIVE_THRESHOLD_DOLLARS` | `-1.0` | Alert when LMP < this |
| `MIN_RENEWABLES_PCT` | `70.0` | Only badge "green" if above this |
| `ALERT_COOLDOWN_MINS` | `30` | Min gap between alerts per subscriber |
| `STATION_RADIUS_MILES` | `3.0` | Charger search radius |

---

## Scaling path

- **0–100 subs:** GitHub Actions + Supabase + Twilio trial — $0
- **100–2,000 subs:** Twilio pay-as-you-go — ~$15/mo
- **2,000+ subs:** Add Supabase Pro ($25/mo) + consider moving cron to Railway
- **Fleet B2B:** Expose a `/api/negative-prices` endpoint via Supabase Edge Functions
