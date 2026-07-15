"""
Unattended daily entry point: waits (briefly) for today's mapped output to land in
Azure, downloads the day's raw + mapped data, and runs the reconciliation.

Usage:
    SAS_TOKEN=... python daily_run.py [YYYY-MM-DD]

If no date is given, uses today's UTC date — matching the observed convention that
a given trading day's files are named after that same UTC calendar date, and appear
near the end of that date (~23:00-23:10 UTC), which is why they look like they land
"a few minutes after midnight" in UK local time (BST, UTC+1) the next day.
"""

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icepof_reconciliation.azure_download import download_day, list_available_dates
from run_reconciliation import run_for_date

POLL_INTERVAL_SECONDS = 120
MAX_WAIT_SECONDS = 25 * 60  # 25 minutes


def main():
    sas_token = os.environ.get("SAS_TOKEN")
    if not sas_token:
        print("ERROR: SAS_TOKEN environment variable not set.")
        sys.exit(1)

    report_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Target date: {report_date}")

    waited = 0
    while report_date not in list_available_dates(sas_token):
        if waited >= MAX_WAIT_SECONDS:
            print(f"ERROR: {report_date}'s mapped output still not available after "
                  f"{MAX_WAIT_SECONDS // 60} minutes of polling. Giving up for this run.")
            sys.exit(2)
        print(f"{report_date} not yet available, waiting {POLL_INTERVAL_SECONDS}s...")
        time.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS

    print(f"{report_date} is available. Downloading...")
    raw_dir = f"data/{report_date}/raw"
    mapped_dir = f"data/{report_date}/mapped"
    download_day(sas_token, report_date, raw_dir, mapped_dir)

    print("Running reconciliation...")
    dashboard_payload, csv_files = run_for_date(report_date)

    print(f"DONE. dashboard_export/data/{report_date}.json and "
          f"{len(csv_files)} raw_exports/by_id/*.csv are ready under output/dashboard_export/.")
    print("Next: upload these to the Claude Design project via DesignSync, and add "
          f"'{report_date}' to data/index.json there (see data/README.md in the dashboard project).")


if __name__ == "__main__":
    main()
