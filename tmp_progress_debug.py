from pathlib import Path
from unittest.mock import MagicMock, patch
from adb_bot.automation.flows.instagram import InstagramStoryUploadFlow
from adb_bot.core.models import Profile

flow = InstagramStoryUploadFlow()
profile = Profile(id='profile-1', status='active', ip='127.0.0.1', port='5554', pwd='pwd')
adb_client = MagicMock()
adb_client.run_command.return_value = None
media_path = Path('tests/fixtures/story_media/sample.jpg')
media_path.parent.mkdir(parents=True, exist_ok=True)
media_path.write_bytes(b'fake-image')

with patch('adb_bot.automation.flows.instagram._adb_resolve_story_media_path', return_value=str(media_path)), \
     patch('adb_bot.automation.flows.instagram._adb_push_media_to_device', return_value=True), \
     patch('adb_bot.automation.flows.instagram._adb_verify_remote_media_exists', return_value=True), \
     patch('adb_bot.automation.flows.instagram._adb_verify_remote_media_matches_local', return_value=True), \
     patch('adb_bot.automation.flows.instagram._adb_wait_for_instagram_story_composer', return_value=True), \
     patch('adb_bot.automation.flows.instagram._adb_ensure_instagram_feed_visible', return_value=True), \
     patch.object(flow, '_open_story_composer', return_value=True), \
     patch.object(flow, '_select_story_media', return_value=True), \
     patch.object(flow, '_tap_your_story', return_value=True), \
     patch.object(flow, '_verify_story_post_completed', return_value=True), \
     patch('adb_bot.automation.flows.instagram._adb_find_instagram_home_button_center', return_value=None), \
     patch('adb_bot.automation.flows.instagram.time.sleep', return_value=None):
    result = flow.run(profile, adb_client=adb_client, logger=MagicMock())

print('result:', result)
print('mark count:', adb_client.mark_progress_step.call_count)
for idx, call in enumerate(adb_client.mark_progress_step.call_args_list, start=1):
    print('call', idx, call)
