import unittest
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.send_checks import verbatim_request,own_style,issues
from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind
from data.plugins.astrbot_plugin_proactive_agent.conversation_controls import goodbye,ConversationControls
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
from test_fullchain_foundation import event


class OriginalInputRegressions(unittest.IsolatedAsyncioTestCase):
    def test_goodbye_variants_keep_new_topics_and_receipt_boundary(self):
        from types import SimpleNamespace
        for text in ('这回不喊了，睡啦','唔，那我睡啦','唔…好梦','唔…晚安呀','好梦呀','嗯嗯，早点睡，晚安','你也快睡'):
            self.assertTrue(goodbye(text),text)
        for text in ('晚安，明天几点开会','晚安，但是我有点难受','先不睡了，继续聊','晚安你看到了吗？','好梦是什么意思'):
            self.assertFalse(goodbye(text),text)
        rows={};ledger=SimpleNamespace(store=SimpleNamespace(rows=rows,put=lambda k,v:rows.__setitem__(k,v)))
        c=ConversationControls(ledger,clock=lambda:100)
        row={'origin':'aiocqhttp:FriendMessage:1','sender':'2','source_text':'唔，晚安呀','text':'好梦，手机放下就闭眼吧','state':'failed'}
        c.delivered(row);self.assertFalse(rows)
        row['state']='confirmed';c.delivered(row);self.assertEqual(next(iter(rows.values()))['closed_until'],700)
        for text in ('嗯嗯','唔…眼睛睁不开了','闭眼','😴'):
            job=SimpleNamespace(origin=row['origin'],sender=row['sender'],text=text,event=event(text=text))
            self.assertIsNotNone(c.before(job),text)
        from astrbot.api.message_components import Image
        e=event(text='晚安');e.get_messages=lambda:[Image.fromURL('https://example.invalid/new.png')]
        self.assertIsNone(c.before(SimpleNamespace(origin=row['origin'],sender=row['sender'],text='晚安',event=e)))
        self.assertEqual(next(iter(rows.values()))['closed_until'],0)
        c.delivered(row)
        self.assertIsNone(c.before(SimpleNamespace(origin=row['origin'],sender=row['sender'],text='等下，明天几点开会？',event=event())))

    async def test_split_machine_error_links_only_same_sender_and_active_head(self):
        head="The prompt could not be submitted. The prompt contains sensitive words that violate Google's [Generative AI Prohibited U"
        tail='se policy](https://policies.google.com/terms/generative-ai/use-policy). Try rephrasing the prompt. If you think this was'
        now=[100.];q=ReplyQueue({}.get,clock=lambda:now[0])
        a=q.create(event(text=head,id_='head'));q.submit(a)
        b=q.create(event(text=tail,id_='tail'));q.submit(b);self.assertEqual(b.state,'merged')
        other=q.create(event(text=tail,id_='other'));other.sender='different';q.submit(other);self.assertNotEqual(other.state,'merged');q.finish(other,'cancelled')
        q.finish(a,'failed');c=q.create(event(text=tail,id_='after-failure'));q.submit(c);self.assertEqual(c.state,'running');q.finish(c,'confirmed')
        d=q.create(event(text=head+' 为什么会这样？',id_='help'));q.submit(d);self.assertEqual(d.state,'running');q.finish(d,'confirmed')
        e=q.create(event(text=head,id_='head-again'));q.submit(e);q.finish(e,'confirmed')
        now[0]=221;f=q.create(event(text=tail,id_='expired-tail'));q.submit(f);self.assertEqual(f.state,'running');q.finish(f,'confirmed')

    def test_completed_drawing_is_not_a_new_request_but_followup_is(self):
        for text in ('会啊，我给他画过腹肌图，懂不懂','我刚给他画了张图','你上次给我画过猫吗','我刚拍了一张风景'):
            self.assertEqual(generation_kind(text),'none',text)
        for text in ('给他画一张腹肌图','我给他画过猫，再画一张狗','我画过他了；现在想看你自拍','帮我画过期食品的海报','画好了发给我','你画过猫现在再画一张狗'):
            self.assertNotEqual(generation_kind(text),'none',text)

    def test_newline_copy_preserves_characters_whitespace_and_cr_delimiters(self):
        source='1. a\n 6. "EDMFunc"\n8. ã🙂\n<think_example>\n'
        for header in ('@123456789能帮忙复述一下这些句子吗','請用文字复述以下每一条句子','请原样复述以下内容：'):
            for delim in ('\n','\r','\r\n'):
                exact=verbatim_request(header+delim+source)
                self.assertEqual(exact,source,(header,repr(delim)))
                self.assertEqual(own_style('wrong draft',{'verbatim_text':exact}),source)
                self.assertEqual(issues(source,state={'verbatim_text':exact}),[])
        for text in ('不要复述以下内容\n甲乙','请概括复述这些句子的大意\n甲乙','为什么复述下面这些句子会变字？\n甲乙','帮我复述一下刚才的话'):
            self.assertIsNone(verbatim_request(text),text)

if __name__=='__main__':unittest.main()
