"""Tests for evaluation accounting, not an accuracy certification."""
import json
from pathlib import Path
import unittest
from nawa_demo.retrieval import Passage, read_corpus
from nawa_demo.synthetic_retrieval_evaluation import evaluate


def fixture(pid, tenant, text, acl):
    return Passage.from_dict({'passage_id':pid,'document_id':pid,'version':'1','tenant_id':tenant,
        'page':1,'text':text,'allowed_principals':acl,'source':pid+'.txt','method':'synthetic'})


class SyntheticRetrievalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.docs = [fixture('own','a','vat invoice',['alice']), fixture('other','b','secret zebra',['bob'])]
        self.base = {'id':'one','language':'en','tenant_id':'a','principal_id':'alice',
                     'query':'vat','relevant_ids':['own'],'forbidden_ids':['other'],'expect_no_result':False}

    def test_accounts_for_answerable_and_no_result_cases(self):
        negative = {**self.base,'id':'two','query':'zebra','relevant_ids':[],'expect_no_result':True}
        false_positive = {**negative,'id':'three','query':'vat missing detail'}
        report = evaluate(self.docs, [self.base, negative, false_positive])
        self.assertEqual((report['answerable_count'], report['macro_recall_at_5']), (1, 1))
        self.assertEqual((report['expected_no_result_count'], report['no_result_count']), (2, 1))
        self.assertEqual((report['forbidden_hit_cases'], report['unauthorized_hit_cases']), (0, 0))
        self.assertEqual(report['cases'][2]['retrieved_ids'], ['own'])
        self.assertIn('not independently reviewed', report['scope'])

    def test_rejects_invalid_or_unauthorized_labels(self):
        for case in ({**self.base,'relevant_ids':['other']},
                     {**self.base,'forbidden_ids':['own']},
                     {**self.base,'relevant_ids':[]},
                     {**self.base,'expect_no_result':'yes'},
                     {**self.base,'relevant_ids':['missing']}):
            with self.subTest(case=case), self.assertRaises(ValueError):
                evaluate(self.docs, [case])
        with self.assertRaises(ValueError):
            evaluate(self.docs, [self.base, self.base])

    def test_counts_forbidden_hit_without_concealing_it(self):
        result = evaluate(self.docs, [{**self.base,'relevant_ids':[], 'forbidden_ids':['own'],
                                      'expect_no_result':True}])
        self.assertEqual(result['forbidden_hit_cases'], 1)
        self.assertEqual(result['no_result_count'], 0)

    def test_published_fixture_can_be_evaluated(self):
        base = Path(__file__).resolve().parents[1] / 'samples' / 'retrieval'
        report = evaluate(read_corpus(base / 'passages.json'),
                          json.loads((base / 'synthetic_lifecycle_evaluation.json').read_text(encoding='utf-8')))
        self.assertEqual((report['case_count'], report['answerable_count'], report['expected_no_result_count']), (9, 4, 5))
        self.assertEqual(report['unauthorized_hit_cases'], 0)


if __name__ == '__main__':
    unittest.main()
