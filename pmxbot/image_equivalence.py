"""Versioned artwork equivalence and synchronous persistent submission guard."""

import sqlite3
from contextlib import closing, contextmanager

from .music import GenerationInputs

# Increment whenever prompt construction or the interpretation of inputs changes.
VERSION = 1


def descriptor(cache, inputs):
    """Exclude form/source identity; retain exact nonempty prompt text."""
    fields = ('artist_name', 'title') + GenerationInputs.creative_fields
    return dict(
        version=VERSION,
        album_id=inputs['album_id'],
        artist_id=inputs['artist_id'],
        properties={name: inputs[name] or '' for name in fields},
        api=cache.image_api,
        settings=dict(cache.settings),
        responses=(
            dict(model=cache.responses_model, store=cache.responses_store)
            if cache.image_api == 'responses'
            else None
        ),
    )


@contextmanager
def submission_guard(cache):
    """Serialize structured submissions across processes, without locking bot DB.

    SQLite releases this persistent sidecar transaction on exceptions/process exit.
    No durable running flag can strand retries. Future jobs can use the same guard
    around lookup/queue insertion, rather than holding it during worker execution.
    """
    with closing(
        sqlite3.connect(str(cache.database) + '.artwork-claims', timeout=600)
    ) as db:
        db.execute('CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
        finally:
            db.rollback()


def find_equivalent(cache, source, inputs):
    """Earliest proven match in this album: ancestor restore or same context."""
    from .images import generation_metadata, response_reference

    requested = descriptor(cache, inputs)
    with closing(cache.read_connection()) as db:
        rows = [
            dict(row)
            for row in db.execute(
                '''SELECT image_cache.* FROM image_cache JOIN album_images USING (cache_key)
            WHERE album_id = ? ORDER BY image_cache.id''',
                (inputs['album_id'],),
            )
        ]
    by_id = {row['id']: row for row in rows}
    if source['id'] not in by_id:
        return None
    ancestors = set()
    current = source
    while current and current['id'] not in ancestors:
        ancestors.add(current['id'])
        current = by_id.get(current['parent_image_id'])
    context = (
        response_reference(source) if cache.image_api == 'responses' else source['id']
    )
    for row in rows:
        saved = generation_metadata(row)
        if saved.get('effective_generation') != requested:
            continue
        if row['id'] in ancestors or (
            context is not None and saved.get('variation_context') == context
        ):
            return dict(row, reused=True)
    return None
