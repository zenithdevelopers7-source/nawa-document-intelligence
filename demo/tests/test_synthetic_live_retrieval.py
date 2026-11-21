"""Synthetic callback checks, not proof of real identity or source authorization."""
from dataclasses import replace
import unittest

from nawa_demo.authorization import Principal
from nawa_demo.retrieval import BM25Index, Passage
from nawa_demo.synthetic_live_retrieval import EvidenceUnavailable, PassageSnapshot, SyntheticLiveRetrieval


def passage(pid, tenant, version, text, principals):
    return Passage.from_dict({'passage_id':pid, 'document_id':'doc', 'version':version,
        'tenant_id':tenant, 'page':1, 'text':text, 'allowed_principals':principals,
        'source':'synthetic.txt', 'method':'synthetic'})


class LiveRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.members = {('alice','a'):Principal('alice','a','analyst'),
                        ('bob','b'):Principal('bob','b','viewer')}
        self.docs = [passage('a-v1','a','1','shared project alpha',['alice']),
                     passage('b-v1','b','1','shared project beta',['bob'])]
        self.service = SyntheticLiveRetrieval(lambda s,t:self.members.get((s,t)), lambda p:list(self.docs))

    def test_two_tenants_and_positive_controls(self):
        self.assertEqual([h['passage_id'] for h in self.service.search('alice','a','shared')], ['a-v1'])
        self.assertEqual([h['passage_id'] for h in self.service.search('bob','b','shared')], ['b-v1'])
        with self.assertRaises(EvidenceUnavailable):
            self.service.resolve('alice','a','b-v1')
        self.assertEqual(self.service.resolve('bob','b','b-v1').version, '1')

    def test_stale_index_risk_and_refreshed_version(self):
        old_index = BM25Index(self.docs, tenant_id='a', principal_id='alice')
        self.docs[0] = passage('a-v2','a','2','shared project revised',['alice'])
        self.assertEqual(old_index.search('shared')[0]['passage_id'], 'a-v1')
        self.assertEqual(self.service.search('alice','a','shared')[0]['passage_id'], 'a-v2')
        with self.assertRaises(EvidenceUnavailable):
            self.service.resolve('alice','a','a-v1')

    def test_revocation_and_withdrawal_on_next_request(self):
        self.docs.pop(0)
        with self.assertRaises(EvidenceUnavailable):
            self.service.resolve('alice','a','a-v1')
        self.members[('alice','a')] = replace(self.members[('alice','a')], active=False)
        with self.assertRaises(EvidenceUnavailable):
            self.service.search('alice','a','shared')
        self.assertEqual(self.service.search('bob','b','shared')[0]['passage_id'], 'b-v1')

    def test_changed_source_between_reads_fails_closed(self):
        calls = []
        def changed(principal):
            calls.append(1)
            if len(calls) == 2:
                self.docs[0] = passage('a-v2','a','2','shared revised',['alice'])
            return list(self.docs)
        service = SyntheticLiveRetrieval(lambda s,t:self.members.get((s,t)), changed)
        with self.assertRaises(EvidenceUnavailable):
            service.search('alice','a','shared')

    def test_grant_change_and_restore_between_reads_fails_closed(self):
        calls = []
        def snapshots(principal):
            calls.append(1)
            return PassageSnapshot((self.docs[0],), (('a-v1', 1 if len(calls) == 1 else 3),))
        service = SyntheticLiveRetrieval(lambda s,t:self.members.get((s,t)), snapshots)
        with self.assertRaises(EvidenceUnavailable):
            service.search('alice','a','shared')

    def test_generation_metadata_must_match_the_authorized_snapshot(self):
        service = SyntheticLiveRetrieval(lambda s,t:self.members.get((s,t)),
            lambda p:PassageSnapshot((self.docs[0],), (('wrong-id', 1),)))
        with self.assertRaises(EvidenceUnavailable):
            service.search('alice','a','shared')

    def test_membership_generation_change_between_reads_fails_closed(self):
        calls = []
        def resolve(subject, tenant):
            calls.append(1)
            p = self.members.get((subject, tenant))
            return replace(p, membership_generation=1) if len(calls) == 2 else p
        with self.assertRaises(EvidenceUnavailable):
            SyntheticLiveRetrieval(resolve, lambda p:list(self.docs)).search('alice','a','shared')

    def test_expected_session_principal_must_match_current_membership(self):
        old = self.members[('alice','a')]
        self.members[('alice','a')] = replace(old, membership_generation=1)
        with self.assertRaises(EvidenceUnavailable):
            self.service.search('alice','a','shared', expected_principal=old)
        with self.assertRaises(EvidenceUnavailable):
            self.service.resolve('alice','a','a-v1', expected_principal=old)

    def test_dependency_failure_does_not_reveal_details(self):
        def failed(*args):
            raise RuntimeError('internal synthetic database detail')
        with self.assertRaises(EvidenceUnavailable) as caught:
            SyntheticLiveRetrieval(failed, lambda p:[]).search('alice','a','shared')
        self.assertNotIn('internal', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
