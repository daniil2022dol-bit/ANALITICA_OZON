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
            page.add_init_script("""const originalFetch=window.fetch;let delayed=false;
                window.fetch=(...args)=>originalFetch(...args).then(response=>{
                  if(!delayed&&String(args[0]).includes('/api/dashboard?')){
                    delayed=true;return new Promise(resolve=>setTimeout(()=>resolve(response),1500));
                  }return response;
                });""")
            errors = []
            advertising_requests = []
            page.on(
                "request",
                lambda request, captured=advertising_requests: (
                    captured.append(request.url)
                    if "/api/advertising?" in request.url
                    else None
                ),
            )
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
            page.wait_for_url(base + "/")
            page.locator('button[data-tab="stocks"]').click()
            page.wait_for_selector("#stocks .panel")
            assert not page.locator("#include-pickup").is_checked()
            assert not any(
                "ПВЗ_" in name
                for name in page.locator("#warehouse option").all_text_contents()
            )
            snapshot = page.evaluate("model.snapshot_date")
            source = page.request.get(base + f"/api/stocks/source?day={snapshot}")
            assert (
                source.status == 200
                and source.json()["endpoint"] == "/v1/analytics/stocks"
            )
            with page.expect_response(lambda r: "/api/dashboard?" in r.url) as included:
                page.locator("#include-pickup").check()
            assert included.value.status == 200
            page.wait_for_function(
                "() => model.options.warehouses.some(w=>w.name.startsWith('ПВЗ_'))"
            )
            with page.expect_response(lambda r: "/api/dashboard?" in r.url) as hidden:
                page.locator("#include-pickup").uncheck()
            assert hidden.value.status == 200
            page.wait_for_function(
                "() => model.options.warehouses.every(w=>!w.name.startsWith('ПВЗ_'))"
            )
            assert page.evaluate("model.options.products.every(p=>p.name)")
            page.locator('button[data-tab="overview"]').click()
            page.wait_for_selector("#overview .kpi")
            page.screenshot(path=str(folder / f"{width}-overview.png"), full_page=True)
            assert not advertising_requests, "Advertising must load lazily"
            for tab in ["stocks", "sales", "storage", "advertising", "server"]:
                page.locator(f'button[data-tab="{tab}"]').click()
                if tab == "advertising":
                    page.wait_for_selector("#advertising .kpi")
                    assert advertising_requests
                    page.locator("#filter-summary").click()
                    assert not page.locator("#cluster").is_visible()
                    assert page.locator("#campaign").is_visible()
                    options = page.locator("#campaign option").all()
                    if len(options) > 1:
                        page.locator("#campaign").select_option(
                            options[1].get_attribute("value")
                        )
                        with page.expect_response(
                            lambda r: "/api/advertising?" in r.url
                        ) as result:
                            page.locator("#apply").click()
                        assert result.value.status == 200
                    if not page.locator("#campaign").is_visible():
                        page.locator("#filter-summary").click()
                    page.locator("#campaign").select_option("")
                    page.locator("#filter-panel").evaluate("e=>e.open=false")
                assert page.locator(f"#{tab}").is_visible(), (tab, errors)
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
