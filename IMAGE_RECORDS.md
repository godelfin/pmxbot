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
Unlinked-image recovery and legacy-ID retirement tools have also been removed
following the completed production migration. Historical recovery is available
at `119c089`, and the legacy table cleanup command at `36f3f89`.
Opening the music library adds the ownership constraint to existing databases. If shared
images already exist, the schema upgrade fails atomically; resolve their album
ownership before retrying. Existing links are never silently reassigned or deleted.

The read-only album page is `/albums/<album ID>`, rendered by `album.html`.
It displays album and band properties with the newest linked cached image,
its prompt and metadata. Artwork history shows every linked image newest first;
hosted images are selectable previews, and selecting any version updates the
displayed prompt and metadata. Missing hosted artwork has a text placeholder.
Albums without cached artwork still display their properties. The page never
generates images or changes storage.

Failed album artwork requests are saved separately in `album_image_failures`,
including the album ID, original prompt, requester, channel, timestamp, error
type, and a message safe for display. Generation, configuration, and upload
errors are recorded even when no image-cache row exists. Unexpected exceptions
retain their type with a generic message; their raw text is never persisted.
OpenAI failures may include sanitized provider diagnostics: HTTP status, error
code/type, a bounded API error message, request ID, and Retry-After. Only these
allowlisted fields are read; credentials, headers other than request ID/retry
information, raw responses, and arbitrary exception text are excluded from both
the stored message and ordinary diagnostic logs. The original exception remains
chained for debugging.
The album page displays this history alongside any existing artwork, and
successful retries retain earlier failures. The music library automatically
creates the new table for existing databases; read-only pages also support
databases that predate it. Failures from before this change cannot be recovered
unless they were recorded elsewhere. If database storage itself is unavailable,
the bot logs that it could not save the failure and preserves the original error.

The read-only `/gallery` page shows 24 albums per page, newest creation date first,
with the newest linked image as a lazy-loaded thumbnail. Each card opens its
album details. Albums without valid hosted artwork have a placeholder.
Use `/gallery?page=2` for subsequent pages; the album index and details link to
the gallery. Pagination is performed in SQLite, and viewing the gallery never
initializes or changes storage.
The sorting dropdown offers Alphabetical (Band), Alphabetical (Album),
Date Ascending, Date Descending, and Genre. Genre uses the album genre,
case-insensitively, with missing genres
last. Date ties use album IDs, and alphabetical ties use title then ID for stable
pagination. The `sort` query parameter persists in pagination links; changing
the dropdown starts again on page one.


## Image ancestry (F4)

`image_cache.parent_image_id` is an optional foreign key to `image_cache.id`.
Original images have NULL parents. `ImageCache.persist_image(db, key, prompt,
filename, metadata, nick='', channel='', parent_image_id=None)` persists a
record using an initialized image-cache connection, without generating or
uploading an image. Album variations supply the selected source image ID here.
Each variation uses a distinct cache key, atomically links to the same album, and stores exact prompt
and structured generation inputs in generation metadata. Existing records remain
unchanged.

The parent must already exist. SQLite triggers reject nonexistent parents,
self-parenting, cycles, changes to persisted parent relationships or image IDs,
and deletion of a parent with children, even on connections with foreign-key
checks disabled. Invalid ancestry raises `sqlite3.IntegrityError`. Repeating a
persistence operation for an existing cache key preserves its original parent;
omitting the parent permits ordinary upload/regeneration retries, while an
explicit different parent is rejected. Existing generation callers create roots.

Opening an image-cache connection automatically adds the nullable column and
triggers to pre-F4 databases. The migration is serialized and transactional,
and repeated initialization is safe. Existing IDs, metadata and album links
are preserved, with NULL ancestry; historical parents cannot be inferred.
Back up the database before deployment. The earlier F1 numeric-ID prerequisite
still applies. Read-only retrieval does not migrate storage: pre-F4 records
lack the dictionary field until an image-cache connection initializes the schema.
