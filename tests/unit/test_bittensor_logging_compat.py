"""The SDK's logging API must not break the validator worker loop.

bittensor v11 removed `bittensor.logging`; accessing it raises. That exception
propagated out of every worker-loop iteration and silently stopped audit-report
publishing for ~30 hours -- /healthz stayed "ok" the whole time because the loop
catches per-iteration errors, so only /readyz's worker_last_error showed it.

bittensor is not version-pinned, so this must be feature-detected, not gated on
a version number.
"""
import ast
import logging
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / (
    "services/validator/src/greencompute_validator/domain/chain.py")


def _load():
    tree = ast.parse(SRC.read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_silence_bittensor_logging")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<f>", "exec"), {"logging": logging}, ns)
    return ns["_silence_bittensor_logging"]


class _LegacySDK:
    """bittensor < 11: bt.logging.off() exists."""
    class logging:  # noqa: N801
        called = False
        @classmethod
        def off(cls):
            cls.called = True


class _V11SDK:
    """bittensor >= 11: attribute access raises."""
    @property
    def logging(self):
        raise AttributeError("bittensor.logging was removed in v11")


def test_legacy_sdk_still_uses_the_old_api():
    f = _load()
    sdk = _LegacySDK()
    f(sdk)
    assert _LegacySDK.logging.called is True


def test_v11_does_not_raise_and_silences_the_standard_logger():
    """The whole bug: this call raising killed the worker loop."""
    f = _load()
    logging.getLogger("bittensor").setLevel(logging.NOTSET)
    f(_V11SDK())          # must not raise
    assert logging.getLogger("bittensor").level == logging.CRITICAL


def test_any_sdk_failure_is_survivable():
    """Feature detection, not version detection -- the package is unpinned, so a
    future major can move this again."""
    class _Weird:
        @property
        def logging(self):
            raise RuntimeError("something else entirely")
    _load()(_Weird())     # must not raise
