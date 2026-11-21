"""Run: python -m nawa_demo.qa_cli --tenant demo-a --principal reviewer-a --question '...'"""
import argparse
import json
from pathlib import Path
from .grounded_qa import ask, prepare_human_review
from .retrieval import BM25Index, read_corpus


def main():
    parser = argparse.ArgumentParser(description='NAWA synthetic extractive, cited Q&A (no LLM)')
    parser.add_argument('--corpus', type=Path, default=Path('samples/retrieval/passages.json'))
    parser.add_argument('--tenant', required=True, help='Trusted tenant ID (CLI simulation only)')
    parser.add_argument('--principal', required=True, help='Trusted principal ID (CLI simulation only)')
    parser.add_argument('--question', required=True)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--min-coverage', type=float, default=1.0,
                        help='Lexical threshold; lowering it can admit unsupported answers')
    parser.add_argument('--review-only', action='store_true',
                        help='Return untrusted source previews for human review, without an answer')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        index = BM25Index(read_corpus(args.corpus), tenant_id=args.tenant, principal_id=args.principal)
        if args.review_only and args.min_coverage != 1.0:
            raise ValueError('--min-coverage does not apply to --review-only')
        report = (prepare_human_review(index, args.question, top_k=args.top_k) if args.review_only
                  else ask(index, args.question, top_k=args.top_k, min_coverage=args.min_coverage))
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output is not None:
            args.output.write_text(rendered + '\n', encoding='utf-8')
        print(rendered)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
