"""Guard: never derive a Bittensor keypair from a wallet PATH.

`Keypair.create_from_uri(s)` treats `s` as an sr25519 derivation URI — passing a
filesystem path silently mints a keypair from the path TEXT and never opens the
wallet. It is a valid call that returns a valid-looking keypair, so nothing
fails loudly; you only notice when the chain doesn't recognise your identity.

This trap has now bitten three separate call sites:
  1. domain/chain.py — fixed previously, its loader docstring documents it.
  2. application/services.py::generate_audit_report — every audit report was
     signed by 5H1gEPqV… (derived from the wallet path string) instead of the
     real validator hotkey 5CCf21ie… (uid 0 on netuid 110).
  3. transport/routes.py::get_audit_hotkey — the endpoint auditors call to
     fetch the key for verifying those signatures had the same bug, so the
     system was self-consistently wrong and signature checks still passed.

That third one is why this is a source-level test rather than a behavioural
one: the two sides were wrong in matching ways, so no runtime assertion about
"signature verifies" could have caught it. Only the *identity* was wrong, and
only against the chain.

A literal `//name//hotkey` derivation URI is legitimate (chain.py's local-dev
fallback). Anything else — a variable, an attribute, a settings value — is the
bug, so this test discriminates on the argument rather than banning the call.
"""
import ast
import pathlib

VALIDATOR_SRC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "services" / "validator" / "src" / "greencompute_validator"
)


def _first_arg_is_derivation_uri(node: ast.Call) -> bool:
    """True when the argument is a literal starting with '//' — a real Subkey
    derivation URI, not a path handed over by mistake."""
    if not node.args:
        return False
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value.startswith("//")
    # f-string: only the leading literal chunk decides the shape.
    if isinstance(arg, ast.JoinedStr):
        for part in arg.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return part.value.startswith("//")
            return False
    return False


def _offending_calls() -> list[str]:
    bad: list[str] = []
    for path in sorted(VALIDATOR_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "create_from_uri":
                continue
            if not _first_arg_is_derivation_uri(node):
                bad.append(f"{path.name}:{node.lineno}")
    return bad


def test_no_keypair_is_derived_from_a_wallet_path():
    offenders = _offending_calls()
    assert not offenders, (
        "create_from_uri() called with something that is not a literal '//…' "
        f"derivation URI at {offenders}. If that argument is a wallet path, the "
        "keypair is derived from the path TEXT and will not match the wallet — "
        "use _load_keypair_from_wallet_file() from domain/chain.py instead."
    )


def test_the_guard_actually_detects_the_bug():
    # A guard that cannot fail is worse than no guard, so prove it fires on the
    # exact shape of the original defect.
    tree = ast.parse("Keypair.create_from_uri(settings.bittensor_wallet_path)")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _first_arg_is_derivation_uri(call) is False


def test_the_guard_permits_a_real_derivation_uri():
    for src in ('Keypair.create_from_uri("//Alice")',
                'Keypair.create_from_uri(f"//{wallet_name}//{hotkey_name}")'):
        tree = ast.parse(src)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        assert _first_arg_is_derivation_uri(call) is True, src


def test_audit_signing_and_the_public_hotkey_endpoint_use_the_same_loader():
    # These two MUST move together: the signer and the key auditors fetch to
    # verify it. Fixing one alone silently breaks every auditor.
    services = (VALIDATOR_SRC / "application" / "services.py").read_text()
    routes = (VALIDATOR_SRC / "transport" / "routes.py").read_text()
    assert "_load_keypair_from_wallet_file" in services
    assert "_load_keypair_from_wallet_file" in routes
