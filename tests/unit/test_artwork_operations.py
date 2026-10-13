"""Explicit album operations and per-submission request identity (#48)."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pmxbot
import pytest
from bs4 import BeautifulSoup

from pmxbot.jobs import JobStore, process_job
from pmxbot.music import MusicLibrary
from tests.unit.test_album_page import (  # noqa: F401
    page,
    variation,
    enable_responses,
)
from tests.unit.test_jobs import draft


def submission(values, operation, token=None):
    return dict(values, operation=operation, submission_token=token or uuid.uuid4().hex)


@pytest.mark.parametrize('api', ['images', 'responses'])
def test_repeated_create_new_is_fresh_and_retry_is_idempotent(variation, api):
    cache, request, post, client, values, source = variation
    if api == 'responses':
        enable_responses(cache, post)
    source.unlink()  # Create New must never open the highlighted source.
    original = cache.get_image(42)
    store = JobStore(cache.database)
    jobs = []
    for _ in range(2):
        data = submission(values, 'create_new')
        response = request(method='POST', data=data, accept='application/json')
        assert response['status'] == 202
        identifier = json.loads(response['body'])['id']
        retry = request(method='POST', data=data, accept='application/json')
        assert json.loads(retry['body'])['id'] == identifier
        jobs.append(identifier)
    assert len(set(jobs)) == 2  # Identical properties don't deduplicate pending jobs.
    post.assert_not_called()
    for _ in jobs:
        job = store.claim()
        assert job['operation'] == 'create_new'
        assert job['source_image_id'] == 0
        process_job(store, job, pmxbot.config)
        assert store.get(job['id'])['status'] == 'succeeded'
    records = [cache.get_image(store.get(job)['result_image_id']) for job in jobs]
    assert records[0]['id'] != records[1]['id']
    assert records[0]['cache_key'] != records[1]['cache_key']
    assert records[0]['prompt'] == records[1]['prompt']
    for record in records:
        assert record['parent_image_id'] is None
        assert 'Continue the source' not in record['prompt']
        assert values['artist_description'] in record['prompt']
        metadata = json.loads(record['generation_metadata_json'])
        assert metadata['operation'] == 'create_new'
        assert metadata['generation_inputs']['source_image_id'] == 0
        for field in draft(cache).creative_fields:
            assert metadata['generation_inputs'][field] == values[field]
        assert record['requested_by'] == 'Alice'
    for call in post.call_args_list:
        assert 'files' not in call.kwargs
        if api == 'responses':
            assert 'previous_response_id' not in call.kwargs['json']
        else:
            assert call.args[0].endswith('/images/generations')
    retry = request(method='POST', data=data, accept='application/json')
    assert json.loads(retry['body'])['id'] == jobs[-1]
    assert post.call_count == 2
    assert cache.get_image(42) == original
    assert len(cache.album_image_history(42)) == 3


@pytest.mark.parametrize('api', ['images', 'responses'])
def test_edit_uses_selected_child_and_repeated_edit_is_fresh(variation, api):
    cache, request, post, client, values, source = variation
    if api == 'responses':
        enable_responses(cache, post)
    store = JobStore(cache.database)
    request(method='POST', data=submission(values, 'edit'))
    process_job(store, store.claim(), pmxbot.config)
    child = cache.album_image_history(42)[0]
    selected = json.loads(
        request(
            '/albums/42?source_image_id=' + str(child['id']), accept='application/json'
        )['body']
    )
    assert selected['image_id'] == str(child['id'])
    assert selected['variation_available']
    if api == 'images':

        def assert_selected_png(*args, **kwargs):
            assert (
                kwargs['files']['image[]'][1].name
                == cache.get_image(child['id'])['local_filename']
            )
            return post.return_value

        post.side_effect = assert_selected_png
    for _ in range(2):
        response = request(
            method='POST',
            data=submission(
                dict(
                    values,
                    source_image_id=selected['image_id'],
                    description='Changed cue',
                ),
                'edit',
            ),
        )
        assert response['status'] == 303  # Non-JavaScript fallback.
        job = store.claim()
        assert job['source_image_id'] == child['id']
        process_job(store, job, pmxbot.config)
        result = cache.get_image(store.get(job['id'])['result_image_id'])
        assert result['parent_image_id'] == child['id']
        metadata = json.loads(result['generation_metadata_json'])
        assert metadata['operation'] == 'edit'
        assert metadata['generation_inputs']['description'] == 'Changed cue'
        if api == 'responses':
            assert post.call_args.kwargs['json']['previous_response_id'] == 'resp_child'
    assert post.call_count == 3
    html = request()['body']
    assert "form.elements.source_image_id.value = String(data.image_id)" in html
    assert 'event.submitter' in html
    assert "body.set('operation', button.value)" in html
    assert "querySelectorAll('button[type=submit]').forEach" in html


def test_responses_legacy_edit_error_leaves_create_available(variation):
    cache, request, post, client, values, source = variation
    pmxbot.config.update(image_api='responses', responses_model='gpt-5')
    soup = BeautifulSoup(request()['body'], 'html.parser')
    assert soup.select_one('[value=edit]').has_attr('disabled')
    assert not soup.select_one('[value=create_new]').has_attr('disabled')
    assert 'legacy image has no Responses context' in soup.get_text()
    response = request(
        method='POST', data=submission(values, 'edit'), accept='application/json'
    )
    assert response['status'] == 409
    assert (
        'legacy image has no Responses context' in json.loads(response['body'])['error']
    )
    post.assert_not_called()


def test_create_new_without_artwork(variation):
    cache, request, post, client, values, source = variation
    album = MusicLibrary(cache.database).create_album('Band', 'New release')
    target = '/albums/' + str(album['id'])
    soup = BeautifulSoup(request(target)['body'], 'html.parser')
    assert soup.select_one('[value=edit]').has_attr('disabled')
    assert not soup.select_one('[value=create_new]').has_attr('disabled')
    data = submission(dict(values, source_image_id=''), 'create_new')
    response = request(target, method='POST', data=data)
    assert response['status'] == 303
    job_page = request(response['headers']['Location'][4:])
    job_soup = BeautifulSoup(job_page['body'], 'html.parser')
    assert job_soup.find('a', string='Return to album')['href'] == '/bot' + target
    assert 'Operation: Create New' in job_soup.get_text()
    store = JobStore(cache.database)
    process_job(store, store.claim(), pmxbot.config)
    assert len(cache.album_image_history(album['id'])) == 1


@pytest.mark.parametrize('operation', ['create_new', 'edit'])
def test_explicit_validation_and_csrf(variation, operation):
    cache, request, post, client, values, source = variation
    data = submission(values, operation)
    assert request(method='POST', data=dict(data, csrf_token='wrong'))['status'] == 403
    assert request(method='POST', data=dict(data, genre='invalid'))['status'] == 400
    assert (
        request(method='POST', data=dict(data, submission_token='bad'))['status'] == 400
    )
    assert (
        request(method='POST', data=dict(values, operation=operation))['status'] == 400
    )
    post.assert_not_called()


def test_token_atomic_reuse_and_input_conflict(variation):
    cache, request, post, client, values, source = variation
    store = JobStore(cache.database)
    token = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(
            pool.map(
                lambda _: store.enqueue(
                    draft(cache),
                    cache,
                    operation='create_new',
                    submission_token=token,
                ),
                range(8),
            )
        )
    assert len(set(identifiers)) == 1
    data = submission(values, 'create_new')
    assert request(method='POST', data=data)['status'] == 303
    assert (
        request(method='POST', data=dict(data, description='changed'))['status'] == 409
    )
    assert request(method='POST', data=dict(data, operation='edit'))['status'] == 409
    post.assert_not_called()
