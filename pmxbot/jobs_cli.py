"""Read-only administrator inspection of the durable generation queue."""

import argparse
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

import yaml

from .dictlib import ConfigDict
from .images import ImageError, image_database_path
from .jobs import JobStore

STATUSES = ('queued', 'running', 'succeeded', 'failed')
SETTINGS = (
    'image_api',
    'images_model',
    'images_size',
    'images_quality',
    'responses_model',
    'responses_store',
)


class QueueInspectionError(ValueError):
    pass


FIELDS = (
    'id',
    'kind',
    'status',
    'album_id',
    'source_image_id',
    'user_id',
    'requested_by',
    'channel',
    'created_at',
    'started_at',
    'completed_at',
    'result_image_id',
    'error',
    'delivered_at',
)


def inspect_queue(database, status=None, kind=None, limit=50, identifier=None):
    """Counts and selected rows come from one read transaction; never migrate."""
    store = JobStore(database)
    if not database.is_file():
        raise QueueInspectionError(
            'Database does not exist; check its configured path.'
        )
    counts = dict.fromkeys(STATUSES, 0)
    try:
        db = store.read_connection()
    except LookupError:
        if identifier:
            raise QueueInspectionError('Unknown generation job.') from None
        return counts, []
    with closing(db):
        db.execute('BEGIN')
        counts.update(
            {
                row['status']: row['total']
                for row in db.execute(
                    'SELECT status, COUNT(*) AS total FROM generation_jobs GROUP BY status'
                )
            }
        )
        columns = {
            row['name'] for row in db.execute('PRAGMA table_info(generation_jobs)')
        }
        conditions, parameters = [], []
        if identifier:
            conditions.append('id LIKE ?')
            parameters.append(identifier + '%')
        else:
            if status == 'pending':
                conditions.append("status IN ('queued', 'running')")
            elif status:
                conditions.append('status = ?')
                parameters.append(status)
            if kind:
                conditions.append(
                    ('kind' if 'kind' in columns else "'variation'") + ' = ?'
                )
                parameters.append(kind)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        ordering = 'ASC' if status == 'pending' else 'DESC'
        parameters.append(2 if identifier else limit)
        rows = db.execute(
            'SELECT * FROM generation_jobs'
            + where
            + f' ORDER BY created_at {ordering}, id {ordering} LIMIT ?',
            parameters,
        ).fetchall()
    if identifier and len(rows) != 1:
        raise QueueInspectionError(
            'Job prefix is ambiguous; use more characters.'
            if rows
            else 'Unknown generation job.'
        )
    jobs = []
    for row in rows:
        record = dict(row)
        item = {field: record.get(field) for field in FIELDS}
        item['kind'] = record.get('kind', 'variation')
        settings = json.loads(record['settings_json'])
        if not isinstance(settings, dict):
            raise QueueInspectionError('Saved job settings are invalid.')
        item['settings'] = {key: settings[key] for key in SETTINGS if key in settings}
        jobs.append(item)
    return counts, jobs


def plain(value):
    """Keep terminal fields on one line, including stored user attribution."""
    text = '-' if value is None or value == '' else str(value)
    return ''.join(
        char if not unicodedata.category(char).startswith('C') else ' ' for char in text
    )


def positive(value):
    number = int(value)
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError('limit must be between 1 and 1000')
    return number


def job_id(value):
    if not re.fullmatch('[0-9a-f]{8,32}', value):
        raise argparse.ArgumentTypeError(
            'use a job ID or its first 8–32 hexadecimal characters'
        )
    return value


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', nargs='?', help='bot/viewer configuration YAML')
    parser.add_argument(
        '--database', type=Path, help='SQLite file instead of a configuration file'
    )
    parser.add_argument('--status', choices=(*STATUSES, 'pending'))
    parser.add_argument('--kind', choices=('music', 'variation'))
    parser.add_argument('--limit', type=positive, default=50)
    parser.add_argument(
        '--job', type=job_id, help='inspect one job by full ID or unique prefix'
    )
    parser.add_argument(
        '--json', action='store_true', help='machine-readable counts and jobs'
    )
    args = parser.parse_args(argv)
    if bool(args.config) == bool(args.database):
        parser.error('provide either a configuration file or --database')
    if args.job and (args.status or args.kind):
        parser.error('--job cannot be combined with --status or --kind')
    try:
        database = (
            args.database.expanduser().resolve()
            if args.database
            else image_database_path(ConfigDict.from_yaml(args.config))
        )
        counts, jobs = inspect_queue(
            database, args.status, args.kind, args.limit, args.job
        )
    except (
        OSError,
        sqlite3.Error,
        yaml.YAMLError,
        ImageError,
        ValueError,
        TypeError,
    ) as exc:
        # Configuration/SQLite exceptions can contain credentials or stored input.
        message = (
            str(exc)
            if isinstance(exc, QueueInspectionError)
            else 'Could not read generation queue; check configuration and database access.'
        )
        parser.exit(1, message + '\n')
    if args.json:
        print(json.dumps(dict(counts=counts, jobs=jobs), ensure_ascii=True))
    elif args.job:
        job = jobs[0]
        for field in FIELDS:
            print(f'{field}: {plain(job[field])}')
        for key, value in job['settings'].items():
            print(f'{key}: {plain(value)}')
    else:
        print('  '.join(f'{status}: {counts[status]}' for status in STATUSES))
        print(
            f"{'JOB':12}  {'STATUS':9}  {'KIND':9}  {'ALBUM':6}  {'API':9}  REQUESTED BY"
        )
        for job in jobs:
            values = (
                job['id'][:12],
                job['status'],
                job['kind'],
                job['album_id'],
                job['settings'].get('image_api', 'images'),
            )
            print(
                '  '.join(
                    f'{plain(value):{width}}'
                    for value, width in zip(values, (12, 9, 9, 6, 9))
                )
                + '  '
                + plain(job['requested_by'])
            )
        if not jobs:
            print('No matching generation jobs.')


if __name__ == '__main__':
    run()
