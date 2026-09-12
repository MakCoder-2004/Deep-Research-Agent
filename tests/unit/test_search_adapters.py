"""Mocked contract tests for the Milestone 3 search adapters."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import httpx
import pytest
import respx

from research_agent.config import Settings
from research_agent.errors import ErrorCategory
from research_agent.models import Language, SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools import (
    ArxivTool,
    BraveTool,
    CrossrefTool,
    DdgsTool,
    EuropePMCTool,
    ExaTool,
    GdeltTool,
    GithubTool,
    OfficialDomainTool,
    OpenAlexTool,
    PubMedTool,
    SearXNGTool,
    SemanticScholarTool,
    SerpApiTool,
    StackExchangeTool,
    TavilyTool,
    WikipediaTool,
)
from research_agent.tools._common import (
    build_hit,
    clean_text,
    coerce_url,
    max_results_from_filters,
    normalize_doi,
    parse_datetime,
    strip_html,
)
from research_agent.tools.base import ToolError


def _task(tool_name: str, query: str = "climate policy", **filters: str) -> SearchTask:
    return SearchTask(
        task_id=f"test-{tool_name}",
        tool_name=tool_name,
        query=query,
        language=Language.ENGLISH,
        filters=filters,
    )


def _assert_hit(hit: SearchHit, tool_name: str, source_type: SourceType) -> None:
    assert hit.tool_name == tool_name
    assert hit.source_type is source_type
    assert hit.publisher
    assert hit.accessed_at.tzinfo is not None


@pytest.mark.asyncio
@respx.mock
async def test_tavily_and_brave_normalize_web_and_news_results() -> None:
    tavily_route = respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/article?utm_source=test",
                        "title": "Example article",
                        "content": "A useful result.",
                        "publisher": "Example",
                        "published_date": "2026-01-02",
                        "score": 0.91,
                    }
                ],
                "usage": {"credits": 1},
            },
            headers={"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "999"},
        )
    )
    tool = TavilyTool(api_key="tavily-secret")
    hits = await tool.search(_task("tavily", max_results="1", search_depth="advanced"))
    assert tavily_route.called
    _assert_hit(hits[0], "tavily", SourceType.WEB)
    assert hits[0].score == 0.91
    tavily_request = tavily_route.calls[0].request
    assert tavily_request.headers["authorization"] == "Bearer tavily-secret"
    assert "tavily-secret" not in tavily_request.content.decode()
    assert json.loads(tavily_request.content)["topic"] == "general"
    assert tool.quota_metadata == {"limit": 1000, "remaining": 999, "credits": 1}
    news_hits = await tool.search(_task("tavily", source_category="news", max_results="1"))
    assert json.loads(tavily_route.calls[1].request.content)["topic"] == "news"
    assert news_hits[0].source_type is SourceType.NEWS
    assert await tool.health_check()
    await tool.aclose()

    brave_web = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://docs.example.org/guide",
                            "title": "Guide",
                            "description": "Documentation",
                            "meta_url": {"hostname": "docs.example.org"},
                            "page_age": "2026-02-03",
                        }
                    ]
                }
            },
        )
    )
    brave = BraveTool(api_key="brave-secret")
    hits = await brave.search(_task("brave_search", max_results="1"))
    assert brave_web.called
    _assert_hit(hits[0], "brave_search", SourceType.WEB)
    assert brave_web.calls[0].request.headers["x-subscription-token"] == "brave-secret"
    assert brave_web.calls[0].request.url.params["count"] == "1"
    assert "channel" not in brave_web.calls[0].request.url.params
    assert await brave.health_check()
    await brave.aclose()

    brave_news = respx.get("https://api.search.brave.com/res/v1/news/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://news.example.org/story",
                        "title": "News story",
                        "description": "Current events",
                        "meta_url": {"hostname": "news.example.org"},
                    }
                ]
            },
        )
    )
    brave = BraveTool(api_key="brave-secret")
    hits = await brave.search(_task("brave_search", channel="news", max_results="1"))
    assert brave_news.called
    assert brave_news.calls[0].request.url.path.endswith("/news/search")
    assert brave_news.calls[0].request.url.params["search_lang"] == "en"
    _assert_hit(hits[0], "brave_search", SourceType.NEWS)
    await brave.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_wikipedia_supports_arabic_and_wikimedia_projects() -> None:
    route = respx.get("https://commons.wikimedia.org/w/api.php").mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "search": [
                        {
                            "pageid": 7,
                            "title": "Climate",
                            "snippet": "<b>Background</b>",
                            "timestamp": "2026-01-01T00:00:00Z",
                        }
                    ]
                }
            },
        )
    )
    respx.get("https://en.wikipedia.org/w/api.php").mock(
        return_value=httpx.Response(200, json={"query": {"general": {"sitename": "Wikipedia"}}})
    )
    tool = WikipediaTool()
    hits = await tool.search(_task("wikipedia", project="commons", max_results="1"))
    assert route.called
    assert "commons.wikimedia.org" in str(hits[0].url)
    _assert_hit(hits[0], "wikipedia", SourceType.WIKI)
    assert await tool.health_check()
    await tool.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_academic_adapters_normalize_metadata() -> None:
    semantic = respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": "A paper",
                        "abstract": "Abstract text",
                        "url": "https://papers.example.org/paper",
                        "venue": "Journal",
                        "year": 2025,
                        "paperId": "paper-id",
                        "externalIds": {"DOI": "10.1000/test"},
                        "publicationDate": "2025-04-05",
                    }
                ]
            },
        )
    )
    semantic_tool = SemanticScholarTool(api_key="semantic-secret")
    hits = await semantic_tool.search(_task("semantic_scholar", max_results="1"))
    assert semantic.called
    _assert_hit(hits[0], "semantic_scholar", SourceType.PAPER)
    assert hits[0].doi == "10.1000/test"
    assert hits[0].published_at == parse_datetime("2025-04-05")
    assert await semantic_tool.health_check()
    await semantic_tool.aclose()

    crossref = respx.get("https://api.crossref.org/works").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/crossref",
                            "title": ["Crossref paper"],
                            "publisher": "Publisher",
                            "URL": "https://doi.org/10.1000/crossref",
                            "published": {"date-parts": [[2024, 3, 4]]},
                            "container-title": ["Journal"],
                        }
                    ]
                }
            },
        )
    )
    crossref_tool = CrossrefTool(mailto="research@example.org")
    hits = await crossref_tool.search(_task("crossref", max_results="1"))
    assert crossref.called
    _assert_hit(hits[0], "crossref", SourceType.ACADEMIC)
    assert hits[0].doi == "10.1000/crossref"
    assert hits[0].published_at == parse_datetime("2024-03-04")
    doi_route = respx.get("https://api.crossref.org/works/10.1000%2Fcrossref").mock(
        return_value=httpx.Response(
            200,
            json={"message": {"DOI": "10.1000/crossref", "title": ["DOI paper"]}},
        )
    )
    doi_hits = await crossref_tool.search(_task("crossref", query="DOI:10.1000/CROSSREF"))
    assert doi_route.called
    assert doi_hits[0].title == "DOI paper"
    assert doi_hits[0].doi == "10.1000/crossref"
    assert await crossref_tool.health_check()
    await crossref_tool.aclose()

    arxiv = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(
            200,
            text="""
                 <feed xmlns="http://www.w3.org/2005/Atom"
                       xmlns:arxiv="http://arxiv.org/schemas/atom">
                  <entry>
                    <id>https://arxiv.org/abs/1234.5678</id>
                    <title>Preprint title</title>
                    <summary>Preprint summary</summary>
                    <published>2024-01-02T00:00:00Z</published>
                    <arxiv:doi>10.1000/arxiv</arxiv:doi>
                  </entry>
                </feed>
            """,
        )
    )
    arxiv_tool = ArxivTool(min_interval_seconds=0)
    hits = await arxiv_tool.search(_task("arxiv", max_results="1"))
    assert arxiv.called
    _assert_hit(hits[0], "arxiv", SourceType.PAPER)
    assert hits[0].doi == "10.1000/arxiv"
    assert await arxiv_tool.health_check()
    await arxiv_tool.aclose()

    openalex = respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.1000/openalex",
                        "title": "OpenAlex work",
                        "publication_date": "2023-01-01",
                        "abstract_inverted_index": {"Useful": [0], "abstract": [1]},
                        "primary_location": {
                            "landing_page_url": "https://journal.example.org/work",
                            "source": {"display_name": "Journal"},
                        },
                    }
                ]
            },
        )
    )
    openalex_tool = OpenAlexTool(mailto="research@example.org")
    hits = await openalex_tool.search(_task("openalex", max_results="1"))
    assert openalex.called
    _assert_hit(hits[0], "openalex", SourceType.PAPER)
    assert hits[0].doi == "10.1000/openalex"
    assert hits[0].published_at == parse_datetime("2023-01-01")
    assert await openalex_tool.health_check()
    await openalex_tool.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_news_technical_and_medical_adapters() -> None:
    gdelt = respx.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
        return_value=httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://news.example.org/current",
                        "title": "Current story",
                        "snippet": "News snippet",
                        "domain": "news.example.org",
                        "seendate": "20260102030405",
                    }
                ]
            },
        )
    )
    gdelt_tool = GdeltTool()
    hits = await gdelt_tool.search(_task("gdelt", max_results="1"))
    assert gdelt.called
    request = gdelt.calls[0].request
    assert request.url.path == "/api/v2/doc/doc"
    assert request.url.params["mode"] == "artlist"
    assert request.url.params["format"] == "json"
    assert request.url.params["maxrecords"] == "1"
    _assert_hit(hits[0], "gdelt", SourceType.NEWS)
    assert await gdelt_tool.health_check()
    await gdelt_tool.aclose()

    github_repo = respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "html_url": "https://github.com/acme/project",
                        "full_name": "acme/project",
                        "description": "A project",
                        "language": "Python",
                        "stargazers_count": 500,
                        "owner": {"login": "acme"},
                        "created_at": "2024-01-01T00:00:00Z",
                    }
                ]
            },
        )
    )
    github = GithubTool(token="github-secret")  # noqa: S106
    hits = await github.search(_task("github", kind="repositories", max_results="1"))
    assert github_repo.called
    _assert_hit(hits[0], "github", SourceType.REPOSITORY)
    await github.aclose()

    issue = respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "html_url": "https://github.com/acme/project/issues/1",
                        "title": "An issue",
                        "body": "Issue body",
                        "user": {"login": "contributor"},
                        "created_at": "2024-02-01T00:00:00Z",
                    }
                ]
            },
        )
    )
    respx.get("https://api.github.com/rate_limit").mock(
        return_value=httpx.Response(200, json={"resources": {}})
    )
    github = GithubTool()
    hits = await github.search(_task("github", kind="issues", max_results="1"))
    assert issue.called
    _assert_hit(hits[0], "github", SourceType.TECHNICAL)
    assert await github.health_check()
    await github.aclose()

    release_search = respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"full_name": "acme/project", "owner": {"login": "acme"}}]},
        )
    )
    releases = respx.get("https://api.github.com/repos/acme/project/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "html_url": "https://github.com/acme/project/releases/tag/v1",
                    "tag_name": "v1",
                    "name": "First release",
                    "body": "Release notes",
                    "published_at": "2024-03-01T00:00:00Z",
                }
            ],
        )
    )
    github = GithubTool()
    hits = await github.search(_task("github", kind="releases", max_results="1"))
    assert release_search.called and releases.called
    _assert_hit(hits[0], "github", SourceType.REPOSITORY)
    await github.aclose()

    stack = respx.get("https://api.stackexchange.com/2.3/search/advanced").mock(
        return_value=httpx.Response(
            200,
            json={
                "quota_max": 300,
                "quota_remaining": 299,
                "items": [
                    {
                        "link": "https://stackoverflow.com/questions/1",
                        "title": "How do I test this?",
                        "body": "<p>Answer</p>",
                        "creation_date": 1700000000,
                    }
                ],
            },
        )
    )
    stack_tool = StackExchangeTool()
    hits = await stack_tool.search(_task("stack_exchange", max_results="1"))
    assert stack.called
    _assert_hit(hits[0], "stack_exchange", SourceType.TECHNICAL)
    assert stack_tool.quota_metadata == {"limit": 300, "remaining": 299}
    respx.get("https://api.stackexchange.com/2.3/info").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    assert await stack_tool.health_check()
    await stack_tool.aclose()

    esearch = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["123"]}})
    )
    esummary = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["123"],
                    "123": {
                        "title": "Medical paper",
                        "fulljournalname": "Medical Journal",
                        "pubdate": "2022 Jan",
                        "sortfirstauthor": "Author",
                        "articleids": [{"idtype": "doi", "value": "doi:10.1000/medical"}],
                    },
                }
            },
        )
    )
    pubmed = PubMedTool(email="research@example.org", min_interval_seconds=0)
    hits = await pubmed.search(_task("pubmed", max_results="1"))
    assert esearch.called and esummary.called
    _assert_hit(hits[0], "pubmed", SourceType.MEDICAL)
    assert hits[0].doi == "10.1000/medical"
    assert hits[0].published_at == parse_datetime("2022 Jan")
    assert await pubmed.health_check()
    await pubmed.aclose()

    europe = respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [
                        {
                            "id": "456",
                            "title": "Europe paper",
                            "abstractText": "An abstract.",
                            "firstPublicationDate": "2021-05-06",
                            "doi": "https://doi.org/10.1000/europe",
                            "journalInfo": {"journal": {"title": "European Journal"}},
                        }
                    ]
                }
            },
        )
    )
    europe_tool = EuropePMCTool()
    hits = await europe_tool.search(_task("europe_pmc", max_results="1"))
    assert europe.called
    _assert_hit(hits[0], "europe_pmc", SourceType.MEDICAL)
    assert hits[0].doi == "10.1000/europe"
    assert hits[0].published_at == parse_datetime("2021-05-06")
    assert await europe_tool.health_check()
    await europe_tool.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_provider_error_shapes_raise_normalized_tool_errors() -> None:
    crossref = respx.get("https://api.crossref.org/works").mock(
        return_value=httpx.Response(200, json={"message": {"status": "ok"}})
    )
    crossref_tool = CrossrefTool()
    with pytest.raises(ToolError) as crossref_error:
        await crossref_tool.search(_task("crossref"))
    assert crossref.called
    assert crossref_error.value.category is ErrorCategory.INVALID_REQUEST
    await crossref_tool.aclose()

    arxiv = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(
            200,
            text="""
                <feed xmlns="http://www.w3.org/2005/Atom">
                  <entry>
                    <id>http://arxiv.org/api/errors#bad_query</id>
                    <title>Error</title>
                    <summary>bad query</summary>
                  </entry>
                </feed>
            """,
        )
    )
    arxiv_tool = ArxivTool(min_interval_seconds=0)
    with pytest.raises(ToolError) as arxiv_error:
        await arxiv_tool.search(_task("arxiv"))
    assert arxiv.called
    assert arxiv_error.value.category is ErrorCategory.INVALID_REQUEST
    await arxiv_tool.aclose()

    pubmed = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(
            200,
            json={"esearchresult": {"ERROR": "invalid query"}},
        )
    )
    pubmed_tool = PubMedTool(min_interval_seconds=0)
    with pytest.raises(ToolError) as pubmed_error:
        await pubmed_tool.search(_task("pubmed"))
    assert pubmed.called
    assert pubmed_error.value.category is ErrorCategory.INVALID_REQUEST
    await pubmed_tool.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_official_domain_discovery_is_allowlisted() -> None:
    sitemap = respx.get("https://gov.example/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="""
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://gov.example/budget-2026</loc></url>
                  <url><loc>https://sub.gov.example/regulator</loc></url>
                  <url><loc>https://evil.example/phishing</loc></url>
                </urlset>
            """,
        )
    )
    homepage = respx.get("https://gov.example/").mock(
        return_value=httpx.Response(200, text="<html><title>Government</title></html>")
    )
    tool = OfficialDomainTool(["GOV.EXAMPLE", "bad/path"])
    hits = await tool.search(_task("official_domains", query="budget", max_results="2"))
    assert sitemap.called
    assert all("gov.example" in str(hit.url) for hit in hits)
    assert all(hit.source_type is SourceType.OFFICIAL for hit in hits)
    assert await tool.health_check()
    assert homepage.called
    await tool.aclose()

    empty = OfficialDomainTool()
    with pytest.raises(ToolError) as excinfo:
        await empty.search(_task("official_domains"))
    assert excinfo.value.category is ErrorCategory.UNAVAILABLE

    settings = Settings(_env_file=None, OFFICIAL_ALLOWED_DOMAINS="other.example,reg.example")
    from_settings = OfficialDomainTool.from_settings(settings)
    assert await from_settings.health_check() is False
    await from_settings.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_optional_http_adapters_share_contract() -> None:
    exa_route = respx.post("https://api.exa.ai/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://example.org/exa", "title": "Exa", "text": "Text"}]},
        )
    )
    exa = ExaTool(api_key="exa-secret")
    hits = await exa.search(_task("exa", max_results="1"))
    assert exa_route.called
    _assert_hit(hits[0], "exa", SourceType.WEB)
    assert await exa.health_check()
    await exa.aclose()

    serp_route = respx.get("https://serpapi.com/search.json").mock(
        return_value=httpx.Response(
            200,
            json={"organic_results": [{"link": "https://example.org/serp", "title": "Serp"}]},
        )
    )
    serp = SerpApiTool(api_key="serp-secret")
    hits = await serp.search(_task("serpapi", max_results="1"))
    assert serp_route.called
    _assert_hit(hits[0], "serpapi", SourceType.WEB)
    assert await serp.health_check()
    await serp.aclose()

    searx_route = respx.get("https://search.example/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.org/searx", "title": "SearX", "content": "Text"}
                ]
            },
        )
    )
    respx.get("https://search.example/").mock(return_value=httpx.Response(200, text="ok"))
    searx = SearXNGTool(base_url="https://search.example")
    hits = await searx.search(_task("searxng", max_results="1"))
    assert searx_route.called
    _assert_hit(hits[0], "searxng", SourceType.WEB)
    assert await searx.health_check()
    await searx.aclose()

    ddgs = DdgsTool()
    with pytest.raises(ToolError) as excinfo:
        await ddgs.search(_task("ddgs"))
    assert excinfo.value.category is ErrorCategory.UNAVAILABLE
    assert await ddgs.health_check() is False


@pytest.mark.asyncio
async def test_optional_http_adapters_disable_cleanly_without_configuration() -> None:
    tools = (ExaTool(), SerpApiTool(), SearXNGTool())
    for tool in tools:
        with pytest.raises(ToolError) as excinfo:
            await tool.search(_task(tool.name))
        assert excinfo.value.category is ErrorCategory.UNAVAILABLE
        assert await tool.health_check() is False
        await tool.aclose()


@pytest.mark.asyncio
async def test_ddgs_adapter_normalizes_optional_local_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("ddgs")

    class FakeDDGS:
        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def text(self, query: str, **kwargs: object) -> list[dict[str, str]]:
            assert query == "python"
            assert kwargs["max_results"] == 1
            return [{"href": "https://example.org/ddgs", "title": "DDG", "body": "Result"}]

    module.DDGS = FakeDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)
    tool = DdgsTool()
    hits = await tool.search(_task("ddgs", query="python", max_results="1"))
    _assert_hit(hits[0], "ddgs", SourceType.WEB)
    assert await tool.health_check()
    await tool.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_tool_errors_include_retry_metadata_and_cancelled_error_propagates() -> None:
    route = respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            429,
            text="api_key=secret-not-logged; slow down",
            headers={
                "Retry-After": "2",
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "0",
            },
        )
    )
    tool = TavilyTool(api_key="secret-not-logged")
    with pytest.raises(ToolError) as excinfo:
        await tool.search(_task("tavily"))
    assert route.called
    assert excinfo.value.category is ErrorCategory.RATE_LIMITED
    assert excinfo.value.http_status == 429
    assert excinfo.value.retry_after == 2.0
    assert "secret-not-logged" not in str(excinfo.value)
    assert tool.quota_metadata == {"limit": 100, "remaining": 0, "retry_after": 2.0}
    assert "secret-not-logged" not in route.calls[0].request.content.decode()
    await tool.aclose()

    async def cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(cancelled))
    cancelled_tool = TavilyTool(api_key="key", client=client)
    with pytest.raises(asyncio.CancelledError):
        await cancelled_tool.search(_task("tavily"))
    await client.aclose()


def test_shared_normalization_helpers_and_settings() -> None:
    assert coerce_url("https://example.org/path") == "https://example.org/path"
    assert clean_text(["one", 2, None]) == "one 2"
    assert coerce_url("javascript:alert(1)") is None
    assert coerce_url("https://user:pass@example.org") is None
    assert strip_html("<b>hello</b>&amp; world") == "hello & world"
    assert parse_datetime("20260102030405") is not None
    assert parse_datetime("2024-01-02T03:04:05Z") is not None
    assert parse_datetime("Tue, 02 Jan 2024 03:04:05 GMT") is not None
    assert parse_datetime(1700000000) is not None
    assert parse_datetime(True) is None
    assert parse_datetime("not a date") is None
    assert normalize_doi("https://doi.org/10.1000/ABC.") == "10.1000/abc"
    assert normalize_doi("doi: 10.1000/ABC") == "10.1000/abc"
    assert normalize_doi("not a doi") is None
    assert max_results_from_filters({"max_results": "100"}, maximum=10) == 10
    assert max_results_from_filters({"max_results": "nope"}) == 5
    hit = build_hit(
        url="https://example.org",
        title="Result",
        score="bad",  # type: ignore[arg-type]
        tool_name="test",
    )
    assert hit is not None and hit.score == 0
