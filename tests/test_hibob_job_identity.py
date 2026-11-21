from batch_apply.urls import canonical_job_url, job_key


def test_listing_and_apply_are_one_hibob_job():
    job = "https://sapiens.careers.hibob.com/jobs/01234567-89ab-cdef-0123-456789abcdef"
    form = job + "/apply?utm_source=example&vq_campaign=example"
    assert canonical_job_url(form) == job
    assert job_key(form) == job_key(job)


def test_hibob_mapping_does_not_guess_other_hosts_or_invalid_ids():
    url = "https://other.careers.hibob.com/jobs/01234567-89ab-cdef-0123-456789abcdef/apply"
    assert canonical_job_url(url).endswith("/apply")
    url = "https://sapiens.careers.hibob.com/jobs/not-a-uuid/apply"
    assert canonical_job_url(url).endswith("/apply")
