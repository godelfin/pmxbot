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

Each linked image has one album owner: a unique index on
`album_images.cache_key` enforces this even for direct SQL inserts. An album can
have multiple images. Linking an image to its current album is idempotent;
linking it to another album raises `sqlite3.IntegrityError`.
Image attribution is stored only in `image_cache.requested_by` and `created_at`.
The completed link-attribution migration has been retired. To upgrade a database
that still has `album_images.image_created_by` or `image_created_at`, first open
the music library at commit `119c089` (or merge `88850f1`), then deploy this version.
Opening the music library adds the ownership constraint to existing databases. If shared
images already exist, the schema upgrade fails atomically; resolve their album
ownership before retrying. Existing links are never silently reassigned or deleted.

The read-only album page is `/albums/<album ID>`, rendered by `album.html`.
It displays album and band properties with the newest linked cached image,
its prompt and metadata. Albums without cached artwork still display their
properties. The page never generates images or changes storage.
