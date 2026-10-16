"""Local owner-list validation and session-bound snapshots for automatic collection."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from errors import WDMSError


OWNER_COUNT = 24
SNAPSHOT_REVISION = 'native-owner-list-v1'


def validated_names(names):
    if (not isinstance(names, (list, tuple)) or len(names) != OWNER_COUNT
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(set(names)) != OWNER_COUNT):
        raise WDMSError('OWNER_LIST_INVALID', '当前业务需要24个不同的非空货主名称，实际名单不符合；未开始查询。')
    return tuple(names)


def fingerprint(names):
    data = json.dumps(validated_names(names), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def context_key(config):
    # Keep different configured executables/accounts/credential targets apart.
    data = [os.path.normcase(str(Path(config.wdms_exe).resolve())), config.username, config.credential_target]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode('utf-8')).hexdigest()


def read_plan(path, expected_sha256=None):
    try:
        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise WDMSError('OWNER_PLAN_CHANGED', '本轮名单文件发生变化，停止且不查询。')
        document = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(document, dict):
            raise ValueError('not an object')
        names = validated_names(document.get('owners'))
        if 'revision' in document or 'identity' in document:
            if (document.get('schema_version') != 1 or document.get('revision') != SNAPSHOT_REVISION
                    or document.get('fingerprint') != fingerprint(names)):
                raise ValueError('snapshot protocol differs')
            identity = document.get('identity')
            if (not isinstance(identity, dict) or set(identity) != {'pid', 'main_hwnd'}
                    or any(type(value) is not int or value <= 0 for value in identity.values())
                    or not isinstance(document.get('context_key'), str) or len(document['context_key']) != 64):
                raise ValueError('snapshot identity differs')
        return document, names, digest
    except WDMSError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise WDMSError('OWNER_PLAN_INVALID', '本轮货主名单无法完整读取或校验，未开始查询。') from error


def write_snapshot(path, names, config, root):
    names = validated_names(names)
    document = {'schema_version': 1, 'revision': SNAPSHOT_REVISION, 'owners': names,
                'fingerprint': fingerprint(names), 'context_key': context_key(config),
                'identity': {'pid': root['pid'], 'main_hwnd': root['hwnd']}}
    try:
        with Path(path).open('x', encoding='utf-8') as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    except OSError as error:
        raise WDMSError('OWNER_SNAPSHOT_SAVE_FAILED', '本轮名单快照保存失败，未开始查询；不会覆盖已有文件。') from error


def prepare_collection(path, config, *, cancelled=None):
    """Validate this run's captured list; name review happens in the Excel report."""
    cancelled = cancelled or (lambda: False)
    if cancelled():
        raise WDMSError('TASK_CANCELLED', '任务已取消，未开始库存查询。')
    document, names, digest = read_plan(path)
    if (document.get('revision') != SNAPSHOT_REVISION
            or document.get('context_key') != context_key(config)):
        raise WDMSError('OWNER_SESSION_CHANGED', '读取名单后本机配置或会话来源变化，未开始查询。')
    read_plan(path, expected_sha256=digest)
    if cancelled():
        raise WDMSError('TASK_CANCELLED', '任务已取消，未开始库存查询。')
    return names, digest
