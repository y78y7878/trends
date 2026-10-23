from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.engine import Engine

from trends.database import GoogleTrend, GoogleTrendNews, KeywordClassification, get_engine, init_db
from trends.entity_resolution_etl import EntityResolver
from trends.gemini_control import (
    DEFAULT_MODELS,
    GeminiLimiter,
    GeminiProgress,
    GeminiStopped,
    gemini_models,
    get_gemini_limiter,
    reset_gemini_limiter,
    retry_delay_seconds,
)
from trends.keyword_auto_classification import auto_classify_unclassified_keywords
from trends.keyword_classification import _generate_json, classify_pending_keywords, classify_pending_news


class FakeAPIError(Exception):
    """Shape of google.genai.errors.APIError: ``code`` plus the JSON ``details``."""

    def __init__(self, code: int, retry_delay: str | None = None, quota_id: str = "PerMinute") -> None:
        details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]}]
        if retry_delay is not None:
            details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
        message = "You exceeded your current quota. Quota exceeded for metric" if code == 429 else "high demand"
        self.code = code
        self.details = {"error": {"code": code, "message": message, "details": details}}
        super().__init__(f"{code} {self.details}")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def failing(errors: list[Exception], result: str = "ok"):
    calls = []

    def request(model):
        calls.append(model)
        if errors:
            raise errors.pop(0)
        return result

    return request, calls


class LimiterTestCase(TestCase):
    def setUp(self) -> None:
        self.time = FakeClock()
        self.limiter = GeminiLimiter(clock=self.time.clock, sleep=self.time.sleep)

    def call(self, request, limiter: GeminiLimiter | None = None):
        """Single-model call: retry and stop rules without fallback."""
        return (limiter or self.limiter).call(request, ["m"])

    def tearDown(self) -> None:
        reset_gemini_limiter()


class RateLimitRetryTests(LimiterTestCase):
    def test_429_waits_for_server_retry_delay_then_succeeds(self) -> None:
        request, calls = failing([FakeAPIError(429, retry_delay="29s")])

        self.assertEqual(self.call(request), "ok")

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.time.sleeps, [29.0])
        self.assertEqual(self.limiter.stats.rate_limited, 1)
        self.assertEqual(self.limiter.stats.wait_seconds, 29.0)

    def test_429_without_retry_delay_waits_at_least_60_seconds(self) -> None:
        request, _ = failing([FakeAPIError(429)])

        self.call(request)

        self.assertEqual(self.time.sleeps, [60.0])

    def test_429_stops_the_run_after_retries(self) -> None:
        request, calls = failing([FakeAPIError(429, retry_delay="1s") for _ in range(4)])

        with self.assertRaises(GeminiStopped):
            self.call(request)

        self.assertEqual(len(calls), 4)
        self.assertTrue(self.limiter.stopped)

    def test_daily_quota_stops_immediately_without_waiting(self) -> None:
        request, calls = failing([FakeAPIError(429, retry_delay="30s", quota_id="GenerateRequestsPerDayPerProject")])

        with self.assertRaises(GeminiStopped):
            self.call(request)

        self.assertEqual((len(calls), self.time.sleeps), (1, []))

    def test_stopped_limiter_does_not_call_the_api_again(self) -> None:
        self.limiter.stats.stopped_reason = "daily quota exhausted"
        request, calls = failing([])

        with self.assertRaises(GeminiStopped):
            self.call(request)

        self.assertEqual(calls, [])

    def test_retry_delay_is_read_from_error_text_when_details_are_missing(self) -> None:
        error = Exception("429 RESOURCE_EXHAUSTED. {'retryDelay': '31s'}")

        self.assertEqual(retry_delay_seconds(error), 31.0)

    def test_wait_budget_stops_the_run(self) -> None:
        limiter = GeminiLimiter(clock=self.time.clock, sleep=self.time.sleep, wait_budget_seconds=30)
        request, _ = failing([FakeAPIError(429, retry_delay="45s")])

        with self.assertRaises(GeminiStopped):
            self.call(request, limiter)

        self.assertEqual(self.time.sleeps, [])


class ServerErrorRetryTests(LimiterTestCase):
    def test_503_retries_once_after_10_seconds(self) -> None:
        request, calls = failing([FakeAPIError(503)])

        self.assertEqual(self.call(request), "ok")

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.time.sleeps, [10.0])
        self.assertEqual(self.limiter.stats.server_errors, 1)

    def test_503_gives_up_on_the_model_after_one_retry(self) -> None:
        request, calls = failing([FakeAPIError(503) for _ in range(3)])

        with self.assertRaises(GeminiStopped):
            self.call(request)

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.time.sleeps, [10.0])
        self.assertIn("m HTTP 503 after retries", self.limiter.stats.stopped_reason)

    def test_other_errors_are_not_retried(self) -> None:
        request, calls = failing([ValueError("bad json")])

        with self.assertRaises(ValueError):
            self.call(request)

        self.assertEqual((len(calls), self.time.sleeps), (1, []))
        self.assertFalse(self.limiter.stopped)


class ModelFallbackTests(LimiterTestCase):
    def test_daily_quota_moves_the_same_request_to_the_next_model(self) -> None:
        request, calls = failing([FakeAPIError(429, quota_id="GenerateRequestsPerDayPerProject")])

        self.assertEqual(self.limiter.call(request, ["a", "b"]), "ok")

        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(self.time.sleeps, [])
        self.assertFalse(self.limiter.stopped)
        self.assertEqual(self.limiter.unavailable, {"a": "daily quota exhausted"})

    def test_overloaded_model_falls_back_after_one_retry(self) -> None:
        request, calls = failing([FakeAPIError(503), FakeAPIError(503)])

        self.assertEqual(self.limiter.call(request, ["a", "b"]), "ok")

        self.assertEqual(calls, ["a", "a", "b"])
        self.assertEqual(self.time.sleeps, [10.0])

    def test_unavailable_model_is_skipped_for_the_rest_of_the_run(self) -> None:
        request, calls = failing([FakeAPIError(429, quota_id="GenerateRequestsPerDayPerProject")])
        self.limiter.call(request, ["a", "b"])

        self.limiter.call(request, ["a", "b"])

        self.assertEqual(calls, ["a", "b", "b"])

    def test_missing_model_falls_back(self) -> None:
        request, calls = failing([FakeAPIError(404)])

        self.assertEqual(self.limiter.call(request, ["typo", "b"]), "ok")

        self.assertEqual(calls, ["typo", "b"])

    def test_stops_when_every_model_is_out(self) -> None:
        daily = lambda: FakeAPIError(429, quota_id="GenerateRequestsPerDayPerProject")
        request, calls = failing([daily(), FakeAPIError(503), FakeAPIError(503)])

        with self.assertRaises(GeminiStopped):
            self.limiter.call(request, ["a", "b"])
        with self.assertRaises(GeminiStopped):
            self.limiter.call(request, ["a", "b"])

        self.assertEqual(calls, ["a", "b", "b"])
        self.assertEqual(
            self.limiter.stats.stopped_reason,
            "all models unavailable (a daily quota exhausted; b HTTP 503 after retries)",
        )

    def test_requests_are_counted_per_model(self) -> None:
        request, _ = failing([FakeAPIError(429, quota_id="GenerateRequestsPerDayPerProject")])

        self.limiter.call(request, ["a", "b"])
        self.limiter.call(request, ["a", "b"])

        self.assertEqual(self.limiter.stats.requests, 3)
        self.assertEqual(dict(self.limiter.stats.model_requests), {"a": 1, "b": 2})

    def test_default_models_and_env_override(self) -> None:
        with patch.dict("os.environ", {"GEMINI_MODELS": ""}):
            self.assertEqual(gemini_models(), DEFAULT_MODELS)
        with patch.dict("os.environ", {"GEMINI_MODELS": " x , y ,"}):
            self.assertEqual(gemini_models(), ("x", "y"))


class RateLimitTests(LimiterTestCase):
    def test_sixth_request_in_a_minute_waits_for_the_window(self) -> None:
        for _ in range(5):
            self.call(lambda model: "ok")
        self.assertEqual(self.time.sleeps, [])

        self.call(lambda model: "ok")

        self.assertEqual(self.time.sleeps, [60.0])
        self.assertEqual(self.limiter.stats.requests, 6)

    def test_window_slides(self) -> None:
        for _ in range(5):
            self.call(lambda model: "ok")
            self.time.now += 10

        self.call(lambda model: "ok")

        self.assertEqual(self.time.sleeps, [10.0])

    def test_each_model_has_its_own_per_minute_window(self) -> None:
        for _ in range(5):
            self.limiter.call(lambda model: "ok", ["a"])

        self.limiter.call(lambda model: "ok", ["b"])

        self.assertEqual(self.time.sleeps, [])

    def test_all_gemini_callers_share_one_limiter(self) -> None:
        limiter = reset_gemini_limiter(clock=self.time.clock, sleep=self.time.sleep)
        client = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(text='{"items": []}')
        ))

        for task in ("entity", "keyword", "news", "entity", "keyword", "news"):
            _generate_json(client, "model", "prompt", task=task, items=20)

        self.assertIs(get_gemini_limiter(), limiter)
        self.assertEqual(limiter.stats.requests, 6)
        self.assertEqual(self.time.sleeps, [60.0])
        self.assertEqual(limiter.stats.processed, {"entity": 40, "keyword": 40, "news": 40})


class StatsReportTests(LimiterTestCase):
    def test_report_lists_counts_and_wait_time(self) -> None:
        stats = self.limiter.stats
        stats.requests = 7
        stats.model_requests.update({"gemini-3.6-flash": 5, "gemini-3.8-flash": 2})
        stats.record_processed("keyword", 40)
        stats.record_processed("news", 17)
        stats.rate_limited = 3
        stats.server_errors = 1
        stats.wait_seconds = 184.4

        report = stats.report(skipped={"keywords": 600, "news": 66})

        self.assertEqual(report.splitlines(), [
            "Gemini Stats",
            "Requests: 7",
            "Models: gemini-3.6-flash 5, gemini-3.8-flash 2",
            "Processed: 57 (keyword 40, news 17)",
            "Skipped: 666 (keywords 600, news 66)",
            "429 Count: 3",
            "503 Count: 1",
            "Wait Time: 184 seconds",
            "Stopped: no",
        ])

    def test_report_shows_stop_reason(self) -> None:
        self.limiter.stats.stopped_reason = "daily quota exhausted"

        self.assertIn("Stopped: daily quota exhausted", self.limiter.stats.report())


class StopAfter:
    """Fake batch classifier that answers ``batches`` calls, then raises GeminiStopped."""

    def __init__(self, batches: int, answer=None) -> None:
        self.remaining = batches
        self.calls: list[list] = []
        self.answer = answer or (lambda batch: {})

    def classify_batch(self, batch):
        self.calls.append(list(batch))
        if self.remaining <= 0:
            raise GeminiStopped("rate limited after retries")
        self.remaining -= 1
        return self.answer(batch)


class ResumeProcessingTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "resume.db"
        self.engine: Engine = init_db(get_engine(self.db_path))

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def add_trends(self, *keywords: str) -> None:
        now = datetime(2026, 10, 14, 12, 0)
        with self.engine.begin() as connection:
            for index, keyword in enumerate(keywords):
                trend_id = connection.execute(GoogleTrend.__table__.insert().values(
                    keyword=keyword, approx_traffic="100+", published_at=now, fetched_at=now,
                )).inserted_primary_key[0]
                connection.execute(GoogleTrendNews.__table__.insert().values(
                    trend_id=trend_id, news_title=f"{keyword} 新聞", news_url=f"https://example.com/{index}",
                    news_source="測試",
                ))

    def classified(self) -> set[str]:
        with self.engine.connect() as connection:
            return set(connection.execute(select(KeywordClassification.keyword)).scalars())

    def test_progress_file_lives_next_to_the_database(self) -> None:
        progress = GeminiProgress.for_engine(self.engine)
        progress.mark("keyword", ["A"])

        self.assertEqual(progress.path, self.db_path.with_name("resume.gemini_progress.json"))
        self.assertEqual(GeminiProgress.for_engine(self.engine).done("keyword"), {"A"})

    def test_keyword_backlog_resumes_where_the_quota_stopped(self) -> None:
        keywords = [f"kw{index}" for index in range(6)]
        self.add_trends(*keywords)
        theme = lambda batch: {k: {"keyword": k, "theme_name": "科技類", "confidence_score": 0.9} for k in batch}

        first = StopAfter(1, theme)
        classify_pending_keywords(self.engine, classifier=first, batch_size=2, limit=0, resolve_entities=False)
        second = StopAfter(10, theme)
        classify_pending_keywords(self.engine, classifier=second, batch_size=2, limit=0, resolve_entities=False)

        self.assertEqual(first.calls, [["kw0", "kw1"], ["kw2", "kw3"]])
        self.assertEqual(second.calls, [["kw2", "kw3"], ["kw4", "kw5"]])
        self.assertEqual(self.classified(), set(keywords))

    def test_keyword_answered_as_other_is_not_resent(self) -> None:
        self.add_trends("路人甲")
        other = lambda batch: {k: {"keyword": k, "theme_name": "其他", "confidence_score": 0.2} for k in batch}
        classify_pending_keywords(self.engine, classifier=StopAfter(5, other), limit=0, resolve_entities=False)
        # Entity resolution later rewrites the row as fallback/其他, which re-enters the pending set.
        with self.engine.begin() as connection:
            connection.execute(KeywordClassification.__table__.update().values(classification_source="fallback"))

        again = StopAfter(5, other)
        classify_pending_keywords(self.engine, classifier=again, limit=0, resolve_entities=False)

        self.assertEqual(again.calls, [])

    def test_entity_resolution_does_not_resend_answered_keywords(self) -> None:
        self.add_trends("路人甲", "路人乙")
        no_entity = lambda batch: {k: {"keyword": k, "entity_type": "其他", "confidence_score": 0} for k in batch}
        first = StopAfter(5, no_entity)
        EntityResolver(self.engine, classifier=first).run(limit=0)
        second = StopAfter(5, no_entity)
        EntityResolver(self.engine, classifier=second).run(limit=0)

        self.assertEqual(len(first.calls), 1)
        self.assertEqual(second.calls, [])

    def test_keywords_gemini_omitted_are_retried(self) -> None:
        self.add_trends("答了", "漏了")
        partial = lambda batch: {"答了": {"keyword": "答了", "theme_name": "科技類", "confidence_score": 0.9}}
        classify_pending_keywords(self.engine, classifier=StopAfter(5, partial), limit=0, resolve_entities=False)
        EntityResolver(self.engine, classifier=StopAfter(5, lambda batch: {})).run(limit=0)

        self.assertEqual(GeminiProgress.for_engine(self.engine).done("keyword"), {"答了"})
        self.assertEqual(GeminiProgress.for_engine(self.engine).done("entity"), set())

    def test_seed_rules_apply_beyond_the_gemini_limit(self) -> None:
        # 「aaa…」 sort before the seed company and fill the limit; seeds must still resolve.
        self.add_trends("aaa1", "aaa2", "緯穎股價")
        classifier = StopAfter(5)

        EntityResolver(self.engine, classifier=classifier).run(limit=2)

        self.assertEqual(classifier.calls, [["aaa1", "aaa2"]])
        with self.engine.connect() as connection:
            row = connection.execute(
                select(KeywordClassification.theme_name, KeywordClassification.classification_source)
                .where(KeywordClassification.keyword == "緯穎股價")
            ).one()
        self.assertEqual(tuple(row), ("科技類", "entity_resolution"))

    def test_entity_queue_puts_new_keywords_before_answered_ones(self) -> None:
        self.add_trends("a1", "a2", "b1")
        GeminiProgress.for_engine(self.engine).mark("entity", ["a1", "a2"])
        classifier = StopAfter(5)

        EntityResolver(self.engine, classifier=classifier).run(limit=1)

        self.assertEqual(classifier.calls, [["b1"]])

    def test_auto_classification_skips_answered_keywords(self) -> None:
        self.add_trends("路人甲", "路人乙")
        GeminiProgress.for_engine(self.engine).mark("auto_quality", ["路人甲"])
        classifier = StopAfter(5)

        auto_classify_unclassified_keywords(self.engine, classifier=classifier)

        self.assertEqual(classifier.calls, [["路人乙"]])

    def test_news_backlog_resumes_without_repeating_finished_articles(self) -> None:
        self.add_trends(*[f"n{index}" for index in range(4)])
        sentiment = lambda batch: {
            article["news_id"]: {"news_id": article["news_id"], "news_sentiment": "Positive",
                                 "sentiment_score": 0.8, "event_type": "其他"}
            for article in batch
        }

        first = StopAfter(1, sentiment)
        self.assertEqual(classify_pending_news(self.engine, classifier=first, batch_size=2, limit=0), 2)
        second = StopAfter(5, sentiment)
        self.assertEqual(classify_pending_news(self.engine, classifier=second, batch_size=2, limit=0), 2)

        self.assertEqual([[a["news_id"] for a in batch] for batch in first.calls], [[1, 2], [3, 4]])
        self.assertEqual([[a["news_id"] for a in batch] for batch in second.calls], [[3, 4]])


class DailyEtlBatchSizeTests(TestCase):
    def test_daily_etl_sends_150_items_per_gemini_request(self) -> None:
        from trends import etl

        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        engine = init_db(get_engine(Path(temporary_directory.name) / "etl.db"))
        self.addCleanup(engine.dispose)
        history = SimpleNamespace(rows=0, rate_limited=False, summary=lambda: "Completed")

        with patch.object(etl, "sync_theme_mapping", return_value=0), \
             patch.object(etl, "run_entity_resolution_etl", return_value=0) as entity, \
             patch.object(etl, "classify_pending_keywords", return_value=0) as keywords, \
             patch.object(etl, "auto_classify_unclassified_keywords", return_value=0) as auto, \
             patch.object(etl, "classify_pending_news", return_value=0) as news, \
             patch("trends.trend_history_collector.run_history_collection", return_value=history), \
             patch.object(etl, "refresh_theme_daily_stats", return_value=0):
            etl.run_daily_etl(engine, keyword_limit=400, news_limit=1000)

        self.assertEqual(entity.call_args.kwargs, {"limit": 400, "batch_size": 150})
        self.assertEqual(keywords.call_args.kwargs["batch_size"], 150)
        self.assertEqual(auto.call_args.kwargs["batch_size"], 150)
        self.assertEqual(news.call_args.kwargs, {"limit": 1000, "batch_size": 150})


class DailyEtlNotBlockedTests(TestCase):
    def test_history_and_theme_stats_run_when_gemini_is_stopped(self) -> None:
        from trends import etl

        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        engine = init_db(get_engine(Path(temporary_directory.name) / "etl.db"))
        self.addCleanup(engine.dispose)
        stopped = GeminiStopped("daily quota exhausted")
        history = SimpleNamespace(rows=12, rate_limited=False, summary=lambda: "Completed")

        with patch.object(etl, "sync_theme_mapping", return_value=0), \
             patch.object(etl, "run_entity_resolution_etl", side_effect=stopped), \
             patch.object(etl, "classify_pending_keywords", side_effect=Exception("unexpected")), \
             patch.object(etl, "auto_classify_unclassified_keywords", side_effect=stopped), \
             patch.object(etl, "classify_pending_news", side_effect=stopped), \
             patch("trends.trend_history_collector.run_history_collection", return_value=history) as collect, \
             patch.object(etl, "refresh_theme_daily_stats", return_value=9) as stats:
            result = etl.run_daily_etl(engine)

        collect.assert_called_once()
        stats.assert_called_once()
        self.assertEqual((result["history_rows"], result["daily_stat_themes"]), (12, 9))
        self.assertEqual((result["classified_keywords"], result["classified_news"]), (0, 0))
