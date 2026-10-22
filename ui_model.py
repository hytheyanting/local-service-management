"""Persist presentation preferences independently of service commands and data."""
import copy
import fcntl
import hashlib
import json
import os
import pathlib
import re
import struct
import subprocess
import tempfile


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root/'ui-preferences.json'
        self.lock = self.root/'ui-preferences.lock'

    def _write(self, value):
        with tempfile.NamedTemporaryFile(mode='w', dir=self.root, delete=False) as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            temporary = pathlib.Path(f.name)
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def _load(self, services):
        state = json.loads(self.path.read_text()) if self.path.exists() else {
            'revision': 0, 'categories': [], 'group_map': {}, 'assignments': {},
            'appearance': {'transparency': 35, 'blur': 10, 'wallpaper': 'default'}}
        old = copy.deepcopy(state)
        for s in services.values():
            group = s.get('group', '未分类')
            if group not in state['group_map']:
                matching = next((c['id'] for c in state['categories'] if c['name'].casefold() == group.casefold()), None)
                if matching:
                    ident = matching
                elif group == '未分类':
                    ident = 'uncategorized'
                else:
                    ident = 'cat-'+hashlib.sha256(group.encode()).hexdigest()[:12]
                    position = next((i for i,c in enumerate(state['categories']) if c['id'] == 'uncategorized'), len(state['categories']))
                    state['categories'].insert(position, {'id': ident, 'name': group})
                state['group_map'][group] = ident
        if not any(c['id'] == 'uncategorized' for c in state['categories']):
            state['categories'].append({'id': 'uncategorized', 'name': '未分类'})
        if not self.path.exists() or state != old:
            state['revision'] += 1
            self._write(state)
        return state

    def read(self, services):
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._load(services)

    @staticmethod
    def category_for(service, state):
        return state['assignments'].get(service['id'], state['group_map'].get(service.get('group', '未分类'), 'uncategorized'))

    def _check_revision(self, state, revision):
        if revision != state['revision']:
            raise Conflict('设置已在其他页面更新，请重新打开设置后再保存。')

    def save_preferences(self, services, value):
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._load(services)
            self._check_revision(state, value.get('revision'))
            categories = value.get('categories')
            if not isinstance(categories, list) or not 1 <= len(categories) <= 100:
                raise ValueError('分类数量需为 1–100 个。')
            names, ids, clean = set(), set(), []
            for c in categories:
                ident, name = c.get('id', ''), c.get('name', '').strip()
                if not isinstance(ident, str) or not re.fullmatch(r'(cat-[a-z0-9-]{1,64}|uncategorized)', ident):
                    raise ValueError('分类编号不合法。')
                if not 1 <= len(name) <= 24 or any(ord(x) < 32 for x in name):
                    raise ValueError('分类名称需为 1–24 个字符。')
                if name.casefold() in names or ident in ids:
                    raise ValueError('分类名称不能重复。')
                if (ident == 'uncategorized') != (name == '未分类'):
                    raise ValueError('“未分类”是保留分类，不能改名。')
                names.add(name.casefold()); ids.add(ident)
                clean.append({'id': ident, 'name': name})
            if 'uncategorized' not in ids:
                raise ValueError('不能删除“未分类”。')
            appearance = value.get('appearance', state['appearance'])
            transparency, blur = appearance.get('transparency'), appearance.get('blur')
            if type(transparency) not in [int, float] or not 0 <= transparency <= 90:
                raise ValueError('透明度需为 0–90%。')
            if type(blur) not in [int, float] or not 0 <= blur <= 24:
                raise ValueError('模糊程度需为 0–24px。')
            wallpaper = appearance.get('wallpaper', 'default')
            if wallpaper not in ['default', 'none']:
                if not isinstance(wallpaper, str) or not re.fullmatch(r'/media/[a-f0-9]{64}\.(png|jpg|webp)', wallpaper):
                    raise ValueError('只能使用本机上传的背景。')
                if not (self.root/'uploads'/wallpaper.rsplit('/', 1)[1]).exists():
                    raise ValueError('上传的背景不存在，请重新上传。')
            for s in services.values():
                if self.category_for(s, state) not in ids:
                    state['assignments'][s['id']] = 'uncategorized'
            state['group_map'] = {k: v if v in ids else 'uncategorized' for k, v in state['group_map'].items()}
            state['assignments'] = {k: v if v in ids else 'uncategorized' for k, v in state['assignments'].items()}
            state['categories'] = clean
            logo = appearance.get('logo', state['appearance'].get('logo', 'default'))
            if logo != 'default':
                if not isinstance(logo, str) or not re.fullmatch(r'/media/[a-f0-9]{64}\.(png|jpg|webp)', logo):
                    raise ValueError('只能使用本机上传的图标。')
                if not (self.root/'uploads'/logo.rsplit('/', 1)[1]).is_file():
                    raise ValueError('上传的图标不存在，请重新上传。')
            state['appearance'] = {'transparency': round(transparency), 'blur': round(blur), 'wallpaper': wallpaper, 'logo': logo}
            state['revision'] += 1
            self._write(state)
            return state

    def assign(self, services, revision, service_id, category):
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._load(services)
            self._check_revision(state, revision)
            if service_id not in services or category not in {c['id'] for c in state['categories']}:
                raise ValueError('服务或分类不存在。')
            state['assignments'][service_id] = category
            state['revision'] += 1
            self._write(state)
            return state

    def edit_service_info(self, services, revision, service_id, values):
        if not isinstance(values,dict) or set(values) != {'description','help'}:
            raise ValueError('仅允许编辑用途简介和使用方法。')
        description, help_text = values.get('description'), values.get('help')
        if not isinstance(description,str) or not 1 <= len(description.strip()) <= 240 or any(ord(c)<32 for c in description):
            raise ValueError('用途简介需为 1–240 个字符。')
        if not isinstance(help_text,str) or len(help_text)>4000 or '\x00' in help_text:
            raise ValueError('使用方法最多 4000 个字符。')
        with self.lock.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state = self._load(services)
            self._check_revision(state, revision)
            if service_id not in services:
                raise ValueError('此服务已不存在，请刷新服务列表。')
            state.setdefault('service_info',{})[service_id] = {'description':description.strip(),'help':help_text.strip()}
            state['revision'] += 1
            self._write(state)
            return state

    def service_icon(self, value='default'):
        if value in ['', 'default']:
            return 'default'
        if not isinstance(value,str) or not re.fullmatch(r'/media/[a-f0-9]{64}\.(png|jpg|webp)',value):
            raise ValueError('应用图标只能使用本机上传的 JPG、PNG 或 WebP 图片。')
        if not (self.root/'uploads'/value.rsplit('/',1)[1]).is_file():
            raise ValueError('上传的应用图标不存在，请重新上传。')
        return value

    def upload(self, data, mime):
        if not isinstance(data, bytes) or not 0 < len(data) <= 8*1024*1024:
            raise ValueError('图片大小不能超过 8 MB。')
        valid = {'image/png': ('png', data.startswith(b'\x89PNG\r\n\x1a\n')),
                 'image/jpeg': ('jpg', data.startswith(b'\xff\xd8\xff')),
                 'image/webp': ('webp', data.startswith(b'RIFF') and data[8:12] == b'WEBP')}
        ext, matched = valid.get(mime, ('', False))
        if not matched:
            raise ValueError('请选择有效的 JPG、PNG 或 WebP 图片。')
        directory = self.root/'uploads'
        directory.mkdir(exist_ok=True, mode=0o700)
        name = hashlib.sha256(data).hexdigest()+'.'+ext
        destination = directory/name
        if not destination.exists():
            with tempfile.NamedTemporaryFile(dir=directory, suffix='.'+ext, delete=False) as f:
                f.write(data); temporary = pathlib.Path(f.name)
            try:
                result = subprocess.run(['/usr/bin/sips','-g','pixelWidth','-g','pixelHeight',str(temporary)],
                                        capture_output=True, text=True, timeout=15)
                dims = re.findall(r'pixel(?:Width|Height):\s*(\d+)', result.stdout)
                if result.returncode or len(dims) != 2 or not 0 < int(dims[0])*int(dims[1]) <= 40_000_000:
                    raise ValueError('图片无法读取，或超过 4000 万像素。')
                temporary.chmod(0o600); temporary.replace(destination)
            finally:
                if temporary.exists(): temporary.unlink()
        return '/media/'+name
