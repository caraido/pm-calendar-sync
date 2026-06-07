"""AppFolio v2 Reports API client."""
import time
import requests

from .config import _AF_BASE, _AF_HEADERS, AF_API_DELAY_SEC, log


class AppFolioClient:

    def _post_report(self, report: str, payload: dict = None) -> list[dict]:
        url, results = f"{_AF_BASE}/{report}.json", []
        while url:
            r = requests.post(url, headers=_AF_HEADERS, json=(payload or {}), timeout=30)
            if r.status_code == 429:
                log.warning("AppFolio rate limit — waiting 60s"); time.sleep(60); continue
            r.raise_for_status()
            body = r.json()
            results.extend(body.get("results", []))
            url = body.get("next_page_url"); payload = None
        time.sleep(AF_API_DELAY_SEC)
        return results

    def get_rent_roll(self)       -> list[dict]: return self._post_report("rent_roll")
    def get_owner_directory(self) -> list[dict]: return self._post_report("owner_directory")
    def get_tenant_directory(self)-> list[dict]: return self._post_report("tenant_directory")
    def get_tenant_ledger_month(self, from_date: str, to_date: str) -> list[dict]:
        return self._post_report("tenant_ledger", {"from_date": from_date, "to_date": to_date})
