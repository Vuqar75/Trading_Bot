import unittest
from trading_bot.paper_watchdog import assess,transitions,recovery_proofs,format_event


def sample(now=10000):
    health={}
    for market in ('spot','futures','cross','margin'):
        health[market]={'time':now,'paused':market in ('cross','margin'),'pause_source':'agreed' if market in ('cross','margin') else None,'error':None,
            'blocked':None,'quote':{'time':now},'quotes_fresh':True,'protection_fresh':True,
            'protection_checked_at':now*1000 if market=='margin' else now,'data_at':now,'position_open':False}
    return {'services':{s:'active' for s in ('spot','futures','cross','margin','monitor','backup_timer')},
        'health':health,'queue':{'pending':0},'backups':{'local':now,'external':None,'external_configured':False},
        'resources':{'memory_available_bytes':200*1024*1024,'disk_free_bytes':600*1024*1024}}


class WatchdogHardeningTests(unittest.TestCase):
    def test_agreed_pauses_are_not_incidents_but_protection_is_independent(self):
        s=sample();problems=assess(s,10000)
        self.assertNotIn('pause:cross',problems);self.assertNotIn('pause:margin',problems)
        self.assertIn('setup:external',problems);self.assertNotIn('backup:external',problems)
        s['health']['cross']['protection_fresh']=False
        self.assertIn('protection:cross',assess(s,10000))

    def test_missing_parent_never_resolves_existing_protection_incident(self):
        s=sample();s['health']['cross']['protection_fresh']=False
        state,events=transitions(assess(s,10000),{},10000,healthy_keys=recovery_proofs(s,10000))
        self.assertEqual(sum(e['key']=='protection:cross' for e in events),1)
        s['health']['cross']=None
        state,events=transitions(assess(s,10001),state,10001,healthy_keys=recovery_proofs(s,10001))
        self.assertIn('protection:cross',state);self.assertFalse(any(e['kind']=='recovered' and e['key']=='protection:cross' for e in events))
        s=sample(10002)
        state,events=transitions(assess(s,10002),state,10002,healthy_keys=recovery_proofs(s,10002))
        self.assertIn('protection:cross',state)
        s=sample(10003)
        state,events=transitions(assess(s,10003),state,10003,healthy_keys=recovery_proofs(s,10003))
        self.assertNotIn('protection:cross',state);self.assertTrue(any(e['kind']=='recovered' and e['key']=='protection:cross' for e in events))

    def test_restart_preserves_first_seen_and_setup_is_sent_once(self):
        s=sample();s['health']['cross']['protection_fresh']=False
        state,first=transitions(assess(s,10000),{},10000,healthy_keys=recovery_proofs(s,10000))
        import json
        state=json.loads(json.dumps(state));s=sample(13601);s['health']['cross']['protection_fresh']=False
        state,second=transitions(assess(s,13601),state,13601,healthy_keys=recovery_proofs(s,13601))
        self.assertFalse(second);self.assertEqual(state['protection:cross']['first_seen'],10000)
        self.assertEqual(state['setup:external']['first_seen'],10000)

    def test_flapping_requires_two_consecutive_healthy_checks(self):
        s=sample();s['health']['cross']['protection_fresh']=False
        state,_=transitions(assess(s,10000),{},10000,healthy_keys=recovery_proofs(s,10000))
        s=sample(10001);state,_=transitions(assess(s,10001),state,10001,healthy_keys=recovery_proofs(s,10001))
        s=sample(10002);s['health']['cross']['protection_fresh']=False
        state,events=transitions(assess(s,10002),state,10002,healthy_keys=recovery_proofs(s,10002))
        self.assertIn('protection:cross',state);self.assertFalse(any(e['key']=='protection:cross' for e in events))

    def test_message_baku_occurrence_and_distinct_unknowns(self):
        message=format_event({'key':'protection:cross','kind':'failure','at':10010,'first_seen':10000,
                              'message':'cross: защита не подтверждена','last_success':None,
                              'observed_at':10010,'health_at':10009,'data_at':10008,'protection_at':10007})
        self.assertTrue(message.startswith('[Контроль системы]'));self.assertIn('1970-01-01 06:46:40',message)
        self.assertIn('защитной проверки',message);self.assertNotIn('Futures paper',message);self.assertNotIn('нет данных UTC',message)
        for text in ('Проверка наблюдателя','Health рынка','Время источника','Защитная проверка'):self.assertIn(text,message)

    def test_open_spot_and_isolated_positions_need_fresh_protection_marks(self):
        s=sample();s['health']['spot'].update(position_open=True,protection_checked_at=None)
        s['health']['futures'].update(position_open=True,protection_checked_at=9700)
        problems=assess(s,10000);self.assertIn('protection:spot',problems);self.assertIn('protection:futures',problems)
        s['health']['spot']['protection_checked_at']=10000;s['health']['futures']['protection_checked_at']=10000
        self.assertNotIn('protection:spot',assess(s,10000));self.assertNotIn('protection:futures',assess(s,10000))


if __name__=='__main__':unittest.main()
