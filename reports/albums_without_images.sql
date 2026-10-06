-- Albums with no linked generated image record.
-- Missing images do not establish that a request was rejected for content.
SELECT a.id AS album_id,
       ar.name AS band_name,
       a.title AS album_name,
       a.created_at,
       a.created_by
FROM albums AS a
JOIN artists AS ar ON ar.id = a.artist_id
WHERE NOT EXISTS (
    SELECT 1
    FROM album_images AS ai
    JOIN image_cache AS ic ON ic.cache_key = ai.cache_key
    WHERE ai.album_id = a.id
)
ORDER BY a.id;
