"""Small hand-labeled answer-level fixture; not a production evaluation."""
import argparse
import json
from pathlib import Path

from .grounded_qa import ask
from .retrieval import BM25Index, Passage


def evaluate(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('Expected 1 to 100 synthetic cases')
    results, seen = [], set()
    keys = {'id','language','question','source_text','expected','answer_contains'}
    for case in cases:
        if (not isinstance(case, dict) or set(case) != keys
                or not all(isinstance(case[k], str) and case[k].strip() for k in ('id','question','source_text'))
                or case['language'] not in ('en','ar','mixed') or case['expected'] not in ('answered','insufficient_evidence')
                or not isinstance(case['answer_contains'], str) or case['id'] in seen):
            raise ValueError('Invalid synthetic answer case')
        seen.add(case['id'])
        if (case['expected'] == 'answered') != bool(case['answer_contains']):
            raise ValueError('Answerable cases require an expected excerpt')
        if case['expected'] == 'answered' and case['answer_contains'] not in case['source_text']:
            raise ValueError('Expected excerpt must occur in synthetic source')
        passage = Passage.from_dict({'passage_id':case['id'], 'document_id':case['id'], 'version':'1',
            'tenant_id':'synthetic', 'page':1, 'text':case['source_text'],
            'allowed_principals':['fixture'], 'source':'synthetic.txt', 'method':'synthetic'})
        result = ask(BM25Index([passage], tenant_id='synthetic', principal_id='fixture'), case['question'])
        actual = result['status']
        if actual == 'insufficient_evidence' and (result['answer'] is not None or result['citations']):
            raise ValueError('Abstention returned answer content')
        false_answer = case['expected'] == 'insufficient_evidence' and actual == 'answered'
        false_abstention = case['expected'] == 'answered' and actual == 'insufficient_evidence'
        wrong_excerpt = (case['expected'] == 'answered' and actual == 'answered'
                         and case['answer_contains'] not in result['answer'])
        results.append({'id':case['id'], 'language':case['language'], 'expected':case['expected'],
                        'actual':actual, 'false_answer':false_answer, 'false_abstention':false_abstention,
                        'wrong_excerpt':wrong_excerpt})
    return {'scope':'Synthetic hand-authored fixture; not independently reviewed; no production accuracy claim.',
            'case_count':len(results), 'false_answer_count':sum(x['false_answer'] for x in results),
            'false_abstention_count':sum(x['false_abstention'] for x in results),
            'wrong_excerpt_count':sum(x['wrong_excerpt'] for x in results), 'cases':results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    print(json.dumps(evaluate(cases), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
