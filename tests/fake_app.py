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
      if (q === 'Sauce Labs Backpack') {
        out.innerHTML =
          '<h2>Records Found</h2>' +
          '<table><tr><td>Item Name</td><td id="jt">Sauce Labs Backpack</td></tr>' +
          '<tr><td>Item Price</td><td id="su">$29.99</td></tr>' +
          '<tr><td>Balance</td><td id="bal">$12,480.55</td></tr></table>';
      } else {
        out.innerHTML = '<h2>Your cart is empty</h2>';
      }
    }
  </script>
</body></html>
"""

#: A page where the results row renders but the value cells are empty --
#: exercises the extraction-empty hard failure.
SEARCH_EMPTY_CELLS = SEARCH.replace('id="jt">Sauce Labs Backpack<', 'id="jt"><')

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


# --------------------------------------------------------------------------
# A tiny store, for testing that an artifact works with inputs it never saw
# --------------------------------------------------------------------------
#
# Two products with different prices. An artifact recorded against one must
# return the *other's* price when replayed with the other's name -- which is
# the property the whole system exists to deliver, and the one that no amount
# of schema validation can establish.

CATALOGUE = {
    "Widget": ("$29.99", "A sturdy widget for everyday use."),
    "Gizmo": ("$9.99", "A compact gizmo that fits in a pocket."),
}

STORE_LIST = """
<!doctype html><html><body>
  <h1>Catalogue</h1>
  <ul>
    <li><a href="/mock/item?name=Widget">Widget</a></li>
    <li><a href="/mock/item?name=Gizmo">Gizmo</a></li>
  </ul>
</body></html>
"""


def _item_page(name: str) -> str:
    price, description = CATALOGUE.get(name, ("", ""))
    if not price:
        return "<!doctype html><html><body><h1>No such product</h1></body></html>"
    return f"""
<!doctype html><html><body>
  <h1>{name}</h1>
  <div class="detail">
    <label for="price">Price</label><input id="price" value="{price}" readonly>
    <label for="desc">Description</label><input id="desc" value="{description}" readonly>
  </div>
  <a href="/mock/store">Back to catalogue</a>
</body></html>
"""


async def serve_store(page: Page) -> None:
    """Serve the catalogue, with per-product detail pages."""

    async def handler(route, request):  # type: ignore[no-untyped-def]
        url = request.url
        path = url.split("localhost:8081", 1)[-1]
        if path.startswith("/mock/item"):
            name = ""
            if "name=" in path:
                name = path.split("name=", 1)[1].split("&")[0].replace("%20", " ")
            body = _item_page(name)
        elif path.startswith("/mock/store"):
            body = STORE_LIST
        else:
            await route.fulfill(status=404, content_type="text/html", body="<h1>404</h1>")
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    await page.route("**/*", handler)
