"""Conservative reference scope and numbered-subpart detection."""
import re


def numbered_parts(text):
    return list(dict.fromkeys(re.findall(
        r'(?:^|[\n；;。])\s*(?:解\s*[：:]?\s*)?\$*\s*[（(]\s*(\d+)\s*[)）]', str(text or ''))))


def scope_reference(text, problem_no):
    """Exclude an unmistakable later textbook question copied into an answer."""
    raw=str(text or '')
    match=re.search(r'第\s*(\d+)\s*题',str(problem_no or ''))
    if not match or raw.lstrip().startswith('['): return raw
    number=int(match.group(1))
    for heading in re.finditer(r'(?m)^\s*(\d{1,2})[.．、]\s*(?:求下列|计算下列|利用[^\n]{0,40}(?:计算|求)|设\s|若\s)',raw):
        if int(heading.group(1))>number:
            return raw[:heading.start()].rstrip()
    return raw
