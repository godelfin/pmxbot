# Image records (F1)

Images now have stable numeric `id` values (`INTEGER PRIMARY KEY AUTOINCREMENT`).
The existing `image_cache` table remains the canonical image record table; its
unique `cache_key` is a lookup key, rather than the image's primary identity.

`ImageCache.get_image(image_id)` returns the persisted record as a dictionary.
`ImageCache.get_image_by_key(cache_key)` resolves an existing cache key to the same
record. Both raise `LookupError` for an unknown image and do not generate or upload
images. `ImageCache.get(prompt, nick, channel)` continues returning the hosted URL.

On the first image-cache connection, existing rows are migrated in one SQLite
transaction. Legacy rowids become explicit image IDs, preserving the ordering
used to select the latest album cover. Every existing metadata field, cache key,
local filename and hosted URL is copied unchanged. Migration does not read or
move image files, call providers, or require credentials. A failed migration
rolls back. Subsequent connections keep the assigned IDs, including after VACUUM.
As with other database upgrades, take a database backup before deployment.

Upload retries, hosting changes, cache hits and regeneration of a missing pending
file retain the image ID and original request attribution. New keys create new
records. Album links continue resolving by cache key, and `!music <id>` continues
to interpret its argument as an **album ID**, independently of image IDs.

This change adds no UI, ratings, ancestry, user model or deletion behavior.
