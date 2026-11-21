"""Disposable PostgreSQL and temporary-source integration only."""
import json
import os
import hashlib
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

try:
    import psycopg
except ImportError:
    psycopg = None

from nawa_demo.authorization import Principal
from nawa_demo.identity_repository import membership_by_key
from nawa_demo.intake_postgres import PostgresIntakeJobs
from nawa_demo.intake_scoped import ScopedIntake
from nawa_demo.intake_store import LocalIntakeStore
from nawa_demo.intake_worker import _extract_pdf, process_one
from phase13_database_snapshot import load_snapshot
from hybrid_review import retrieve_for_review
from resolve_review_evidence import resolve_for_review
from compare_retrieval import embed

DSN = os.environ.get('NAWA_PHASE14_TEST_DSN')
ENABLED = os.environ.get('NAWA_PHASE14_DISPOSABLE') == '1'
DEMO = Path(__import__('nawa_demo').__file__).resolve().parent.parent


@unittest.skipUnless(DSN and ENABLED and psycopg, 'disposable Phase 14 PostgreSQL not configured')
class Phase13DatabaseSnapshotTests(unittest.TestCase):
    def setUp(self):
        config = psycopg.conninfo.conninfo_to_dict(DSN)
        if (config.get('host') != '127.0.0.1' or 'hostaddr' in config
                or not config.get('dbname', '').startswith('nawa_phase13_qa_')):
            raise ValueError('Disposable localhost test database required')
        self.tenant = 'qa-' + uuid4().hex
        self.other = 'qa-' + uuid4().hex
        self.issuer, self.subject = 'https://synthetic.invalid', uuid4().hex
        key = json.dumps([self.issuer,self.subject], separators=(',', ':'))
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_tenants (id) VALUES (%s),(%s)', (self.tenant,self.other))
            conn.execute('INSERT INTO nawa_identities (issuer,subject) VALUES (%s,%s)', (self.issuer,self.subject))
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (self.issuer,self.subject,self.tenant,'analyst'))
        with self.connect() as conn:
            self.principal = membership_by_key(conn, identity_key=key, tenant=self.tenant)
        if self.principal is None:
            raise AssertionError('Synthetic analyst membership unavailable')
        self.temp = TemporaryDirectory(prefix='nawa14-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.store = LocalIntakeStore(self.temp.name)
        self.intake = ScopedIntake(self.connect,self.store)
        self.jobs = PostgresIntakeJobs(self.connect)

    def add_pdf(self, sample, family, version, *, complete):
        content = (DEMO / 'samples' / 'pdf' / sample).read_bytes()
        document, created = self.intake.submit(self.principal, sample, content)
        self.assertTrue(created)
        self.jobs.link_version(self.tenant,document,family,expected_previous=version-1)
        with self.connect() as conn:
            conn.execute("INSERT INTO nawa_phase14_qa_lifecycle (tenant_id,document_id,state) VALUES (%s,%s,'active')",
                         (self.tenant,document))
        if complete:
            with TemporaryDirectory(prefix='nawa14-extract-') as temp:
                result = _extract_pdf(content,temp)
            job, token = self.jobs.claim(self.tenant)
            self.assertEqual(self.jobs.finish(self.tenant,job,token,success=True,result=result), 'completed')
        return document

    def test_real_membership_source_and_version_recheck(self):
        family = uuid4().hex
        old = self.add_pdf('english_supplier.pdf',family,1,complete=True)
        first = load_snapshot(self.connect,self.store,self.principal)
        self.assertTrue(first['passages'])
        self.assertTrue(all(p.passage_id.startswith(old+':') and p.page > 0 for p in first['passages']))
        newer = self.add_pdf('incorrect_vat.pdf',family,2,complete=False)
        self.assertEqual(load_snapshot(self.connect,self.store,self.principal)['passages'], [])
        with self.connect() as conn:
            conn.execute("UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s",
                         (self.issuer,self.subject,self.tenant))
        with self.assertRaises(PermissionError):
            load_snapshot(self.connect,self.store,self.principal)
        self.assertTrue(newer)

    def test_mixed_arabic_pdf_is_processed_and_labeled_by_representation(self):
        family=uuid4().hex
        document=self.add_pdf('arabic_text_supplier.pdf',family,1,complete=False)
        self.assertEqual(process_one(self.jobs,self.store,self.tenant),'completed')
        snapshot=load_snapshot(self.connect,self.store,self.principal)
        self.assertTrue(snapshot['passages'])
        self.assertTrue(all(p.passage_id.startswith(document+':') for p in snapshot['passages']))
        representations={p.passage_id:snapshot['provenance'][p.passage_id]['text_representation']
                         for p in snapshot['passages']}
        self.assertEqual(representations[document+':supplier'],'normalized')
        review=retrieve_for_review('supplier',tenant=self.tenant,principal=self.principal.subject,
                                   load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal),
                                   embed_fn=lambda texts:[[1.0] for _ in texts],limit=10)
        resolved=resolve_for_review(review,tenant=self.tenant,principal=self.principal.subject,
                                    load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal))
        self.assertEqual(resolved['status'],'requires_human_review')
        supplier=next(e for e in resolved['evidence'] if e['citation']['passage_id']==document+':supplier')
        self.assertIn('untrusted_normalized_text',supplier)
        self.assertNotIn('untrusted_source_text',supplier)

    def test_opt_in_modified_arabic_pdf_through_disposable_database(self):
        content=(DEMO/'samples'/'pdf'/'arabic_text_supplier.pdf').read_bytes()+b'\n'
        document,created=self.intake.submit(self.principal,'synthetic-arabic.pdf',content)
        self.assertTrue(created)
        self.jobs.link_version(self.tenant,document,uuid4().hex,expected_previous=0)
        with self.connect() as conn:
            conn.execute("INSERT INTO nawa_phase14_qa_lifecycle (tenant_id,document_id,state) VALUES (%s,%s,'active')",
                         (self.tenant,document))
        with patch.dict(os.environ,{'NAWA_INTAKE':'synthetic','NAWA_EXPERIMENTAL_ARABIC_PDF':'1'}):
            self.assertEqual(process_one(self.jobs,self.store,self.tenant),'completed')
            snapshot=load_snapshot(self.connect,self.store,self.principal)
            self.assertTrue(snapshot['passages'])
            self.assertEqual(snapshot['provenance'][document+':supplier']['text_representation'],'normalized')

    @unittest.skipUnless(os.environ.get('NAWA_PHASE14_TEST_ARABIC_PDF') == '1'
                         and os.environ.get('NAWA_PHASE14_TEST_OLLAMA') == '1',
                         'local synthetic Arabic PDF and embedding model not enabled')
    def test_local_arabic_label_pdf_end_to_end_for_human_review(self):
        fixture=Path(os.environ['NAWA_PHASE14_ARABIC_FIXTURE'])
        content=fixture.read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(),
                         'd1176daade474cb5189860448ae9d463660857371598654734a30342ef2833b5')
        document,created=self.intake.submit(self.principal,'synthetic-arabic-labels.pdf',content)
        self.assertTrue(created)
        self.jobs.link_version(self.tenant,document,uuid4().hex,expected_previous=0)
        with self.connect() as conn:
            conn.execute("INSERT INTO nawa_phase14_qa_lifecycle (tenant_id,document_id,state) VALUES (%s,%s,'active')",
                         (self.tenant,document))
        with patch.dict(os.environ,{'NAWA_INTAKE':'synthetic','NAWA_EXPERIMENTAL_ARABIC_PDF':'1'}):
            self.assertEqual(process_one(self.jobs,self.store,self.tenant),'completed')
            def snapshot():
                return load_snapshot(self.connect,self.store,self.principal)
            review=retrieve_for_review('كم يبلغ الإجمالي شامل الضريبة؟',
                       tenant=self.tenant,principal=self.principal.subject,
                       load_snapshot=snapshot,embed_fn=embed,limit=10)
            self.assertEqual(review['status'],'requires_human_review')
            resolved=resolve_for_review(review,tenant=self.tenant,principal=self.principal.subject,
                                        load_snapshot=snapshot)
            self.assertEqual(resolved['status'],'requires_human_review')
            total=next(e for e in resolved['evidence'] if e['citation']['passage_id']==document+':total')
            self.assertEqual(total['citation']['page'],1)
            self.assertEqual(total['untrusted_normalized_text'],'الإجمالي شامل الضريبة: ٩٤٥٫٠٠')

    def test_tampered_source_and_cross_tenant_membership_are_denied(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        (Path(self.temp.name) / document).write_bytes(b'tampered synthetic file')
        with self.assertRaises(ValueError):
            load_snapshot(self.connect,self.store,self.principal)
        cross_tenant = Principal(self.principal.subject,self.other,'analyst')
        with self.assertRaises(PermissionError):
            load_snapshot(self.connect,self.store,cross_tenant)

    def test_membership_revocation_between_retrieval_snapshots(self):
        self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        calls = 0
        def snapshot():
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.connect() as conn:
                    conn.execute("UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s",
                                 (self.issuer,self.subject,self.tenant))
            return load_snapshot(self.connect,self.store,self.principal)
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                                     load_snapshot=snapshot,embed_fn=lambda texts:[[1.0] for _ in texts])
        self.assertEqual(result, {'status':'unavailable'})

    def test_citations_and_source_reads_are_tenant_scoped(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        with self.connect() as conn:
            conn.execute('INSERT INTO nawa_memberships (issuer,subject,tenant_id,role) VALUES (%s,%s,%s,%s)',
                         (self.issuer,self.subject,self.other,'analyst'))
        with self.connect() as conn:
            other_principal = membership_by_key(conn, identity_key=self.principal.subject, tenant=self.other)
        if other_principal is None:
            raise AssertionError('Synthetic other-tenant membership unavailable')
        other_intake = ScopedIntake(self.connect,self.store)
        other_content = (DEMO/'samples'/'pdf'/'incorrect_vat.pdf').read_bytes()
        other_document, created = other_intake.submit(other_principal,'incorrect_vat.pdf',other_content)
        self.assertTrue(created)
        self.jobs.link_version(self.other,other_document,uuid4().hex,expected_previous=0)
        with self.connect() as conn:
            conn.execute("INSERT INTO nawa_phase14_qa_lifecycle (tenant_id,document_id,state) VALUES (%s,%s,'active')",
                         (self.other,other_document))
        with TemporaryDirectory(prefix='nawa14-extract-') as temp:
            other_result = _extract_pdf(other_content,temp)
        job,token = self.jobs.claim(self.other)
        self.jobs.finish(self.other,job,token,success=True,result=other_result)

        reads = []
        class ObservedStore:
            def read_verified(inner, identifier, digest):
                reads.append(identifier)
                return self.store.read_verified(identifier,digest)
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                    load_snapshot=lambda:load_snapshot(self.connect,ObservedStore(),self.principal),
                    embed_fn=lambda texts:[[1.0] for _ in texts])
        self.assertEqual(result['status'], 'requires_human_review')
        self.assertTrue(result['evidence'])
        self.assertEqual(set(reads), {document})
        expected_hash = hashlib.sha256((DEMO/'samples'/'pdf'/'english_supplier.pdf').read_bytes()).hexdigest()
        for citation in result['evidence']:
            self.assertEqual(citation['source_document_id'], document)
            self.assertEqual(citation['source_sha256'], expected_hash)
            self.assertGreaterEqual(citation['page'], 1)
            self.assertGreaterEqual(citation['page_line'], 1)

    def test_withdrawn_and_deleted_latest_never_reveal_older_version(self):
        family = uuid4().hex
        self.add_pdf('english_supplier.pdf',family,1,complete=True)
        latest = self.add_pdf('incorrect_vat.pdf',family,2,complete=False)
        for state in ('withdrawn', 'deleted'):
            with self.connect() as conn:
                conn.execute('UPDATE nawa_phase14_qa_lifecycle SET state=%s WHERE tenant_id=%s AND document_id=%s',
                             (state,self.tenant,latest))
            self.assertEqual(load_snapshot(self.connect,self.store,self.principal)['passages'], [])
        with self.connect() as conn:
            conn.execute('DELETE FROM nawa_phase14_qa_lifecycle WHERE tenant_id=%s AND document_id=%s',
                         (self.tenant,latest))
        with self.assertRaises(ValueError):
            load_snapshot(self.connect,self.store,self.principal)

    def test_document_withdrawal_between_retrieval_snapshots(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        calls = 0
        def snapshot():
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.connect() as conn:
                    conn.execute("UPDATE nawa_phase14_qa_lifecycle SET state='withdrawn' WHERE tenant_id=%s AND document_id=%s",
                                 (self.tenant,document))
            return load_snapshot(self.connect,self.store,self.principal)
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                                     load_snapshot=snapshot,embed_fn=lambda texts:[[1.0] for _ in texts])
        self.assertEqual(result, {'status':'unavailable'})

    def test_source_tampering_during_embedding_blocks_result(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        def embed(texts):
            (Path(self.temp.name) / document).write_bytes(b'tampered synthetic file')
            return [[1.0] for _ in texts]
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                    load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal),
                    embed_fn=embed)
        self.assertEqual(result, {'status':'unavailable'})

    def test_resolving_citations_rechecks_source_and_membership(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        def snapshot():
            return load_snapshot(self.connect,self.store,self.principal)
        review = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                                     load_snapshot=snapshot,embed_fn=lambda texts:[[1.0] for _ in texts])
        resolved = resolve_for_review(review,tenant=self.tenant,principal=self.principal.subject,
                                      load_snapshot=snapshot)
        self.assertEqual(resolved['status'], 'requires_human_review')
        self.assertTrue(all(item['untrusted_source_text'] for item in resolved['evidence']))
        (Path(self.temp.name) / document).write_bytes(b'tampered synthetic file')
        self.assertEqual(resolve_for_review(review,tenant=self.tenant,principal=self.principal.subject,
                                            load_snapshot=snapshot), {'status':'unavailable'})
        with self.connect() as conn:
            conn.execute("UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s",
                         (self.issuer,self.subject,self.tenant))
        self.assertEqual(resolve_for_review(review,tenant=self.tenant,principal=self.principal.subject,
                                            load_snapshot=snapshot), {'status':'unavailable'})

    def test_new_pending_version_during_embedding_blocks_older_result(self):
        family = uuid4().hex
        self.add_pdf('english_supplier.pdf',family,1,complete=True)
        def embed(texts):
            self.add_pdf('incorrect_vat.pdf',family,2,complete=False)
            return [[1.0] for _ in texts]
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                    load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal),
                    embed_fn=embed)
        self.assertEqual(result, {'status':'unavailable'})

    def test_missing_lifecycle_during_embedding_blocks_result(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        def embed(texts):
            with self.connect() as conn:
                conn.execute('DELETE FROM nawa_phase14_qa_lifecycle WHERE tenant_id=%s AND document_id=%s',
                             (self.tenant,document))
            return [[1.0] for _ in texts]
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                    load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal),
                    embed_fn=embed)
        self.assertEqual(result, {'status':'unavailable'})

    def test_modified_extracted_line_is_not_accepted_as_source_evidence(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        self.assertTrue(load_snapshot(self.connect,self.store,self.principal)['passages'])
        with self.connect() as conn:
            conn.execute("""UPDATE nawa_intake_jobs SET result=jsonb_set(
                         result,'{evidence,total,source_text}',to_jsonb(%s::text))
                         WHERE tenant_id=%s AND document_id=%s""",
                         ('Synthetic false line',self.tenant,document))
        with self.assertRaises(ValueError):
            load_snapshot(self.connect,self.store,self.principal)

    @unittest.skipUnless(os.environ.get('NAWA_PHASE14_TEST_OLLAMA') == '1',
                         'local synthetic embedding model not enabled')
    def test_local_model_with_disposable_database_and_pdf_evidence(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        def snapshot():
            return load_snapshot(self.connect,self.store,self.principal)
        review = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                                     load_snapshot=snapshot,embed_fn=embed)
        self.assertEqual(review['status'],'requires_human_review')
        self.assertTrue(review['evidence'])
        self.assertTrue(all(item['source_document_id'] == document for item in review['evidence']))
        resolved = resolve_for_review(review,tenant=self.tenant,principal=self.principal.subject,
                                      load_snapshot=snapshot)
        self.assertEqual(resolved['status'],'requires_human_review')
        self.assertTrue(all(item['untrusted_source_text'] for item in resolved['evidence']))

    @unittest.skipUnless(os.environ.get('NAWA_PHASE14_TEST_OLLAMA') == '1',
                         'local synthetic embedding model not enabled')
    def test_local_model_membership_revocation_during_embedding(self):
        self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        def embed_then_revoke(texts):
            vectors = embed(texts)
            with self.connect() as conn:
                conn.execute("UPDATE nawa_memberships SET active=false WHERE issuer=%s AND subject=%s AND tenant_id=%s",
                             (self.issuer,self.subject,self.tenant))
            return vectors
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                    load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal),
                    embed_fn=embed_then_revoke)
        self.assertEqual(result, {'status':'unavailable'})

    @unittest.skipUnless(os.environ.get('NAWA_PHASE14_TEST_OLLAMA') == '1',
                         'local synthetic embedding model not enabled')
    def test_local_model_document_withdrawal_during_embedding(self):
        document = self.add_pdf('english_supplier.pdf',uuid4().hex,1,complete=True)
        def embed_then_withdraw(texts):
            vectors = embed(texts)
            with self.connect() as conn:
                conn.execute("UPDATE nawa_phase14_qa_lifecycle SET state='withdrawn' WHERE tenant_id=%s AND document_id=%s",
                             (self.tenant,document))
            return vectors
        result = retrieve_for_review('total',tenant=self.tenant,principal=self.principal.subject,
                    load_snapshot=lambda:load_snapshot(self.connect,self.store,self.principal),
                    embed_fn=embed_then_withdraw)
        self.assertEqual(result, {'status':'unavailable'})


if __name__ == '__main__':
    unittest.main()


