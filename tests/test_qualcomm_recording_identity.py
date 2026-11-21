from batch_apply.urls import canonical_job_url, job_key


def test_qualcomm_application_and_receipt_match_posting_id():
    job = "https://careers.qualcomm.com/careers/job/123456789012"
    form = "https://careers.qualcomm.com/careers/apply?pid=123456789012&domain=qualcomm.com"
    success = (
        "https://careers.qualcomm.com/careers/apply/success?domain=qualcomm.com&pid=123456789012"
    )
    assert canonical_job_url(form) == job
    assert canonical_job_url(success) == job
    assert job_key(form) == job_key(job)
    assert job_key(success) == job_key(job)


def test_qualcomm_identity_does_not_merge_unrelated_domains_or_bad_ids():
    url = "https://other.example/careers/apply?pid=123456789012&domain=qualcomm.com"
    assert canonical_job_url(url).startswith("https://other.example/")
    url = "https://careers.qualcomm.com/careers/apply?pid=not-a-number&domain=qualcomm.com"
    assert "/careers/apply?" in canonical_job_url(url)
    url = "https://careers.qualcomm.com/careers/apply?pid=123456789012&domain=other.example"
    assert "/careers/apply?" in canonical_job_url(url)


def test_ambiguous_qualcomm_ids_are_not_collapsed():
    url = "https://careers.qualcomm.com/careers/apply?pid=123&pid=456"
    assert "/careers/apply?" in canonical_job_url(url)
