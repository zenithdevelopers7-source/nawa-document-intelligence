"""Evaluate a small synthetic BM25 fixture, not real-world retrieval quality."""
import argparse
import json
from pathlib import Path

from .retrieval import BM25Index, read_corpus


def evaluate(passages, cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('A nonempty synthetic evaluation set is required')
    catalog = {p.passage_id:p for p in passages}
    if len(catalog) != len(passages):
        raise ValueError('Duplicate passage IDs')
    results, used = [], set()
    for case in cases:
        required = {'id','language','tenant_id','principal_id','query','relevant_ids','forbidden_ids','expect_no_result'}
        if not isinstance(case, dict) or set(case) != required:
            raise ValueError('Invalid synthetic evaluation case')
        if not all(isinstance(case[k], str) and case[k].strip() for k in ('id','tenant_id','principal_id','query')):
            raise ValueError('Missing case identity or query')
        if case['id'] in used or case['language'] not in ('en','ar','mixed') or type(case['expect_no_result']) is not bool:
            raise ValueError('Duplicate case ID or invalid case metadata')
        used.add(case['id'])
        for key in ('relevant_ids','forbidden_ids'):
            ids = case[key]
            if (not isinstance(ids, list) or not all(isinstance(x,str) and x in catalog for x in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError('Invalid case passage IDs')
        relevant, forbidden = set(case['relevant_ids']), set(case['forbidden_ids'])
        if relevant & forbidden or bool(relevant) == case['expect_no_result']:
            raise ValueError('Inconsistent synthetic labels')
        index = BM25Index(passages, tenant_id=case['tenant_id'], principal_id=case['principal_id'])
        accessible = {p.passage_id for p in index.passages}
        if not relevant <= accessible:
            raise ValueError('Relevant passage is not authorized in the fixture')
        retrieved = [hit['passage_id'] for hit in index.search(case['query'], k=5)]
        results.append({'id':case['id'], 'language':case['language'], 'retrieved_ids':retrieved,
            'recall_at_5':len(relevant & set(retrieved))/len(relevant) if relevant else None,
            'expected_no_result':case['expect_no_result'],
            'no_result':not retrieved, 'forbidden_returned':bool(forbidden & set(retrieved)),
            'unauthorized_returned':bool(set(retrieved) - accessible)})
    answerable = [r for r in results if r['recall_at_5'] is not None]
    no_answer = [r for r in results if r['expected_no_result']]
    return {'scope':'Small synthetic fixture; labels are not independently reviewed; no production accuracy claim.',
        'case_count':len(results), 'answerable_count':len(answerable),
        'macro_recall_at_5':sum(r['recall_at_5'] for r in answerable)/len(answerable) if answerable else None,
        'expected_no_result_count':len(no_answer),
        'no_result_count':sum(r['no_result'] for r in no_answer),
        'forbidden_hit_cases':sum(r['forbidden_returned'] for r in results),
        'unauthorized_hit_cases':sum(r['unauthorized_returned'] for r in results), 'cases':results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(read_corpus(args.corpus), json.loads(args.cases.read_text(encoding='utf-8')))
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
