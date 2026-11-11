import unittest
from types import SimpleNamespace
from test_round8_visual import Event
from astrbot.api.message_components import Image
from data.plugins.astrbot_plugin_proactive_agent import image_spec as spec

class MediaContext(unittest.TestCase):
    def test_full_video_request_drops_previous_apple_and_recalled_chat_image(self):
        event=Event('给我拍一小段你对着镜头挥手的视频，白色日常连衣裙，自拍视角，手和表情都要动',origin='probe:FriendMessage:a')
        req=SimpleNamespace(contexts=[{'role':'user','content':'拍张拿苹果的自拍'},{'role':'assistant','content':'本龙的自拍，苹果看清了吧'}],image_urls=['previous-chat.png'],extra_user_content_parts=[])
        self.assertTrue(spec.independent_description(event.text))
        self.assertEqual(spec.context_material(event,req)['recent'],[])
        self.assertEqual(spec.reference_paths(event,req),[])
    def test_terse_video_and_explicit_edit_keep_requirements(self):
        for text in ('我要看你拍的视频','拍个视频看看','这个视频里你要穿蓝色卫衣，小鸭继续拿着','还是刚才那套，挥挥手'):
            self.assertFalse(spec.independent_description(text),text)
    def test_direct_new_attachment_remains_available(self):
        event=Event('拍张你的自拍，穿蓝色卫衣，手里拿着红苹果',images=[Image.fromFileSystem('new-original.png')])
        req=SimpleNamespace(image_urls=['new-original.png','old-chat.png'])
        direct=next(c for c in event.get_messages() if type(c).__name__=='Image')
        self.assertEqual(spec.reference_paths(event,req),[direct.path or direct.file])
    def test_selfie_prompt_has_two_arm_constraint_without_removing_required_prop(self):
        text=spec.prompt({'subject_self':True,'identity':'龙娘','scene':'自拍挥手，苹果在桌上','requirements':['苹果可见'],'camera_mode':'selfie'},frames=True)
        self.assertIn('正常双臂',text);self.assertIn('苹果可见',text);self.assertIn('连续六帧',text)
    def test_requested_animation_cannot_become_completed_fact(self):
        from data.plugins.astrbot_plugin_proactive_agent.image_generation import review_media
        event=Event('拍个视频');event.set_extra('qqbot_requested_animation',True)
        self.assertIsNone(review_media(event,SimpleNamespace(qqbot_generated_image=True)))
        event.set_extra('qqbot_image_provenance',{'format':'gif','frames':6})
        self.assertIsNone(review_media(event,SimpleNamespace(qqbot_generated_image=False)))
    def test_own_mirror_selfie_keeps_mirror_and_anatomy_requirements(self):
        text=spec.prompt({'subject_self':True,'identity':'龙娘','scene':'镜前自拍','requirements':[], 'camera_mode':'mirror_selfie'})
        self.assertIn('必须是镜前自拍',text);self.assertIn('正常双臂',text)
    def test_static_picture_is_not_promoted_to_animation(self):
        from data.plugins.astrbot_plugin_proactive_agent.image_generation import media_details
        from test_round13_images import picture
        self.assertEqual(media_details(picture()),{'format':'png','frames':1})
