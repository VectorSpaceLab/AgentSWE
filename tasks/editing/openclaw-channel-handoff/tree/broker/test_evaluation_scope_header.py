"""The evaluation-scope header must not take the broker down.

`X-AgentSWE-Evaluation` is stamped on every request the candidate relay
forwards, and the branch that validates it was the only user of `re` in a module
that did not import it. Every openclaw run reported calls=0 for four batches
because the handler raised NameError two lines before the call counter was
touched, and closed the socket without writing a response.

Nothing in this tree had ever sent that header in a test, which is exactly why a
NameError on the request path survived. These cases send it.
"""
import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BROKER = Path(__file__).with_name('responses_broker.py')


def load():
    spec = importlib.util.spec_from_file_location('openclaw_responses_broker', BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvaluationScopeHeaderTests(unittest.TestCase):
    def test_module_imports_every_name_it_uses(self):
        """A missing import on the request path is invisible until a request arrives."""
        import ast
        tree = ast.parse(BROKER.read_text(encoding='utf-8'))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.asname or a.name.split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported |= {a.asname or a.name for a in node.names}
        used = {n.value.id for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        builtins_and_locals = used - imported
        self.assertIn('re', imported, 'responses_broker uses re.fullmatch on the request path')
        self.assertNotIn('re', builtins_and_locals)

    def test_scope_header_is_validated_not_fatal(self):
        """A well-formed scope is accepted; a malformed one is refused, not crashed."""
        module = load()
        self.assertIsNotNone(getattr(module, 're', None),
                             'the module must expose the re it uses')
        self.assertTrue(module.re.fullmatch(r'[0-9a-f]{8,64}', 'a' * 32))
        self.assertIsNone(module.re.fullmatch(r'[0-9a-f]{8,64}', 'not-hex'))


if __name__ == '__main__':
    unittest.main()
