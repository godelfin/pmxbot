Six Flags stock command
=======================

``!fun`` is available to every user and reports NYSE FUN (Six Flags
Entertainment Corporation) in USD. No command arguments are required.

Provider and configuration
--------------------------

The command uses `Twelve Data <https://twelvedata.com/>`_. Its
`FUN instrument page <https://twelvedata.com/markets/741234/stock/nyse/fun/historical-data>`_
lists Six Flags on NYSE with a previous closing price. The
`API documentation <https://twelvedata.com/docs>`_ specifies ``close``,
``previous_close`` and Unix ``timestamp`` for ``/quote``, and dated daily
OHLC bars for ``/time_series``. Coverage and fields were checked on
October 22, 2026; no credentialed live request was needed for the test suite.

Set ``twelve_data_api_key`` in the bot's YAML configuration, or set the
``TWELVE_DATA_API_KEY`` environment variable in the bot process. Configuration
takes precedence. Credentials are sent in an Authorization header and are
never included in IRC errors. No existing stock integration was present.

Data delay, licensing and cost
------------------------------

Twelve Data advertises real-time US equities on Basic and higher plans;
coverage can be a subset of venues rather than a consolidated exchange feed.
See its `US stock data overview <https://twelvedata.com/stocks/>`_.
Data availability and delay depend on the subscription and exchange
entitlements. The command does not claim that quotes are live: every price
includes its source time in Eastern Time. Intraday timestamps at least three
minutes old carry a ``delayed/stale Nm`` label, including quotes retrieved
from cache. A quote from a different session is rejected during trading.

Check `current pricing <https://twelvedata.com/pricing>`_ and
`business pricing <https://twelvedata.com/pricing-business>`_ before deployment.
Basic advertises 8 API credits per minute and 800 per day; each uncached
single-symbol request to these endpoints normally consumes one credit.
There are no paid requests in the tests, and installing this feature does
not purchase a subscription. Individual plans are for personal use;
IRC display or redistribution requires appropriate provider and exchange
rights. Business display access and redistribution agreements may incur
additional subscription or licensing costs; see Twelve Data's
`commercial and personal usage policy <https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage>`_.

Session selection and failures
------------------------------

``pandas_market_calendars`` supplies the NYSE regular-session schedule,
including holidays and early closes. Dates use ``America/New_York`` and
account for daylight saving time. Keep the calendar dependency current for
newly announced exchange closures. Python versions below 3.10 use the 4.x
calendar series because newer releases can fail on import with Python 3.9.

During a regular session, ``/quote`` supplies the price and previous close.
Outside it, ``/time_series`` supplies the two most recent completed daily
bars, requested with ``adjust=none`` and an exclusive ``end_date`` of the
calendar day after the target session. This includes the required closing bar
without including the following session; see the provider's
`historical-data boundary example <https://support.twelvedata.com/en/articles/5214728-getting-historical-data>`_.
Both dates must match consecutive calendar sessions. The prior bar's close
is the previous trading day's close, so premarket, weekends and holidays
retain the completed day's performance rather than resetting it to zero.
Extended hours are explicitly disabled and extended-hours quotes rejected.

For the first 15 minutes after a session closes, the command reports that a
quote is unavailable or the final close is pending, allowing delayed feeds
to complete their daily bars. It then requires the completed session's daily
bar rather than promoting an intraday quote to a final close. The timestamp
on a daily result is the calendar's regular closing time for that bar.

Prices and percentage changes use Decimal arithmetic; display values round
to cents and two percentage decimal places. Requests have connection/read
timeouts of 3.05/10 seconds. Missing, nonpositive or nonfinite prices, missing
previous closes, future timestamps, malformed data, API errors and rate limits
produce a single unavailable message without fabricated numbers.

Successful quotes are cached for 60 seconds while trading and 15 minutes
outside trading. Session changes and credential changes invalidate the cache;
failures are cached for 60 seconds to limit retries. Expired data is not
served after a failed refresh.

Example responses::

    FUN (Six Flags): $32.45 | +$1.20 (+3.84%) today | NYSE: as of Oct 21 2026 03:42 PM ET
    FUN (Six Flags): $32.45 | -$0.85 (-2.55%) at last close | Oct 21 2026 04:00 PM ET
