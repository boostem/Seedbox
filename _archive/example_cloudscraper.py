"""
Basic cloudscraper example: fetch the Wikipedia page for Anthropic
and print what it sees.

Install dependencies first:
    pip install cloudscraper beautifulsoup4
"""

import cloudscraper
from bs4 import BeautifulSoup

URL = "https://en.wikipedia.org/wiki/Anthropic"


def main():
    # Create a scraper session (behaves like a requests.Session)
    scraper = cloudscraper.create_scraper()

    response = scraper.get(URL, timeout=30)
    print(f"Status code: {response.status_code}")
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Page title
    title = soup.find("h1", id="firstHeading")
    print(f"Title: {title.get_text(strip=True) if title else '(not found)'}")
    print("-" * 60)

    # Main article body, with scripts, styles, and citation markers removed
    content = soup.find("div", id="mw-content-text")
    if content:
        for tag in content.find_all(["script", "style", "sup"]):
            tag.decompose()
        text = content.get_text("\n", strip=True)
    else:
        text = soup.get_text("\n", strip=True)

    print(text)


if __name__ == "__main__":
    main()
