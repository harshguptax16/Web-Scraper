# SearchIQS Ashford CT Land Records Scraper

A lightweight Python scraper that dynamically extracts trailing 80 days of land record data from SearchIQS without browser automation.

## Features
- **No Selenium / Playwright:** Relies purely on HTTP postback emulation via `requests.Session`.
- **Dynamic Date Window:** Computes start date (`Today - 80 days`) and end date (`Today`) programmatically.
- **ASP.NET Session Handling:** Automatically parses and passes `__VIEWSTATE` and postback parameters across paginated pages.
- **Direct Export:** Saves data directly into a public Google Sheet with read access.

## Prerequisites & Setup

1. **Clone & Install Dependencies:**
   ```bash
   pip install -r requirements.txt
