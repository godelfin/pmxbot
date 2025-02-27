import random
import operator

from . import storage
from .core import command
from . import quotes


class QuotesPlus(quotes.Quotes):
    """
    A version of Quotes module that allows quotes to be referenced using "library" keys.
    For instance, quotes can be stored as "albums" or "bands" and can be looked up later.
    """
    @classmethod
    def initialize(cls):
        cls.store = cls.from_URI()
        cls._finalizers.append(cls.finalize)

    @classmethod
    def finalize(cls):
        cls.store.close()
        del cls.store

    @staticmethod
    def split_num(lookup):
        prefix, _, num = lookup.rpartition(' ')
        if not prefix or not num.isdigit():
            return lookup, 0
        return prefix, int(num)

    def lookup(self, lib, rest=''):
        rest = rest.strip()
        return self.lookup_with_num(lib, *self.split_num(rest))


class SQLiteQuotesPlus(QuotesPlus, storage.SQLiteStorage):
    """
    SQLite implentation of QuotesPlus
    """
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

    def lookup_with_num(self, lib, thing='', num=0):
        BASE_SEARCH_SQL = """
            SELECT quoteid, quote
            FROM quotes
            WHERE library = ? %s order by quoteid
            """
        thing = thing.strip().lower()
        num = int(num)
        if thing:
            wtf = ' AND %s' % (
                ' AND '.join(["quote like '%%%s%%'" % x for x in thing.split()])
            )
            SEARCH_SQL = BASE_SEARCH_SQL % wtf
        else:
            SEARCH_SQL = BASE_SEARCH_SQL % ''
        results = [x[1] for x in self.db.execute(SEARCH_SQL, (lib,)).fetchall()]
        n = len(results)
        if n > 0:
            if num:
                i = num - 1
            else:
                i = random.randrange(n)
            quote = results[i]
        else:
            i = 0
            quote = ''
        return (quote, i + 1, n)

    def add(self, lib, quote):
        quote = quote.strip()
        if not quote:
            # Do not add empty quotes
            return
        ADD_QUOTE_SQL = 'INSERT INTO quotes (library, quote) VALUES (?, ?)'
        res = self.db.execute(ADD_QUOTE_SQL, (lib, quote))
        quoteid = res.lastrowid
        query = 'SELECT id, message FROM LOGS order by datetime desc limit 1'
        log_id, log_message = self.db.execute(query).fetchone()
        if quote in log_message:
            query = 'INSERT INTO quote_log (quoteid, logid) VALUES (?, ?)'
            self.db.execute(query, (quoteid, log_id))
        self.db.commit()

    # def __iter__(self):
    #     # Note: also filter on quote not null, for backward compatibility
    #     query = "SELECT quote FROM quotes WHERE library = ? and quote is not null"
    #     for row in self.db.execute(query, [self.lib]):
    #         yield {'text': row[0]}

    def export_all(self):
        query = """
            SELECT quote, library, logid
            from quotes
            left outer join quote_log on quotes.quoteid = quote_log.quoteid
            """
        fields = 'text', 'library', 'log_id'
        return (dict(zip(fields, res)) for res in self.db.execute(query))


class MongoDBQuotes(QuotesPlus, storage.MongoDBStorage):
    collection_name = 'quotes'

    def find_matches(self, lib, thing):
        thing = thing.strip().lower()
        words = thing.split()

        def matches(quote):
            quote = quote.lower()
            return all(word in quote for word in words)

        return [
            row
            for row in self.db.find(dict(library=lib)).sort('_id')
            if matches(row['text'])
        ]

    def lookup_with_num(self, lib, thing='', num=0):
        by_text = operator.itemgetter('text')
        results = list(map(by_text, self.find_matches(lib, thing)))

        n = len(results)
        if n > 0:
            if num:
                i = num - 1
            else:
                i = random.randrange(n)
            quote = results[i]
        else:
            i = 0
            quote = ''
        return (quote, i + 1, n)

    def delete(self, lib, lookup):
        """
        If exactly one quote matches, delete it. Otherwise,
        raise a ValueError.
        """
        lookup, num = self.split_num(lookup)
        if num:
            result = self.find_matches(lib, lookup)[num - 1]
        else:
            (result,) = self.find_matches(lib, lookup)
        self.db.delete_one(result)

    def add(self, lib, quote):
        """add a mongo quote to a lib"""
        quote = quote.strip()
        quote_id = self.db.insert_one(dict(library=lib, text=quote))
        # see if the quote added is in the last IRC message logged
        newest_first = [('_id', storage.pymongo.DESCENDING)]
        last_message = self.db.database.logs.find_one(sort=newest_first)
        if last_message and quote in last_message['message']:
            self.db.update_one(
                {'_id': quote_id}, {'$set': dict(log_id=last_message['_id'])}
            )

    # def __iter__(self):
    #     return self.db.find(dict(library=self.lib))

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


def quote_command(lib, rest):
    """
    If passed with nothing then get a random quote. If passed with some
    string then search for that. If prepended with "add:" then add it to the
    db, eg "!quote add: drivers: I only work here because of pmxbot!".
    Delete an individual quote by prepending "del:" and passing a search
    matching exactly one query.
    """
    rest = rest.strip()
    if rest.startswith('add: ') or rest.startswith('add '):
        quote_to_add = rest.split(' ', 1)[1]
        QuotesPlus.store.add(lib, quote_to_add)
        quot = False
        return f'{lib} added!'
    if rest.startswith('del: ') or rest.startswith('del '):
        _, _, lookup = rest.partition(' ')
        QuotesPlus.store.delete(lookup)
        return f'Deleted the sole {lib} that matched'
    quot, i, num = QuotesPlus.store.lookup(rest)
    if not quot:
        return
    return f'({i}/{num}): {quot}'


@command
def album(rest):
    """ !album commond """
    return quote_command('album', rest)

@command
def band(rest):
    """ !band commond """
    return quote_command('band', rest)

@command
def song(rest):
    """ !song commond """
    return quote_command('song', rest)

@command
def robjob(rest):
    """ !robjob commond """
    return quote_command('robjob', rest)

@command
def food(rest):
    """ !food commond """
    return quote_command('food', rest)

@command
def tagline(rest):
    """ !tagline commond """
    return quote_command('tagline', rest)
