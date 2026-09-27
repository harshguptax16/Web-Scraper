from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://www.searchiqs.com"
ASHFORD_URL = f"{BASE_URL}/CTASH/"

OUTPUT_CSV = "ashford_land_records.csv"

# Connecticut local time.
TIMEZONE = "America/New_York"

# Required by assignment.
DAYS_BACK = 80

# Be polite to the website.
REQUEST_DELAY = 1.0

# HTTP timeout.
TIMEOUT = 30

# Optional Google Sheet settings.
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "service-account.json"
)

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

GOOGLE_WORKSHEET_NAME = os.getenv(
    "GOOGLE_WORKSHEET_NAME",
    "Land Records"
)

# Fields requested by assignment.
OUTPUT_FIELDS = [
    "Party 1",
    "Party 2",
    "Type",
    "Book-Page",
    "Date",
    "Description",
    "Additional Description",
    "Related",
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("searchiqs")


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
})


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value: str | None) -> str:
    """Normalize whitespace."""
    if not value:
        return ""

    return re.sub(r"\s+", " ", value).strip()


def get_soup(url: str) -> BeautifulSoup:
    """GET a page and return BeautifulSoup."""
    logger.info("GET %s", url)

    response = session.get(
        url,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    return BeautifulSoup(
        response.text,
        "lxml"
    )


def absolute_url(base: str, href: str) -> str:
    """Convert relative URL to absolute URL."""
    return urljoin(base, href)


def same_domain(url: str) -> bool:
    """Make sure URLs remain on SearchIQS."""
    host = urlparse(url).netloc.lower()

    return (
        host.endswith("searchiqs.com")
        or host == "searchiqs.com"
    )


def sleep_between_requests():
    time.sleep(REQUEST_DELAY)


# ============================================================
# DATE HANDLING
# ============================================================

def get_date_range() -> tuple[str, str]:
    """
    Dynamically calculate:

        From Date = current date - 80 days
        To Date   = current date

    Uses Connecticut/New York timezone.
    """

    now = datetime.now(
        ZoneInfo(TIMEZONE)
    ).date()

    from_date = now - timedelta(
        days=DAYS_BACK
    )

    # SearchIQS commonly accepts MM/DD/YYYY.
    from_string = from_date.strftime("%m/%d/%Y")
    to_string = now.strftime("%m/%d/%Y")

    logger.info(
        "Date range: %s -> %s",
        from_string,
        to_string
    )

    return from_string, to_string


# ============================================================
# FORM HELPERS
# ============================================================

def get_form(soup: BeautifulSoup):
    """
    Find the most relevant HTML form.

    SearchIQS uses ASP.NET-style forms, so we prefer a form
    containing inputs/selects related to searching.
    """

    forms = soup.find_all("form")

    if not forms:
        raise RuntimeError(
            "No HTML form found on the SearchIQS page."
        )

    # Prefer a form containing select controls.
    for form in forms:
        if form.find("select"):
            return form

    return forms[0]


def hidden_form_values(form) -> dict:
    """
    Collect hidden ASP.NET fields such as:

        __VIEWSTATE
        __EVENTVALIDATION
        __VIEWSTATEGENERATOR
        etc.
    """

    payload = {}

    for element in form.select(
        'input[type="hidden"]'
    ):
        name = element.get("name")

        if not name:
            continue

        payload[name] = element.get(
            "value",
            ""
        )

    return payload


def all_form_values(form) -> dict:
    """
    Collect current values from an HTML form.

    This is important for ASP.NET WebForms because the
    server often requires ViewState and other hidden values.
    """

    payload = {}

    # Inputs
    for element in form.find_all("input"):

        name = element.get("name")

        if not name:
            continue

        input_type = (
            element.get("type", "text")
            .lower()
        )

        if input_type in {
            "submit",
            "button",
            "image",
            "reset",
            "file",
        }:
            continue

        if input_type in {
            "checkbox",
            "radio",
        }:
            if element.has_attr("checked"):
                payload[name] = element.get(
                    "value",
                    "on"
                )

            continue

        payload[name] = element.get(
            "value",
            ""
        )

    # Selects
    for select in form.find_all("select"):

        name = select.get("name")

        if not name:
            continue

        selected = select.find(
            "option",
            selected=True
        )

        if selected:
            payload[name] = selected.get(
                "value",
                ""
            )

    # Textareas
    for textarea in form.find_all("textarea"):

        name = textarea.get("name")

        if name:
            payload[name] = textarea.get_text()

    return payload


def normalized_label(text: str) -> str:
    """Normalize a label for comparison."""
    return re.sub(
        r"[^a-z0-9]+",
        "",
        text.lower()
    )


def element_label(element) -> str:
    """
    Try to discover the human-readable label associated
    with an input/select.
    """

    element_id = element.get("id")

    if element_id:
        label = element.find_previous(
            "label",
            attrs={"for": element_id}
        )

        if label:
            return clean_text(
                label.get_text(" ", strip=True)
            )

    # Look around the element.
    parent = element.parent

    if parent:
        text = clean_text(
            parent.get_text(
                " ",
                strip=True
            )
        )

        if len(text) < 200:
            return text

    return ""


def find_control_by_keywords(
    form,
    keywords: list[str],
    tag_names=("input", "select"),
):
    """
    Locate a form control based on:

    - id
    - name
    - placeholder
    - associated label
    """

    normalized_keywords = [
        normalized_label(k)
        for k in keywords
    ]

    candidates = form.find_all(tag_names)

    for element in candidates:

        values = [
            element.get("id", ""),
            element.get("name", ""),
            element.get("placeholder", ""),
            element_label(element),
        ]

        combined = normalized_label(
            " ".join(values)
        )

        for keyword in normalized_keywords:

            if keyword and keyword in combined:
                return element

    return None


def find_select_by_option_text(
    form,
    wanted_text: str,
):
    """
    Find a <select> containing an option whose visible
    text equals/includes the requested text.
    """

    wanted = normalized_label(
        wanted_text
    )

    for select in form.find_all("select"):

        for option in select.find_all("option"):

            text = normalized_label(
                option.get_text(
                    " ",
                    strip=True
                )
            )

            if text == wanted:
                return select, option

            if wanted in text:
                return select, option

    return None, None


# ============================================================
# GUEST ACCESS
# ============================================================

def open_guest_search() -> tuple[str, BeautifulSoup]:
    """
    Open Ashford page and locate "Search Records as Guest".
    """

    soup = get_soup(
        ASHFORD_URL
    )

    guest_link = None

    for link in soup.find_all("a"):

        text = clean_text(
            link.get_text(
                " ",
                strip=True
            )
        )

        if "search records as guest" in text.lower():

            guest_link = link

            break

    if not guest_link:
        raise RuntimeError(
            "Could not find 'Search Records as Guest'. "
            "SearchIQS may have changed its page structure."
        )

    href = guest_link.get("href")

    if not href:
        raise RuntimeError(
            "Guest search link has no href."
        )

    guest_url = absolute_url(
        ASHFORD_URL,
        href
    )

    if not same_domain(guest_url):
        raise RuntimeError(
            f"Unexpected guest URL: {guest_url}"
        )

    sleep_between_requests()

    guest_soup = get_soup(
        guest_url
    )

    logger.info(
        "Guest search URL: %s",
        guest_url
    )

    return guest_url, guest_soup


# ============================================================
# SEARCH FORM
# ============================================================

def build_search_payload(
    form,
    from_date: str,
    to_date: str,
) -> dict:
    """
    Build an ASP.NET-compatible search POST payload.
    """

    payload = all_form_values(form)

    # --------------------------------------------------------
    # Select Land Records
    # --------------------------------------------------------

    document_group, option = (
        find_select_by_option_text(
            form,
            "Land Records"
        )
    )

    if document_group is None:
        raise RuntimeError(
            "Could not find Document Group select "
            "containing 'Land Records'."
        )

    select_name = document_group.get("name")

    if not select_name:
        raise RuntimeError(
            "Document Group select has no name."
        )

    option_value = option.get(
        "value",
        ""
    )

    payload[select_name] = option_value

    logger.info(
        "Document Group: Land Records "
        "(value=%s)",
        option_value
    )

    # --------------------------------------------------------
    # From Date
    # --------------------------------------------------------

    from_control = find_control_by_keywords(
        form,
        [
            "From Date",
            "FromDate",
            "Date From",
            "Begin Date",
        ]
    )

    if from_control is None:
        raise RuntimeError(
            "Could not find From Date field."
        )

    from_name = from_control.get("name")

    if not from_name:
        raise RuntimeError(
            "From Date control has no name."
        )

    payload[from_name] = from_date

    logger.info(
        "From Date field: %s",
        from_name
    )

    # --------------------------------------------------------
    # To Date
    # --------------------------------------------------------

    to_control = find_control_by_keywords(
        form,
        [
            "To Date",
            "ToDate",
            "Date To",
            "Thru Date",
            "Through Date",
        ]
    )

    if to_control is None:
        raise RuntimeError(
            "Could not find To Date field."
        )

    to_name = to_control.get("name")

    if not to_name:
        raise RuntimeError(
            "To Date control has no name."
        )

    payload[to_name] = to_date

    logger.info(
        "To Date field: %s",
        to_name
    )

    # --------------------------------------------------------
    # Search button
    # --------------------------------------------------------

    search_button = None

    for element in form.find_all(
        ["input", "button"]
    ):

        text = clean_text(
            element.get(
                "value",
                ""
            )
        )

        if not text:
            text = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

        if text.lower() in {
            "search",
            "search records",
            "submit",
        }:
            search_button = element

            break

    if search_button:

        name = search_button.get(
            "name"
        )

        if name:
            payload[name] = search_button.get(
                "value",
                text
            )

    return payload


def submit_search(
    url: str,
    soup: BeautifulSoup,
) -> tuple[str, BeautifulSoup]:
    """
    Submit the search form.
    """

    from_date, to_date = get_date_range()

    form = get_form(soup)

    payload = build_search_payload(
        form,
        from_date,
        to_date,
    )

    action = form.get(
        "action"
    ) or url

    post_url = absolute_url(
        url,
        action
    )

    logger.info(
        "POST search form -> %s",
        post_url
    )

    sleep_between_requests()

    response = session.post(
        post_url,
        data=payload,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    result_soup = BeautifulSoup(
        response.text,
        "lxml"
    )

    return response.url, result_soup


# ============================================================
# TABLE EXTRACTION
# ============================================================

FIELD_ALIASES = {
    "Party 1": [
        "party 1",
        "party1",
        "grantor",
    ],

    "Party 2": [
        "party 2",
        "party2",
        "grantee",
    ],

    "Type": [
        "type",
        "document type",
        "doctype",
    ],

    "Book-Page": [
        "book-page",
        "book page",
        "book/page",
        "book",
        "instrument",
    ],

    "Date": [
        "date",
        "recorded date",
        "recording date",
    ],

    "Description": [
        "description",
        "document description",
    ],

    "Additional Description": [
        "additional description",
        "additionaldescription",
    ],

    "Related": [
        "related",
        "related records",
    ],
}


def canonical_header(header: str) -> str | None:
    """
    Map a table header to one of the requested output fields.
    """

    normalized = normalized_label(
        header
    )

    for canonical, aliases in FIELD_ALIASES.items():

        for alias in aliases:

            if normalized == normalized_label(alias):
                return canonical

            if normalized_label(alias) in normalized:
                return canonical

    return None


def extract_tables(soup: BeautifulSoup) -> list[dict]:
    """
    Extract records from all HTML tables.
    """

    records = []

    for table in soup.find_all("table"):

        rows = table.find_all("tr")

        if len(rows) < 2:
            continue

        # ----------------------------------------------------
        # Find header row.
        # ----------------------------------------------------

        header_row = None
        headers = []

        for row in rows:

            cells = row.find_all(
                ["th", "td"]
            )

            values = [
                clean_text(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )
                for cell in cells
            ]

            mapped = [
                canonical_header(v)
                for v in values
            ]

            if any(mapped):
                header_row = row
                headers = mapped
                break

        if header_row is None:
            continue

        # Need at least two useful requested fields.
        if sum(
            h is not None
            for h in headers
        ) < 2:
            continue

        header_index = {}

        for i, header in enumerate(headers):

            if header:
                header_index[header] = i

        # ----------------------------------------------------
        # Extract rows.
        # ----------------------------------------------------

        header_found = False

        for row in rows:

            if row is header_row:
                header_found = True
                continue

            if not header_found:
                continue

            cells = row.find_all(
                ["td", "th"]
            )

            if not cells:
                continue

            values = [
                clean_text(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )
                for cell in cells
            ]

            # Ignore repeated header rows.
            if values and normalized_label(
                values[0]
            ) == normalized_label(
                "Party 1"
            ):
                continue

            record = {
                field: ""
                for field in OUTPUT_FIELDS
            }

            found_data = False

            for field, index in header_index.items():

                if index < len(values):

                    record[field] = values[index]

                    if record[field]:
                        found_data = True

            if found_data:
                records.append(record)

    return records


# ============================================================
# DETAIL PAGE EXTRACTION
# ============================================================

def extract_labeled_values(
    soup: BeautifulSoup,
) -> dict:
    """
    Extract values from detail pages where information is
    presented as label/value pairs instead of a table.
    """

    record = {
        field: ""
        for field in OUTPUT_FIELDS
    }

    # --------------------------------------------------------
    # Common HTML patterns.
    # --------------------------------------------------------

    for element in soup.find_all(
        ["td", "th", "dt", "label", "span", "div"]
    ):

        label_text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if len(label_text) > 100:
            continue

        canonical = canonical_header(
            label_text
        )

        if not canonical:
            continue

        # Try next sibling.
        sibling = element.find_next_sibling()

        if sibling:

            value = clean_text()
            sibling.get_text()