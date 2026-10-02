# Image records (F1)

Images now have stable numeric `id` values (`INTEGER PRIMARY KEY AUTOINCREMENT`).
The existing `image_cache` table remains the canonical image record table; its
unique `cache_key` is a lookup key, rather than the image's primary identity.

`ImageCache.get_image(image_id)` returns the persisted record as a dictionary.
It raises `LookupError` for an unknown image and does not generate or upload
images. `ImageCache.get(prompt, nick, channel)` continues returning the hosted URL.

New databases are initialized with the current schema. Existing databases must
already have the F1 `image_cache.id` primary key; runtime legacy migration has
been retired after production was upgraded. IDs remain stable across connections
and VACUUM.

To upgrade an older database that still uses `cache_key` as its primary key,
back it up and first run the F1 release at commit `1e7dfae` (or the F1 merge
`6efce91`). Open an image-cache connection with that release to perform the atomic
migration, then verify the numeric primary key before deploying this version.
The migration preserves existing rows, metadata, filenames, URLs and album links.

Upload retries, hosting changes, cache hits and regeneration of a missing pending
file retain the image ID and original request attribution. New keys create new
records. Album links continue resolving by cache key, and `!music <id>` continues
to interpret its argument as an **album ID**, independently of image IDs.

This change adds no UI, ratings, ancestry, user model or deletion behavior.
