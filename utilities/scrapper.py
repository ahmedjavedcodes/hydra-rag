import os
import re
import sys
import time
from io import BytesIO
from urllib.parse import parse_qsl, urljoin, urlparse
from playwright.sync_api import sync_playwright

from PIL import Image
import requests
import urllib3
from bs4 import BeautifulSoup

scraped_data = "./data"
os.makedirs(scraped_data, exist_ok=True)


def fix_url_schema(url: str) -> str:
    """Fixes common URL typos such as missing slashes ('https:/domain.com' -> 'https://domain.com')."""
    if not url or not isinstance(url, str):
        return ""

    cleaned_url = url.strip().replace("\n", "").replace("\r", "")

    # Fix single slash after protocol scheme
    cleaned_url = re.sub(r"^(https?):/(?!/)", r"\1://", cleaned_url)

    # Prepend https:// if no scheme is provided
    if not cleaned_url.startswith(("http://", "https://")):
        cleaned_url = "https://" + cleaned_url

    return cleaned_url


def sanitize_and_generate_dynamic_folder(raw_url: str) -> tuple:
    """Cleans trailing whitespaces, fixes broken URL slashes, and generates safe folder paths."""
    sanitized_url = fix_url_schema(raw_url)
    if not sanitized_url:
        return None, None

    parsed = urlparse(sanitized_url)
    netloc = parsed.netloc.lower()

    if not netloc:
        netloc = "unknown_domain"

    query_dict = dict(parse_qsl(parsed.query, keep_blank_values=False))
    domain_clean = netloc.replace("www.", "")
    id_marker = (
        query_dict.get("id") or query_dict.get("oga_service") or "index"
    )

    id_marker_clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(id_marker))[
        :30
    ].strip("_")
    final_folder_name = f"{domain_clean}_{id_marker_clean}"

    unique_target_path = os.path.normpath(
        os.path.join(scraped_data, final_folder_name)
    )

    return sanitized_url, unique_target_path


def get_target_subfolder(filename: str, content_type: str = "") -> str:
    """Maps extracted files to target subfolders based on extension or Content-Type."""
    ext = os.path.splitext(filename)[1].lower()

    if ext in [".csv", ".xlsx", ".xls"] or any(
        t in content_type for t in ["csv", "excel", "spreadsheet"]
    ):
        return "spreadsheets"
    elif ext in [".pdf", ".docx", ".doc", ".txt", ".rtf"] or any(
        t in content_type for t in ["pdf", "word", "text/plain"]
    ):
        return "documents"
    elif ext in [".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp"] or "image" in content_type:
        return "images"
    return "others"


import os
import re
from io import BytesIO
from urllib.parse import parse_qsl, urljoin, urlparse

from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import sync_playwright
import requests
import urllib3


def fetch_dynamic_html_with_playwright(url: str) -> str:
    """Launches Playwright using the local installed Chrome browser."""
    with sync_playwright() as p:
        # Pass channel="chrome" or channel="msedge" to use locally installed browsers
        browser = p.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)

            html_content = page.content()
            browser.close()
            return html_content
        except Exception as e:
            print(f"   ✗ Playwright JS Rendering failed: {e}")
            browser.close()
            return ""

def dynamic_session_extractor(target_url: str, session_folder_path: str):
    # Sanitize inputs
    target_url = target_url.strip() if target_url else ""
    if not target_url or not session_folder_path:
        print(" ✗ Error: Target URL or session folder path is invalid.")
        return

    # Ensure path stays inside ./data
    if not session_folder_path.startswith("./data"):
        session_folder_path = os.path.join("./data", session_folder_path)

    os.makedirs(session_folder_path, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        response = requests.get(
            target_url,
            headers=headers,
            stream=True,
            timeout=20,
            verify=False,
        )

        if response.status_code == 404:
            print("   ⚠ 404 Server Status: Target rejected or record missing.")
            return

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        content_disposition = response.headers.get("Content-Disposition", "")

        is_binary = any(
            t in content_type
            for t in [
                "pdf",
                "spreadsheet",
                "excel",
                "word",
                "octet-stream",
                "zip",
                "csv",
            ]
        )
        is_forced = "filename=" in content_disposition

        # --- SCENARIO A: Direct File Streams (PDF, Word, Excel, CSV) ---
        if is_binary or is_forced:
            if "text/html" in content_type:
                return

            if is_forced:
                filename = re.findall(
                    r'filename=["\']?(.*?)["\']?$', content_disposition
                )[0]
            else:
                filename = os.path.basename(urlparse(target_url).path)

            if not filename or not os.path.splitext(filename)[1]:
                ext = (
                    ".pdf"
                    if "pdf" in content_type
                    else (
                        ".xlsx"
                        if "excel" in content_type
                        or "spreadsheet" in content_type
                        else (".csv" if "csv" in content_type else ".docx")
                    )
                )
                filename = f"downloaded_file{ext}"

            subfolder = get_target_subfolder(filename, content_type)
            target_dir = os.path.join(session_folder_path, subfolder)
            os.makedirs(target_dir, exist_ok=True)

            with open(os.path.join(target_dir, filename), "wb") as f:
                for chunk in response.iter_content(chunk_size=16384):
                    f.write(chunk)
            print(
                f"   ✓ Extracted direct stream: {filename} -> Saved to"
                f" '{subfolder}/'"
            )

        # --- SCENARIO B: HTML Page Parsing (Static + Dynamic Fallback) ---
        else:
            raw_html = response.text
            soup = BeautifulSoup(raw_html, "html.parser")

            # Check if static request yields meaningful content or just an empty React/SPA shell
            readable_elements = soup.find_all(
                ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"]
            )
            has_content = any(
                tag.get_text(strip=True) for tag in readable_elements
            )

            if not has_content:
                print(
                    "   ⚡ Static GET returned empty/JS shell. Switching to"
                    " Playwright browser rendering..."
                )
                rendered_html = fetch_dynamic_html_with_playwright(target_url)
                if rendered_html:
                    raw_html = rendered_html
                    soup = BeautifulSoup(raw_html, "html.parser")

            # Clean clutter & non-content tags
            for element in soup(
                [
                    "script",
                    "style",
                    "nav",
                    "footer",
                    "header",
                    "aside",
                    "form",
                    "iframe",
                    "noscript",
                ]
            ):
                element.decompose()

            # Prevent "Continue reading" navigational noise
            nav_noise_patterns = re.compile(
                r"^(continue reading|read more|click here|learn more|view"
                r" more|go to.*|back to top)$",
                re.IGNORECASE,
            )
            for noisy_element in soup.find_all(["a", "button", "span", "p"]):
                if nav_noise_patterns.match(noisy_element.get_text(strip=True)):
                    noisy_element.decompose()

            structured_blocks = []
            tags_to_extract = [
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "p",
                "ul",
                "ol",
                "table",
            ]

            for element in soup.find_all(tags_to_extract):
                if element.find_parent(["table", "ul", "ol"]):
                    continue

                if element.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                    text = element.get_text(strip=True)
                    if text:
                        structured_blocks.append(f"\n{text}\n")
                elif element.name == "p":
                    text = element.get_text(strip=True)
                    if text:
                        structured_blocks.append(text)
                elif element.name in ["ul", "ol"]:
                    list_items = [
                        f"- {li.get_text(strip=True)}"
                        for li in element.find_all("li", recursive=False)
                        if li.get_text(strip=True)
                    ]
                    if list_items:
                        structured_blocks.append(
                            "\n" + "\n".join(list_items) + "\n"
                        )
                elif element.name == "table":
                    headers_list = []
                    columns_data = {}
                    thead = element.find("thead")
                    header_source = thead if thead else element
                    first_tr = header_source.find("tr")
                    if first_tr:
                        for th in first_tr.find_all(["th", "td"]):
                            header_text = th.get_text(strip=True)
                            headers_list.append(header_text)
                            columns_data[header_text] = []
                    tbody = element.find("tbody")
                    row_source = tbody if tbody else element
                    all_trs = row_source.find_all("tr")
                    start_idx = 1 if not thead and first_tr in all_trs else 0
                    for tr in all_trs[start_idx:]:
                        for idx, td in enumerate(tr.find_all(["td", "th"])):
                            if idx < len(headers_list):
                                cell_strings = [s for s in td.stripped_strings]
                                cell_text = (
                                    " ".join(cell_strings)
                                    if cell_strings
                                    else "-"
                                )
                                columns_data[headers_list[idx]].append(
                                    cell_text
                                )
                    table_output = [
                        f"{header}: {', '.join(columns_data[header])}"
                        for header in headers_list
                        if header
                    ]
                    if table_output:
                        structured_blocks.append(
                            "\n" + "\n".join(table_output) + "\n"
                        )

            # Save extracted text payload to documents/content.txt
            final_page_text = "\n".join(structured_blocks).strip()
            doc_subfolder = get_target_subfolder("content.txt", "text/plain")
            docs_dir = os.path.join(session_folder_path, doc_subfolder)
            os.makedirs(docs_dir, exist_ok=True)

            text_filepath = os.path.join(docs_dir, "content.txt")
            with open(text_filepath, "w", encoding="utf-8") as text_file:
                text_file.write(
                    f"Source URL: {target_url}\n"
                    + "=" * 80
                    + "\n\n"
                    + final_page_text
                )
            print(
                f"   ✓ Extracted page content text and saved to"
                f" '{doc_subfolder}/content.txt'"
            )

            # Linked Document/Spreadsheet Discovery
            anchors = soup.find_all("a", href=True)
            doc_extensions = re.compile(
                r"\.(pdf|xlsx|xls|csv|docx|doc|txt|rtf)(\?|$)",
                re.IGNORECASE,
            )
            valid_links = set()

            for link in anchors:
                href = link["href"]
                if doc_extensions.search(href):
                    if not any(
                        term in href.lower()
                        for term in [
                            "login",
                            "logout",
                            "javascript:",
                            "facebook",
                            "twitter",
                        ]
                    ):
                        valid_links.add(urljoin(target_url, href))

            for asset_route in valid_links:
                try:
                    with requests.get(
                        asset_route,
                        headers=headers,
                        stream=True,
                        timeout=15,
                        verify=False,
                    ) as sub_res:
                        sub_type = sub_res.headers.get(
                            "Content-Type", ""
                        ).lower()
                        if "text/html" in sub_type or not sub_res.ok:
                            continue

                        sub_disp = sub_res.headers.get(
                            "Content-Disposition", ""
                        )
                        if "filename=" in sub_disp:
                            sub_filename = re.findall(
                                r'filename=["\']?(.*?)["\']?$', sub_disp
                            )[0]
                        else:
                            sub_filename = os.path.basename(
                                urlparse(asset_route).path
                            )

                        subfolder = get_target_subfolder(
                            sub_filename, sub_type
                        )
                        target_dir = os.path.join(
                            session_folder_path, subfolder
                        )
                        os.makedirs(target_dir, exist_ok=True)

                        with open(
                            os.path.join(target_dir, sub_filename), "wb"
                        ) as f_sub:
                            for chunk in sub_res.iter_content(chunk_size=16384):
                                f_sub.write(chunk)
                        print(
                            f"      ✓ Saved asset: {sub_filename} ->"
                            f" '{subfolder}/'"
                        )
                except Exception:
                    pass

            # Image Extraction & Filtering
            images = soup.find_all("img", src=True)
            if images:
                image_folder = os.path.join(session_folder_path, "images")
                img_counter = 1
                MIN_WIDTH, MIN_HEIGHT = 100, 80
                noise_keywords = re.compile(
                    r"(logo|avatar|favicon|loader|spinner|banner-ad|arrow|button|sprite|share-on|social-media)",
                    re.IGNORECASE,
                )

                for img in images:
                    img_url = urljoin(target_url, img["src"])
                    if img_url.startswith("data:image"):
                        continue

                    parsed_img_path = urlparse(img_url).path
                    img_name_raw = os.path.basename(parsed_img_path)
                    if noise_keywords.search(img_name_raw):
                        continue

                    try:
                        with requests.get(
                            img_url,
                            headers=headers,
                            stream=True,
                            timeout=10,
                            verify=False,
                        ) as img_res:
                            if img_res.ok:
                                img_data = img_res.content
                                with Image.open(BytesIO(img_data)) as loaded_img:
                                    width, height = loaded_img.size
                                    if (
                                        width < MIN_WIDTH
                                        or height < MIN_HEIGHT
                                    ):
                                        continue

                                    img_name = img_name_raw
                                    if not img_name or "." not in img_name:
                                        ext = (
                                            f".{loaded_img.format.lower()}"
                                            if loaded_img.format
                                            else ".jpg"
                                        )
                                        img_name = f"image_{img_counter}{ext}"
                                        img_counter += 1

                                    os.makedirs(image_folder, exist_ok=True)
                                    with open(
                                        os.path.join(image_folder, img_name),
                                        "wb",
                                    ) as img_f:
                                        img_f.write(img_data)
                                    print(
                                        f"      ✓ Image Saved: {img_name} ->"
                                        f" 'images/' ({width}x{height})"
                                    )
                    except Exception:
                        pass

    except Exception as e:
        print(f"   ✗ Connection Interrupted: {e}")


def run_scrapper(url: str) -> str:
    """Pipeline execution wrapper.

    Takes a URL, sanitizes it, runs the dynamic extractor, and returns the
    absolute path where data was written.
    """
    print(f"🚀 Initializing scraper for: {url}")

    clean_url, target_path = sanitize_and_generate_dynamic_folder(url)

    if not clean_url or not target_path:
        print(" ✗ Error: Invalid URL input provided.")
        return ""

    dynamic_session_extractor(clean_url, target_path)

    print(f" Task finished! Data directory: {os.path.abspath(target_path)}")
    return target_path