"""Materialize the pinned upstream extension and the workbench integration."""
import hashlib
import json
import secrets
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASE = 'https://wpics.oss-cn-shanghai.aliyuncs.com/wechatsync-2.0.9.zip?date=20260324'
SHA256 = 'dfb191decd63a45063c4f6d8f7687745e5ecebe34363792dd6781b657d69fd29'
ENTRY = 'assets/index.ts-Bw-475TG.js'


def prepare(data):
    data = Path(data).resolve() / 'platform-accounts' / 'wechatsync'
    data.mkdir(parents=True, exist_ok=True)
    archive = data / 'upstream-2.0.9.zip'
    if not archive.exists():
        local = ROOT.parent.parent / 'runs/upstream-inspection/wechatsync-2.0.9.zip'
        archive.write_bytes(local.read_bytes() if local.exists() else urllib.request.urlopen(RELEASE, timeout=30).read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Wechatsync release checksum changed')
    config_path = data / 'bridge.json'
    if not config_path.exists():
        config_path.write_text(json.dumps({'port': 19527, 'token': secrets.token_urlsafe(32)}), encoding='utf-8')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    extension = data / 'extension'
    extension.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            destination = (extension / item.filename).resolve()
            if not destination.is_relative_to(extension):
                raise ValueError('Unexpected upstream archive path')
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read(item))
    entry = extension / ENTRY
    code = entry.read_text(encoding='utf-8')
    anchor = 'case"extractArticle":'
    if code.count(anchor) != 1:
        raise ValueError('Upstream bridge entry changed')
    cases = ''.join(f'case"workbench{name}":return globalThis.workbench{name}(e);' for name in ('Auth', 'Save', 'Inspect', 'Find', 'Renew', 'Ping'))
    code = code.replace(anchor, cases + anchor)
    code += '\nexport {je as workbenchAdapter, G as workbenchClient, Vt as workbenchConnect};\n'
    entry.write_text(code, encoding='utf-8')
    (extension / 'workbench.js').write_bytes((ROOT / 'workbench.js').read_bytes())
    (extension / 'workbench-config.js').write_text('export default ' + json.dumps(config) + ';\n', encoding='utf-8')
    (extension / 'service-worker-loader.js').write_text('import "./workbench.js";\n', encoding='utf-8')
    manifest_path = extension / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.update(name='Wechatsync · 内容工作台', version_name='2.0.9 + workbench integration')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (extension / 'LICENSE').write_bytes((ROOT / 'upstream/LICENSE').read_bytes())
    return extension


if __name__ == '__main__':
    print(prepare(sys.argv[1]))
