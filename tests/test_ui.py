"""The GUI page in a real headless browser: queue reordering, Export, Import and the history switch."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time

import pytest
from test_web import get, gui, post  # noqa: F401  (gui is a fixture)
from websockets.sync.client import connect

from grab.browser import _launch_browser, _open_page_ws, find_browser


class Page:
    """Just enough DevTools protocol to load the GUI, run JavaScript in it and collect its errors."""

    def __init__(self, ws_url):
        self.ws = connect(ws_url, max_size=50 * 2**20, open_timeout=10).__enter__()
        self.n, self.errors = 0, []
        self.call("Runtime.enable")

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"])
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def wait(self, expr, what, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.js(expr)
            if v:
                return v
            time.sleep(0.2)
        shown = self.js("document.querySelector('#jobs').innerText.slice(0, 600)")
        problems = [e.get("exception", {}).get("description") or e.get("text") for e in self.errors]
        raise AssertionError(f"timed out waiting for {what}; the list shows: {shown!r}; page errors: {problems}")


@pytest.fixture
def page(gui):  # noqa: F811
    browser = find_browser()
    if not browser:
        pytest.skip("no Chromium-based browser installed")
    base = gui[0]
    with tempfile.TemporaryDirectory(prefix="grab-ui-test-", ignore_cleanup_errors=True) as profile:
        proc, port = _launch_browser(browser, profile, None)
        try:
            pg = Page(_open_page_ws(port, base))
            pg.call("Page.navigate", url=base)
            pg.wait("document.querySelector('#version') && document.querySelector('#version').textContent.length > 0", "the page to start")
            yield pg
            pg.ws.close()
        finally:
            proc.terminate()
            proc.wait(5)


TITLES = "[...document.querySelectorAll('#jobs .job .title')].map(e => e.textContent.slice(-3))"


def test_queue_buttons_reorder_the_list(page, gui, site):  # noqa: F811
    base, _, _ = gui
    post(base + "/api/prefs", {"prefs": {"workers": 1}})
    post(base + "/api/add", {"urls": f"{site}/slow/big.bin", "options": {"mode": "direct", "aria2": False}})  # one slow connection holds the only worker
    page.wait("!!document.querySelector('#jobs .job.running')", "the first download to start")
    post(base + "/api/add", {"urls": "\n".join(f"{site}/files/data.zip?n={n}" for n in (1, 2, 3))})
    page.wait("document.querySelectorAll('#jobs .job.queued').length === 3", "three waiting downloads")

    assert page.js(TITLES)[1:] == ["n=1", "n=2", "n=3"]
    row = lambda n: f"[...document.querySelectorAll('#jobs .job')].find(r => r.querySelector('.title').textContent.endsWith('n={n}'))"  # noqa: E731
    has = lambda n, act: page.js(f"!!{row(n)}.querySelector('[data-act={act}]')")  # noqa: E731
    assert [has(1, "top"), has(1, "bottom")] == [False, True]     # already first: only "back of the line"
    assert [has(2, "top"), has(2, "bottom")] == [True, True]
    assert [has(3, "top"), has(3, "bottom")] == [True, False]     # already last: only "front of the line"

    page.js(f"{row(3)}.querySelector('[data-act=top]').click()")
    page.wait(f"{TITLES}.slice(1).join() === 'n=3,n=1,n=2'", "the list to follow the new order")
    page.js(f"{row(3)}.querySelector('[data-act=bottom]').click()")
    page.wait(f"{TITLES}.slice(1).join() === 'n=1,n=2,n=3'", "the list to follow the old order again")
    assert not page.errors
    post(base + "/api/cancel", {})


def test_export_button_sends_the_current_tab_and_reports(page, gui, site):  # noqa: F811
    base, _, _ = gui
    assert page.js("document.querySelector('#exportLinks').hidden")          # nothing to export yet
    post(base + "/api/add", {"urls": f"{site}/files/data.zip\n{site}/nothing-here"})
    page.wait("!document.querySelector('#exportLinks').hidden", "the Export button")
    # the real endpoint would also reveal the file in Finder, so stand in for it and check what was asked
    page.js("""(() => { const real = window.fetch; window.fetch = (u, o) => String(u).startsWith('/api/export')
        ? (window.__asked = JSON.parse(o.body), Promise.resolve({json: async () => ({count: 2, path: '/tmp/grab-links-all-1.txt'})}))
        : real(u, o); })()""")
    page.js("document.querySelector('[data-f=failed]').click(); document.querySelector('#exportLinks').click()")
    page.wait("window.__asked", "the export request")
    assert page.js("window.__asked") == {"which": "failed", "reveal": True}
    page.wait("document.querySelector('#toast').textContent.startsWith('Saved 2 links')", "the confirmation")
    assert not page.errors


def test_import_button_puts_the_links_in_the_box(page):
    page.js("""(() => { const dt = new DataTransfer();
        dt.items.add(new File(["# my list\\nhttps://example.com/a.mp4\\nhttps://example.com/b.mp4\\n"], "list.txt", {type: "text/plain"}));
        const input = document.querySelector('#importFile'); input.files = dt.files; input.dispatchEvent(new Event('change')); })()""")
    page.wait("document.querySelector('#urls').value.includes('b.mp4')", "the links to appear in the box")
    assert page.js("document.querySelector('#count').textContent") == "2 links"
    page.js("""(() => { const dt = new DataTransfer(); dt.items.add(new File(["nothing to see"], "empty.txt"));
        const input = document.querySelector('#importFile'); input.files = dt.files; input.dispatchEvent(new Event('change')); })()""")
    page.wait("document.querySelector('#toast').textContent === 'No links found in that file'", "the warning")
    assert page.js("document.querySelector('#count').textContent") == "2 links"     # the box is untouched
    assert not page.errors


def test_history_checkbox_is_saved(page, gui):  # noqa: F811
    base, _, _ = gui
    assert page.js("document.querySelector('#keep_history').checked") is True      # on unless you turn it off
    page.js("document.querySelector('#keep_history').click()")
    deadline = time.time() + 10
    while get(base + "/api/config")["prefs"].get("keep_history") is not False:
        assert time.time() < deadline, "the setting was never saved"
        time.sleep(0.2)
    assert not page.errors
