"""Enter the data workspace through the startup account dialog when unauthenticated."""


def open_data_page(page, origin):
    page.goto(origin, wait_until="domcontentloaded")
    if page.locator("#accountRestoreStatus").count():
        page.wait_for_selector('#manageAccounts[aria-busy="false"]', timeout=15000)
    if page.locator("#accountsDialog").is_visible():
        page.locator('#accountsDialog .modal-footer [data-close]').click()
