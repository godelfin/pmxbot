import pytest

import pmxbot
from pmxbot import core, quotes


@pytest.fixture(params=['sqlite', 'mongodb'])
def store(request, tmp_path):
    if request.param == 'mongodb':
        uri = request.getfixturevalue('mongodb_uri')
    else:
        uri = f'sqlite:{tmp_path / "quotes.sqlite"}'
    store = quotes.Quotes.from_URI(uri)
    if request.param == 'sqlite':
        store.db.execute('CREATE TABLE logs (id INTEGER, message TEXT, datetime TEXT)')
    else:
        store.db.delete_many({'library': {'$in': ['pmx', 'album', 'song']}})
    yield store
    if request.param == 'mongodb':
        store.db.delete_many({'library': {'$in': ['pmx', 'album', 'song']}})
    store.close()


def test_library_operations(store):
    store.add('shared default')
    store.add("shared artist's first", library='album')
    store.add('shared second', library='album')
    store.add('shared song', library='song')
    assert store.lookup('shared') == ('shared default', 1, 1)
    assert store.lookup("artist's", library='album') == ("shared artist's first", 1, 1)
    assert store.lookup('shared 2', library='album') == ('shared second', 2, 2)
    assert store.lookup(library='song') == ('shared song', 1, 1)
    assert store.lookup('missing', library='album') == ('', 1, 0)
    with pytest.raises(ValueError):
        store.delete('shared', library='album')
    with pytest.raises(ValueError):
        store.delete('missing', library='album')
    with pytest.raises(IndexError):
        store.delete('missing 3', library='album')
    with pytest.raises(IndexError):
        store.delete('shared 3', library='album')
    with pytest.raises(IndexError):
        store.lookup('shared 3', library='album')
    store.delete('shared 2', library='album')
    store.delete("artist's", library='album')
    assert store.lookup(library='album')[2] == 0
    assert store.lookup()[2] == 1
    assert store.lookup(library='song')[2] == 1
    store.add('  ', library='song')
    assert store.lookup(library='song')[2] == 1
    store.lib = 'song'
    assert store.lookup()[0] == 'shared song'
    assert [row['text'] for row in store] == ['shared song']


def test_command_operations(store, monkeypatch):
    monkeypatch.setattr(quotes.Quotes, 'store', store, raising=False)
    music = quotes.Quotes.library_command('song')
    assert quotes.quote('add: default') == 'Quote added!'
    assert music('add: Blue') == 'song added!'
    assert music('Blue') == '(1/1): Blue'
    assert quotes.quote('Blue') is None
    assert music('add: ') == 'No quote added: text is empty.'
    assert music('del: ') == 'Deletion requires a search.'
    assert music('Blue 9') == 'Quote number out of range'
    assert music('del: missing') == 'Deletion requires exactly one matching quote'
    assert music('del: Blue') == 'Deleted the sole song that matched'
    assert quotes.quote('del: default') == 'Deleted the sole quote that matched'


def test_sqlite_log_link_and_cleanup(tmp_path):
    store = quotes.SQLiteQuotes(f'sqlite:{tmp_path / "log.sqlite"}')
    store.db.execute('CREATE TABLE logs (id INTEGER, message TEXT, datetime TEXT)')
    store.db.execute("INSERT INTO logs VALUES (1, 'a quote', '2026-01-01')")
    store.add('a quote', library='album')
    assert list(store.export_all()) == [
        {'text': 'a quote', 'library': 'album', 'log_id': 1}
    ]
    store.delete('a quote', library='album')
    assert store.db.execute('SELECT * FROM quote_log').fetchall() == []
    store.close()


@pytest.fixture
def command_registry(monkeypatch):
    monkeypatch.setattr(core.Handler, '_registry', [])
    monkeypatch.setattr(quotes.Quotes, '_command_handlers', [])
    monkeypatch.setattr(pmxbot, 'config', {})


def test_registration(command_registry, monkeypatch, caplog):
    existing = core.CommandHandler(name='help', doc='Help')
    existing.decorate(lambda rest: rest)
    pmxbot.config['quote_libraries'] = {
        'Music': 'song',
        'tunes': 'song',
        'quote': 'bad',
        'q': 'bad',
        'help': 'bad',
        'music': 'bad',
        'two words': 'bad',
        '!bad': 'bad',
        'empty': '',
        'invalid': 1,
    }
    monkeypatch.setattr(quotes, 'quote_command', lambda rest, library: (rest, library))
    quotes.Quotes.register_commands()
    assert {h.name for h in core.Handler._registry} == {'help', 'music', 'tunes'}
    for handler in quotes.Quotes._command_handlers:
        assert handler.func('Blue') == ('Blue', 'song')
        assert 'song' in handler.doc
        assert handler.match(f'!{handler.name} Blue', '#test')
    assert 'conflicts' in caplog.text
    assert 'Invalid' in caplog.text
    quotes.Quotes.register_commands()
    assert len(core.Handler._registry) == 3
    pmxbot.config['quote_libraries'] = {'album': 'album'}
    quotes.Quotes.register_commands()
    assert {h.name for h in core.Handler._registry} == {'help', 'album'}
    quotes.Quotes.clear_commands()
    assert core.Handler._registry == [existing]


@pytest.mark.parametrize('config', [{}, {'quote_libraries': []}])
def test_no_commands(command_registry, config):
    pmxbot.config.update(config)
    quotes.Quotes.register_commands()
    assert core.Handler._registry == []


def test_extension_registration_order(command_registry, monkeypatch):
    events = []

    class EntryPoint:
        name = 'test'

        def load(self):
            return lambda: events.append('extension')

    monkeypatch.setattr(
        core.importlib_metadata, 'entry_points', lambda **kwargs: [EntryPoint()]
    )
    monkeypatch.setattr(quotes.Quotes, 'store', object(), raising=False)
    monkeypatch.setattr(
        quotes.Quotes, 'register_commands', lambda: events.append('commands')
    )
    core._load_library_extensions()
    assert events == ['extension', 'commands']


def test_finalize_unregisters_commands(command_registry, monkeypatch, tmp_path):
    monkeypatch.setattr(quotes.Quotes, '_finalizers', [])
    pmxbot.config.update(
        database=f'sqlite:{tmp_path / "lifecycle.sqlite"}',
        quote_libraries={'album': 'album'},
    )
    try:
        quotes.Quotes.initialize()
        quotes.Quotes.register_commands()
        assert len(core.Handler._registry) == 1
        quotes.Quotes.initialize()
        quotes.Quotes.register_commands()
        assert len(core.Handler._registry) == 1
        assert len(quotes.Quotes._finalizers) == 1
    finally:
        quotes.Quotes.finalize()
    assert core.Handler._registry == []
    assert not hasattr(quotes.Quotes, 'store')


def test_mongodb_log_link(monkeypatch):
    from unittest.mock import MagicMock

    import pymongo

    monkeypatch.setattr(quotes.storage, 'pymongo', pymongo)
    store = object.__new__(quotes.MongoDBQuotes)
    store.db = MagicMock()
    store.db.insert_one.return_value.inserted_id = 'inserted-quote'
    store.db.database.logs.find_one.return_value = {
        '_id': 'logged-message',
        'message': 'Blue Monday',
    }
    store.add('Blue Monday', library='song')
    store.db.insert_one.assert_called_once_with(
        {'library': 'song', 'text': 'Blue Monday'}
    )
    store.db.update_one.assert_called_once_with(
        {'_id': 'inserted-quote'}, {'$set': {'log_id': 'logged-message'}}
    )
