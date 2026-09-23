from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili.db import Task, TaskStore
from yt2bili.desktop_settings import DesktopSettings
from yt2bili.paths import AppPaths
from yt2bili.translation import tasks
from yt2bili.translation.config import DEFAULTS
from yt2bili.translation.types import TranslationResult, TranslationError
from yt2bili.translation.deployment import safe_extract


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=TaskStore(self.root/'tasks.sqlite');self.addCleanup(self.store.close)
        self.task=Task('abcdefghijk','url','ready',title_zh='用户标题',desc_zh='用户简介')
        self.store.upsert(self.task)
        self.meta=SimpleNamespace(title='Source',description='Description',language='en',uploader='author',webpage_url='https://youtube.com')
        self.settings=SimpleNamespace(**DEFAULTS,title_limit=80,desc_limit=2000,translation_root=self.root/'translation')

    def test_failed_retranslation_preserves_old_pair(self):
        with patch.object(tasks,'translate_group',side_effect=TranslationError('FAIL','failure')),self.assertRaises(TranslationError):
            tasks.prepare(self.settings,self.store,self.task,self.meta,self.root,force=True)
        self.assertEqual(self.store.require(self.task.video_id).title_zh,'用户标题')
        self.assertEqual(self.store.require(self.task.video_id).desc_zh,'用户简介')

    def test_edit_preserved_and_files_recovered_without_call(self):
        self.store.save_translation(self.task,{'state':'edited','user_edited':True})
        with patch.object(tasks,'translate_group',side_effect=AssertionError('must not translate')):
            tasks.prepare(self.settings,self.store,self.task,self.meta,self.root)
        self.assertEqual((self.root/'title.txt').read_text(encoding='utf-8'),'用户标题')

    def test_new_pair_commits_with_provenance_and_single_footer(self):
        with patch.object(tasks,'translate_group',return_value=TranslationResult('新标题','新正文','local_llm',model_digest='digest')):
            tasks.prepare(self.settings,self.store,self.task,self.meta,self.root,force=True)
        self.assertEqual(self.store.require(self.task.video_id).title_zh,'新标题')
        self.assertEqual(self.task.desc_zh.count('原标题：'),1)
        record=self.store.translation(self.task.video_id)
        self.assertEqual(record['config_snapshot']['model_digest'],'digest')
        self.assertEqual(record['state'],'complete')

    def test_sql_failure_rolls_back_both_fields(self):
        self.store._conn.execute("CREATE TRIGGER deny_translation BEFORE INSERT ON task_translation BEGIN SELECT RAISE(ABORT,'test'); END")
        self.task.title_zh='new'
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_translation(self.task,{'state':'complete'})
        self.assertEqual(self.store.require(self.task.video_id).title_zh,'用户标题')
        self.assertIsNone(self.store.translation(self.task.video_id))

    def test_legacy_schema_migration_preserves_old_task_policy(self):
        self.store.save_job(self.task.video_id,{'settings':{'work_dir':str(self.root)},'mode':'preview'})
        self.store._conn.execute('DROP TABLE task_translation')
        self.store._conn.execute('DROP TABLE translation_attempts')
        self.store._conn.execute('PRAGMA user_version=1');self.store._conn.commit();self.store.close()
        self.store=TaskStore(self.root/'tasks.sqlite');self.addCleanup(self.store.close)
        record=self.store.translation(self.task.video_id)
        self.assertEqual(record['state'],'legacy_preserved')
        self.assertEqual(record['config_snapshot']['translation_primary'],'deepl')
        self.assertFalse(record['config_snapshot']['translation_fallback_enabled'])
        self.assertTrue((self.root/'tasks.sqlite.pre-translation.bak').is_file())
        self.assertEqual(self.store.get_job(self.task.video_id)['settings']['translation_primary'],'deepl')

    def test_vault_failure_does_not_prevent_local_settings_build(self):
        class Vault:
            def get_password(self,*args):raise RuntimeError('locked')
        config=DesktopSettings(AppPaths.default(str(self.root),str(self.root)),Vault())
        settings=config.build()
        self.assertEqual(settings.translation_primary,'local_llm')
        self.assertEqual(settings.deepl_auth_key,'')
        self.assertTrue(config.public()['vault_error'])

    def test_zip_path_traversal_and_size_rejected(self):
        archive=self.root/'bad.zip'
        for name in ('../escape','C:/escape'):
            with zipfile.ZipFile(archive,'w') as out:out.writestr(name,'x')
            with self.assertRaises(TranslationError):safe_extract(archive,self.root/'target')
        with zipfile.ZipFile(archive,'w') as out:out.writestr('ok','1234')
        with self.assertRaises(TranslationError):safe_extract(archive,self.root/'target',limit=3)
