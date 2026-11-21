"""Evaluation accounting only; the fixture is not independent validation."""
import json
from pathlib import Path
import unittest
from nawa_demo.synthetic_answer_evaluation import evaluate


class AnswerEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.positive = {'id':'p','language':'en','question':'VAT deadline',
            'source_text':'VAT deadline is 2026-12-01.','expected':'answered','answer_contains':'2026-12-01'}

    def test_reports_false_answers_and_false_abstentions(self):
        negative = {**self.positive, 'id':'n','source_text':'VAT deadline remains unknown.',
                    'expected':'insufficient_evidence','answer_contains':''}
        paraphrase = {**self.positive,'id':'q','source_text':'VAT due date is 2026-12-01.'}
        report = evaluate([self.positive, negative, paraphrase])
        self.assertEqual((report['case_count'], report['false_answer_count'], report['false_abstention_count']), (3, 1, 1))
        self.assertIn('not independently reviewed', report['scope'])

    def test_rejects_invalid_labels_and_duplicate_cases(self):
        for item in ({**self.positive,'answer_contains':'missing'},
                     {**self.positive,'expected':'insufficient_evidence'},
                     {**self.positive,'language':'unknown'},
                     {**self.positive,'expected':'unknown'}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                evaluate([item])
        with self.assertRaises(ValueError):
            evaluate([self.positive,self.positive])

    def test_published_fixture_runs(self):
        fixture = Path(__file__).resolve().parents[1] / 'samples/retrieval/synthetic_answer_cases.json'
        report = evaluate(json.loads(fixture.read_text(encoding='utf-8')))
        self.assertEqual(report['case_count'], 12)
        self.assertEqual({r['language'] for r in report['cases']}, {'en','ar'})


if __name__ == '__main__':
    unittest.main()
