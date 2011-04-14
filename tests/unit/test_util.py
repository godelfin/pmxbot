import py.test
from pmxbot.util import *

def pytest_funcarg__mongodb_uri(request):
	test_host = 'mongodb://localhost'
	try:
		import pymongo
		conn = pymongo.Connection(test_host)
	except Exception:
		py.test.skip("No local mongodb found")
	return test_host

def test_MongoDBKarma(mongodb_uri):
	k = MongoDBKarma(mongodb_uri)
	k.db = k.db.database.connection[k.db.database.name+'_test'][k.db.name]
	k.db.drop()
	try:
		k.change('foo', 1)
		k.change('bar', 1)
		k.set('baz', 3)
		k.set('baz', 2)
		k.link('foo', 'bar')
		assert k.lookup('foo') == 2
		k.link('foo', 'baz')
		assert k.lookup('baz') == k.lookup('foo') == 4
	finally:
		k.db.drop()

def test_MongoDBQuotes(mongodb_uri):
	q = MongoDBQuotes(mongodb_uri, 'test')
	clean = lambda: q.db.remove({'library': 'test'})
	clean()
	try:
		q.quoteAdd('who would ever say such a thing')
		q.quoteAdd('go ahead, take my pay')
		q.quoteAdd("let's do the Time Warp again")
		q.quoteLookup('time warp')
		q.quoteLookup('nonexistent')
	finally:
		clean()
