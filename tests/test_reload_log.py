from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unity_bench import reload_log  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures'


def events(name):
    return reload_log.parse(FIXTURES.joinpath(name).read_text(encoding='utf-8').splitlines())


class ReloadLogTests(unittest.TestCase):
    def test_mono_reload_block_from_a_windows_ci_log(self):
        block = [e for e in events('editor-log-6.7-ci.txt') if e['event'] == 'reload'][0]
        self.assertEqual(block['total_ms'], 3065)
        phases = block['phases']
        self.assertEqual((phases['BeginReloadAssembly'], phases['LoadAllAssembliesAndSetupDomain'], phases['FinalizeReload']), (250, 989, 1713))
        self.assertEqual(phases['TypeCache.ScanAssembly'], 232)
        self.assertEqual(phases['ProcessInitializeOnLoadAttributes'], 509)
        self.assertNotIn('Serialization', phases)   # "Serialization: 171ms" is not a "(Nms)" phase line

    def test_coreclr_reload_block_from_a_windows_ci_log(self):
        block = [e for e in events('editor-log-7-ci.txt') if e['event'] == 'reload'][0]
        self.assertEqual(block['total_ms'], 3362)
        self.assertEqual((block['phases']['BeginReloadAssembly'], block['phases']['TypeCache.ScanAssembly']), (1528, 768))

    def test_asset_pipeline_refresh_scripting_line(self):
        refresh = [e for e in events('editor-log-6.7-ci.txt') if e['event'] == 'refresh'][0]
        self.assertEqual((refresh['total_ms'], refresh['domain_reloads'], refresh['domain_reload_ms'], refresh['compile_ms']), (6615, 1, 1437, 2790))
        self.assertIn('InitialRefreshV2', refresh['initiated_by'])

    def test_refresh_without_a_scripting_line_is_ignored(self):
        text = 'Asset Pipeline Refresh (id=ab12): Total: 0.054 seconds - Initiated by X\n\t(Values over 0.05ms)\n\tSummary:\n\t\tImports: total=0\n'
        self.assertEqual(reload_log.parse(text.splitlines()), [])

    def test_markers_attribute_blocks_to_steps(self):
        groups = reload_log.attribute(events('editor-log-7-marked.txt'))
        step = [g for g in groups if g['step'] == 'leaf']
        self.assertEqual(len(step), 1)
        self.assertEqual(step[0]['round'], 1)
        self.assertEqual(len(step[0]['reloads']), 1)
        self.assertEqual(step[0]['reloads'][0]['total_ms'], 1208)
        self.assertEqual(step[0]['reloads'][0]['phases']['FinalizeReload'], 812)
        self.assertEqual(step[0]['refreshes'][0]['domain_reload_ms'], 409)

    def test_blocks_outside_markers_have_no_step(self):
        text = (FIXTURES / 'editor-log-6.7-ci.txt').read_text(encoding='utf-8') + '[UnityBench] begin core 0\n' + \
               (FIXTURES / 'editor-log-7-ci.txt').read_text(encoding='utf-8') + '[UnityBench] end core 0\n'
        groups = reload_log.attribute(reload_log.parse(text.splitlines()))
        self.assertEqual([g['step'] for g in groups], [None, 'core'])
        self.assertEqual(groups[0]['reloads'][0]['total_ms'], 3065)
        self.assertEqual(groups[1]['reloads'][0]['total_ms'], 3362)

    def test_a_step_without_blocks_is_kept(self):
        groups = reload_log.attribute(reload_log.parse(['[UnityBench] begin exit 0', '[UnityBench] end exit 0']))
        self.assertEqual([(g['step'], g['reloads']) for g in groups], [('exit', [])])


if __name__ == '__main__':
    unittest.main()
