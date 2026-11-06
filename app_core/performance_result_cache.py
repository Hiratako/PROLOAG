"""Bounded, session-owned memoization of performance analysis results only.

Callers still load JSON/build the daily frame on every rerun. No process-global
data cache, file contents, or figures are retained here.
"""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from copy import deepcopy
from hashlib import sha256
import marshal
import os
from pathlib import Path
import pickle
from typing import Any, TypeVar

import pandas as pd

from app_core import analysis_statistics as statistics
from app_core import performance_analysis_service as analysis_service


_SESSION_KEY = "_performance_numeric_results_v1"
_MAX_RESULTS = 8  # Profile, evidence, and at most the six comparable-day factors.
_Result = TypeVar("_Result")


def _calculation_revision() -> str:
    """Include fixed seeds, bootstrap defaults, and dependent numerical code.

    This fingerprint is code/configuration only, never user/session data. It is
    rebuilt when the module reloads after an implementation change.
    """
    digest = sha256()
    for module in (statistics, analysis_service):
        for name, function in sorted(vars(module).items()):
            if callable(function) and getattr(function, "__module__", None) == module.__name__ and hasattr(function, "__code__"):
                digest.update(name.encode("utf-8"))
                digest.update(marshal.dumps(function.__code__))
                digest.update(pickle.dumps((function.__defaults__, function.__kwdefaults__), protocol=5))
    digest.update(pickle.dumps(analysis_service.DEFAULT_PROFILE_SPECS, protocol=5))
    return digest.hexdigest()


_CALCULATION_REVISION = _calculation_revision()


def _frame_digest(frame: pd.DataFrame) -> str:
    digest = sha256()
    schema = (tuple(frame.columns), tuple(str(dtype) for dtype in frame.dtypes),
              tuple(frame.index.names), str(frame.index.dtype))
    digest.update(pickle.dumps(schema, protocol=5))
    digest.update(pd.util.hash_pandas_object(frame, index=True, categorize=False).to_numpy().tobytes())
    return digest.hexdigest()


class PerformanceResultCache:
    """Reuse numerical results only within the supplied session-state mapping.

    ``context`` must include the date, period, missing-value policy, and analysis
    filters/settings. Result-specific arguments go in ``get_or_compute``. The
    root is explicit to prevent same-session reuse after changing data roots.
    """

    def __init__(
        self,
        session_state: MutableMapping[str, Any],
        frame: pd.DataFrame,
        *,
        root: Path,
        context: dict[str, Any],
    ) -> None:
        signature = sha256(pickle.dumps((
            _CALCULATION_REVISION,
            os.path.normcase(str(Path(root).resolve())),
            _frame_digest(frame),
            context,
        ), protocol=5)).hexdigest()
        stored = session_state.get(_SESSION_KEY)
        if not isinstance(stored, dict) or stored.get("signature") != signature:
            stored = {"signature": signature, "results": {}}
            session_state[_SESSION_KEY] = stored
        self._results: dict[tuple[str, str], Any] = stored["results"]

    def get_or_compute(
        self,
        name: str,
        settings: dict[str, Any],
        compute: Callable[[], _Result],
    ) -> _Result:
        key = (name, sha256(pickle.dumps(settings, protocol=5)).hexdigest())
        if key not in self._results:
            result = compute()
            if len(self._results) >= _MAX_RESULTS:
                del self._results[next(iter(self._results))]
            self._results[key] = deepcopy(result)
        # Display formatting/eligibility must not mutate the stored statistics.
        return deepcopy(self._results[key])
