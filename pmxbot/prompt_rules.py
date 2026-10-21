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


MATCH_FIELDS = {'scope', 'match_text', 'match_type'}
DETAIL_FIELDS = {'instruction', 'priority', 'enabled'}


def validate(values, fields):
    from .music import normalize

    if set(values) != fields:
        raise UserError('Submit only the fields for this action.')
    result = dict(values)
    if 'scope' in fields:
        if values['scope'] not in ('band', 'album', 'either'):
            raise UserError('Choose where to match.')
        if values['match_type'] not in ('whole', 'contains'):
            raise UserError('Choose a match type.')
        result['match_text'] = text(values['match_text'], 255, 'Text to find')
        if not normalize(result['match_text']):
            raise UserError('Enter text to find.')
    if 'instruction' in fields:
        result['instruction'] = text(values['instruction'], 10000, 'Clarification')
        if not result['instruction'].strip():
            raise UserError('Enter clarification text.')
        result['priority'] = integer(values['priority'], 'priority', -2147483648)
        if type(values['enabled']) is not bool:
            raise UserError('Enabled must be a boolean.')
    return result


def structured(values):
    return json.dumps([{key: values[key] for key in sorted(MATCH_FIELDS)}])


def literal_match(condition, artist, album):
    from .music import normalize

    needle = normalize(condition['match_text'])
    names = {'band': (artist,), 'album': (album,), 'either': (artist, album)}
    if condition['match_type'] == 'contains':
        return any(needle in name for name in names[condition['scope']])
    # Unicode word characters (including underscores) delimit larger words.
    # Escape every user character; punctuation inside a phrase remains literal.
    pattern = r'(?<!\w)' + re.escape(needle) + r'(?!\w)'
    return any(re.search(pattern, name) for name in names[condition['scope']])


# Human-readable counterparts to the trusted legacy vocabulary. These are
# descriptions only; production and preview continue to share evaluate().
_LEGACY_WORDS = (
    'ass, tit, tits, penis, cock, dick, pussy, cunt, boob, boobs, breast, breasts, stripper, strippers, vagina, genital, genitals',
    'anal, sex, sexual, sexy, fuck, fucking, blowjob, blowjobs, handjob, handjobs, gangbang, gangbangs, orgy, orgies, porn, cum, semen, ejaculation, masturbation, incest, bestiality, goon, gooner, gooners, dick sucking, pussy licking, jizz',
    'rape, raped, raping, molest, molestation, assault',
    'fag, fags, fagged, homos, faggot, faggots, holocaust, nazi, nazis',
)


def describe(rule):
    """A presentation view that never carries internal compatibility patterns."""
    conditions = json.loads(rule.get('matching_json') or 'null')
    if conditions is not None:
        labels = {'band': 'Band name', 'album': 'Album title', 'either': 'Either name'}
        descriptions = [
            f"{labels[c['scope']]} {'contains text' if c['match_type'] == 'contains' else 'matches whole word/phrase'} “{c['match_text']}”"
            for c in conditions
        ]
        summary = ' AND '.join(descriptions) + '.'
    else:
        # Never interpret a stored pattern to build display text.
        meta = compatibility(rule)
        key = rule.get('seed_key')
        summary = {
            'torta-album': 'Album title matches the whole word “torta” or “tortas” (plural).',
            'torta-artist': 'Band name matches the whole word “torta” or “tortas” (plural).',
            'medve': 'Either name matches the whole word “medve”, once per prompt.',
            'title-0': 'Combined band and album names match anatomy words, including singular and plural forms.',
            'title-1': 'Combined band and album names match sexual-content words and phrases, including plural forms and phrases separated by spaces or hyphens. Phrases can span both names.',
            'title-2': 'Combined band and album names match sexual-violence words and their original word forms.',
            'title-3': 'Combined band and album names match hate-related words, including plural forms.',
        }.get(key, 'Preserved legacy word and phrase matching.')
        if key in ('title-0', 'title-1', 'title-2', 'title-3'):
            summary += ' Whole words: ' + _LEGACY_WORDS[int(key[-1])] + '.'
        if meta.get('group'):
            summary += ' Shared context (once per prompt): ' + meta['intro']
            if meta.get('extra'):
                summary += ' ' + meta['extra']
    result = {key: rule[key] for key in ('id', 'priority', 'enabled', 'instruction')}
    result['summary'] = summary
    if conditions is not None and len(conditions) == 1:
        result.update(conditions[0])
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
                columns = {
                    row[1] for row in self.db.execute('PRAGMA table_info(prompt_rules)')
                }
                if 'matching_json' not in columns:
                    self.db.execute(
                        'ALTER TABLE prompt_rules ADD COLUMN matching_json TEXT'
                    )
                if not self.db.execute(
                    "SELECT 1 FROM prompt_rule_migrations WHERE name = 'structured-v2'"
                ).fetchone():
                    # Preserve old substring semantics, including AND and blank
                    # normalized conditions. Do not touch attribution or audits.
                    from .music import normalize

                    for rule in self.list_rules():
                        if json.loads(rule['compatibility_json']):
                            continue
                        conditions = [
                            dict(
                                scope=scope,
                                match_text=rule[field],
                                match_type='contains',
                            )
                            for scope, field in (
                                ('band', 'artist_pattern'),
                                ('album', 'album_pattern'),
                            )
                            if normalize(rule[field])
                        ]
                        self.db.execute(
                            'UPDATE prompt_rules SET matching_json = ? WHERE id = ?',
                            (json.dumps(conditions), rule['id']),
                        )
                    self.db.execute(
                        "INSERT INTO prompt_rule_migrations VALUES ('structured-v2')"
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
            'replace',
            'delete',
            'enable',
            'disable',
            'reorder',
        ):
            raise UserError('Invalid rule operation.')
        if operation in ('create', 'update', 'replace'):
            fields = {
                'create': MATCH_FIELDS | DETAIL_FIELDS,
                'update': DETAIL_FIELDS,
                'replace': MATCH_FIELDS,
            }[operation]
            values = validate(values, fields)
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
                    'INSERT INTO prompt_rules (artist_pattern, instruction, enabled, priority, matching_json, created_by, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        # Retain the old table CHECK without accepting old fields.
                        'structured',
                        values['instruction'],
                        values['enabled'],
                        values['priority'],
                        structured(values),
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
                if operation == 'replace':
                    values = {
                        'matching_json': structured(values),
                        'compatibility_json': '{}',
                        'artist_pattern': 'structured',
                        'album_pattern': '',
                    }
                assignments = ', '.join(f'{key} = ?' for key in values)
                self.db.execute(
                    f'UPDATE prompt_rules SET {assignments}, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (*values.values(), actor.id, identifier),
                )
            after = None if operation == 'delete' else self.get_rule(identifier)
            self.audit(actor.id, identifier, operation, before, after)
        return identifier


def compatibility(rule):
    meta = json.loads(rule['compatibility_json'])
    if not isinstance(meta, dict) or (
        meta
        and (
            meta.get('mode') != 'regex'
            or meta.get('scope') not in (None, 'either', 'joined')
        )
    ):
        raise ValueError('Invalid rule compatibility metadata.')
    return meta


def evaluate(rules, artist, album):
    """Single production/preview evaluator, in deterministic application order."""
    from .music import normalize

    artist, album = normalize(artist), normalize(album)
    matches = []
    for rule in sorted(rules, key=lambda rule: (rule['priority'], rule['id'])):
        if not rule['enabled']:
            continue
        meta = compatibility(rule)
        a, b = rule['artist_pattern'], rule['album_pattern']
        conditions = json.loads(rule.get('matching_json') or 'null')
        if conditions is not None:
            matched = all(literal_match(c, artist, album) for c in conditions)
        elif meta.get('mode') == 'regex':
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
