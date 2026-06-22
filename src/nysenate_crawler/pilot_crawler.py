from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urldefrag, urljoin, urlparse
from xml.sax.saxutils import escape


LEVEL_KEYS = [f"level{level}" for level in range(10, 101, 10)]
LOGGER_NAME = "nysenate_crawler"


class MissingDependencyError(RuntimeError):
    """Raised when an optional runtime dependency is required for a live run."""


class HumanVerificationRequired(RuntimeError):
    """Raised when the live site requires manual verification."""


class LevelOverflowError(RuntimeError):
    """Raised when a crawl path is deeper than the configured XML schema."""


class XMLDocumentStoreError(RuntimeError):
    """Raised when an existing XML checkpoint cannot be loaded safely."""


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    label: str = ""
    description: str = ""


@dataclass
class CrawlerConfig:
    root_url: str
    pilot_title_limit: int | None = 1
    max_pages: int = 250
    max_documents: int = 50
    output_xml: Path = Path("data/nysenate_consolidated_laws_pilot.xml")
    log_file: Path = Path("logs/nysenate_crawler.log")
    visited_file: Path = Path("data/nysenate_visited_urls.txt")
    failed_file: Path = Path("data/nysenate_failed_urls.txt")
    debug_dir: Path = Path("logs/nysenate_debug")
    headless: bool = False
    channel: str | None = "chrome"
    viewport_width: int = 1920
    viewport_height: int = 1080
    locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"
    timezone_id: str = "America/New_York"
    user_data_dir: Path = Path("data/nysenate_browser_profile")
    user_agents: list[str] = field(default_factory=list)
    browser_args: list[str] = field(default_factory=list)
    page_load_timeout_ms: int = 45000
    selector_timeout_ms: int = 8000
    navigation_delay_ms: int = 1000
    content_stable_delay_ms: int = 1200
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    selectors: dict[str, Any] = field(default_factory=dict)
    save_html_on_failure: bool = True
    save_screenshot_on_failure: bool = True
    save_debug_on_success: bool = False
    min_contents_chars: int = 20
    require_source_url: bool = True
    require_at_least_one_level: bool = True
    reject_navigation_pages: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CrawlerConfig":
        browser = dict(raw.get("browser") or {})
        targets = dict(raw.get("targets") or {})
        files = dict(raw.get("files") or {})
        timing = dict(raw.get("timing") or {})
        debug = dict(raw.get("debug") or {})
        validation = dict(raw.get("validation") or {})
        selectors = dict(raw.get("selectors") or {})

        return cls(
            root_url=str(targets.get("root_url", "https://www.nysenate.gov/legislation/laws/CONSOLIDATED")),
            pilot_title_limit=as_optional_int(targets.get("pilot_title_limit", 1)),
            max_pages=int(targets.get("max_pages", 250)),
            max_documents=int(targets.get("max_documents", 50)),
            output_xml=Path(files.get("output_xml", "data/nysenate_consolidated_laws_pilot.xml")),
            log_file=Path(files.get("log_file", "logs/nysenate_crawler.log")),
            visited_file=Path(files.get("visited_file", "data/nysenate_visited_urls.txt")),
            failed_file=Path(files.get("failed_file", "data/nysenate_failed_urls.txt")),
            debug_dir=Path(files.get("debug_dir", "logs/nysenate_debug")),
            headless=as_bool(browser.get("headless", False)),
            channel=none_if_blank(browser.get("channel", "chrome")),
            viewport_width=int(browser.get("viewport_width", 1920)),
            viewport_height=int(browser.get("viewport_height", 1080)),
            locale=str(browser.get("locale", "en-US")),
            accept_language=str(browser.get("accept_language", browser.get("Accept-Language", "en-US,en;q=0.9"))),
            timezone_id=str(browser.get("timezone", "America/New_York")),
            user_data_dir=Path(browser.get("user_data_dir", "data/nysenate_browser_profile")),
            user_agents=list(browser.get("user_agents") or []),
            browser_args=list(browser.get("args") or []),
            page_load_timeout_ms=int(timing.get("page_load_timeout_ms", 45000)),
            selector_timeout_ms=int(timing.get("selector_timeout_ms", 8000)),
            navigation_delay_ms=int(timing.get("navigation_delay_ms", 1000)),
            content_stable_delay_ms=int(timing.get("content_stable_delay_ms", 1200)),
            max_retries=int(timing.get("max_retries", 3)),
            retry_backoff_seconds=float(timing.get("retry_backoff_seconds", 2)),
            selectors=selectors,
            save_html_on_failure=as_bool(debug.get("save_html_on_failure", True)),
            save_screenshot_on_failure=as_bool(debug.get("save_screenshot_on_failure", True)),
            save_debug_on_success=as_bool(debug.get("save_debug_on_success", False)),
            min_contents_chars=int(validation.get("min_contents_chars", 20)),
            require_source_url=as_bool(validation.get("require_source_url", True)),
            require_at_least_one_level=as_bool(validation.get("require_at_least_one_level", True)),
            reject_navigation_pages=as_bool(validation.get("reject_navigation_pages", True)),
        )

    def with_overrides(
        self,
        *,
        headless: bool | None = None,
        max_pages: int | None = None,
        max_documents: int | None = None,
    ) -> "CrawlerConfig":
        values = dict(self.__dict__)
        if headless is not None:
            values["headless"] = headless
        if max_pages is not None:
            values["max_pages"] = max_pages
        if max_documents is not None:
            values["max_documents"] = max_documents
        return CrawlerConfig(**values)

    def selector(self, key: str, default: str) -> str:
        value = self.selectors.get(key, default)
        if isinstance(value, list):
            return str(value[0]) if value else default
        return str(value or default)

    def selector_list(self, key: str) -> list[str]:
        value = self.selectors.get(key, [])
        if isinstance(value, list):
            return [str(item) for item in value if item]
        if value:
            return [str(value)]
        return []


class XMLDocumentStore:
    """Small incremental XML store for resumable crawl runs."""

    def __init__(self, path: Path, root_url: str) -> None:
        self.path = path
        self.root_url = root_url
        self.records: list[dict[str, str]] = []
        self.source_urls: set[str] = set()
        self.extraction_date = datetime.now(timezone.utc).isoformat()
        self.pilot_law = ""

    @property
    def document_count(self) -> int:
        return len(self.records)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._load_existing()
            self._write()
            return
        self._write()

    def _load_existing(self) -> None:
        try:
            tree = ET.parse(self.path)
        except ET.ParseError as exc:
            raise XMLDocumentStoreError(f"Existing XML is malformed and will not be overwritten: {self.path}") from exc

        root = tree.getroot()
        if root.tag != "nysenateDocuments":
            raise XMLDocumentStoreError(f"Existing XML has unexpected root element {root.tag!r}: {self.path}")

        self.extraction_date = root.findtext("./metadata/extractionDate") or self.extraction_date
        self.pilot_law = root.findtext("./metadata/pilotLaw") or self.pilot_law
        self.records = []
        self.source_urls = set()

        for element in root.findall("./documents/document"):
            record = {
                "sourceURL": clean_text(element.findtext("sourceURL") or ""),
                "revisionDate": clean_text(element.findtext("revisionDate") or ""),
                "contents": clean_text(element.findtext("contents") or ""),
            }
            for key in LEVEL_KEYS:
                record[key] = clean_text(element.findtext(key) or "")
            self.records.append(record)
            source_url = source_url_key(record.get("sourceURL", ""))
            if source_url:
                self.source_urls.add(source_url)

    def set_crawl_scope(self, value: str) -> None:
        self.pilot_law = clean_text(value)
        self._write()

    def set_pilot_law(self, value: str) -> None:
        self.set_crawl_scope(value)

    def has_source_url(self, url: str) -> bool:
        return source_url_key(url) in self.source_urls

    def append(self, record: Mapping[str, str]) -> bool:
        prepared = {str(key): str(value or "") for key, value in record.items()}
        source_url = source_url_key(prepared.get("sourceURL", ""))
        if source_url and source_url in self.source_urls:
            return False
        self.records.append(prepared)
        if source_url:
            self.source_urls.add(source_url)
        self._write()
        return True

    def _write(self) -> None:
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<nysenateDocuments>",
            "  <metadata>",
            "    <source>New York Senate Consolidated Laws</source>",
            f"    <sourceURL>{xml_escape(self.root_url)}</sourceURL>",
            f"    <extractionDate>{xml_escape(self.extraction_date)}</extractionDate>",
            f"    <pilotLaw>{xml_escape(self.pilot_law)}</pilotLaw>",
            f"    <totalDocuments>{len(self.records)}</totalDocuments>",
            "  </metadata>",
            "  <documents>",
        ]
        for record in self.records:
            lines.extend(render_xml_document(record))
        lines.extend(["  </documents>", "</nysenateDocuments>", ""])
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text("\n".join(lines), encoding="utf-8")
        temp_path.replace(self.path)


class NYSenatePilotCrawler:
    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self.logger = logging.getLogger(LOGGER_NAME)
        self.visited_urls: set[str] = set()
        self.failed_urls: set[str] = set()
        self.pages_seen = 0
        self.store = XMLDocumentStore(config.output_xml, config.root_url)

    async def run(self) -> int:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise MissingDependencyError("Playwright is required for live crawling. Run: python -m pip install -r requirements.txt") from exc

        self.prepare_runtime_paths()
        try:
            self.load_checkpoint()
            self.store.initialize()

            async with async_playwright() as playwright:
                context = await self.open_context(playwright)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    root_links = await self.discover_root_title_links(page)
                    if not root_links:
                        await self.save_debug(page, "root-no-title-links")
                        raise RuntimeError("No root title links were discovered.")

                    title_links = limit_root_title_links(root_links, self.config.pilot_title_limit)
                    self.store.set_crawl_scope(crawl_scope_label(title_links, self.config.pilot_title_limit))
                    self.logger.info(
                        "Starting crawl for %s of %s root title link(s).",
                        len(title_links),
                        len(root_links),
                    )

                    for title_link in title_links:
                        if self.safety_limit_reached():
                            self.logger.info("Stopping crawl before next title because a safety limit was reached.")
                            break
                        self.logger.info("Selected crawl title: %s (%s)", title_link.label or title_link.url, title_link.url)
                        await self.crawl_page(page, title_link.url, {}, depth=0)
                finally:
                    await context.close()
        except KeyboardInterrupt:
            self.logger.warning("Crawl interrupted by user; XML and checkpoints have been preserved.")
            raise
        finally:
            self.flush_checkpoint()

        self.logger.info("Crawl complete: %s document(s), %s page(s), %s failure(s)", self.store.document_count, self.pages_seen, len(self.failed_urls))
        return self.store.document_count

    def safety_limit_reached(self) -> bool:
        return self.pages_seen >= self.config.max_pages or self.store.document_count >= self.config.max_documents

    def prepare_runtime_paths(self) -> None:
        for path in [
            self.config.output_xml.parent,
            self.config.log_file.parent,
            self.config.visited_file.parent,
            self.config.failed_file.parent,
            self.config.debug_dir,
            self.config.user_data_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def load_checkpoint(self) -> None:
        if self.config.visited_file.exists():
            self.visited_urls.update(line.strip() for line in self.config.visited_file.read_text(encoding="utf-8").splitlines() if line.strip())
        if self.config.failed_file.exists():
            self.failed_urls.update(line.strip() for line in self.config.failed_file.read_text(encoding="utf-8").splitlines() if line.strip())

    def flush_checkpoint(self) -> None:
        write_text_atomic(self.config.visited_file, "\n".join(sorted(self.visited_urls)) + ("\n" if self.visited_urls else ""))
        write_text_atomic(self.config.failed_file, "\n".join(sorted(self.failed_urls)) + ("\n" if self.failed_urls else ""))

    async def open_context(self, playwright: Any) -> Any:
        kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "viewport": {"width": self.config.viewport_width, "height": self.config.viewport_height},
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
            "extra_http_headers": {"Accept-Language": self.config.accept_language},
            "args": self.config.browser_args,
        }
        if self.config.user_agents:
            kwargs["user_agent"] = random.choice(self.config.user_agents)
        if self.config.channel:
            kwargs["channel"] = self.config.channel

        try:
            return await playwright.chromium.launch_persistent_context(str(self.config.user_data_dir), **kwargs)
        except Exception:
            if "channel" not in kwargs:
                raise
            channel = kwargs.pop("channel")
            self.logger.warning("Could not launch Chromium channel %s; retrying default Playwright Chromium.", channel)
            return await playwright.chromium.launch_persistent_context(str(self.config.user_data_dir), **kwargs)

    async def discover_root_title_links(self, page: Any) -> list[LinkCandidate]:
        await self.goto_with_retry(page, self.config.root_url)
        await self.dismiss_obstacles(page)
        await page.wait_for_timeout(self.config.content_stable_delay_ms)
        links = await self.extract_child_links(page)
        self.logger.info("Root discovery found %s candidate title link(s).", len(links))
        return links

    async def crawl_page(self, page: Any, url: str, carried_levels: Mapping[str, str], depth: int) -> None:
        normalized_url = canonical_url(url)
        if self.store.has_source_url(normalized_url):
            self.logger.info("Skipping already emitted document URL: %s", normalized_url)
            self.visited_urls.add(normalized_url)
            self.flush_checkpoint()
            return
        if normalized_url in self.failed_urls:
            self.logger.info("Skipping previously failed URL: %s", normalized_url)
            return
        if self.pages_seen >= self.config.max_pages:
            self.logger.warning("Stopping at max_pages=%s before %s", self.config.max_pages, normalized_url)
            return
        if self.store.document_count >= self.config.max_documents:
            self.logger.info("Stopping at max_documents=%s", self.config.max_documents)
            return

        if normalized_url in self.visited_urls:
            self.logger.info("Revisiting checkpointed URL to continue child discovery: %s", normalized_url)

        self.pages_seen += 1
        try:
            await self.goto_with_retry(page, normalized_url)
        except HumanVerificationRequired:
            raise
        except Exception as exc:
            self.logger.error("Could not crawl %s: %s", normalized_url, exc)
            self.failed_urls.add(normalized_url)
            self.flush_checkpoint()
            return
        await self.dismiss_obstacles(page)
        await page.wait_for_timeout(self.config.content_stable_delay_ms)

        title = await self.extract_current_title(page)
        try:
            levels = assign_level(carried_levels, depth, title)
        except LevelOverflowError:
            self.logger.error("Path exceeded level100 at %s; flagging for schema review.", normalized_url)
            self.failed_urls.add(normalized_url)
            self.flush_checkpoint()
            return

        child_links = await self.extract_child_links(page)
        legal_text = await self.extract_legal_text(page)
        revision = await self.extract_revision_metadata(page)
        page_type = classify_page(child_links, legal_text)

        self.logger.info(
            "Visited %s | depth=%s | captured=%s | title=%s | child_count=%s | decision=%s",
            normalized_url,
            depth,
            LEVEL_KEYS[depth] if depth < len(LEVEL_KEYS) else "overflow",
            title,
            len(child_links),
            "skip-navigation" if page_type == "navigation" else page_type,
        )

        if page_type == "navigation":
            completed_children = True
            for child in child_links:
                if self.safety_limit_reached():
                    completed_children = False
                    break
                await self.crawl_page(page, child.url, levels, depth + 1)
                if self.safety_limit_reached():
                    completed_children = False
                    break
            if completed_children:
                self.visited_urls.add(normalized_url)
            self.flush_checkpoint()
            return

        if page_type == "leaf":
            record = build_record(normalized_url, levels, revision, legal_text)
            errors = validate_record(record, has_children=False, config=self.config)
            if errors:
                self.logger.warning("Validation failed for %s: %s", normalized_url, "; ".join(errors))
                self.failed_urls.add(normalized_url)
                await self.save_debug(page, "validation-failed")
            else:
                added = self.store.append(record)
                if added:
                    self.logger.info("Emitted XML document %s from %s", self.store.document_count, normalized_url)
                else:
                    self.logger.info("Skipping duplicate XML document from %s", normalized_url)
                self.visited_urls.add(normalized_url)
            self.flush_checkpoint()
            return

        self.logger.warning("Empty or malformed page at %s; no child links and no legal text.", normalized_url)
        self.failed_urls.add(normalized_url)
        await self.save_debug(page, "empty-page")
        self.flush_checkpoint()

    async def goto_with_retry(self, page: Any, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_load_timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=self.config.selector_timeout_ms)
                except Exception:
                    pass
                await self.assert_no_human_verification(page, url)
                status = getattr(response, "status", None)
                if status and status >= 400:
                    raise RuntimeError(f"HTTP status {status}")
                await page.wait_for_timeout(self.config.navigation_delay_ms)
                return
            except HumanVerificationRequired:
                raise
            except Exception as exc:
                last_error = exc
                wait = self.config.retry_backoff_seconds * attempt
                self.logger.warning("Navigation attempt %s/%s failed for %s: %s", attempt, self.config.max_retries, url, exc)
                if attempt < self.config.max_retries:
                    await page.wait_for_timeout(int(wait * 1000))
        self.failed_urls.add(canonical_url(url))
        raise RuntimeError(f"Could not navigate to {url}") from last_error

    async def assert_no_human_verification(self, page: Any, url: str) -> None:
        title = ""
        body = ""
        try:
            title = await page.title()
            body = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            pass
        challenge_text = f"{title}\n{body}".lower()
        if any(marker in challenge_text for marker in ["captcha", "verify you are human", "cloudflare", "just a moment", "attention required"]):
            await self.save_debug(page, "human-verification")
            raise HumanVerificationRequired(f"Human verification required at {url}")

    async def dismiss_obstacles(self, page: Any) -> None:
        selectors = [
            'button:has-text("Accept All")',
            'button:has-text("Accept all")',
            'button:has-text("I Accept")',
            'button:has-text("Continue")',
            '#onetrust-accept-btn-handler',
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if await button.is_visible(timeout=800):
                    await button.click()
                    self.logger.info("Dismissed obstacle with selector %s", selector)
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    async def extract_child_links(self, page: Any) -> list[LinkCandidate]:
        selector = self.config.selector("child_links", ".nys-openleg-result-item-link")
        label_selector = self.config.selector("child_label", ".nys-openleg-result-item-name")
        description_selector = self.config.selector("child_description", ".nys-openleg-result-item-description")
        try:
            raw_links = await page.locator(selector).evaluate_all(
                """
                (nodes, cfg) => nodes.map((node) => {
                  const row = node.closest('.nys-openleg-result-item') || node.parentElement || node;
                  const labelNode = row.querySelector(cfg.labelSelector);
                  const descriptionNode = row.querySelector(cfg.descriptionSelector);
                  return {
                    href: node.href || node.getAttribute('href') || '',
                    label: (labelNode ? labelNode.textContent : node.textContent || '').trim(),
                    description: (descriptionNode ? descriptionNode.textContent : '').trim()
                  };
                })
                """,
                {"labelSelector": label_selector, "descriptionSelector": description_selector},
            )
        except Exception:
            raw_links = []

        seen: set[str] = set()
        candidates: list[LinkCandidate] = []
        for raw in raw_links:
            href = str(raw.get("href") or "")
            absolute = canonical_url(urljoin(page.url, href))
            if absolute in seen or not is_relevant_law_url(absolute, self.config.root_url):
                continue
            seen.add(absolute)
            candidates.append(
                LinkCandidate(
                    url=absolute,
                    label=clean_text(str(raw.get("label") or "")),
                    description=clean_text(str(raw.get("description") or "")),
                )
            )
        return candidates

    async def extract_current_title(self, page: Any) -> str:
        headline = await first_locator_text(
            page,
            self.config.selector("title_headline", ".nys-openleg-result-title-headline"),
            self.config.selector_timeout_ms,
        )
        short_title = await first_locator_text(
            page,
            self.config.selector("title_short", ".nys-openleg-result-title-short"),
            self.config.selector_timeout_ms,
        )
        location_context = await first_locator_text(
            page,
            self.config.selector("title_location", ".nys-openleg-result-title-location"),
            1000,
        )
        title = compose_level_title(headline, short_title, location_context=location_context)
        if title:
            return title
        if location_context:
            self.logger.warning("Ignoring location context because no clean title was found for %s", page.url)

        fallback = await first_locator_text(
            page,
            ".nys-openleg-result-title :is(h1, h2, h3, h4)",
            self.config.selector_timeout_ms,
        )
        if fallback:
            self.logger.info("Using scoped fallback title selector for %s", page.url)
        return fallback

    async def extract_legal_text(self, page: Any) -> str:
        selector = self.config.selector("legal_text", ".nys-openleg-result-text")
        return clean_text(await first_locator_text(page, selector, self.config.selector_timeout_ms))

    async def extract_revision_metadata(self, page: Any) -> str:
        for selector in self.config.selector_list("revision"):
            text = clean_text(await first_locator_text(page, selector, 1500))
            if text:
                return text
        try:
            body = await page.locator("body").inner_text(timeout=1500)
        except Exception:
            return ""
        match = re.search(r"Viewing\s+most\s+recent\s+revision\s*\(from\s*([^)]+)\)", body, flags=re.IGNORECASE)
        return clean_text(match.group(0)) if match else ""

    async def save_debug(self, page: Any, reason: str) -> None:
        if not (self.config.save_html_on_failure or self.config.save_screenshot_on_failure):
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason).strip("-") or "debug"
        base = self.config.debug_dir / f"{timestamp}_{safe_reason}"
        self.config.debug_dir.mkdir(parents=True, exist_ok=True)
        if self.config.save_html_on_failure:
            try:
                (base.with_suffix(".html")).write_text(await page.content(), encoding="utf-8")
            except Exception as exc:
                self.logger.warning("Could not save debug HTML: %s", exc)
        if self.config.save_screenshot_on_failure:
            try:
                await page.screenshot(path=str(base.with_suffix(".png")), full_page=True, timeout=10000)
            except Exception as exc:
                self.logger.warning("Could not save debug screenshot: %s", exc)


async def first_locator_text(page: Any, selector: str, timeout_ms: int) -> str:
    try:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            return ""
        return clean_text(await locator.inner_text(timeout=timeout_ms))
    except Exception:
        return ""


def assign_level(carried_levels: Mapping[str, str], depth: int, current_title: str) -> dict[str, str]:
    if depth >= len(LEVEL_KEYS):
        raise LevelOverflowError(f"Depth {depth} exceeds level100.")
    levels = {key: str(carried_levels.get(key, "")) for key in LEVEL_KEYS}
    title = clean_text(current_title)
    if title:
        levels[LEVEL_KEYS[depth]] = title
    return levels


def compose_level_title(headline: Any, short_title: Any, *, location_context: Any = None) -> str:
    _ = location_context
    parts = []
    for part in [headline, short_title]:
        text = clean_text(part)
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts)


def classify_page(child_links: Iterable[LinkCandidate], legal_text: str) -> str:
    if list(child_links):
        return "navigation"
    if clean_text(legal_text):
        return "leaf"
    return "empty"


def limit_root_title_links(root_links: Iterable[LinkCandidate], pilot_title_limit: int | None) -> list[LinkCandidate]:
    links = list(root_links)
    if pilot_title_limit is None:
        return links
    return links[:pilot_title_limit]


def crawl_scope_label(title_links: Iterable[LinkCandidate], pilot_title_limit: int | None) -> str:
    links = list(title_links)
    if not links:
        return ""
    if pilot_title_limit is None:
        return "all root-discovered titles"
    if len(links) == 1:
        return links[0].label or links[0].url
    return f"{len(links)} root-discovered titles"


def build_record(source_url: str, levels: Mapping[str, str], revision: str, contents: str) -> dict[str, str]:
    record = {
        "sourceURL": source_url,
        "revisionDate": clean_text(revision),
        "contents": clean_text(contents),
    }
    for key in LEVEL_KEYS:
        record[key] = clean_text(levels.get(key, ""))
    return record


def validate_record(record: Mapping[str, str], *, has_children: bool, config: CrawlerConfig) -> list[str]:
    errors: list[str] = []
    if config.require_source_url and not clean_text(record.get("sourceURL", "")):
        errors.append("missing sourceURL")
    if config.require_at_least_one_level and not any(clean_text(record.get(key, "")) for key in LEVEL_KEYS):
        errors.append("missing captured level")
    if len(clean_text(record.get("contents", ""))) < config.min_contents_chars:
        errors.append("contents too short")
    if config.reject_navigation_pages and has_children:
        errors.append("navigation page cannot be emitted")
    return errors


def render_xml_document(record: Mapping[str, str]) -> list[str]:
    lines = ["    <document>"]
    lines.append(f"      <sourceURL>{xml_escape(record.get('sourceURL', ''))}</sourceURL>")
    lines.append(f"      <revisionDate>{xml_escape(record.get('revisionDate', ''))}</revisionDate>")
    for key in LEVEL_KEYS:
        lines.append(f"      <{key}>{xml_escape(record.get(key, ''))}</{key}>")
    lines.append(f"      <contents>{xml_escape(record.get('contents', ''))}</contents>")
    lines.append("    </document>")
    return lines


def is_relevant_law_url(url: str, root_url: str) -> bool:
    parsed = urlparse(url)
    root = urlparse(root_url)
    if parsed.netloc and root.netloc and parsed.netloc.lower() != root.netloc.lower():
        return False
    path = parsed.path.rstrip("/")
    if not path.startswith("/legislation/laws/"):
        return False
    if path.upper().endswith("/CONSOLIDATED"):
        return False
    return True


def canonical_url(url: str) -> str:
    clean, _fragment = urldefrag(str(url))
    return clean.rstrip("/")


def source_url_key(url: str) -> str:
    return canonical_url(url) if clean_text(url) else ""


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def xml_escape(text: Any) -> str:
    return escape(str(text or ""), {'"': "&quot;", "'": "&apos;"})


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
        return None
    return int(value)


def none_if_blank(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def load_config(path: Path) -> CrawlerConfig:
    try:
        import yaml
    except ImportError as exc:
        raise MissingDependencyError("PyYAML is required to read YAML config. Run: python -m pip install -r requirements.txt") from exc

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Config root must be a mapping: {path}")
    return CrawlerConfig.from_mapping(raw)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the bounded NYSenate Consolidated Laws crawler.")
    parser.add_argument("--config", default="config/nysenate_crawler.yaml", help="Path to YAML config.")
    parser.add_argument("--headless", choices=["true", "false"], help="Override browser headless setting.")
    parser.add_argument("--max-pages", type=int, help="Override max page safety limit.")
    parser.add_argument("--max-documents", type=int, help="Override max document safety limit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(Path(args.config)).with_overrides(
            headless=None if args.headless is None else as_bool(args.headless),
            max_pages=args.max_pages,
            max_documents=args.max_documents,
        )
        configure_logging(config.log_file)
        count = asyncio.run(NYSenatePilotCrawler(config).run())
        print(f"Crawl complete: {count} document(s) written to {config.output_xml}")
        return 0
    except KeyboardInterrupt:
        print("Crawl interrupted by user; XML and checkpoints were preserved.", file=sys.stderr)
        return 130
    except HumanVerificationRequired as exc:
        print(f"Human verification required: {exc}", file=sys.stderr)
        return 3
    except MissingDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Crawl failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
