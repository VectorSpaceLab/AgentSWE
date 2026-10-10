"""Verifier-owned PPTX/source evidence. No Candidate code or network execution."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import io
import zipfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root):
    result = hashlib.sha256()
    for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b'L', os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b'F', path.read_bytes()
        elif path.is_dir():
            continue
        else:
            kind, payload = b'O', b''
        result.update(kind + len(relative).to_bytes(8, 'big') + relative + len(payload).to_bytes(8, 'big') + payload)
    return result.hexdigest()


def safe_file(root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise RuntimeError('invalid trusted evidence path')
    target = root / path
    if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise RuntimeError('symlink in trusted evidence path')
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('missing trusted evidence file')
    return target


def collect_sources(case_dir, root, pdftoppm):
    """Keep full supplied evidence; unknown binary sources fail closed, never vanish."""
    records, images = [], []
    text_suffixes = {'.md', '.txt', '.csv', '.tsv', '.json', '.jsonl', '.html', '.htm', '.svg', '.xml', '.sql', '.yaml', '.yml'}
    paths = [case_dir / 'input.md', *sorted((case_dir / 'assets').rglob('*'))]
    for index, source in enumerate(paths):
        if source.is_dir():
            continue
        if source.is_symlink() or not source.is_file():
            raise RuntimeError('unsafe supplied source: ' + source.name)
        item = {'source': source.relative_to(case_dir).as_posix(), 'sha256': file_hash(source), 'bytes': source.stat().st_size}
        suffix = source.suffix.lower()
        if suffix in text_suffixes:
            item['text'] = source.read_text(encoding='utf-8')
            if suffix == '.svg':
                import cairosvg
                target = root / 'sources' / f'{index}-svg.png'
                target.parent.mkdir(parents=True, exist_ok=True)
                cairosvg.svg2png(bytestring=source.read_bytes(), write_to=str(target), scale=2,
                                unsafe=False)
                images.append({'role': 'source_svg_raster', 'source': item['source'],
                               'file': target.relative_to(root).as_posix(), 'sha256': file_hash(target)})
        elif suffix in {'.png', '.jpg', '.jpeg'}:
            target = root / 'sources' / f'{index}{suffix}'
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            images.append({'role': 'source_image', 'source': item['source'], 'file': target.relative_to(root).as_posix(), 'sha256': file_hash(target)})
        elif suffix == '.pdf':
            prefix = root / 'sources' / str(index)
            prefix.parent.mkdir(parents=True, exist_ok=True)
            pdftotext = pdftoppm.with_name('pdftotext')
            for command in ([str(pdftotext), '-layout', str(source), str(prefix) + '.txt'],
                            [str(pdftoppm), '-png', '-r', '100', str(source), str(prefix)]):
                cp = subprocess.run(command, capture_output=True, text=True, timeout=180)
                if cp.returncode:
                    raise RuntimeError('supplied PDF extraction/rasterization failed: ' + item['source'])
            item['text'] = Path(str(prefix) + '.txt').read_text(encoding='utf-8')
            pages = sorted(prefix.parent.glob(prefix.name + '-*.png'), key=lambda p: int(re.search(r'(\d+)\.png$', p.name)[1]))
            if not pages:
                raise RuntimeError('supplied PDF rasterization produced no pages')
            item['page_count'] = len(pages)
            for number, page in enumerate(pages, 1):
                images.append({'role': 'source_pdf_page', 'source': item['source'], 'page': number,
                               'file': page.relative_to(root).as_posix(), 'sha256': file_hash(page)})
        else:
            raise RuntimeError('source format has no independent evidence reader: ' + item['source'])
        records.append(item)
    return records, images


def compare_source_marks(deck, root, source_images):
    """Expose source SVG/media pixel and aspect comparisons as quality evidence."""
    from PIL import Image, ImageChops, ImageStat
    from xml.etree import ElementTree as ET
    ns = {'p':'http://schemas.openxmlformats.org/presentationml/2006/main',
          'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
    results = []
    with zipfile.ZipFile(deck) as archive:
        media = []
        for name in archive.namelist():
            if name.startswith('ppt/media/'):
                try:
                    with Image.open(io.BytesIO(archive.read(name))) as im:
                        media.append((name, im.convert('RGB')))
                except (OSError, ValueError):
                    continue
        for item in source_images:
            if item.get('role') != 'source_svg_raster': continue
            with Image.open(safe_file(root,item['file'])) as original:
                rgb=original.convert('RGB');ratio=rgb.width/rgb.height;reference=rgb.resize((360,96))
            matches=[]
            for name,picture in media:
                deviation=abs(picture.width/picture.height-ratio)/ratio
                difference=ImageStat.Stat(ImageChops.difference(reference,picture.resize((360,96)))).mean
                matches.append({'media_part':name,'aspect_ratio_relative_error':deviation,'pixel_mean_absolute_error':sum(difference)/3})
            results.append({'source':item['source'],'source_raster_sha256':item['sha256'],
                'candidate_media_comparisons':sorted(matches,key=lambda x:x['pixel_mean_absolute_error'])})
        pictures=[]
        for name in archive.namelist():
            if re.fullmatch(r'ppt/slides/slide\d+\.xml',name):
                document=ET.fromstring(archive.read(name))
                for pic in document.findall('.//p:pic',ns):
                    extent=pic.find('.//a:xfrm/a:ext',ns);blip=pic.find('.//a:blip',ns)
                    if extent is not None and blip is not None:
                        pictures.append({'slide_part':name,'relationship':dict(blip.attrib),
                            'display_ratio':int(extent.attrib['cx'])/int(extent.attrib['cy']) if int(extent.attrib['cy']) else None})
    return {'source_marks':results,'picture_geometry':pictures,
        'interpretation':'Pixel similarity is quality evidence, never a new fatal gate. Compare displayed proportions and actual rendered cover/closing marks with the source raster.'}


def seal(root, content):
    content['schema_version'] = 'pptx-visual-evidence-v1'
    content['files'] = {p.relative_to(root).as_posix(): file_hash(p)
                        for p in sorted(root.rglob('*')) if p.is_file()}
    path = root / 'evidence.json'
    path.write_text(json.dumps(content, sort_keys=True, ensure_ascii=False, indent=2) + '\n')
    return {'directory': root.name, 'manifest_sha256': file_hash(path),
            'slide_count': len(content['slides']), 'image_count': len(content['images'])}


def load_bundle(root, descriptor):
    path = safe_file(root, 'evidence.json')
    if file_hash(path) != descriptor.get('manifest_sha256'):
        raise RuntimeError('trusted visual evidence manifest hash mismatch')
    content = json.loads(path.read_text())
    if content.get('schema_version') != 'pptx-visual-evidence-v1':
        raise RuntimeError('unsupported visual evidence schema')
    files = content.get('files', {})
    for name, expected in files.items():
        if file_hash(safe_file(root, name)) != expected:
            raise RuntimeError('trusted evidence file hash mismatch: ' + name)
    slides, images = content['slides'], content['images']
    page_images = [i for i in images if i.get('role') == 'candidate_slide']
    if not slides or [i.get('slide') for i in slides] != list(range(1, len(slides) + 1)):
        raise RuntimeError('invalid slide evidence order/coverage')
    if [i.get('slide') for i in page_images] != list(range(1, len(slides) + 1)):
        raise RuntimeError('not every Candidate slide has a rendered image')
    if descriptor.get('slide_count') != len(slides) or descriptor.get('image_count') != len(images):
        raise RuntimeError('visual evidence count mismatch')
    for record in images:
        if files.get(record['file']) != record['sha256']:
            raise RuntimeError('image is not bound to trusted manifest')
    return content


def image_payload(root, content):
    blocks = []
    for record in content['images']:
        path = safe_file(root, record['file'])
        mime = 'image/jpeg' if path.suffix.lower() in {'.jpg', '.jpeg'} else 'image/png'
        blocks.extend([{'type': 'input_text', 'text': 'Evidence image: ' + json.dumps(record, sort_keys=True)},
                       {'type': 'input_image', 'image_url': 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode(), 'detail': 'high'}])
    return blocks
