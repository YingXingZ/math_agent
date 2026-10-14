"""Backward-compatible multi-crop storage; original rectangle remains the first crop."""
import json
import math


def ensure_region_schema(conn):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(submission_question_regions)')}
    if columns and 'regions_json' not in columns:
        conn.execute("ALTER TABLE submission_question_regions ADD COLUMN regions_json TEXT NOT NULL DEFAULT '[]'")


def expand_regions(mapping):
    raw = mapping.get('regions')
    if raw is None:
        raw = json.loads(mapping.get('regions_json') or '[]')
    if not raw:
        raw = [mapping]
    expanded = []
    seen = set()
    for region in raw:
        item = {
            **{key: region[key] for key in ('page_no', 'x', 'y', 'width', 'height')},
            **({'orientation_correction_degrees': region['orientation_correction_degrees']}
               if region.get('orientation_correction_degrees') in (90, 180, 270) else {}),
        }
        # Auto semantic retries can return the same page-space rectangle more
        # than once. A duplicate is never evidence of a cross-page answer and
        # would make OCR/scoring read the same handwriting twice. Preserve
        # genuinely distinct continuation regions, but collapse identical
        # geometry before any crop is generated.
        signature = (
            int(item['page_no']), *(round(float(item[key]), 6)
                                  for key in ('x', 'y', 'width', 'height')),
            int(item.get('orientation_correction_degrees') or 0),
        )
        if signature not in seen:
            seen.add(signature)
            expanded.append(item)
    return expanded


def validate_regions(regions, page_count):
    if not 1 <= len(regions) <= 12:
        raise ValueError('每题须包含 1–12 个作答区域')
    for region in regions:
        page = region['page_no']
        x, y, w, h = [float(region[k]) for k in ('x', 'y', 'width', 'height')]
        if (not isinstance(page, int) or not 1 <= page <= page_count
                or not all(math.isfinite(v) for v in (x, y, w, h))
                or x < 0 or y < 0 or w <= 0 or h <= 0
                or x+w > 1.000001 or y+h > 1.000001):
            raise ValueError('页码或裁切区域超出作业页面范围')
    return regions


def public_mapping(row):
    mapping = dict(row)
    mapping['regions'] = expand_regions(mapping)
    mapping.pop('regions_json', None)
    return mapping
