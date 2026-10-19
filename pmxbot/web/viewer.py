import os
import random
import calendar
import datetime
import textwrap
import html
import urllib.parse
import operator
import contextlib
import functools
import json
import sqlite3
import uuid
import re
from dataclasses import replace

import cherrypy
import jinja2.loaders
import pytz
import inflect
import importlib_resources as resources

import pmxbot.core
import pmxbot.albums
import pmxbot.styles
import pmxbot.logging
import pmxbot.util
from pmxbot.favorites import FavoriteError, FavoriteStore
from pmxbot.images import ImageCache, ImageError
from pmxbot.users import InvalidCredentials, UserError, UserStore, UsernameTaken
from pmxbot.web import auth
from pmxbot.music import GenerationInputs
from pmxbot.jobs import GenerationExecutor, JobStore
from pmxbot import creations, tendie, tendie_jobs

jenv = jinja2.Environment(loader=jinja2.loaders.PackageLoader('pmxbot.web'))
TIMEOUT = 10.0
tendie_executor = tendie_jobs.TendieExecutor(cherrypy.engine)


def wants_json():
    return 'application/json' in cherrypy.request.headers.get('Accept', '')


def json_response(data, status=200):
    cherrypy.response.status = status
    cherrypy.response.headers['Content-Type'] = 'application/json'
    cherrypy.response.headers['Cache-Control'] = 'no-store'
    return json.dumps(data).encode('utf-8')


colors = [
    "06F",
    "900",
    "093",
    "F0C",
    "C30",
    "0C9",
    "666",
    "C90",
    "C36",
    "F60",
    "639",
    "630",
    "966",
    "69C",
    "039",
    '7e1e9c',
    '15b01a',
    '0343df',
    'ff81c0',
    '653700',
    'e50000',
    '029386',
    'f97306',
    'c20078',
    '75bbfd',
]
random.shuffle(colors)


def get_context():
    c = pmxbot.config
    d = dict(
        request=cherrypy.request,
        name=c.bot_nickname,
        config=c,
        base=c.web_base,
        logo=c.logo,
    )
    d['current_user'] = auth.current_user()
    d['csrf_token'] = auth.csrf_token()
    if 'web byline' in c:
        d['byline'] = c['web byline']
    return d


COLLECTION_SORTS = ('artist', 'album', 'chronology', 'variations', 'genre')
GALLERY_SORTS = {
    'band': ('artist', 'asc'),
    'date_asc': ('chronology', 'asc'),
    'date_desc': ('chronology', 'desc'),
}

COLLECTION_PAGE_SIZE = 24
SORT_OPTIONS = (
    ('chronology:desc', 'Newest first'),
    ('chronology:asc', 'Oldest first'),
    ('artist:asc', 'Band A–Z'),
    ('artist:desc', 'Band Z–A'),
    ('album:asc', 'Album A–Z'),
    ('album:desc', 'Album Z–A'),
    ('variations:desc', 'Most image variations'),
    ('variations:asc', 'Fewest image variations'),
    ('genre:asc', 'Genre A–Z'),
    ('genre:desc', 'Genre Z–A'),
)


def collection_sort(sort, order, view):
    if isinstance(sort, str) and sort in dict(SORT_OPTIONS):
        sort, direction = sort.split(':')
        return sort, direction
    if isinstance(sort, str) and sort in GALLERY_SORTS:
        sort, direction = GALLERY_SORTS[sort]
        return sort, order if order is not None else direction
    if order is None:
        order = 'asc' if view == 'gallery' and sort in ('album', 'genre') else 'desc'
    return sort, order


def collection_filters(url):
    """Decode a validated results URL with the same defaults as its collection view."""
    parsed = urllib.parse.urlsplit(url)
    params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    view = 'gallery' if parsed.path.endswith('/gallery') else 'list'
    sort, order = collection_sort(
        params.get('sort', 'chronology'), params.get('order'), view
    )
    return dict(
        q=params.get('q', '').strip(),
        artist=params.get('artist', '').strip(),
        album_title=params.get('album_title', '').strip(),
        sort=sort,
        order=order,
    )


def results_return_to(value):
    """Allow only collection URLs, with valid query state and optional scroll."""
    fallback = pmxbot.config.web_base + '/'
    if not isinstance(value, str) or any(ord(c) < 32 for c in value) or '\\' in value:
        return fallback
    try:
        parsed = urllib.parse.urlsplit(value)
        base = pmxbot.config.web_base
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.path not in (base + '/', base + '/gallery')
        ):
            return fallback
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if set(params) - {'q', 'artist', 'album_title', 'sort', 'order', 'page'} or any(
            len(v) != 1 for v in params.values()
        ):
            return fallback
        sort = params.get('sort', ['chronology'])[0]
        allowed_sorts = (
            COLLECTION_SORTS
            + tuple(dict(SORT_OPTIONS))
            + (tuple(GALLERY_SORTS) if parsed.path.endswith('/gallery') else ())
        )
        if sort not in allowed_sorts or params.get('order', ['asc'])[0] not in (
            'asc',
            'desc',
        ):
            return fallback
        page = params.get('page', ['1'])[0]
        if not re.fullmatch(r'[0-9]{1,9}', page) or int(page) < 1:
            return fallback
        if parsed.fragment and not re.fullmatch(r'scroll=[0-9]{1,8}', parsed.fragment):
            return fallback
        return value
    except ValueError:
        return fallback


def describe_artwork_history(history):
    """Label proven roots/children; leave incomplete or legacy provenance neutral."""
    chronological = sorted(history, key=lambda item: (item['created_at'], item['id']))
    by_id = {item['id']: item for item in chronological}
    roots = [
        item['id']
        for item in chronological
        if item.get('operation') == 'create_new' and item.get('parent_image_id') is None
    ]
    labels = {
        identifier: 'Original' if len(roots) == 1 else f'Original {number}'
        for number, identifier in enumerate(roots, 1)
    }
    variations = set()
    for item in chronological:
        parent = item.get('parent_image_id')
        if parent is None:
            continue
        seen = {item['id']}
        while parent is not None and parent in by_id and parent not in seen:
            seen.add(parent)
            parent = by_id[parent].get('parent_image_id')
        if parent is None:
            variations.add(item['id'])
    number = 0
    for position, item in enumerate(chronological, 1):
        if item['id'] in variations:
            number += 1
            labels[item['id']] = f'Variation {number}'
        elif item['id'] not in labels:
            labels[item['id']] = f'Image {position}'
    return [
        dict(
            item,
            history_label=labels[item['id']],
            parent_label=(
                labels.get(item.get('parent_image_id'))
                if item['id'] in variations
                else None
            ),
            image_url=safe_image_url(item['hosted_url']),
            thumb_url=safe_image_url(item.get('thumb_url')),
        )
        for item in history
    ]


def make_anchor(line):
    time, nick = line
    return "{}.{}".format(str(time).replace(':', '.'), nick)


def pmon(month):
    """
    P the month

    >>> print(pmon('2012-08'))
    August, 2012
    """
    year, month = month.split('-')
    return '{month_name}, {year}'.format(
        month_name=calendar.month_name[int(month)], year=year
    )


def pday(dayfmt):
    """
    P the day

    >>> print(pday('2012-08-24'))
    Friday the 24th
    """

    year, month, day = map(int, dayfmt.split('-'))
    return '{day} the {number}'.format(
        day=calendar.day_name[calendar.weekday(year, month, day)],
        number=inflect.engine().ordinal(day),
    )


class ChannelPage:
    month_ordinal = {calendar.month_name[m_ord]: m_ord for m_ord in range(1, 13)}

    @cherrypy.expose
    def default(self, channel):
        page = jenv.get_template('channel.html')

        db = pmxbot.logging.Logger.store
        context = get_context()
        contents = db.get_channel_days(channel)
        months = {}
        for fn in sorted(contents, reverse=True):
            mon_des, day = fn.rsplit('-', 1)
            months.setdefault(pmon(mon_des), []).append((pday(fn), fn))
        context['months'] = sorted(months.items(), key=self.by_date, reverse=True)
        context['channel'] = channel
        return page.render(**context).encode('utf-8')

    @classmethod
    def by_date(cls, month_item):
        month_string, days = month_item
        return cls.date_key(month_string)

    @classmethod
    def date_key(cls, month_string):
        """
        Return a key suitable for sorting by month.

        >>> k1 = ChannelPage.date_key('September, 2012')
        >>> k2 = ChannelPage.date_key('August, 2013')
        >>> k2 > k1
        True
        """
        month, year = month_string.split(',')
        month_ord = cls.month_ordinal[month]
        return year, month_ord


class DayPage:
    @cherrypy.expose
    def default(self, channel, day):
        page = jenv.get_template('day.html')
        db = pmxbot.logging.Logger.store
        context = get_context()
        day_logs = db.get_day_logs(channel, day)
        data = [(t, n, make_anchor((t, n)), html.escape(m)) for (t, n, m) in day_logs]
        usernames = [x[1] for x in data]
        color_map = {}
        clrs = colors[:]
        for u in usernames:
            if u not in color_map:
                try:
                    color = clrs.pop(0)
                except IndexError:
                    color = "000"
                color_map[u] = color
        context['color_map'] = color_map
        context['history'] = data
        context['channel'] = channel
        context['pdate'] = "{pday} of {days}".format(
            pday=pday(day), days=pmon(day.rsplit('-', 1)[0])
        )
        return page.render(**context).encode('utf-8')


class KarmaPage:
    @cherrypy.expose
    def default(self, term=""):
        page = jenv.get_template('karma.html')
        context = get_context()
        karma = pmxbot.karma.Karma.store
        term = term.strip()
        if term:
            context['lookup'] = self.karma_comma(karma.search(term)) or [
                ('NO RESULTS FOUND', '')
            ]
        context['top100'] = self.karma_comma(karma.list(select=100))
        context['bottom100'] = self.karma_comma(karma.list(select=-100))
        return page.render(**context).encode('utf-8')

    @staticmethod
    def karma_comma(karma_results):
        """
        (say that 5 times fast)

        Take the results of a karma query (keys, value) and return the same
        result with the keys joined by commas.
        """
        return [(', '.join(keys), value) for keys, value in karma_results]


class SearchPage:
    @cherrypy.expose
    def default(self, term=''):
        page = jenv.get_template('search.html')
        context = get_context()
        db = pmxbot.logging.Logger.store

        # a hack to enable the database to create anchors when building search
        #  results
        db.make_anchor = make_anchor

        if not term:
            raise cherrypy.HTTPRedirect(cherrypy.request.base)
        terms = term.strip().split()
        results = sorted(db.search(*terms), key=lambda x: x[1], reverse=True)
        context['search_results'] = results
        context['num_results'] = len(results)
        context['term'] = term
        return page.render(**context).encode('utf-8')


class HelpPage:
    @cherrypy.expose
    def default(self):
        page = jenv.get_template('help.html')
        return page.render(**self.get_context()).encode('utf-8')

    @staticmethod
    def get_context():
        context = get_context()
        commands = []
        contains = []
        by_name = operator.attrgetter('name')
        for handler in sorted(pmxbot.core.Handler._registry, key=by_name):
            if type(handler) is pmxbot.core.CommandHandler:
                commands.append(handler)
            elif isinstance(handler, pmxbot.core.ContainsHandler):
                contains.append(handler)
        context['commands'] = commands
        context['contains'] = contains
        return context


class LegacyPage:
    """
    Forwards legacy /day/{channel}/{date}#{time}.{nick} in local time to
    the proper page at /day (in UTC).
    """

    timezone = pytz.timezone('US/Pacific')

    @cherrypy.expose
    def default(self, channel, date_s):
        """
        Return a web page that will get the fragment out and pass it to
        us so we can parse it.
        """
        return textwrap.dedent(
            """
            <html>
            <head>
            <script type="text/javascript">
                window.onload = function() {
                    fragment = parent.location.hash;
                    window.location.pathname=window.location.pathname.replace(
                        'legacy', 'legacy/forward')
                        + "/" + window.location.hash.slice(1);
                };
            </script>
            </head>
            <body></body>
            </html>
        """
        ).lstrip()

    @cherrypy.expose
    def forward(self, channel, date_s, fragment):
        """
        Given an HREF in the legacy timezone, redirect to an href for UTC.
        """
        time_s, sep, nick = fragment.rpartition('.')
        time = datetime.datetime.strptime(time_s, '%H.%M.%S')
        date = datetime.datetime.strptime(date_s, '%Y-%m-%d')
        dt = datetime.datetime.combine(date, time.time())
        loc_dt = self.timezone.localize(dt)
        utc_dt = loc_dt.astimezone(pytz.utc)
        target_date = utc_dt.date().isoformat()
        target_time = utc_dt.time().strftime('%H.%M.%S')
        url = f'/day/{channel}/{target_date}#{target_time}.{nick}'
        raise cherrypy.HTTPRedirect(url, 301)


def safe_image_url(value):
    """Allow public HTTPS artwork URLs without credentials or URL ambiguity."""
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    if '\\' in value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return value


def image_metadata(image):
    """Expose useful provider fields, keeping paths and hosting credentials private."""
    sections = []
    for label, field, keys in (
        (
            'Settings',
            'settings_json',
            (
                'model',
                'size',
                'quality',
                'output_format',
                'api',
                'responses_model',
                'store',
            ),
        ),
        (
            'Generation metadata',
            'generation_metadata_json',
            (
                'created',
                'usage',
                'revised_prompt',
                'model',
                'size',
                'quality',
                'output_format',
                'api',
                'response_id',
                'previous_response_id',
                'store',
                'requested_model',
                'tool',
                'image_generation_call_id',
                'image_settings',
            ),
        ),
    ):
        try:
            data = json.loads(image[field])
        except (ValueError, TypeError):
            data = {}
        selected = (
            {key: data[key] for key in keys if key in data}
            if isinstance(data, dict)
            else {}
        )
        sections.append(
            (
                label,
                json.dumps(selected, ensure_ascii=False, indent=2) if selected else '',
            )
        )
    return sections


class AdminPages:
    @cherrypy.expose
    def tendies(self, csrf_token='', operation='update', page_limit='1', **params):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        auth.require_admin()
        if cherrypy.request.query_string or params:
            raise cherrypy.HTTPError(400, 'Use a form body.')
        error = None
        if cherrypy.request.method == 'POST':
            auth.require_csrf(csrf_token)
            if operation not in ('update', 'recover', 'recover_range'):
                raise cherrypy.HTTPError(400, 'Invalid tendie operation.')
            try:
                page_limit = tendie.validated_page_limit(page_limit)
            except ValueError:
                raise cherrypy.HTTPError(
                    400, 'Choose a page limit from 1 to 100.'
                ) from None
            try:
                accepted = tendie_executor.enqueue(
                    pmxbot.config, operation=operation, page_limit=page_limit
                )
            except Exception:
                raise cherrypy.HTTPError(
                    503,
                    'Tendie update is unavailable. Check configuration and storage.',
                ) from None
            if not accepted:
                cherrypy.response.status = 409
                error = 'A tendie update is already queued or running.'
            else:
                auth.redirect(pmxbot.config.web_base + '/admin/tendies')
        try:
            state = tendie_jobs.status(tendie.settings_snapshot(pmxbot.config))
        except Exception:
            raise cherrypy.HTTPError(
                503, 'Tendie storage or configuration is unavailable.'
            ) from None
        return (
            jenv.overlay(autoescape=True)
            .get_template('admin_tendies.html')
            .render(
                state=state,
                error=error,
                **get_context(),
            )
            .encode('utf-8')
        )

    @cherrypy.expose
    def tendie_status(self, **params):
        cherrypy.lib.cptools.allow(['GET', 'HEAD'])
        auth.require_admin()
        if params or cherrypy.request.query_string:
            raise cherrypy.HTTPError(400, 'Unexpected status fields.')
        try:
            state = tendie_jobs.status(tendie.settings_snapshot(pmxbot.config))
        except Exception:
            raise cherrypy.HTTPError(
                503, 'Tendie storage or configuration is unavailable.'
            ) from None
        return json_response(state)

    @cherrypy.expose
    def users(
        self,
        user_id='',
        operation='',
        role='',
        enabled='0',
        can_generate_images='0',
        csrf_token='',
    ):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        actor = auth.require_admin()
        temporary_password = error = None
        if cherrypy.request.query_string:
            raise cherrypy.HTTPError(400, 'Use a form body.')
        if cherrypy.request.method == 'POST':
            auth.require_csrf(csrf_token)
        try:
            with contextlib.closing(UserStore(pmxbot.config.database)) as store:
                if cherrypy.request.method == 'POST':
                    if not isinstance(user_id, str) or not re.fullmatch(
                        r'[0-9]{1,18}', user_id
                    ):
                        raise UserError('Invalid user ID.')
                    if operation == 'reset_password':
                        temporary_password = store.admin_reset_password(
                            actor, int(user_id)
                        )
                    elif (
                        operation == 'update'
                        and enabled in ('0', '1')
                        and can_generate_images in ('0', '1')
                    ):
                        store.admin_update(
                            actor,
                            int(user_id),
                            role=role,
                            enabled=enabled == '1',
                            can_generate_images=can_generate_images == '1',
                        )
                    else:
                        raise UserError('Invalid administration action.')
                    # Show reset secrets once, including when an admin resets themself.
                    if int(user_id) == actor.id:
                        auth.invalidate_session()
                users = store.list_users()
        except UserError as exc:
            cherrypy.response.status = 400
            error = str(exc)
            with contextlib.closing(UserStore(pmxbot.config.database)) as store:
                users = store.list_users()
        except sqlite3.Error:
            raise cherrypy.HTTPError(503, 'User storage is unavailable') from None
        cherrypy.response.headers['Referrer-Policy'] = 'no-referrer'
        return (
            jenv.overlay(autoescape=True)
            .get_template('admin_users.html')
            .render(
                users=users,
                error=error,
                temporary_password=temporary_password,
                **get_context(),
            )
            .encode('utf-8')
        )


class AlbumPage:
    @cherrypy.expose
    def default(self, *path, **params):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        requesting_user = (
            auth.require_permission('generate_images')
            if cherrypy.request.method == 'POST'
            else None
        )
        if len(path) != 1 or not path[0].isascii() or not path[0].isdecimal():
            raise cherrypy.HTTPError(404, 'Unknown album ID')
        # SQLite IDs are positive signed 64-bit integers. Bound before conversion.
        value = path[0].lstrip('0')
        if not value or len(value) > 19 or int(value) > 2**63 - 1:
            raise cherrypy.HTTPError(404, 'Unknown album ID')
        csrf = (
            params.pop('csrf_token', None)
            if cherrypy.request.method == 'POST'
            else None
        )
        explicit_operation = 'operation' in params
        operation = params.pop('operation', 'edit')
        submission_token = params.pop('submission_token', None)
        if cherrypy.request.method == 'POST':
            if operation not in ('create_new', 'edit'):
                raise cherrypy.HTTPError(400, 'Invalid artwork operation')
            if explicit_operation and submission_token is None:
                raise cherrypy.HTTPError(400, 'A submission token is required')
            if submission_token is not None and (
                not isinstance(submission_token, str)
                or not re.fullmatch('[0-9a-f]{32}', submission_token)
            ):
                raise cherrypy.HTTPError(400, 'Invalid submission token')
        return_value = params.pop('return_to', '')
        return_to = results_return_to(return_value)
        browsing_filters = collection_filters(return_to)
        return_query = (
            ('&return_to=' + urllib.parse.quote(return_to, safe=''))
            if return_value
            else ''
        )
        source_id = params.pop('source_image_id', None) or None
        if source_id is not None:
            if (
                not isinstance(source_id, str)
                or not source_id.isascii()
                or not source_id.isdecimal()
                or len(source_id) > 19
                or not 0 < int(source_id) <= 2**63 - 1
            ):
                raise cherrypy.HTTPError(400, 'Invalid source image ID')
            source_id = int(source_id)
        if (
            cherrypy.request.method == 'POST'
            and operation == 'edit'
            and source_id is None
        ):
            raise cherrypy.HTTPError(400, 'A source image is required')
        if cherrypy.request.method != 'POST' and (
            params or explicit_operation or submission_token is not None
        ):
            raise cherrypy.HTTPError(400, 'Unexpected variation fields')
        context = get_context()
        user = context['current_user']
        try:
            cache = ImageCache(pmxbot.config)
            album, image = cache.get_album_page(
                int(value), source_id, user.id if user else None
            )
            neighbors = cache.album_neighbors(
                int(value),
                include_nsfw=bool(user and user.has_permission('nsfw')),
                **browsing_filters,
            )
            image_history = cache.album_image_history(int(value))
            failures = cache.album_image_failures(int(value))
        except LookupError:
            raise cherrypy.HTTPError(404, 'Unknown album ID') from None
        except (sqlite3.Error, OSError, ImageError):
            raise cherrypy.HTTPError(503, 'Album storage is unavailable') from None
        context['pending_jobs'] = []
        try:
            context['pending_jobs'] = [
                job
                for job in JobStore(cache.database).visible(
                    context['current_user'],
                    cherrypy.session.get('generation_owner', ''),
                )
                if job['album_id'] == int(value)
                and job['status'] in ('queued', 'running')
            ]
        except LookupError:
            pass
        except (sqlite3.Error, OSError):
            raise cherrypy.HTTPError(
                503, 'Generation job storage is unavailable'
            ) from None
        context['metadata_sections'] = image_metadata(image) if image else []
        context.update(
            image=image,
            album=album,
            return_to=return_to,
            return_query=return_query,
            neighbors=neighbors,
            browsing_filtered=any(
                browsing_filters[field] for field in ('q', 'artist', 'album_title')
            ),
            navigation_sort_label=dict(SORT_OPTIONS)[
                f"{browsing_filters['sort']}:{browsing_filters['order']}"
            ],
            image_history=describe_artwork_history(image_history),
            failures=failures,
            formats=creations.generation_choices()['format'],
            artwork_styles=pmxbot.styles.ALBUM_ART_STYLES,
            artwork_style_groups=pmxbot.styles.ALBUM_ART_STYLE_GROUPS,
            format_descriptions=creations.generation_choices()['format_description'],
            genres=creations.generation_choices()['genre'],
            image_url=safe_image_url(image['hosted_url']) if image else None,
        )
        draft = GenerationInputs.from_source(album, image)
        context.update(
            draft=draft,
            variation_error=None,
            submission_token=uuid.uuid4().hex,
            create_available=pmxbot.config.get('images_enabled', False),
            variation_available=False,
            variation_message='Edit requires a selected image.',
        )
        if image:
            available, message = cache.continuation_capability(image)
            variation_available, variation_message = cache.variation_capability(image)
            if not pmxbot.config.get('images_enabled', False):
                variation_available, variation_message = (
                    False,
                    'Image generation is disabled.',
                )
            context.update(
                continuation_available=available,
                continuation_message=message,
                variation_available=variation_available,
                variation_message=variation_message,
            )
        if cherrypy.request.method == 'POST':
            try:
                draft = draft.prepare(
                    params,
                    {
                        'format': context['formats'],
                        'format_description': context['format_descriptions'],
                        'genre': context['genres'],
                        'artist_genre': context['genres'],
                    },
                )
            except ValueError as exc:
                cherrypy.response.status = 400
                context['variation_error'] = str(exc)
            else:
                context.update(draft=draft)
                if operation == 'create_new':
                    draft = replace(draft, source_image_id=0)
                auth.require_csrf(csrf)
                if not context['create_available'] or (
                    operation == 'edit' and not context['variation_available']
                ):
                    cherrypy.response.status = 409
                    context['variation_error'] = (
                        context['variation_message']
                        if context['create_available']
                        else 'Image generation is disabled.'
                    )
                else:
                    user = requesting_user
                    try:
                        identifier = JobStore(cache.database).enqueue(
                            draft,
                            cache,
                            user,
                            operation=operation,
                            submission_token=submission_token,
                        )
                    except ValueError as exc:
                        raise cherrypy.HTTPError(409, str(exc)) from None
                    except (sqlite3.Error, OSError):
                        raise cherrypy.HTTPError(
                            503, 'Generation job storage is unavailable'
                        ) from None
                    status_url = f"{context['base']}/jobs/{identifier}"
                    if wants_json():
                        return json_response(
                            dict(
                                id=identifier,
                                status_url=status_url,
                                submission_token=uuid.uuid4().hex,
                            ),
                            202,
                        )
                    auth.redirect(status_url)
        if cherrypy.request.method == 'POST' and wants_json():
            return json_response(
                dict(error=context['variation_error']), cherrypy.response.status
            )
        # Escape the entire inherited layout too, without changing legacy pages.
        page = jenv.overlay(autoescape=True).get_template('album.html')
        if cherrypy.request.method in ('GET', 'HEAD') and wants_json():
            rendering = page.new_context(context)
            names = ('artwork', 'prompt', 'metadata', 'cache_key')
            return json_response(
                dict(
                    # Preserve SQLite's 64-bit IDs across JavaScript's number limit.
                    image_id=str(image['id']) if image else None,
                    properties=(
                        {
                            field: getattr(draft, field)
                            for field in GenerationInputs.creative_fields
                        }
                        if draft
                        else {}
                    ),
                    variation_message=context['variation_message'],
                    variation_available=bool(
                        context.get('variation_available') and context['current_user']
                    ),
                    fragments={
                        name: ''.join(page.blocks['selected_' + name](rendering))
                        for name in names
                    },
                )
            )
        return page.render(**context).encode('utf-8')


class PmxbotPages:
    admin = AdminPages()

    # Only the album index, gallery, and album pages are published. Keep the other
    # page classes available for when the rest of the viewer is enabled again.
    albums = AlbumPage()

    @cherrypy.expose
    def create_album(self, **params):
        try:
            return self._create_album(**params)
        except (sqlite3.Error, OSError, ImageError):
            raise cherrypy.HTTPError(
                503, 'Album creation storage is unavailable'
            ) from None

    def _create_album(self, **params):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        user = auth.require_permission('generate_images')
        context = get_context()
        cache = ImageCache(pmxbot.config)
        library = creations.MusicLibrary(cache.database)
        errors = {}
        token = params.pop(
            'submission_token',
            None if cherrypy.request.method == 'POST' else uuid.uuid4().hex,
        )
        csrf = params.pop('csrf_token', None)
        retry = params.pop('retry', None)
        if cherrypy.request.method == 'POST':
            auth.require_csrf(csrf)
            if not pmxbot.config.get('images_enabled', False):
                raise cherrypy.HTTPError(409, 'Image generation is disabled.')
            if not creations.public_application_url(
                pmxbot.config.get('albums_url'), 'albums/1'
            ):
                raise cherrypy.HTTPError(
                    503, 'Configure albums_url before creating albums.'
                )
            channel = pmxbot.config.get('album_creation_channel') or pmxbot.config.get(
                'inane_channel'
            )
            if not isinstance(channel, str) or not re.fullmatch(
                r'[#&][^\s,\x00-\x1f\x7f]{1,100}', channel
            ):
                raise cherrypy.HTTPError(
                    503, 'Configure an album creation IRC channel.'
                )
            store = JobStore(cache.database)
            if retry:
                try:
                    store.retry_creation(retry, user)
                except LookupError:
                    raise cherrypy.HTTPError(404, 'Unknown creation job') from None
                except (ValueError, sqlite3.IntegrityError):
                    raise cherrypy.HTTPError(
                        409, 'This album already exists or is being created.'
                    ) from None
                auth.redirect(f"{context['base']}/jobs/{retry}")
            if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
                raise cherrypy.HTTPError(400, 'Invalid submission token')
            draft, errors = creations.prepare(params)
            if not errors:
                try:
                    with contextlib.closing(library.connect()):
                        pass
                    identifier = store.enqueue(
                        draft,
                        cache,
                        user,
                        operation='create_album',
                        submission_token=token,
                        channel=channel,
                    )
                except ValueError as exc:
                    errors['title'] = str(exc)
                else:
                    status_url = f"{context['base']}/jobs/{identifier}"
                    if wants_json():
                        return json_response(
                            dict(id=identifier, status_url=status_url), 202
                        )
                    auth.redirect(status_url)
            cherrypy.response.status = 400
            if wants_json():
                return json_response(dict(errors=errors), 400)
        elif params or retry:
            raise cherrypy.HTTPError(400, 'Unexpected creation fields')
        with contextlib.closing(library.connect()) as db:
            suggestions = [
                dict(row)
                for row in db.execute(
                    'SELECT artists.name AS artist, albums.title FROM artists '
                    'LEFT JOIN albums ON artists.id = albums.artist_id ORDER BY artists.normalized_name'
                )
            ]
        return (
            jenv.overlay(autoescape=True)
            .get_template('create_album.html')
            .render(
                **context,
                values=params,
                errors=errors,
                suggestions=suggestions,
                choices=creations.generation_choices(),
                submission_token=token,
                artwork_styles=pmxbot.styles.ALBUM_ART_STYLES,
                artwork_style_groups=pmxbot.styles.ALBUM_ART_STYLE_GROUPS,
            )
            .encode('utf-8')
        )

    @cherrypy.expose
    def jobs(self, identifier=None, **params):
        if cherrypy.request.method not in ('GET', 'HEAD'):
            cherrypy.response.headers['Allow'] = 'GET, HEAD'
            raise cherrypy.HTTPError(405)
        if params:
            raise cherrypy.HTTPError(400, 'Unexpected job fields')
        context = get_context()
        user = context['current_user']
        try:
            cache = ImageCache(pmxbot.config)
            store = JobStore(cache.database)
            if identifier is None:
                context['jobs'] = store.visible(
                    user, cherrypy.session.get('generation_owner', '')
                )
                context['job'] = None
            else:
                job = store.get(identifier)
                if job.get('kind', 'variation') != 'variation':
                    raise LookupError('Unknown job')
                if job['user_id'] is not None and (
                    not user or user.id != job['user_id']
                ):
                    raise LookupError('Unknown job')
                context.update(job=job, jobs=[])
        except LookupError:
            if identifier is not None:
                raise cherrypy.HTTPError(404, 'Unknown generation job') from None
            context.update(job=None, jobs=[])
        except (sqlite3.Error, OSError, ImageError):
            raise cherrypy.HTTPError(
                503, 'Generation job storage is unavailable'
            ) from None
        if identifier is not None and job['operation'] == 'create_album':
            if job['status'] == 'succeeded' and not wants_json():
                auth.redirect(f"{context['base']}/albums/{job['album_id']}")
            context['creation_inputs'] = json.loads(job['inputs_json'])
        cherrypy.response.headers['Cache-Control'] = 'no-store'
        cherrypy.response.headers['Referrer-Policy'] = 'no-referrer'
        if identifier is not None and wants_json():
            return json_response(
                dict(
                    id=job['id'],
                    status=job['status'],
                    error=job['error'],
                    result_url=(
                        f"{context['base']}/albums/{job['album_id']}?source_image_id={job['result_image_id']}"
                        if job['status'] == 'succeeded'
                        else None
                    ),
                )
            )
        return (
            jenv.overlay(autoescape=True)
            .get_template('jobs.html')
            .render(**context)
            .encode('utf-8')
        )

    @cherrypy.expose
    def torta_mode(self, enabled='0', csrf_token='', return_to=''):
        user = auth.require_authenticated_user()
        auth.require_csrf(csrf_token)
        if cherrypy.request.query_string:
            raise cherrypy.HTTPError(400, 'Use a form body.')
        if enabled not in ('0', '1'):
            raise cherrypy.HTTPError(400, 'Invalid Torta Mode setting.')
        try:
            with contextlib.closing(UserStore(pmxbot.config.database)) as store:
                store.set_nsfw(user, enabled == '1')
        except InvalidCredentials:
            auth.invalidate_session()
            raise cherrypy.HTTPError(401, 'Sign in again.') from None
        except (sqlite3.Error, UserError):
            raise cherrypy.HTTPError(503, 'User storage is unavailable') from None
        auth.redirect(auth.safe_return_to(return_to))

    @cherrypy.expose
    def favorites(self, album_id='', image_id='', csrf_token=''):
        cherrypy.lib.cptools.allow(['GET', 'POST'])
        user = auth.require_authenticated_user()
        if cherrypy.request.method == 'POST':
            auth.require_csrf(csrf_token)
            if cherrypy.request.query_string:
                raise cherrypy.HTTPError(400, 'Use a form body.')
        for value in (
            (album_id, image_id) if cherrypy.request.method == 'POST' else (album_id,)
        ):
            if (
                not isinstance(value, str)
                or not re.fullmatch(r'[0-9]{1,19}', value)
                or not 0 < int(value) <= 2**63 - 1
            ):
                raise cherrypy.HTTPError(400, 'Invalid album or image ID.')
        try:
            cache = ImageCache(pmxbot.config)
            if cherrypy.request.method == 'POST':
                favorite = FavoriteStore(cache.database).toggle(
                    user, int(album_id), int(image_id)
                )
            else:
                album, _ = cache.get_album_page(int(album_id), user_id=user.id)
                favorite = album['favorite_image_id']
        except FavoriteError as exc:
            raise cherrypy.HTTPError(400, str(exc)) from None
        except PermissionError:
            raise cherrypy.HTTPError(401, 'Sign in again.') from None
        except LookupError:
            raise cherrypy.HTTPError(404, 'Unknown album ID') from None
        except (sqlite3.Error, OSError, ImageError):
            raise cherrypy.HTTPError(503, 'Favorite storage is unavailable') from None
        return json_response(
            {'favorite_image_id': str(favorite) if favorite is not None else None}
        )

    @cherrypy.expose
    def login(self, username='', password='', csrf_token='', return_to=''):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        error = None
        if cherrypy.request.method == 'POST':
            # Credentials and tokens must be in the form body, never the URL.
            if cherrypy.request.query_string:
                raise cherrypy.HTTPError(400, 'Use a form body.')
            auth.require_csrf(csrf_token)
            auth.login_throttle.check()
            try:
                with contextlib.closing(UserStore(pmxbot.config.database)) as store:
                    user = store.authenticate(username, password)
            except InvalidCredentials:
                cherrypy.response.status = 401
                error = 'Invalid username or password.'
            except (sqlite3.Error, UserError):
                raise cherrypy.HTTPError(503, 'User storage is unavailable') from None
            else:
                cherrypy.session.clear()
                cherrypy.session.regenerate()
                cherrypy.session['user_id'] = user.id
                cherrypy.session['session_version'] = user.session_version
                if user.must_change_password:
                    auth.redirect(pmxbot.config.web_base + '/change_password')
                auth.redirect(auth.safe_return_to(return_to))
        page = jenv.overlay(autoescape=True).get_template('login.html')
        return page.render(
            error=error, return_to=auth.safe_return_to(return_to), **get_context()
        ).encode('utf-8')

    @cherrypy.expose
    def change_password(
        self, current_password='', password='', password_confirmation='', csrf_token=''
    ):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        user = auth.require_authenticated_user()
        error = None
        if cherrypy.request.query_string:
            raise cherrypy.HTTPError(400, 'Use a form body.')
        if cherrypy.request.method == 'POST':
            auth.require_csrf(csrf_token)
            auth.login_throttle.check()
            try:
                auth.validate_registration_password(password, password_confirmation)
                with contextlib.closing(UserStore(pmxbot.config.database)) as store:
                    store.change_password(user, current_password, password)
            except UserError as exc:
                cherrypy.response.status = 400
                error = str(exc)
            except sqlite3.Error:
                raise cherrypy.HTTPError(503, 'User storage is unavailable') from None
            else:
                auth.invalidate_session()
                auth.redirect(pmxbot.config.web_base + '/login')
        return (
            jenv.overlay(autoescape=True)
            .get_template('change_password.html')
            .render(error=error, **get_context())
            .encode('utf-8')
        )

    @cherrypy.expose
    def register(
        self,
        username='',
        password='',
        password_confirmation='',
        csrf_token='',
        created='',
    ):
        cherrypy.lib.cptools.allow(['GET', 'HEAD', 'POST'])
        error = None
        if cherrypy.request.method == 'POST':
            if cherrypy.request.query_string:
                raise cherrypy.HTTPError(400, 'Use a form body.')
            auth.require_csrf(csrf_token)
            auth.login_throttle.check()
            try:
                auth.validate_registration_password(password, password_confirmation)
                with contextlib.closing(UserStore(pmxbot.config.database)) as store:
                    store.create(username, password=password, enabled=True)
            except UsernameTaken:
                error = 'That username is already registered.'
            except UserError as exc:
                error = str(exc)
            except sqlite3.Error:
                raise cherrypy.HTTPError(503, 'User storage is unavailable') from None
            else:
                auth.redirect(pmxbot.config.web_base + '/register?created=1')
            cherrypy.response.status = 400
        page = jenv.overlay(autoescape=True).get_template('register.html')
        return page.render(
            error=error,
            created=cherrypy.request.method != 'POST' and created == '1',
            **get_context(),
        ).encode('utf-8')

    @cherrypy.expose
    def logout(self, csrf_token=''):
        auth.require_csrf(csrf_token)
        auth.invalidate_session()
        auth.redirect(pmxbot.config.web_base + '/')

    @cherrypy.expose
    def gallery(
        self,
        page='1',
        sort='date_desc',
        q='',
        artist='',
        album_title='',
        order=None,
        **params,
    ):
        return self.collection(
            q, artist, album_title, sort, order, page, 'gallery', **params
        )

    @cherrypy.expose
    def index(
        self,
        q='',
        artist='',
        album_title='',
        sort='chronology',
        order='desc',
        page='1',
        **params,
    ):
        return self.collection(
            q, artist, album_title, sort, order, page, 'list', **params
        )

    def collection(self, q, artist, album_title, sort, order, page, view, **params):
        cherrypy.lib.cptools.allow(['GET', 'HEAD'])
        if order is not None and order not in ('asc', 'desc'):
            raise cherrypy.HTTPError(400, 'Invalid collection order')
        sort, order = collection_sort(sort, order, view)
        if (
            params
            or any(
                not isinstance(value, str)
                for value in (q, artist, album_title, sort, order, page)
            )
            or sort not in COLLECTION_SORTS
            or order not in ('asc', 'desc')
            or not page.isascii()
            or not page.isdecimal()
            or len(page) > 9
            or int(page) < 1
        ):
            raise cherrypy.HTTPError(400, 'Invalid catalog parameters')
        q = q.strip()
        artist = artist.strip()
        album_title = album_title.strip()
        page_number = int(page)

        context = get_context()
        user = context['current_user']
        try:
            cache = ImageCache(pmxbot.config)
            albums, total = cache.catalog_albums(
                page_number,
                COLLECTION_PAGE_SIZE,
                q,
                artist,
                album_title,
                sort,
                order,
                user_id=user.id if user else None,
                include_nsfw=bool(user and user.has_permission('nsfw')),
            )
            leaderboard = cache.generation_leaderboard() if view == 'list' else []
        except (sqlite3.Error, ImageError):
            raise cherrypy.HTTPError(503, 'Album storage is unavailable') from None
        page_count = max(1, (total + COLLECTION_PAGE_SIZE - 1) // COLLECTION_PAGE_SIZE)
        if page_number > page_count:
            raise cherrypy.HTTPError(404, 'Unknown catalog page')
        for album in albums:
            album['image_url'] = safe_image_url(album.pop('hosted_url'))
            album['thumb_url'] = safe_image_url(album['thumb_url'])
        filters = dict(
            q=q, artist=artist, album_title=album_title, sort=sort, order=order
        )

        def collection_url(target=view, **changes):
            values = dict(filters, page=page_number)
            values.update(changes)
            path = '/gallery' if target == 'gallery' else '/'
            return (
                pmxbot.config.web_base
                + path
                + '?'
                + urllib.parse.urlencode({k: v for k, v in values.items() if v != ''})
            )

        def page_url(number):
            return collection_url(page=number)

        def sort_url(column):
            direction = 'asc' if column in ('artist', 'album', 'genre') else 'desc'
            if sort == column:
                direction = 'desc' if order == 'asc' else 'asc'
            return collection_url(sort=column, order=direction, page=1)

        def album_url(identifier):
            return (
                pmxbot.config.web_base
                + '/albums/'
                + str(identifier)
                + '?'
                + urllib.parse.urlencode({'return_to': collection_url()})
            )

        page = jenv.overlay(autoescape=True).get_template(
            'gallery.html' if view == 'gallery' else 'album_index.html'
        )
        return page.render(
            albums=albums,
            leaderboard=leaderboard,
            total=total,
            page_number=page_number,
            page_count=page_count,
            result_start=(page_number - 1) * COLLECTION_PAGE_SIZE + 1 if albums else 0,
            result_end=(
                (page_number - 1) * COLLECTION_PAGE_SIZE + len(albums) if albums else 0
            ),
            sort_options=SORT_OPTIONS,
            selected_sort=f'{sort}:{order}',
            page_url=page_url,
            sort_url=sort_url,
            collection_url=collection_url,
            album_url=album_url,
            view=view,
            collection_path=pmxbot.config.web_base
            + ('/gallery' if view == 'gallery' else '/'),
            **filters,
            **context,
        ).encode('utf-8')


def patch_compat(config):
    """
    Support older config values.
    """
    if 'web_host' in config:
        config['host'] = config.pop('web_host')
    if 'web_port' in config:
        config['port'] = config.pop('web_port')


def _setup_logging():
    cherrypy.log.error_log.propagate = False
    cherrypy.log.access_log.propagate = False
    pmxbot.core._setup_logging()


def init_config(config={}):
    config.setdefault('web_base', '/')
    config.setdefault('host', '::0')
    config.setdefault('port', int(os.environ.get('PORT', 8080)))

    config = pmxbot.core.init_config(config)

    if not config.web_base.startswith('/'):
        config['web_base'] = '/' + config.web_base
    if config.web_base.endswith('/'):
        config['web_base'] = config.web_base.rstrip('/')
    if 'logo' not in config:
        web_base = config.web_base or '/'
        config['logo'] = urllib.parse.urljoin(web_base, 'pmxbot.png')

    return config


def resolve_file(mgr, filename):
    """
    Given a file manager (ExitStack), load the filename
    and set the exit stack to clean up. See
    https://importlib-resources.readthedocs.io/en/latest/migration.html#pkg-resources-resource-filename
    for more details.
    """
    path = resources.files('pmxbot.web.templates') / filename
    return str(mgr.enter_context(resources.as_file(path)))


def startup(config):
    patch_compat(config)

    config = init_config(config)

    _setup_logging()

    pmxbot.core._load_library_extensions()

    file_manager = contextlib.ExitStack()
    static = functools.partial(resolve_file, file_manager)

    # Cherrypy configuration here
    app_conf = {
        'global': {
            'server.socket_port': config.port,
            'server.socket_host': config.host,
            'environment': 'production',
            'engine.autoreload.on': False,
            # 'tools.encode.on': True,
            'tools.encode.encoding': 'utf-8',
        },
        '/': auth.session_config(config),
        '/pmxbot.png': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': static('pmxbot.png'),
        },
        '/Autolinker.js': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': static('Autolinker.js'),
        },
    }

    executor = GenerationExecutor(cherrypy.engine, config)
    executor.subscribe()
    tendie_executor.subscribe()
    try:
        with file_manager:
            cherrypy.quickstart(PmxbotPages(), config.web_base, config=app_conf)
    finally:
        executor.stop()
        executor.unsubscribe()
        tendie_executor.stop()
        tendie_executor.unsubscribe()


def run():
    startup(pmxbot.core.get_args().config)
