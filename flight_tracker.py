import os
import sys
import json
import sqlite3
import statistics
import re
import urllib.request
from bs4 import BeautifulSoup
from datetime import datetime, date, time, timedelta
from pathlib import Path
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Ensure UTF-8 output across standard streams
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ORIGIN = "BCN"
DESTINATION = "BER"
DATES = ["2027-01-03", "2027-01-04"]
RECIPIENT_EMAILS = ["schuetzenc@gmail.com", "guillejhl@gmail.com"]
FINAL_DEADLINE = date(2026, 11, 15)
URGENT_PRICE_THRESHOLD = 70.0  # Alert immediately if a direct flight drops below €70

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "flight_history.db"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"
INDEX_HTML_PATH = BASE_DIR / "index.html"
DATA_JSON_PATH = BASE_DIR / "flight_data.json"
REPORT_TXT_PATH = BASE_DIR / "latest_flight_report.txt"
REMOTE_DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://guillermohern023.github.io/bcn-ber-flight-tracker/dashboard.html")
LOCAL_DASHBOARD_URL = "http://localhost:8080/dashboard.html"
ARTIFACT_DASHBOARD_PATH = Path(r"C:\Users\guill\.gemini\antigravity\brain\c24e7ab2-5e57-4fb7-a741-1bfa4c7f2fdb\flight_dashboard.html")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS flight_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        search_date TEXT NOT NULL,
        departure_date TEXT NOT NULL,
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        airline TEXT NOT NULL,
        flight_number TEXT NOT NULL,
        departure_time TEXT NOT NULL,
        arrival_time TEXT NOT NULL,
        price REAL NOT NULL,
        currency TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sent_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_date TEXT NOT NULL,
        alert_key TEXT NOT NULL,
        price REAL NOT NULL,
        sent_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sent_daily_summaries (
        summary_date TEXT PRIMARY KEY,
        sent_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def check_date_condition():
    today = date.today()
    if today > FINAL_DEADLINE:
        print(f"Current date ({today}) is past final execution date ({FINAL_DEADLINE}). Stopping automation.")
        sys.exit(0)
    is_final_day = (today == FINAL_DEADLINE)
    return is_final_day

def search_ryanair_flights(date_str):
    results = []
    try:
        from ryanair import Ryanair
        api = Ryanair("EUR")
        query_url = "".join((Ryanair.BASE_SERVICES_API_URL, "oneWayFares"))
        params = {
            "departureAirportIataCode": ORIGIN,
            "arrivalAirportIataCode": DESTINATION,
            "outboundDepartureDateFrom": date_str,
            "outboundDepartureDateTo": date_str,
            "currency": "EUR"
        }
        data = api._retryable_query(query_url, params)
        for fare in data.get("fares", []):
            outbound = fare.get("outbound", {})
            dep_iso = outbound.get("departureDate")
            arr_iso = outbound.get("arrivalDate")
            if not dep_iso or not arr_iso:
                continue
            dep_dt = datetime.fromisoformat(dep_iso)
            arr_dt = datetime.fromisoformat(arr_iso)

            # Filter flights departing before 10:00 AM local time
            if dep_dt.time() < time(10, 0):
                continue

            flight_num = outbound.get("flightNumber", "FR 133")
            price_val = outbound.get("price", {}).get("value", 0.0)

            results.append({
                "airline": "Ryanair",
                "flight_number": flight_num,
                "departure_time": dep_dt.strftime("%H:%M"),
                "arrival_time": arr_dt.strftime("%H:%M"),
                "price": price_val,
                "price_str": f"EUR {price_val:.2f}",
                "date": date_str
            })
    except Exception as e:
        print(f"Error querying Ryanair for {date_str}: {e}")
    return results

def scrape_live_google_flights(date_str):
    url = f"https://www.google.com/travel/flights?q=one-way%20flights%20from%20{ORIGIN}%20to%20{DESTINATION}%20on%20{date_str}%20nonstop&curr=EUR&hl=en"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "CONSENT=YES+cb.20230531-04-p0.en+FX+908; SOCS=CAISHAgBEhJnd3NfMjAyMzA4MTUtMF9SQzIaAmVuIAEaBgiAo_CmBg"
        }
    )
    flights = []
    seen = set()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for it in soup.find_all(attrs={"aria-label": True}):
            l = it["aria-label"]
            if "leaves" in l.lower() and ("euro" in l.lower() or "€" in l):
                norm = re.sub(r"\s+", " ", l)
                p = re.search(r"From\s+(\d+)\s+euro", norm, re.I)
                a = re.search(r"flight with ([^.]+)\.", norm)
                times = re.findall(r"at\s+(\d+:\d+\s*[AP]M)", norm)
                if p and a and len(times) >= 2:
                    price_val = float(p.group(1))
                    airline_name = a.group(1).strip()
                    dep_raw = times[0]
                    arr_raw = times[1]
                    dep_t = datetime.strptime(dep_raw, "%I:%M %p").strftime("%H:%M")
                    arr_t = datetime.strptime(arr_raw, "%I:%M %p").strftime("%H:%M")
                    f_num = f"{airline_name[:2].upper()} Nonstop"
                    if "easyJet" in airline_name: f_num = "U2 Nonstop"
                    elif "Vueling" in airline_name: f_num = "VY Nonstop"
                    elif "Ryanair" in airline_name: f_num = "FR Nonstop"
                    key = (airline_name, dep_t)
                    if key not in seen:
                        seen.add(key)
                        flights.append({
                            "airline": airline_name,
                            "flight_number": f_num,
                            "departure_time": dep_t,
                            "arrival_time": arr_t,
                            "price": price_val,
                            "price_str": f"EUR {price_val:.2f}",
                            "date": date_str
                        })
        print(f"[INFO] Scraped {len(flights)} live nonstop flights from Google Flights for {date_str}.")
    except Exception as e:
        print(f"[WARN] Error scraping Google Flights for {date_str}: {e}")
    return flights

def fetch_all_flights_for_date(date_str):
    gf_flights = scrape_live_google_flights(date_str)
    ryanair = search_ryanair_flights(date_str)
    
    # Merge and deduplicate by (airline, departure_time)
    all_flights = []
    seen = set()
    # Prioritize direct Ryanair API for exact cents, merged with Google Flights live
    for f in ryanair + gf_flights:
        key = (f["airline"].lower(), f["departure_time"])
        if key not in seen:
            seen.add(key)
            all_flights.append(f)

    # Filter out flights departing before 10:00 AM local time
    filtered = [f for f in all_flights if datetime.strptime(f["departure_time"], "%H:%M").time() >= time(10, 0)]
    filtered.sort(key=lambda x: x["price"])
    return filtered

def record_flights_to_db(all_flights_by_date):
    today_str = date.today().isoformat()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for d, flights in all_flights_by_date.items():
        for f in flights:
            cur.execute("""
            INSERT INTO flight_prices 
            (recorded_at, search_date, departure_date, origin, destination, airline, flight_number, departure_time, arrival_time, price, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EUR')
            """, (now_iso, today_str, d, ORIGIN, DESTINATION, f["airline"], f["flight_number"], f["departure_time"], f["arrival_time"], f["price"]))
    conn.commit()
    conn.close()

def compute_statistics():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Per Airline Stats
    cur.execute("SELECT airline, price FROM flight_prices")
    airline_data = {}
    for a, p in cur.fetchall():
        airline_data.setdefault(a, []).append(p)
    airline_stats = {}
    for a, prices in airline_data.items():
        airline_stats[a] = {
            "min": round(min(prices), 2),
            "max": round(max(prices), 2),
            "avg": round(statistics.mean(prices), 2),
            "median": round(statistics.median(prices), 2),
            "count": len(prices)
        }

    # Per Departure Date Stats
    cur.execute("SELECT departure_date, price FROM flight_prices")
    date_data = {}
    for d, p in cur.fetchall():
        date_data.setdefault(d, []).append(p)
    date_stats = {}
    for d, prices in date_data.items():
        date_stats[d] = {
            "min": round(min(prices), 2),
            "max": round(max(prices), 2),
            "avg": round(statistics.mean(prices), 2),
            "median": round(statistics.median(prices), 2),
            "count": len(prices)
        }

    # Check-by-Check Price Trend History (Every check timestamp vs Min/Avg/Max)
    cur.execute("SELECT SUBSTR(recorded_at, 6, 11) as check_ts, MIN(price), AVG(price), MAX(price) FROM flight_prices GROUP BY SUBSTR(recorded_at, 1, 16) ORDER BY recorded_at")
    daily_trends = []
    for s_date, min_p, avg_p, max_p in cur.fetchall():
        daily_trends.append({
            "search_date": s_date,
            "min_price": round(min_p, 2),
            "avg_price": round(avg_p, 2),
            "max_price": round(max_p, 2)
        })

    # Hourly distribution (Time of Day vs Price)
    cur.execute("SELECT departure_time, price, airline, departure_date FROM flight_prices")
    hourly_data = []
    for dep_t, p, airl, dep_d in cur.fetchall():
        hour_val = int(dep_t.split(":")[0]) + int(dep_t.split(":")[1])/60.0
        hourly_data.append({
            "time_str": dep_t,
            "hour": round(hour_val, 2),
            "price": round(p, 2),
            "airline": airl,
            "departure_date": dep_d
        })

    conn.close()
    return {
        "airline_stats": airline_stats,
        "date_stats": date_stats,
        "daily_trends": daily_trends,
        "hourly_data": hourly_data
    }

def generate_dashboard_html(stats, top_flights_by_date, all_flights_by_date=None):
    if all_flights_by_date is None:
        all_flights_by_date = top_flights_by_date

    # Overall summary numbers across top flights
    flat_top_flights = [dict(f, date=d) for d, flights in top_flights_by_date.items() for f in flights]
    flat_all_flights = [dict(f, date=d) for d, flights in all_flights_by_date.items() for f in flights]
    all_current = [f["price"] for f in flat_top_flights]
    lowest_flight = min(flat_all_flights, key=lambda x: x["price"]) if flat_all_flights else {"price": 0.0, "airline": "N/A", "date": "N/A", "departure_time": ""}
    lowest_now = lowest_flight["price"]
    lowest_label = f"{lowest_flight['date']} ({lowest_flight['airline']})"
    avg_now = round(statistics.mean(all_current), 2) if all_current else 0.0
    median_now = round(statistics.median(all_current), 2) if all_current else 0.0
    total_options_count = len(flat_all_flights)

    # Dynamic Date Minimums
    jan3_flights = all_flights_by_date.get("2027-01-03", [])
    jan4_flights = all_flights_by_date.get("2027-01-04", [])
    jan3_min = min([f["price"] for f in jan3_flights]) if jan3_flights else 0.0
    jan4_min = min([f["price"] for f in jan4_flights]) if jan4_flights else 0.0
    if jan4_min < jan3_min:
        date_diff_str = f"Jan 4 saves €{jan3_min - jan4_min:.2f}"
    elif jan3_min < jan4_min:
        date_diff_str = f"Jan 3 saves €{jan4_min - jan3_min:.2f}"
    else:
        date_diff_str = "Equal minimum fare"

    # Dynamic Top Value Carrier
    airline_stats = stats.get("airline_stats", {})
    if airline_stats:
        sorted_carriers = sorted(airline_stats.items(), key=lambda kv: kv[1]["min"])
        top_carrier_name, top_carrier_data = sorted_carriers[0]
        top_carrier_avg = top_carrier_data["avg"]
        other_carriers_summary = " • ".join([f"{k}: €{v['avg']:.0f}" for k, v in sorted_carriers[1:]]) or "All direct carriers tracked"
    else:
        top_carrier_name = "N/A"
        top_carrier_avg = 0.0
        other_carriers_summary = "No data"

    days_left = (FINAL_DEADLINE - date.today()).days
    last_updated_str = datetime.now().strftime("%b %d, %H:%M")

    # JSON for charts
    stats_json = json.dumps(stats)
    flights_json = json.dumps(all_flights_by_date)

    # Build Mobile Flight Cards (grouped by day, showing all options for that day)
    mobile_cards_html = ""
    desktop_rows_html = ""

    for dep_date, flights in all_flights_by_date.items():
        weekday_str = "Sunday, Jan 3, 2027" if dep_date == "2027-01-03" else "Monday, Jan 4, 2027"
        short_date_str = "Sun, Jan 3" if dep_date == "2027-01-03" else "Mon, Jan 4"
        day_min = min([f["price"] for f in flights]) if flights else 0.0

        mobile_cards_html += f"""
        <div class="flight-day-group space-y-3" data-date-group="{dep_date}">
          <div class="flex items-center justify-between bg-slate-900 text-white px-4 py-2.5 rounded-xl shadow-sm">
            <div class="flex items-center gap-2">
              <span class="text-sm font-extrabold tracking-tight">📅 {weekday_str}</span>
              <span class="text-[11px] bg-white/15 text-blue-200 px-2 py-0.5 rounded-full font-semibold">{len(flights)} flights</span>
            </div>
            <span class="text-xs font-bold text-emerald-300">From €{day_min:.2f}</span>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        """

        for idx, f in enumerate(flights, 1):
            airline_url, gflights_url = get_booking_urls(f["airline"], dep_date, f["flight_number"])
            is_top3 = idx <= 3
            is_best = idx == 1

            if f["airline"] == "easyJet":
                badge_color = "bg-orange-100 text-orange-800 border-orange-200"
                card_accent = "border-l-4 border-l-orange-500"
            elif f["airline"] == "Vueling":
                badge_color = "bg-amber-100 text-amber-900 border-amber-200"
                card_accent = "border-l-4 border-l-amber-500"
            else:
                badge_color = "bg-blue-100 text-blue-800 border-blue-200"
                card_accent = "border-l-4 border-l-blue-600"

            if is_best:
                rank_badge = '<span class="text-[10px] bg-emerald-100 text-emerald-800 border border-emerald-300 px-2 py-0.5 rounded-full font-extrabold uppercase tracking-wide">#1 Cheapest</span>'
            elif is_top3:
                rank_badge = f'<span class="text-[10px] bg-blue-50 text-blue-700 border border-blue-200 px-2 py-0.5 rounded-full font-bold">#{idx} Top 3</span>'
            else:
                rank_badge = f'<span class="text-[10px] bg-slate-100 text-slate-600 px-2 py-0.5 rounded-full font-semibold">Option #{idx}</span>'

            top3_attr = "true" if is_top3 else "false"

            # Mobile Card
            mobile_cards_html += f"""
            <div class="flight-item bg-white rounded-xl p-4 shadow-sm border border-slate-200/90 {card_accent} flex flex-col justify-between gap-3 transition" data-date="{dep_date}" data-top3="{top3_attr}">
              <!-- Top row: Airline + Rank + Price -->
              <div class="flex items-start justify-between gap-2">
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-bold border {badge_color}">
                    {f['airline']}
                  </span>
                  <span class="font-mono text-xs text-slate-600 font-bold bg-slate-100 px-2 py-0.5 rounded">{f['flight_number']}</span>
                  {rank_badge}
                </div>
                <div class="text-right shrink-0">
                  <div class="text-xl font-extrabold {'text-emerald-600' if is_best else 'text-slate-900'} leading-none">€{f['price']:.2f}</div>
                  <div class="text-[10px] text-slate-400 font-medium mt-0.5">One-way</div>
                </div>
              </div>

              <!-- Middle row: Visual Flight Timeline -->
              <div class="bg-slate-50 rounded-lg px-3 py-2.5 border border-slate-100 flex items-center justify-between gap-2">
                <div class="text-left">
                  <div class="text-base font-extrabold text-slate-900 leading-tight">{f['departure_time']}</div>
                  <div class="text-[11px] font-bold text-slate-500">BCN</div>
                </div>
                <div class="flex-1 flex flex-col items-center px-2">
                  <span class="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">2h 40m • Direct</span>
                  <div class="w-full flex items-center gap-1 my-0.5">
                    <div class="h-0.5 flex-1 bg-slate-300 rounded"></div>
                    <span class="text-xs text-blue-600">✈️</span>
                    <div class="h-0.5 flex-1 bg-slate-300 rounded"></div>
                  </div>
                  <span class="text-[10px] text-emerald-600 font-semibold">{short_date_str}</span>
                </div>
                <div class="text-right">
                  <div class="text-base font-extrabold text-slate-900 leading-tight">{f['arrival_time']}</div>
                  <div class="text-[11px] font-bold text-slate-500">BER</div>
                </div>
              </div>

              <!-- Bottom row: Touch-friendly Action Buttons -->
              <div class="grid grid-cols-2 gap-2 pt-0.5">
                <a href="{gflights_url}" target="_blank" rel="noopener noreferrer" class="flex items-center justify-center gap-1 bg-slate-900 hover:bg-slate-800 active:bg-slate-950 text-white text-xs font-bold py-2.5 px-3 rounded-lg shadow-sm transition min-h-[40px] text-center">
                  <span>Google Flights</span>
                  <span>➔</span>
                </a>
                <a href="{airline_url}" target="_blank" rel="noopener noreferrer" class="flex items-center justify-center gap-1 bg-slate-100 hover:bg-slate-200 active:bg-slate-300 text-slate-800 border border-slate-300 text-xs font-bold py-2.5 px-3 rounded-lg transition min-h-[40px] text-center">
                  <span>{f['airline']}.com</span>
                  <span>↗</span>
                </a>
              </div>
            </div>
            """

            # Desktop Table Row
            desktop_rows_html += f"""
            <tr class="flight-item hover:bg-slate-50/80 transition" data-date="{dep_date}" data-top3="{top3_attr}">
              <td class="px-4 py-3.5 font-semibold text-slate-900 whitespace-nowrap">
                <div>{dep_date}</div>
                <div class="text-[11px] text-slate-400 font-normal">{short_date_str}</div>
              </td>
              <td class="px-4 py-3.5 whitespace-nowrap">
                <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold border {badge_color}">
                  {f['airline']}
                </span>
              </td>
              <td class="px-4 py-3.5 font-mono text-xs text-slate-700 font-bold whitespace-nowrap">{f['flight_number']}</td>
              <td class="px-4 py-3.5 font-bold text-slate-900 whitespace-nowrap">{f['departure_time']} <span class="text-xs font-normal text-slate-400">BCN</span></td>
              <td class="px-4 py-3.5 font-bold text-slate-900 whitespace-nowrap">{f['arrival_time']} <span class="text-xs font-normal text-slate-400">BER</span></td>
              <td class="px-4 py-3.5 text-xs text-slate-500 whitespace-nowrap">2h 40m (Direct)</td>
              <td class="px-4 py-3.5 font-extrabold text-slate-900 whitespace-nowrap">
                <span class="{'text-emerald-600' if is_best else 'text-slate-900'}">€{f['price']:.2f}</span>
                <span class="ml-1.5">{rank_badge}</span>
              </td>
              <td class="px-4 py-3.5 text-right whitespace-nowrap space-x-1.5">
                <a href="{gflights_url}" target="_blank" rel="noopener noreferrer" class="inline-block text-xs bg-slate-900 hover:bg-slate-800 text-white font-semibold px-3 py-1.5 rounded-lg shadow-sm transition">Google Flights ➔</a>
                <a href="{airline_url}" target="_blank" rel="noopener noreferrer" class="inline-block text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-300 font-semibold px-2.5 py-1.5 rounded-lg transition">{f['airline']} ↗</a>
              </td>
            </tr>
            """

        mobile_cards_html += """
          </div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en" class="scroll-smooth">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
  <meta name="theme-color" content="#0f172a">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>BCN ➔ BER Flight Price Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    body {{
      font-family: 'Plus Jakarta Sans', sans-serif;
      -webkit-tap-highlight-color: transparent;
      overflow-x: hidden;
    }}
    .glass-card {{
      background: rgba(255, 255, 255, 0.92);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(226, 232, 240, 0.85);
    }}
    canvas {{
      touch-action: pan-y;
    }}
    /* Hide scrollbar for Chrome, Safari and Opera on filter pills */
    .no-scrollbar::-webkit-scrollbar {{
      display: none;
    }}
    .no-scrollbar {{
      -ms-overflow-style: none;
      scrollbar-width: none;
    }}
  </style>
</head>
<body class="bg-slate-100 text-slate-900 min-h-screen pb-20 md:pb-10">

  <!-- Top Navigation / Banner -->
  <header class="bg-gradient-to-r from-slate-900 via-indigo-950 to-blue-900 text-white shadow-lg">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-5 sm:py-7">
      <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div>
          <div class="inline-flex flex-wrap items-center gap-2 bg-blue-500/20 border border-blue-400/30 text-blue-200 text-[11px] px-3 py-1 rounded-full uppercase tracking-wider font-semibold mb-2.5">
            <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
            <span>Live Monitor • Updated {last_updated_str}</span>
          </div>
          <h1 class="text-2xl sm:text-4xl font-extrabold tracking-tight flex flex-wrap items-center gap-2 sm:gap-3">
            <span>Barcelona (BCN)</span>
            <span class="text-blue-400">➔</span>
            <span>Berlin (BER)</span>
          </h1>
          <p class="text-slate-300 text-xs sm:text-sm mt-1">One-Way Direct Flights • Jan 3 &amp; 4, 2027 (Departures &ge; 10:00 AM)</p>
        </div>

        <div class="flex items-center justify-between sm:justify-start gap-3 bg-white/10 backdrop-blur-md border border-white/10 rounded-xl px-4 py-2.5">
          <div class="text-left sm:text-center">
            <div class="text-[10px] sm:text-xs text-slate-300 uppercase tracking-wide">Booking Deadline</div>
            <div class="text-sm sm:text-lg font-bold text-amber-300">Nov 15, 2026 <span class="text-xs font-normal text-slate-300">({days_left}d left)</span></div>
          </div>
          <button onclick="window.print()" class="bg-blue-600 hover:bg-blue-500 active:bg-blue-700 text-white text-xs sm:text-sm font-semibold px-3.5 py-2 rounded-lg shadow transition shrink-0">
            Export PDF
          </button>
        </div>
      </div>
    </div>
  </header>

  <!-- Sticky Mobile Quick-Jump Bar -->
  <nav class="sticky top-0 z-30 bg-white/95 backdrop-blur-md border-b border-slate-200 shadow-xs md:hidden">
    <div class="max-w-7xl mx-auto px-3 py-2 flex items-center justify-around gap-1 text-xs font-bold">
      <a href="#section-kpis" class="flex-1 text-center py-1.5 px-2 rounded-lg text-slate-700 hover:bg-slate-100 active:bg-slate-200 transition">📌 Summary</a>
      <a href="#section-flights" class="flex-1 text-center py-1.5 px-2 rounded-lg bg-blue-50 text-blue-700 border border-blue-200 transition">✈️ Flights ({total_options_count})</a>
      <a href="#section-charts" class="flex-1 text-center py-1.5 px-2 rounded-lg text-slate-700 hover:bg-slate-100 active:bg-slate-200 transition">📊 Charts</a>
    </div>
  </nav>

  <main class="max-w-7xl mx-auto px-3 sm:px-6 lg:px-8 py-5 sm:py-8 space-y-6 sm:space-y-8">
    
    <!-- Top KPI Cards: 2x2 Grid on Mobile, 4-col on Desktop -->
    <section id="section-kpis" class="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-5 scroll-mt-14">
      <!-- Lowest Price Card -->
      <div class="glass-card rounded-2xl p-3.5 sm:p-5 shadow-xs">
        <div class="flex items-center justify-between gap-1">
          <span class="text-[10px] sm:text-xs font-bold text-slate-500 uppercase tracking-wider">Lowest Fare</span>
          <span class="p-1.5 sm:p-2 bg-emerald-100 text-emerald-700 rounded-lg sm:rounded-xl shrink-0">
            <svg class="w-4 h-4 sm:w-5 sm:h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          </span>
        </div>
        <div class="mt-2 sm:mt-3">
          <div class="text-xl sm:text-3xl font-extrabold text-emerald-600 leading-tight">€{lowest_now:.2f}</div>
          <div class="text-[11px] sm:text-xs text-slate-500 font-medium truncate mt-0.5">{lowest_label}</div>
        </div>
      </div>

      <!-- Average Price Card -->
      <div class="glass-card rounded-2xl p-3.5 sm:p-5 shadow-xs">
        <div class="flex items-center justify-between gap-1">
          <span class="text-[10px] sm:text-xs font-bold text-slate-500 uppercase tracking-wider">Average (Top 3)</span>
          <span class="p-1.5 sm:p-2 bg-blue-100 text-blue-700 rounded-lg sm:rounded-xl shrink-0">
            <svg class="w-4 h-4 sm:w-5 sm:h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
          </span>
        </div>
        <div class="mt-2 sm:mt-3">
          <div class="text-xl sm:text-3xl font-extrabold text-blue-600 leading-tight">€{avg_now:.2f}</div>
          <div class="text-[11px] sm:text-xs text-slate-500 mt-0.5">Median: <strong>€{median_now:.2f}</strong></div>
        </div>
      </div>

      <!-- Departure Split -->
      <div class="glass-card rounded-2xl p-3.5 sm:p-5 shadow-xs">
        <div class="flex items-center justify-between gap-1">
          <span class="text-[10px] sm:text-xs font-bold text-slate-500 uppercase tracking-wider">Jan 3 vs Jan 4</span>
          <span class="p-1.5 sm:p-2 bg-indigo-100 text-indigo-700 rounded-lg sm:rounded-xl shrink-0">
            <svg class="w-4 h-4 sm:w-5 sm:h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
          </span>
        </div>
        <div class="mt-2 sm:mt-3 flex items-baseline justify-between gap-1">
          <div>
            <div class="text-[10px] text-slate-500 font-semibold">Jan 3</div>
            <div class="text-sm sm:text-xl font-bold text-slate-800">€{jan3_min:.0f}</div>
          </div>
          <div class="h-6 w-px bg-slate-200"></div>
          <div>
            <div class="text-[10px] text-slate-500 font-semibold">Jan 4</div>
            <div class="text-sm sm:text-xl font-bold text-emerald-600">€{jan4_min:.2f}</div>
          </div>
        </div>
        <div class="mt-1 text-[10px] sm:text-xs text-emerald-600 font-semibold truncate">{date_diff_str}</div>
      </div>

      <!-- Most Affordable Airline -->
      <div class="glass-card rounded-2xl p-3.5 sm:p-5 shadow-xs">
        <div class="flex items-center justify-between gap-1">
          <span class="text-[10px] sm:text-xs font-bold text-slate-500 uppercase tracking-wider">Best Airline</span>
          <span class="p-1.5 sm:p-2 bg-orange-100 text-orange-700 rounded-lg sm:rounded-xl shrink-0">
            <svg class="w-4 h-4 sm:w-5 sm:h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
          </span>
        </div>
        <div class="mt-2 sm:mt-3">
          <div class="text-lg sm:text-2xl font-extrabold text-orange-600 leading-tight truncate">{top_carrier_name}</div>
          <div class="text-[10px] sm:text-xs text-slate-500 truncate mt-0.5">Avg €{top_carrier_avg:.0f} • {other_carriers_summary}</div>
        </div>
      </div>
    </section>

    <!-- All Possible Flights Section (Placed high for immediate mobile usability) -->
    <section id="section-flights" class="glass-card rounded-2xl p-4 sm:p-6 shadow-xs space-y-4 sm:space-y-5 scroll-mt-14">
      <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3 border-b border-slate-200 pb-4">
        <div>
          <h2 class="text-lg sm:text-xl font-extrabold text-slate-900">All Possible Direct Flights by Day ({total_options_count} Options)</h2>
          <p class="text-xs text-slate-500">Every available nonstop flight departing at or after 10:00 AM local time</p>
        </div>

        <!-- Touch-Friendly Filter Controls -->
        <div class="flex flex-wrap items-center gap-2">
          <div class="inline-flex bg-slate-100 p-1 rounded-xl w-full sm:w-auto">
            <button id="btn-date-all" onclick="setDateFilter('all')" class="flex-1 sm:flex-initial px-3 py-1.5 text-xs font-bold rounded-lg bg-blue-600 text-white shadow-xs transition">All Dates</button>
            <button id="btn-date-jan3" onclick="setDateFilter('2027-01-03')" class="flex-1 sm:flex-initial px-3 py-1.5 text-xs font-semibold rounded-lg text-slate-700 hover:bg-white/60 transition">Sun, Jan 3</button>
            <button id="btn-date-jan4" onclick="setDateFilter('2027-01-04')" class="flex-1 sm:flex-initial px-3 py-1.5 text-xs font-semibold rounded-lg text-slate-700 hover:bg-white/60 transition">Mon, Jan 4</button>
          </div>
          <button id="btn-top3-toggle" onclick="toggleTop3Only()" class="w-full sm:w-auto px-3 py-2 text-xs font-bold rounded-xl border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition">
            Showing: All Flights (Tap for Top 3 Only)
          </button>
        </div>
      </div>

      <!-- Mobile & Tablet Native Card View (Visible on screens < md) -->
      <div class="block md:hidden space-y-5" id="mobileFlightContainer">
        {mobile_cards_html}
      </div>

      <!-- Desktop Full Table View (Visible on screens >= md) -->
      <div class="hidden md:block overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-600">
          <thead class="bg-slate-100/80 text-slate-700 uppercase text-[11px] font-bold tracking-wider">
            <tr>
              <th class="px-4 py-3 rounded-l-lg">Departure Date</th>
              <th class="px-4 py-3">Airline</th>
              <th class="px-4 py-3">Flight No</th>
              <th class="px-4 py-3">Departure</th>
              <th class="px-4 py-3">Arrival</th>
              <th class="px-4 py-3">Duration</th>
              <th class="px-4 py-3">Price &amp; Rank</th>
              <th class="px-4 py-3 rounded-r-lg text-right">Booking Links</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100" id="flightTableBody">
            {desktop_rows_html}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Main Visualizations Grid -->
    <section id="section-charts" class="grid grid-cols-1 lg:grid-cols-2 gap-5 sm:gap-8 scroll-mt-14">
      
      <!-- Graph 1: Airline Price Distribution -->
      <div class="glass-card rounded-2xl p-4 sm:p-6 shadow-xs">
        <div class="flex items-start justify-between gap-2 mb-3 sm:mb-4">
          <div>
            <h2 class="text-base sm:text-lg font-bold text-slate-900">Airline Cost Analysis</h2>
            <p class="text-[11px] sm:text-xs text-slate-500">Min, Median, Average, and Max fares per airline</p>
          </div>
          <span class="text-[11px] bg-slate-100 text-slate-700 px-2 py-0.5 rounded-md font-semibold shrink-0">EUR (€)</span>
        </div>
        <div class="h-60 sm:h-72">
          <canvas id="airlineStatsChart"></canvas>
        </div>
      </div>

      <!-- Graph 2: Price Trajectory Over Time -->
      <div class="glass-card rounded-2xl p-4 sm:p-6 shadow-xs">
        <div class="flex items-start justify-between gap-2 mb-3 sm:mb-4">
          <div>
            <h2 class="text-base sm:text-lg font-bold text-slate-900">Price Trend History</h2>
            <p class="text-[11px] sm:text-xs text-slate-500">Tracked lowest &amp; average fares over time</p>
          </div>
          <span class="text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 px-2 py-0.5 rounded-md font-semibold shrink-0">3x Daily</span>
        </div>
        <div class="h-60 sm:h-72">
          <canvas id="priceTrendChart"></canvas>
        </div>
      </div>

      <!-- Graph 3: Departure Date Comparison -->
      <div class="glass-card rounded-2xl p-4 sm:p-6 shadow-xs">
        <div class="flex items-start justify-between gap-2 mb-3 sm:mb-4">
          <div>
            <h2 class="text-base sm:text-lg font-bold text-slate-900">Sunday vs Monday Fare Metrics</h2>
            <p class="text-[11px] sm:text-xs text-slate-500">Min, Median, and Average comparison (Jan 3 vs Jan 4)</p>
          </div>
          <span class="text-[11px] bg-indigo-50 text-indigo-700 border border-indigo-200 px-2 py-0.5 rounded-md font-semibold shrink-0">Date Shift</span>
        </div>
        <div class="h-60 sm:h-72">
          <canvas id="dateComparisonChart"></canvas>
        </div>
      </div>

      <!-- Graph 4: Time of Day vs Price Distribution -->
      <div class="glass-card rounded-2xl p-4 sm:p-6 shadow-xs">
        <div class="flex items-start justify-between gap-2 mb-3 sm:mb-4">
          <div>
            <h2 class="text-base sm:text-lg font-bold text-slate-900">Departure Time vs Fare Matrix</h2>
            <p class="text-[11px] sm:text-xs text-slate-500">Flight prices plotted against local departure hour</p>
          </div>
          <span class="text-[11px] bg-blue-50 text-blue-700 border border-blue-200 px-2 py-0.5 rounded-md font-semibold shrink-0">10:00–21:00</span>
        </div>
        <div class="h-60 sm:h-72">
          <canvas id="timeOfDayChart"></canvas>
        </div>
      </div>
    </section>

    <!-- Footer -->
    <footer class="text-center text-xs text-slate-400 py-6 border-t border-slate-200">
      Flight Price Monitor Automation • 3x Daily Checks (08:00, 14:00, 20:00 CET) • Cutoff Date: November 15, 2026
    </footer>
  </main>

  <!-- Interactive Chart & Filter Script -->
  <script>
    const statsData = {stats_json};
    const flightData = {flights_json};
    const isMobile = window.innerWidth < 640;

    const commonLegend = {{
      position: 'bottom',
      labels: {{
        boxWidth: isMobile ? 10 : 14,
        padding: isMobile ? 10 : 16,
        font: {{ size: isMobile ? 10 : 12 }}
      }}
    }};

    // 1. Airline Stats Chart
    const airlineLabels = Object.keys(statsData.airline_stats);
    new Chart(document.getElementById('airlineStatsChart'), {{
      type: 'bar',
      data: {{
        labels: airlineLabels,
        datasets: [
          {{ label: 'Lowest (€)', data: airlineLabels.map(a => statsData.airline_stats[a].min), backgroundColor: '#10B981', borderRadius: 4 }},
          {{ label: 'Median (€)', data: airlineLabels.map(a => statsData.airline_stats[a].median), backgroundColor: '#3B82F6', borderRadius: 4 }},
          {{ label: 'Average (€)', data: airlineLabels.map(a => statsData.airline_stats[a].avg), backgroundColor: '#8B5CF6', borderRadius: 4 }},
          {{ label: 'Highest (€)', data: airlineLabels.map(a => statsData.airline_stats[a].max), backgroundColor: '#EF4444', borderRadius: 4 }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: commonLegend }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ font: {{ size: isMobile ? 10 : 12 }} }}, title: {{ display: !isMobile, text: 'Price (EUR)' }} }},
          x: {{ ticks: {{ font: {{ size: isMobile ? 11 : 12 }} }} }}
        }}
      }}
    }});

    // 2. Price Trend Chart
    const trendDates = statsData.daily_trends.map(t => t.search_date);
    new Chart(document.getElementById('priceTrendChart'), {{
      type: 'line',
      data: {{
        labels: trendDates,
        datasets: [
          {{
            label: 'Lowest (€)',
            data: statsData.daily_trends.map(t => t.min_price),
            borderColor: '#10B981',
            backgroundColor: 'rgba(16, 185, 129, 0.12)',
            fill: true,
            tension: 0.3,
            borderWidth: 2.5,
            pointRadius: isMobile ? 3 : 4
          }},
          {{
            label: 'Average (€)',
            data: statsData.daily_trends.map(t => t.avg_price),
            borderColor: '#3B82F6',
            backgroundColor: 'transparent',
            borderDash: [4, 4],
            tension: 0.3,
            borderWidth: 2,
            pointRadius: isMobile ? 3 : 4
          }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: commonLegend }},
        scales: {{
          y: {{ ticks: {{ font: {{ size: isMobile ? 10 : 12 }} }}, title: {{ display: !isMobile, text: 'Fare in EUR' }} }},
          x: {{ ticks: {{ font: {{ size: isMobile ? 9 : 11 }}, maxRotation: 30 }} }}
        }}
      }}
    }});

    // 3. Date Comparison Chart
    const dateLabels = Object.keys(statsData.date_stats);
    new Chart(document.getElementById('dateComparisonChart'), {{
      type: 'bar',
      data: {{
        labels: dateLabels.map(d => d === '2027-01-03' ? 'Sun, Jan 3' : 'Mon, Jan 4'),
        datasets: [
          {{ label: 'Min (€)', data: dateLabels.map(d => statsData.date_stats[d].min), backgroundColor: '#10B981', borderRadius: 4 }},
          {{ label: 'Median (€)', data: dateLabels.map(d => statsData.date_stats[d].median), backgroundColor: '#6366F1', borderRadius: 4 }},
          {{ label: 'Avg (€)', data: dateLabels.map(d => statsData.date_stats[d].avg), backgroundColor: '#F59E0B', borderRadius: 4 }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{ legend: commonLegend }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ font: {{ size: isMobile ? 10 : 12 }} }} }},
          x: {{ ticks: {{ font: {{ size: isMobile ? 11 : 12 }} }} }}
        }}
      }}
    }});

    // 4. Departure Time vs Price Matrix
    const timePoints = statsData.hourly_data.map(h => ({{
      x: h.hour,
      y: h.price,
      label: `${{h.airline}} (${{h.time_str}} on ${{h.departure_date.slice(5)}})`
    }}));

    new Chart(document.getElementById('timeOfDayChart'), {{
      type: 'scatter',
      data: {{
        datasets: [{{
          label: 'Flight Departures (Hour vs Price)',
          data: timePoints,
          backgroundColor: '#3B82F6',
          pointRadius: isMobile ? 5 : 6,
          pointHoverRadius: 8
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        scales: {{
          x: {{ min: 10, max: 21, ticks: {{ font: {{ size: isMobile ? 10 : 12 }}, callback: v => v + ':00' }}, title: {{ display: !isMobile, text: 'Departure Hour (24h)' }} }},
          y: {{ ticks: {{ font: {{ size: isMobile ? 10 : 12 }} }}, title: {{ display: !isMobile, text: 'Price (€)' }} }}
        }},
        plugins: {{
          legend: commonLegend,
          tooltip: {{
            callbacks: {{
              label: (context) => {{
                const pt = timePoints[context.dataIndex];
                return `${{pt.label}}: €${{pt.y}}`;
              }}
            }}
          }}
        }}
      }}
    }});

    // Unified Filtering for both Mobile Cards & Desktop Table
    let currentDateFilter = 'all';
    let top3Only = false;

    function applyFilters() {{
      const items = document.querySelectorAll('.flight-item');
      items.forEach(el => {{
        const matchesDate = (currentDateFilter === 'all' || el.getAttribute('data-date') === currentDateFilter);
        const matchesRank = (!top3Only || el.getAttribute('data-top3') === 'true');
        el.style.display = (matchesDate && matchesRank) ? '' : 'none';
      }});

      const groups = document.querySelectorAll('.flight-day-group');
      groups.forEach(g => {{
        const groupDate = g.getAttribute('data-date-group');
        g.style.display = (currentDateFilter === 'all' || groupDate === currentDateFilter) ? '' : 'none';
      }});
    }}

    function setDateFilter(val) {{
      currentDateFilter = val;
      const btnMap = {{
        'all': document.getElementById('btn-date-all'),
        '2027-01-03': document.getElementById('btn-date-jan3'),
        '2027-01-04': document.getElementById('btn-date-jan4')
      }};
      Object.keys(btnMap).forEach(k => {{
        if (k === val) {{
          btnMap[k].className = 'flex-1 sm:flex-initial px-3 py-1.5 text-xs font-bold rounded-lg bg-blue-600 text-white shadow-xs transition';
        }} else {{
          btnMap[k].className = 'flex-1 sm:flex-initial px-3 py-1.5 text-xs font-semibold rounded-lg text-slate-700 hover:bg-white/60 transition';
        }}
      }});
      applyFilters();
    }}

    function toggleTop3Only() {{
      top3Only = !top3Only;
      const btn = document.getElementById('btn-top3-toggle');
      if (top3Only) {{
        btn.textContent = 'Showing: Top 3 Per Day (Tap for All)';
        btn.className = 'w-full sm:w-auto px-3 py-2 text-xs font-bold rounded-xl border border-blue-300 bg-blue-50 text-blue-700 transition';
      }} else {{
        btn.textContent = 'Showing: All Flights (Tap for Top 3 Only)';
        btn.className = 'w-full sm:w-auto px-3 py-2 text-xs font-bold rounded-xl border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition';
      }}
      applyFilters();
    }}
  </script>
</body>
</html>
"""
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[INFO] Mobile-friendly Interactive Dashboard saved to: {DASHBOARD_PATH}")
    if ARTIFACT_DASHBOARD_PATH.parent.exists():
        with open(ARTIFACT_DASHBOARD_PATH, "w", encoding="utf-8") as f:
            f.write(html)

    # Also save raw JSON for external APIs / tools
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "stats": stats,
            "top_flights": top_flights_by_date,
            "all_flights": all_flights_by_date
        }, f, indent=2)

def get_booking_urls(airline, date_str, flight_num):
    # Guarantees one-way nonstop search in Google Flights to avoid defaulting to 4-day round-trips
    gflights_url = f"https://www.google.com/travel/flights?q=one-way%20flights%20from%20BCN%20to%20BER%20on%20{date_str}%20nonstop"
    
    # Direct airline official portals
    if airline == "easyJet":
        airline_url = "https://www.easyjet.com/en"
    elif airline == "Vueling":
        airline_url = "https://www.vueling.com/en"
    elif airline == "Ryanair":
        airline_url = f"https://www.ryanair.com/gb/en/trip/flights/select?originIata=BCN&destinationIata=BER&tpAdults=1&tpStartDate={date_str}&isReturn=false"
    else:
        airline_url = gflights_url
    return airline_url, gflights_url

def build_day_options_html_for_email(all_flights_by_date):
    """Builds mobile-responsive HTML cards & full daily flight option tables for emails."""
    date_cards_html = ""
    for d, flights in all_flights_by_date.items():
        weekday = "Sunday" if d == "2027-01-03" else "Monday"
        gflights_day_url = f"https://www.google.com/travel/flights?q=one-way%20flights%20from%20BCN%20to%20BER%20on%20{d}%20nonstop"
        cards = ""
        for idx, f in enumerate(flights, 1):
            border_accent = "#ea580c" if f["airline"] == "easyJet" else ("#ca8a04" if f["airline"] == "Vueling" else "#2563eb")
            badge_bg = "#ffedd5" if f["airline"] == "easyJet" else ("#fef9c3" if f["airline"] == "Vueling" else "#dbeafe")
            badge_text = "#9a3412" if f["airline"] == "easyJet" else ("#854d0e" if f["airline"] == "Vueling" else "#1e40af")
            btn_bg = "#ea580c" if f["airline"] == "easyJet" else ("#ca8a04" if f["airline"] == "Vueling" else "#2563eb")
            if idx == 1:
                rank_tag = '<span style="background-color: #dcfce7; color: #166534; font-size: 10px; font-weight: 800; padding: 2px 6px; border-radius: 4px; margin-left: 6px; display: inline-block;">#1 CHEAPEST</span>'
            elif idx <= 3:
                rank_tag = f'<span style="background-color: #e0f2fe; color: #0369a1; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-left: 6px; display: inline-block;">#{idx} TOP 3</span>'
            else:
                rank_tag = f'<span style="background-color: #f1f5f9; color: #475569; font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px; margin-left: 6px; display: inline-block;">OPTION #{idx}</span>'

            airline_url, gflights_url = get_booking_urls(f["airline"], d, f["flight_number"])

            cards += f"""
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #ffffff; border: 1px solid #e2e8f0; border-left: 5px solid {border_accent}; border-radius: 10px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
              <tr>
                <td style="padding: 14px 16px;">
                  <!-- Top Row: Airline, Flight No, Price -->
                  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 10px;">
                    <tr>
                      <td align="left" valign="middle">
                        <span style="background-color: {badge_bg}; color: {badge_text}; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; display: inline-block;">{f['airline']}</span>
                        <span style="color: #475569; font-size: 12px; font-family: monospace; font-weight: 700; margin-left: 4px;">{f['flight_number']}</span>
                        {rank_tag}
                      </td>
                      <td align="right" valign="middle" style="font-size: 19px; font-weight: 800; color: {'#16a34a' if idx == 1 else '#0f172a'}; white-space: nowrap;">
                        {f['price_str']}
                      </td>
                    </tr>
                  </table>

                  <!-- Middle Row: Times and Route -->
                  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="padding: 8px 10px; background-color: #f8fafc; border-radius: 6px; margin-bottom: 10px;">
                    <tr>
                      <td align="left" style="font-size: 14px; font-weight: 800; color: #0f172a; white-space: nowrap;">
                        {f['departure_time']} <span style="font-size: 11px; color: #64748b; font-weight: 600;">BCN</span>
                      </td>
                      <td align="center" style="color: #64748b; font-size: 11px; font-weight: 600; padding: 0 6px;">
                        ✈ 2h 40m Direct ➔
                      </td>
                      <td align="right" style="font-size: 14px; font-weight: 800; color: #0f172a; white-space: nowrap;">
                        {f['arrival_time']} <span style="font-size: 11px; color: #64748b; font-weight: 600;">BER</span>
                      </td>
                    </tr>
                  </table>

                  <!-- Action Buttons Row: Mobile-friendly full-width 2-column buttons -->
                  <table width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                      <td width="58%" style="padding-right: 6px;">
                        <a href="{gflights_url}" target="_blank" rel="noopener noreferrer" style="display: block; text-align: center; background-color: {btn_bg}; color: #ffffff !important; text-decoration: none; font-size: 12px; font-weight: 700; padding: 9px 10px; border-radius: 6px;">
                          Google Flights ➔
                        </a>
                      </td>
                      <td width="42%">
                        <a href="{airline_url}" target="_blank" rel="noopener noreferrer" style="display: block; text-align: center; background-color: #f8fafc; color: #334155 !important; border: 1px solid #cbd5e1; text-decoration: none; font-size: 12px; font-weight: 700; padding: 8px 10px; border-radius: 6px;">
                          {f['airline']}.com ↗
                        </a>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
            """

        date_cards_html += f"""
        <div style="margin-bottom: 26px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 10px; background-color: #0f172a; border-radius: 8px; padding: 10px 14px;">
            <tr>
              <td align="left" valign="middle">
                <h3 style="margin: 0; color: #ffffff; font-size: 15px; font-weight: 700;">
                  📅 {weekday}, {d} ({len(flights)} Flights)
                </h3>
              </td>
              <td align="right" valign="middle">
                <a href="{gflights_day_url}" target="_blank" rel="noopener noreferrer" style="color: #38bdf8; text-decoration: none; font-size: 12px; font-weight: 700; white-space: nowrap;">
                  Compare on Google Flights ➔
                </a>
              </td>
            </tr>
          </table>
          {cards}
        </div>
        """
    return date_cards_html

def format_rich_html_email(top_flights_by_date, stats, is_final_day, all_flights_by_date=None):
    if all_flights_by_date is None:
        all_flights_by_date = top_flights_by_date

    if is_final_day:
        subject = "FINAL DEADLINE: Cheapest Flights BCN to BER (Jan 3 & 4, 2027) - Immediate Purchase Required"
    else:
        subject = "Daily Flight Alert: BCN to BER (Jan 3 & 4, 2027) - All Flight Options & Top Deals"

    days_left = (FINAL_DEADLINE - date.today()).days

    # Plain text alternative with clickable URLs for ALL possible flights that day
    text_lines = [
        "DAILY FLIGHT SEARCH SUMMARY (ALL POSSIBLE NONSTOP OPTIONS)",
        "Route: Barcelona (BCN) -> Berlin Brandenburg (BER)",
        "Departure Filter: Excluded flights departing before 10:00 AM local time",
        f"Live Mobile-Friendly Web Dashboard: {REMOTE_DASHBOARD_URL}",
        "=" * 65,
    ]
    if is_final_day:
        text_lines.append("[FINAL DEADLINE] Today is November 15, 2026. Please make your purchase immediately!\n")

    for d, flights in all_flights_by_date.items():
        weekday = "Sunday" if d == "2027-01-03" else "Monday"
        text_lines.append(f"\nDate: {weekday}, {d} ({len(flights)} Nonstop Options Available)")
        text_lines.append("-" * 55)
        for idx, f in enumerate(flights, 1):
            airline_url, gflights_url = get_booking_urls(f["airline"], d, f["flight_number"])
            tag = "[#1 CHEAPEST]" if idx == 1 else (f"[#{idx} TOP 3]" if idx <= 3 else f"[Option {idx}]")
            text_lines.append(f"{tag} {f['airline']} {f['flight_number']} | {f['departure_time']} BCN -> {f['arrival_time']} BER | {f['price_str']}")
            text_lines.append(f"  👉 Google Flights (One-Way): {gflights_url}")
            text_lines.append(f"  👉 Airline Direct: {airline_url}")

    plain_text = "\n".join(text_lines)

    # Deadline Banner using table for Gmail compatibility
    if is_final_day:
        deadline_banner = """
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #fee2e2; border-left: 4px solid #ef4444; border-radius: 8px; margin-bottom: 20px;">
          <tr>
            <td style="padding: 14px 16px;">
              <h3 style="margin: 0; color: #991b1b; font-size: 15px; font-weight: 700;">🚨 FINAL PURCHASE DEADLINE TODAY (Nov 15, 2026)</h3>
              <p style="margin: 4px 0 0 0; color: #b91c1c; font-size: 12px;">This is the final automated flight execution. Please lock in your ticket now to avoid price increases.</p>
            </td>
          </tr>
        </table>
        """
    else:
        deadline_banner = f"""
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; margin-bottom: 20px;">
          <tr>
            <td align="left" style="padding: 12px 14px; color: #166534; font-size: 12px; font-weight: 700;">
              ⏳ Cutoff: Nov 15, 2026 ({days_left}d left)
            </td>
            <td align="right" style="padding: 12px 14px; color: #15803d; font-size: 11px; font-weight: 600;">
              All daily options listed below
            </td>
          </tr>
        </table>
        """

    # Compute overall stats
    flat_flights = [dict(f, date=d) for d, flights in top_flights_by_date.items() for f in flights]
    all_current = [f["price"] for f in flat_flights]
    lowest_flight = min(flat_flights, key=lambda x: x["price"]) if flat_flights else {"price": 0.0, "airline": "N/A", "date": "N/A"}
    lowest_now = lowest_flight["price"]
    lowest_email_label = f"{lowest_flight['airline']} ({lowest_flight['date']})"
    avg_now = round(statistics.mean(all_current), 2) if all_current else 0.0
    median_now = round(statistics.median(all_current), 2) if all_current else 0.0

    date_cards_html = build_day_options_html_for_email(all_flights_by_date)

    html_email = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-text-size-adjust: 100%;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f1f5f9; padding: 12px 6px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); border: 1px solid #e2e8f0;">
          
          <!-- Hero Header -->
          <tr>
            <td style="background-color: #0f172a; padding: 24px 18px; color: #ffffff; text-align: center;">
              <div style="display: inline-block; background-color: rgba(255,255,255,0.15); border-radius: 20px; padding: 4px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase; margin-bottom: 10px; color: #38bdf8;">
                ✈️ Daily Price Alert • All Options Included
              </div>
              <h1 style="margin: 0 0 6px 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px; color: #ffffff;">Barcelona (BCN) ➔ Berlin (BER)</h1>
              <p style="margin: 0 0 16px 0; color: #93c5fd; font-size: 12px;">Jan 3 &amp; 4, 2027 • Departures &ge; 10:00 AM • Direct Non-stop</p>
              
              <!-- Prominent Header Action Buttons -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" align="center">
                <tr>
                  <td align="center" style="padding-bottom: 10px;">
                    <a href="{REMOTE_DASHBOARD_URL}" target="_blank" rel="noopener noreferrer" style="display: block; max-width: 340px; margin: 0 auto; background-color: #2563eb; color: #ffffff !important; text-decoration: none; font-size: 13px; font-weight: 700; padding: 11px 18px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
                      📱 Open Mobile-Friendly Live Dashboard ➔
                    </a>
                  </td>
                </tr>
                <tr>
                  <td align="center">
                    <a href="https://www.google.com/travel/flights?q=one-way%20flights%20from%20BCN%20to%20BER%20on%202027-01-03%20nonstop" target="_blank" rel="noopener noreferrer" style="display: inline-block; background-color: #1e293b; color: #cbd5e1 !important; border: 1px solid #475569; text-decoration: none; font-size: 11px; font-weight: 600; padding: 6px 10px; border-radius: 6px; margin: 2px;">
                      🔍 Sun Jan 3 Flights
                    </a>
                    <a href="https://www.google.com/travel/flights?q=one-way%20flights%20from%20BCN%20to%20BER%20on%202027-01-04%20nonstop" target="_blank" rel="noopener noreferrer" style="display: inline-block; background-color: #1e293b; color: #cbd5e1 !important; border: 1px solid #475569; text-decoration: none; font-size: 11px; font-weight: 600; padding: 6px 10px; border-radius: 6px; margin: 2px;">
                      🔍 Mon Jan 4 Flights
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body Container -->
          <tr>
            <td style="padding: 18px 16px;">
              {deadline_banner}

              <!-- Quick Metrics Summary -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 22px;">
                <tr>
                  <td width="32%" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 6px; text-align: center;" valign="top">
                    <div style="color: #64748b; font-size: 10px; font-weight: 700; text-transform: uppercase;">Lowest Fare</div>
                    <div style="color: #16a34a; font-size: 17px; font-weight: 800; margin: 4px 0;">€{lowest_now:.2f}</div>
                    <div style="color: #94a3b8; font-size: 10px;">{lowest_email_label}</div>
                  </td>
                  <td width="2%"></td>
                  <td width="32%" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 6px; text-align: center;" valign="top">
                    <div style="color: #64748b; font-size: 10px; font-weight: 700; text-transform: uppercase;">Top 3 Avg</div>
                    <div style="color: #2563eb; font-size: 17px; font-weight: 800; margin: 4px 0;">€{avg_now:.2f}</div>
                    <div style="color: #94a3b8; font-size: 10px;">Best options</div>
                  </td>
                  <td width="2%"></td>
                  <td width="32%" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 6px; text-align: center;" valign="top">
                    <div style="color: #64748b; font-size: 10px; font-weight: 700; text-transform: uppercase;">Median</div>
                    <div style="color: #7c3aed; font-size: 17px; font-weight: 800; margin: 4px 0;">€{median_now:.2f}</div>
                    <div style="color: #94a3b8; font-size: 10px;">Route median</div>
                  </td>
                </tr>
              </table>

              <!-- All Flight Options Per Day -->
              {date_cards_html}

              <!-- Live Remote Dashboard Card -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #eff6ff; border: 1px solid #bfdbfe; border-radius: 12px; margin-top: 20px;">
                <tr>
                  <td style="padding: 18px 14px; text-align: center;">
                    <div style="font-size: 20px; margin-bottom: 4px;">📊</div>
                    <h4 style="margin: 0 0 6px 0; color: #1e3a8a; font-size: 15px; font-weight: 700;">Interactive Mobile &amp; Desktop Dashboard</h4>
                    <p style="margin: 0 0 14px 0; color: #3b82f6; font-size: 12px; line-height: 1.4;">
                      View touch-friendly flight cards, historical price trends, and airline comparisons on your phone or computer:
                    </p>
                    <a href="{REMOTE_DASHBOARD_URL}" target="_blank" rel="noopener noreferrer" style="display: block; max-width: 300px; margin: 0 auto; background-color: #1d4ed8; color: #ffffff !important; text-decoration: none; font-size: 13px; font-weight: 700; padding: 10px 18px; border-radius: 8px;">
                      Open Live Dashboard ➔
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Direct Airline Booking Hub -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; margin-top: 14px; text-align: center;">
                <tr>
                  <td style="padding: 14px; text-align: center;">
                    <h4 style="margin: 0 0 6px 0; color: #0f172a; font-size: 13px; font-weight: 700;">Direct Airline Portals</h4>
                    <p style="margin: 0 0 10px 0; color: #64748b; font-size: 11px;">Search or book directly on the official airline websites:</p>
                    <a href="https://www.easyjet.com/en" target="_blank" rel="noopener noreferrer" style="display: inline-block; background-color: #ea580c; color: #ffffff !important; text-decoration: none; font-size: 11px; font-weight: 700; padding: 6px 12px; border-radius: 6px; margin: 2px;">easyJet.com ➔</a>
                    <a href="https://www.vueling.com/en" target="_blank" rel="noopener noreferrer" style="display: inline-block; background-color: #ca8a04; color: #ffffff !important; text-decoration: none; font-size: 11px; font-weight: 700; padding: 6px 12px; border-radius: 6px; margin: 2px;">Vueling.com ➔</a>
                    <a href="https://www.ryanair.com" target="_blank" rel="noopener noreferrer" style="display: inline-block; background-color: #2563eb; color: #ffffff !important; text-decoration: none; font-size: 11px; font-weight: 700; padding: 6px 12px; border-radius: 6px; margin: 2px;">Ryanair.com ➔</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 14px 16px; text-align: center; color: #94a3b8; font-size: 11px; line-height: 1.6;">
              Delivered automatically to {', '.join(RECIPIENT_EMAILS)}<br>
              Antigravity Flight Engine • 3 daily automated checks (08:00, 14:00, 20:00 CET)<br>
              <a href="{REMOTE_DASHBOARD_URL}" style="color: #2563eb; text-decoration: none;">View Mobile-Friendly Price Dashboard</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return subject, plain_text, html_email

def send_and_save_email(subject, plain_text, html_email):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    sender_email = os.environ.get("SENDER_EMAIL", smtp_user or "flight-tracker@antigravity.local")

    env_path = Path.home() / ".env"
    if env_path.exists() and (not smtp_user or not smtp_pass):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    k, v = k.strip(), v.strip().strip("\"'")
                    if k == "SMTP_HOST" and not smtp_host: smtp_host = v
                    if k == "SMTP_USER" and not smtp_user: smtp_user = v
                    if k == "SMTP_PASS" and not smtp_pass: smtp_pass = v.replace(" ", "")
                    if k == "SENDER_EMAIL" and not sender_email: sender_email = v

    recipients_str = ", ".join(RECIPIENT_EMAILS)

    # Save local plain report
    with open(REPORT_TXT_PATH, "w", encoding="utf-8") as f:
        f.write(f"Subject: {subject}\nTo: {recipients_str}\n\n{plain_text}")

    print(f"[INFO] Report written to: {REPORT_TXT_PATH}")
    print(f"Subject: {subject}")
    print(f"Recipients: {recipients_str}\n")
    print(plain_text)

    if smtp_host and smtp_user and smtp_pass:
        try:
            print(f"\n[INFO] Sending rich multipart HTML email via {smtp_host}:{smtp_port}...")
            msg = MIMEMultipart("alternative")
            msg["From"] = sender_email
            msg["To"] = recipients_str
            msg["Subject"] = subject

            # Attach plain text and HTML versions
            msg.attach(MIMEText(plain_text, "plain", "utf-8"))
            msg.attach(MIMEText(html_email, "html", "utf-8"))

            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender_email, RECIPIENT_EMAILS, msg.as_string())
            server.quit()
            print(f"[SUCCESS] Rich HTML Email successfully sent to {recipients_str}!")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to send email via SMTP: {e}")
            return False
    else:
        print("\n[CONFIG NEEDED] Outbound SMTP credentials are not yet configured.")
        return False

def format_urgent_buy_email(deal, all_flights_by_date=None):
    subject = f"🚨 [URGENT DEAL < €70] Book Immediately: {deal['airline']} to Berlin for {deal['price_str']}!"
    airline_url, gflights_url = get_booking_urls(deal['airline'], deal['date'], deal['flight_number'])

    same_day_flights = (all_flights_by_date or {}).get(deal['date'], [deal])
    other_lines = []
    for idx, f in enumerate(same_day_flights, 1):
        a_url, g_url = get_booking_urls(f["airline"], deal["date"], f["flight_number"])
        other_lines.append(f"  {idx}. {f['airline']} {f['flight_number']} | {f['departure_time']} BCN -> {f['arrival_time']} BER | {f['price_str']} ({g_url})")
    other_options_txt = "\n".join(other_lines)

    plain_text = f"""*** URGENT PRICE ALERT: FLIGHT UNDER €70 DETECTED! ***
Route: Barcelona (BCN) -> Berlin Brandenburg (BER) [Direct Non-Stop]
Departure Date: {deal['date']}
Airline: {deal['airline']} ({deal['flight_number']})
Times: {deal['departure_time']} BCN -> {deal['arrival_time']} BER
Price: {deal['price_str']}

👉 1-WAY GOOGLE FLIGHTS LINK:
{gflights_url}

👉 AIRLINE DIRECT PORTAL:
{airline_url}

ALL POSSIBLE FLIGHT OPTIONS FOR {deal['date']}:
{other_options_txt}

📊 LIVE MOBILE-FRIENDLY ANALYTICS DASHBOARD:
{REMOTE_DASHBOARD_URL}
"""

    day_options_html = build_day_options_html_for_email(all_flights_by_date or {deal['date']: [deal]})

    html_email = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #fef2f2; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-text-size-adjust: 100%;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #fef2f2; padding: 12px 6px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 25px -5px rgba(220, 38, 38, 0.25); border: 2px solid #ef4444;">
          
          <!-- Urgent Alert Header -->
          <tr>
            <td style="background-color: #b91c1c; padding: 24px 18px; color: #ffffff; text-align: center;">
              <div style="display: inline-block; background-color: #ef4444; color: #ffffff; border-radius: 20px; padding: 4px 14px; font-size: 11px; font-weight: 800; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 10px;">
                ⚡ URGENT PRICE DROP ALERT (&lt; €70)
              </div>
              <h1 style="margin: 0 0 6px 0; font-size: 24px; font-weight: 900; letter-spacing: -0.5px; color: #ffffff;">Book Now: {deal['price_str']} Direct Flight!</h1>
              <p style="margin: 0; color: #fecaca; font-size: 13px;">Barcelona (BCN) ➔ Berlin (BER) • {deal['date']}</p>
            </td>
          </tr>

          <!-- Deal Card -->
          <tr>
            <td style="padding: 18px 16px;">
              <!-- Deal Highlight Box -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #fff1f2; border: 2px dashed #f43f5e; border-radius: 12px; margin-bottom: 20px;">
                <tr>
                  <td style="padding: 18px; text-align: center;">
                    <span style="background-color: #ffe4e6; color: #9f1239; font-size: 12px; font-weight: 800; padding: 4px 12px; border-radius: 12px; text-transform: uppercase;">
                      {deal['airline']} • Flight {deal['flight_number']}
                    </span>
                    <div style="font-size: 40px; font-weight: 900; color: #be123c; margin: 10px 0 6px 0;">
                      {deal['price_str']}
                    </div>
                    <div style="color: #4c0519; font-size: 13px; font-weight: 600;">
                      Direct Non-Stop • {deal['departure_time']} BCN ➔ {deal['arrival_time']} BER (2h 40m)
                    </div>
                  </td>
                </tr>
              </table>

              <!-- Prominent Action Buttons -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 24px;">
                <tr>
                  <td align="center" style="padding-bottom: 10px;">
                    <a href="{gflights_url}" target="_blank" rel="noopener noreferrer" style="display: block; background-color: #e11d48; color: #ffffff !important; font-size: 15px; font-weight: 800; text-decoration: none; padding: 14px 16px; border-radius: 8px; box-shadow: 0 4px 12px rgba(225, 29, 72, 0.35); text-transform: uppercase; text-align: center;">
                      ⚡ BOOK DEAL ON GOOGLE FLIGHTS ➔
                    </a>
                  </td>
                </tr>
                <tr>
                  <td align="center" style="padding-bottom: 10px;">
                    <a href="{airline_url}" target="_blank" rel="noopener noreferrer" style="display: block; background-color: #f1f5f9; color: #334155 !important; border: 1px solid #cbd5e1; text-decoration: none; font-size: 12px; font-weight: 700; padding: 10px 16px; border-radius: 6px; text-align: center;">
                      Or Book Direct on {deal['airline']}.com ➔
                    </a>
                  </td>
                </tr>
                <tr>
                  <td align="center">
                    <a href="{REMOTE_DASHBOARD_URL}" target="_blank" rel="noopener noreferrer" style="color: #2563eb; font-size: 12px; font-weight: 600; text-decoration: underline;">
                      📱 Open Mobile-Friendly Live Web Dashboard
                    </a>
                  </td>
                </tr>
              </table>

              <!-- All Possible Flight Options Section -->
              <h3 style="margin: 0 0 12px 0; color: #0f172a; font-size: 16px; font-weight: 800; border-top: 1px solid #e2e8f0; padding-top: 18px;">
                All Possible Flight Options Tracked Today
              </h3>
              {day_options_html}

              <!-- Explanatory Box -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;">
                <tr>
                  <td style="padding: 12px; font-size: 12px; color: #64748b; line-height: 1.5;">
                    <strong>Why this alert?</strong> Your automated flight monitor notifies you immediately whenever a direct flight drops below €70.00, while also listing all other options for comparison.
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 14px 16px; text-align: center; color: #94a3b8; font-size: 11px; line-height: 1.6;">
              Delivered automatically to {', '.join(RECIPIENT_EMAILS)} • Antigravity Flight Engine<br>
              <a href="{REMOTE_DASHBOARD_URL}" style="color: #2563eb; text-decoration: none;">Live Web Dashboard</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return subject, plain_text, html_email

def check_and_send_urgent_alerts(all_flights_by_date):
    today_str = date.today().isoformat()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    triggered_deals = []
    for d, flights in all_flights_by_date.items():
        for f in flights:
            if f["price"] < URGENT_PRICE_THRESHOLD:
                deal_key = f"{f['flight_number']}_{d}_{f['price']}"
                # Check if already sent today
                cur.execute("SELECT id FROM sent_alerts WHERE alert_date = ? AND alert_key = ?", (today_str, deal_key))
                if not cur.fetchone():
                    deal_obj = dict(f)
                    deal_obj["date"] = d
                    triggered_deals.append((deal_key, deal_obj))

    for deal_key, deal in triggered_deals:
        print(f"\n[URGENT] Direct flight below €{URGENT_PRICE_THRESHOLD} found: {deal['airline']} {deal['flight_number']} at {deal['price_str']}!")
        subj, txt, html = format_urgent_buy_email(deal, all_flights_by_date)
        success = send_and_save_email(subj, txt, html)
        if success:
            cur.execute("INSERT INTO sent_alerts (alert_date, alert_key, price, sent_at) VALUES (?, ?, ?, ?)",
                        (today_str, deal_key, deal["price"], now_str))
            conn.commit()

    conn.close()

def main():
    init_db()
    is_final_day = check_date_condition()

    all_flights_by_date = {}
    top_flights_by_date = {}
    for d in DATES:
        all_d_flights = fetch_all_flights_for_date(d)
        all_flights_by_date[d] = all_d_flights
        top_flights_by_date[d] = all_d_flights[:3]

    # Save to SQLite database (all 3 daily runs record full direct flight inventory for rich analytics)
    record_flights_to_db(all_flights_by_date)

    # Compute comprehensive analytics
    stats = compute_statistics()

    # Generate mobile-friendly interactive web dashboard with all flight options
    generate_dashboard_html(stats, top_flights_by_date, all_flights_by_date)

    # 1. ALWAYS check for urgent price drop (< €70) across all 3 daily runs!
    check_and_send_urgent_alerts(all_flights_by_date)

    # 2. Check if we should send the routine daily summary email:
    # Send once per day (at 08:00 AM, or immediately upon wake-up if the PC was asleep at 08:00 AM)
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT sent_at FROM sent_daily_summaries WHERE summary_date = ?", (today_str,))
    already_sent_today = cur.fetchone() is not None
    conn.close()

    should_send_summary = (
        (datetime.now().hour >= 7 and not already_sent_today)
        or ("--force-summary" in sys.argv)
        or is_final_day
    )

    if should_send_summary:
        print("\n[INFO] Daily Summary execution: Formatting and sending today's summary email...")
        subject, plain_text, html_email = format_rich_html_email(top_flights_by_date, stats, is_final_day, all_flights_by_date)
        if send_and_save_email(subject, plain_text, html_email):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO sent_daily_summaries (summary_date, sent_at) VALUES (?, ?)",
                        (today_str, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            conn.close()
    else:
        print(f"\n[INFO] Scheduled check (Hour {datetime.now().hour}): Analytics & database updated. Today's summary email was already delivered.")

if __name__ == "__main__":
    main()

