"""
pullama.search — Search the Ollama model library
Parses https://ollama.com/search?q=<query> HTML (server-rendered, no JSON API available).
"""

import sys
import html.parser
import urllib.request
import urllib.error
import urllib.parse


# ─── Colors (duplicated from __main__ to avoid circular import) ───────────────

class Colors:
    HEADER  = '\033[95m'
    OKBLUE  = '\033[94m'
    OKCYAN  = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL    = '\033[91m'
    ENDC    = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'

def _print_warning(msg):
    print(f"{Colors.WARNING}⚠ {msg}{Colors.ENDC}")

def _print_error(msg):
    print(f"{Colors.FAIL}✖ {msg}{Colors.ENDC}", file=sys.stderr)


# ─── HTML Parser ──────────────────────────────────────────────────────────────

class OllamaSearchParser(html.parser.HTMLParser):
    """
    State-machine parser for ollama.com/search HTML.
    Extracts model entries identified by <li x-test-model> elements.

    States:
      root          — between model entries
      in_li         — inside a model <li>
      in_title      — inside <span x-test-search-response-title>
      in_desc       — inside the first <p> after <h2> (description)
      in_size       — inside <span x-test-size>
      in_capability — inside <span x-test-capability>
      in_pull_count — inside <span x-test-pull-count>
      in_tag_count  — inside <span x-test-tag-count>
      in_updated    — inside <span x-test-updated>
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._current = None
        self._state = "root"
        self._li_depth = 0
        self._desc_seen_h2 = False
        self._desc_consumed = False

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)

        # Detect model entry start — boolean attr, value is None
        if tag == "li" and "x-test-model" in attr_dict:
            self._current = {
                "name": "", "description": "", "url": "",
                "sizes": [], "capabilities": [],
                "pulls": "", "tags": "", "updated": "",
            }
            self._state = "in_li"
            self._li_depth = 1
            self._desc_seen_h2 = False
            self._desc_consumed = False
            return

        if self._state == "root":
            return

        # Track nested <li> depth to detect the correct closing </li>
        if tag == "li":
            self._li_depth += 1
            return

        if tag == "a" and "href" in attr_dict:
            href = attr_dict["href"]
            if href.startswith("/library/"):
                self._current["url"] = href
            return

        if tag == "h2":
            self._desc_seen_h2 = True
            return

        # First <p> after <h2> is the description
        if (tag == "p" and self._desc_seen_h2
                and not self._desc_consumed and self._state == "in_li"):
            self._state = "in_desc"
            return

        if tag == "span":
            if "x-test-search-response-title" in attr_dict:
                self._state = "in_title"
            elif "x-test-size" in attr_dict:
                self._state = "in_size"
            elif "x-test-capability" in attr_dict:
                self._state = "in_capability"
            elif "x-test-pull-count" in attr_dict:
                self._state = "in_pull_count"
            elif "x-test-tag-count" in attr_dict:
                self._state = "in_tag_count"
            elif "x-test-updated" in attr_dict:
                self._state = "in_updated"

    def handle_endtag(self, tag):
        if self._state == "root":
            return

        if tag == "li":
            self._li_depth -= 1
            if self._li_depth == 0:
                if self._current and self._current["name"]:
                    self.results.append(self._current)
                self._current = None
                self._state = "root"
            return

        if tag == "p" and self._state == "in_desc":
            self._desc_consumed = True
            self._state = "in_li"
            return

        if tag == "span" and self._state not in ("root", "in_li"):
            self._state = "in_li"

    def handle_data(self, data):
        # handle_data can fire multiple times per text node — use += for strings
        if self._state == "root" or self._current is None:
            return
        text = data.strip()
        if not text:
            return
        if self._state == "in_title":
            self._current["name"] += text
        elif self._state == "in_desc":
            self._current["description"] += text
        elif self._state == "in_size":
            self._current["sizes"].append(text)
        elif self._state == "in_capability":
            self._current["capabilities"].append(text)
        elif self._state == "in_pull_count":
            self._current["pulls"] = text
        elif self._state == "in_tag_count":
            self._current["tags"] = text
        elif self._state == "in_updated":
            self._current["updated"] = text


# ─── Output ───────────────────────────────────────────────────────────────────

def _print_search_results(results):
    col_name = 28

    print(f"  {Colors.BOLD}{'Model':<{col_name}}  {'Pulls':<10}  {'Tags':<6}  Updated{Colors.ENDC}")
    print("  " + "─" * 68)

    for r in results:
        name    = r["name"] or "?"
        pulls   = r["pulls"] or "—"
        tags    = r["tags"] or "—"
        updated = r["updated"] or "—"

        # Header line: name, pulls, tags, updated
        print(
            f"  {Colors.BOLD}{Colors.OKCYAN}{name:<{col_name}}{Colors.ENDC}"
            f"  {Colors.OKGREEN}{pulls:<10}{Colors.ENDC}"
            f"  {tags:<6}"
            f"  {Colors.DIM}{updated}{Colors.ENDC}"
        )

        # Description (truncated to 90 chars)
        if r["description"]:
            desc = r["description"][:90]
            if len(r["description"]) > 90:
                desc += "…"
            print(f"  {Colors.DIM}{desc}{Colors.ENDC}")

        # Sizes + capabilities
        sizes_str = "  ".join(r["sizes"]) if r["sizes"] else "—"
        caps_str  = "  ".join(r["capabilities"]) if r["capabilities"] else "—"
        print(
            f"  {Colors.DIM}Sizes:{Colors.ENDC} {Colors.WARNING}{sizes_str}{Colors.ENDC}"
            f"    {Colors.DIM}Capabilities:{Colors.ENDC} {caps_str}"
        )

        # Pull hint — use smallest size as tag if available
        model_ref = name.split("/")[-1] if "/" in name else name
        tag = f":{r['sizes'][0]}" if r["sizes"] else ""
        print(f"  {Colors.DIM}→ pullama pull {model_ref}{tag}{Colors.ENDC}")
        print()


# ─── Command ──────────────────────────────────────────────────────────────────

def cmd_search(args):
    query = args.query.strip()
    if not query:
        _print_error("Search query cannot be empty.")
        sys.exit(1)

    limit = getattr(args, "limit", 10)
    filter_caps = getattr(args, "capabilities", None)

    encoded = urllib.parse.quote_plus(query)
    url = f"https://ollama.com/search?q={encoded}"

    print(f"\n{Colors.BOLD}Searching Ollama library for "
          f"{Colors.OKCYAN}\"{query}\"{Colors.ENDC}{Colors.BOLD}...{Colors.ENDC}\n")

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; pullama-cli)",
                "Accept": "text/html",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        _print_error(f"HTTP {e.code} fetching search results.")
        sys.exit(1)
    except urllib.error.URLError as e:
        _print_error(f"Connection error: {e.reason}")
        sys.exit(1)

    parser = OllamaSearchParser()
    try:
        parser.feed(html_content)
    except Exception:
        _print_warning("Could not parse search results — ollama.com may have changed its layout.")
        print(f"  Browse manually: {Colors.OKCYAN}https://ollama.com/search?q={encoded}{Colors.ENDC}\n")
        return

    results = parser.results

    # Filter by capabilities if requested
    if filter_caps:
        wanted = {c.lower() for c in filter_caps}
        results = [r for r in results if wanted.issubset({c.lower() for c in r["capabilities"]})]

    # Truncate
    results = results[:limit]

    if not results:
        _print_warning(f"No models found for \"{query}\".")
        print(f"  Try a broader term or browse: "
              f"{Colors.OKCYAN}https://ollama.com/search?q={encoded}{Colors.ENDC}\n")
        return

    _print_search_results(results)
    print(f"  {Colors.DIM}Showing {len(results)} result(s).  "
          f"Use --limit N for more or --capabilities to filter.{Colors.ENDC}\n")
