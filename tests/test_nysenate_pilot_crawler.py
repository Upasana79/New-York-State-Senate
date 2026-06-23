from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nysenate_crawler.pilot_crawler import (  # noqa: E402
    BrowserIdentity,
    CrawlerConfig,
    LEVEL_KEYS,
    LinkCandidate,
    LevelOverflowError,
    NYSenatePilotCrawler,
    XMLDocumentStore,
    XMLDocumentStoreError,
    assign_level,
    build_record,
    canonical_url,
    classify_page,
    compose_level_title,
    is_relevant_law_url,
    limit_root_title_links,
    load_capsolver_api_key,
    source_url_key,
    validate_record,
)


class LevelAssignmentTests(unittest.TestCase):
    def test_composes_level_title_from_headline_and_short_title(self) -> None:
        title = compose_level_title("ARTICLE 1", "Short Title; Policy of State and Purpose of Chapter: Definitions")

        self.assertEqual(title, "ARTICLE 1 Short Title; Policy of State and Purpose of Chapter: Definitions")

    def test_level_title_uses_only_headline_and_short_title(self) -> None:
        location_context = "Alcoholic Beverage Control (ABC) CHAPTER 3-B, ARTICLE 1"

        title = compose_level_title("SECTION 1", "Short title")

        self.assertEqual(title, "SECTION 1 Short title")
        self.assertNotIn(location_context, title)
        self.assertNotIn("CHAPTER 3-B", title)

    def test_assigns_page_title_by_depth_and_carries_previous_levels(self) -> None:
        level10 = assign_level({}, 0, "CHAPTER 3-B Alcoholic Beverage Control")
        level20 = assign_level(
            level10,
            1,
            "ARTICLE 1 Short Title; Policy of State and Purpose of Chapter: Definitions",
        )

        self.assertEqual(level20["level10"], "CHAPTER 3-B Alcoholic Beverage Control")
        self.assertEqual(level20["level20"], "ARTICLE 1 Short Title; Policy of State and Purpose of Chapter: Definitions")
        self.assertEqual(level20["level30"], "")

    def test_depth_beyond_level100_is_flagged(self) -> None:
        with self.assertRaises(LevelOverflowError):
            assign_level({}, len(LEVEL_KEYS), "TOO DEEP")


class PageDecisionTests(unittest.TestCase):
    def test_child_links_beat_leaf_detection(self) -> None:
        children = [LinkCandidate(url="https://www.nysenate.gov/legislation/laws/ABC/A1")]

        self.assertEqual(classify_page(children, "Legal text is present."), "navigation")

    def test_leaf_requires_no_children_and_contents(self) -> None:
        self.assertEqual(classify_page([], "Legal text is present."), "leaf")
        self.assertEqual(classify_page([], ""), "empty")


class DestinationHeaderLevelTests(unittest.TestCase):
    def test_crawl_levels_come_from_destination_headers_not_navigation_rows(self) -> None:
        root_url = "https://www.nysenate.gov/legislation/laws/CONSOLIDATED"
        law_url = "https://www.nysenate.gov/legislation/laws/ABC"
        article_url = "https://www.nysenate.gov/legislation/laws/ABC/A1"
        section_url = "https://www.nysenate.gov/legislation/laws/ABC/1"
        root_link = LinkCandidate(
            url=law_url,
            label="ABC",
            description="ROOT ROW ONLY - Alcoholic Beverage Control",
        )

        class FakePage:
            url = ""

            async def wait_for_timeout(self, timeout_ms: int) -> None:
                return None

        class HeaderOnlyCrawler(NYSenatePilotCrawler):
            titles = {
                law_url: "CHAPTER 3-B Destination Law Header",
                article_url: "ARTICLE 1 Destination Article Header",
                section_url: "SECTION 1 Destination Section Header",
            }
            children = {
                law_url: [
                    LinkCandidate(
                        url=article_url,
                        label="ARTICLE 1",
                        description="ROW ONLY - Article title must not be captured",
                    )
                ],
                article_url: [
                    LinkCandidate(
                        url=section_url,
                        label="SECTION 1",
                        description="ROW ONLY - Section title must not be captured",
                    )
                ],
                section_url: [],
            }

            async def goto_with_retry(self, page: FakePage, url: str) -> None:
                page.url = canonical_url(url)

            async def dismiss_obstacles(self, page: FakePage) -> None:
                return None

            async def extract_current_title(self, page: FakePage) -> str:
                return self.titles[page.url]

            async def extract_child_links(self, page: FakePage) -> list[LinkCandidate]:
                return self.children[page.url]

            async def extract_legal_text(self, page: FakePage) -> str:
                if page.url == section_url:
                    return "This is destination legal text from the leaf page only."
                return ""

            async def extract_revision_metadata(self, page: FakePage) -> str:
                return "Viewing most recent revision (from 2014-09-22)" if page.url == section_url else ""

            async def save_debug(self, page: FakePage, reason: str) -> None:
                raise AssertionError(f"Unexpected debug save: {reason}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = CrawlerConfig(
                root_url=root_url,
                max_pages=10,
                max_documents=10,
                output_xml=tmp_path / "pilot.xml",
                log_file=tmp_path / "crawler.log",
                visited_file=tmp_path / "visited.txt",
                failed_file=tmp_path / "failed.txt",
                debug_dir=tmp_path / "debug",
                user_data_dir=tmp_path / "profile",
                min_contents_chars=20,
            )
            crawler = HeaderOnlyCrawler(config)
            crawler.prepare_runtime_paths()
            crawler.load_checkpoint()
            crawler.store.initialize()

            asyncio.run(crawler.crawl_page(FakePage(), root_link.url, {}, depth=0))

            document = ET.parse(config.output_xml).getroot().find("./documents/document")
            self.assertIsNotNone(document)
            self.assertEqual(document.findtext("level10"), "CHAPTER 3-B Destination Law Header")
            self.assertEqual(document.findtext("level20"), "ARTICLE 1 Destination Article Header")
            self.assertEqual(document.findtext("level30"), "SECTION 1 Destination Section Header")
            self.assertIn("destination legal text", document.findtext("contents") or "")

            xml_text = config.output_xml.read_text(encoding="utf-8")
            self.assertNotIn("ROOT ROW ONLY", xml_text)
            self.assertNotIn("ROW ONLY", xml_text)


class RootTitleLimitTests(unittest.TestCase):
    def test_null_pilot_title_limit_keeps_all_root_titles(self) -> None:
        links = [
            LinkCandidate(url="https://www.nysenate.gov/legislation/laws/ABC", label="ABC"),
            LinkCandidate(url="https://www.nysenate.gov/legislation/laws/AGM", label="AGM"),
            LinkCandidate(url="https://www.nysenate.gov/legislation/laws/BSC", label="BSC"),
        ]
        config = CrawlerConfig.from_mapping({"targets": {"pilot_title_limit": None}})

        self.assertIsNone(config.pilot_title_limit)
        self.assertEqual(limit_root_title_links(links, config.pilot_title_limit), links)

    def test_one_title_behavior_still_works_with_limit_one(self) -> None:
        links = [
            LinkCandidate(url="https://www.nysenate.gov/legislation/laws/ABC", label="ABC"),
            LinkCandidate(url="https://www.nysenate.gov/legislation/laws/AGM", label="AGM"),
        ]
        config = CrawlerConfig.from_mapping({"targets": {"pilot_title_limit": 1}})

        self.assertEqual(limit_root_title_links(links, config.pilot_title_limit), links[:1])


class ConfigOverrideTests(unittest.TestCase):
    def test_with_overrides_changes_only_explicit_values(self) -> None:
        config = CrawlerConfig.from_mapping(
            {
                "browser": {"headless": False},
                "targets": {"max_pages": 25, "max_documents": 10},
            }
        )

        overridden = config.with_overrides(headless=True, max_pages=5)

        self.assertTrue(overridden.headless)
        self.assertEqual(overridden.max_pages, 5)
        self.assertEqual(overridden.max_documents, 10)
        self.assertFalse(config.headless)
        self.assertEqual(config.max_pages, 25)

    def test_browser_args_include_fingerprint_defaults(self) -> None:
        config = CrawlerConfig.from_mapping(
            {
                "browser": {
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
            }
        )

        self.assertIn("--disable-blink-features=AutomationControlled", config.browser_args)
        self.assertIn("--start-maximized", config.browser_args)
        self.assertTrue(config.stealth_enabled)
        self.assertTrue(config.rotate_user_agent_per_title)
        self.assertGreaterEqual(len(config.user_agents), 1)

    def test_session_profile_uses_target_under_sessions_dir(self) -> None:
        config = CrawlerConfig.from_mapping(
            {
                "browser": {
                    "session_dir": "data/sessions",
                    "session_target": "NY Senate",
                }
            }
        )

        self.assertEqual(config.user_data_dir, Path("data/sessions/ny_senate_profile"))

    def test_user_data_dir_remains_explicit_profile_override(self) -> None:
        config = CrawlerConfig.from_mapping(
            {
                "browser": {
                    "session_dir": "data/sessions",
                    "session_target": "NY Senate",
                    "user_data_dir": "data/custom_profile",
                }
            }
        )

        self.assertEqual(config.user_data_dir, Path("data/custom_profile"))


class BrowserIdentityPoolTests(unittest.TestCase):
    def test_browser_identity_pool_loops_user_agents_with_separate_profiles_and_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "sessions"
            config = CrawlerConfig(
                root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED",
                session_dir=session_dir,
                session_target="NY Senate",
                user_agents=["UA-1", "UA-2"],
            )
            crawler = NYSenatePilotCrawler(config)

            first = crawler.next_browser_identity()
            second = crawler.next_browser_identity()
            third = crawler.next_browser_identity()

            self.assertEqual(first.user_agent, "UA-1")
            self.assertEqual(second.user_agent, "UA-2")
            self.assertEqual(third.user_agent, "UA-1")
            self.assertEqual(first.user_data_dir, session_dir / "ny_senate_ua01_profile")
            self.assertEqual(second.user_data_dir, session_dir / "ny_senate_ua02_profile")
            self.assertTrue(first.user_data_dir.exists())
            self.assertTrue(second.user_data_dir.exists())

            first_meta = json.loads(first.meta_file.read_text(encoding="utf-8"))
            second_meta = json.loads(second.meta_file.read_text(encoding="utf-8"))
            cursor = json.loads((session_dir / "ny_senate_user_agent_cursor.json").read_text(encoding="utf-8"))

            self.assertEqual(first_meta["user_agent"], "UA-1")
            self.assertEqual(second_meta["user_agent"], "UA-2")
            self.assertEqual(cursor["next_index"], 1)


class BrowserLaunchTests(unittest.TestCase):
    def test_open_context_uses_real_chrome_visible_persistent_profile(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeChromium:
            async def launch_persistent_context(self, **kwargs: object) -> str:
                calls.append(kwargs)
                return "context"

        class FakePlaywright:
            chromium = FakeChromium()

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            config = CrawlerConfig(
                root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED",
                user_data_dir=profile,
                user_agents=["UA-TEST"],
                headless=False,
                channel="chrome",
                browser_args=["--disable-blink-features=AutomationControlled"],
            )
            crawler = NYSenatePilotCrawler(config)

            context = asyncio.run(crawler.open_context(FakePlaywright()))

        self.assertEqual(context, "context")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["user_data_dir"], str(profile))
        self.assertEqual(calls[0]["channel"], "chrome")
        self.assertFalse(calls[0]["headless"])
        self.assertEqual(calls[0]["args"], ["--disable-blink-features=AutomationControlled", "--start-maximized"])
        self.assertEqual(calls[0]["user_agent"], "UA-TEST")

    def test_open_context_uses_browser_identity_profile_and_user_agent(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeChromium:
            async def launch_persistent_context(self, **kwargs: object) -> str:
                calls.append(kwargs)
                return "context"

        class FakePlaywright:
            chromium = FakeChromium()

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            identity = BrowserIdentity(
                index=1,
                user_agent="UA-IDENTITY",
                user_data_dir=profile,
                meta_file=Path(tmp) / "meta.json",
            )
            config = CrawlerConfig(root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED")
            crawler = NYSenatePilotCrawler(config)

            context = asyncio.run(crawler.open_context(FakePlaywright(), identity))

        self.assertEqual(context, "context")
        self.assertEqual(calls[0]["user_data_dir"], str(profile))
        self.assertEqual(calls[0]["user_agent"], "UA-IDENTITY")

    def test_open_context_does_not_fallback_when_real_chrome_launch_fails(self) -> None:
        class FakeChromium:
            calls = 0

            async def launch_persistent_context(self, **kwargs: object) -> str:
                self.calls += 1
                raise RuntimeError("chrome missing")

        class FakePlaywright:
            chromium = FakeChromium()

        config = CrawlerConfig(
            root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED",
            headless=False,
            channel="chrome",
        )
        crawler = NYSenatePilotCrawler(config)

        with self.assertRaisesRegex(RuntimeError, "Could not launch real Chrome channel 'chrome'"):
            asyncio.run(crawler.open_context(FakePlaywright()))

        self.assertEqual(FakePlaywright.chromium.calls, 1)


class StealthTests(unittest.TestCase):
    def test_apply_stealth_can_be_disabled(self) -> None:
        config = CrawlerConfig(
            root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED",
            stealth_enabled=False,
        )
        crawler = NYSenatePilotCrawler(config)

        asyncio.run(crawler.apply_stealth(object()))

    def test_apply_stealth_uses_playwright_stealth_when_enabled(self) -> None:
        applied_pages: list[object] = []
        original_module = sys.modules.get("playwright_stealth")

        module = types.ModuleType("playwright_stealth")

        class FakeStealth:
            async def apply_stealth_async(self, page: object) -> None:
                applied_pages.append(page)

        module.Stealth = FakeStealth
        sys.modules["playwright_stealth"] = module

        try:
            config = CrawlerConfig(root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED")
            crawler = NYSenatePilotCrawler(config)
            page = object()

            asyncio.run(crawler.apply_stealth(page))

            self.assertEqual(applied_pages, [page])
        finally:
            if original_module is None:
                sys.modules.pop("playwright_stealth", None)
            else:
                sys.modules["playwright_stealth"] = original_module


class CapSolverKeyTests(unittest.TestCase):
    def test_capsolver_key_prefers_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "captcha-key.txt"
            key_file.write_text(" file-key ", encoding="utf-8")

            key = load_capsolver_api_key(key_file, {"CAPSOLVER_API_KEY": " env-key "})

            self.assertEqual(key, "env-key")

    def test_capsolver_key_falls_back_to_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "captcha-key.txt"
            key_file.write_text("\nfile-key\n", encoding="utf-8")

            key = load_capsolver_api_key(key_file, {})

            self.assertEqual(key, "file-key")

    def test_capsolver_key_is_empty_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = load_capsolver_api_key(Path(tmp) / "missing.txt", {})

            self.assertEqual(key, "")


class URLFilteringTests(unittest.TestCase):
    def test_canonical_url_trims_fragments_and_surrounding_whitespace(self) -> None:
        raw_url = "  https://www.nysenate.gov/legislation/laws/ABC/1/#section  "

        self.assertEqual(canonical_url(raw_url), "https://www.nysenate.gov/legislation/laws/ABC/1")
        self.assertEqual(source_url_key(raw_url), "https://www.nysenate.gov/legislation/laws/ABC/1")

    def test_filters_to_nysenate_law_hierarchy_and_excludes_root(self) -> None:
        root_url = "https://www.nysenate.gov/legislation/laws/CONSOLIDATED"

        self.assertTrue(is_relevant_law_url("https://www.nysenate.gov/legislation/laws/ABC", root_url))
        self.assertTrue(is_relevant_law_url("https://www.nysenate.gov/legislation/laws/ABC/A1", root_url))
        self.assertFalse(is_relevant_law_url(root_url, root_url))
        self.assertFalse(is_relevant_law_url("https://www.nysenate.gov/legislation/bills/2025/S1", root_url))
        self.assertFalse(is_relevant_law_url("https://example.com/legislation/laws/ABC", root_url))


class CheckpointTests(unittest.TestCase):
    def test_load_checkpoint_reads_nonblank_stripped_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            visited_file = Path(tmp) / "visited.txt"
            failed_file = Path(tmp) / "failed.txt"
            visited_file.write_text(" https://www.nysenate.gov/legislation/laws/ABC \n\n", encoding="utf-8")
            failed_file.write_text("\nhttps://www.nysenate.gov/legislation/laws/ABC/1\n", encoding="utf-8")
            config = CrawlerConfig(
                root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED",
                visited_file=visited_file,
                failed_file=failed_file,
            )
            crawler = NYSenatePilotCrawler(config)

            crawler.load_checkpoint()

            self.assertEqual(crawler.visited_urls, {"https://www.nysenate.gov/legislation/laws/ABC"})
            self.assertEqual(crawler.failed_urls, {"https://www.nysenate.gov/legislation/laws/ABC/1"})


class XMLStoreTests(unittest.TestCase):
    def test_incremental_xml_contains_required_fields_and_empty_deeper_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.xml"
            store = XMLDocumentStore(path, "https://www.nysenate.gov/legislation/laws/CONSOLIDATED")
            store.initialize()
            store.set_pilot_law("CHAPTER 3-B Alcoholic Beverage Control")

            levels = assign_level({}, 0, "CHAPTER 3-B Alcoholic Beverage Control")
            levels = assign_level(levels, 1, "ARTICLE 1 Short Title")
            levels = assign_level(levels, 2, "SECTION 1 Short title")
            store.append(
                build_record(
                    "https://www.nysenate.gov/legislation/laws/ABC/1",
                    levels,
                    "Viewing most recent revision (from 2014-09-22)",
                    "This is the legal text contents for the pilot record.",
                )
            )

            root = ET.parse(path).getroot()
            document = root.find("./documents/document")
            self.assertIsNotNone(document)
            self.assertEqual(root.findtext("./metadata/totalDocuments"), "1")
            self.assertEqual(document.findtext("sourceURL"), "https://www.nysenate.gov/legislation/laws/ABC/1")
            self.assertEqual(document.findtext("revisionDate"), "Viewing most recent revision (from 2014-09-22)")
            self.assertEqual(document.findtext("level10"), "CHAPTER 3-B Alcoholic Beverage Control")
            self.assertEqual(document.findtext("level20"), "ARTICLE 1 Short Title")
            self.assertEqual(document.findtext("level30"), "SECTION 1 Short title")
            self.assertEqual(document.findtext("level100") or "", "")
            self.assertIn("legal text contents", document.findtext("contents") or "")

    def test_existing_xml_records_load_and_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawl.xml"
            root_url = "https://www.nysenate.gov/legislation/laws/CONSOLIDATED"
            store = XMLDocumentStore(path, root_url)
            store.initialize()

            first_levels = assign_level({}, 0, "CHAPTER 3-B Alcoholic Beverage Control")
            self.assertTrue(
                store.append(
                    build_record(
                        "https://www.nysenate.gov/legislation/laws/ABC/1",
                        first_levels,
                        "Viewing most recent revision (from 2014-09-22)",
                        "This is the original legal text contents.",
                    )
                )
            )

            resumed_store = XMLDocumentStore(path, root_url)
            resumed_store.initialize()
            self.assertEqual(resumed_store.document_count, 1)

            second_levels = assign_level({}, 0, "AGRICULTURE AND MARKETS")
            self.assertTrue(
                resumed_store.append(
                    build_record(
                        "https://www.nysenate.gov/legislation/laws/AGM/2",
                        second_levels,
                        "Viewing most recent revision (from 2020-01-01)",
                        "This is another legal text record after resume.",
                    )
                )
            )

            root = ET.parse(path).getroot()
            documents = root.findall("./documents/document")
            self.assertEqual(root.findtext("./metadata/totalDocuments"), "2")
            self.assertEqual(len(documents), 2)
            self.assertEqual(documents[0].findtext("sourceURL"), "https://www.nysenate.gov/legislation/laws/ABC/1")
            self.assertEqual(documents[1].findtext("sourceURL"), "https://www.nysenate.gov/legislation/laws/AGM/2")

    def test_duplicate_source_url_records_are_not_appended_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawl.xml"
            store = XMLDocumentStore(path, "https://www.nysenate.gov/legislation/laws/CONSOLIDATED")
            store.initialize()

            levels = assign_level({}, 0, "CHAPTER 3-B Alcoholic Beverage Control")
            record = build_record(
                "https://www.nysenate.gov/legislation/laws/ABC/1",
                levels,
                "Viewing most recent revision (from 2014-09-22)",
                "This is the legal text contents for the duplicate check.",
            )

            self.assertTrue(store.append(record))
            self.assertFalse(store.append(record))

            root = ET.parse(path).getroot()
            self.assertEqual(root.findtext("./metadata/totalDocuments"), "1")
            self.assertEqual(len(root.findall("./documents/document")), 1)

    def test_malformed_existing_xml_fails_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawl.xml"
            original = "<nysenateDocuments><documents>"
            path.write_text(original, encoding="utf-8")

            store = XMLDocumentStore(path, "https://www.nysenate.gov/legislation/laws/CONSOLIDATED")
            with self.assertRaises(XMLDocumentStoreError):
                store.initialize()

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_validation_rejects_navigation_and_short_contents(self) -> None:
        config = CrawlerConfig(root_url="https://www.nysenate.gov/legislation/laws/CONSOLIDATED", min_contents_chars=20)
        levels = assign_level({}, 0, "CHAPTER 3-B Alcoholic Beverage Control")
        record = build_record("https://www.nysenate.gov/legislation/laws/ABC", levels, "", "short")

        self.assertIn("contents too short", validate_record(record, has_children=False, config=config))
        self.assertIn("navigation page cannot be emitted", validate_record(record, has_children=True, config=config))


if __name__ == "__main__":
    unittest.main()
