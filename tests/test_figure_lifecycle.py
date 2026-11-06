from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
PAGES = ("11", "12", "13", "14", "16", "17")


def is_call(statement, owner, method):
    return (
        isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == owner and statement.value.func.attr == method
    )


def plot_blocks():
    """Extract actual render/cleanup statements, never execute page initialization."""
    result = []
    for prefix in PAGES:
        path = next((ROOT / "pages").glob(prefix + "_*.py"))
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        seen = set()
        for parent in ast.walk(tree):
            for _field, value in ast.iter_fields(parent):
                if not isinstance(value, list):
                    continue
                for index, statement in enumerate(value):
                    if not is_call(statement, "st", "pyplot"):
                        continue
                    if id(statement) in seen:
                        continue
                    seen.add(id(statement))
                    argument = statement.value.args[0]
                    if not isinstance(argument, ast.Name):
                        raise AssertionError(f"{path}:{statement.lineno}: explicit figure required")
                    finalizer = (
                        parent.finalbody if isinstance(parent, ast.Try) and value is parent.body
                        else value[index + 1:index + 2]
                    )
                    if not (
                        len(finalizer) == 1 and is_call(finalizer[0], "plt", "close")
                        and len(finalizer[0].value.args) == 1
                        and isinstance(finalizer[0].value.args[0], ast.Name)
                        and finalizer[0].value.args[0].id == argument.id
                    ):
                        raise AssertionError(f"{path}:{statement.lineno}: figure must close locally")
                    guarded = isinstance(parent, ast.Try) and value is parent.body
                    block = [parent] if guarded else [statement, finalizer[0]]
                    result.append((path, statement.lineno, argument.id, guarded, deepcopy(block)))
        for node in ast.walk(tree):
            if is_call(node, "plt", "close"):
                if not node.value.args or not isinstance(node.value.args[0], ast.Name):
                    raise AssertionError(f"{path}:{node.lineno}: do not close other sessions' figures")
    return result


class FigureLifecycleTests(unittest.TestCase):
    def test_every_plot_has_targeted_cleanup(self):
        blocks = plot_blocks()
        self.assertEqual(len(blocks), 21)
        self.assertEqual(sum(guarded for _, _, _, guarded, _ in blocks), 19)

    def execute_block(self, block, figure_name, *, fail=False):
        figure, axis = plt.subplots()
        self.addCleanup(plt.close, figure)
        axis.plot([0, 1], [1, 2], label="synthetic")
        captured = []

        def render(received, **kwargs):
            self.assertIs(received, figure)
            captured.append((list(received.axes[0].lines[0].get_ydata()), kwargs))
            if fail:
                raise RuntimeError("synthetic render failure")
            if kwargs.get("clear_figure"):
                received.clf()

        code = compile(ast.Module(body=block, type_ignores=[]), "<actual-page-plot-block>", "exec")
        scope = {figure_name: figure, "st": SimpleNamespace(pyplot=render), "plt": plt}
        if fail:
            with self.assertRaisesRegex(RuntimeError, "synthetic render failure"):
                exec(code, scope)
        else:
            exec(code, scope)
        self.assertEqual(captured[0][0], [1, 2])
        self.assertEqual(len(captured), 1)
        self.assertFalse(plt.fignum_exists(figure.number))

    def test_repeated_plot_blocks_do_not_accumulate_or_close_other_figures(self):
        other = plt.figure()
        self.addCleanup(plt.close, other)
        baseline = set(plt.get_fignums())
        for path, line, name, _guarded, block in plot_blocks():
            for iteration in range(3):
                with self.subTest(page=path.name, line=line, iteration=iteration):
                    self.execute_block(block, name)
                    self.assertEqual(set(plt.get_fignums()), baseline)
                    self.assertTrue(plt.fignum_exists(other.number))

    def test_new_cleanup_runs_when_rendering_raises(self):
        other = plt.figure()
        self.addCleanup(plt.close, other)
        baseline = set(plt.get_fignums())
        for path, line, name, guarded, block in plot_blocks():
            if guarded:
                with self.subTest(page=path.name, line=line):
                    self.execute_block(block, name, fail=True)
                    self.assertEqual(set(plt.get_fignums()), baseline)
                    self.assertTrue(plt.fignum_exists(other.number))


if __name__ == "__main__":
    unittest.main()
