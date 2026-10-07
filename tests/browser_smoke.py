"""Optional live UI smoke: only reads the dashboard's PostgreSQL-backed API.

uv run python tests/browser_smoke.py --base-url https://185.255.132.160
Requires ADMIN_USER/ADMIN_PASSWORD in local ignored .env or environment.
Screenshots go into ignored var/verification. No Ozon calls are triggered.
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    folder = Path("var/verification")
    folder.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, height in [(390, 844), (1280, 900)]:
            page = browser.new_page(
                viewport={"width": width, "height": height},
                is_mobile=width < 800,
                has_touch=width < 800,
            )
            errors = []
            page.on(
                "pageerror", lambda error, captured=errors: captured.append(str(error))
            )
            for _ in range(20):
                if page.request.get(base + "/healthz").status == 200:
                    break
                page.wait_for_timeout(500)
            response = page.goto(base + "/login")
            assert response.status == 200
            page.locator("input[name=username]").fill(
                os.getenv("ADMIN_USER", "analytics")
            )
            page.locator("input[name=password]").fill(os.environ["ADMIN_PASSWORD"])
            page.get_by_role("button", name="Войти").click()
            page.wait_for_selector("#overview .kpi")
            page.screenshot(path=str(folder / f"{width}-overview.png"), full_page=True)
            for tab in ["stocks", "sales", "storage", "server"]:
                page.locator(f'button[data-tab="{tab}"]').click()
                assert page.locator(f"#{tab}").is_visible()
                assert not page.locator("#error").is_visible()
                assert not page.evaluate(
                    "document.documentElement.scrollWidth>innerWidth"
                )
                page.screenshot(path=str(folder / f"{width}-{tab}.png"))
            page.locator("#filter-summary").click()
            for key in ["sku", "cluster", "warehouse"]:
                choices = page.locator(f"#{key} option").all()
                if len(choices) > 1:
                    page.locator(f"#{key}").select_option(
                        choices[1].get_attribute("value")
                    )
            with page.expect_response(lambda r: "/api/dashboard?" in r.url) as response:
                page.locator("#apply").click()
            assert response.value.status == 200
            assert not page.locator("#error").is_visible()
            with page.expect_response(lambda r: "/api/dashboard?" in r.url) as response:
                page.locator('button[data-period="30"]').click()
            assert response.value.status == 200
            assert not errors, errors
            page.get_by_role("button", name="Выйти").click()
            page.wait_for_url(base + "/login")
            assert page.request.get(base + "/api/dashboard").status == 401
            print(
                f"UI {width}x{height}: login, tabs, filters, dates, logout passed; no overflow/JS errors"
            )
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
