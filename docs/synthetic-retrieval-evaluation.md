# Synthetic retrieval evaluation fixture

Run from the `demo` directory:

```powershell
python -m nawa_demo.synthetic_retrieval_evaluation --corpus samples/retrieval/passages.json --cases samples/retrieval/synthetic_lifecycle_evaluation.json
```

This nine-case fixture has four answerable questions and five cases labeled as expecting no search results. It includes English, Arabic, a mixed-language query, restricted content and a second-tenant positive control. It is a small hand-authored fixture, not independently reviewed or separated from development; do not use its numbers as real-world quality or isolation claims.

One local run returned the labeled passage within five results for all four answerable cases, returned no passages for three of the five no-result cases, and returned no IDs labeled forbidden or unauthorized. The English and Arabic missing-payment-date questions returned VAT-related passages despite the fixture having no payment-date answer. A retrieval hit may provide context without answering the question: an empty search result is not the same as a generation model abstaining, and a nonempty result is not proof of an answer. The current fixture does not evaluate semantic support, source freshness, live authentication, production access controls, OCR, or generated answers. An independently reviewed bilingual holdout and explicit answer-level abstention tests remain open.

The separate offline extractive and mock-generation experiments default to requiring all non-stopword question tokens in the selected text. Six synthetic missing-payment queries, including English and Arabic partial-overlap examples, abstain under that default; the mock provider is not called for two of them. The selected text is also checked for a small fixed set of explicit absence phrases such as "not specified" and "غير محدد". English and Arabic fixture examples abstain and two present-date examples remain answerable. This check is conservative, not a general negation or semantic verifier: another fixture saying the deadline remains "unknown" is still labeled answered. It can also reject relevant text containing an unrelated absence phrase. A lower explicit coverage threshold remains available and can admit unsupported answers, and strict matching can reject valid paraphrases. No real model, production data or portal answer flow was tested.
