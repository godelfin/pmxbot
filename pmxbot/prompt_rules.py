"""Persistent prompt clarifications and their single runtime evaluator."""

import json
import re
import sqlite3
from contextlib import closing

import pmxbot
from .users import UserError, UserStore


_LEGACY_CONTEXTS = (
    (
        r"ass|tits?|penis|cock|dick|pussy|cunt|boobs?|breasts?|strippers?|vagina|genitals?",
        "Keep the artwork non-explicit, with no nudity or sexual anatomy.",
    ),
    (
        r"anal|sex|sexual|sexy|fuck|fucking|blowjobs?|handjobs?|gangbangs?|orgy|"
        r"orgies|porn|cum|semen|ejaculation|masturbation|incest|bestiality|goon(?:ers?)?|"
        r"dick[-\s]+sucking|pussy[-\s]+licking|jizz",
        "Do not depict sexual acts or explicit sexual content.",
    ),
    (
        r"rape|raped|raping|molest|molestation|assault",
        "Do not depict sexual violence or abuse.",
    ),
    (
        r"fag|fags|fagged|homos|faggot|faggots|holocaust|nazi|nazis",
        "Do not depict hateful imagery, extremist symbols, or atrocities.",
    ),
)


def legacy_seeds():
    yield 'torta-album', '', r'\btortas?\b', 'Torta is slang for "thicc Latina".', {}
    yield 'torta-artist', r'\btortas?\b', '', 'Torta is slang for "thicc Latina" and describes the band members.', {}
    yield 'medve', r'\bmedve\b', '', "Medve isn't a bear, he's a middle aged white man with glasses and a beard and a lopsided grin.", {
        'scope': 'either'
    }
    for index, (words, instruction) in enumerate(_LEGACY_CONTEXTS):
        yield f'title-{index}', rf'\b(?:{words})\b', '', instruction, {
            'scope': 'joined',
            'group': 'title',
            'intro': 'The quoted band and album names are text labels for a music release.',
            'extra': (
                'The album and band names are only metaphors or figures of speech.'
                if index < 3
                else ''
            ),
        }


def text(value, limit, label):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(
            (ord(c) < 32 and c not in '\n\r\t')
            or ord(c) == 127
            or 0xD800 <= ord(c) <= 0xDFFF
            for c in value
        )
    ):
        raise UserError(
            f'{label} must be text of at most {limit} characters without control characters.'
        )
    return value


def integer(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not re.fullmatch(r'-?[0-9]{1,10}', str(value)):
        raise UserError(f'Invalid {label}.')
    result = int(value)
    if not minimum <= result <= maximum:
        raise UserError(f'Invalid {label}.')
    return result


def validate(values):
    from .music import normalize

    if set(values) != {
        'artist_pattern',
        'album_pattern',
        'instruction',
        'priority',
        'enabled',
    }:
        raise UserError('Submit only the rule fields.')
    result = {
        key: text(values[key], limit, key)
        for key, limit in (
            ('artist_pattern', 255),
            ('album_pattern', 255),
            ('instruction', 10000),
        )
    }
    if not any(normalize(result[key]) for key in ('artist_pattern', 'album_pattern')):
        raise UserError('Enter at least one artist or album condition.')
    if not result['instruction'].strip():
        raise UserError('Enter clarification text.')
    result['priority'] = integer(values['priority'], 'priority', -2147483648)
    if type(values['enabled']) is not bool:
        raise UserError('Enabled must be a boolean.')
    result['enabled'] = values['enabled']
    return result


class PromptRuleStore(UserStore):
    """Use the configured SQLite lifecycle and canonical users for authority."""

    def init_tables(self):
        super().init_tables()
        try:
            with self._transaction():
                self.db.execute(
                    """CREATE TABLE IF NOT EXISTS prompt_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seed_key TEXT UNIQUE,
                    artist_pattern TEXT NOT NULL DEFAULT '',
                    album_pattern TEXT NOT NULL DEFAULT '',
                    instruction TEXT NOT NULL CHECK(length(trim(instruction)) > 0),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    priority INTEGER NOT NULL,
                    compatibility_json TEXT NOT NULL DEFAULT '{}',
                    created_by INTEGER REFERENCES users(id),
                    updated_by INTEGER REFERENCES users(id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(length(trim(artist_pattern)) + length(trim(album_pattern)) > 0)
                )"""
                )
                self.db.execute(
                    """CREATE TABLE IF NOT EXISTS prompt_rule_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER REFERENCES users(id),
                    rule_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    before_json TEXT, after_json TEXT
                )"""
                )
                self.db.execute(
                    'CREATE TABLE IF NOT EXISTS prompt_rule_migrations (name TEXT PRIMARY KEY)'
                )
                if not self.db.execute(
                    "SELECT 1 FROM prompt_rule_migrations WHERE name = 'legacy-v1'"
                ).fetchone():
                    for priority, (
                        key,
                        artist,
                        album,
                        instruction,
                        metadata,
                    ) in enumerate(legacy_seeds()):
                        metadata['mode'] = 'regex'
                        cursor = self.db.execute(
                            'INSERT INTO prompt_rules (seed_key, artist_pattern, album_pattern, instruction, enabled, priority, compatibility_json) VALUES (?, ?, ?, ?, 1, ?, ?)',
                            (
                                key,
                                artist,
                                album,
                                instruction,
                                priority,
                                json.dumps(metadata),
                            ),
                        )
                        self.audit(
                            None,
                            cursor.lastrowid,
                            'migrate',
                            None,
                            self.get_rule(cursor.lastrowid),
                        )
                    self.db.execute(
                        "INSERT INTO prompt_rule_migrations VALUES ('legacy-v1')"
                    )
        except Exception:
            self.close()
            raise

    def list_rules(self):
        self.db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in self.db.execute(
                'SELECT * FROM prompt_rules ORDER BY priority, id'
            )
        ]

    def get_rule(self, identifier):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute(
            'SELECT * FROM prompt_rules WHERE id = ?', (identifier,)
        ).fetchone()
        if row is None:
            raise UserError('Unknown rule ID.')
        return dict(row)

    def audit(self, actor_id, identifier, action, before, after):
        self.db.execute(
            'INSERT INTO prompt_rule_audit (actor_id, rule_id, action, before_json, after_json) VALUES (?, ?, ?, ?, ?)',
            (actor_id, identifier, action, json.dumps(before), json.dumps(after)),
        )

    def mutate(self, actor, operation, identifier='', **values):
        if operation not in (
            'create',
            'update',
            'delete',
            'enable',
            'disable',
            'reorder',
        ):
            raise UserError('Invalid rule operation.')
        if operation in ('create', 'update'):
            values = validate(values)
        elif operation == 'reorder':
            if set(values) != {'priority'}:
                raise UserError('Submit only a priority to reorder.')
            values = {
                'priority': integer(values.get('priority'), 'priority', -2147483648)
            }
        elif values:
            raise UserError('Unexpected rule fields.')
        if operation == 'create' and identifier not in ('', None):
            raise UserError('A new rule must not supply an ID.')
        identifier = (
            None if operation == 'create' else integer(identifier, 'rule ID', 1)
        )
        with self._transaction():
            fresh = self.get_by_id(actor.id)
            if (
                not fresh.has_permission('manage_prompt_rules')
                or fresh.session_version != actor.session_version
            ):
                raise UserError('Prompt-rule permission is required. Sign in again.')
            before = self.get_rule(identifier) if identifier else None
            if operation == 'create':
                cursor = self.db.execute(
                    'INSERT INTO prompt_rules (artist_pattern, album_pattern, instruction, enabled, priority, created_by, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        *[
                            values[k]
                            for k in (
                                'artist_pattern',
                                'album_pattern',
                                'instruction',
                                'enabled',
                                'priority',
                            )
                        ],
                        actor.id,
                        actor.id,
                    ),
                )
                identifier = cursor.lastrowid
            elif operation == 'delete':
                self.db.execute('DELETE FROM prompt_rules WHERE id = ?', (identifier,))
            else:
                if operation in ('enable', 'disable'):
                    values = {'enabled': operation == 'enable'}
                if operation == 'update' and any(
                    values[k] != before[k] for k in ('artist_pattern', 'album_pattern')
                ):
                    values['compatibility_json'] = '{}'
                assignments = ', '.join(f'{key} = ?' for key in values)
                self.db.execute(
                    f'UPDATE prompt_rules SET {assignments}, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (*values.values(), actor.id, identifier),
                )
            after = None if operation == 'delete' else self.get_rule(identifier)
            self.audit(actor.id, identifier, operation, before, after)
        return identifier


def evaluate(rules, artist, album):
    """All matches, once each; ordinary patterns are NFKC/casefold literals."""
    from .music import normalize

    artist, album = normalize(artist), normalize(album)
    matches = []
    for rule in sorted(rules, key=lambda rule: (rule['priority'], rule['id'])):
        if not rule['enabled']:
            continue
        meta = json.loads(rule['compatibility_json'])
        if not isinstance(meta, dict) or (
            meta
            and (
                meta.get('mode') != 'regex'
                or meta.get('scope') not in (None, 'either', 'joined')
            )
        ):
            raise ValueError('Invalid rule compatibility metadata.')
        a, b = rule['artist_pattern'], rule['album_pattern']
        if meta.get('mode') == 'regex':
            scope = meta.get('scope')
            if scope == 'joined':
                matched = re.search(a, normalize(artist + ' ' + album))
            elif scope == 'either':
                matched = re.search(a, artist) or re.search(a, album)
            else:
                matched = (not a or re.search(a, artist)) and (
                    not b or re.search(b, album)
                )
        else:
            matched = (not normalize(a) or normalize(a) in artist) and (
                not normalize(b) or normalize(b) in album
            )
        if matched:
            matches.append(rule)
    parts, groups = [], set()
    for rule in matches:
        meta = json.loads(rule['compatibility_json'])
        group = meta.get('group')
        if group and group not in groups:
            groups.add(group)
            parts.append(meta['intro'])
            extras = dict.fromkeys(
                json.loads(other['compatibility_json']).get('extra', '')
                for other in matches
                if json.loads(other['compatibility_json']).get('group') == group
            )
            parts.extend(extra for extra in extras if extra)
        parts.append(rule['instruction'])
    return matches, ''.join(' ' + part for part in parts)


def clarifications(artist, album, database=None):
    from .images import ImageError

    try:
        with closing(
            PromptRuleStore(
                str(database or pmxbot.config.get('database', 'sqlite:pmxbot.sqlite'))
            )
        ) as store:
            rules = store.list_rules()
        return evaluate(rules, artist, album)
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, re.error):
        raise ImageError(
            'Prompt rules are unavailable; check the configured SQLite database before retrying.'
        ) from None


def legacy_result(artist, album):
    """Reconstruct only the pre-migration semantics for historical job snapshots."""
    rules = []
    for index, (key, a, b, instruction, meta) in enumerate(legacy_seeds()):
        rules.append(
            dict(
                id=index,
                priority=index,
                enabled=True,
                artist_pattern=a,
                album_pattern=b,
                instruction=instruction,
                compatibility_json=json.dumps(dict(meta, mode='regex')),
            )
        )
    return evaluate(rules, artist, album)
