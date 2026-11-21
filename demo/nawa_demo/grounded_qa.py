"""Phase 5: deterministic, extractive, citation-linked answers over Phase 4 BM25.

No LLM, no claim of understanding. Only verbatim source sentences are returned.
Trusted caller must supply verified tenant/principal; CLI is a local simulation.
"""
import re
import hashlib
from .retrieval import BM25Index, tokenize

STOP = set(tokenize('''what which who when where why how is are was were the a an of for in on at to from do does did
please tell me show give find document documents quotation quote supplier supplies price total value amount
كم ما هي هو في من عن على إلى لدى لي اذكر اعرض ابحث الوثيقة المستند مستند عرض عروض سعر السعر'''))

# Conservative fixture-oriented patterns, not a general negation detector.
ABSENCE_PHRASES = tuple(tuple(tokenize(phrase)) for phrase in (
    'not specified', 'not provided', 'not available', 'not stated', 'not disclosed',
    'غير محدد', 'غير محددة', 'غير مذكور', 'غير مذكورة', 'غير متاح', 'غير متاحة',
    'لم يحدد', 'لم تحدد', 'لم يتم تحديد', 'لا يوجد', 'لا تتوفر'))


def _explicit_absence(text: str) -> bool:
    tokens = tokenize(text)
    for phrase in ABSENCE_PHRASES:
        if any(tuple(tokens[i:i + len(phrase)]) == phrase for i in range(len(tokens) - len(phrase) + 1)):
            return True
    # E.g. "no payment date specified". This bounded rule can still reject
    # relevant text and does not understand arbitrary negation or context.
    return any(token == 'no' and any(word in ('specified', 'provided', 'available', 'stated')
                                     for word in tokens[i+1:i+5]) for i, token in enumerate(tokens))


def _sentences(text: str) -> list[str]:
    # Retain currency decimals / Arabic fractional numbers rather than cutting within them.
    return [part.strip() for part in re.split(r'(?<=[.!?؟])\s+', text.strip()) if part.strip()]


def format_review_handoff(hits: list[dict]) -> dict:
    """Format caller-supplied results as untrusted previews, never answers.

    This formatter does not establish access, source integrity or freshness.
    Callers must obtain and check results through an appropriate boundary.
    """
    if not isinstance(hits, list) or len(hits) > 5:
        raise ValueError('At most five search results are supported')
    evidence = []
    for hit in hits:
        fields = ('passage_id','document_id','version','source','method','text')
        if (not isinstance(hit, dict) or any(not isinstance(hit.get(k), str) or not hit[k] for k in fields)
                or type(hit.get('page')) is not int or hit['page'] < 1):
            raise ValueError('Invalid search result')
        text = hit['text']
        evidence.append({'passage_id':hit['passage_id'], 'document_id':hit['document_id'],
            'version':hit['version'], 'page':hit['page'], 'source':hit['source'], 'method':hit['method'],
            'untrusted_source_text':text[:4000], 'preview_truncated':len(text) > 4000,
            'passage_text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()})
    return {'status':'requires_human_review' if evidence else 'insufficient_evidence',
            'answer':None, 'citations':[], 'evidence':evidence,
            'note':'Untrusted source previews only; confirm current access and original-source integrity.'}


def prepare_human_review(index: BM25Index, question: str, *, top_k: int = 5) -> dict:
    """Return untrusted previews from a static synthetic index, never answers.

    The caller must establish identity, current access and source integrity.
    """
    if not isinstance(index, BM25Index):
        raise TypeError('Expected a permission-filtered BM25Index')
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError('A nonempty question of at most 500 characters is required')
    if type(top_k) is not int or not 1 <= top_k <= 5:
        raise ValueError('top_k must be from 1 to 5')
    return format_review_handoff(index.search(question, k=top_k))


def ask(index: BM25Index, question: str, *, top_k: int = 5, min_coverage: float = 1.0) -> dict:
    """Return a verbatim sentence and citation, or abstain for insufficient lexical support.

    The default requires every non-stopword query token to appear in the
    selected text. This is lexical matching, not semantic support. Lowering
    min_coverage makes the experiment less conservative.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError('Nonempty question required')
    if not isinstance(min_coverage, (float, int)) or isinstance(min_coverage, bool) or not 0 < min_coverage <= 1:
        raise ValueError('min_coverage must be greater than 0 and at most 1')
    if not isinstance(index, BM25Index):
        raise TypeError('Expected a permission-filtered BM25Index')
    terms = set(tokenize(question)) - STOP
    result = {'question': question, 'tenant_id': index.tenant_id,
              'principal_id': index.principal_id, 'status': 'insufficient_evidence',
              'answer': None, 'citations': [], 'lexical_coverage': 0.0,
              'note': 'Extractive evidence only; lexical coverage is not model confidence'}
    if not terms:
        return result
    matches = index.search(question, k=top_k)
    best = None
    for hit in matches:
        for sentence in [*_sentences(hit['text']), hit['text']]:
            overlap = terms.intersection(tokenize(sentence))
            coverage = len(overlap) / len(terms)
            # Deterministic ordering: support first, BM25 rank second, sentence order third.
            candidate = (coverage, len(overlap))
            if best is None or candidate > best[0]:
                best = (candidate, hit, sentence)
    if best is None:
        return result
    (coverage, matched), hit, sentence = best
    result['lexical_coverage'] = round(coverage, 4)
    # One matching token for a multi-term question is not enough evidence.
    if coverage < min_coverage or (len(terms) > 1 and matched < 2) or _explicit_absence(sentence):
        return result
    result.update({'status': 'answered', 'answer': sentence,
                   'citations': [{'passage_id': hit['passage_id'],
                                  'document_id': hit['document_id'],
                                  'version': hit['version'], 'source': hit['source'],
                                  'page': hit['page'], 'method': hit['method'],
                                  'quote': sentence}]})
    return result
