from __future__ import annotations

import argparse
import asyncio
import html
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nysenate_crawler.pilot_crawler import (  # noqa: E402
    CHALLENGE_BODY_MARKERS,
    CHALLENGE_STATUS_CODES,
    CHALLENGE_TITLE_MARKERS,
    CrawlerConfig,
    MissingDependencyError,
    challenge_markers,
    clean_text,
    compose_level_title,
    is_relevant_law_url,
    load_config,
    playwright_proxy_from_capsolver_proxy,
)


DEFAULT_CONFIG = Path("config/nysenate_crawler.yaml")
DEFAULT_LAW_PATH = "/legislation/laws/ABC"
DEFAULT_ARTICLE_COUNT = 4
DEFAULT_MANUAL_WAIT_SECONDS = 180
DEFAULT_SLOW_MO_MS = 120


@dataclass(frozen=True)
class ReportSettings:
    config: CrawlerConfig
    config_path: Path
    law_path: str
    article_count: int
    report_dir: Path
    shot_dir: Path
    profile_dir: Path
    slow_mo_ms: int
    manual_wait_seconds: int

    @property
    def article_path_prefix(self) -> str:
        return f"{self.law_path.rstrip('/')}/A"

    @property
    def child_path_prefix(self) -> str:
        return f"{self.law_path.rstrip('/')}/"


@dataclass(frozen=True)
class AntiBotEvent:
    kind: str
    url: str
    detail: str
    attempt: int = 0
    wait_seconds: float = 0.0


def repo_path(path: Path | str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else REPO_ROOT / value


def parse_bool(value: str) -> bool:
    return clean_text(value).lower() in {"1", "true", "yes", "y", "on"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a visible-browser HTML evidence report for NY Senate click/capture rules."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to the crawler YAML config.")
    parser.add_argument("--law-path", default=DEFAULT_LAW_PATH, help="Law path to capture, such as /legislation/laws/ABC.")
    parser.add_argument("--article-count", type=int, default=DEFAULT_ARTICLE_COUNT, help="Number of article paths to include.")
    parser.add_argument("--output-root", default="evidence", help="Directory that receives timestamped report folders.")
    parser.add_argument("--profile-dir", help="Persistent Chrome profile directory for this evidence run.")
    parser.add_argument("--headless", choices=["true", "false"], help="Override config browser.headless. Defaults to visible Chrome.")
    parser.add_argument("--slow-mo", type=int, default=DEFAULT_SLOW_MO_MS, help="Playwright slow_mo in milliseconds.")
    parser.add_argument(
        "--manual-wait-seconds",
        type=int,
        default=DEFAULT_MANUAL_WAIT_SECONDS,
        help="Seconds to wait for visible human verification to clear.",
    )
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> ReportSettings:
    config_path = repo_path(args.config)
    config = load_config(config_path).with_overrides(
        headless=False if args.headless is None else parse_bool(args.headless)
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = repo_path(args.output_root) / f"nysenate_click_capture_{timestamp}"
    profile_dir = repo_path(args.profile_dir) if args.profile_dir else repo_path(
        config.session_dir / f"{config.session_target}_evidence_profile"
    )
    law_path = "/" + clean_text(args.law_path).strip("/")
    return ReportSettings(
        config=config,
        config_path=config_path,
        law_path=law_path,
        article_count=max(1, int(args.article_count)),
        report_dir=report_dir,
        shot_dir=report_dir / "screenshots",
        profile_dir=profile_dir,
        slow_mo_ms=max(0, int(args.slow_mo)),
        manual_wait_seconds=max(1, int(args.manual_wait_seconds)),
    )


def browser_context_kwargs(settings: ReportSettings) -> dict[str, Any]:
    config = settings.config
    kwargs: dict[str, Any] = {
        "headless": config.headless,
        "slow_mo": settings.slow_mo_ms,
        "viewport": {"width": config.viewport_width, "height": config.viewport_height},
        "locale": config.locale,
        "timezone_id": config.timezone_id,
        "extra_http_headers": {"Accept-Language": config.accept_language},
        "user_agent": config.user_agents[0],
        "args": config.browser_args,
    }
    if config.channel:
        kwargs["channel"] = config.channel
    proxy = playwright_proxy_from_capsolver_proxy(config.capsolver_proxy) if config.capsolver_use_proxy_for_browser else {}
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


async def apply_stealth(page: Any, config: CrawlerConfig) -> None:
    if not config.stealth_enabled:
        return
    try:
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise MissingDependencyError(
            "playwright-stealth is required when browser.stealth_enabled is true. "
            "Run: python -m pip install -r requirements.txt"
        ) from exc
    await Stealth().apply_stealth_async(page)


async def wait_ready(page: Any, config: CrawlerConfig) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=config.page_load_timeout_ms)
    except PlaywrightTimeoutError:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=config.selector_timeout_ms)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(config.navigation_delay_ms)


async def wait_seconds(page: Any, seconds: float) -> None:
    await page.wait_for_timeout(max(0, int(seconds * 1000)))


async def page_verification_text(page: Any) -> tuple[str, str]:
    title = ""
    body = ""
    try:
        title = await page.title()
    except Exception:
        pass
    try:
        body = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        pass
    return title, body


async def wait_for_manual_challenge_clear(
    page: Any,
    url: str,
    settings: ReportSettings,
    status: int | None = None,
    anti_bot_events: list[AntiBotEvent] | None = None,
) -> None:
    for attempt in range(settings.manual_wait_seconds + 1):
        title, body = await page_verification_text(page)
        markers = challenge_markers(status if attempt == 0 else None, title, body)
        if not markers:
            if attempt > 0 and anti_bot_events is not None:
                anti_bot_events.append(
                    AntiBotEvent(
                        kind="challenge-cleared",
                        url=url,
                        detail="Human-verification markers cleared while the visible browser stayed open.",
                        attempt=attempt,
                    )
                )
            return
        if attempt == 0:
            print(
                f"Human verification visible at {url}: {', '.join(markers)}. "
                f"Waiting up to {settings.manual_wait_seconds} seconds for it to clear..."
            )
            if anti_bot_events is not None:
                anti_bot_events.append(
                    AntiBotEvent(
                        kind="challenge-detected",
                        url=url,
                        detail=", ".join(markers),
                        wait_seconds=settings.manual_wait_seconds,
                    )
                )
        await page.wait_for_timeout(1000)
    if anti_bot_events is not None:
        anti_bot_events.append(
            AntiBotEvent(
                kind="challenge-timeout",
                url=url,
                detail="Human-verification markers did not clear before the manual wait expired.",
                wait_seconds=settings.manual_wait_seconds,
            )
        )
    raise RuntimeError(f"Human verification page did not clear at {url}")


async def dismiss_obstacles(page: Any) -> None:
    for selector in [
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("I Accept")',
        'button:has-text("Continue")',
        "#onetrust-accept-btn-handler",
    ]:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=800):
                await button.click()
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def goto(
    page: Any,
    url: str,
    settings: ReportSettings,
    anti_bot_events: list[AntiBotEvent] | None = None,
) -> None:
    config = settings.config
    last_error: Exception | None = None
    for attempt in range(1, config.max_retries + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=config.page_load_timeout_ms)
            await wait_ready(page, config)
            await dismiss_obstacles(page)
            status = getattr(response, "status", None) if response else None
            title, body = await page_verification_text(page)

            if status == 429:
                wait = config.rate_limit_backoff_base_seconds * (2 ** (attempt - 1))
                print(f"429 Too Many Requests at {url}. Waiting {wait:.1f}s before retry {attempt}/{config.max_retries}.")
                if anti_bot_events is not None:
                    anti_bot_events.append(
                        AntiBotEvent(
                            kind="rate-limit",
                            url=url,
                            detail="HTTP 429 Too Many Requests; backed off instead of escalating to CAPTCHA solving.",
                            attempt=attempt,
                            wait_seconds=wait,
                        )
                    )
                if attempt < config.max_retries:
                    await wait_seconds(page, wait)
                    continue

            markers = challenge_markers(status, title, body)
            if markers:
                await wait_for_manual_challenge_clear(page, url, settings, status, anti_bot_events)
                await wait_ready(page, config)
                return

            if status and status >= 400:
                raise RuntimeError(f"HTTP status {status}")
            if anti_bot_events is not None:
                anti_bot_events.append(
                    AntiBotEvent(
                        kind="clean-navigation",
                        url=url,
                        detail=f"Loaded without challenge markers; status={status or 'none'}.",
                        attempt=attempt,
                    )
                )
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            wait = config.timeout_backoff_base_seconds * (2 ** (attempt - 1))
            print(f"Timeout loading {url}. Waiting {wait:.1f}s before retry {attempt}/{config.max_retries}.")
            if anti_bot_events is not None:
                anti_bot_events.append(
                    AntiBotEvent(
                        kind="timeout-backoff",
                        url=url,
                        detail="Playwright timeout; backed off and retried without spending a solve.",
                        attempt=attempt,
                        wait_seconds=wait,
                    )
                )
            if attempt < config.max_retries:
                await wait_seconds(page, wait)
        except Exception as exc:
            last_error = exc
            wait = config.retry_backoff_seconds * attempt
            print(f"Navigation attempt {attempt}/{config.max_retries} failed for {url}: {exc}")
            if anti_bot_events is not None:
                anti_bot_events.append(
                    AntiBotEvent(
                        kind="navigation-retry",
                        url=url,
                        detail=str(exc),
                        attempt=attempt,
                        wait_seconds=wait,
                    )
                )
            if attempt < config.max_retries:
                await wait_seconds(page, wait)
    raise RuntimeError(f"Could not navigate to {url}") from last_error


async def row_data(page: Any, config: CrawlerConfig) -> list[dict[str, str]]:
    selector = config.selector("child_links", ".nys-openleg-result-item-link")
    label_selector = config.selector("child_label", ".nys-openleg-result-item-name")
    description_selector = config.selector("child_description", ".nys-openleg-result-item-description")
    raw_rows = await page.locator(selector).evaluate_all(
        """
        (nodes, cfg) => nodes.map((link) => {
          const row = link.closest('.nys-openleg-result-item') || link.parentElement || link;
          const nameNode = row.querySelector(cfg.labelSelector);
          const descNode = row.querySelector(cfg.descriptionSelector);
          const url = new URL(link.href || link.getAttribute('href') || '', document.baseURI);
          return {
            url: url.href,
            path: url.pathname,
            name: (nameNode ? nameNode.textContent : link.textContent || '').trim().replace(/\\s+/g, ' '),
            description: (descNode ? descNode.textContent : '').trim().replace(/\\s+/g, ' '),
          };
        });
        """,
        {"labelSelector": label_selector, "descriptionSelector": description_selector},
    )

    rows: list[dict[str, str]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        url = clean_text(raw_row.get("url", ""))
        if not is_relevant_law_url(url, config.root_url):
            continue
        rows.append(
            {
                "url": url,
                "path": urlparse(url).path,
                "name": clean_text(raw_row.get("name", "")),
                "description": clean_text(raw_row.get("description", "")),
            }
        )
    return rows


async def title_data(page: Any, config: CrawlerConfig) -> dict[str, str]:
    selectors = {
        "headline": config.selector("title_headline", ".nys-openleg-result-title-headline"),
        "shortTitle": config.selector("title_short", ".nys-openleg-result-title-short"),
        "location": config.selector("title_location", ".nys-openleg-result-title-location"),
        "legalText": config.selector("legal_text", ".nys-openleg-result-text"),
    }
    return await page.evaluate(
        """
        (selectors) => {
          const text = (selector) => {
            const node = document.querySelector(selector);
            return node ? node.textContent.trim().replace(/\\s+/g, ' ') : '';
          };
          return {
            headline: text(selectors.headline),
            shortTitle: text(selectors.shortTitle),
            location: text(selectors.location),
            legalText: text(selectors.legalText),
          };
        }
        """,
        selectors,
    )


async def row_url_for_path(page: Any, path: str, settings: ReportSettings) -> str:
    url = await page.evaluate(
        """
        (cfg) => {
          const link = Array.from(document.querySelectorAll(cfg.childSelector))
            .find((node) => new URL(node.href || node.getAttribute('href') || '', document.baseURI).pathname === cfg.path);
          return link ? new URL(link.href || link.getAttribute('href') || '', document.baseURI).href : '';
        }
        """,
        {"childSelector": settings.config.selector("child_links", ".nys-openleg-result-item-link"), "path": path},
    )
    url = clean_text(url)
    if not url:
        raise RuntimeError(f"Could not find row link for {path}")
    return url


async def scroll_row(page: Any, path: str, settings: ReportSettings) -> None:
    await page.evaluate(
        """
        (cfg) => {
          const link = Array.from(document.querySelectorAll(cfg.childSelector))
            .find((node) => new URL(node.href || node.getAttribute('href') || '', document.baseURI).pathname === cfg.path);
          if (link) link.scrollIntoView({ block: 'center', inline: 'nearest' });
        }
        """,
        {"childSelector": settings.config.selector("child_links", ".nys-openleg-result-item-link"), "path": path},
    )
    await page.wait_for_timeout(350)


async def annotate(page: Any, settings: ReportSettings, items: list[dict[str, str]]) -> None:
    config = settings.config
    await page.evaluate(
        """
        (cfg) => {
          if (window.__nysEvidenceOverlay) {
            window.__nysEvidenceOverlay.remove();
          }
          const overlay = document.createElement('div');
          overlay.id = '__nysEvidenceOverlay';
          overlay.style.position = 'fixed';
          overlay.style.inset = '0';
          overlay.style.pointerEvents = 'none';
          overlay.style.zIndex = '2147483647';
          overlay.style.fontFamily = 'Arial, sans-serif';
          document.body.appendChild(overlay);
          window.__nysEvidenceOverlay = overlay;

          const colors = {
            click: '#e11d48',
            header: '#2563eb',
            content: '#7c3aed',
            note: '#f97316',
          };

          const addBox = (node, label, kind) => {
            if (!node) return;
            const rect = node.getBoundingClientRect();
            if (rect.width < 2 || rect.height < 2) return;
            const color = colors[kind] || colors.note;
            const box = document.createElement('div');
            box.style.position = 'fixed';
            box.style.left = `${Math.max(0, rect.left - 4)}px`;
            box.style.top = `${Math.max(0, rect.top - 4)}px`;
            box.style.width = `${rect.width + 8}px`;
            box.style.height = `${rect.height + 8}px`;
            box.style.border = `4px solid ${color}`;
            box.style.borderRadius = '6px';
            box.style.boxSizing = 'border-box';
            box.style.background = kind === 'content' ? 'rgba(124,58,237,0.12)' : 'rgba(255,255,255,0.08)';
            box.style.boxShadow = '0 0 0 2px rgba(255,255,255,0.85)';

            const tag = document.createElement('div');
            tag.textContent = label;
            tag.style.position = 'absolute';
            tag.style.left = kind === 'click' || kind === 'content' ? '4px' : '-4px';
            tag.style.top = kind === 'click' || kind === 'content' ? '4px' : '-34px';
            tag.style.maxWidth = 'min(620px, calc(100vw - 28px))';
            tag.style.whiteSpace = 'normal';
            tag.style.overflowWrap = 'anywhere';
            tag.style.background = color;
            tag.style.color = 'white';
            tag.style.fontSize = '15px';
            tag.style.fontWeight = '700';
            tag.style.lineHeight = '1.15';
            tag.style.padding = '7px 9px';
            tag.style.borderRadius = '5px 5px 0 0';
            box.appendChild(tag);
            overlay.appendChild(box);
          };

          const findRowLink = (path) => Array.from(document.querySelectorAll(cfg.childSelector))
            .find((node) => new URL(node.href || node.getAttribute('href') || '', document.baseURI).pathname === path);

          for (const item of cfg.items) {
            if (item.type === 'row') {
              const link = findRowLink(item.path);
              const row = link ? (link.closest('.nys-openleg-result-item') || link) : null;
              addBox(row || link, item.clickLabel || 'CLICK this row', 'click');
            }
            if (item.type === 'header') {
              addBox(document.querySelector(cfg.titleHeadlineSelector), item.headlineLabel || 'CAPTURE page headline', 'header');
              addBox(document.querySelector(cfg.titleShortSelector), item.shortLabel || 'CAPTURE page short title', 'header');
            }
            if (item.type === 'legalText') {
              addBox(document.querySelector(cfg.legalTextSelector), item.label || 'CAPTURE legal text contents', 'content');
            }
          }
        }
        """,
        {
            "items": items,
            "childSelector": config.selector("child_links", ".nys-openleg-result-item-link"),
            "titleHeadlineSelector": config.selector("title_headline", ".nys-openleg-result-title-headline"),
            "titleShortSelector": config.selector("title_short", ".nys-openleg-result-title-short"),
            "legalTextSelector": config.selector("legal_text", ".nys-openleg-result-text"),
        },
    )
    await page.wait_for_timeout(250)


async def clear_annotations(page: Any) -> None:
    await page.evaluate(
        """
        () => {
          if (window.__nysEvidenceOverlay) {
            window.__nysEvidenceOverlay.remove();
            window.__nysEvidenceOverlay = null;
          }
        }
        """
    )


async def screenshot(page: Any, settings: ReportSettings, name: str) -> str:
    path = settings.shot_dir / name
    await page.screenshot(path=str(path), full_page=False)
    await clear_annotations(page)
    return f"screenshots/{name}"


async def click_row(
    page: Any,
    path: str,
    settings: ReportSettings,
    anti_bot_events: list[AntiBotEvent] | None = None,
) -> None:
    config = settings.config
    target_url = await row_url_for_path(page, path, settings)
    await page.evaluate(
        """
        (cfg) => {
          const link = Array.from(document.querySelectorAll(cfg.childSelector))
            .find((node) => new URL(node.href || node.getAttribute('href') || '', document.baseURI).pathname === cfg.path);
          if (!link) throw new Error(`Could not find row link for ${cfg.path}`);
          link.click();
        }
        """,
        {"childSelector": config.selector("child_links", ".nys-openleg-result-item-link"), "path": path},
    )
    try:
        await page.wait_for_url(target_url, timeout=config.page_load_timeout_ms)
    except PlaywrightTimeoutError:
        if clean_text(page.url).rstrip("/") != target_url.rstrip("/"):
            await goto(page, target_url, settings, anti_bot_events)
            return
    await wait_ready(page, config)
    await dismiss_obstacles(page)
    await wait_for_manual_challenge_clear(page, page.url, settings, anti_bot_events=anti_bot_events)


def img_block(src: str, alt: str) -> str:
    if not src:
        return f'<figure class="missing"><figcaption>{html.escape(alt)} not available</figcaption></figure>'
    return f'<figure><img src="{html.escape(src)}" alt="{html.escape(alt)}"><figcaption>{html.escape(alt)}</figcaption></figure>'


def text_value(*parts: str) -> str:
    return " ".join(clean_text(part) for part in parts if clean_text(part)).strip()


def first_row(rows: list[dict[str, str]], path: str, label: str) -> dict[str, str]:
    for row in rows:
        if row["path"] == path:
            return row
    available = ", ".join(row["path"] for row in rows[:12]) or "none"
    raise RuntimeError(f"Could not find {label} at {path}. Available paths: {available}")


def report_config_facts(settings: ReportSettings) -> str:
    config = settings.config
    proxy = "enabled" if (config.capsolver_use_proxy_for_browser and config.capsolver_proxy) else "none"
    facts = [
        ("Config", str(settings.config_path)),
        ("Browser profile", str(settings.profile_dir)),
        ("Root URL", config.root_url),
        ("Law path", settings.law_path),
        ("Article count", str(settings.article_count)),
        ("Headless", str(config.headless)),
        ("Channel", config.channel or "default"),
        ("Viewport", f"{config.viewport_width}x{config.viewport_height}"),
        ("User agent", config.user_agents[0]),
        ("Proxy", proxy),
        ("Child selector", config.selector("child_links", ".nys-openleg-result-item-link")),
        ("Header selectors", f"{config.selector('title_headline', '.nys-openleg-result-title-headline')} + {config.selector('title_short', '.nys-openleg-result-title-short')}"),
        ("Content selector", config.selector("legal_text", ".nys-openleg-result-text")),
    ]
    return "".join(
        f"<div><b>{html.escape(name)}</b><br>{html.escape(value)}</div>"
        for name, value in facts
    )


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def enabled_disabled(value: bool) -> str:
    return "enabled" if value else "disabled"


def masked_proxy(proxy: str) -> str:
    text = clean_text(proxy)
    if not text:
        return "not configured"

    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        if parsed.username:
            host = f"***:***@{host}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))

    parts = text.split(":")
    if len(parts) >= 4 and parts[0].lower() in {"http", "https", "socks4", "socks5"}:
        return f"{parts[0]}:{parts[1]}:{parts[2]}:***:***"
    if len(parts) >= 4:
        return f"{parts[0]}:{parts[1]}:***:***"
    return text


def anti_bot_config_facts(settings: ReportSettings) -> str:
    config = settings.config
    status_markers = ", ".join(str(status) for status in sorted(CHALLENGE_STATUS_CODES))
    title_markers = ", ".join(CHALLENGE_TITLE_MARKERS)
    body_markers = ", ".join(CHALLENGE_BODY_MARKERS)
    facts = [
        ("CapSolver key", "configured" if config.capsolver_api_key else "not configured"),
        ("CapSolver preferred", yes_no(config.capsolver_preferred)),
        ("CapSolver API", config.capsolver_api_base),
        ("Balance check on start", enabled_disabled(config.capsolver_log_balance_on_start)),
        ("Solver proxy", masked_proxy(config.capsolver_proxy)),
        ("Browser uses solver proxy", yes_no(config.capsolver_use_proxy_for_browser and bool(config.capsolver_proxy))),
        ("Challenge interaction fallback", enabled_disabled(config.challenge_interaction_enabled)),
        ("Manual evidence wait", f"{settings.manual_wait_seconds}s"),
        ("Rate-limit backoff", f"{config.rate_limit_backoff_base_seconds:g}s exponential base"),
        ("Timeout backoff", f"{config.timeout_backoff_base_seconds:g}s exponential base"),
        ("Challenge HTTP statuses", status_markers),
        ("Title challenge markers", title_markers),
        ("Body challenge markers", body_markers),
    ]
    return "".join(
        f"<div><b>{html.escape(name)}</b><br>{html.escape(value)}</div>"
        for name, value in facts
    )


def anti_bot_policy_html(settings: ReportSettings) -> str:
    solve_mode = "CapSolver first, then browser fallback" if settings.config.capsolver_preferred else "browser fallback first, then CapSolver"
    steps = [
        ("Normal page", "Continue crawling. No paid solve."),
        ("HTTP 429", "Back off using the configured rate-limit delay. No paid solve."),
        ("Timeout", "Back off using the timeout delay and retry. No paid solve."),
        ("Challenge fingerprints", f"Detect status/text markers. Normal crawler mode: {solve_mode}."),
        ("Turnstile", "Use a proxyless Turnstile task, inject token fields, then reload or submit."),
        ("Cloudflare", "Require a static or sticky proxy and keep browser IP aligned with solver IP."),
        ("Evidence report", "Keep Chrome visible and wait for manual clearance, so generating evidence does not spend solves."),
        ("Failure", "Preserve screenshots/HTML/debug context and keep checkpoints intact."),
    ]
    return "".join(
        f"""
        <li>
          <b>{html.escape(label)}</b>
          <span>{html.escape(detail)}</span>
        </li>
        """
        for label, detail in steps
    )


def anti_bot_event_summary(events: list[AntiBotEvent]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    if not counts:
        return '<span class="event-chip">no events recorded</span>'
    return "".join(
        f'<span class="event-chip">{html.escape(kind)}: {count}</span>'
        for kind, count in sorted(counts.items())
    )


def anti_bot_events_html(events: list[AntiBotEvent]) -> str:
    if not events:
        return "<p>No anti-bot events were recorded during this evidence run.</p>"

    rows = []
    for event in events[-40:]:
        wait = f"{event.wait_seconds:g}s" if event.wait_seconds else ""
        attempt = str(event.attempt) if event.attempt else ""
        rows.append(
            f"""
            <tr>
              <td><span class="event-kind">{html.escape(event.kind)}</span></td>
              <td>{html.escape(attempt)}</td>
              <td>{html.escape(wait)}</td>
              <td><a href="{html.escape(event.url)}">{html.escape(event.url)}</a></td>
              <td>{html.escape(event.detail)}</td>
            </tr>
            """
        )
    if len(events) > 40:
        rows.insert(
            0,
            f"""
            <tr>
              <td colspan="5">Showing the latest 40 of {len(events)} recorded anti-bot events.</td>
            </tr>
            """,
        )

    return f"""
    <div class="event-summary">{anti_bot_event_summary(events)}</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Signal</th>
            <th>Try</th>
            <th>Wait</th>
            <th>URL</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </div>
    """


def anti_bot_section_html(settings: ReportSettings, events: list[AntiBotEvent]) -> str:
    return f"""
    <section class="anti-bot">
      <h2>CAPTCHA and Anti-bot Evidence</h2>
      <p>
        This final section shows how the current crawler configuration handles rate limits, slow pages,
        human-verification screens, Turnstile, and Cloudflare-style challenges.
      </p>
      <div class="facts anti-facts">
        {anti_bot_config_facts(settings)}
      </div>
      <ol class="anti-flow">
        {anti_bot_policy_html(settings)}
      </ol>
      <h3>Signals Seen During This Report Run</h3>
      {anti_bot_events_html(events)}
    </section>
    """


async def capture_report(settings: ReportSettings) -> dict[str, Any]:
    settings.shot_dir.mkdir(parents=True, exist_ok=True)
    observations: list[dict[str, Any]] = []
    anti_bot_events: list[AntiBotEvent] = []
    if settings.config.stealth_enabled:
        anti_bot_events.append(
            AntiBotEvent(
                kind="stealth-enabled",
                url=settings.config.root_url,
                detail="playwright-stealth is applied to the evidence page before navigation.",
            )
        )
    if settings.config.capsolver_use_proxy_for_browser and settings.config.capsolver_proxy:
        anti_bot_events.append(
            AntiBotEvent(
                kind="proxy-aligned",
                url=settings.config.root_url,
                detail="Browser proxy is configured from captcha.capsolver_proxy so clearance cookies match the browser IP.",
            )
        )

    async with async_playwright() as p:
        print(f"Launching visible evidence browser with profile: {settings.profile_dir}")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(settings.profile_dir),
            **browser_context_kwargs(settings),
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await apply_stealth(page, settings.config)

        await goto(page, settings.config.root_url, settings, anti_bot_events)
        await scroll_row(page, settings.law_path, settings)
        root_rows = await row_data(page, settings.config)
        law_row = first_row(root_rows, settings.law_path, "law row")
        await annotate(
            page,
            settings,
            [
                {
                    "type": "row",
                    "path": settings.law_path,
                    "clickLabel": "CLICK row to enter level10",
                }
            ],
        )
        root_shot = await screenshot(page, settings, "00_root_click_law_enter_level10.png")

        await goto(page, law_row["url"], settings, anti_bot_events)
        law_title = await title_data(page, settings.config)
        await annotate(
            page,
            settings,
            [
                {
                    "type": "header",
                    "headlineLabel": "CAPTURE level10 from destination headline",
                    "shortLabel": "CAPTURE level10 from destination short title",
                }
            ],
        )
        law_header_shot = await screenshot(page, settings, "00b_law_destination_capture_level10.png")
        article_rows = [
            row
            for row in await row_data(page, settings.config)
            if row["path"].startswith(settings.article_path_prefix)
        ][: settings.article_count]
        if not article_rows:
            raise RuntimeError(f"No article rows found under {settings.article_path_prefix}")
        if len(article_rows) < settings.article_count:
            print(f"Only found {len(article_rows)} article rows; report requested {settings.article_count}.")

        for idx, article in enumerate(article_rows, 1):
            await goto(page, law_row["url"], settings, anti_bot_events)
            await scroll_row(page, article["path"], settings)
            await annotate(
                page,
                settings,
                [
                    {
                        "type": "header",
                        "headlineLabel": "CAPTURE level10 from destination headline",
                        "shortLabel": "CAPTURE level10 from destination short title",
                    },
                    {
                        "type": "row",
                        "path": article["path"],
                        "clickLabel": f"CLICK article {idx} row to enter level20",
                    },
                ],
            )
            article_row_shot = await screenshot(page, settings, f"{idx:02d}_law_click_article_enter_level20.png")
            await click_row(page, article["path"], settings, anti_bot_events)
            article_url = page.url
            article_title = await title_data(page, settings.config)
            section_rows = [
                row
                for row in await row_data(page, settings.config)
                if row["path"].startswith(settings.child_path_prefix)
                and not row["path"].startswith(settings.article_path_prefix)
            ]
            first_section = section_rows[0] if section_rows else None

            if first_section:
                await scroll_row(page, first_section["path"], settings)
                await annotate(
                    page,
                    settings,
                    [
                        {
                            "type": "header",
                            "headlineLabel": "CAPTURE level20 from destination headline",
                            "shortLabel": "CAPTURE level20 from destination short title",
                        },
                        {
                            "type": "row",
                            "path": first_section["path"],
                            "clickLabel": "CLICK first section row to enter level30",
                        },
                    ],
                )
                section_row_shot = await screenshot(page, settings, f"{idx:02d}_article_click_section_enter_level30.png")
                await click_row(page, first_section["path"], settings, anti_bot_events)
                section_url = page.url
                section_title = await title_data(page, settings.config)
                await annotate(
                    page,
                    settings,
                    [
                        {
                            "type": "header",
                            "headlineLabel": "CAPTURE level30 from destination headline",
                            "shortLabel": "CAPTURE level30 from destination short title",
                        },
                        {
                            "type": "legalText",
                            "label": "CAPTURE contents from .nys-openleg-result-text",
                        },
                    ],
                )
                legal_text_shot = await screenshot(page, settings, f"{idx:02d}_leaf_capture_legal_text.png")
            else:
                section_row_shot = ""
                section_url = ""
                section_title = {}
                legal_text_shot = ""

            observations.append(
                {
                    "articleIndex": idx,
                    "articleRow": article,
                    "articleRowLevel": text_value(article["name"], article["description"]),
                    "articleUrl": article_url,
                    "articleHeaderLevel": compose_level_title(
                        article_title.get("headline", ""),
                        article_title.get("shortTitle", ""),
                    ),
                    "firstSection": first_section,
                    "sectionRowLevel": text_value(
                        first_section["name"] if first_section else "",
                        first_section["description"] if first_section else "",
                    ),
                    "sectionUrl": section_url,
                    "sectionHeaderLevel": compose_level_title(
                        section_title.get("headline", "") if section_title else "",
                        section_title.get("shortTitle", "") if section_title else "",
                    ),
                    "legalTextPreview": (section_title.get("legalText", "") if section_title else "")[:360],
                    "shots": {
                        "articleRow": article_row_shot,
                        "sectionRow": section_row_shot,
                        "legalText": legal_text_shot,
                    },
                }
            )

        await context.close()

    return {
        "lawRow": law_row,
        "lawTitle": law_title,
        "rootShot": root_shot,
        "lawHeaderShot": law_header_shot,
        "observations": observations,
        "antiBotEvents": anti_bot_events,
    }


def write_report(settings: ReportSettings, capture: Mapping[str, Any]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    law_row = capture["lawRow"]
    law_title = capture["lawTitle"]
    anti_bot_events = list(capture.get("antiBotEvents", []))
    root_navigation_text = text_value(law_row["name"], law_row["description"])
    current_level10 = compose_level_title(law_title.get("headline", ""), law_title.get("shortTitle", ""))

    sections_html = []
    for obs in capture["observations"]:
        article = obs["articleRow"]
        first_section = obs["firstSection"] or {"url": "", "name": "", "description": ""}
        sections_html.append(
            f"""
            <section class="article">
              <h2>Article {obs['articleIndex']}: {html.escape(obs['articleHeaderLevel'])}</h2>
              <div class="facts">
                <div><b>Article row clicked to enter level20</b><br><a href="{html.escape(article['url'])}">{html.escape(article['url'])}</a></div>
                <div><b>Article row text is navigation only</b><br>{html.escape(obs['articleRowLevel'])}</div>
                <div><b>Captured level20 after click</b><br>{html.escape(obs['articleHeaderLevel'])}</div>
                <div><b>Section row clicked to enter level30</b><br><a href="{html.escape(first_section['url'])}">{html.escape(first_section['url'])}</a></div>
                <div><b>Section row text is navigation only</b><br>{html.escape(obs['sectionRowLevel'])}</div>
                <div><b>Captured level30 after click</b><br>{html.escape(obs['sectionHeaderLevel'])}</div>
              </div>
              <div class="grid">
                {img_block(obs['shots']['articleRow'], 'Law page: capture level10 from header and click article row to enter level20')}
                {img_block(obs['shots']['sectionRow'], 'Article page: capture level20 from header and click section row to enter level30')}
                {img_block(obs['shots']['legalText'], 'Leaf section page: capture level30 from header and final legal text contents')}
              </div>
              <p class="preview"><b>Legal text preview:</b> {html.escape(obs['legalTextPreview'])}</p>
            </section>
            """
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NY Senate Click and Capture Evidence</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18212f;
      --muted: #667085;
      --line: #d6dbe3;
      --paper: #f6f7f9;
      --click: #e11d48;
      --header: #2563eb;
      --content: #7c3aed;
    }}
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      color: var(--ink);
      background: white;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 24px 56px;
    }}
    h1 {{
      font-size: 30px;
      margin: 0 0 8px;
      letter-spacing: 0;
    }}
    h2 {{
      font-size: 22px;
      margin: 0 0 16px;
      letter-spacing: 0;
    }}
    h3 {{
      font-size: 17px;
      margin: 24px 0 12px;
      letter-spacing: 0;
    }}
    p {{
      line-height: 1.5;
    }}
    .meta {{
      color: var(--muted);
      margin: 0 0 24px;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      margin: 20px 0 26px;
      padding: 14px 16px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
    }}
    .swatch {{
      width: 18px;
      height: 18px;
      border-radius: 4px;
      display: inline-block;
    }}
    .click {{ background: var(--click); }}
    .header {{ background: var(--header); }}
    .content {{ background: var(--content); }}
    section {{
      border-top: 1px solid var(--line);
      padding-top: 28px;
      margin-top: 30px;
    }}
    .facts {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin: 14px 0 22px;
    }}
    .facts div {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
      overflow-wrap: anywhere;
    }}
    .run-facts {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .anti-facts {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .anti-flow {{
      list-style: none;
      counter-reset: flow;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      padding: 0;
      margin: 18px 0 10px;
    }}
    .anti-flow li {{
      counter-increment: flow;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 12px 12px 46px;
      background: #fff;
      position: relative;
      min-height: 74px;
    }}
    .anti-flow li::before {{
      content: counter(flow);
      position: absolute;
      left: 12px;
      top: 12px;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      background: var(--header);
      color: white;
      display: grid;
      place-items: center;
      font-size: 13px;
      font-weight: 700;
    }}
    .anti-flow span {{
      display: block;
      color: var(--muted);
      margin-top: 5px;
      line-height: 1.4;
    }}
    .event-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 8px 0 12px;
    }}
    .event-chip {{
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      background: var(--paper);
      font-size: 13px;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
    }}
    table {{
      width: 100%;
      min-width: 860px;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th,
    td {{
      text-align: left;
      vertical-align: top;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }}
    th {{
      color: var(--muted);
      background: var(--paper);
      font-weight: 700;
    }}
    tr:last-child td {{
      border-bottom: 0;
    }}
    .event-kind {{
      display: inline-block;
      border-radius: 5px;
      padding: 4px 7px;
      color: white;
      background: var(--content);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 22px;
    }}
    figure {{
      margin: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: var(--paper);
    }}
    img {{
      display: block;
      width: 100%;
      height: auto;
      background: white;
    }}
    figcaption {{
      padding: 10px 12px;
      font-size: 14px;
      color: var(--muted);
      border-top: 1px solid var(--line);
    }}
    .missing {{
      min-height: 72px;
      display: flex;
      align-items: center;
    }}
    .preview {{
      background: var(--paper);
      border-left: 4px solid var(--content);
      padding: 12px 14px;
      margin: 16px 0 0;
    }}
    a {{
      color: #075985;
    }}
    @media (max-width: 900px) {{
      .run-facts {{ grid-template-columns: 1fr; }}
      .anti-facts {{ grid-template-columns: 1fr; }}
      .anti-flow {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 760px) {{
      main {{ padding: 24px 14px 40px; }}
      .facts {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>NY Senate Click and Capture Evidence</h1>
    <p class="meta">Fresh live screenshots generated {html.escape(now)} from <a href="{html.escape(settings.config.root_url)}">{html.escape(settings.config.root_url)}</a>.</p>
    <div class="legend">
      <span><i class="swatch click"></i>Red: click target</span>
      <span><i class="swatch header"></i>Blue: destination page header captured as level</span>
      <span><i class="swatch content"></i>Purple: final legal text contents</span>
    </div>

    <section>
      <h2>Run settings</h2>
      <div class="facts run-facts">
        {report_config_facts(settings)}
      </div>
    </section>

    <section>
      <h2>Root law selection</h2>
      <div class="facts">
        <div><b>Law row clicked to enter level10</b><br><a href="{html.escape(law_row['url'])}">{html.escape(law_row['url'])}</a></div>
        <div><b>Root row text is navigation only</b><br>{html.escape(root_navigation_text)}</div>
        <div><b>Captured level10 after click</b><br>{html.escape(current_level10)}</div>
      </div>
      <div class="grid">
        {img_block(capture['rootShot'], 'Root page: click law row to enter level10; do not capture row text as level')}
        {img_block(capture['lawHeaderShot'], 'Law destination page: capture level10 from page header')}
      </div>
    </section>

    {''.join(sections_html)}

    {anti_bot_section_html(settings, anti_bot_events)}
  </main>
</body>
</html>
"""
    (settings.report_dir / "index.html").write_text(document, encoding="utf-8")


async def async_main(argv: list[str] | None = None) -> Path:
    settings = build_settings(parse_args(argv))
    print(f"Writing report under: {settings.report_dir}")
    capture = await capture_report(settings)
    write_report(settings, capture)
    return settings.report_dir.resolve()


def main(argv: list[str] | None = None) -> int:
    report_dir = asyncio.run(async_main(argv))
    print(report_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
