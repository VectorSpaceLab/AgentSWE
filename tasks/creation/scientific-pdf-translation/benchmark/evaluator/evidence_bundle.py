"""Offline PDF evidence producer and credential-safe sealed-evidence reader."""
from __future__ import annotations
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

CEILINGS = {
    'wrong_language_or_25pct_missing': 35,
    'material_meaning_reversal': 45,
    'material_scientific_corruption': 40,
    'one_unusable_dense_region': 55,
    'multiple_unusable_dense_regions_or_page': 35,
    '10_to_25pct_missing': 60,
    'alignment_and_viewer_nonfunctional': 75,
}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def tree_digest(root):
    result = hashlib.sha256()
    for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink(): kind, payload = b'L', os.readlink(path).encode()
        elif path.is_file(): kind, payload = b'F', path.read_bytes()
        elif path.is_dir(): continue
        else: kind, payload = b'O', b''
        result.update(kind + len(relative).to_bytes(8, 'big') + relative + len(payload).to_bytes(8, 'big') + payload)
    return result.hexdigest()

def safe_file(root, name):
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts or not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('unsafe/missing evidence path: ' + str(name))
    return path

def text_digest(text):
    return hashlib.sha256(re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip().encode()).hexdigest()

def extract_pdf(path, root, side, offset=0, document_id=None):
    import fitz
    document = fitz.open(path)
    if document.is_encrypted or not len(document):
        raise ValueError('encrypted PDF or no pages')
    pages, images = [], []
    for local, page in enumerate(document, 1):
        number = offset + local
        target = root / f'{side}-{number:03d}.png'
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
        pixmap.save(target)
        native_text = page.get_text('text', sort=True)
        textpage = None
        extraction = 'native'
        if len(native_text.strip()) < 12:
            try:
                textpage = page.get_textpage_ocr(language='eng', dpi=180, full=True)
                extraction = 'ocr-eng; uncertain-script-requires-visual-review'
            except Exception as exc:
                if side == 'source':
                    raise RuntimeError('source scan inventory requires working trusted OCR: ' + type(exc).__name__) from exc
                extraction = 'native-empty; visual-review-required: ' + type(exc).__name__
        blocks = page.get_text('dict', textpage=textpage, sort=True)['blocks']
        units = []
        for block in blocks:
            if block.get('type') != 0: continue
            text = '\n'.join(''.join(span['text'] for span in line['spans']) for line in block['lines'])
            if text.strip():
                units.append({'id': f'{side}-p{number}-u{len(units)+1}', 'text': text,
                    'bbox': list(block['bbox']), 'lines': block['lines'], 'extraction': extraction})
        record = {'page': number, 'document_id': document_id, 'local_page': local,
            'width': page.rect.width, 'height': page.rect.height, 'rotation': page.rotation,
            'text': '\n'.join(u['text'] for u in units), 'units': units,
            'image_objects': len(page.get_images()), 'drawing_objects': len(page.get_drawings()),
            'file_sha256': file_hash(path)}
        pages.append(record)
        images.append({'role': side + '_page', 'page': number, 'file': target.name, 'sha256': file_hash(target)})
    document.close()
    return pages, images

def check_alignment(path, sources, target):
    import fitz
    errors, records = [], []
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict): raise ValueError('alignment must be an object')
        for key, expected in [('schema_version', '1.0'), ('coordinate_system', 'normalized_top_left'), ('digest_algorithm', 'sha256')]:
            if value.get(key) != expected: errors.append('invalid ' + key)
        paragraphs = value.get('paragraphs')
        if not isinstance(paragraphs, list) or not paragraphs: raise ValueError('no paragraph records')
        documents, offset = {}, 0
        for index, source in enumerate(sources, 1):
            doc = fitz.open(source)
            documents[f'source-{index}'] = (doc, offset)
            offset += len(doc)
        declared = value.get('source_documents')
        if not isinstance(declared, list) or len(declared) != len(sources): errors.append('source document sequence mismatch')
        else:
            documents = {}
            offset = 0
            for source, item in zip(sources, declared):
                doc = fitz.open(source)
                if not isinstance(item, dict) or not isinstance(item.get('document_id'), str):
                    raise ValueError('invalid document declaration')
                if item.get('global_page_start') != offset + 1 or item.get('page_count') != len(doc) or item.get('input_name') != source.name:
                    errors.append('wrong source page mapping: ' + source.name)
                documents[item['document_id']] = (doc, offset)
                offset += len(doc)
        target_doc = fitz.open(target)
        seen, orders, ocr_pages = set(), set(), {}
        # 0916 fix: keep every fitz Page alive for the whole loop. PyMuPDF TextPage objects hold weak
        # references to their Page; re-fetching doc[i] per anchor let the old Page be collected and a
        # later OCR reuse raised "ReferenceError: weakly-referenced object no longer exists".
        page_cache = {}
        for item in paragraphs:
            if not isinstance(item, dict): errors.append('paragraph is not an object'); continue
            rid = item.get('id')
            if not isinstance(rid, str) or not rid or rid in seen: errors.append('invalid/duplicate paragraph id')
            seen.add(str(rid))
            order, confidence = item.get('reading_order'), item.get('confidence')
            if type(order) is not int or order < 1 or str(order) in orders: errors.append('invalid/duplicate reading order')
            orders.add(str(order))
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1: errors.append('invalid confidence')
            if item.get('status') not in ('aligned', 'low_confidence', 'unresolved'): errors.append('invalid status')
            checked = {'id': rid, 'reading_order': order, 'status': item.get('status'), 'confidence': confidence}
            for side in ('source', 'target'):
                part = item.get(side)
                if not isinstance(part, dict) or not isinstance(part.get('anchors'), list):
                    errors.append(str(rid) + ': missing ' + side); continue
                texts, anchors = [], []
                for anchor in part['anchors']:
                    try:
                        if not isinstance(anchor, dict): raise ValueError('anchor is not an object')
                        number, box = anchor.get('page'), anchor.get('bbox')
                        if type(number) is not int or not isinstance(box, list) or len(box) != 4 or any(type(x) not in (int, float) or not math.isfinite(x) for x in box): raise ValueError('invalid page/box')
                        x,y,w,h = box
                        if min(x,y) < 0 or min(w,h) <= 0 or x+w > 1.002 or y+h > 1.002: raise ValueError('out of bounds')
                        if side == 'source':
                            doc, start = documents[anchor['document_id']]
                            if not 0 <= number-start-1 < len(doc): raise ValueError('source page outside declared document')
                            page_key = (side, anchor.get('document_id'), number)
                            if page_key not in page_cache: page_cache[page_key] = doc[number-start-1]
                            page = page_cache[page_key]
                        else:
                            if not 1 <= number <= len(target_doc): raise ValueError('target page outside PDF')
                            page_key = (side, None, number)
                            if page_key not in page_cache: page_cache[page_key] = target_doc[number-1]
                            page = page_cache[page_key]
                        rect = fitz.Rect(x*page.rect.width, y*page.rect.height, (x+w)*page.rect.width, (y+h)*page.rect.height) * page.derotation_matrix
                        text = page.get_text('text', clip=rect, sort=True)
                        method = 'native'
                        if not text.strip() and page.get_images():
                            key = (side, anchor.get('document_id'), number)
                            if key not in ocr_pages:
                                try:ocr_pages[key] = page.get_textpage_ocr(language='eng',dpi=180,full=True)
                                except Exception:ocr_pages[key] = None
                            if ocr_pages[key] is not None:
                                # TextPage extraction ignores clip: retain words by visible geometry.
                                words = page.get_text('words',textpage=ocr_pages[key],sort=True)
                                text = ' '.join(w[4] for w in words if fitz.Rect(w[:4]).intersects(rect))
                                method = 'ocr-eng; confidence requires review'
                            elif item.get('status') == 'aligned':
                                errors.append(f'{rid} {side}: confident anchor has no independently recovered text')
                        texts.append(text)
                        anchors.append({**anchor, 'extracted_text': text, 'extraction': method})
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        errors.append(f'{rid} {side}: {exc}')
                actual = text_digest(' '.join(texts))
                matched = actual == part.get('text_digest')
                if not matched: errors.append(f'{rid} {side}: text digest mismatch')
                if not anchors and item.get('status') != 'unresolved': errors.append(f'{rid} {side}: no reliable anchors')
                checked[side] = {'anchors': anchors, 'actual_text_digest': actual, 'declared_text_digest': part.get('text_digest'), 'digest_matches': matched}
            records.append(checked)
        target_doc.close()
        for doc, _ in documents.values(): doc.close()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
    return {'functional': bool(records) and not errors, 'errors': errors, 'records': records}

def seal(root, value):
    value['schema_version'] = 'pdf-visual-evidence-v1'
    value['files'] = {p.relative_to(root).as_posix(): file_hash(p) for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'evidence.json'}
    path = root / 'evidence.json'
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return {'directory': root.name, 'manifest_sha256': file_hash(path), 'source_page_count': len(value['source_pages']),
        'target_page_count': len(value['target_pages']), 'image_count': len(value['images'])}

def load_bundle(root, descriptor):
    path = safe_file(root, 'evidence.json')
    if file_hash(path) != descriptor.get('manifest_sha256'): raise RuntimeError('evidence manifest hash mismatch')
    value = json.loads(path.read_text())
    if value.get('schema_version') != 'pdf-visual-evidence-v1': raise RuntimeError('unsupported PDF evidence')
    for name, expected in value['files'].items():
        if file_hash(safe_file(root, name)) != expected: raise RuntimeError('evidence hash mismatch: ' + name)
    for side in ('source', 'target'):
        pages = value[side + '_pages']
        images = [i for i in value['images'] if i['role'] == side + '_page']
        expected = list(range(1, len(pages)+1))
        if not pages or [p['page'] for p in pages] != expected or [p['page'] for p in images] != expected or descriptor.get(side + '_page_count') != len(pages):
            raise RuntimeError('incomplete PDF page coverage: ' + side)
    if descriptor.get('image_count') != len(value['images']): raise RuntimeError('PDF image count mismatch')
    for item in value['images']:
        if value['files'].get(item['file']) != item['sha256']: raise RuntimeError('unsealed image')
    return value

def image_payload(root, value):
    blocks = []
    for item in value['images']:
        blocks.extend([{'type': 'input_text', 'text': 'Independent PDF evidence: ' + json.dumps(item)},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(safe_file(root, item['file']).read_bytes()).decode(), 'detail': 'high'}])
    return blocks

def validate_quality_review(review, bundle, mode='exact'):
    """mode 'exact': the paper's final (0916) rules. mode 'release': plus the judge-format repairs (unknown ceiling
    source units dropped and recorded; ceilings mandated by trusted evidence applied and recorded). run_eval and
    verify_score load this same file and pass the eval manifest's judge_format, so the independent final
    verification always applies the rules the evaluation used."""
    if mode == 'release':
        return _validate_quality_review_release(review, bundle)
    if mode != 'exact':
        raise ValueError('unknown judge_format: ' + str(mode))
    return _validate_quality_review_exact(review, bundle)


def _validate_quality_review_exact(review, bundle):
    """Validate complete source-unit coverage and evidence-backed rubric ceilings."""
    if not isinstance(review, dict): raise ValueError('missing PDF quality_review')
    source = {u['id']: p['page'] for p in bundle['source_pages'] for u in p['units']}
    coverage = review.get('unit_coverage')
    # 0916 rejudge leniency (documented): (a) rows for omitted/source_language/uncertain units may carry an
    # empty evidence locator, there is nothing on the target side to cite; (b) if the page reviewers dropped
    # at most 10% of the units, the missing units are recorded as 'uncertain' instead of voiding the review.
    if not isinstance(coverage, list): raise ValueError('incomplete substantive source-unit coverage')
    present = {row.get('source_unit') for row in coverage if isinstance(row, dict)}
    absent = [u for u in source if u not in present]
    if absent:
        if len(absent) * 10 > len(source): raise ValueError('incomplete substantive source-unit coverage')
        coverage = list(coverage) + [{'source_unit': u, 'status': 'uncertain', 'evidence': 'not reported by page reviewer; treated as uncertain', 'filled_by_validator': True} for u in absent]
        review['unit_coverage'] = coverage
        review['coverage_filled_units'] = absent
    if len(coverage) != len(source): raise ValueError('incomplete substantive source-unit coverage')
    seen, missing = set(), 0
    for row in coverage:
        if not isinstance(row, dict) or row.get('source_unit') not in source or row['source_unit'] in seen: raise ValueError('invalid/duplicate source coverage unit')
        seen.add(row['source_unit'])
        if row.get('status') not in ('translated', 'omitted', 'source_language', 'uncertain'): raise ValueError('invalid source coverage status')
        if row['status'] in ('omitted', 'source_language'): missing += 1
        if not isinstance(row.get('evidence'), str): row['evidence'] = ''
        if not row['evidence'].strip():
            if row['status'] == 'translated': raise ValueError('coverage lacks target evidence')
            row['evidence'] = 'no target locator provided (' + row['status'] + ')'
    checks = review.get('ceilings')
    if not isinstance(checks, dict) or set(checks) != set(CEILINGS): raise ValueError('all seven PDF ceiling findings are required')
    applied = []
    for name, maximum in CEILINGS.items():
        finding = checks[name]
        if not isinstance(finding, dict) or type(finding.get('applies')) is not bool: raise ValueError('invalid ceiling finding: ' + name)
        if not isinstance(finding.get('evidence'), str) or not finding['evidence'].strip(): raise ValueError('ceiling finding lacks evidence: ' + name)
        refs = finding.get('source_units', [])
        pages = finding.get('target_pages', [])
        if not isinstance(refs, list) or any(r not in source for r in refs): raise ValueError('unknown ceiling source unit')
        if not isinstance(pages, list) or any(type(p) is not int or not 1 <= p <= len(bundle['target_pages']) for p in pages): raise ValueError('unknown ceiling target page')
        if finding['applies']:
            if name != 'alignment_and_viewer_nonfunctional' and (not refs or not pages): raise ValueError('applied core ceiling needs exact source and target references')
            applied.append({'name': name, 'maximum': maximum, **finding})
    ratio = missing / len(source) if source else 0
    if ratio >= .25 and not checks['wrong_language_or_25pct_missing']['applies']: raise ValueError('coverage requires 25-percent ceiling')
    if .10 < ratio < .25 and not checks['10_to_25pct_missing']['applies']: raise ValueError('coverage requires 10-to-25-percent ceiling')
    if checks['10_to_25pct_missing']['applies'] and not .10 < ratio < .25:
        raise ValueError('10-to-25-percent ceiling contradicts counted substantive omissions')
    if not bundle['alignment']['functional'] and not bundle['viewer']['functional']:
        if not checks['alignment_and_viewer_nonfunctional']['applies']: raise ValueError('offline evidence requires alignment/viewer ceiling')
    return {'source_unit_count': len(source), 'missing_unit_count': missing, 'missing_fraction': ratio,
        'applied_ceilings': applied, 'maximum_total': min([100] + [item['maximum'] for item in applied])}

def _validate_quality_review_release(review, bundle):
    """Validate complete source-unit coverage and evidence-backed rubric ceilings."""
    if not isinstance(review, dict): raise ValueError('missing PDF quality_review')
    source = {u['id']: p['page'] for p in bundle['source_pages'] for u in p['units']}
    coverage = review.get('unit_coverage')
    # 0916 rejudge leniency (documented): (a) rows for omitted/source_language/uncertain units may carry an
    # empty evidence locator, there is nothing on the target side to cite; (b) if the page reviewers dropped
    # at most 10% of the units, the missing units are recorded as 'uncertain' instead of voiding the review.
    if not isinstance(coverage, list): raise ValueError('incomplete substantive source-unit coverage')
    present = {row.get('source_unit') for row in coverage if isinstance(row, dict)}
    absent = [u for u in source if u not in present]
    if absent:
        if len(absent) * 10 > len(source): raise ValueError('incomplete substantive source-unit coverage')
        coverage = list(coverage) + [{'source_unit': u, 'status': 'uncertain', 'evidence': 'not reported by page reviewer; treated as uncertain', 'filled_by_validator': True} for u in absent]
        review['unit_coverage'] = coverage
        review['coverage_filled_units'] = absent
    if len(coverage) != len(source): raise ValueError('incomplete substantive source-unit coverage')
    seen, missing = set(), 0
    for row in coverage:
        if not isinstance(row, dict) or row.get('source_unit') not in source or row['source_unit'] in seen: raise ValueError('invalid/duplicate source coverage unit')
        seen.add(row['source_unit'])
        if row.get('status') not in ('translated', 'omitted', 'source_language', 'uncertain'): raise ValueError('invalid source coverage status')
        if row['status'] in ('omitted', 'source_language'): missing += 1
        if not isinstance(row.get('evidence'), str): row['evidence'] = ''
        if not row['evidence'].strip():
            if row['status'] == 'translated': raise ValueError('coverage lacks target evidence')
            row['evidence'] = 'no target locator provided (' + row['status'] + ')'
    checks = review.get('ceilings')
    if not isinstance(checks, dict) or set(checks) != set(CEILINGS): raise ValueError('all seven PDF ceiling findings are required')
    applied = []
    for name, maximum in CEILINGS.items():
        finding = checks[name]
        if not isinstance(finding, dict) or type(finding.get('applies')) is not bool: raise ValueError('invalid ceiling finding: ' + name)
        if not isinstance(finding.get('evidence'), str) or not finding['evidence'].strip(): raise ValueError('ceiling finding lacks evidence: ' + name)
        refs = finding.get('source_units', [])
        pages = finding.get('target_pages', [])
        if not isinstance(refs, list): raise ValueError('unknown ceiling source unit')
        unknown_refs = [r for r in refs if r not in source]
        if unknown_refs:  # 0918: drop unknown units and record them instead of voiding the whole review
            refs = [r for r in refs if r in source]; finding['source_units'] = refs
            finding['evidence'] = (finding.get('evidence') or '') + ' [unknown source units dropped: ' + ','.join(str(u) for u in unknown_refs) + ']'
            finding['unknown_source_units_dropped'] = unknown_refs
        if not isinstance(pages, list) or any(type(p) is not int or not 1 <= p <= len(bundle['target_pages']) for p in pages): raise ValueError('unknown ceiling target page')
        if finding['applies']:
            if name != 'alignment_and_viewer_nonfunctional' and (not refs or not pages) and not finding.get('auto_applied'): raise ValueError('applied core ceiling needs exact source and target references')
            applied.append({'name': name, 'maximum': maximum, **finding})
    ratio = missing / len(source) if source else 0
    # 0917 v2-lite: a ceiling mandated by trusted evidence is applied even when the judge omitted it
    # (the omission is a formatting lapse, not a judgment); the auto-application is recorded.
    def _auto(name, why):
        if not checks[name]['applies']:
            checks[name]['applies'] = True
            checks[name]['evidence'] = (checks[name].get('evidence') or '') + ' [auto-applied from trusted evidence: ' + why + ']'
            checks[name]['auto_applied'] = True
            maximum = CEILINGS[name] if isinstance(CEILINGS, dict) else dict(CEILINGS)[name]
            applied.append({'name': name, 'maximum': maximum, **checks[name]})
    if ratio >= .25: _auto('wrong_language_or_25pct_missing', 'counted substantive omissions >= 25%')
    if .10 < ratio < .25: _auto('10_to_25pct_missing', 'counted substantive omissions between 10% and 25%')
    if checks['10_to_25pct_missing']['applies'] and not .10 < ratio < .25 and not checks['10_to_25pct_missing'].get('auto_applied'):
        raise ValueError('10-to-25-percent ceiling contradicts counted substantive omissions')
    if not bundle['alignment']['functional'] and not bundle['viewer']['functional']:
        _auto('alignment_and_viewer_nonfunctional', 'alignment.json and viewer both nonfunctional in sealed evidence')
    # idempotent: re-verifying an already repaired review yields the same decision
    order = list(dict(CEILINGS)); applied.sort(key=lambda item: order.index(item['name']))
    return {'source_unit_count': len(source), 'missing_unit_count': missing, 'missing_fraction': ratio,
        'applied_ceilings': applied, 'maximum_total': min([100] + [item['maximum'] for item in applied])}

def validate_semantic_fatal(finding, bundle):
    """Only the PDF rubric's two content-based whole-document fatal conditions."""
    if not isinstance(finding, dict) or finding.get('condition') not in ('placeholder_only', 'unrelated_document'):
        raise ValueError('unregistered PDF semantic fatal condition')
    if bundle.get('run_report', {}).get('status') != 'success':
        raise ValueError('semantic fatal requires a reported successful run')
    if sorted(finding.get('target_pages', [])) != list(range(1, len(bundle['target_pages']) + 1)):
        raise ValueError('semantic fatal requires every translated page to be inspected')
    if not isinstance(finding.get('evidence'), str) or not finding['evidence'].strip():
        raise ValueError('semantic fatal requires exact source/target document evidence')
    return True
