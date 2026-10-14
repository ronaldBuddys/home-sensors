"""Screenshot the dashboard with headless Chrome (dev tool; also used for README images).

Usage: uv run --group e2e python scripts/screenshot.py URL OUT.png [--dark] [--preset 7d]
                                                        [--width 1360] [--mobile]
Prints browser console errors and page timing so it doubles as a smoke test.
"""

import argparse
import time

from playwright.sync_api import sync_playwright


def main() -> None:
    """Take the screenshot."""
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--preset", default=None)
    ap.add_argument("--width", type=int, default=1360)
    ap.add_argument("--mobile", action="store_true")
    a = ap.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        width = 412 if a.mobile else a.width
        page = browser.new_page(viewport={"width": width, "height": 900}, device_scale_factor=1)
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        if a.dark:
            page.add_init_script("localStorage.setItem('theme', JSON.stringify('dark'))")
        t0 = time.perf_counter()
        page.goto(a.url, wait_until="networkidle")
        page.wait_for_selector("#chart .main-svg", timeout=20000)
        page.wait_for_selector(".kpi-value", timeout=20000)
        page.wait_for_timeout(400)
        print(f"loaded+rendered in {time.perf_counter() - t0:.2f}s")
        if a.preset:
            t0 = time.perf_counter()
            page.click(f"#preset label:has-text('{a.preset}')")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(300)
            print(f"preset {a.preset} in {time.perf_counter() - t0:.2f}s")
        page.screenshot(path=a.out, full_page=True)
        print("console errors:", errors or "none")
        browser.close()


if __name__ == "__main__":
    main()
