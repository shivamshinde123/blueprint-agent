"""A fake web app served by Playwright request interception.

Lets replay tests exercise real navigation, real locators, and real conditions
without a network or a live demo site. Requests to the allowlisted localhost
origin are fulfilled from these pages before they ever leave the browser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

BASE = "http://localhost:8081/mock"

LOGIN = """
<!doctype html><html><body>
  <h1>Sign in</h1>
  <label for="u">Username</label><input id="u">
  <label for="p">Password</label><input id="p" type="password">
  <button type="button" onclick="location.href='/mock/dashboard'">Login</button>
</body></html>
"""

DASHBOARD = """
<!doctype html><html><body>
  <h1>Dashboard</h1>
  <a href="/mock/search">Directory</a>
</body></html>
"""

SEARCH = """
<!doctype html><html><body>
  <h1>Directory</h1>
  <label for="q">Employee Name</label><input id="q" placeholder="Type a name">
  <button type="button" onclick="go()">Search</button>
  <div id="out"></div>
  <script>
    function go() {
      var q = document.getElementById('q').value.trim();
      var out = document.getElementById('out');
      if (q === 'Peter Anderson') {
        out.innerHTML =
          '<h2>Records Found</h2>' +
          '<table><tr><td>Job Title</td><td id="jt">Senior Engineer</td></tr>' +
          '<tr><td>Sub Unit</td><td id="su">Engineering</td></tr>' +
          '<tr><td>Balance</td><td id="bal">$12,480.55</td></tr></table>';
      } else {
        out.innerHTML = '<h2>No Records Found</h2>';
      }
    }
  </script>
</body></html>
"""

#: A page where the results row renders but the value cells are empty --
#: exercises the extraction-empty hard failure.
SEARCH_EMPTY_CELLS = SEARCH.replace('id="jt">Senior Engineer<', 'id="jt"><')

PAGES = {
    "/mock/": LOGIN,
    "/mock/login": LOGIN,
    "/mock/dashboard": DASHBOARD,
    "/mock/search": SEARCH,
}


async def serve(page: Page, pages: dict[str, str] | None = None) -> None:
    """Intercept every request and fulfil it from `pages`."""
    table = pages or PAGES

    async def handler(route, request):  # type: ignore[no-untyped-def]
        path = request.url.split("localhost:8081", 1)[-1].split("?", 1)[0]
        body = table.get(path)
        if body is None:
            await route.fulfill(status=404, content_type="text/html", body="<h1>404</h1>")
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    await page.route("**/*", handler)
