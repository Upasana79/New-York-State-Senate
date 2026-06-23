from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urldefrag, urljoin, urlparse
from xml.sax.saxutils import escape


LEVEL_KEYS = [f"level{level}" for level in range(10, 101, 10)]
LOGGER_NAME = "nysenate_crawler"
CAPSOLVER_ENV_VAR = "CAPSOLVER_API_KEY"
DEFAULT_CAPSOLVER_KEY_FILE = Path("captcha key.txt")
DEFAULT_CAPSOLVER_API_BASE = "https://api.capsolver.com"
DEFAULT_CAPSOLVER_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_CAPSOLVER_TIMEOUT_SECONDS = 120.0
DEFAULT_CAPSOLVER_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
]
CHALLENGE_STATUS_CODES = {403, 503}
CHALLENGE_TITLE_MARKERS = (
    "captcha",
    "verify you are human",
    "cloudflare",
    "just a moment",
    "verification",
    "attention required",
)
CHALLENGE_BODY_MARKERS = (
    "captcha",
    "verify you are human",
    "checking if you are human",
    "checking your browser",
    "cloudflare",
    "just a moment",
    "attention required",
)
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]


class MissingDependencyError(RuntimeError):
    """Raised when an optional runtime dependency is required for a live run."""


class HumanVerificationRequired(RuntimeError):
    """Raised when the live site requires manual verification."""


class CapSolverError(RuntimeError):
    """Raised when CapSolver cannot produce or apply a usable solution."""


class LevelOverflowError(RuntimeError):
    """Raised when a crawl path is deeper than the configured XML schema."""


class XMLDocumentStoreError(RuntimeError):
    """Raised when an existing XML checkpoint cannot be loaded safely."""


@dataclass(frozen=True)
class LinkCandidate:
    """Navigation row metadata; label/description are not XML level sources."""

    url: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class BrowserIdentity:
    index: int
    user_agent: str
    user_data_dir: Path
    meta_file: Path

    @property
    def label(self) -> str:
        return f"ua{self.index + 1:02d}"


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
    session_dir: Path = Path("data/sessions")
    session_target: str = "nysenate"
    user_data_dir: Path = Path("data/sessions/nysenate_profile")
    user_agents: list[str] = field(default_factory=lambda: list(DEFAULT_USER_AGENTS))
    rotate_user_agent_per_title: bool = True
    browser_args: list[str] = field(default_factory=lambda: list(DEFAULT_BROWSER_ARGS))
    stealth_enabled: bool = True
    page_load_timeout_ms: int = 45000
    selector_timeout_ms: int = 8000
    navigation_delay_ms: int = 1000
    content_stable_delay_ms: int = 1200
    pre_navigation_delay_min_ms: int = 1000
    pre_navigation_delay_max_ms: int = 3000
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    rate_limit_backoff_base_seconds: float = 15.0
    timeout_backoff_base_seconds: float = 10.0
    selectors: dict[str, Any] = field(default_factory=dict)
    save_html_on_failure: bool = True
    save_screenshot_on_failure: bool = True
    save_debug_on_success: bool = False
    min_contents_chars: int = 20
    require_source_url: bool = True
    require_at_least_one_level: bool = True
    reject_navigation_pages: bool = True
    capsolver_api_key: str = ""
    capsolver_key_file: Path = DEFAULT_CAPSOLVER_KEY_FILE
    capsolver_api_base: str = DEFAULT_CAPSOLVER_API_BASE
    capsolver_proxy: str = ""
    capsolver_poll_interval_seconds: float = DEFAULT_CAPSOLVER_POLL_INTERVAL_SECONDS
    capsolver_timeout_seconds: float = DEFAULT_CAPSOLVER_TIMEOUT_SECONDS
    capsolver_request_timeout_seconds: float = DEFAULT_CAPSOLVER_REQUEST_TIMEOUT_SECONDS
    capsolver_preferred: bool = True
    capsolver_log_balance_on_start: bool = True
    capsolver_use_proxy_for_browser: bool = True
    challenge_interaction_enabled: bool = True
    challenge_mouse_moves: int = 4
    challenge_iframe_timeout_ms: int = 15000
    challenge_settle_ms: int = 10000
    challenge_cooldown_seconds: float = 15.0

    def __post_init__(self) -> None:
        self.session_target = safe_session_target(self.session_target)
        self.user_agents = user_agents_with_defaults(self.user_agents)
        self.browser_args = browser_args_with_defaults(self.browser_args)
        self.pre_navigation_delay_min_ms = max(0, int(self.pre_navigation_delay_min_ms))
        self.pre_navigation_delay_max_ms = max(self.pre_navigation_delay_min_ms, int(self.pre_navigation_delay_max_ms))
        self.capsolver_api_base = str(self.capsolver_api_base or DEFAULT_CAPSOLVER_API_BASE).rstrip("/")
        self.capsolver_proxy = clean_text(self.capsolver_proxy)
        self.capsolver_poll_interval_seconds = max(0.5, float(self.capsolver_poll_interval_seconds))
        self.capsolver_timeout_seconds = max(self.capsolver_poll_interval_seconds, float(self.capsolver_timeout_seconds))
        self.capsolver_request_timeout_seconds = max(1.0, float(self.capsolver_request_timeout_seconds))
        self.challenge_mouse_moves = max(0, int(self.challenge_mouse_moves))
        self.challenge_iframe_timeout_ms = max(0, int(self.challenge_iframe_timeout_ms))
        self.challenge_settle_ms = max(0, int(self.challenge_settle_ms))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CrawlerConfig":
        browser = dict(raw.get("browser") or {})
        targets = dict(raw.get("targets") or {})
        files = dict(raw.get("files") or {})
        timing = dict(raw.get("timing") or {})
        debug = dict(raw.get("debug") or {})
        validation = dict(raw.get("validation") or {})
        selectors = dict(raw.get("selectors") or {})
        captcha = dict(raw.get("captcha") or {})
        capsolver_key_file = Path(captcha.get("capsolver_key_file", DEFAULT_CAPSOLVER_KEY_FILE))

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
            session_dir=Path(browser.get("session_dir", "data/sessions")),
            session_target=safe_session_target(browser.get("session_target", "nysenate")),
            user_data_dir=session_profile_dir(browser),
            user_agents=user_agents_with_defaults(browser.get("user_agents") or []),
            rotate_user_agent_per_title=as_bool(browser.get("rotate_user_agent_per_title", True)),
            browser_args=browser_args_with_defaults(browser.get("args") or []),
            stealth_enabled=as_bool(browser.get("stealth_enabled", True)),
            page_load_timeout_ms=int(timing.get("page_load_timeout_ms", 45000)),
            selector_timeout_ms=int(timing.get("selector_timeout_ms", 8000)),
            navigation_delay_ms=int(timing.get("navigation_delay_ms", 1000)),
            content_stable_delay_ms=int(timing.get("content_stable_delay_ms", 1200)),
            pre_navigation_delay_min_ms=int(timing.get("pre_navigation_delay_min_ms", 1000)),
            pre_navigation_delay_max_ms=int(timing.get("pre_navigation_delay_max_ms", 3000)),
            max_retries=int(timing.get("max_retries", 3)),
            retry_backoff_seconds=float(timing.get("retry_backoff_seconds", 2)),
            rate_limit_backoff_base_seconds=float(timing.get("rate_limit_backoff_base_seconds", 15)),
            timeout_backoff_base_seconds=float(timing.get("timeout_backoff_base_seconds", 10)),
            selectors=selectors,
            save_html_on_failure=as_bool(debug.get("save_html_on_failure", True)),
            save_screenshot_on_failure=as_bool(debug.get("save_screenshot_on_failure", True)),
            save_debug_on_success=as_bool(debug.get("save_debug_on_success", False)),
            min_contents_chars=int(validation.get("min_contents_chars", 20)),
            require_source_url=as_bool(validation.get("require_source_url", True)),
            require_at_least_one_level=as_bool(validation.get("require_at_least_one_level", True)),
            reject_navigation_pages=as_bool(validation.get("reject_navigation_pages", True)),
            capsolver_api_key=load_capsolver_api_key(capsolver_key_file),
            capsolver_key_file=capsolver_key_file,
            capsolver_api_base=str(captcha.get("capsolver_api_base", DEFAULT_CAPSOLVER_API_BASE)),
            capsolver_proxy=clean_text(captcha.get("capsolver_proxy", captcha.get("proxy", ""))),
            capsolver_poll_interval_seconds=float(captcha.get("capsolver_poll_interval_seconds", DEFAULT_CAPSOLVER_POLL_INTERVAL_SECONDS)),
            capsolver_timeout_seconds=float(captcha.get("capsolver_timeout_seconds", DEFAULT_CAPSOLVER_TIMEOUT_SECONDS)),
            capsolver_request_timeout_seconds=float(captcha.get("capsolver_request_timeout_seconds", DEFAULT_CAPSOLVER_REQUEST_TIMEOUT_SECONDS)),
            capsolver_preferred=as_bool(captcha.get("capsolver_preferred", True)),
            capsolver_log_balance_on_start=as_bool(captcha.get("capsolver_log_balance_on_start", True)),
            capsolver_use_proxy_for_browser=as_bool(captcha.get("capsolver_use_proxy_for_browser", True)),
            challenge_interaction_enabled=as_bool(captcha.get("challenge_interaction_enabled", True)),
            challenge_mouse_moves=int(captcha.get("challenge_mouse_moves", 4)),
            challenge_iframe_timeout_ms=int(captcha.get("challenge_iframe_timeout_ms", 15000)),
            challenge_settle_ms=int(captcha.get("challenge_settle_ms", 10000)),
            challenge_cooldown_seconds=float(captcha.get("challenge_cooldown_seconds", 15)),
        )

    def with_overrides(
        self,
        *,
        headless: bool | None = None,
        max_pages: int | None = None,
        max_documents: int | None = None,
    ) -> "CrawlerConfig":
        values: dict[str, Any] = {}
        if headless is not None:
            values["headless"] = headless
        if max_pages is not None:
            values["max_pages"] = max_pages
        if max_documents is not None:
            values["max_documents"] = max_documents
        return replace(self, **values)

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


class CapSolverClient:
    def __init__(self, api_key: str, *, api_base: str = DEFAULT_CAPSOLVER_API_BASE, request_timeout_seconds: float = DEFAULT_CAPSOLVER_REQUEST_TIMEOUT_SECONDS) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.request_timeout_seconds = request_timeout_seconds

    async def solve_task(self, task: Mapping[str, Any], *, poll_interval_seconds: float, timeout_seconds: float) -> dict[str, Any]:
        create_response = await self.post_json("createTask", {"clientKey": self.api_key, "task": dict(task)})
        self.raise_for_capsolver_error(create_response, "createTask")
        task_id = clean_text(create_response.get("taskId", ""))
        if not task_id:
            raise CapSolverError("CapSolver createTask did not return a taskId.")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            result = await self.post_json("getTaskResult", {"clientKey": self.api_key, "taskId": task_id})
            self.raise_for_capsolver_error(result, "getTaskResult")
            status = clean_text(result.get("status", "")).lower()
            if status == "ready":
                solution = result.get("solution")
                if not isinstance(solution, Mapping):
                    raise CapSolverError("CapSolver returned a ready result without a solution object.")
                return dict(solution)
            if status not in {"", "idle", "processing"}:
                raise CapSolverError(f"CapSolver returned unexpected task status {status!r}.")
            if loop.time() >= deadline:
                raise CapSolverError(f"CapSolver task {task_id} did not finish within {timeout_seconds:.0f}s.")
            await asyncio.sleep(poll_interval_seconds)

    async def get_balance(self) -> dict[str, Any]:
        response = await self.post_json("getBalance", {"clientKey": self.api_key})
        self.raise_for_capsolver_error(response, "getBalance")
        return response

    async def post_json(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post_json_sync, path, payload)

    def _post_json_sync(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/{path.lstrip('/')}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CapSolverError(f"CapSolver HTTP {exc.code} from {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CapSolverError(f"Could not reach CapSolver {path}: {exc.reason}") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CapSolverError(f"CapSolver {path} returned non-JSON response.") from exc
        if not isinstance(decoded, Mapping):
            raise CapSolverError(f"CapSolver {path} returned unexpected JSON.")
        return dict(decoded)

    @staticmethod
    def raise_for_capsolver_error(response: Mapping[str, Any], operation: str) -> None:
        try:
            error_id = int(response.get("errorId", 0))
        except (TypeError, ValueError):
            error_id = 0
        if error_id == 0:
            return
        code = clean_text(response.get("errorCode", "")) or "ERROR_CAPSOLVER"
        description = clean_text(response.get("errorDescription", ""))
        detail = f"{code}: {description}" if description else code
        raise CapSolverError(f"CapSolver {operation} failed: {detail}")


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
            await self.log_capsolver_readiness()

            async with async_playwright() as playwright:
                context = None
                try:
                    context = await self.open_context(playwright, self.next_browser_identity())
                    page = context.pages[0] if context.pages else await context.new_page()
                    await self.apply_stealth(page)
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

                    for index, title_link in enumerate(title_links):
                        if self.safety_limit_reached():
                            self.logger.info("Stopping crawl before next title because a safety limit was reached.")
                            break
                        if index > 0 and self.config.rotate_user_agent_per_title:
                            await context.close()
                            context = await self.open_context(playwright, self.next_browser_identity())
                            page = context.pages[0] if context.pages else await context.new_page()
                            await self.apply_stealth(page)
                        self.logger.info("Selected crawl title: %s (%s)", title_link.label or title_link.url, title_link.url)
                        await self.crawl_page(page, title_link.url, {}, depth=0)
                finally:
                    if context is not None:
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
            self.config.session_dir,
            self.config.user_data_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def load_checkpoint(self) -> None:
        self.visited_urls.update(read_checkpoint_urls(self.config.visited_file))
        self.failed_urls.update(read_checkpoint_urls(self.config.failed_file))

    def flush_checkpoint(self) -> None:
        write_text_atomic(self.config.visited_file, "\n".join(sorted(self.visited_urls)) + ("\n" if self.visited_urls else ""))
        write_text_atomic(self.config.failed_file, "\n".join(sorted(self.failed_urls)) + ("\n" if self.failed_urls else ""))

    async def log_capsolver_readiness(self) -> None:
        if not self.config.capsolver_api_key:
            self.logger.info("CapSolver API key is not configured; challenge solving will use browser fallback only.")
            return
        if not self.config.capsolver_log_balance_on_start:
            self.logger.info("CapSolver API key is configured; startup balance check is disabled.")
            return

        client = CapSolverClient(
            self.config.capsolver_api_key,
            api_base=self.config.capsolver_api_base,
            request_timeout_seconds=self.config.capsolver_request_timeout_seconds,
        )
        try:
            balance = await client.get_balance()
        except CapSolverError as exc:
            self.logger.warning("CapSolver API key is configured, but startup balance check failed: %s", exc)
            return

        packages = balance.get("packages")
        package_count = len(packages) if isinstance(packages, list) else 0
        self.logger.info(
            "CapSolver ready: balance=%s USD, active package entries=%s, preferred=%s.",
            balance.get("balance", "unknown"),
            package_count,
            self.config.capsolver_preferred,
        )

    def next_browser_identity(self) -> BrowserIdentity:
        index = self.next_user_agent_index()
        user_agent = self.config.user_agents[index]
        profile_name = f"{self.config.session_target}_ua{index + 1:02d}_profile"
        meta_name = f"{self.config.session_target}_ua{index + 1:02d}_meta.json"
        identity = BrowserIdentity(
            index=index,
            user_agent=user_agent,
            user_data_dir=self.config.session_dir / profile_name,
            meta_file=self.config.session_dir / meta_name,
        )
        identity.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.write_browser_identity_meta(identity)
        self.write_user_agent_cursor((index + 1) % len(self.config.user_agents))
        self.logger.info("Selected browser identity %s with profile=%s", identity.label, identity.user_data_dir)
        return identity

    def next_user_agent_index(self) -> int:
        cursor = read_json_mapping(self.user_agent_cursor_file())
        raw_index = cursor.get("next_index", 0)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = 0
        return index % len(self.config.user_agents)

    def write_user_agent_cursor(self, next_index: int) -> None:
        payload = {
            "session_target": self.config.session_target,
            "next_index": next_index,
            "pool_size": len(self.config.user_agents),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json_atomic(self.user_agent_cursor_file(), payload)

    def write_browser_identity_meta(self, identity: BrowserIdentity) -> None:
        payload = {
            "session_target": self.config.session_target,
            "identity": identity.label,
            "identity_index": identity.index,
            "user_agent": identity.user_agent,
            "profile_dir": str(identity.user_data_dir),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json_atomic(identity.meta_file, payload)

    def user_agent_cursor_file(self) -> Path:
        return self.config.session_dir / f"{self.config.session_target}_user_agent_cursor.json"

    async def open_context(self, playwright: Any, identity: BrowserIdentity | None = None) -> Any:
        user_data_dir = identity.user_data_dir if identity else self.config.user_data_dir
        user_agent = identity.user_agent if identity else self.config.user_agents[0]
        kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "viewport": {"width": self.config.viewport_width, "height": self.config.viewport_height},
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
            "extra_http_headers": {"Accept-Language": self.config.accept_language},
            "args": self.config.browser_args,
            "user_agent": user_agent,
        }
        if self.config.channel:
            kwargs["channel"] = self.config.channel
        proxy = playwright_proxy_from_capsolver_proxy(self.config.capsolver_proxy) if self.config.capsolver_use_proxy_for_browser else {}
        if proxy:
            kwargs["proxy"] = proxy

        try:
            self.logger.info(
                "Launching Playwright persistent browser context: channel=%s headless=%s profile=%s user_agent=%s proxy=%s",
                self.config.channel or "default",
                self.config.headless,
                user_data_dir,
                user_agent,
                proxy.get("server", "none") if proxy else "none",
            )
            return await playwright.chromium.launch_persistent_context(user_data_dir=str(user_data_dir), **kwargs)
        except Exception as exc:
            if not self.config.channel:
                raise
            raise RuntimeError(
                f"Could not launch real Chrome channel {self.config.channel!r}. "
                "Install Chrome with: python -m playwright install chrome"
            ) from exc

    async def apply_stealth(self, page: Any) -> None:
        if not self.config.stealth_enabled:
            return
        try:
            from playwright_stealth import Stealth
        except ImportError as exc:
            raise MissingDependencyError("playwright-stealth is required when browser.stealth_enabled is true. Run: python -m pip install -r requirements.txt") from exc

        await Stealth().apply_stealth_async(page)
        self.logger.info("Applied playwright-stealth to the shared NY Senate crawl page.")

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

        # Levels come from the destination page after navigation. Child-row
        # label/description text is navigation metadata, not XML level text.
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
            await self.pause_before_navigation(page)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_load_timeout_ms)
                if response is None:
                    last_error = RuntimeError("Navigation returned no response.")
                    wait = self.config.retry_backoff_seconds * attempt
                    self.logger.warning("No response for %s on attempt %s/%s. Backing off %.1fs.", url, attempt, self.config.max_retries, wait)
                    if attempt < self.config.max_retries:
                        await self.wait_seconds(page, wait)
                    continue

                try:
                    await page.wait_for_load_state("networkidle", timeout=self.config.selector_timeout_ms)
                except Exception:
                    pass

                status = getattr(response, "status", None)
                title, body = await self.page_verification_text(page)

                if status == 429:
                    wait = self.config.rate_limit_backoff_base_seconds * (2 ** (attempt - 1))
                    self.logger.warning("429 Too Many Requests for %s. Backing off %.1fs.", url, wait)
                    last_error = RuntimeError("HTTP status 429")
                    if attempt < self.config.max_retries:
                        await self.wait_seconds(page, wait)
                    continue

                markers = challenge_markers(status, title, body)
                if markers:
                    await self.save_debug(page, "human-verification")
                    self.logger.warning(
                        "Human-verification challenge detected for %s on attempt %s/%s: %s",
                        url,
                        attempt,
                        self.config.max_retries,
                        ", ".join(markers),
                    )
                    if await self.try_challenge_solvers(page, url, markers, title, body):
                        await page.wait_for_timeout(self.config.navigation_delay_ms)
                        return

                    wait = self.config.challenge_cooldown_seconds
                    last_error = HumanVerificationRequired(self.human_verification_message(url))
                    if attempt < self.config.max_retries:
                        self.logger.warning("Still blocked by human verification. Cooling down %.1fs before retry.", wait)
                        await self.wait_seconds(page, wait)
                    continue

                if status and status >= 400:
                    raise RuntimeError(f"HTTP status {status}")

                await page.wait_for_timeout(self.config.navigation_delay_ms)
                return
            except Exception as exc:
                last_error = exc
                if is_playwright_timeout_error(exc):
                    wait = self.config.timeout_backoff_base_seconds * (2 ** (attempt - 1))
                    self.logger.warning("Timeout while navigating to %s on attempt %s/%s. Backing off %.1fs.", url, attempt, self.config.max_retries, wait)
                else:
                    wait = self.config.retry_backoff_seconds * attempt
                    self.logger.warning("Navigation attempt %s/%s failed for %s: %s", attempt, self.config.max_retries, url, exc)
                if attempt < self.config.max_retries:
                    await self.wait_seconds(page, wait)

        self.failed_urls.add(canonical_url(url))
        await self.save_debug(page, "navigation-failed")
        if isinstance(last_error, HumanVerificationRequired):
            raise last_error
        raise RuntimeError(f"Could not navigate to {url}") from last_error

    async def pause_before_navigation(self, page: Any) -> None:
        delay = random.randint(self.config.pre_navigation_delay_min_ms, self.config.pre_navigation_delay_max_ms)
        if delay > 0:
            await page.wait_for_timeout(delay)

    async def wait_seconds(self, page: Any, seconds: float) -> None:
        await page.wait_for_timeout(max(0, int(seconds * 1000)))

    async def page_verification_text(self, page: Any) -> tuple[str, str]:
        title = ""
        body = ""
        try:
            title = await page.title()
        except Exception:
            pass
        try:
            body = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            pass
        return title, body

    async def try_challenge_solvers(self, page: Any, url: str, markers: list[str], title: str, body: str) -> bool:
        capsolver_error: HumanVerificationRequired | None = None

        if self.config.capsolver_api_key and self.config.capsolver_preferred:
            try:
                if await self.try_capsolver_human_verification(page, url, markers, title, body):
                    return True
            except HumanVerificationRequired as exc:
                capsolver_error = exc
                self.logger.warning("CapSolver did not clear %s before browser fallback: %s", url, exc)

        if await self.try_light_challenge_interaction(page, url):
            return True

        if self.config.capsolver_api_key and not self.config.capsolver_preferred:
            if await self.try_capsolver_human_verification(page, url, markers, title, body):
                return True

        if capsolver_error is not None:
            raise capsolver_error
        return False

    async def try_light_challenge_interaction(self, page: Any, url: str) -> bool:
        if not self.config.challenge_interaction_enabled:
            return False
        try:
            for _ in range(self.config.challenge_mouse_moves):
                await page.mouse.move(
                    random.randint(100, min(800, max(100, self.config.viewport_width - 1))),
                    random.randint(100, min(600, max(100, self.config.viewport_height - 1))),
                )
                await page.wait_for_timeout(random.randint(200, 500))

            self.logger.info("Waiting for challenge iframe to appear for %s.", url)
            await page.wait_for_selector("iframe", state="attached", timeout=self.config.challenge_iframe_timeout_ms)
            iframe_locator = page.locator("iframe")
            iframe_count = await iframe_locator.count()
            self.logger.info("Found %s iframe(s) while checking challenge page for %s.", iframe_count, url)

            for index in range(iframe_count):
                box = await iframe_locator.nth(index).bounding_box()
                if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                    continue
                x_offset = min(30, max(1, box["width"] / 2))
                cx = box["x"] + x_offset
                cy = box["y"] + box["height"] / 2
                await page.mouse.move(cx, cy, steps=10)
                await page.wait_for_timeout(400)
                await page.mouse.click(cx, cy)
                self.logger.info("Clicked visible challenge iframe %s for %s.", index, url)
                break
        except Exception as exc:
            self.logger.warning("Could not complete light challenge interaction for %s: %s", url, exc)

        if self.config.challenge_settle_ms > 0:
            await page.wait_for_timeout(self.config.challenge_settle_ms)
        title, body = await self.page_verification_text(page)
        if not challenge_markers(None, title, body):
            self.logger.info("Human-verification fingerprints cleared for %s.", url)
            return True
        return False

    async def try_capsolver_human_verification(self, page: Any, url: str, markers: list[str], title: str, body: str) -> bool:
        if not self.config.capsolver_api_key:
            return False

        try:
            self.logger.info("Attempting CapSolver challenge resolution for %s.", url)
            turnstile = await self.extract_turnstile_challenge(page)
            if clean_text(turnstile.get("websiteKey", "")):
                await self.solve_turnstile_challenge(page, url, turnstile)
            else:
                challenge_text = f"{' '.join(markers)}\n{title}\n{body}".lower()
                if not any(marker in challenge_text for marker in ("cloudflare", "just a moment", "attention required", "http 403", "http 503")):
                    raise CapSolverError("No supported CAPTCHA parameters were found on the challenge page.")
                await self.solve_cloudflare_challenge(page, url)

            new_title, new_body = await self.page_verification_text(page)
            remaining = challenge_markers(None, new_title, new_body)
            if remaining:
                await self.save_debug(page, "human-verification-capsolver-remaining")
                raise CapSolverError(f"solution applied but challenge markers remained: {', '.join(remaining)}")

            self.logger.info("CapSolver cleared human-verification fingerprints for %s.", url)
            return True
        except CapSolverError as exc:
            raise HumanVerificationRequired(f"Human verification required at {url}; CapSolver could not solve it: {exc}") from exc

    async def extract_turnstile_challenge(self, page: Any) -> dict[str, str]:
        try:
            raw = await page.evaluate(
                """
                () => {
                  const candidates = Array.from(document.querySelectorAll('.cf-turnstile[data-sitekey], [data-sitekey]'));
                  for (const node of candidates) {
                    const websiteKey = node.getAttribute('data-sitekey') || '';
                    if (websiteKey) {
                      return {
                        websiteKey,
                        action: node.getAttribute('data-action') || '',
                        cdata: node.getAttribute('data-cdata') || ''
                      };
                    }
                  }
                  const html = document.documentElement ? document.documentElement.innerHTML : '';
                  const match = html.match(/data-sitekey=["']([^"']+)["']/i)
                    || html.match(/["']sitekey["']\\s*[:=]\\s*["']([^"']+)["']/i)
                    || html.match(/["']siteKey["']\\s*[:=]\\s*["']([^"']+)["']/i);
                  return match ? { websiteKey: match[1], action: '', cdata: '' } : {};
                }
                """
            )
        except Exception:
            return {}
        if not isinstance(raw, Mapping):
            return {}
        return {
            "websiteKey": clean_text(raw.get("websiteKey", "")),
            "action": clean_text(raw.get("action", "")),
            "cdata": clean_text(raw.get("cdata", "")),
        }

    async def solve_turnstile_challenge(self, page: Any, url: str, challenge: Mapping[str, str]) -> None:
        task: dict[str, Any] = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": url,
            "websiteKey": clean_text(challenge.get("websiteKey", "")),
        }
        metadata = {
            key: clean_text(challenge.get(key, ""))
            for key in ("action", "cdata")
            if clean_text(challenge.get(key, ""))
        }
        if metadata:
            task["metadata"] = metadata

        solution = await self.solve_capsolver_task(task)
        token = clean_text(
            solution.get("token", "")
            or solution.get("gRecaptchaResponse", "")
            or solution.get("captchaToken", "")
        )
        if not token:
            raise CapSolverError("Turnstile task returned no token.")

        await self.apply_turnstile_token(page, token)
        await page.wait_for_timeout(self.config.challenge_settle_ms)

    async def apply_turnstile_token(self, page: Any, token: str) -> None:
        await page.evaluate(
            """
            (token) => {
              const responseNames = ['cf-turnstile-response', 'g-recaptcha-response', 'h-captcha-response'];
              for (const name of responseNames) {
                const fields = document.querySelectorAll(`textarea[name="${name}"], input[name="${name}"]`);
                for (const field of fields) {
                  field.value = token;
                  field.dispatchEvent(new Event('input', { bubbles: true }));
                  field.dispatchEvent(new Event('change', { bubbles: true }));
                }
              }

              const callbackNames = new Set();
              for (const node of document.querySelectorAll('[data-callback]')) {
                const name = node.getAttribute('data-callback');
                if (name) callbackNames.add(name);
              }
              for (const name of callbackNames) {
                const callback = name.split('.').reduce((value, part) => value && value[part], window);
                if (typeof callback === 'function') callback(token);
              }

              const submit = Array.from(document.querySelectorAll('button[type="submit"], input[type="submit"]'))
                .find((node) => !node.disabled && node.offsetParent !== null);
              if (submit) submit.click();
            }
            """,
            token,
        )

    async def solve_cloudflare_challenge(self, page: Any, url: str) -> None:
        if not self.config.capsolver_proxy:
            raise CapSolverError("Cloudflare Challenge solving requires captcha.capsolver_proxy to be a static or sticky proxy.")

        task: dict[str, Any] = {
            "type": "AntiCloudflareTask",
            "websiteURL": url,
            "proxy": self.config.capsolver_proxy,
        }
        user_agent = await self.browser_user_agent(page)
        if user_agent:
            task["userAgent"] = user_agent
        try:
            html = await page.content()
        except Exception:
            html = ""
        if html:
            task["html"] = html

        solution = await self.solve_capsolver_task(task)
        await self.apply_capsolver_cookies(page, url, solution)
        await self.reload_after_capsolver(page, url)

    async def browser_user_agent(self, page: Any) -> str:
        try:
            return clean_text(await page.evaluate("() => navigator.userAgent"))
        except Exception:
            return self.config.user_agents[0] if self.config.user_agents else ""

    async def solve_capsolver_task(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self.logger.info("Submitting CapSolver task: type=%s websiteURL=%s", task.get("type", "unknown"), task.get("websiteURL", "unknown"))
        client = CapSolverClient(
            self.config.capsolver_api_key,
            api_base=self.config.capsolver_api_base,
            request_timeout_seconds=self.config.capsolver_request_timeout_seconds,
        )
        solution = await client.solve_task(
            task,
            poll_interval_seconds=self.config.capsolver_poll_interval_seconds,
            timeout_seconds=self.config.capsolver_timeout_seconds,
        )
        self.logger.info("CapSolver task completed: type=%s websiteURL=%s", task.get("type", "unknown"), task.get("websiteURL", "unknown"))
        return solution

    async def apply_capsolver_cookies(self, page: Any, url: str, solution: Mapping[str, Any]) -> None:
        cookies = capsolver_cookie_payloads(solution, url)
        if not cookies:
            raise CapSolverError("Cloudflare task returned no cookies or cf_clearance token.")
        await page.context.add_cookies(cookies)

    async def reload_after_capsolver(self, page: Any, url: str) -> None:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_load_timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=self.config.selector_timeout_ms)
        except Exception:
            pass
        status = getattr(response, "status", None)
        if status and status >= 400:
            self.logger.warning("Reload after CapSolver for %s returned HTTP %s.", url, status)

    def human_verification_message(self, url: str) -> str:
        if self.config.capsolver_api_key:
            return f"Human verification required at {url}; CapSolver API key is configured."
        return f"Human verification required at {url}; no CapSolver API key is configured."

    async def assert_no_human_verification(self, page: Any, url: str) -> None:
        title, body = await self.page_verification_text(page)
        markers = challenge_markers(None, title, body)
        if markers:
            await self.save_debug(page, "human-verification")
            if await self.try_challenge_solvers(page, url, markers, title, body):
                return
            raise HumanVerificationRequired(self.human_verification_message(url))

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
        title = compose_level_title(headline, short_title)
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


def capsolver_cookie_payloads(solution: Mapping[str, Any], url: str) -> list[dict[str, Any]]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}/" if parsed.netloc else url
    payloads: list[dict[str, Any]] = []

    raw_cookies = solution.get("cookies")
    if isinstance(raw_cookies, Mapping):
        for name, value in raw_cookies.items():
            cookie_name = clean_text(name)
            cookie_value = clean_text(value)
            if cookie_name and cookie_value:
                payloads.append({"name": cookie_name, "value": cookie_value, "url": origin})
    elif isinstance(raw_cookies, list):
        for raw_cookie in raw_cookies:
            if not isinstance(raw_cookie, Mapping):
                continue
            cookie_name = clean_text(raw_cookie.get("name", ""))
            cookie_value = clean_text(raw_cookie.get("value", ""))
            if not cookie_name or not cookie_value:
                continue
            cookie = dict(raw_cookie)
            cookie["name"] = cookie_name
            cookie["value"] = cookie_value
            if not cookie.get("url") and not cookie.get("domain"):
                cookie["url"] = origin
            payloads.append(cookie)

    token = clean_text(solution.get("token", ""))
    if token and not any(cookie.get("name") == "cf_clearance" for cookie in payloads):
        payloads.append({"name": "cf_clearance", "value": token, "url": origin})
    return payloads


def playwright_proxy_from_capsolver_proxy(proxy: str) -> dict[str, str]:
    text = clean_text(proxy)
    if not text:
        return {}

    scheme = "http"
    rest = text
    if "://" in text:
        scheme, rest = text.split("://", 1)
    else:
        parts = text.split(":")
        if parts and parts[0].lower() in {"http", "https", "socks4", "socks5"}:
            scheme = parts[0].lower()
            rest = ":".join(parts[1:])

    parts = rest.split(":")
    if len(parts) < 2:
        return {}

    host = parts[0].strip()
    port = parts[1].strip()
    if not host or not port:
        return {}

    parsed: dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    if len(parts) >= 3 and parts[2]:
        parsed["username"] = parts[2]
    if len(parts) >= 4:
        parsed["password"] = ":".join(parts[3:])
    return parsed


def challenge_markers(status: int | None, title: str, body: str) -> list[str]:
    markers: list[str] = []
    if status in CHALLENGE_STATUS_CODES:
        markers.append(f"HTTP {status}")

    title_text = title.lower()
    body_text = body.lower()
    for marker in CHALLENGE_TITLE_MARKERS:
        if marker in title_text and marker not in markers:
            markers.append(marker)
    for marker in CHALLENGE_BODY_MARKERS:
        if marker in body_text and marker not in markers:
            markers.append(marker)
    return markers


def is_playwright_timeout_error(exc: Exception) -> bool:
    error_type = exc.__class__
    return "timeout" in error_type.__name__.lower() and "playwright" in error_type.__module__.lower()


def assign_level(carried_levels: Mapping[str, str], depth: int, current_title: str) -> dict[str, str]:
    if depth >= len(LEVEL_KEYS):
        raise LevelOverflowError(f"Depth {depth} exceeds level100.")
    levels = {key: str(carried_levels.get(key, "")) for key in LEVEL_KEYS}
    title = clean_text(current_title)
    if title:
        levels[LEVEL_KEYS[depth]] = title
    return levels


def compose_level_title(headline: Any, short_title: Any) -> str:
    parts = []
    for part in [headline, short_title]:
        text = clean_text(part)
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts)


def classify_page(child_links: list[LinkCandidate], legal_text: str) -> str:
    if child_links:
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
    source_url = clean_text(record.get("sourceURL", ""))
    contents = clean_text(record.get("contents", ""))
    has_captured_level = any(clean_text(record.get(key, "")) for key in LEVEL_KEYS)

    if config.require_source_url and not source_url:
        errors.append("missing sourceURL")
    if config.require_at_least_one_level and not has_captured_level:
        errors.append("missing captured level")
    if len(contents) < config.min_contents_chars:
        errors.append("contents too short")
    if config.reject_navigation_pages and has_children:
        errors.append("navigation page cannot be emitted")
    return errors


def render_xml_document(record: Mapping[str, str]) -> list[str]:
    lines = ["    <document>"]
    for key in ("sourceURL", "revisionDate", *LEVEL_KEYS, "contents"):
        lines.append(f"      <{key}>{xml_escape(record.get(key, ''))}</{key}>")
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
    clean, _fragment = urldefrag(clean_text(url))
    return clean.rstrip("/")


def source_url_key(url: str) -> str:
    return canonical_url(url)


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


def safe_session_target(value: Any) -> str:
    target = clean_text(value or "nysenate").lower()
    return re.sub(r"[^a-z0-9._-]+", "_", target).strip("._-") or "nysenate"


def user_agents_with_defaults(values: Iterable[Any]) -> list[str]:
    agents = [clean_text(value) for value in values if clean_text(value)]
    return agents or list(DEFAULT_USER_AGENTS)


def session_profile_dir(browser: Mapping[str, Any]) -> Path:
    if browser.get("user_data_dir"):
        return Path(browser["user_data_dir"])

    session_dir = Path(browser.get("session_dir", "data/sessions"))
    target = safe_session_target(browser.get("session_target", "nysenate"))
    return session_dir / f"{target}_profile"


def browser_args_with_defaults(values: Iterable[Any]) -> list[str]:
    args = [str(value) for value in values if value]
    for default_arg in DEFAULT_BROWSER_ARGS:
        if default_arg not in args:
            args.append(default_arg)
    return args


def load_capsolver_api_key(key_file: Path = DEFAULT_CAPSOLVER_KEY_FILE, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    env_key = clean_text(values.get(CAPSOLVER_ENV_VAR, ""))
    if env_key:
        return env_key
    try:
        return clean_text(key_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""


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


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def read_checkpoint_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


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
