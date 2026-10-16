"""Public WeChat article extraction gate for Github Actions. No login or anti-bot bypass."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

class FetchFailure(Exception):
    pass

def validate_url(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 2048 or not raw:
        raise FetchFailure('Invalid article URL')
    try:
        p = urlsplit(raw)
    except ValueError as exc:
        raise FetchFailure('Invalid article URL') from exc
    if p.scheme != 'https' or p.hostname != 'mp.weixin.qq.com' or p.port is not None or p.username or p.password or p.fragment:
        raise FetchFailure('Only public WeChat HTTPS article URLs are allowed')
    if not (p.path.startswith('/s/') and len(p.path) > 3 or p.path == '/s' and '__biz=' in p.query):
        raise FetchFailure('Expected a public WeChat article link')
    return raw

def verify_result(result_dir: Path, request: dict) -> dict:
    docs = list(result_dir.rglob('*.md'))
    if len(docs) != 1:
        raise FetchFailure('Expected exactly one article Markdown file')
    doc = docs[0]
    text = doc.read_text(encoding='utf-8')
    if len(text) < 200 or 'source_url:' not in text or 'title:' not in text:
        raise FetchFailure('Missing valid article content and metadata')
    for phrase in ('请输入验证码', '访问过于频繁', '操作过于频繁', '环境异常'):
        if phrase in text[:350]:
            raise FetchFailure('WeChat verification challenge; cannot claim article read')
    refs = re.findall(r'!\[[^\]]*\]\((assets/[^)]+)\)', text)
    files = [p for p in result_dir.rglob('*') if p.is_file() and p != doc]
    image_files = [p for p in files if p.suffix.lower() in ('.png','.jpg','.jpeg','.gif','.webp','.bmp')]
    for ref in refs:
        target = (doc.parent / ref).resolve()
        if not target.is_relative_to(result_dir.resolve()) or not target.is_file():
            raise FetchFailure('Missing image file referred by Markdown')
    if len(refs) != len(image_files):
        raise FetchFailure('Discovered image references do not match downloaded image files')
    for image in image_files:
        if image.stat().st_size <= 150:
            raise FetchFailure('Image file unexpectedly small')
    title_match = re.search(r'^title:\s*(.*)$', text, re.M)
    title = title_match.group(1).strip() if title_match else ''
    if not title:
        raise FetchFailure('Empty article title')
    body = text.split('---', 2)[-1]
    if len(body.strip()) < 100:
        raise FetchFailure('Article body suspiciously empty')
    manifest = {
        'schema': 1, 'source_url': request['url'],
        'request_id': request.get('request_id', ''), 'title': title,
        'markdown_file': doc.name,
        'markdown_sha256': hashlib.sha256(doc.read_bytes()).hexdigest(),
        'downloaded_images': len(image_files),
        'image_references': len(refs),
        'residual_inline_html_images': len(re.findall(r'<img\b', text, flags=re.IGNORECASE)),
        'status': 'article_read',
        'quality_note': 'Downloaded every image linked as a local Markdown asset; dynamic media and residual inline HTML are not guaranteed complete.'
    }
    (result_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest

def main() -> int:
    if len(sys.argv) != 5:
        print('Usage: worker.py <request-json> <url-md-bin> <result-dir> <timeout-seconds>', file=sys.stderr)
        return 2
    request_path, binary, result_folder, timeout_str = sys.argv[1:]
    request = json.loads(Path(request_path).read_text('utf-8'))
    if not isinstance(request, dict) or set(request) - {'url', 'request_id'}:
        raise FetchFailure('Invalid request properties')
    url = validate_url(request.get('url'))
    output = Path(result_folder)
    output.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run([binary, url, '-o', str(output)], capture_output=True, text=True, timeout=min(int(timeout_str), 80), check=False)
    except subprocess.TimeoutExpired as exc:
        raise FetchFailure('WeChat article fetch timed out') from exc
    if p.returncode != 0:
        raise FetchFailure(f'Article extractor returned exit {p.returncode}; may be blocked by WeChat verification')
    report = verify_result(output, request)
    print('ARTICLE_OK=true')
    print('MARKDOWN_BYTES='+str((output / report['markdown_file']).stat().st_size))
    print('IMAGES_VERIFIED='+str(report['downloaded_images']))
    print('RESIDUAL_INLINE_IMAGES='+str(report['residual_inline_html_images']))
    print('ARTICLE_TITLE='+report['title'])
    return 0

if __name__=='__main__':
    try:
        sys.exit(main())
    except (FetchFailure, ValueError, OSError, json.JSONDecodeError) as exc:
        print('ARTICLE_OK=false')
        print('READER_ERROR='+str(exc), file=sys.stderr)
        sys.exit(1)
