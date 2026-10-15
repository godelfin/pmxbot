"""Personal artwork preferences, independent of roles, generation and ratings."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .music import MusicLibrary
from .users import UserStore


class FavoriteError(ValueError):
    """The album or artwork cannot be favorited."""


@dataclass(frozen=True)
class ArtworkChoice:
    image: dict
    favorite_image_id: int | None


def resolve_album_artwork(db, album_ids, user_id=None, explicit_image_id=None):
    """Batch explicit, personal, unique aggregate winner, then newest selection.

    Reads legacy databases without migrating them. Created-at and ID descending
    preserve the existing newest-image ordering, including unhosted images.
    """
    if not album_ids:
        return {}
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {'image_cache', 'album_images'} <= tables:
        return {}
    placeholders = ','.join('?' for _ in album_ids)
    if 'user_album_favorites' in tables:
        votes = f'''SELECT album_id, image_id, count(*) AS votes,
            MAX(CASE WHEN user_id = ? THEN 1 ELSE 0 END) AS mine
            FROM user_album_favorites WHERE album_id IN ({placeholders})
            GROUP BY album_id, image_id'''
        parameters = [user_id, *album_ids]
    else:
        votes = (
            'SELECT NULL AS album_id, NULL AS image_id, 0 AS votes, 0 AS mine WHERE 0'
        )
        parameters = []
    rows = db.execute(
        f'''WITH votes AS ({votes}), candidates AS (
            SELECT image_cache.*, album_images.album_id AS choice_album,
                COALESCE(votes.votes, 0) AS choice_votes,
                COALESCE(votes.mine, 0) AS choice_mine
            FROM image_cache JOIN album_images USING (cache_key)
            LEFT JOIN votes ON votes.album_id = album_images.album_id
                AND votes.image_id = image_cache.id
            WHERE album_images.album_id IN ({placeholders})
        ), maxima AS (
            SELECT *, MAX(choice_votes) OVER (PARTITION BY choice_album) AS choice_max,
                MAX(CASE WHEN choice_mine = 1 THEN id END)
                    OVER (PARTITION BY choice_album) AS choice_favorite
            FROM candidates
        ), leaders AS (
            SELECT *, SUM(choice_votes = choice_max)
                OVER (PARTITION BY choice_album) AS choice_leaders FROM maxima
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY choice_album ORDER BY
                (id = ?) DESC, choice_mine DESC,
                CASE WHEN choice_leaders = 1 THEN choice_votes ELSE 0 END DESC,
                created_at DESC, id DESC) AS choice_rank FROM leaders
        ) SELECT * FROM ranked WHERE choice_rank = 1''',
        [*parameters, *album_ids, explicit_image_id],
    )
    return {
        row['choice_album']: ArtworkChoice(
            {
                key: value
                for key, value in dict(row).items()
                if not key.startswith('choice_')
            },
            row['choice_favorite'],
        )
        for row in rows
    }


class FavoriteStore:
    def __init__(self, database):
        self.database = Path(database)

    def connect(self):
        # Initialize existing canonical schemas, preserving all historical data.
        from .images import initialize_image_records

        with closing(UserStore('sqlite:' + str(self.database))):
            pass
        db = MusicLibrary(self.database).connect()
        try:
            db.isolation_level = None
            initialize_image_records(db)
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS image_cache_id_key ON image_cache(id, cache_key)'
            )
            db.execute(
                '''CREATE TABLE IF NOT EXISTS user_album_favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                image_id INTEGER NOT NULL,
                cache_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, album_id),
                FOREIGN KEY (image_id, cache_key) REFERENCES image_cache(id, cache_key)
                    ON DELETE CASCADE ON UPDATE CASCADE,
                FOREIGN KEY (album_id, cache_key) REFERENCES album_images(album_id, cache_key)
                    ON DELETE CASCADE ON UPDATE CASCADE
            )'''
            )
            db.execute(
                'CREATE INDEX IF NOT EXISTS user_album_favorites_album_image ON user_album_favorites(album_id, image_id)'
            )
            db.execute(
                'CREATE INDEX IF NOT EXISTS user_album_favorites_image_key ON user_album_favorites(image_id, cache_key)'
            )
            db.commit()
        except Exception:
            db.rollback()
            db.close()
            raise
        return db

    def toggle(self, user, album_id, image_id):
        """Toggle only this identity's preference under a serialized write lock."""
        with closing(self.connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                fresh = db.execute(
                    'SELECT enabled, must_change_password, session_version FROM users WHERE id = ?',
                    (user.id,),
                ).fetchone()
                if (
                    not fresh
                    or not fresh[0]
                    or fresh[1]
                    or fresh[2] != user.session_version
                ):
                    raise PermissionError('Sign in again.')
                images = db.execute(
                    '''SELECT id, cache_key FROM image_cache JOIN album_images USING (cache_key)
                    WHERE album_id = ?''',
                    (album_id,),
                ).fetchall()
                key = next(
                    (row['cache_key'] for row in images if row['id'] == image_id), None
                )
                if len(images) < 2 or key is None:
                    raise FavoriteError(
                        'Favorites require an image belonging to an album with multiple artwork versions.'
                    )
                current = db.execute(
                    'SELECT image_id FROM user_album_favorites WHERE user_id = ? AND album_id = ?',
                    (user.id, album_id),
                ).fetchone()
                if current and current[0] == image_id:
                    db.execute(
                        'DELETE FROM user_album_favorites WHERE user_id = ? AND album_id = ?',
                        (user.id, album_id),
                    )
                    favorite = None
                else:
                    db.execute(
                        '''INSERT INTO user_album_favorites (user_id, album_id, image_id, cache_key)
                        VALUES (?, ?, ?, ?) ON CONFLICT(user_id, album_id) DO UPDATE SET
                        image_id = excluded.image_id, cache_key = excluded.cache_key,
                        created_at = CURRENT_TIMESTAMP''',
                        (user.id, album_id, image_id, key),
                    )
                    favorite = image_id
                db.commit()
                return favorite
            except Exception:
                db.rollback()
                raise
