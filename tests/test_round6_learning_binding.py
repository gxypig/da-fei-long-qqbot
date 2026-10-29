import tempfile,unittest,json,time,asyncio
from pathlib import Path
from unittest.mock import Mock
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.idle_learning import IdleLearning
from data.plugins.astrbot_plugin_proactive_agent.retired_features import migrate

class BindingTests(unittest.TestCase):
    def test_upgrade_quarantines_removed_reaction_without_erasing_samples(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'learn.sqlite3'
            def create():return IdleLearning(path,{},SimpleNamespace(path=None),AsyncMock(),Mock(),Mock(),lambda:False)
            service=create();now=time.time()
            with service._db:
                service._db.execute("DELETE FROM meta WHERE key='removed_counterattack_v1'")
                for index,mood in enumerate(('timid','rebound','serious','cheeky')):
                    payload={'text':'保留原样'+str(index),'send_state':'confirmed','character':{'mood':mood},'exclusion_reasons':['independent_reason']}
                    service._db.execute('INSERT INTO cases(fingerprint,category,payload,created,expires) VALUES(?,?,?,?,?)',
                        (str(index),'no_template',json.dumps(payload),now,now+1000))
            asyncio.run(service.close())
            service=create()
            rows=[json.loads(r[0]) for r in service._db.execute('SELECT payload FROM cases ORDER BY id')]
            self.assertEqual(len(rows),4)
            for index,row in enumerate(rows):
                self.assertEqual(row['text'],'保留原样'+str(index))
                self.assertIn('independent_reason',row['exclusion_reasons'])
                self.assertEqual('removed_counterattack_feature' in row['exclusion_reasons'],index<2)
                if index<2:self.assertTrue(row['exclude_evidence'])
            asyncio.run(service.close())

    def test_receipt_sample_keeps_role_version_and_relevant_adapters(self):
        state={'persona_id':'second','persona_status':'selected','persona_version':'v42',
            'persona_variants':{'chat':'工程师性格'*50,'review':'保持理性','bili':'不用于这个案例'},'secret':'exclude'}
        case=IdleLearning._case('测试正文',[], 'p:FriendMessage:1',state)
        self.assertEqual(case['character']['persona_version'],'v42')
        self.assertEqual(case['character']['persona_id'],'second')
        self.assertEqual(case['character']['persona_variants'],{k:state['persona_variants'][k] for k in ('chat','review')})
        self.assertNotIn('secret',case['character'])

    def test_retired_switch_migration_preserves_other_fields_and_restores_on_error(self):
        class Config(dict):
            save_config=Mock()
        c=Config(counterattack_apology_enabled=True,personality='保留用户设定')
        self.assertTrue(migrate(c));self.assertEqual(c,{'personality':'保留用户设定'})
        self.assertFalse(migrate(c));c.save_config.assert_called_once()
        c['counterattack_apology_enabled']=False;c.save_config=Mock(side_effect=OSError())
        with self.assertRaises(OSError):migrate(c)
        self.assertIs(c['counterattack_apology_enabled'],False)
