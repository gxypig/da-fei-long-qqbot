import asyncio,copy,unittest
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.identity_expression import detect,guard
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer
from data.plugins.astrbot_plugin_proactive_agent.send_checks import own_style

STATE={'persona_status':'selected','persona_prompt':'你是白毛龙娘大肥龙\n身份备用短答：本龙是大肥龙，白毛龙娘','persona_variants':{'review':'身份只按白毛龙娘回应'},'identity_scene':True}
class IdentityTests(unittest.IsolatedAsyncioTestCase):
 def test_scope(self):
  for text in ('你是谁','你是什么AI','你底层是什么模型','你是不是GPT','大肥龙是DeepSeek吗','你是啥？'):
   with self.subTest(text=text):self.assertTrue(detect(text,STATE))
  for text in ('你怎么看GPT','你是什么时候休息','你觉得哪个模型好','他说“你是AI”是什么意思','你用的虚拟机怎么安装','你知道什么模型支持图片','哪个模型适合写前端'):
   with self.subTest(text=text):self.assertFalse(detect(text,STATE))
 def test_followup_sender_and_age(self):
  rows=[{'text':'你是什么模型','sender':'a','time':100}]
  self.assertTrue(detect('我问的是底层型号',STATE,rows,'a',200))
  self.assertFalse(detect('我问的是底层型号',STATE,rows,'b',200))
  self.assertFalse(detect('底层呢',STATE,rows,'a',800))
 def test_narrow_local_fallback(self):
  for text in ('白发、有龙角和龙尾的虚拟龙娘','本龙是白毛龙娘，具体底层型号没有确切信息','本龙是AI扮演的龙娘','是人工智能，平时以白毛龙娘身份聊天'):
   state=copy.deepcopy(STATE);self.assertEqual(own_style(text,state), '本龙是大肥龙，白毛龙娘');self.assertIn('identity_guard',state)
  self.assertEqual(guard('龙娘呀，白毛的这只',STATE),'龙娘呀，白毛的这只')
  self.assertEqual(guard('“虚拟机”这个词是引用',STATE),'“虚拟机”这个词是引用')
 def test_other_persona_and_nonidentity(self):
  for state in ({'persona_prompt':''},{'persona_prompt':'你是技术助理'},{**STATE,'persona_status':'disabled'},{**STATE,'identity_scene':False}):
   self.assertEqual(guard('我是虚拟龙娘',state),'我是虚拟龙娘')
  self.assertEqual(guard('型号未知',STATE,'management'),'型号未知')
 async def test_identity_repair_still_requires_independent_judge(self):
  calls=[]
  async def generated(system,data,origin):
   calls.append(data)
   if len(calls)==2:return '{"reply":"具体底层型号，本龙这边没有确切信息"}'
   if len(calls)==4:return '{"reply":"龙骨架，外面裹龙鳞"}'
   if len(calls) in (3,5):return '{"pass":true,"natural":true,"chat_fit":true}'
   return '{"pass":false,"natural":true,"chat_fit":false,"reason":"没有接住底层型号追问","replacement":"具体底层型号，本龙这边没有确切信息"}'
  reviewer=ReplyReviewer(generated,lambda *a:None,enabled=lambda:True);state=copy.deepcopy(STATE)
  text=await reviewer._review('本龙是白毛龙娘',origin='test',character=state)
  self.assertEqual(text,'龙骨架，外面裹龙鳞');self.assertEqual(len(calls),5);self.assertFalse(state.get('identity_guard_review_override',False))
  self.assertEqual(calls[2]['待发文字'],'具体底层型号，本龙这边没有确切信息')

if __name__=='__main__':unittest.main()
