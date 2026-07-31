from pathlib import Path
from unittest.mock import MagicMock, patch
import hashlib
import shlex
import adb_bot.automation.flows.instagram as mod

local_path = Path('tests/fixtures/story_media/sample (2).jpg')
local_path.parent.mkdir(parents=True, exist_ok=True)
local_path.write_bytes(b'fake-image')
remote_path = '/sdcard/images/Unknown/sample (2).jpg'
expected = shlex.quote(remote_path)

digest = hashlib.sha256()
digest.update(b'fake-image')
local_hash = digest.hexdigest()

with patch('adb_bot.automation.flows.instagram.subprocess.run') as mocked_run:
    mocked_run.return_value = MagicMock(returncode=0, stdout=f'{local_hash}  {remote_path}\n', stderr='')
    result = mod._adb_verify_remote_media_matches_local('target-device', str(local_path), remote_path, logger=MagicMock())
    print('result', result)
    print('expected remote arg', expected)
    print('call args', mocked_run.call_args_list)
    if mocked_run.call_args_list:
        args = mocked_run.call_args_list[0][0][0]
        print('args list len', len(args))
        for i, arg in enumerate(args):
            print('arg', i, repr(arg))
