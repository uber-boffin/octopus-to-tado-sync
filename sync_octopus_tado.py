import argparse
import asyncio
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPBasicAuth
from playwright.async_api import async_playwright
from PyTado.http import Action, Endpoint, TadoRequest
from PyTado.interface import Tado

OCTOPUS_API = "https://api.octopus.energy/v1"
OCTOPUS_GRAPHQL = f"{OCTOPUS_API}/graphql/"
UK = ZoneInfo("Europe/London")

# How far (in meter units) a calculated reading may fall below Tado's latest
# before it is treated as an error rather than normal re-anchoring noise
MAX_READING_SHORTFALL = 20


def fail(message):
    """
    Prints an error and exits non-zero so the GitHub Actions run shows as failed.
    """
    print(f"ERROR: {message}")
    sys.exit(1)


def octopus_get(url, api_key, params=None):
    response = requests.get(
        url, params=params, auth=HTTPBasicAuth(api_key, ""), timeout=60
    )
    if response.status_code != 200:
        fail(
            f"Octopus API request failed. Status code: {response.status_code}, Message: {response.text}"
        )
    return response.json()


def octopus_graphql(query, variables=None, token=None):
    headers = {"Authorization": token} if token else {}
    response = requests.post(
        OCTOPUS_GRAPHQL,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=60,
    )
    body = response.json() if response.content else {}
    if response.status_code != 200 or body.get("errors"):
        fail(
            f"Octopus GraphQL request failed. Status code: {response.status_code}, Message: {body or response.text}"
        )
    return body["data"]


def get_kraken_token(api_key):
    return octopus_graphql(
        "mutation($key: String!) { obtainKrakenToken(input: {APIKey: $key}) { token } }",
        {"key": api_key},
    )["obtainKrakenToken"]["token"]


def get_current_gas_unit_rate(api_key, mprn, payment_method):
    """
    Returns (pence_per_kwh_inc_vat, start_date) for the gas tariff currently in
    force on this meter point. start_date is the UK date the rate (or the
    agreement, if later) began.
    """
    account_number = octopus_graphql(
        "{ viewer { accounts { number } } }", token=get_kraken_token(api_key)
    )["viewer"]["accounts"][0]["number"]
    account = octopus_get(f"{OCTOPUS_API}/accounts/{account_number}/", api_key)

    now = datetime.now(timezone.utc)

    def in_force(item):
        starts = datetime.fromisoformat(item["valid_from"])
        ends = item["valid_to"] and datetime.fromisoformat(item["valid_to"])
        return starts <= now and (not ends or now < ends)

    agreements = [
        agreement
        for prop in account["properties"]
        for point in prop["gas_meter_points"]
        if point["mprn"] == mprn
        for agreement in point["agreements"]
    ]
    agreement = next((a for a in agreements if in_force(a)), None)
    if not agreement:
        fail(f"No gas tariff agreement in force for MPRN {mprn}")

    # e.g. G-1R-VAR-22-11-01-G -> product VAR-22-11-01
    tariff_code = agreement["tariff_code"]
    product_code = "-".join(tariff_code.split("-")[2:-1])
    rates = octopus_get(
        f"{OCTOPUS_API}/products/{product_code}/gas-tariffs/{tariff_code}/standard-unit-rates/",
        api_key,
        {"period_from": (now - timedelta(days=1)).isoformat()},
    )["results"]
    # Fixed tariffs have one rate for all payment methods (payment_method is null)
    rate = next(
        (
            r
            for r in rates
            if in_force(r) and r.get("payment_method") in (payment_method, None)
        ),
        None,
    )
    if not rate:
        fail(f"No {payment_method} unit rate in force for tariff {tariff_code}")

    starts = max(
        datetime.fromisoformat(rate["valid_from"]),
        datetime.fromisoformat(agreement["valid_from"]),
    )
    start_date = starts.astimezone(UK).date()
    print(
        f"Octopus gas tariff {tariff_code}: {rate['value_inc_vat']:.3f}p/kWh since {start_date}"
    )
    return rate["value_inc_vat"], start_date


def get_register_readings(api_key, mprn, gas_serial_number, count=5):
    """
    Returns the most recent real register readings for the gas meter (newest first)
    as a list of (read_at, value). These are read from the meter itself by Octopus,
    roughly monthly. Estimates are excluded.

    The REST API only exposes consumption, so this uses Octopus's GraphQL API,
    which accepts the same API key.
    """
    token = get_kraken_token(api_key)

    data = octopus_graphql(
        """
        query($count: Int!) { viewer { accounts { ... on AccountType { properties {
          gasMeterPoints { mprn meters { serialNumber
            readings(first: $count) { edges { node { readAt source registers { value } } } }
          } }
        } } } } }
        """,
        {"count": count},
        token,
    )

    readings = []
    for account in data["viewer"]["accounts"]:
        for prop in account.get("properties") or []:
            for point in prop["gasMeterPoints"]:
                if point["mprn"] != mprn:
                    continue
                for meter in point["meters"]:
                    if meter["serialNumber"] != gas_serial_number:
                        continue
                    for edge in meter["readings"]["edges"]:
                        node = edge["node"]
                        if node["source"] == "ESTIMATE" or not node["registers"]:
                            continue
                        readings.append(
                            (
                                datetime.fromisoformat(node["readAt"]),
                                float(node["registers"][0]["value"]),
                            )
                        )

    return sorted(readings, reverse=True)


def get_consumption_between(api_key, mprn, gas_serial_number, period_from, period_to):
    """
    Sums gas consumption from the Octopus Energy API between two timezone-aware
    datetimes (period_from inclusive, period_to exclusive).
    """
    url = (
        f"{OCTOPUS_API}/gas-meter-points/{mprn}/meters/{gas_serial_number}/consumption/"
    )
    params = {
        "group_by": "day",
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
    }
    total = 0.0

    while url:
        page = octopus_get(url, api_key, params)
        total += sum(interval["consumption"] for interval in page["results"])
        url = page.get("next")
        params = None  # the "next" URL already carries the query string

    return total


def get_latest_interval_end(api_key, mprn, gas_serial_number):
    """
    Returns the end time of the most recent half-hour of consumption data.
    Smart meter data typically lags by a day or more.
    """
    url = (
        f"{OCTOPUS_API}/gas-meter-points/{mprn}/meters/{gas_serial_number}/consumption/"
    )
    page = octopus_get(url, api_key, {"page_size": 1})
    if not page["results"]:
        fail("Octopus returned no consumption data for this meter")
    return datetime.fromisoformat(page["results"][0]["interval_end"])


def calculate_meter_reading(
    api_key, mprn, gas_serial_number, anchor_reading=None, anchor_date=None
):
    """
    Calculates the current meter reading as a real register reading (the anchor)
    plus the half-hourly consumption recorded since.

    By default the anchor is the latest register reading Octopus took from the
    meter, so the result corrects itself every month. A manual anchor (e.g. read
    from the physical meter) can be given instead, treated as taken at the end of
    anchor_date.

    Returns (reading_date, reading), or (None, None) if there is no data yet.
    """
    if anchor_reading is not None:
        anchor_at = datetime.combine(
            anchor_date + timedelta(days=1), time(), tzinfo=timezone.utc
        )
        print(f"Using manual anchor: {anchor_reading} on {anchor_date}")
    else:
        readings = get_register_readings(api_key, mprn, gas_serial_number)
        if not readings:
            fail(
                "Octopus has no register readings for this meter; pass --anchor-reading and --anchor-date"
            )
        anchor_at, anchor_reading = readings[0]
        print(f"Latest Octopus register reading: {anchor_reading} at {anchor_at}")

    data_until = get_latest_interval_end(api_key, mprn, gas_serial_number)
    print(f"Octopus consumption data available until: {data_until}")

    if data_until <= anchor_at:
        print("No consumption data after the anchor yet; no reading to submit")
        return None, None

    usage_since_anchor = get_consumption_between(
        api_key, mprn, gas_serial_number, anchor_at, data_until
    )
    reading = anchor_reading + usage_since_anchor
    # The reading belongs to the day the last half-hour falls in
    reading_date = (data_until - timedelta(minutes=1)).date()
    print(
        f"Anchor {anchor_reading} + usage since {usage_since_anchor:.2f}"
        f" = {reading:.2f} on {reading_date}"
    )
    return reading_date, reading


async def browser_login(url, username, password):

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )  # Set to True if you don't want a browser window
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(url)

        # Click the "Submit" button before login
        await page.wait_for_selector('text="Submit"', timeout=5000)
        await page.click('text="Submit"')

        # Wait for the login form to appear
        await page.wait_for_selector('input[name="loginId"]')

        # Replace with actual selectors for your site
        await page.fill('input[id="loginId"]', username)
        await page.fill('input[name="password"]', password)

        await page.click('button.c-btn--primary:has-text("Sign in")')

        # Optionally take a screenshot
        await page.screenshot(path="screenshot.png")

        await page.wait_for_selector(
            ".text-center.message-screen.b-bubble-screen__spaced", timeout=10000
        )

        # Take a screenshot (optional)
        await page.screenshot(path="after-message.png")
        await browser.close()


def tado_delete(tado, command):
    """
    PyTado has no delete methods for Energy IQ, so send the request directly.
    """
    request = TadoRequest()
    request.command = command
    request.action = Action.RESET  # HTTP DELETE
    request.endpoint = Endpoint.EIQ
    return tado._http.request(request)


def set_tado_tariff(tado, pence, start, end=None):
    # PyTado multiplies by 100 to get Tado's "cents" (pence here), so pass pounds
    result = tado.set_eiq_tariff(
        from_date=start.isoformat(),
        to_date=(end or start).isoformat(),
        tariff=round(pence / 100, 5),
        unit="kWh",
        is_period=end is not None,
    )
    if not isinstance(result, dict) or "id" not in result:
        fail(f"Tado rejected the tariff: {result}")
    return result


def sync_tariff_to_tado(tado, pence, start_date):
    """
    Makes Tado's latest Energy IQ tariff match the current Octopus unit rate.
    When the rate has changed, the open-ended tariff is closed the day before
    the new rate starts and a new open-ended tariff is added from that day.
    """
    tariffs = tado.get_eiq_tariffs().get("tariffs", [])
    latest = max(tariffs, key=lambda t: t["startDate"], default=None)

    if latest and round(latest["tariffInCents"], 3) == round(pence, 3):
        print(f"Tariff in Tado is current: {latest['tariffInCents']}p/kWh")
        return

    if latest and latest["startDate"] >= start_date.isoformat():
        fail(
            f"Tado's latest tariff ({latest['tariffInCents']}p from {latest['startDate']}) conflicts"
            f" with Octopus's {pence:.3f}p from {start_date}; correct it in the Tado app"
        )

    if latest and "endDate" not in latest:
        # Tado has no update endpoint, so replace the open tariff with a closed one
        tado_delete(tado, f"tariffs/{latest['id']}")
        set_tado_tariff(
            tado,
            latest["tariffInCents"],
            date.fromisoformat(latest["startDate"]),
            start_date - timedelta(days=1),
        )
        print(
            f"Closed tariff {latest['tariffInCents']}p/kWh from {latest['startDate']} on {start_date - timedelta(days=1)}"
        )

    set_tado_tariff(tado, pence, start_date)
    print(f"Added tariff {round(pence, 3)}p/kWh from {start_date}")


def tado_login(username, password):
    tado = Tado(token_file_path="/tmp/tado_refresh_token")

    status = tado.device_activation_status()

    if status == "PENDING":
        url = tado.device_verification_url()

        asyncio.run(browser_login(url, username, password))

        tado.device_activation()

        status = tado.device_activation_status()

    if status == "COMPLETED":
        print("Login successful")
    else:
        fail(f"Tado login status is {status}")

    return tado


def get_latest_tado_reading(tado):
    """
    Returns the most recent Energy IQ meter reading held by Tado as a dict
    with "date" and "reading", or None if there are none.
    """
    response = tado.get_eiq_meter_readings()

    if isinstance(response, dict) and "readings" in response:
        readings_list = response["readings"]
    else:
        readings_list = response if isinstance(response, list) else []

    if not readings_list:
        return None
    return max(readings_list, key=lambda r: r.get("date", ""))


def send_reading_to_tado(tado, reading_date, reading):
    """
    Sends the meter reading to Tado using its Energy IQ feature.
    Tado rejects readings lower than ones it already holds, so only submits
    if the new reading is higher than the latest one.
    """

    # Tado only accepts whole numbers. Round down so we never get ahead of the
    # real meter, which would block the next genuine reading.
    new_reading = int(reading)
    print(f"New reading to submit: {new_reading} on {reading_date}")

    latest = get_latest_tado_reading(tado)
    if latest:
        print(
            f"Current latest reading in Tado: {latest.get('reading')} on {latest.get('date')}"
        )
        if latest.get("date", "") >= reading_date.isoformat():
            print(
                "Skipping submission: Tado already has a reading for this date or later"
            )
            return
        shortfall = latest.get("reading", 0) - new_reading
        if shortfall > MAX_READING_SHORTFALL:
            fail(
                f"New reading ({new_reading}) is far below Tado's latest ({latest.get('reading')})."
                " Check the readings in Tado against the physical meter."
            )
        if shortfall > 0:
            # Half-hourly totals and Octopus's monthly register readings differ
            # slightly, so a re-anchor can land just below the last submission
            print(
                f"Skipping submission: new reading ({new_reading}) is slightly below Tado's latest"
                f" ({latest.get('reading')}); waiting for usage to catch up"
            )
            return
        if new_reading == latest.get("reading", 0):
            print("Skipping submission: less than 1 unit used since the last reading")
            return
    else:
        print("No existing readings found in Tado, proceeding with submission")

    result = tado.set_eiq_meter_readings(
        date=reading_date.isoformat(), reading=new_reading
    )
    print(f"Tado response: {result}")
    if isinstance(result, dict) and ("errors" in result or "error" in result):
        fail("Tado rejected the reading")


def parse_args():
    """
    Parses command-line arguments for Tado and Octopus API credentials and meter details.
    """
    parser = argparse.ArgumentParser(
        description="Tado and Octopus API Interaction Script"
    )

    # Tado API arguments
    parser.add_argument("--tado-email", required=True, help="Tado account email")
    parser.add_argument("--tado-password", required=True, help="Tado account password")

    # Octopus API arguments
    parser.add_argument(
        "--mprn",
        required=True,
        help="MPRN (Meter Point Reference Number) for the gas meter",
    )
    parser.add_argument(
        "--gas-serial-number", required=True, help="Gas meter serial number"
    )
    parser.add_argument("--octopus-api-key", required=True, help="Octopus API key")

    # Optional manual anchor, overriding Octopus's own register readings
    parser.add_argument(
        "--anchor-reading",
        type=float,
        help="A real reading from the gas meter's register (optional; defaults to Octopus's latest)",
    )
    parser.add_argument(
        "--anchor-date",
        type=date.fromisoformat,
        help="Date the anchor reading was taken (YYYY-MM-DD), treated as end of day",
    )
    parser.add_argument(
        "--payment-method",
        choices=["DIRECT_DEBIT", "NON_DIRECT_DEBIT"],
        default="DIRECT_DEBIT",
        help="Which Octopus unit rate applies (default DIRECT_DEBIT)",
    )
    parser.add_argument(
        "--no-tariff-sync",
        action="store_true",
        help="Do not keep Tado's Energy IQ tariff in line with Octopus",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate the reading and tariff but do not log in to or submit to Tado",
    )

    args = parser.parse_args()
    if (args.anchor_reading is None) != (args.anchor_date is None):
        parser.error("--anchor-reading and --anchor-date must be given together")
    return args


if __name__ == "__main__":
    args = parse_args()

    reading_date, reading = calculate_meter_reading(
        args.octopus_api_key,
        args.mprn,
        args.gas_serial_number,
        args.anchor_reading,
        args.anchor_date,
    )

    unit_rate = None
    if not args.no_tariff_sync:
        unit_rate = get_current_gas_unit_rate(
            args.octopus_api_key, args.mprn, args.payment_method
        )

    if args.dry_run:
        print("Dry run: not submitting to Tado")
    else:
        tado = tado_login(username=args.tado_email, password=args.tado_password)
        if reading is not None:
            send_reading_to_tado(tado, reading_date, reading)
        if unit_rate:
            sync_tariff_to_tado(tado, *unit_rate)
