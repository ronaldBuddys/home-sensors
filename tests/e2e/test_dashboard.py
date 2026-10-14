"""Browser tests (headless Chrome via Playwright). Run: uv run --group e2e pytest -m e2e"""

import socket
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    from aranet_monitor.api.app import create_app
    from aranet_monitor.config import Settings
    from aranet_monitor.db.store import Store, init_db, make_engine
    from aranet_monitor.models import DeviceInfo
    from aranet_monitor.sim.simulator import Aranet4Simulator

    url = f"sqlite:///{tmp_path_factory.mktemp('e2e')}/e2e.sqlite"
    eng = make_engine(url)
    init_db(eng)
    st = Store(eng)
    sim = Aranet4Simulator()
    dev = st.upsert_device(DeviceInfo(sim.address, "Aranet4 E2E", "Aranet4", "sim", 60))
    now = int(time.time())
    st.bulk_insert_raw(dev, sim.generate(now - 40 * 86400, now - 20))
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(create_app(Settings(db_url=url)), port=port, log_level="error")
    )
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no Chrome available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    pg = browser.new_page(viewport={"width": 1360, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector("#chart .main-svg")
    pg.wait_for_selector(".kpi-value")
    yield pg
    assert errors == []
    pg.close()


def label(pg):
    return pg.inner_text("#window-label")


def test_initial_render(page):
    assert "raw" in label(page)
    assert page.inner_text(".kpi-hero .kpi-value").replace(",", "").isdigit()
    assert "Live" in page.inner_text("#freshness")
    assert page.locator("table.summary tbody tr").count() == 4


def test_presets_change_resolution(page):
    page.click("#preset label:has-text('30d')")
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('2 h averages')"
    )
    page.click("#preset label:has-text('1h')")
    page.wait_for_function("document.querySelector('#window-label').innerText.includes('raw')")


def test_zoom_reloads_detail_and_dblclick_resets(page):
    page.click("#preset label:has-text('30d')")
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('2 h averages')"
    )
    box = page.locator("#chart .nsewdrag").first.bounding_box()
    y = box["y"] + box["height"] / 2
    x0 = box["x"] + box["width"] * 0.80
    page.mouse.move(x0, y)
    page.mouse.down()
    page.mouse.move(x0 + box["width"] * 0.04, y, steps=8)
    page.mouse.up()
    # ~1.2 days selected -> drops to raw/fine resolution
    page.wait_for_function(
        "!document.querySelector('#window-label').innerText.includes('2 h averages')", timeout=10000
    )
    assert "2 h" not in label(page)
    page.mouse.dblclick(box["x"] + box["width"] / 2, y)
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('2 h averages')"
    )


def test_theme_toggle(page):
    before = page.evaluate("document.documentElement.dataset.theme")
    page.click("#theme-toggle")
    page.wait_for_function(f"document.documentElement.dataset.theme !== '{before}'")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg in ("rgb(13, 13, 13)", "rgb(249, 249, 247)")


def test_csv_link(page, server):
    href = page.get_attribute("#csv", "href")
    assert href.startswith("/api/v1/devices/1/export.csv?start=")


def test_device_dropdown_opens(page):
    page.click("#device")
    opt = page.get_by_role("option", name="Aranet4 E2E")
    opt.wait_for(state="visible", timeout=5000)
    box = opt.bounding_box()
    assert box and box["width"] > 50


def test_mobile_has_no_horizontal_overflow(browser, server):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector("#chart .main-svg")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    pg.close()
