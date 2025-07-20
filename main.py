import hashlib
import json
import os
import re
import sys
import time
from bs4 import BeautifulSoup
import requests


def parse_truncated_json(json_str: str) -> dict:
    """
    Parse a potentially truncated JSON object by extracting complete key-value pairs
    and ignoring any truncated values at the end.
    """
    # Remove leading/trailing whitespace and braces
    content = json_str.strip()
    if content.startswith("{"):
        content = content[1:]
    if content.endswith("}"):
        content = content[:-1]

    # Pattern to match complete key-value pairs
    # This handles strings, numbers, booleans, nulls, arrays, and objects
    pattern = r'"([^"\\]|\\.)*"\s*:\s*(?:"(?:[^"\\]|\\.)*"|[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|\[(?:[^\[\]"]|"(?:[^"\\]|\\.)*")*\]|\{(?:[^{}"]|"(?:[^"\\]|\\.)*")*\})'

    matches = re.findall(pattern, content)
    result = {}

    # Extract complete pairs from the content
    pairs = []
    pos = 0
    brace_count = 0
    bracket_count = 0
    in_string = False
    escape_next = False
    current_pair = ""

    for char in content:
        if escape_next:
            escape_next = False
            current_pair += char
            continue

        if char == "\\" and in_string:
            escape_next = True
            current_pair += char
            continue

        if char == '"' and not escape_next:
            in_string = not in_string

        if not in_string:
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
            elif char == "[":
                bracket_count += 1
            elif char == "]":
                bracket_count -= 1
            elif char == "," and brace_count == 0 and bracket_count == 0:
                # Found a complete pair
                pair = current_pair.strip()
                if ":" in pair:
                    pairs.append(pair)
                current_pair = ""
                continue

        current_pair += char

    # Add the last pair if it's complete
    if (
        current_pair.strip()
        and ":" in current_pair
        and brace_count == 0
        and bracket_count == 0
    ):
        pairs.append(current_pair.strip())

    # Parse each complete pair
    for pair in pairs:
        try:
            # Wrap in braces to make it valid JSON
            test_json = "{" + pair + "}"
            parsed = json.loads(test_json)
            result.update(parsed)
        except json.JSONDecodeError:
            # Skip invalid pairs
            continue

    return result


def fetch_ssr_props(url: str) -> dict | None:
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()

    not_found_cache_file = f"out/cache/not_found_{cache_key}"
    if os.path.exists(not_found_cache_file):
        return None

    cache_file = f"out/cache/ssr_props_{cache_key}.json"
    if os.path.exists(cache_file):
        print(f"Using cached SSR props from {cache_file}")
        with open(cache_file, "r") as f:
            return json.load(f)

    print(f"Fetching SSR props from {url}")

    time.sleep(1)
    resp = requests.get(url)
    if resp.status_code != 200:
        print(f"Failed to fetch {url}: status {resp.status_code}")
        if resp.status_code == 404:
            with open(not_found_cache_file, "w") as f:
                f.write("")
        return None

    with open("out/processing.html", "w") as f:
        f.write(resp.text)

    html_content = resp.text

    tag_needle = 'id="ssr-props"'
    attr_needle = 'data-initial-props="'
    tag_start = html_content.find(tag_needle)
    assert tag_start != -1, f"{tag_needle} not found"
    attr_start = html_content.find(attr_needle, tag_start + len(tag_needle))
    assert attr_start != -1, f"{attr_needle} not found"
    if html_content.find('"', attr_start + len(attr_needle)) == -1:
        print("WARN: Attempting to fix truncated response")
        is_truncated = True
        html_content += '"></script>'
    else:
        is_truncated = False

    soup = BeautifulSoup(html_content, "html.parser")
    script_tag = soup.find("script", id="ssr-props")
    assert script_tag is not None, "No script tag with id 'ssr-props' found"
    data_json = script_tag.get("data-initial-props")
    assert data_json is not None, "attr data-initial-props missing"

    if is_truncated:
        data = parse_truncated_json(data_json)
        assert "oasDefinition" in data, "truncated JSON missing oasDefinition"
    else:
        data = json.loads(data_json)
    assert isinstance(data, dict), "data-initial-props is not a JSON object"

    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)

    os.remove("out/processing.html")

    return data


def merge_oas_definitions(base: dict, new: dict) -> dict:
    merged = base.copy()

    if "paths" in new:
        for path, methods in new["paths"].items():
            if path not in merged["paths"]:
                merged["paths"][path] = methods
            else:
                merged["paths"][path].update(methods)

    if "components" in new:
        for section in new["components"]:
            if section not in merged["components"]:
                merged["components"][section] = new["components"][section]
            else:
                for name, item in new["components"][section].items():
                    if name not in merged["components"][section]:
                        merged["components"][section][name] = item
                    else:
                        merged["components"][section][name].update(item)

    return merged


def main(start_url: str):
    os.makedirs("out/cache", exist_ok=True)

    print("Processing start url")
    start_props = fetch_ssr_props(start_url)
    refs = start_props["sidebars"]["refs"]

    non_reference_pages = set()
    for doc in start_props["sidebars"]["docs"]:
        for page in doc["pages"]:
            if not page["isReference"]:
                non_reference_pages.add(page["slug"])

    url_prefix = start_url
    if not url_prefix.endswith("/"):
        url_prefix += "/"

    oas_definition = start_props["oasDefinition"].copy()
    oas_definition["paths"] = {}
    oas_definition["components"] = {}

    for ref in refs:
        for page in ref["pages"]:
            if page["slug"] in non_reference_pages:
                print(f"Skipping non-reference page {page['title']} ({page['slug']})")
                continue

            print(f"Processing {page['title']} ({page['slug']})")
            props = fetch_ssr_props(url_prefix + page["slug"])
            if props is None:
                continue
            if "oasDefinition" not in props or not props["oasDefinition"]:
                continue
            oas_definition = merge_oas_definitions(
                oas_definition, props["oasDefinition"]
            )

    for k in ["x-readme-fauxas", "_id"]:
        if k in oas_definition:
            del oas_definition[k]

    with open("out/oas_definition.json", "w") as f:
        json.dump(oas_definition, f, indent=2)


if __name__ == "__main__":
    assert len(sys.argv) == 2, "Usage: main.py <start_url>"
    main(sys.argv[1])
