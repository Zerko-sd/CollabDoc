"""Shared Selenium fixtures for the CollabDoc end-to-end suite.

Start the app first:   python -m uvicorn app.main:app --app-dir server --port 8000
Then run:              pytest selenium_tests -v --html=selenium_report.html --self-contained-html

Selenium Manager finds Chrome and a matching ChromeDriver automatically.
Set CHROME_BIN / CHROMEDRIVER to override, HEADLESS=0 to watch the browsers.
"""
import os
import pytest
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def make_driver():
    opts = webdriver.ChromeOptions()
    if os.environ.get("CHROME_BIN"):
        opts.binary_location = os.environ["CHROME_BIN"]
    if os.environ.get("HEADLESS", "1") == "1":
        opts.add_argument("--headless=new")
    for a in ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,900"]:
        opts.add_argument(a)
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    service = Service(os.environ["CHROMEDRIVER"]) if os.environ.get("CHROMEDRIVER") else Service()
    return webdriver.Chrome(service=service, options=opts)


@pytest.fixture(scope="session")
def browsers():
    """Three real browser sessions: Alice (owner), Bob (editor), Carol (viewer)."""
    ds = {name: make_driver() for name in ("alice", "bob", "carol")}
    yield ds
    for d in ds.values():
        d.quit()


@pytest.fixture(scope="session")
def state():
    return {}
