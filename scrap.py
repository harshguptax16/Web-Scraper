import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://www.searchiqs.com/CTASH/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# If required, put your REAL proxy here.
# Example:
#
# PROXIES = {
#     "http": "http://user:password@host:port",
#     "https": "http://user:password@host:port",
# }
#
# Leave empty if you are not using one.
PROXIES = {}

GOOGLE_SHEET_URL = os.getenv(
    "GOOGLE_SHEET_URL",
    "YOUR_GOOGLE_SHEET_URL_HERE"
)

GOOGLE_CREDS_FILE = os.getenv(
    "GOOGLE_CREDS_FILE",
    "service_account.json"
)

REQUEST_TIMEOUT = 45
REQUEST_DELAY = 1.0


# ============================================================
# DATE RANGE
# ============================================================

def calculate_dynamic_dates(days_back=80):
    """
    Return dates in MM/DD/YYYY format.
    """
    today = datetime.now()
    from_date = today - timedelta(days=days_back)

    return (
        from_date.strftime("%m/%d/%Y"),
        today.strftime("%m/%d/%Y"),
    )


# ============================================================
# HTTP / ASP.NET HELPERS
# ============================================================

def create_session():
    session = requests.Session()

    session.headers.update(HEADERS)

    if PROXIES:
        session.proxies.update(PROXIES)

    return session


def soup_from_response(response):
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def get_hidden_fields(soup):
    """
    Extract ALL hidden inputs.

    This is important for ASP.NET because __VIEWSTATE,
    __EVENTVALIDATION, __VIEWSTATEGENERATOR, etc. can change
    after every postback.
    """
    data = {}

    for tag in soup.select("input[type='hidden']"):
        name = tag.get("name")

        if not name:
            continue

        data[name] = tag.get("value", "")

    return data


def absolute_action(base_url, soup):
    """
    Return the form's actual action URL.
    """
    form = soup.find("form")

    if not form:
        return base_url

    action = form.get("action")

    if not action:
        return base_url

    return urljoin(base_url, action)


def submit_form(session, url, soup, extra_data=None):
    """
    POST the current ASP.NET form while preserving all hidden
    state fields.
    """
    payload = get_hidden_fields(soup)

    if extra_data:
        payload.update(extra_data)

    action = absolute_action(url, soup)

    response = session.post(
        action,
        data=payload,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    return response, BeautifulSoup(response.text, "html.parser")


# ============================================================
# DEBUGGING HELPERS
# ============================================================

def dump_forms(soup):
    """
    Print all forms and controls. Very useful if SearchIQS
    changes its HTML.
    """
    print("\n========== FORMS / CONTROLS ==========")

    for form_no, form in enumerate(soup.find_all("form"), 1):

        print(
            f"\nFORM {form_no}: "
            f"action={form.get('action')} "
            f"method={form.get('method')}"
        )

        for element in form.find_all(
            ["input", "select", "button", "textarea"]
        ):

            print(
                " ",
                element.name,
                "name=",
                element.get("name"),
                "id=",
                element.get("id"),
                "value=",
                element.get("value"),
                "type=",
                element.get("type"),
            )

            if element.name == "select":
                for option in element.find_all("option"):
                    print(
                        "     OPTION:",
                        repr(option.get_text(strip=True)),
                        "=>",
                        repr(option.get("value")),
                    )

    print("=======================================\n")


def save_debug_html(soup, filename):
    with open(filename, "w", encoding="utf-8") as f:
        f.write(str(soup))

    print(f"[DEBUG] Saved HTML to {filename}")


# ============================================================
# CONTROL DISCOVERY
# ============================================================

def find_guest_button(soup):
    """
    Find the Search Records as Guest button without assuming
    a particular ASP.NET ID.
    """

    # First look at submit inputs/buttons.
    candidates = soup.find_all(
        ["input", "button"]
    )

    for tag in candidates:

        text = (
            tag.get("value", "")
            or tag.get_text(" ", strip=True)
        ).strip()

        if "search records as guest" in text.lower():
            return tag

    return None


def find_search_button(soup):
    """
    Discover the actual Search button.
    """

    candidates = soup.find_all(
        ["input", "button"]
    )

    for tag in candidates:

        text = (
            tag.get("value", "")
            or tag.get_text(" ", strip=True)
        ).strip()

        if text.lower() == "search":
            return tag

    for tag in candidates:

        text = (
            tag.get("value", "")
            or tag.get_text(" ", strip=True)
        ).strip()

        if "search" in text.lower():
            return tag

    return None


def find_select_by_text(soup, wanted_text):
    """
    Find a SELECT whose option text matches wanted_text.
    Returns (select_tag, option_value).
    """

    wanted = wanted_text.strip().lower()

    for select in soup.find_all("select"):

        for option in select.find_all("option"):

            text = option.get_text(
                " ",
                strip=True
            ).lower()

            if text == wanted:

                return select, option.get("value", "")

    return None, None


def find_date_inputs(soup):
    """
    Try to discover From/To date inputs based on names,
    IDs and nearby labels.
    """

    from_input = None
    to_input = None

    # First, use obvious naming conventions.
    for tag in soup.find_all("input"):

        name = (tag.get("name") or "").lower()
        id_ = (tag.get("id") or "").lower()

        combined = f"{name} {id_}"

        if (
            "fromdate" in combined
            or "startdate" in combined
            or "datefrom" in combined
        ):
            from_input = tag

        if (
            "todate" in combined
            or "enddate" in combined
            or "dateto" in combined
        ):
            to_input = tag

    return from_input, to_input


# ============================================================
# POSTBACK HANDLING
# ============================================================

def parse_postback_href(href):
    """
    Parse:

        javascript:__doPostBack('TARGET','ARGUMENT')
    """

    if not href:
        return None, None

    match = re.search(
        r"__doPostBack\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)",
        href,
        flags=re.I,
    )

    if not match:
        return None, None

    return match.group(1), match.group(2)


def postback(session, url, soup, event_target, event_argument):
    """
    Perform an ASP.NET postback using the CURRENT page state.
    """

    payload = get_hidden_fields(soup)

    payload["__EVENTTARGET"] = event_target
    payload["__EVENTARGUMENT"] = event_argument

    # ASP.NET commonly expects these fields.
    payload.setdefault("__LASTFOCUS", "")

    response = session.post(
        absolute_action(url, soup),
        data=payload,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    return response, BeautifulSoup(response.text, "html.parser")


# ============================================================
# RESULT TABLE
# ============================================================

EXPECTED_HEADERS = [
    "Party 1",
    "Party 2",
    "Type",
    "Book-Page",
    "Date",
    "Description",
    "Additional Description",
    "Related",
]


def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        text or ""
    ).strip().lower()


def find_results_table(soup):
    """
    Find the table by looking at its headers rather than relying
    on a hard-coded ASP.NET-generated ID.
    """

    for table in soup.find_all("table"):

        first_row = table.find("tr")

        if not first_row:
            continue

        headers = [
            normalize_text(cell.get_text(" ", strip=True))
            for cell in first_row.find_all(
                ["th", "td"]
            )
        ]

        if not headers:
            continue

        # Look for multiple recognizable result columns.
        matches = 0

        for expected in EXPECTED_HEADERS:
            expected_norm = normalize_text(expected)

            if any(
                expected_norm == header
                or expected_norm in header
                or header in expected_norm
                for header in headers
            ):
                matches += 1

        if matches >= 3:
            return table

    return None


def extract_records(soup):
    """
    Extract records from the discovered results table.
    """

    table = find_results_table(soup)

    if not table:
        return []

    rows = table.find_all("tr")

    if not rows:
        return []

    # Determine whether first row is a header.
    first_cells = rows[0].find_all(
        ["th", "td"]
    )

    first_text = [
        normalize_text(x.get_text(" ", strip=True))
        for x in first_cells
    ]

    start_index = 1 if any(
        "party" in x
        or "description" in x
        or "book" in x
        for x in first_text
    ) else 0

    records = []

    for row in rows[start_index:]:

        cells = row.find_all("td")

        if len(cells) < 8:
            continue

        values = [
            cell.get_text(
                " ",
                strip=True
            )
            for cell in cells
        ]

        record = {
            EXPECTED_HEADERS[i]: values[i]
            for i in range(8)
        }

        records.append(record)

    return records


# ============================================================
# PAGINATION
# ============================================================

def find_next_page(soup, current_page):
    """
    Find the actual ASP.NET pagination link.

    We search for a link containing:
        Page$N
    rather than constructing an ASP.NET control ID.
    """

    next_page = current_page + 1

    patterns = [
        f"Page${next_page}",
        f"Page%24{next_page}",
        f"page={next_page}",
    ]

    for link in soup.find_all("a", href=True):

        href = link.get("href", "")

        if any(
            pattern.lower() in href.lower()
            for pattern in patterns
        ):
            return link

        text = link.get_text(
            " ",
            strip=True
        )

        if text == str(next_page):
            return link

    return None


# ============================================================
# MAIN SCRAPER
# ============================================================

def scrape_searchiqs():
    from_date, to_date = calculate_dynamic_dates()

    print(
        f"[*] Date range: "
        f"{from_date} -> {to_date}"
    )

    session = create_session()

    # --------------------------------------------------------
    # 1. Initial page
    # --------------------------------------------------------

    print("[*] Opening SearchIQS...")

    response = session.get(
        BASE_URL,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    print(
        "[*] Initial URL:",
        response.url
    )

    # --------------------------------------------------------
    # 2. Guest login
    # --------------------------------------------------------

    guest_button = find_guest_button(soup)

    if not guest_button:

        print(
            "[ERROR] Could not find "
            "'Search Records as Guest'."
        )

        dump_forms(soup)
        save_debug_html(
            soup,
            "searchiqs_guest_debug.html"
        )

        raise RuntimeError(
            "SearchIQS guest button was not found."
        )

    guest_name = guest_button.get("name")
    guest_value = (
        guest_button.get("value")
        or guest_button.get_text(
            " ",
            strip=True
        )
    )

    if not guest_name:
        raise RuntimeError(
            "Guest button has no name attribute."
        )

    print(
        "[*] Guest control:",
        guest_name,
        "=",
        guest_value
    )

    response, soup = submit_form(
        session,
        response.url,
        soup,
        {
            guest_name: guest_value
        }
    )

    time.sleep(REQUEST_DELAY)

    print(
        "[*] Guest/login response:",
        response.url
    )

    # --------------------------------------------------------
    # 3. Discover search controls
    # --------------------------------------------------------

    print("[*] Discovering search controls...")

    doc_group_select, land_records_value = (
        find_select_by_text(
            soup,
            "Land Records"
        )
    )

    if not doc_group_select:

        print(
            "[ERROR] Could not find "
            "'Land Records' option."
        )

        dump_forms(soup)
        save_debug_html(
            soup,
            "searchiqs_search_debug.html"
        )

        raise RuntimeError(
            "Land Records dropdown was not found."
        )

    print(
        "[*] Document group:",
        doc_group_select.get("name"),
        "=",
        land_records_value
    )

    from_input, to_input = find_date_inputs(soup)

    if not from_input or not to_input:

        print(
            "[WARNING] Could not automatically "
            "identify both date fields."
        )

        print(
            "[INFO] Available inputs:"
        )

        for tag in soup.find_all("input"):
            print(
                "   ",
                tag.get("name"),
                tag.get("id"),
                tag.get("type")
            )

        save_debug_html(
            soup,
            "searchiqs_date_debug.html"
        )

        raise RuntimeError(
            "Search date controls could not be discovered."
        )

    print(
        "[*] From date:",
        from_input.get("name")
    )

    print(
        "[*] To date:",
        to_input.get("name")
    )

    search_button = find_search_button(soup)

    if not search_button:

        print(
            "[ERROR] Search button not found."
        )

        dump_forms(soup)

        raise RuntimeError(
            "Search button could not be discovered."
        )

    # --------------------------------------------------------
    # 4. Build search request
    # --------------------------------------------------------

    search_data = {
        doc_group_select.get("name"): land_records_value,

        from_input.get("name"): from_date,
        to_input.get("name"): to_date,
    }

    search_button_name = search_button.get("name")

    if search_button_name:
        search_data[search_button_name] = (
            search_button.get("value")
            or search_button.get_text(
                " ",
                strip=True
            )
        )

    print("[*] Submitting search...")

    response, soup = submit_form(
        session,
        response.url,
        soup,
        search_data
    )

    time.sleep(REQUEST_DELAY)

    print(
        "[*] Search response:",
        response.url
    )

    # --------------------------------------------------------
    # 5. Extract records
    # --------------------------------------------------------

    all_records = []
    page_number = 1

    while True:

        print(
            f"[*] Processing result page "
            f"{page_number}..."
        )

        page_records = extract_records(soup)

        print(
            f"[+] Found {len(page_records)} "
            f"records on page {page_number}."
        )

        all_records.extend(page_records)

        # ----------------------------------------------------
        # Pagination
        # ----------------------------------------------------

        next_link = find_next_page(
            soup,
            page_number
        )

        if not next_link:
            print(
                "[*] No next page found."
            )
            break

        href = next_link.get(
            "href",
            ""
        )

        print(
            "[*] Next-page link:",
            href
        )

        event_target, event_argument = (
            parse_postback_href(href)
        )

        if event_target is None:

            print(
                "[*] Next link is not an ASP.NET "
                "postback. Stopping pagination."
            )

            break

        print(
            "[*] Postback:",
            event_target,
            event_argument
        )

        response, soup = postback(
            session,
            response.url,
            soup,
            event_target,
            event_argument
        )

        time.sleep(REQUEST_DELAY)

        page_number += 1

    print(
        f"[SUCCESS] Total records: "
        f"{len(all_records)}"
    )

    return all_records


# ============================================================
# GOOGLE SHEETS
# ============================================================

def export_to_google_sheets(
    data,
    sheet_url,
    creds_path="service_account.json"
):
    if not data:
        print(
            "[WARNING] No records to export."
        )
        return

    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Google credentials not found: "
            f"{creds_path}"
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = (
        Credentials
        .from_service_account_file(
            creds_path,
            scopes=scopes
        )
    )

    client = gspread.authorize(
        credentials
    )

    spreadsheet = client.open_by_url(
        sheet_url
    )

    worksheet = spreadsheet.sheet1

    headers = EXPECTED_HEADERS

    values = [
        headers
    ]

    for record in data:
        values.append([
            record.get(header, "")
            for header in headers
        ])

    print(
        f"[*] Writing {len(data)} records..."
    )

    worksheet.clear()

    # One batch operation is substantially better
    # than append_row() + append_rows().
    worksheet.update(
        range_name="A1",
        values=values,
    )

    print(
        f"[SUCCESS] Google Sheet updated "
        f"with {len(data)} records."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        records = scrape_searchiqs()

        if GOOGLE_SHEET_URL == (
            "YOUR_GOOGLE_SHEET_URL_HERE"
        ):
            print(
                "\n[WARNING] Google Sheet URL "
                "has not been configured."
            )

            print(
                f"[*] Records collected: "
                f"{len(records)}"
            )

        else:

            export_to_google_sheets(
                records,
                GOOGLE_SHEET_URL,
                GOOGLE_CREDS_FILE,
            )

    except requests.RequestException as exc:

        print(
            "\n[HTTP ERROR]",
            exc
        )

        print(
            "[TIP] Check the URL, internet "
            "connection, proxy and SearchIQS availability."
        )

    except Exception as exc:

        print(
            "\n[ERROR]",
            type(exc).__name__,
            str(exc)
        )
        raise