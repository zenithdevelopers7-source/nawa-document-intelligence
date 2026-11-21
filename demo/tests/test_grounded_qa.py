"""Phase 5 grounded answer, citation integrity, and permission isolation tests."""
import unittest
from pathlib import Path
from nawa_demo.grounded_qa import ask
from nawa_demo.retrieval import BM25Index, Passage, read_corpus

CORPUS = Path(__file__).resolve().parents[1] / 'samples/retrieval/passages.json'


class GroundedQATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.passages = read_corpus(CORPUS)

    def idx(self, tenant='demo-a', principal='reviewer-a'):
        return BM25Index(self.passages, tenant_id=tenant, principal_id=principal)

    def test_english_answer_with_citation(self):
        r = ask(self.idx(), 'Example English Supplies grand total 1050')
        self.assertEqual(r['status'], 'answered')
        self.assertIn('Grand total AED 1050.00', r['answer'])
        self.assertEqual(r['citations'][0]['page'], 1)
        self.assertEqual(r['citations'][0]['source'], 'english_supplier.pdf')

    def test_arabic_answer(self):
        r = ask(self.idx(), 'شركة المثال الضريبة ٤٥')
        self.assertEqual(r['status'], 'answered')
        self.assertIn('٤٥', r['answer'])
        self.assertEqual(r['citations'][0]['passage_id'], 'ar-quote-v1-p1')

    def test_policy_evidence(self):
        r = ask(self.idx(), 'human reviewer verify evidence VAT discrepancies')
        self.assertEqual(r['status'], 'answered')
        self.assertEqual(r['citations'][0]['page'], 2)

    def test_no_hallucinated_answer(self):
        r = ask(self.idx(), 'interplanetary antimatter quantum')
        self.assertEqual(r['status'], 'insufficient_evidence')
        self.assertIsNone(r['answer'])
        self.assertEqual(r['citations'], [])

    def test_missing_payment_date_abstains_despite_partial_overlap(self):
        for question in ('VAT payment due date', 'reviewer VAT payment', 'reviewer VAT deadline',
                         'موعد دفع الضريبة', 'شركة المثال موعد دفع الضريبة', 'شركة المثال دفع الضريبة'):
            with self.subTest(question=question):
                result = ask(self.idx(), question)
                self.assertEqual(result['status'], 'insufficient_evidence')
                self.assertIsNone(result['answer'])
                self.assertEqual(result['citations'], [])

    def test_lower_threshold_is_an_explicit_less_conservative_override(self):
        result = ask(self.idx(), 'reviewer VAT payment', min_coverage=0.60)
        self.assertEqual(result['status'], 'answered')
        self.assertNotIn('payment', result['answer'].casefold())

    def test_explicit_absence_phrases_abstain_with_positive_controls(self):
        examples = [
            ('Reviewer VAT deadline is not specified.', 'reviewer VAT deadline', 'insufficient_evidence'),
            ('Reviewer VAT deadline is NOT PROVIDED.', 'reviewer VAT deadline', 'insufficient_evidence'),
            ('No reviewer VAT deadline specified.', 'reviewer VAT deadline', 'insufficient_evidence'),
            ('موعد دفع الضريبة غير محدد.', 'موعد دفع الضريبة', 'insufficient_evidence'),
            ('موعد دفع الضريبة لم يتم تحديد.', 'موعد دفع الضريبة', 'insufficient_evidence'),
            ('Reviewer VAT deadline 2026-12-01.', 'reviewer VAT deadline', 'answered'),
            ('موعد دفع الضريبة ٢٠٢٦ ١٢ ٠١.', 'موعد دفع الضريبة', 'answered'),
        ]
        for text, question, status in examples:
            with self.subTest(text=text):
                passage = Passage.from_dict({'passage_id':'fixture','document_id':'doc','version':'1',
                    'tenant_id':'t','page':1,'text':text,'allowed_principals':['p'],
                    'source':'synthetic.txt','method':'synthetic'})
                result = ask(BM25Index([passage], tenant_id='t', principal_id='p'), question)
                self.assertEqual(result['status'], status)
                if status == 'insufficient_evidence':
                    self.assertIsNone(result['answer'])
                    self.assertEqual(result['citations'], [])

    def test_unknown_absence_word_remains_an_open_semantic_limit(self):
        passage = Passage.from_dict({'passage_id':'unknown','document_id':'doc','version':'1',
            'tenant_id':'t','page':1,'text':'Reviewer VAT deadline remains unknown.',
            'allowed_principals':['p'],'source':'synthetic.txt','method':'synthetic'})
        result = ask(BM25Index([passage], tenant_id='t', principal_id='p'), 'reviewer VAT deadline')
        # This records an open limitation; it is not a safety success.
        self.assertEqual(result['status'], 'answered')
        self.assertIn('unknown', result['answer'])

    def test_unauthorized_information_not_exposed(self):
        r = ask(self.idx(), 'Falcon Robotics confidential inspection')
        self.assertEqual(r['status'], 'insufficient_evidence')
        self.assertNotIn('Falcon', str(r['answer']))
        self.assertEqual(r['citations'], [])

    def test_other_tenant_isolation(self):
        r = ask(self.idx(), 'Exclusive tenant B satellite Orion')
        self.assertEqual(r['status'], 'insufficient_evidence')

    def test_admin_can_retrieve_restricted(self):
        r = ask(self.idx(principal='admin-a'), 'Falcon Robotics inspection')
        self.assertEqual(r['status'], 'answered')
        self.assertEqual(r['citations'][0]['source'], 'restricted.pdf')

    def test_wrong_tenant_even_same_principal(self):
        r = ask(self.idx(tenant='demo-a'), 'Exclusive tenant B quotation satellite equipment')
        self.assertEqual(r['status'], 'insufficient_evidence')

    def test_citation_verbatim_and_source(self):
        idx = self.idx()
        r = ask(idx, 'Example English Supplies grand total 1050')
        source = next(p for p in idx.passages if p.passage_id == r['citations'][0]['passage_id'])
        self.assertIn(r['answer'], source.text)
        self.assertEqual(r['citations'][0]['quote'], r['answer'])
        self.assertEqual(r['citations'][0]['version'], source.version)

    def test_empty_question_rejected(self):
        with self.assertRaises(ValueError):
            ask(self.idx(), ' ')

    def test_coverage_validation(self):
        for threshold in (0, 1.5, True):
            with self.subTest(threshold=threshold):
                with self.assertRaises(ValueError):
                    ask(self.idx(), 'supplier', min_coverage=threshold)

    def test_only_stopwords_abstains(self):
        self.assertEqual(ask(self.idx(), 'What is the?')['status'], 'insufficient_evidence')

    def test_repeated_answers_deterministic(self):
        self.assertEqual(ask(self.idx(), 'Example English Supplies grand total 1050'),
                         ask(self.idx(), 'Example English Supplies grand total 1050'))


if __name__ == '__main__':
    unittest.main()
