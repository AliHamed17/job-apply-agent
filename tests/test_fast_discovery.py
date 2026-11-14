from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading

import httpx
import pytest

from discovery.fast_discovery import DiscoveryBatch, read_saved_urls


def job_html(url: str, title: str = "Software Engineer") -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "url": url,
        "hiringOrganization": {"name": "Example Engineering"},
        "description": "Build and validate Python services in a collaborative engineering team.",
        "jobLocation": {"address": {"addressLocality": "Haifa", "addressCountry": "IL"}},
    }
    return '<script type="application/ld+json">' + json.dumps(posting) + "</script>"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )


def test_saved_whatsapp_inputs_extract_only_urls_and_deduplicate():
    saved = {
        "leads": [
            {"url": "https://www.linkedin.com/jobs/view/123?trk=chat", "company": "Example"},
            {"source_text": "Apply: https://jobs.lever.co/example/role. Call 555-0000"},
            {"url": "https://www.linkedin.com/jobs/view/123?trk=chat"},
        ],
        "private_note": "Not a job URL: https://mail.example.test/private",
    }
    assert read_saved_urls(json.dumps(saved)) == [
        "https://www.linkedin.com/jobs/view/123?trk=chat",
        "https://jobs.lever.co/example/role",
    ]
    assert read_saved_urls("New role: *https://boards.greenhouse.io/example/jobs/42*.") == [
        "https://boards.greenhouse.io/example/jobs/42"
    ]


async def test_batch_parallel_fetches_dedup_and_cached_second_run(tmp_path):
    first_two_started = asyncio.Event()
    requests = []
    active = 0
    maximum_active = 0

    async def respond(request):
        nonlocal active, maximum_active
        requests.append(str(request.url))
        active += 1
        maximum_active = max(maximum_active, active)
        if len(requests) >= 2:
            first_two_started.set()
        await asyncio.wait_for(first_two_started.wait(), timeout=2)
        active -= 1
        return httpx.Response(200, text=job_html(str(request.url)), headers={"etag": '"version-1"'})

    urls = [
        "https://boards.greenhouse.io/example/jobs/42?utm_source=whatsapp",
        "https://boards.greenhouse.io/example/jobs/42",
        "https://jobs.lever.co/example/role",
    ]
    cache = tmp_path / "cache.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        batch = DiscoveryBatch(cache_path=cache, client=client, concurrency=2)
        first = await batch.run(urls)
        second = await DiscoveryBatch(cache_path=cache, client=client).run(urls)

    assert first["input_count"] == 3
    assert first["duplicate_count"] == 1
    assert maximum_active == 2
    assert len(requests) == 2
    assert [row["status"] for row in first["results"]] == ["discovered", "discovered"]
    assert first["results"][0]["jobs"][0]["title"] == "Software Engineer"
    assert first["results"][0]["jobs"][0]["company"] == "Example Engineering"
    assert all(row["cache_hit"] for row in second["results"])
    assert second["results"][0]["steps"][-1]["action"] == "cache_hit"
    assert "utm_source" not in cache.read_text(encoding="utf-8")


async def test_signed_in_sites_and_unknown_company_hosts_require_browser_without_http():
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, text=job_html(str(request.url)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client).run(
            [
                "https://il.linkedin.com/jobs/view/software-engineer-123?trk=chat",
                "https://www.linkedin.com/jobs/search/?currentJobId=123&keywords=software",
                "https://il.indeed.com/viewjob?jk=abc&from=chat",
                "https://careers.example.test/jobs/42",
            ]
        )

    assert requests == []
    assert report["duplicate_count"] == 1
    assert [row["url"] for row in report["results"]] == [
        "https://www.linkedin.com/jobs/view/123",
        "https://www.indeed.com/viewjob?jk=abc",
        "https://careers.example.test/jobs/42",
    ]
    assert all(row["status"] == "browser_handoff" for row in report["results"])


async def test_stale_etag_304_reuses_parsed_jobs(tmp_path):
    requests = []

    async def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, text=job_html(str(request.url)), headers={"etag": '"v1"'})
        return httpx.Response(304)

    cache = tmp_path / "cache.json"
    url = "https://boards.greenhouse.io/example/jobs/42"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await DiscoveryBatch(client=client, cache_path=cache, ttl_seconds=0).run([url])
        report = await DiscoveryBatch(client=client, cache_path=cache, ttl_seconds=0).run([url])

    assert requests[1].headers["if-none-match"] == '"v1"'
    assert report["results"][0]["revalidated"] is True
    assert report["results"][0]["jobs"][0]["title"] == "Software Engineer"
    assert report["results"][0]["steps"][-1]["action"] == "not_modified"


async def test_one_failure_does_not_interrupt_other_jobs_and_challenges_are_handoffs():
    async def respond(request):
        if request.url.path.endswith("/42"):
            raise httpx.ConnectError("private detail must not be persisted", request=request)
        if request.url.path.endswith("/43"):
            return httpx.Response(403, text="Access denied")
        if request.url.path.endswith("/44"):
            return httpx.Response(200, text="<title>Just a moment...</title>Verify you are human")
        return httpx.Response(200, text=job_html(str(request.url)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client).run(
            [f"https://boards.greenhouse.io/example/jobs/{number}" for number in range(42, 46)]
        )

    assert [row["status"] for row in report["results"]] == [
        "error",
        "browser_handoff",
        "browser_handoff",
        "discovered",
    ]
    assert "private detail" not in json.dumps(report)


async def test_company_opt_in_redirects_cannot_fetch_private_or_unapproved_hosts():
    requests = []

    async def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/private":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        if request.url.path == "/outside":
            return httpx.Response(302, headers={"location": "https://unapproved.example.test/job"})
        return httpx.Response(200, text=job_html(str(request.url)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client, allowed_hosts=["careers.example.test"]).run(
            [
                "https://careers.example.test/private",
                "https://careers.example.test/outside",
                "https://careers.example.test/jobs/42",
            ]
        )

    assert len(requests) == 3
    assert [row["status"] for row in report["results"]] == [
        "rejected",
        "browser_handoff",
        "discovered",
    ]
    assert "127.0.0.1" not in json.dumps(report)


async def test_private_dns_and_sensitive_urls_are_rejected_without_logging_secrets(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
    )
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, text=job_html(str(request.url)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client).run(
            [
                "https://boards.greenhouse.io/example/jobs/42",
                "https://user:secret@jobs.lever.co/example/role",
                "https://jobs.lever.co/example/role?access_token=secret-value",
                "http://127.0.0.1/private",
            ]
        )

    assert requests == []
    assert all(row["status"] == "rejected" for row in report["results"])
    assert "secret" not in json.dumps(report)
    assert "10.1.2.3" not in json.dumps(report)


@pytest.mark.parametrize(
    "key", ["apikey", "auth", "authorization", "APIKEY", "Auth", "Authorization"]
)
async def test_credential_query_aliases_never_reach_handoff_or_cache(tmp_path, key):
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, text=job_html(str(request.url)))

    cache = tmp_path / "cache.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client, cache_path=cache).run(
            [
                f"https://www.linkedin.com/jobs/view/123?{key}=synthetic-credential",
                f"https://boards.greenhouse.io/example/jobs/42?{key}=synthetic-credential",
            ]
        )
    assert requests == []
    assert [row["status"] for row in report["results"]] == ["rejected", "rejected"]
    assert "synthetic-credential" not in json.dumps(report)
    assert "synthetic-credential" not in cache.read_text(encoding="utf-8")


async def test_per_host_limit_is_enforced_without_serializing_other_hosts():
    release = asyncio.Event()
    active = {}
    max_per_host = {}

    async def respond(request):
        host = request.url.host
        active[host] = active.get(host, 0) + 1
        max_per_host[host] = max(max_per_host.get(host, 0), active[host])
        if len(active) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        active[host] -= 1
        return httpx.Response(200, text=job_html(str(request.url)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client, concurrency=3, per_host=1).run(
            [
                "https://boards.greenhouse.io/example/jobs/42",
                "https://boards.greenhouse.io/example/jobs/43",
                "https://jobs.lever.co/example/role",
            ]
        )
    assert all(row["status"] == "discovered" for row in report["results"])
    assert max_per_host == {"boards.greenhouse.io": 1, "jobs.lever.co": 1}


async def test_cache_does_not_override_current_fetch_policy(tmp_path):
    async def respond(request):
        return httpx.Response(200, text=job_html(str(request.url)))

    url = "https://careers.example.test/jobs/42"
    cache = tmp_path / "cache.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await DiscoveryBatch(
            client=client, cache_path=cache, allowed_hosts=["careers.example.test"]
        ).run([url])
        report = await DiscoveryBatch(client=client, cache_path=cache).run([url])
    assert report["results"][0]["status"] == "browser_handoff"
    assert report["results"][0]["reason"] == "host_requires_opt_in"


async def test_oversized_response_and_title_only_fallback_are_not_job_details():
    async def respond(request):
        if request.url.path.endswith("/42"):
            return httpx.Response(200, text="x" * 1025)
        return httpx.Response(200, text="<h1>Software Engineer</h1>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client, max_bytes=1024).run(
            [
                "https://boards.greenhouse.io/example/jobs/42",
                "https://boards.greenhouse.io/example/jobs/43",
            ]
        )
    assert [row["reason"] for row in report["results"]] == [
        "response_too_large",
        "job_details_require_browser",
    ]


async def test_bad_encoding_does_not_interrupt_batch():
    async def respond(request):
        if request.url.path.endswith("/42"):
            return httpx.Response(
                200,
                content=b"example",
                headers={"content-type": "text/html; charset=invalid-codec"},
            )
        return httpx.Response(200, text=job_html(str(request.url)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await DiscoveryBatch(client=client).run(
            [
                "https://boards.greenhouse.io/example/jobs/42",
                "https://boards.greenhouse.io/example/jobs/43",
            ]
        )
    assert [row["status"] for row in report["results"]] == ["browser_handoff", "discovered"]


async def test_dns_lookup_obeys_timeout(monkeypatch):
    released = threading.Event()

    def stalled_dns(*args, **kwargs):
        released.wait(timeout=1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", stalled_dns)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
        try:
            report = await asyncio.wait_for(
                DiscoveryBatch(client=client, timeout_seconds=0.01).run(
                    ["https://boards.greenhouse.io/example/jobs/42"]
                ),
                timeout=0.2,
            )
        finally:
            released.set()
    assert report["results"][0]["status"] == "rejected"
    assert report["results"][0]["reason"] == "nonpublic_or_unresolved_host"


async def test_changed_redirect_target_does_not_reuse_old_posting_metadata(tmp_path):
    round_number = 0

    async def respond(request):
        nonlocal round_number
        if request.url.path == "/example":
            round_number += 1
            target = "/example/jobs/42" if round_number == 1 else "/example/jobs/43"
            return httpx.Response(302, headers={"location": target})
        # Both structured postings keep the body identical across redirect targets.
        # The current parser binds the matching record to the fetched ATS identity.
        body = job_html("https://boards.greenhouse.io/example/jobs/42")
        body += job_html("https://boards.greenhouse.io/example/jobs/43")
        return httpx.Response(200, text=body)

    cache = tmp_path / "cache.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await DiscoveryBatch(client=client, cache_path=cache, ttl_seconds=0).run(
            ["https://boards.greenhouse.io/example"]
        )
        second = await DiscoveryBatch(client=client, cache_path=cache, ttl_seconds=0).run(
            ["https://boards.greenhouse.io/example"]
        )
    assert second["results"][0]["jobs"][0]["source_url"] == (
        "https://boards.greenhouse.io/example/jobs/43"
    )


def test_cli_keeps_public_parser_diagnostics_off_json_stdout(tmp_path, monkeypatch, capsys):
    import structlog

    from scripts.discover_job_urls import main

    config = structlog.get_config()
    client_class = httpx.AsyncClient

    def clean_test_client(**kwargs):
        return client_class(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=job_html(str(request.url)))
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", clean_test_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "discover_job_urls",
            "--url",
            "https://boards.greenhouse.io/example/jobs/42",
            "--output",
            str(tmp_path / "review.json"),
            "--cache",
            str(tmp_path / "cache.json"),
        ],
    )
    try:
        assert main() == 0
    finally:
        structlog.configure(**config)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["statuses"] == {"discovered": 1}


def test_cli_reads_saved_urls_and_writes_separate_review_manifest(tmp_path):
    saved = tmp_path / "saved-whatsapp.json"
    saved.write_text(
        json.dumps(
            {
                "leads": [
                    {"url": "https://www.linkedin.com/jobs/view/123?trk=chat"},
                    {"url": "https://www.linkedin.com/jobs/view/123"},
                    {"url": "https://il.indeed.com/viewjob?jk=abc"},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "review.json"
    cache = tmp_path / "cache.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.discover_job_urls",
            "--input",
            str(saved),
            "--output",
            str(output),
            "--cache",
            str(cache),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    summary = json.loads(result.stdout)
    assert report["input_count"] == 3
    assert report["duplicate_count"] == 1
    assert summary["statuses"] == {"browser_handoff": 2}
    assert json.loads(cache.read_text(encoding="utf-8")) == {"version": 1, "entries": {}}
