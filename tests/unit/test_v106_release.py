from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from archive_scout.config import ProjectConfig
from archive_scout.constants import DEFAULT_IMAGE_EXTENSIONS, SCHEMA_VERSION, VERSION
from archive_scout.content import classify_text_candidate, decode_bytes, looks_textual_bytes
from archive_scout.database.connection import open_database
from archive_scout.database.repositories import get_or_create_keyword_set, start_scan_run, upsert_document, save_match
from archive_scout.downloads.downloader import prepare_download_rows
from archive_scout.projects.compaction import compact_project_storage
from archive_scout.scanning.hitlist import search_with_hitlist
from archive_scout.scanning.jobs import ScanJob
from archive_scout.scanning.scoring import analyze_content, prepare_analysis_fields
from archive_scout.storage import capture_path, media_path, url_filename
from archive_scout.ui.dashboard import read_dashboard_counts
from archive_scout.utils import normalize_search


class V106ReleaseTests(unittest.TestCase):
    def test_release_identity(self):
        self.assertEqual(VERSION, '1.0.7.1')
        self.assertEqual(SCHEMA_VERSION, 8)

    def test_windows_file_version_metadata_matches_release(self):
        metadata = Path('packaging/windows/version_info.txt').read_text(encoding='utf-8')
        self.assertIn('filevers=(1, 0, 7, 1)', metadata)
        self.assertIn('prodvers=(1, 0, 7, 1)', metadata)
        self.assertIn("StringStruct('FileVersion', '1.0.7.1')", metadata)
        self.assertIn("StringStruct('ProductVersion', '1.0.7.1')", metadata)

    def test_url_filename_preserves_query_and_is_portable(self):
        a = url_filename('http://example.com/show.php?id=1&x=a')
        b = url_filename('http://example.com/show.php?id=2&x=a')
        self.assertNotEqual(a, b)
        self.assertIn('example.com', a)
        self.assertNotIn('/', a)
        self.assertNotIn('?', a)
        self.assertEqual(a, url_filename('http://example.com/show.php?id=1&x=a'))
        huge = url_filename('http://example.com/' + ('ユ' * 500))
        self.assertLessEqual(len(huge.encode('utf-8')), 235)
        self.assertIn('~', huge)

    def test_capture_and_media_names_use_same_url_policy(self):
        root = Path('/project')
        url = 'http://example.com/a/b.jpg?q=1'
        self.assertEqual(capture_path(root, '20010102030405', url).name, url_filename(url) + '.txt')
        self.assertEqual(media_path(root, 'image', url).name, url_filename(url))
        self.assertEqual(media_path(root, 'image', url).parent, root / 'media' / 'images')

    def test_conflicting_or_weak_metadata_is_not_silently_skipped(self):
        self.assertEqual(classify_text_candidate('http://x/a.jpg', 'text/html'), 'ambiguous')
        self.assertEqual(classify_text_candidate('http://x/file', 'application/octet-stream'), 'ambiguous')
        self.assertEqual(classify_text_candidate('http://x/a.jpg', 'image/jpeg'), 'binary')
        self.assertEqual(classify_text_candidate('http://x/a.svg', 'image/svg+xml'), 'text')
        self.assertIn('.svg', DEFAULT_IMAGE_EXTENSIONS)

    def test_utf16_is_text_and_decodes(self):
        data = '<html><body>needle</body></html>'.encode('utf-16')
        self.assertTrue(looks_textual_bytes(data, 'application/octet-stream'))
        self.assertIn('needle', decode_bytes(data, 'application/octet-stream'))

    def test_full_source_and_markup_split_are_searchable(self):
        marker = 'needle-after-half-million'
        raw = ('x' * 500_100) + marker + '<p>split <b>phrase</b></p>'
        fields, normalized = prepare_analysis_fields('http://x/', '', '', raw, [])
        self.assertIn(marker, fields['source'])
        self.assertIn('split phrase', normalized['compact'])

    def test_js_escape_normalization(self):
        self.assertIn('needle', normalize_search(r'\u006e\u0065\u0065\u0064\u006c\u0065'))

    def test_aho_literal_path_counts_nonoverlapping_like_regex(self):
        job = ScanJob.create(1, 'test', ['ana'])
        analysis = analyze_content('http://x/', '', 'banana', '', [], job.patterns, job.prefilter)
        # body contributes one non-overlapping occurrence, matching re.finditer('ana').
        self.assertEqual(analysis['hits'].get('ana'), 1)

    def test_known_binary_skip_is_auditable_but_not_open_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = open_database(root)
            now = '2026-01-01T00:00:00'
            db.execute("INSERT INTO captures(original_url,timestamp,query_signature,mimetype,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       ('http://x/a.jpg','20010101000000','sig','image/jpeg','pending',now,now))
            db.commit()
            config = ProjectConfig(output_dir=root, targets=['x/*'], keywords=['needle']).normalized()
            # Match the stored row to the active signature for queue preparation.
            from archive_scout.cdx.parameters import cdx_query_signature
            db.execute('UPDATE captures SET query_signature=?', (cdx_query_signature(config),))
            db.commit()
            total, rows = prepare_download_rows(db, config, ScanJob.create(1,'x',['needle']).patterns)
            self.assertEqual(total, 0)
            self.assertEqual(list(rows), [])
            row = db.execute('SELECT state,skip_reason FROM captures').fetchone()
            self.assertEqual((row['state'], row['skip_reason']), ('skipped','known_non_text'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM errors').fetchone()[0], 0)
            db.close()

    def test_keyword_url_skip_requeues_when_scope_becomes_all_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = open_database(root)
            now = '2026-01-01T00:00:00'
            config_fast = ProjectConfig(output_dir=root, targets=['x/*'], keywords=['needle'], download_scope='keyword_urls').normalized()
            from archive_scout.cdx.parameters import cdx_query_signature
            sig = cdx_query_signature(config_fast)
            db.execute("INSERT INTO captures(original_url,timestamp,query_signature,mimetype,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       ('http://x/page.html','20010101000000',sig,'text/html','pending',now,now))
            db.commit()
            prepare_download_rows(db, config_fast, ScanJob.create(1,'x',['needle']).patterns)
            self.assertEqual(db.execute('SELECT skip_reason FROM captures').fetchone()[0], 'url_keyword_filter')
            config_all = ProjectConfig(output_dir=root, targets=['x/*'], keywords=['needle'], download_scope='all_text').normalized()
            # Signature does not include the local download scope; the same indexed queue is re-evaluated.
            total, _ = prepare_download_rows(db, config_all, ScanJob.create(1,'x',['needle']).patterns)
            self.assertEqual(total, 1)
            self.assertEqual(db.execute('SELECT state FROM captures').fetchone()[0], 'pending')
            db.close()

    def test_hitlist_search_reports_url_and_local_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = open_database(root)
            now='2026-01-01T00:00:00'
            db.execute("INSERT INTO captures(original_url,timestamp,query_signature,mimetype,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       ('http://x/urlneedle','20010101000000','sig','text/html','downloaded',now,now))
            c1 = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            p1 = root/'captures'/'a.html'; p1.parent.mkdir(parents=True,exist_ok=True); p1.write_bytes(b'<html>bodyneedle</html>')
            db.execute('UPDATE captures SET local_path=? WHERE id=?',(str(p1),c1))
            db.commit()
            result = search_with_hitlist(root, db, ['urlneedle','bodyneedle'], threading.Event())
            self.assertEqual(result['indexed_checked'], 1)
            self.assertEqual(result['local_checked'], 1)
            hits = {r['keyword'] for r in db.execute('SELECT keyword FROM quick_search_hits')}
            self.assertEqual(hits, {'urlneedle','bodyneedle'})
            self.assertTrue(Path(result['csv']).is_file())
            db.close()

    def test_dashboard_separates_skips_recovery_and_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); db=open_database(root); now='2026-01-01T00:00:00'
            for state, reason in [('skipped','known_non_text'),('skipped','url_keyword_filter'),('pending',None),('downloaded_unscanned',None)]:
                db.execute("INSERT INTO captures(original_url,timestamp,query_signature,state,skip_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (f'http://x/{state}{reason}','20010101000000','sig',state,reason,now,now))
            db.execute("INSERT INTO errors(operation,category,message,retryable,resolved,ignored,first_seen,last_seen) VALUES('download','x','x',1,0,0,?,?)",(now,now))
            db.execute("INSERT INTO recovery_events(stage,category,message,created_at) VALUES('index','retry','ok',?)",(now,))
            db.commit()
            db.commit()
            stats=read_dashboard_counts(root / 'archive_scout.sqlite3')
            self.assertEqual(stats['errors'],1); self.assertEqual(stats['recovery_events'],1)
            self.assertEqual(stats['skipped_non_text'],1); self.assertEqual(stats['skipped_url_filter'],1)
            self.assertEqual(stats['pending'],1); self.assertEqual(stats['downloaded_unscanned'],1)
            db.close()

    def test_compaction_reclaims_legacy_body_and_keyword_hits_without_deleting_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); db=open_database(root); now='2026-01-01T00:00:00'
            db.execute("INSERT INTO captures(original_url,timestamp,query_signature,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",('http://x/a','20010101000000','sig','downloaded',now,now))
            cid=int(db.execute('SELECT last_insert_rowid()').fetchone()[0])
            path=root/'captures'/'a'; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'<html>needle</html>')
            did=upsert_document(db,cid,path,'title','needle',[],hashlib.sha256(path.read_bytes()).hexdigest(),'n',path.stat().st_size)
            # Simulate a v1.0.5 body duplicate and duplicate keyword_hits rows.
            db.execute("UPDATE documents SET body_text='needle' WHERE id=?",(did,))
            ks=get_or_create_keyword_set(db,'k',['needle']); sr=start_scan_run(db,ks,'s',1,'test')
            mid=save_match(db,sr,did,{'score':1,'hits':{'needle':1},'hit_fields':{'needle':['body']}})
            db.execute("INSERT OR REPLACE INTO keyword_hits(match_id,label,count,fields_json) VALUES(?,?,?,?)",(mid,'needle',1,'[\"body\"]'))
            db.commit()
            result=compact_project_storage(root,db,threading.Event())
            self.assertTrue(path.is_file())
            self.assertEqual(db.execute('SELECT body_text FROM documents WHERE id=?',(did,)).fetchone()[0],'')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM keyword_hits').fetchone()[0],0)
            self.assertGreaterEqual(result['documents_compacted'],1)
            db.close()

    def test_macos_build_keeps_outer_brand_but_renames_inner_executable(self):
        script=Path('scripts/build_macos.sh').read_text(encoding='utf-8')
        verify=Path('scripts/verify_macos_bundle.py').read_text(encoding='utf-8')
        self.assertIn('Archive Scout.app',script)
        self.assertIn('Wayback Machine Downloader',script)
        self.assertIn('--expected-executable',verify)


if __name__ == '__main__':
    unittest.main()
