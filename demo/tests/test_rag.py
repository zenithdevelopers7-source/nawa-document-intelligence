"""Phase 6 mock-provider tests. No key, network, or real LLM required."""
import json
from pathlib import Path
import unittest

from nawa_demo.rag import MockProvider, OpenAIProvider, ask_rag, validate_generated
from nawa_demo.retrieval import BM25Index, Passage, read_corpus

BASE = Path(__file__).resolve().parents[1]
CORPUS = BASE / 'samples/retrieval/passages.json'


class FixedProvider:
    def __init__(self, response):
        self.response = response
        self.calls = 0
    def generate(self, question, passages):
        self.calls += 1
        return self.response


class Phase6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.passages = read_corpus(CORPUS)

    def idx(self, tenant='demo-a', principal='reviewer-a'):
        return BM25Index(self.passages, tenant_id=tenant, principal_id=principal)

    def test_mock_english_citation(self):
        r = ask_rag(self.idx(), 'Example English Supplies grand total 1050')
        self.assertEqual(r['status'], 'answered')
        self.assertIn('Grand total AED 1050.00', r['answer'])
        self.assertEqual(r['citations'][0]['page'], 1)
        self.assertEqual(r['citations'][0]['source'], 'english_supplier.pdf')

    def test_mock_arabic_citation(self):
        r = ask_rag(self.idx(), 'شركة المثال الضريبة ٤٥')
        self.assertEqual(r['status'], 'answered')
        self.assertIn('٤٥', r['answer'])
        self.assertEqual(r['citations'][0]['passage_id'], 'ar-quote-v1-p1')

    def test_unknown_abstains_without_model_call(self):
        p = FixedProvider({'answer': 'unsafe', 'citations': []})
        r = ask_rag(self.idx(), 'interplanetary antimatter quantum', provider=p)
        self.assertEqual(r['status'], 'insufficient_evidence')
        self.assertEqual(p.calls, 0)

    def test_missing_payment_date_abstains_before_provider_call(self):
        for question in ('reviewer VAT payment', 'شركة المثال موعد دفع الضريبة'):
            with self.subTest(question=question):
                p = FixedProvider({'answer':'unsafe', 'citations':[]})
                result = ask_rag(self.idx(), question, provider=p)
                self.assertEqual(result['status'], 'insufficient_evidence')
                self.assertEqual(p.calls, 0)

    def test_explicit_missing_deadline_never_calls_provider(self):
        passage = Passage.from_dict({'passage_id':'absence','document_id':'doc','version':'1',
            'tenant_id':'t','page':1,'text':'Reviewer VAT deadline is not specified.',
            'allowed_principals':['p'],'source':'synthetic.txt','method':'synthetic'})
        provider = FixedProvider({'answer':'unsafe', 'citations':[]})
        result = ask_rag(BM25Index([passage], tenant_id='t', principal_id='p'),
                         'reviewer VAT deadline', provider=provider)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(provider.calls, 0)

    def test_restricted_abstains_without_provider_call(self):
        p = FixedProvider({'answer': 'Falcon Robotics', 'citations': []})
        r = ask_rag(self.idx(), 'Falcon Robotics confidential inspection', provider=p)
        self.assertEqual(r['status'], 'insufficient_evidence')
        self.assertEqual(p.calls, 0)

    def test_other_tenant_abstains(self):
        r = ask_rag(self.idx(), 'Exclusive tenant B satellite Orion')
        self.assertEqual(r['status'], 'insufficient_evidence')

    def test_admin_can_access_restricted(self):
        r = ask_rag(self.idx(principal='admin-a'), 'Falcon Robotics inspection')
        self.assertEqual(r['status'], 'answered')
        self.assertEqual(r['citations'][0]['source'], 'restricted.pdf')

    def test_invented_number_rejected(self):
        p = FixedProvider({'answer': 'Grand total AED 9000.00', 'citations': [
            {'passage_id': 'en-quote-v1-p1', 'quote': 'Grand total AED 1050.00.'}]})
        r = ask_rag(self.idx(), 'Example English Supplies grand total 1050', provider=p)
        self.assertEqual(r['status'], 'rejected_ungrounded')
        self.assertIsNone(r['answer'])
        self.assertEqual(r['citations'], [])

    def test_fake_citation_rejected(self):
        p = FixedProvider({'answer': 'Supplier Example English Supplies.', 'citations': [
            {'passage_id': 'fake-secret', 'quote': 'Supplier Example English Supplies.'}]})
        self.assertEqual(ask_rag(self.idx(), 'Example English Supplies grand total 1050', provider=p)['status'], 'rejected_ungrounded')

    def test_changed_quote_rejected(self):
        p = FixedProvider({'answer': 'Grand total AED 1050.00', 'citations': [
            {'passage_id': 'en-quote-v1-p1', 'quote': 'Grand total AED 1050.00!'}]})
        self.assertEqual(ask_rag(self.idx(), 'Example English Supplies grand total 1050', provider=p)['status'], 'rejected_ungrounded')

    def test_unsupported_entity_rejected(self):
        p = FixedProvider({'answer': 'Microsoft grand total AED 1050.00.', 'citations': [
            {'passage_id': 'en-quote-v1-p1', 'quote': 'Grand total AED 1050.00.'}]})
        self.assertEqual(ask_rag(self.idx(), 'Example English Supplies grand total 1050', provider=p)['status'], 'rejected_ungrounded')

    def test_duplicate_citation_rejected(self):
        hits = self.idx().search('English Supplies')
        pair = {'passage_id': hits[0]['passage_id'], 'quote': hits[0]['text']}
        self.assertIsNone(validate_generated({'answer': hits[0]['text'], 'citations': [pair, pair]}, hits))

    def test_missing_citations_rejected(self):
        hits = self.idx().search('English Supplies')
        self.assertIsNone(validate_generated({'answer': 'English Supplies', 'citations': []}, hits))

    def test_unexpected_provider_fields_rejected(self):
        hits = self.idx().search('English Supplies')
        self.assertIsNone(validate_generated({'answer': 'English Supplies', 'citations': [], 'role': 'system'}, hits))

    def test_provider_is_deterministic(self):
        idx = self.idx()
        self.assertEqual(ask_rag(idx, 'Example English Supplies grand total 1050'),
                         ask_rag(idx, 'Example English Supplies grand total 1050'))

    def test_openai_key_not_needed_for_mock(self):
        self.assertEqual(ask_rag(self.idx(), 'Example English Supplies grand total 1050', provider=MockProvider())['status'], 'answered')

    def test_bad_coverage_rejected(self):
        with self.assertRaises(ValueError):
            ask_rag(self.idx(), 'Example English Supplies', min_coverage=0)


if __name__ == '__main__':
    unittest.main()
