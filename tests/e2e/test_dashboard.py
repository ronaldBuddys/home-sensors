"""Browser tests (headless Chrome via Playwright). Run: uv run --group e2e pytest -m e2e"""

import json
import re
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


def test_heatmap_card_renders(page):
    page.wait_for_selector("#heatmap .main-svg .hm image", timeout=10000)
    card = page.locator(".card:has(#heatmap)")
    assert "When is CO₂ highest?" in card.inner_text()
    ticks = page.locator("#heatmap .ytick text")
    y = {ticks.nth(i).text_content(): ticks.nth(i).bounding_box()["y"] for i in range(7)}
    assert y["Mon"] < y["Tue"] < y["Sun"]  # Monday on top


def test_presets_change_resolution(page):
    page.click("#preset label:has-text('30d')")
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('2 h averages')"
    )
    page.click("#preset label:has-text('1h')")
    page.wait_for_function("document.querySelector('#window-label').innerText.includes('raw')")


def _changed(request) -> list[str]:
    return json.loads(request.post_data or "{}").get("changedPropIds", [])


def _is_zoom(request) -> bool:
    """A request carrying a drag-zoom's x range that the preset buttons didn't trigger."""
    return "xaxis.range" in (request.post_data or "") and "preset.value" not in _changed(request)


# Dash's own store (what dash.testing inspects): `queued` callbacks are waiting to be sent,
# `busy` ones are also in flight or being applied.
_DASH = "window.store.getState().callbacks"
QUEUED = f"['requested', 'prioritized', 'blocked'].some(k => {_DASH}[k].length)"
BUSY = f"Object.keys({_DASH}).some(k => !['stored', 'completed'].includes(k) && {_DASH}[k].length)"
RELAYOUT = """(() => {
    const s = window.store.getState();
    let chart = s.layout;
    for (const k of s.paths.strs.chart) chart = chart[k];
    return chart.props.relayoutData || {};
})()"""


def test_preset_survives_relayout_mid_request(page):
    # #25: any chart relayout, e.g. Plotly's own {"autosize": true} after the first draw,
    # used to re-run the preset callback while the click's request was in flight; Dash kept
    # only the newer request, so the click was lost. Hold the click's request until Dash has
    # handled a relayout, then release it: the preset must still win. Slow CPU as on a Pi 3.
    # The Pan button guarantees a relayout even if the autosize one came before the click
    # (Dash only fires callbacks for props that change).
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
    held = []

    def hold(route, request):
        if "preset.value" in _changed(request) and not held:
            held.append(route)
        else:
            route.continue_()

    page.route(re.compile(r"/_dash-update-component"), hold)
    with page.expect_request(lambda r: "preset.value" in _changed(r)):
        page.click("#preset label:has-text('30d')")
    before = page.evaluate(f"JSON.stringify({RELAYOUT})")
    page.click("#chart .modebar-btn[data-title='Pan']")
    page.wait_for_function(f"JSON.stringify({RELAYOUT}) !== {json.dumps(before)} && !({QUEUED})")
    held[0].continue_()
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('2 h averages')",
        timeout=10000,
    )
    page.click("#preset label:has-text('1h')")
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('raw')", timeout=10000
    )


def test_preset_beats_an_older_zoom_still_in_flight(page):
    # The user's newest choice wins: hold a drag-zoom's request, click 30d and let it render,
    # then release the zoom. Its late answer must not replace the 30d window.
    held = []

    def hold(route, request):
        if _is_zoom(request) and not held:
            held.append(route)
        else:
            route.continue_()

    # Let Plotly's first-draw autosize relayout land first: only the zoom is in flight.
    page.wait_for_function(f"{RELAYOUT}.autosize && !({BUSY})")
    page.route(re.compile(r"/_dash-update-component"), hold)
    box = page.locator("#chart .nsewdrag").first.bounding_box()
    y = box["y"] + box["height"] / 2
    with page.expect_request(_is_zoom):
        page.mouse.move(box["x"] + box["width"] * 0.6, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.8, y, steps=5)
        page.mouse.up()
    page.click("#preset label:has-text('30d')")
    page.wait_for_function(
        "document.querySelector('#window-label').innerText.includes('2 h averages')",
        timeout=10000,
    )
    with page.expect_response(lambda r: _is_zoom(r.request)):
        held[0].continue_()
    page.wait_for_function(f"!({BUSY})", timeout=10000)
    assert "2 h averages" in label(page)
    assert page.locator("#preset label.selected").inner_text() == "30d"


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


def test_dblclick_on_unzoomed_preset_keeps_it(page):
    page.click("#preset label:has-text('7d')")
    page.wait_for_function("document.querySelector('#window-label').innerText.includes('15 min')")
    box = page.locator("#chart .nsewdrag").first.bounding_box()
    page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(800)
    assert "15 min" in label(page)
    assert page.locator("#preset label.selected").inner_text() == "7d"
