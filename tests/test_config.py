from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unity_bench import config  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'minimal.toml'


def base(**changes):
    raw = {'name': 'c', 'project': {'path': 'p'},
           'variants': [{'id': 'a', 'editor': '/u/a'}, {'id': 'b', 'editor': '/u/b'}],
           'workloads': [{'id': 'cold', 'kind': 'editor.cold_open'}]}
    raw.update(changes)
    return raw


class ConfigTests(unittest.TestCase):
    def check(self, raw, message):
        with self.assertRaisesRegex(config.ConfigError, message):
            config.validate(raw, '/base')

    def test_example_campaign_is_valid(self):
        cfg = config.load(EXAMPLE)
        self.assertEqual([w['kind'] for w in cfg['workloads']][:3], ['editor.cold_open', 'editor.warm_open', 'editor.script_edit'])
        self.assertTrue(Path(cfg['project']['path']).is_absolute())
        self.assertEqual(cfg['defaults']['baseline'], 'unity-6.7')

    def test_defaults_and_overrides(self):
        cfg = config.validate(base(defaults={'repeats': 5}, workloads=[{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 2}]), '/base')
        self.assertEqual(cfg['workloads'][0]['repeats'], 2)
        self.assertEqual(cfg['workloads'][0]['rounds'], 5)
        self.assertEqual(config.validate(base(defaults={'repeats': 5}), '/base')['workloads'][0]['repeats'], 5)
        self.assertEqual(cfg['defaults']['baseline'], 'a')

    def test_rejects_bad_input(self):
        self.check(base(variants=[]), 'At least one')
        self.check(base(variants=[{'id': 'a', 'editor': '/u'}, {'id': 'a', 'editor': '/u'}]), 'Duplicate variant')
        self.check(base(variants=[{'id': 'a b', 'editor': '/u'}]), 'variant.id')
        self.check(base(variants=[{'id': 'a'}]), 'editor')
        self.check(base(variants=[{'id': 'a', 'editor': '/u', 'backend': 'wasm'}]), 'backend')
        self.check(base(variants=[{'id': 'a', 'editor': '/u', 'typo': 1}]), 'unknown key')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.nope'}]), 'kind')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.cold_open', 'repeats': 0}]), 'positive integer')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.cold_open', 'variants': ['zzz']}]), 'known variant')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.cold_open', 'targets': []}]), 'unknown key')
        self.check(base(defaults={'baseline': 'zzz'}), 'baseline')
        self.check(base(project={}), 'project.path')
        self.check(base(extra=1), 'unknown key')

    def test_script_edit_targets_are_validated(self):
        def edit(targets):
            return base(workloads=[{'id': 'w', 'kind': 'editor.script_edit', 'targets': targets}])
        ok = config.validate(edit([{'id': 't', 'path': 'Assets/A.cs'}]), '/base')
        self.assertEqual(ok['workloads'][0]['targets'][0]['change'], 'comment')
        self.check(edit([]), 'non-empty')
        self.check(edit([{'id': 't', 'path': '../A.cs'}]), 'project-relative')
        self.check(edit([{'id': 't', 'path': '/abs/A.cs'}]), 'project-relative')
        self.check(edit([{'id': 't', 'path': 'A.txt'}]), 'project-relative')
        self.check(edit([{'id': 't', 'path': 'A.cs', 'change': 'rewrite'}]), 'change')
        self.check(edit([{'id': 't', 'path': 'A.cs'}, {'id': 't', 'path': 'B.cs'}]), 'duplicate target')

    def test_play_adapter_and_player_options(self):
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.play', 'domain_reload': 'maybe'}]), 'domain_reload')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.adapter'}]), 'method')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.adapter', 'method': 'NoNamespace'}]), 'method')
        self.check(base(workloads=[{'id': 'w', 'kind': 'player.build_time', 'mode': 'fast'}]), 'mode')
        self.check(base(workloads=[{'id': 'w', 'kind': 'player.build_time', 'backend': 'jvm'}]), 'backend')
        self.check(base(workloads=[{'id': 'w', 'kind': 'player.startup', 'build': 'missing'}]), 'build must name')
        ok = config.validate(base(workloads=[{'id': 'b', 'kind': 'player.build_time'}, {'id': 's', 'kind': 'player.startup', 'build': 'b'}]), '/base')
        self.assertEqual(ok['workloads'][0]['timeout_seconds'], ok['defaults']['build_timeout_seconds'])

    def test_headline_and_build_settings_are_validated(self):
        cfg = config.validate(base(build={'api_compatibility': 'NET_Standard', 'stripping': 'Minimal'},
                                   variants=[{'id': 'a', 'editor': '/u/a'}, {'id': 'b', 'editor': '/u/b', 'build': {'api_compatibility': 'NET'}}],
                                   workloads=[{'id': 'w', 'kind': 'editor.adapter', 'method': 'A.B.Run', 'headline': ['core/*']}]), '/base')
        self.assertEqual([v['build'] for v in cfg['variants']], [{'api_compatibility': 'NET_Standard', 'stripping': 'Minimal'}, {'api_compatibility': 'NET', 'stripping': 'Minimal'}])
        self.assertEqual(cfg['workloads'][0]['headline'], ['core/*'])
        self.check(base(build={'api': 'x'}), 'unknown key')
        self.check(base(build={'stripping': 'not valid'}), 'enum name')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.warm_open', 'headline': []}]), 'headline')
        self.check(base(workloads=[{'id': 'w', 'kind': 'editor.warm_open', 'headline': False}]), 'headline')


class ScheduleTests(unittest.TestCase):
    def test_two_variants_alternate_a_b_then_b_a(self):
        cfg = config.validate(base(workloads=[{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 4}]), '/base')
        rows = config.schedule(cfg)
        self.assertEqual([r['variant'] for r in rows], ['a', 'b', 'b', 'a', 'a', 'b', 'b', 'a'])
        self.assertEqual([r['index'] for r in rows], list(range(8)))
        self.assertEqual([r['repeat'] for r in rows], [0, 0, 1, 1, 2, 2, 3, 3])

    def test_three_variants_use_each_rotation_followed_by_its_reverse(self):
        raw = base(variants=[{'id': x, 'editor': '/u/' + x} for x in 'abc'], workloads=[{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 2}])
        notes = []
        rows = config.schedule(config.validate(raw, '/base'), notes=notes)
        self.assertEqual(''.join(r['variant'] for r in rows), 'abc' + 'cba')
        self.assertEqual(notes, [])                                   # two repeats are already a balanced pair
        raw['workloads'][0]['repeats'] = 3
        rows = config.schedule(config.validate(raw, '/base'), notes=notes)
        self.assertEqual(''.join(r['variant'] for r in rows), 'abc' + 'cba' + 'bca' + 'acb')   # odd repeats are raised to the next even number
        self.assertEqual(max(r['repeat'] for r in rows), 3)
        self.assertIn('repeats 3 -> 4', notes[0])

    def test_every_compared_pair_runs_in_both_orders_equally_often(self):
        for n in (2, 3, 4, 5, 6):
            ids = [chr(97 + i) for i in range(n)]
            raw = base(variants=[{'id': x, 'editor': '/u/' + x} for x in ids], workloads=[{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 2 * n}])
            rows = config.schedule(config.validate(raw, '/base'))
            self.assertEqual(len(rows), 2 * n * n)
            order = {}
            for r in rows:
                order.setdefault(r['repeat'], []).append(r['variant'])
            before = {(x, y): 0 for x in ids for y in ids if x != y}
            for sequence in order.values():
                for i, x in enumerate(sequence):
                    for y in sequence[i + 1:]:
                        before[(x, y)] += 1
            self.assertEqual(set(before.values()), {n}, f'n={n}')   # x before y exactly as often as y before x
            position = {(v, i) for seq in order.values() for i, v in enumerate(seq)}
            if n > 2:
                self.assertEqual(len(position), n * n)               # 2n repeats also put every variant in every position

    def test_workload_variant_subset_and_only_filter(self):
        raw = base(workloads=[{'id': 'one', 'kind': 'editor.warm_open', 'repeats': 2, 'variants': ['b']}, {'id': 'two', 'kind': 'editor.cold_open', 'repeats': 1}])
        cfg = config.validate(raw, '/base')
        self.assertEqual([(r['workload'], r['variant']) for r in config.schedule(cfg)], [('one', 'b'), ('one', 'b'), ('two', 'a'), ('two', 'b')])
        self.assertEqual([r['workload'] for r in config.schedule(cfg, only=['two'])], ['two', 'two'])
        self.assertEqual([r['index'] for r in config.schedule(cfg, only=['two'])], [0, 1])

    def test_only_pulls_in_the_build_workloads_it_needs_for_one_repeat(self):
        raw = base(workloads=[{'id': 'build', 'kind': 'player.build_time', 'repeats': 3}, {'id': 'other', 'kind': 'editor.warm_open'},
                              {'id': 'start', 'kind': 'player.startup', 'build': 'build', 'repeats': 2, 'variants': ['b']}])
        cfg = config.validate(raw, '/base')
        notes = []
        rows = config.schedule(cfg, only=['start'], notes=notes)
        self.assertEqual([(r['workload'], r['variant']) for r in rows], [('build', 'b'), ('start', 'b'), ('start', 'b')])   # restricted to the variants that launch
        self.assertIn('pulled in by --only', notes[0])
        both = config.schedule(cfg, only=['start', 'build'])
        self.assertEqual(sum(1 for r in both if r['workload'] == 'build'), 6)                                  # asked for explicitly: all its repeats
        self.assertEqual([r['workload'] for r in config.schedule(cfg, only=['other'])], ['other'] * 8)

    def test_a_build_workload_must_come_before_the_workloads_that_use_it(self):
        raw = base(workloads=[{'id': 's', 'kind': 'player.startup', 'build': 'b'}, {'id': 'b', 'kind': 'player.build_time'}])
        with self.assertRaisesRegex(config.ConfigError, 'defined before'):
            config.validate(raw, '/base')


if __name__ == '__main__':
    unittest.main()
