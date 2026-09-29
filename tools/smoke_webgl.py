"""Optional browser acceptance for spatial playback (geometry fixture only).

Run against a freshly started Django checkout with Playwright and WebGL2:
    $env:ROBOT_MARKET_BASE_URL='http://127.0.0.1:8013'
    .\.venv\Scripts\python.exe tools\smoke_webgl.py

The fixture contains no operational/financial data and is never persisted.
"""

import hashlib
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright


BASE = os.environ.get("ROBOT_MARKET_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SCENE = {
    "floors": [
        {"label": "Этаж 1", "nodes": [
            {"label": "Лифт", "x": "150", "y": "170", "on_route": True},
            {"label": "Станция", "x": "450", "y": "200", "on_route": True},
        ], "edges": [{"id": "corridor", "start": {"x": "150", "y": "170"},
                       "end": {"x": "450", "y": "200"}, "on_route": True,
                       "resource_id": "aisle-1"}]},
        {"label": "Этаж 2", "nodes": [
            {"label": "Лифт", "x": "150", "y": "170", "on_route": True},
            {"label": "Палата", "x": "450", "y": "200", "on_route": True},
        ], "edges": [{"id": "hospital", "start": {"x": "150", "y": "170"},
                       "end": {"x": "450", "y": "200"}, "on_route": True}]},
    ],
    "transitions": [{"from_floor": "Этаж 1", "to_floor": "Этаж 2",
                     "from_label": "Лифт", "to_label": "Лифт"}],
}

HTML = """
<link rel="stylesheet" href="/static/projects/playback.css">
<section class="playback-3d" data-view-panel="3d" data-facility="hospital">
  <div class="playback-3d-toolbar"><button type="button" data-3d-reset>Сбросить вид</button></div>
  <canvas data-scene3d tabindex="0"></canvas>
  <p data-3d-error hidden role="status"></p>
</section>
"""


def marker(fraction, kind="elevator"):
    return {"key": "slot-1:2", "robotId": "slot-1", "sourceRow": 2,
            "floor": "Этаж 1", "x": 150, "y": 170, "targetFloor": "Этаж 2",
            "targetX": 150, "targetY": 170, "kind": kind,
            "fraction": fraction, "resourceId": "aisle-1"}


with sync_playwright() as browser_runtime:
    browser = browser_runtime.chromium.launch(
        channel=os.environ.get("ROBOT_MARKET_BROWSER_CHANNEL", "msedge"), headless=True,
        args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"],
    )
    try:
        page = browser.new_page(viewport={"width": 1300, "height": 800})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(BASE + "/catalog/", wait_until="domcontentloaded")
        assert response and response.ok, "Django catalogue server did not start"
        page.set_content(HTML.replace('<section class="playback-3d"',
                                      '<section hidden class="playback-3d"'))
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.add_script_tag(url=BASE + "/static/projects/scene3d.js")
        page.evaluate("""(scene) => {
          const panel = document.querySelector('[data-view-panel="3d"]');
          window.playback3d = window.createPlayback3D(panel, scene);
        }""", SCENE)
        assert not page.evaluate("window.playback3d.isWebGL")
        assert not any("/webgl3d.js" in url for url in requests), (
            "3D library must not be downloaded until the 3D view is opened"
        )
        page.evaluate("""() => {
          document.querySelector('[data-view-panel="3d"]').hidden = false;
          window.playback3d.resize();
        }""")
        page.wait_for_function("window.playback3d && window.playback3d.isWebGL", timeout=20000)
        selector = "canvas[data-webgl-scene]"
        canvas = page.locator(selector)
        assert canvas.is_visible(), "WebGL canvas is not mounted"
        assert page.evaluate("document.querySelector('canvas[data-webgl-scene]').getContext('webgl2') !== null")
        assert page.evaluate("document.querySelector('canvas[data-webgl-scene]').width > 100")
        page.evaluate("(m) => window.playback3d.setState({markers:[m]})", marker(0))
        low = canvas.screenshot()
        page.evaluate("(m) => window.playback3d.setState({markers:[m]})", marker(1))
        high = canvas.screenshot()
        assert hashlib.sha256(low).digest() != hashlib.sha256(high).digest(), (
            "Elevator stage does not move the robot between floors"
        )
        page.evaluate("(m) => window.playback3d.setState({markers:[m]})", marker(0, "resource_wait"))
        waiting = canvas.screenshot()
        assert hashlib.sha256(low).digest() != hashlib.sha256(waiting).digest(), (
            "Wait stage does not change robot presentation"
        )
        page.evaluate("(m) => window.playback3d.setState({markers:[m],occupiedResources:{}})",
                      marker(0, "resource_wait"))
        idle_passage = canvas.screenshot()
        page.evaluate("(m) => window.playback3d.setState({markers:[m],occupiedResources:{'aisle-1':1}})",
                      marker(0, "resource_wait"))
        occupied_passage = canvas.screenshot()
        assert hashlib.sha256(idle_passage).digest() != hashlib.sha256(occupied_passage).digest(), (
            "Shared physical resource occupation does not change 3D edge"
        )
        page.evaluate("() => window.playback3d.setState({markers:[]})")
        without_robot = canvas.screenshot()
        assert hashlib.sha256(occupied_passage).digest() != hashlib.sha256(without_robot).digest()
        page.evaluate("(m) => window.playback3d.setState({markers:[m],occupiedResources:{'aisle-1':1}})",
                      marker(0, "resource_wait"))
        assert hashlib.sha256(without_robot).digest() != hashlib.sha256(canvas.screenshot()).digest(), (
            "Removing and recreating a robot damaged shared labels or geometry"
        )
        page.locator("button[data-3d-reset]").click()
        canvas.focus()
        canvas.press("ArrowLeft")
        turned = canvas.screenshot()
        assert hashlib.sha256(occupied_passage).digest() != hashlib.sha256(turned).digest(), (
            "Camera key control does not change the scene"
        )
        output = Path(tempfile.gettempdir()) / "robot-market-webgl-smoke.png"
        output.write_bytes(occupied_passage)
        page.evaluate("""() => document.querySelector('[data-webgl-scene]')
          .dispatchEvent(new Event('webglcontextlost', {cancelable: true}))""")
        assert not page.evaluate("window.playback3d.isWebGL"), "WebGL context loss did not deactivate 3D"
        assert page.locator("canvas[data-scene3d]").is_visible(), (
            "Canvas2D playback did not recover after WebGL context loss"
        )
        assert page.get_by_text("Аппаратный 3D-просмотр недоступен").is_visible()
        assert not errors, errors
        print("PASS lazy WebGL2 canvas; elevator; wait; resource edge; camera; context loss; no errors")
        print(f"SCREENSHOT {output}")
        page.close()

        fallback_page = browser.new_page(viewport={"width": 1300, "height": 800})
        fallback_errors = []
        fallback_page.on("pageerror", lambda error: fallback_errors.append(str(error)))
        fallback_page.goto(BASE + "/catalog/", wait_until="domcontentloaded")
        fallback_page.evaluate("""() => {
          const getContext = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function (name, ...args) {
            if (name === 'webgl2') return null;
            return getContext.call(this, name, ...args);
          };
        }""")
        fallback_page.set_content(HTML)
        fallback_page.add_script_tag(url=BASE + "/static/projects/scene3d.js")
        fallback_page.evaluate("""(scene) => {
          window.playback3d = window.createPlayback3D(
            document.querySelector('[data-view-panel="3d"]'), scene);
        }""", SCENE)
        fallback_page.evaluate("(m) => window.playback3d.setState({markers:[m]})", marker(0))
        assert not fallback_page.evaluate("window.playback3d.isWebGL")
        assert fallback_page.locator("canvas[data-scene3d]").is_visible()
        assert fallback_page.locator("canvas[data-webgl-scene]").count() == 0
        assert not fallback_errors, fallback_errors
        print("PASS Canvas2D fallback without WebGL2; no page errors")
    finally:
        browser.close()
