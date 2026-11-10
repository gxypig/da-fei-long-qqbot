import json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.context_reset_offline import reset
from data.plugins.astrbot_plugin_proactive_agent.context_reset import cutoff
from data.plugins.astrbot_plugin_proactive_agent.history_recall import search

class ContextReset(unittest.TestCase):
 def test_offline_reset_keeps_delivery_outcomes_and_configuration(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);p=root/'plugin_data/astrbot_plugin_proactive_agent';p.mkdir(parents=True)
   manifest=root/'backup.json';manifest.write_text(json.dumps({'format':'qqbot-data-snapshot-v1','complete':True}))
   with closing(sqlite3.connect(root/'data_v4.db')) as db,db:
    db.executescript("CREATE TABLE conversations(content,title,token_usage,persona_id);INSERT INTO conversations VALUES('[1]','old',9,'persona');CREATE TABLE preferences(key,value);INSERT INTO preferences VALUES('history','old');INSERT INTO preferences VALUES('fixed_schedule_v1','timers');CREATE TABLE platform_message_history(content);INSERT INTO platform_message_history VALUES('old');")
   def records(name,rows):
    with closing(sqlite3.connect(p/name)) as db,db:
     db.execute('CREATE TABLE records(id PRIMARY KEY,revision,body)');db.executemany('INSERT INTO records VALUES(?,1,?)',[(k,json.dumps(v)) for k,v in rows.items()])
   records('delivery-ledger.sqlite3',{'a':{'at':1,'cid':'current','state':'confirmed','message_id':'receipt','meme_plan':{'input':'old'}},'b':{'at':2,'cid':'current','state':'unknown','text':'old'}})
   (p/'delivery-ledger.pending.json').write_text(json.dumps({'b':{'revision':2,'at':2,'cid':'current','state':'unknown','text':'newer spool'},'c':{'revision':1,'at':3,'cid':'current','state':'confirmed'}}))
   records('proactive-life.sqlite3',{'event:old':{'summary':'old','record_type':'event'},'contact:one':{'last_sent_at':9,'consumed_events':['old']},'unknown':{'status':'unknown','basis':{'summary':'old'},'delivery_id':'b'}})
   gallery=root/'plugin_data/astrbot_plugin_smart_imagechat_hub';gallery.mkdir();(gallery/'image_index.json').write_text('gallery')
   result=reset(root,backup_manifest=manifest,at=100)
   self.assertEqual(cutoff(p/'context-reset.json'),100);self.assertEqual(result['state'],'complete')
   with closing(sqlite3.connect(root/'data_v4.db')) as db:
    self.assertEqual(db.execute('SELECT * FROM conversations').fetchone(),('[]','',0,'persona'));self.assertEqual(db.execute('SELECT key FROM preferences').fetchall(),[('fixed_schedule_v1',)])
   with closing(sqlite3.connect(p/'delivery-ledger.sqlite3')) as db:rows=[json.loads(x[0]) for x in db.execute('SELECT body FROM records')]
   self.assertEqual({r['state'] for r in rows},{'confirmed','unknown'});self.assertTrue(all(r['cid'].startswith('context-reset:') for r in rows));self.assertTrue(all('meme_plan' not in r for r in rows))
   self.assertEqual(len(rows),3);self.assertIn('newer spool',[r.get('text') for r in rows]);self.assertFalse((p/'delivery-ledger.pending.json').exists())
   with closing(sqlite3.connect(p/'proactive-life.sqlite3')) as db:rows=dict(db.execute('SELECT id,body FROM records'))
   self.assertNotIn('event:old',rows);self.assertIn('contact:one',rows);self.assertNotIn('basis',json.loads(rows['unknown']))
   self.assertEqual((gallery/'image_index.json').read_text(),'gallery')
 def test_missing_backup_denies_reset_and_old_history_epoch(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);backup=root/'backup.json';backup.write_text('{"format":"qqbot-data-snapshot-v1","complete":false}')
   with self.assertRaises(ValueError):reset(root,backup_manifest=backup)
  recent=[{'origin':'test:FriendMessage:1','role':'user','time':1,'text':'猫叫芝麻','sender_id':'1'},{'origin':'test:FriendMessage:1','role':'user','time':200,'text':'猫叫奶茶','sender_id':'1'}]
  result=search(None,'test:FriendMessage:1','查猫叫什么','1',recent=recent,cutoff=100)
  self.assertTrue(result['rows']);self.assertTrue(all('芝麻' not in r['text'] for r in result['rows']))
 def test_invalid_or_incomplete_marker_cannot_restore_old_context(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'context-reset.json'
   for row in ({'at':-1},{'at':'invalid'},{'at':float('nan')},{'at':100,'state':'resetting'},{}):
    path.write_text(json.dumps(row),'utf-8')
    with self.subTest(row=row),self.assertRaises(ValueError):cutoff(path)
