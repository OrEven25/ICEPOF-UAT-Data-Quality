"""
Downloads a day's raw execution-report/SECDEF/UDS files and the mapped staging_input
zip from the `enwcaestoragedevts` Azure File Share, using a SAS token rather than
`az login` — this must run unattended in environments (e.g. a scheduled cloud agent)
where interactive Azure AD login isn't available.
"""

from __future__ import annotations

import io
import os
import zipfile

from azure.storage.fileshare import ShareClient

STORAGE_ACCOUNT = "enwcaestoragedevts"
SHARE_NAME = "shared"


def _share_client(sas_token: str) -> ShareClient:
    account_url = f"https://{STORAGE_ACCOUNT}.file.core.windows.net"
    return ShareClient(account_url=account_url, share_name=SHARE_NAME, credential=sas_token)


def download_day(sas_token: str, report_date: str, dest_raw_dir: str, dest_mapped_dir: str) -> None:
    """Downloads everything run_reconciliation.py needs for `report_date`
    (YYYY-MM-DD): raw api_messages/<date>_* files, and the staging_input zip
    (extracted into dest_mapped_dir)."""
    os.makedirs(dest_raw_dir, exist_ok=True)
    os.makedirs(dest_mapped_dir, exist_ok=True)

    share = _share_client(sas_token)

    # --- raw execution report / SECDEF / UDS files ---
    api_messages_dir = share.get_directory_client("icepof/api_messages")
    for item in api_messages_dir.list_directories_and_files():
        name = item["name"]
        if not name.startswith(f"{report_date}_"):
            continue
        file_client = api_messages_dir.get_file_client(name)
        data = file_client.download_file().readall()
        with open(os.path.join(dest_raw_dir, name), "wb") as f:
            f.write(data)

    # --- mapped staging_input zip ---
    staging_dir = share.get_directory_client("icepof/staging_input")
    zip_name = f"{report_date}-ICE_IF.zip"
    zip_client = staging_dir.get_file_client(zip_name)
    zip_bytes = zip_client.download_file().readall()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(dest_mapped_dir)


def list_available_dates(sas_token: str) -> list[str]:
    """Every trading date that currently has a staging_input zip (i.e. mapped output
    has been produced) — used to check whether a given date's file has landed yet."""
    share = _share_client(sas_token)
    staging_dir = share.get_directory_client("icepof/staging_input")
    dates = []
    for item in staging_dir.list_directories_and_files():
        name = item["name"]
        if name.endswith("-ICE_IF.zip"):
            dates.append(name[: -len("-ICE_IF.zip")])
    return sorted(dates)
