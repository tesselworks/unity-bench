"""Cache states (cold / warm / as-is), snapshots and restore, keyed reuse of prepared copies, `cache list|prune`; fake Editors, no Unity."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).parent))
import testenv  # noqa: E402,F401  (first: keeps the cache directory out of the user's real one)
from unity_bench import cache, cli, config, fastcopy, runner, states, summary, workspace  # noqa: E402

FAKE = str(Path(__file__).with_name('fake_unity.py'))


def editor(*options):
    return [sys.executable, FAKE, *options]


def make_tree(base):
    base = Path(base)
    (base / 'Assets').mkdir(parents=True)
    (base / 'Assets/A.cs').write_text('class A {}\n')
    (base / 'ProjectSettings').mkdir()
    (base / 'ProjectSettings/EditorSettings.asset').write_text('original\n')
    (base / 'Packages').mkdir()
    (base / 'Packages/manifest.json').write_text('{"dependencies": {}}\n')
    return base


class FastCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_copy_tree_keeps_content_modification_times_and_links(self):
        source = make_tree(self.tmp / 'src')
        os.utime(source / 'Assets/A.cs', ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        if os.name != 'nt':
            os.symlink('A.cs', source / 'Assets/link')
        method = fastcopy.copy_tree(source, self.tmp / 'dst')
        self.assertIn(method, ('clonefile', 'reflink-auto', 'copy'))
        self.assertEqual((self.tmp / 'dst/Assets/A.cs').read_text(), 'class A {}\n')
        self.assertEqual((self.tmp / 'dst/Assets/A.cs').stat().st_mtime_ns // 1_000_000, 1_000_000_000_000)
        if os.name != 'nt':
            self.assertTrue((self.tmp / 'dst/Assets/link').is_symlink())
        (self.tmp / 'dst/Assets/A.cs').write_text('changed')   # a clone is a copy: writes never reach the original (nothing is hard-linked)
        self.assertEqual((source / 'Assets/A.cs').read_text(), 'class A {}\n')
        with self.assertRaises(FileExistsError):
            fastcopy.copy_tree(source, self.tmp / 'dst')

    def test_the_plain_copy_is_used_when_the_fast_routes_do_not_apply(self):
        source = make_tree(self.tmp / 'src')
        with mock.patch.object(fastcopy, '_clonefile', return_value=False), mock.patch.object(fastcopy, '_cp_reflink', return_value=False):
            self.assertEqual(fastcopy.copy_tree(source, self.tmp / 'dst'), 'copy')
        self.assertEqual((self.tmp / 'dst/ProjectSettings/EditorSettings.asset').read_text(), 'original\n')

    def test_remove_tree_retries_while_files_are_locked_and_then_gives_up_loudly(self):
        target = make_tree(self.tmp / 'victim')
        calls = []

        def locked_twice(path):
            calls.append(path)
            if len(calls) <= 2:
                raise PermissionError('The process cannot access the file because it is being used by another process')
            shutil.rmtree(path)

        delays = []
        with mock.patch.object(fastcopy, 'remove_path', side_effect=locked_twice), mock.patch.object(fastcopy, 'sleep', delays.append):
            self.assertEqual(fastcopy.remove_tree(target, attempts=5, delay=0.5), 2)   # two retries were needed
        self.assertEqual(delays, [0.5, 0.5])
        self.assertFalse(target.exists())
        with mock.patch.object(fastcopy, 'remove_path', side_effect=PermissionError('locked')), mock.patch.object(fastcopy, 'sleep', delays.append):
            with self.assertRaisesRegex(OSError, 'after 3 attempts'):
                fastcopy.remove_tree(self.tmp, attempts=3)

    def test_read_only_files_are_deleted(self):
        target = make_tree(self.tmp / 'victim')
        os.chmod(target / 'Assets/A.cs', 0o444)
        os.chmod(target / 'Assets', 0o555) if os.name != 'nt' else None
        try:
            fastcopy.remove_tree(target, attempts=1)
        finally:
            if (target / 'Assets').exists():
                os.chmod(target / 'Assets', 0o755)
        self.assertFalse(target.exists())

    def test_sync_root_replaces_the_top_level_and_keeps_what_it_is_told_to(self):
        snapshot = make_tree(self.tmp / 'snap')
        (snapshot / 'Library').mkdir()
        (snapshot / 'Library/state.txt').write_text('primed\n')
        live = make_tree(self.tmp / 'live')
        (live / '.git').write_text('gitdir: elsewhere\n')
        (live / 'Library').mkdir()
        (live / 'Library/state.txt').write_text('dirty\n')
        (live / 'Assets/Extra.cs').write_text('left behind by a process')
        (live / 'Leftover').mkdir()
        result = fastcopy.sync_root(snapshot, live, skip=('.git',))
        self.assertEqual((live / 'Library/state.txt').read_text(), 'primed\n')
        self.assertFalse((live / 'Assets/Extra.cs').exists())
        self.assertFalse((live / 'Leftover').exists())
        self.assertTrue((live / '.git').is_file())
        self.assertEqual(result['retries'], 0)

    def test_long_paths_get_the_extended_prefix_on_windows_only(self):
        text = fastcopy.extended(self.tmp / 'a')
        self.assertEqual(text.startswith('\\\\?\\'), os.name == 'nt')
        self.assertTrue(text.endswith('a'))


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'prep.py').write_text('print(1)\n')
        self.project = {'prepare': [['{python}', '{campaign}/prep.py', '--project', '{project}']], 'prepare_inputs': [], 'campaign_dir': str(self.tmp)}
        self.variant = {'id': 'a', 'backend': 'mono', 'build': {}}
        self.identity = {'mode': 'worktree', 'commit': 'c' * 40, 'relative': 'unity', 'package_lock': 'l1'}
        self.editor = {'full': '7000.0.0a7 (581996e1a8f7)'}

    def key(self, project=None, variant=None, identity=None, editor=None, injected='p1', target='/work/a'):
        cfg = {'project': project or self.project}
        parts, _ = cache.components(cfg, variant or self.variant, target, identity or self.identity, editor or self.editor, injected)
        return cache.key_of(parts)

    def test_the_key_is_stable_and_every_component_changes_it(self):
        base = self.key()
        self.assertEqual(base, self.key())
        changed = {
            'commit': self.key(identity={**self.identity, 'commit': 'd' * 40}),
            'lock': self.key(identity={**self.identity, 'package_lock': 'l2'}),
            'editor': self.key(editor={'full': '7000.0.0a8 (aaaaaaaaaaaa)'}),
            'backend': self.key(variant={**self.variant, 'backend': 'il2cpp'}),
            'build settings': self.key(variant={**self.variant, 'build': {'stripping': 'Minimal'}}),
            'injected package': self.key(injected='p2'),
            'prepare command': self.key(project={**self.project, 'prepare': [['{python}', '{campaign}/prep.py', '--with', 'core']]}),
            'path': self.key(target='/other/a'),
        }
        self.assertEqual(len(set(changed.values()) | {base}), len(changed) + 1, changed)
        (self.tmp / 'prep.py').write_text('print(2)\n')   # the script a command names is part of the key
        self.assertNotEqual(self.key(), base)

    def test_prepare_inputs_files_and_directories_are_hashed_and_the_variant_only_when_used(self):
        (self.tmp / 'adapters').mkdir()
        (self.tmp / 'adapters/Core.cs').write_text('v1')
        project = {**self.project, 'prepare_inputs': ['adapters']}
        first = self.key(project=project)
        (self.tmp / 'adapters/Core.cs').write_text('v2')
        self.assertNotEqual(self.key(project=project), first)
        self.assertEqual(self.key(variant={**self.variant, 'id': 'b'}, project=project), self.key(project=project))   # the id is not in play unless asked for (the path already holds it)
        uses_variant = {**self.project, 'prepare': [['{python}', '{campaign}/prep.py', '{variant}']]}
        self.assertNotEqual(self.key(project=uses_variant), self.key(variant={**self.variant, 'id': 'b'}, project=uses_variant))

    def test_editor_identity_reads_the_bundle_and_falls_back_to_the_version_folder(self):
        contents = self.tmp / '7000.0.0a7/Unity.app/Contents'
        (contents / 'MacOS').mkdir(parents=True)
        (contents / 'MacOS/Unity').write_text('')
        (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleGetInfoString': 'Unity version 7000.0.0a7 (581996e1a8f7). (c) 2005-2026', 'CFBundleVersion': '7000.0.0a7'}))
        info = cache.editor_identity(str(contents / 'MacOS/Unity'))
        self.assertEqual((info['full'], info['source']), ('7000.0.0a7 (581996e1a8f7)', 'Info.plist'))
        plain = self.tmp / 'Hub/Editor/6000.7.0b2/Editor/Unity'
        plain.parent.mkdir(parents=True)
        plain.write_text('binary')
        info = cache.editor_identity([sys.executable, str(plain)])   # a command list: the last item is the Editor
        self.assertEqual(info['source'], 'path')
        self.assertTrue(info['full'].startswith('6000.7.0b2 ['))
        plain.write_text('a reinstalled binary of another size')
        self.assertNotEqual(cache.editor_identity(str(plain))['full'], info['full'])

    def test_a_copy_has_no_commit_so_its_files_name_it(self):
        project = make_tree(self.tmp / 'proj')
        first = cache.stat_digest(project, workspace.SKIP_DIRS)
        (project / 'Library').mkdir()
        (project / 'Library/x').write_text('ignored')
        self.assertEqual(cache.stat_digest(project, workspace.SKIP_DIRS), first)   # Library never counts
        (project / 'Assets/A.cs').write_text('class A { int x; }\n')
        self.assertNotEqual(cache.stat_digest(project, workspace.SKIP_DIRS), first)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = cache.Store(self.tmp / 'cache')

    def make_entry(self, key, last_used=None, size=10):
        staged = self.store.staging(key)
        tree = make_tree(staged / 't')
        (tree / 'Library').mkdir()
        (tree / 'Library/ArtifactDB').write_text('db')
        data = cache.manifest(tree)
        entry = self.store.commit(staged, key, {'variant': 'a', 'components': {'editor': 'e', 'source': {'mode': 'copy', 'tree': 'f' * 64}}, 'project_relative': '.',
                                                'manifest': data, 'manifest_digest': cache.manifest_digest(data), 'size_bytes': size, 'built_seconds': 3})
        if last_used:
            entry.meta['last_used'] = last_used
            (entry.path / 'meta.json').write_text(json.dumps(entry.meta))
        return entry

    def test_lookup_accepts_an_untouched_entry_and_says_why_it_rejects_a_changed_one(self):
        self.assertEqual(self.store.lookup('k1'), (None, 'no entry'))
        entry = self.make_entry('k1')
        found, why = self.store.lookup('k1')
        self.assertIsNone(why)
        self.assertEqual(found.key, 'k1')
        (entry.tree / 'ProjectSettings/EditorSettings.asset').write_text('edited by Unity\n')
        found, why = self.store.lookup('k1')
        self.assertIsNone(found)
        self.assertIn('ProjectSettings/EditorSettings.asset', why)
        self.assertIsNone(cache.Store(self.tmp / 'cache', reuse=False).lookup('k1')[0])

    def test_each_kind_of_tampering_changes_the_manifest(self):
        entry = self.make_entry('k2')
        base = cache.manifest(entry.tree)
        for label, change in {
            'asset added': lambda t: (t / 'Assets/B.cs').write_text('x'),
            'asset rewritten': lambda t: (t / 'Assets/A.cs').write_text('class A { int y; }\n'),
            'package manifest': lambda t: (t / 'Packages/manifest.json').write_text('{"dependencies": {"x": "1"}}\n'),
            'library entry': lambda t: (t / 'Library/Bee').mkdir(),
            'artifact db': lambda t: (t / 'Library/ArtifactDB').write_text('a larger database'),
            'top-level file': lambda t: (t / 'README.md').write_text('new'),
        }.items():
            with self.subTest(label):
                tree = make_tree(self.tmp / ('copy-' + label.replace(' ', '-')))
                shutil.rmtree(tree)
                shutil.copytree(entry.tree, tree)
                change(tree)
                self.assertNotEqual(cache.manifest_digest(cache.manifest(tree)), cache.manifest_digest(base))
                self.assertTrue(cache.manifest_diff(base, cache.manifest(tree)))

    def test_prune_by_age_and_count_and_dry_run(self):
        self.make_entry('old', last_used='2020-01-01T00:00:00')
        self.make_entry('mid', last_used='2026-01-01T00:00:00')
        self.make_entry('new', last_used=cache.now())
        broken = self.store.root / 'e' / 'broken'
        broken.mkdir()
        self.assertEqual({e.key for e in self.store.prune(older_than=365 * 86400, dry_run=True)}, {'old', 'broken'} | ({'mid'} if time.time() > 1.83e9 else set()))
        self.assertEqual(len(self.store.entries()), 4)   # a dry run deletes nothing
        removed = {e.key for e in self.store.prune(keep=1)}
        self.assertEqual(removed, {'broken', 'old', 'mid'})
        self.assertEqual([e.key for e in self.store.entries()], ['new'])
        self.assertEqual([e.key for e in self.store.prune(keep=0)], ['new'])
        self.assertEqual(self.store.entries(), [])

    def test_durations(self):
        self.assertEqual(cache.parse_age('12h'), 12 * 3600)
        self.assertEqual(cache.parse_age('2w'), 14 * 86400)
        with self.assertRaises(ValueError):
            cache.parse_age('soon')

    def test_listing_names_the_entries(self):
        self.assertIn('no prepared copies', cache.listing(self.store)[0])
        self.make_entry('k3', size=5 << 20)
        text = '\n'.join(cache.listing(self.store))
        self.assertIn('k3', text)
        self.assertIn('5.0 MB', text)


class CampaignCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {'UNITY_BENCH_CACHE_DIR': str(self.tmp / 'cache')})
        patch.start()
        self.addCleanup(patch.stop)
        make_tree(self.tmp / 'proj')

    def campaign(self, variants, workloads, project=None, cache_table=None, **defaults):
        cfg = {'name': 'fake', 'project': {'path': 'proj', **(project or {})}, 'defaults': {'repeats': 2, 'rounds': 2, **defaults}, 'variants': variants, 'workloads': workloads,
               **({'cache': cache_table} if cache_table is not None else {})}
        path = self.tmp / 'c.json'
        path.write_text(json.dumps(cfg))
        return path

    def run_campaign(self, path, name='out', **kwargs):
        out = runner.run_campaign(path, self.tmp / name, log=lambda *_: None, **kwargs)
        return out, summary.read_rows(out / 'results.jsonl')

    def processes(self, rows, workload):
        return [r for r in rows if r['type'] == 'process' and r['workload'] == workload]

    def opens(self, out, row):
        """How often the Library had been opened before this process started: what the fake Editor logged."""
        log = (out / row['log']).read_text()
        return int(next(line for line in log.splitlines() if line.startswith('Library opens before this launch:')).rsplit(' ', 1)[1])

    def workloads(self):
        return [{'id': 'cold', 'kind': 'editor.cold_open', 'repeats': 2},
                {'id': 'asis', 'kind': 'editor.warm_open', 'repeats': 3},
                {'id': 'warm', 'kind': 'editor.warm_open', 'state': 'warm', 'repeats': 3}]

    def test_states_start_each_process_from_the_state_they_name(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], self.workloads())
        out, rows = self.run_campaign(path)
        self.assertEqual([self.opens(out, r) for r in self.processes(rows, 'cold')], [0, 0])               # Library removed each time
        self.assertEqual([self.opens(out, r) for r in self.processes(rows, 'asis')], [1, 2, 3])            # starts from whatever was left (the cold run's Library, then each open's)
        self.assertEqual([self.opens(out, r) for r in self.processes(rows, 'warm')], [1, 1, 1])            # the snapshot, every time
        cold, asis, warm = (self.processes(rows, w)[0]['state'] for w in ('cold', 'asis', 'warm'))
        self.assertEqual((cold['requested'], cold['actual'], cold['library_removed']), ('cold', 'cold', True))
        self.assertEqual((asis['requested'], asis['actual']), ('as-is', 'as-is'))
        self.assertEqual((warm['requested'], warm['actual'], warm['verified']), ('warm', 'warm', True))
        self.assertTrue(warm['snapshot'] and warm['restore_method'])
        self.assertEqual(cold['imports']['actual'], 5)                                                   # the Editor's own count: a cold start imports
        self.assertEqual(self.processes(rows, 'warm')[0]['state']['imports']['actual'], 0)               # and a warm one does not
        md = (out / 'summary.md').read_text()
        self.assertIn(f"warm (snapshot `{warm['snapshot']}`)", md)
        self.assertIn('prepared: built', md)
        s = json.loads((out / 'summary.json').read_text())
        self.assertEqual({g['workload'] for g in s['states']['workloads']}, {'cold', 'asis', 'warm'})

    def test_cold_points_the_global_caches_at_empty_directories_made_for_that_process(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'cold', 'kind': 'editor.cold_open', 'repeats': 2}, {'id': 'asis', 'kind': 'editor.warm_open', 'repeats': 1}])
        before = dict(os.environ)
        out, rows = self.run_campaign(path)
        seen = []
        for row in self.processes(rows, 'cold'):
            log = (out / row['log']).read_text().splitlines()
            env = {line.split('=', 1)[0][4:]: line.split('=', 1)[1] for line in log if line.startswith('ENV ')}
            args = next(line for line in log if line.startswith('ARGS '))
            self.assertTrue(env['BEE_CACHE_DIRECTORY'] and env['UPM_CACHE_ROOT'])
            self.assertNotEqual(env['BEE_CACHE_DIRECTORY'], env['UPM_CACHE_ROOT'])
            self.assertIn('-cacheServerEnableDownload false -cacheServerEnableUpload false', args)
            self.assertEqual(sorted(row['state']['isolated']), ['accelerator', 'compilation', 'upm'])
            seen.append(env['UPM_CACHE_ROOT'])
            self.assertFalse(Path(env['UPM_CACHE_ROOT']).exists())   # the scratch directory is gone afterwards
        self.assertEqual(len(set(seen)), 2)                          # a fresh one per process
        asis_log = (out / self.processes(rows, 'asis')[0]['log']).read_text()
        self.assertIn('ENV BEE_CACHE_DIRECTORY=\n', asis_log)       # nothing isolated outside cold
        self.assertNotIn('cacheServer', asis_log)
        self.assertEqual(dict(os.environ), before)                   # this process's environment is back as it was

    def test_the_isolated_caches_can_be_chosen_or_switched_off(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'cold', 'kind': 'editor.cold_open', 'repeats': 1}], cache_table={'isolate': ['compilation']})
        out, rows = self.run_campaign(path)
        log = (out / self.processes(rows, 'cold')[0]['log']).read_text()
        self.assertNotIn('ENV BEE_CACHE_DIRECTORY=\n', log)
        self.assertIn('ENV UPM_CACHE_ROOT=\n', log)
        self.assertNotIn('cacheServer', log)
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'cold', 'kind': 'editor.cold_open', 'repeats': 1}], cache_table={'isolate': []})
        out, rows = self.run_campaign(path, name='out2')
        self.assertEqual(self.processes(rows, 'cold')[0]['state']['isolated'], {})
        self.assertEqual(self.opens(out, self.processes(rows, 'cold')[0]), 0)

    def test_the_second_run_reuses_the_prepared_copy_and_does_not_import_again(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--scale', '2')}], [{'id': 'asis', 'kind': 'editor.warm_open', 'repeats': 2}])
        out1, rows1 = self.run_campaign(path, name='run1')
        out2, rows2 = self.run_campaign(path, name='run2')
        self.assertEqual([r['variant'] for r in rows1 if r['type'] == 'prepare'], ['a', 'b'])
        self.assertEqual([r for r in rows2 if r['type'] == 'prepare'], [])                          # no priming open the second time
        c1, c2 = (json.loads((o / 'campaign.json').read_text())['provenance']['variants'] for o in (out1, out2))
        self.assertEqual({v: c1[v]['prepared']['status'] for v in c1}, {'a': 'built', 'b': 'built'})
        self.assertEqual({v: c2[v]['prepared']['status'] for v in c2}, {'a': 'reused', 'b': 'reused'})
        self.assertEqual(c1['a']['prepared']['key'], c2['a']['prepared']['key'])
        self.assertNotEqual(c1['a']['prepared']['key'], c1['b']['prepared']['key'])
        self.assertEqual(c2['a']['package'], 'injected')                                            # the provenance of the first preparation travels with the entry
        self.assertEqual([self.opens(out2, r) for r in self.processes(rows2, 'asis') if r['variant'] == 'a'], [1, 2])   # the primed Library, not an empty one
        md = (out2 / 'summary.md').read_text()
        self.assertIn(f"prepared: reused `{c2['a']['prepared']['key']}`", md)
        self.assertIn(f"prepared: built", (out1 / 'summary.md').read_text())
        listing = cache.listing(cache.Store(cache.default_cache_dir()))
        self.assertEqual(len(listing), 4)
        self.assertEqual(listing[2].split()[-3], '1')   # reused once

    def test_a_changed_input_prepares_again(self):
        script = self.tmp / 'prep.py'
        script.write_text('print(1)\n')
        project = {'prepare': [['{python}', '{campaign}/prep.py']]}
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}], project=project)
        first = self.run_campaign(path, name='r1')[0]
        script.write_text('print(2)\n')                                                               # the prepare script is part of the key
        second = self.run_campaign(path, name='r2')[0]
        (self.tmp / 'proj/Assets/A.cs').write_text('class A { int changed; }\n')                       # so is the source project
        third = self.run_campaign(path, name='r3')[0]
        keys = [json.loads((o / 'campaign.json').read_text())['provenance']['variants']['a']['prepared'] for o in (first, second, third)]
        self.assertEqual([k['status'] for k in keys], ['built', 'built', 'built'])
        self.assertEqual(len({k['key'] for k in keys}), 3)
        self.assertEqual(self.run_campaign(path, name='r4')[0] and json.loads((self.tmp / 'r4/campaign.json').read_text())['provenance']['variants']['a']['prepared']['status'], 'reused')

    def test_an_entry_whose_files_were_modified_is_not_trusted(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}])
        out, _ = self.run_campaign(path, name='r1')
        key = json.loads((out / 'campaign.json').read_text())['provenance']['variants']['a']['prepared']['key']
        entry = cache.Store(cache.default_cache_dir()).entry_path(key)
        (entry / 't/ProjectSettings/EditorSettings.asset').write_text('rewritten by something else\n')
        out2, rows = self.run_campaign(path, name='r2')
        prep = json.loads((out2 / 'campaign.json').read_text())['provenance']['variants']['a']['prepared']
        self.assertEqual(prep['status'], 'built')
        self.assertIn('ProjectSettings/EditorSettings.asset', prep['rebuilt_because'])
        self.assertIn('the cached copy was not used', (out2 / 'summary.md').read_text())
        self.assertEqual((entry / 't/ProjectSettings/EditorSettings.asset').read_text(), 'original\n')   # replaced by a fresh preparation
        self.assertTrue(all(r['status'] == 'ok' for r in rows if r['type'] == 'process'))

    def test_a_failed_priming_open_is_recorded_and_nothing_untrustworthy_is_cached(self):
        path = self.campaign([{'id': 'a', 'editor': editor('--fail')}, {'id': 'b', 'editor': editor()}], [{'id': 'warm', 'kind': 'editor.warm_open', 'state': 'warm', 'repeats': 2}])
        out, rows = self.run_campaign(path)
        primes = {r['variant']: r for r in rows if r['type'] == 'prepare'}
        self.assertEqual((primes['a']['status'], primes['b']['status']), ('failed', 'ok'))
        self.assertIn('exit code 1', primes['a']['error'])
        a = self.processes([r for r in rows if r['variant'] == 'a'], 'warm')[0]
        self.assertEqual((a['state']['requested'], a['state']['actual']), ('warm', 'as-is'))            # no snapshot: the process ran anyway, and says what it had
        self.assertIn('no snapshot', a['state']['notes'][0])
        self.assertEqual([e.meta['variant'] for e in cache.Store(cache.default_cache_dir()).entries()], ['b'])
        self.assertIn('the priming open failed', (out / 'summary.md').read_text())

    def test_warm_works_without_the_cache_and_leaves_nothing_behind(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'warm', 'kind': 'editor.warm_open', 'state': 'warm', 'repeats': 2}])
        out, rows = self.run_campaign(path, use_cache=False)
        self.assertEqual([self.opens(out, r) for r in self.processes(rows, 'warm')], [1, 1])
        self.assertFalse((out / 'snapshots').exists())
        self.assertFalse((self.tmp / 'cache').exists())                                                 # the user's cache directory was not even created
        self.assertEqual(json.loads((out / 'campaign.json').read_text())['cache']['enabled'], False)
        again = self.run_campaign(path, name='out2', use_cache=False)[0]
        self.assertEqual(json.loads((again / 'campaign.json').read_text())['provenance']['variants']['a']['prepared']['status'], 'built')   # never reused

    def test_without_the_cache_and_without_warm_nothing_changes_from_v03(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'asis', 'kind': 'editor.warm_open', 'repeats': 2}])
        out, rows = self.run_campaign(path, use_cache=False, keep_work=True)
        self.assertEqual([r for r in rows if r['type'] == 'prepare'], [])                               # no priming open
        self.assertEqual([self.opens(out, r) for r in self.processes(rows, 'asis')], [0, 1])            # the first process builds the Library, as before
        self.assertTrue((out / 'work/a/Assets/A.cs').is_file())                                         # live copies stay in the run directory

    def test_restore_retries_while_files_are_held_and_the_process_row_says_so(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'warm', 'kind': 'editor.warm_open', 'state': 'warm', 'repeats': 2}])
        real = fastcopy.remove_path
        failures = {'left': 2}

        def locked_at_first(target):
            if failures['left'] and str(target).endswith('Library'):
                failures['left'] -= 1
                raise PermissionError('in use by another process')
            real(target)

        with mock.patch.object(fastcopy, 'remove_path', side_effect=locked_at_first), mock.patch.object(fastcopy, 'sleep', lambda *_: None):
            out, rows = self.run_campaign(path)
        first = self.processes(rows, 'warm')[0]['state']
        self.assertEqual(first['delete_retries'], 2)
        self.assertTrue(first['verified'])
        self.assertEqual([self.opens(out, r) for r in self.processes(rows, 'warm')], [1, 1])

    def test_a_cached_copy_that_cannot_be_restored_is_prepared_the_long_way(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}])
        self.run_campaign(path, name='r1')
        with mock.patch.object(workspace, 'materialize', side_effect=OSError('No space left on device')):
            out, rows = self.run_campaign(path, name='r2')
        prep = json.loads((out / 'campaign.json').read_text())['provenance']['variants']['a']['prepared']
        self.assertEqual(prep['status'], 'built')
        self.assertIn('No space left on device', prep['rebuilt_because'])
        self.assertTrue(all(r['status'] == 'ok' for r in rows if r['type'] == 'process'))
        self.assertEqual(len(cache.Store(cache.default_cache_dir()).entries()), 1)   # the entry is still there, and was written again

    def test_a_git_worktree_variant_is_reused_too_and_keeps_files_outside_the_project(self):
        repo = self.tmp / 'repo'
        make_tree(repo / 'unity')
        (repo / 'packages/core').mkdir(parents=True)
        (repo / 'packages/core/Core.cs').write_text('class Core {}\n')
        git = lambda *a: subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *a], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
        git('init', '-q')
        git('add', '-A')
        git('commit', '-q', '-m', 'x')
        head = git('rev-parse', 'HEAD')
        cfg = {'name': 'fake', 'project': {'path': 'repo/unity'}, 'defaults': {'repeats': 2, 'rounds': 1}, 'variants': [{'id': 'wt', 'editor': editor(), 'revision': 'HEAD'}],
               'workloads': [{'id': 'warm', 'kind': 'editor.warm_open', 'state': 'warm'},
                             {'id': 'edit', 'kind': 'editor.script_edit', 'repeats': 1, 'targets': [{'id': 'core', 'path': 'packages/core/Core.cs', 'root': 'worktree'}]}]}
        path = self.tmp / 'wt.json'
        path.write_text(json.dumps(cfg))
        out1, _ = self.run_campaign(path, name='r1')
        out2, rows = self.run_campaign(path, name='r2', keep_work=True)
        prepared = json.loads((out2 / 'campaign.json').read_text())['provenance']['variants']['wt']
        self.assertEqual((prepared['prepared']['status'], prepared['commit'], prepared['mode']), ('reused', head, 'worktree'))
        work = Path(json.loads((out2 / 'campaign.json').read_text())['cache']['work']) / 'wt'
        self.assertTrue((work / 'unity/Assets/A.cs').is_file())
        self.assertTrue((work / 'packages/core/Core.cs').is_file())                                      # outside the Unity project, inside the checkout
        self.assertTrue((work / '.git').is_file())                                                       # a registered worktree again, removable by git
        self.assertEqual([self.opens(out2, r) for r in self.processes(rows, 'warm')], [1, 1])
        self.assertTrue(all(r['status'] == 'ok' for r in rows if r['type'] == 'process'))
        subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(work)], capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'worktree', 'prune'], capture_output=True)

    def test_the_default_work_directory_is_stable_and_a_user_one_is_still_protected(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}])
        work = self.tmp / 'cache/w'
        (work / 'a').mkdir(parents=True)
        (work / 'a/leftover.txt').write_text('from a killed run')
        out, rows = self.run_campaign(path)                                                              # the managed directory is ours to clean
        self.assertTrue(all(r['status'] == 'ok' for r in rows if r['type'] == 'process'))
        mine = self.tmp / 'mine'
        (mine / 'a').mkdir(parents=True)
        (mine / 'a/precious.txt').write_text('user data')
        with self.assertRaises(FileExistsError):
            self.run_campaign(path, name='out2', work=mine)
        self.assertTrue((mine / 'a/precious.txt').is_file())

    def test_settings_the_editor_rewrites_during_a_process_are_flagged(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'asis', 'kind': 'editor.warm_open', 'repeats': 1}])
        project = self.tmp / 'proj'
        with mock.patch.object(workspace, 'remove_library', lambda *_: None):
            real = cache.settings_digests
            calls = []

            def changing(p):
                calls.append(1)
                out = real(p)
                if len(calls) % 2 == 0:   # the "after" read of every process
                    out['ProjectSettings/EditorSettings.asset'] = 'something else'
                return out

            with mock.patch.object(cache, 'settings_digests', changing):
                out, rows = self.run_campaign(path)
        state = self.processes(rows, 'asis')[0]['state']
        self.assertIn('ProjectSettings/EditorSettings.asset', state['settings_changed'])
        self.assertEqual(project.name, 'proj')


class StateConfigTests(unittest.TestCase):
    def base(self, **workload):
        return {'name': 'c', 'project': {'path': 'p'}, 'variants': [{'id': 'a', 'editor': '/u/a'}], 'workloads': [{'id': 'w', 'kind': 'editor.warm_open', **workload}]}

    def test_defaults_keep_the_behaviour_of_each_kind(self):
        raw = self.base()
        raw['workloads'] = [{'id': k.replace('.', '-'), 'kind': k, **({'targets': [{'id': 't', 'path': 'Assets/A.cs'}]} if k == 'editor.script_edit' else {}),
                             **({'method': 'A.B.C'} if k == 'editor.adapter' else {})} for k in states.STATE_KINDS]
        cfg = config.validate(raw, '/base')
        self.assertEqual({w['kind']: w['state'] for w in cfg['workloads']},
                         {'editor.cold_open': 'cold', 'editor.warm_open': 'as-is', 'editor.script_edit': 'as-is', 'editor.play': 'as-is', 'editor.adapter': 'as-is', 'player.build_time': 'as-is'})
        self.assertEqual(cfg['cache']['isolate'], ['compilation', 'upm', 'accelerator'])
        self.assertEqual(cfg['project']['prepare_inputs'], [])

    def test_state_is_validated_and_only_for_workloads_that_start_an_editor(self):
        for state in states.STATES:
            self.assertEqual(config.validate(self.base(state=state), '/base')['workloads'][0]['state'], state)
        with self.assertRaisesRegex(config.ConfigError, 'state must be one of'):
            config.validate(self.base(state='lukewarm'), '/base')
        raw = self.base()
        raw['workloads'] = [{'id': 'p', 'kind': 'player.startup', 'state': 'cold'}]
        with self.assertRaisesRegex(config.ConfigError, 'runs a built player'):
            config.validate(raw, '/base')

    def test_cache_table_and_prepare_inputs_are_validated(self):
        raw = self.base()
        raw['cache'] = {'isolate': ['upm']}
        raw['project']['prepare_inputs'] = ['prepare.py', 'adapters']
        cfg = config.validate(raw, '/base')
        self.assertEqual((cfg['cache']['isolate'], cfg['project']['prepare_inputs']), (['upm'], ['prepare.py', 'adapters']))
        for bad, message in (({'isolate': ['gpu']}, 'cache.isolate'), ({'isolate': 'upm'}, 'cache.isolate'), ({'isolate': ['upm', 'upm']}, 'cache.isolate'), ({'size': 1}, 'unknown key')):
            raw = self.base()
            raw['cache'] = bad
            with self.assertRaisesRegex(config.ConfigError, message):
                config.validate(raw, '/base')
        raw = self.base()
        raw['project']['prepare_inputs'] = 'prepare.py'
        with self.assertRaisesRegex(config.ConfigError, 'prepare_inputs'):
            config.validate(raw, '/base')

    def test_log_parsing(self):
        p = Path(tempfile.mkdtemp()) / 'log.txt'
        self.addCleanup(shutil.rmtree, p.parent, True)
        p.write_text('x\n\t\tImports: total=43 (actual=40, local cache=2, cache server=1)\nImports: total=1 (actual=1, local cache=0, cache server=0)\n')
        self.assertEqual(states.initial_import(p), {'total': 43, 'actual': 40, 'local_cache': 2, 'cache_server': 1})
        self.assertIsNone(states.initial_import(p.with_name('missing.txt')))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_cache_list_and_prune(self):
        store = cache.Store(self.tmp / 'cache')
        for key, when in (('aaaa', '2020-01-01T00:00:00'), ('bbbb', cache.now())):
            staged = store.staging(key)
            make_tree(staged / 't')
            store.commit(staged, key, {'variant': 'v', 'components': {'editor': 'e', 'source': {'mode': 'copy', 'tree': 'x' * 64}}, 'last_used': when, 'size_bytes': 2048})
        code, out, _ = self.main('cache', 'list', '--cache-dir', str(self.tmp / 'cache'))
        self.assertEqual(code, 0)
        self.assertIn('aaaa', out)
        self.assertIn('bbbb', out)
        code, out, _ = self.main('cache', 'prune', '--cache-dir', str(self.tmp / 'cache'), '--older-than', '30d', '--dry-run')
        self.assertIn('would delete aaaa', out)
        self.assertNotIn('bbbb', out)
        self.assertEqual(len(store.entries()), 2)
        code, out, _ = self.main('cache', 'prune', '--cache-dir', str(self.tmp / 'cache'), '--older-than', '30d')
        self.assertIn('deleted aaaa', out)
        self.assertEqual([e.key for e in store.entries()], ['bbbb'])
        code, _, err = self.main('cache', 'prune', '--cache-dir', str(self.tmp / 'cache'))
        self.assertEqual(code, 2)
        self.assertIn('--older-than', err)
        code, _, err = self.main('cache', 'prune', '--cache-dir', str(self.tmp / 'cache'), '--older-than', 'soon')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
