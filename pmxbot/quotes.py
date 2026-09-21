import logging
import operator
import random
from collections.abc import Mapping
from typing import ClassVar

import pmxbot

from . import storage
from .core import CommandHandler, Handler, command

log = logging.getLogger(__name__)


class Quotes(storage.SelectableStorage):
    lib = 'pmx'
    _command_handlers: ClassVar[list] = []

    @classmethod
    def initialize(cls):
        if hasattr(cls, "store"):
            cls.finalize()
        cls.store = cls.from_URI()
        if cls.finalize not in cls._finalizers:
            cls._finalizers.append(cls.finalize)

    @classmethod
    def finalize(cls):
        cls.clear_commands()
        if hasattr(cls, 'store'):
            cls.store.close()
            del cls.store

    @classmethod
    def clear_commands(cls):
        owned = {id(handler) for handler in cls._command_handlers}
        Handler._registry[:] = [h for h in Handler._registry if id(h) not in owned]
        cls._command_handlers = []

    @classmethod
    def register_commands(cls):
        cls.clear_commands()
        libraries = pmxbot.config.get('quote_libraries', {})
        if not isinstance(libraries, Mapping):
            log.error('quote_libraries must be a mapping of commands to libraries')
            return
        names = {'quote', 'q'} | {
            h.name.lower() for h in Handler._registry if isinstance(h, CommandHandler)
        }
        for name, library in libraries.items():
            if (
                not isinstance(name, str)
                or not name
                or any(c.isspace() for c in name)
                or '!' in name
                or not isinstance(library, str)
                or not library.strip()
            ):
                log.error('Invalid quote library command: %r = %r', name, library)
                continue
            name = name.lower()
            if name in names:
                log.error(
                    'Quote library command conflicts with existing command: %s', name
                )
                continue
            handler = CommandHandler(
                name=name,
                doc=f'Quotes in {library}: random, search [number], add: text, del: search [number].',
            )
            handler.decorate(cls.library_command(library))
            cls._command_handlers.append(handler)
            names.add(name)

    @staticmethod
    def library_command(library):
        def configured_quote(rest):
            return quote_command(rest, library=library)

        return configured_quote

    @staticmethod
    def split_num(lookup):
        prefix, _, num = lookup.rpartition(' ')
        if not prefix or not num.isdigit():
            return lookup, 0
        return prefix, int(num)

    def lookup(self, rest='', *, library=None):
        rest = rest.strip()
        return self.lookup_with_num(*self.split_num(rest), library=library)

    def resolve_library(self, library):
        return self.lib if library is None else library

    @staticmethod
    def select_quote(results, num):
        n = len(results)
        if not n:
            return '', 1, 0
        i = int(num) - 1 if num else random.randrange(n)
        if not 0 <= i < n:
            raise IndexError('Quote number out of range')
        return results[i], i + 1, n

    def deletion_match(self, results, num):
        if num:
            if not results:
                raise IndexError('Quote number out of range')
            return self.select_quote(results, num)[0]
        if len(results) != 1:
            raise ValueError('Deletion requires exactly one matching quote')
        return results[0]


class SQLiteQuotes(Quotes, storage.SQLiteStorage):
    def init_tables(self):
        CREATE_QUOTES_TABLE = '''
            CREATE TABLE
            IF NOT EXISTS quotes (
                quoteid INTEGER NOT NULL,
                library VARCHAR NOT NULL,
                quote TEXT NOT NULL,
                PRIMARY KEY (quoteid)
            )
            '''
        CREATE_QUOTES_INDEX = '''
            CREATE INDEX
            IF NOT EXISTS ix_quotes_library
            on quotes(library)
            '''
        CREATE_QUOTE_LOG_TABLE = '''
            CREATE TABLE IF NOT EXISTS quote_log (quoteid varchar, logid INTEGER)
            '''
        self.db.execute(CREATE_QUOTES_TABLE)
        self.db.execute(CREATE_QUOTES_INDEX)
        self.db.execute(CREATE_QUOTE_LOG_TABLE)
        self.db.commit()

    def find_matches(self, thing, *, library=None):
        words = thing.strip().lower().split()
        query = 'SELECT quoteid, quote FROM quotes WHERE library = ?'
        query += ' AND quote LIKE ?' * len(words)
        query += ' ORDER BY quoteid'
        params = [self.resolve_library(library)] + [f'%{word}%' for word in words]
        return self.db.execute(query, params).fetchall()

    def lookup_with_num(self, thing='', num=0, *, library=None):
        results = [row[1] for row in self.find_matches(thing, library=library)]
        return self.select_quote(results, num)

    def delete(self, lookup, *, library=None):
        thing, num = self.split_num(lookup.strip())
        result = self.deletion_match(self.find_matches(thing, library=library), num)
        self.db.execute('DELETE FROM quote_log WHERE quoteid = ?', (result[0],))
        self.db.execute('DELETE FROM quotes WHERE quoteid = ?', (result[0],))
        self.db.commit()

    def add(self, quote, *, library=None):
        lib = self.resolve_library(library)
        quote = quote.strip()
        if not quote:
            # Do not add empty quotes
            return
        ADD_QUOTE_SQL = 'INSERT INTO quotes (library, quote) VALUES (?, ?)'
        res = self.db.execute(ADD_QUOTE_SQL, (lib, quote))
        quoteid = res.lastrowid
        query = 'SELECT id, message FROM LOGS order by datetime desc limit 1'
        last_message = self.db.execute(query).fetchone()
        if last_message and quote in last_message[1]:
            log_id = last_message[0]
            query = 'INSERT INTO quote_log (quoteid, logid) VALUES (?, ?)'
            self.db.execute(query, (quoteid, log_id))
        self.db.commit()

    def __iter__(self):
        # Note: also filter on quote not null, for backward compatibility
        query = "SELECT quote FROM quotes WHERE library = ? and quote is not null"
        for row in self.db.execute(query, [self.lib]):
            yield {'text': row[0]}

    def export_all(self):
        query = """
            SELECT quote, library, logid
            from quotes
            left outer join quote_log on quotes.quoteid = quote_log.quoteid
            """
        fields = 'text', 'library', 'log_id'
        return (dict(zip(fields, res)) for res in self.db.execute(query))


class MongoDBQuotes(Quotes, storage.MongoDBStorage):
    collection_name = 'quotes'

    def find_matches(self, thing, *, library=None):
        thing = thing.strip().lower()
        words = thing.split()

        def matches(quote):
            quote = quote.lower()
            return all(word in quote for word in words)

        return [
            row
            for row in self.db.find({'library': self.resolve_library(library)}).sort(
                '_id'
            )
            if matches(row['text'])
        ]

    def lookup_with_num(self, thing='', num=0, *, library=None):
        results = list(
            map(operator.itemgetter('text'), self.find_matches(thing, library=library))
        )
        return self.select_quote(results, num)

    def delete(self, lookup, *, library=None):
        lookup, num = self.split_num(lookup.strip())
        result = self.deletion_match(self.find_matches(lookup, library=library), num)
        self.db.delete_one({'_id': result['_id']})

    def add(self, quote, *, library=None):
        quote = quote.strip()
        if not quote:
            return
        quote_id = self.db.insert_one(
            {'library': self.resolve_library(library), 'text': quote}
        ).inserted_id
        newest_first = [('_id', storage.pymongo.DESCENDING)]
        last_message = self.db.database.logs.find_one(sort=newest_first)
        if last_message and quote in last_message['message']:
            self.db.update_one(
                {'_id': quote_id}, {'$set': {'log_id': last_message['_id']}}
            )

    def __iter__(self):
        return self.db.find({'library': self.lib})

    def _build_log_id_map(self):
        from . import logging

        if not hasattr(logging.Logger, 'log_id_map'):
            log_db = self.db.database.logs
            logging.Logger.log_id_map = {
                logging.MongoDBLogger.extract_legacy_id(rec['_id']): rec['_id']
                for rec in log_db.find(projection=[])
            }
        return logging.Logger.log_id_map

    def import_(self, quote):
        log_id_map = self._build_log_id_map()
        log_id = quote.pop('log_id', None)
        log_id = log_id_map.get(log_id, log_id)
        if log_id is not None:
            quote['log_id'] = log_id
        self.db.insert_one(quote)


@command(aliases='q')
def quote(rest):
    """
    If passed with nothing then get a random quote. If passed with some
    string then search for that. If prepended with "add:" then add it to the
    db, eg "!quote add: drivers: I only work here because of pmxbot!".
    Delete an individual quote by prepending "del:" and passing a search
    matching exactly one query.
    """
    return quote_command(rest)


def quote_command(rest, *, library=None):
    rest = rest.strip()
    label = 'Quote' if library is None else library
    action, _, argument = rest.partition(' ')
    if action in ('add:', 'add'):
        if not argument.strip():
            return 'No quote added: text is empty.'
        Quotes.store.add(argument, library=library)
        return f'{label} added!'
    if action in ('del:', 'del'):
        if not argument.strip():
            return 'Deletion requires a search.'
        try:
            Quotes.store.delete(argument, library=library)
        except (ValueError, IndexError) as exc:
            return str(exc)
        label = 'quote' if library is None else library
        return f'Deleted the sole {label} that matched'
    try:
        qt, i, n = Quotes.store.lookup(rest, library=library)
    except IndexError as exc:
        return str(exc)
    if qt:
        return f'({i}/{n}): {qt}'
