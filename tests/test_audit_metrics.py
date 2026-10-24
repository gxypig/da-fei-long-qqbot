import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_maintenance import Main


class AuditMetrics(unittest.TestCase):
    def test_scheduler_kind_does_not_replace_event_type(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = object.__new__(Main)
            plugin._data_dir = Path(directory)
            plugin._timings = {}
            plugin.logger = Mock()
            fields = {'kind': 'interactive', 'elapsed_ms': 42, 'status': 'ok'}
            plugin._audit('model_work', fields)
            record = json.loads((plugin._data_dir/'conversation-audit.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(record['kind'], 'model_work')
            self.assertEqual(record['work_kind'], 'interactive')
            self.assertEqual(fields['kind'], 'interactive')
            self.assertEqual(list(plugin._timings['model_work']), [42])


if __name__ == '__main__':
    unittest.main()
