"""The index queries, exactly as the library documents them.

Each constant is a tagged block of knowledge-store-builder `docs/how-it-works.md`
("Query index", `<!-- index-query: <name> -->`) at b6cbc5a, the commit that made
them final (#457). They are pasted verbatim, so the server reads the index the way
the library says it must be read, and `tests/test_index_queries.py` fails if one
drifts from the vendored copy of that section in `tests/library_docs/`.
Every `?` is bound; none is built from text.
"""

# index-query: find (docs/how-it-works.md at b6cbc5a)
FIND = """SELECT n.id, n.label, coalesce(r.name, ''), n.kind, f.path, n.url, n.community
FROM labels
JOIN nodes n ON n.k = labels.rowid
JOIN files f ON f.k = n.file
LEFT JOIN repos r ON r.k = f.repo
WHERE labels MATCH ?1
  AND (?2 IS NULL OR coalesce(r.name, '') = ?2)
  AND (?3 IS NULL OR n.kind = ?3)
ORDER BY bm25(labels), n.id
LIMIT ?4"""

# index-query: node (docs/how-it-works.md at b6cbc5a)
NODE = """SELECT n.id, n.label, coalesce(r.name, ''), n.kind, f.path, n.url, n.community
FROM nodes n
JOIN files f ON f.k = n.file
LEFT JOIN repos r ON r.k = f.repo
WHERE n.id = ?1"""

# index-query: neighbours (docs/how-it-works.md at b6cbc5a)
NEIGHBOURS = """SELECT 'out' AS direction, rel.name AS relation, t.id AS other
FROM nodes s
JOIN edges e ON e.source = s.k
JOIN relations rel ON rel.k = e.relation
JOIN nodes t ON t.k = e.target
WHERE s.id = ?1 AND (?2 IS NULL OR rel.name = ?2)
UNION ALL
SELECT 'in', rel.name, s.id
FROM nodes t
JOIN edges e ON e.target = t.k
JOIN relations rel ON rel.k = e.relation
JOIN nodes s ON s.k = e.source
WHERE t.id = ?1 AND (?2 IS NULL OR rel.name = ?2)
ORDER BY 2, 3, 1"""

# index-query: tickets_for_file (docs/how-it-works.md at b6cbc5a)
TICKETS_FOR_FILE = """SELECT t.ticket, t.count, t.landed, coalesce(r.name, ''), f.path
FROM files f
LEFT JOIN repos r ON r.k = f.repo
JOIN tickets t ON t.file = f.k
WHERE f.repo IS CASE WHEN ?1 = '' THEN NULL
                     ELSE coalesce((SELECT k FROM repos WHERE name = ?1), -1) END
  AND (?2 IS NULL OR f.path = ?2)
ORDER BY t.landed DESC, t.ticket, f.path"""

# index-query: files_for_ticket (docs/how-it-works.md at b6cbc5a)
FILES_FOR_TICKET = """SELECT coalesce(r.name, ''), f.path, t.count, t.landed
FROM tickets t
JOIN files f ON f.k = t.file
LEFT JOIN repos r ON r.k = f.repo
WHERE t.ticket = ?1
ORDER BY t.landed DESC, coalesce(r.name, ''), f.path"""
