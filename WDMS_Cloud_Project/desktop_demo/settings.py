from __future__ import annotations
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass
class Settings:
    wdms_exe: str
    username: str
    output_dir: str
    credential_target: str
    schema_version: int = 1


def settings_path():
    base=Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local'/'share'))
    return base/'WDMSInventoryDemo'/'settings.json'


def validate_executable(value):
    path=Path(value.strip()).expanduser()
    if not path.is_file() or path.suffix.lower()!='.exe':
        raise ValueError('请选择实际存在的 WDMS .exe 程序。')
    return str(path.resolve())


def validate_output(value):
    if not value.strip():
        raise ValueError('请选择报表保存文件夹。')
    path=Path(value.strip()).expanduser()
    try:
        path.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as handle:
            handle.write(b'WDMS write check')
            handle.flush()
    except OSError as error:
        raise ValueError('该文件夹无法写入，请选择其他位置。') from error
    return str(path.resolve())


def load_settings(path=None):
    path=path or settings_path()
    if not path.exists():
        return None
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        settings=Settings(**data)
        if settings.schema_version!=1 or not all(isinstance(value,str) and value.strip() for value in (settings.wdms_exe,settings.username,settings.output_dir,settings.credential_target)):
            raise ValueError()
        return settings
    except (ValueError,TypeError,OSError) as error:
        raise ValueError('本机配置无法读取，请重新设置。') from error


def save_settings(exe,username,password,output,credentials,previous=None,path=None):
    # Validate everything before writing credentials. Password never enters JSON.
    exe=validate_executable(exe)
    output=validate_output(output)
    username=username.strip()
    if not username:
        raise ValueError('请填写 WDMS 账号。')
    target=previous.credential_target if previous else 'WDMSInventoryDemo:'+uuid.uuid4().hex
    if not password and (not previous or username!=previous.username or not credentials.exists(target)):
        raise ValueError('请填写密码；修改账号时也需要重新填写密码。')
    if password:
        credentials.write(target,username,password)
    settings=Settings(exe,username,output,target)
    path=path or settings_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp_name=tempfile.mkstemp(prefix='settings_',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output_file:
            json.dump(asdict(settings),output_file,ensure_ascii=False,indent=2)
        Path(temp_name).replace(path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return settings
