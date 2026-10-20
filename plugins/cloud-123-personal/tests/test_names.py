"""Remote labels, chooser rows and relative paths have different contracts."""
import unittest
from movieclaw_sdk import drives as dv
from cloud_123_personal.backend import Personal123
from tests.fake123 import Fake123
from tests.store import MemoryStore
from tests.test_backend import fingerprint


class NameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake=Fake123();self.backend=Personal123(self.fake.transport(),store=MemoryStore())
        self.account=dv.DriveAccount('17',{'token':self.fake.token})
    async def asyncTearDown(self):await self.backend.aclose()

    async def test_remote_punctuation_and_unicode_are_preserved_in_picker_and_tree(self):
        names=['电影：你好','A:B','star*','question?','a|b','<作品>','"quoted"',"O'Brien",'空 格',' 🎬é中文 ','.hidden','%2F..%5C']
        for i,name in enumerate(names,1):
            self.fake.row(i,name,directory=True)
            self.fake.row(100+i,'文件: * ? | < > " é 🎬.txt',1,parent=i,content=b'x')
        dirs=await self.backend.list_dir(self.account,'0')
        self.assertEqual([(d.remote_id,d.name) for d in dirs],[(str(i),name) for i,name in enumerate(names,1)])
        tree=await self.backend.list_tree(self.account,'0')
        self.assertEqual(len(tree.files),len(names))
        self.assertEqual([f.rel_path for f in tree.files],[name+'/文件: * ? | < > " é 🎬.txt' for name in names])
        self.assertEqual(tree.dir_ids,tuple(str(i) for i in range(1,len(names)+1)))

    async def test_picker_ignores_irrelevant_file_name_and_size_errors(self):
        self.fake.row(1,'wanted',directory=True)
        for i,value in enumerate((None,'','..','/escape','a\\b','x\x00y',42,{},[]),2):
            row=self.fake.row(i,'placeholder',1,content=b'x')
            row['FileName']=value;row.pop('Size')
        missing=self.fake.row(20,'missing',1,content=b'x');missing.pop('FileName')
        dirs=await self.backend.list_dir(self.account,'0')
        self.assertEqual([(d.remote_id,d.name) for d in dirs],[('1','wanted')])
        with self.assertRaises(dv.ApiChanged):await self.backend.list_tree(self.account,'0')

    async def test_picker_paginates_even_when_page_contains_only_files(self):
        for i in range(1,201):
            row=self.fake.row(i,'unused',1,content=b'x');row.pop('FileName')
        self.fake.row(201,'last:directory?',directory=True)
        dirs=await self.backend.list_dir(self.account,'0')
        self.assertEqual([(d.remote_id,d.name) for d in dirs],[('201','last:directory?')])
        self.assertEqual(sum(p.endswith('/file/list') for p,_,_ in self.fake.calls),3)

    async def test_directory_missing_empty_or_nonstring_label_remains_error(self):
        row=self.fake.row(1,'folder',directory=True)
        for value in (None,'',False,17,[],{}):
            with self.subTest(value=value):
                row['FileName']=value
                with self.assertRaises(dv.ApiChanged):await self.backend.list_dir(self.account,'0')
        row.pop('FileName')
        with self.assertRaises(dv.ApiChanged):await self.backend.list_dir(self.account,'0')

    async def test_directory_nul_rejected_without_echoing_name(self):
        self.fake.row(1,'private\x00label',directory=True)
        with self.assertRaises(dv.ApiChanged) as caught:await self.backend.list_dir(self.account,'0')
        self.assertNotIn('private',str(caught.exception))
        self.assertIn('NUL',str(caught.exception))

    async def test_picker_and_scan_reject_ambiguous_directory_segments(self):
        # Host also joins picker labels into display_path: ambiguous segments must fail.
        for name in ('.','..','a/b','a\\b','/absolute','../escape','C:\\outside'):
            with self.subTest(name=name):
                self.fake.rows.clear();self.fake.row(1,name,directory=True)
                with self.assertRaises(dv.ApiChanged):await self.backend.list_dir(self.account,'0')
                self.assertEqual(await self.backend.list_dir(self.account,'1'),[])
                with self.assertRaises(dv.ApiChanged):await self.backend.list_tree(self.account,'0')

    async def test_file_path_escape_is_not_renamed_or_silently_skipped(self):
        for name in ('.','..','a/b','a\\b','/absolute','../escape','x\x00y'):
            with self.subTest(name=name):
                self.fake.rows.clear();self.fake.row(1,name,1,content=b'x')
                with self.assertRaises(dv.ApiChanged):await self.backend.list_tree(self.account,'0')

    async def test_duplicate_display_names_keep_distinct_directory_ids(self):
        self.fake.row(1,'same',directory=True);self.fake.row(2,'same',directory=True)
        self.assertEqual([d.remote_id for d in await self.backend.list_dir(self.account,'0')],['1','2'])
        with self.assertRaises(dv.Conflict):await self.backend.list_tree(self.account,'0')

    async def test_file_label_does_not_collide_with_directory_picker_label(self):
        self.fake.row(1,'same',1,content=b'x');self.fake.row(2,'same',directory=True)
        self.assertEqual([d.remote_id for d in await self.backend.list_dir(self.account,'0')],['2'])
        with self.assertRaises(dv.Conflict):await self.backend.list_tree(self.account,'0')

    async def test_type_must_be_explicit_before_ignoring_file_labels(self):
        row=self.fake.row(1,'folder',directory=True)
        for value in (None,False,2,'unknown',{},[]):
            with self.subTest(value=value):
                row['Type']=value
                with self.assertRaises(dv.ApiChanged):await self.backend.list_dir(self.account,'0')
        row.pop('Type')
        with self.assertRaises(dv.ApiChanged):await self.backend.list_dir(self.account,'0')

    async def test_string_type_values_are_supported(self):
        self.fake.row(1,'folder?',directory=True)['Type']='1'
        self.fake.row(2,'file*',1,content=b'x')['Type']='0'
        self.assertEqual([d.name for d in await self.backend.list_dir(self.account,'0')],['folder?'])
        self.assertEqual([f.rel_path for f in (await self.backend.list_tree(self.account,'0')).files],['file*'])

    async def test_new_name_validation_remains_separate_and_rejects_before_network(self):
        for name in ('..','/absolute','a\\b','a:b','a?b','a*b','a|b','a<b','a>b','a"b','a\x00b'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    await self.backend.begin_upload(self.account,'',name,fingerprint(b'abc'),None,None)
        self.assertFalse(self.fake.calls)
