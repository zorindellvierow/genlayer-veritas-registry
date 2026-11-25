#!/usr/bin/env python3
"""Compatibility patches for genlayer-test 0.30.1 direct mode.

WHY THIS EXISTS
---------------
The contract in this repo pins the runner from the GenLayer docs
(py-genlayer:1jb45aa8...). That runner is a *legacy v0.2* runner whose
stdlib uses the old module layout (genlayer.py.calldata, genlayer.py.types,
genlayer.gl.vm, frozen gl.message NamedTuple). genlayer-test 0.30.1's
direct-mode loader was written for the *v0.3* layout
(genlayer.calldata, genlayer.types, genlayer.vm, live genlayer.message).
Result on a stock install: every direct test fails at contract load with
"unexpected end of memory" (the message-context injection silently
no-ops), and per-call sender/value changes are silently ignored.

This script applies minimal, surgical fallbacks to the installed
genlayer-test package so direct tests run against the docs-pinned runner:

1. sdk_compat.import_calldata / import_types: fall back to genlayer.py.*
2. sdk_compat.sync_message_context: also update the old-layout raw dict
   and rebuild the frozen gl.message NamedTuple.
3. loader._patch_run_nondet_for_direct_mode: fall back to genlayer.gl.vm
   and also patch run_nondet_unsafe (the recommended variant).
4. loader._allocate_contract: fall back to genlayer.py.storage.inmem_allocate.
5. vm.VMContext._refresh_gl_message: proceed when genlayer.gl is loaded.
6. vm.VMContext.run_validator: fall back to genlayer.gl.vm for Return.

The contract itself is NOT changed by this script. If a future
genlayer-test release fixes the skew, the anchor assertions below fail
loudly instead of silently corrupting the new code — delete this script
then.

Usage:
    pip install -r requirements-dev.txt
    python scripts/patch_gltest.py
    gltest tests/direct/ -v
"""

import importlib.util
import sys
from pathlib import Path


def _find(package_file: str) -> Path:
    spec = importlib.util.find_spec("gltest.direct")
    if spec is None or spec.origin is None:
        raise SystemExit("genlayer-test is not installed (pip install genlayer-test)")
    base = Path(spec.origin).parent
    path = base / package_file
    if not path.exists():
        raise SystemExit(f"expected file not found: {path}")
    return path


def _patch(path: Path, anchor: str, replacement: str, label: str) -> None:
    text = path.read_text()
    if replacement.strip() in text:
        print(f"  already applied: {label}")
        return
    if anchor not in text:
        raise SystemExit(
            f"ABORT: anchor for '{label}' not found in {path}\n"
            "The installed genlayer-test version differs from 0.30.1; "
            "do not apply these patches blindly."
        )
    path.write_text(text.replace(anchor, replacement, 1))
    print(f"  applied: {label}")


def main() -> None:
    sdk_compat = _find("sdk_compat.py")
    loader = _find("loader.py")
    vm = _find("vm.py")

    print(f"patching genlayer-test at {sdk_compat.parent}")

    _patch(
        sdk_compat,
        '''def import_calldata() -> Any:
    """Return the v0.3 calldata module from the active SDK path."""
    from genlayer import calldata

    return calldata''',
        '''def import_calldata() -> Any:
    """Return the v0.3 calldata module from the active SDK path."""
    try:
        from genlayer import calldata
    except ImportError:
        # Older stdlib layout (docs-pinned runner 1jb45aa8...): calldata
        # lives under genlayer.py instead of the top level.
        from genlayer.py import calldata

    return calldata''',
        "sdk_compat.import_calldata fallback",
    )
    _patch(
        sdk_compat,
        '''def import_types() -> Any:
    """Return the v0.3 types module from the active SDK path."""
    from genlayer import types as sdk_types

    return sdk_types''',
        '''def import_types() -> Any:
    """Return the v0.3 types module from the active SDK path."""
    try:
        from genlayer import types as sdk_types
    except ImportError:
        # Older stdlib layout (docs-pinned runner 1jb45aa8...).
        from genlayer.py import types as sdk_types

    return sdk_types''',
        "sdk_compat.import_types fallback",
    )
    _patch(
        sdk_compat,
        """    for name, next_value in updates.items():
        if next_value is _UNSET:
            continue
        if message_mod is not None:
            setattr(message_mod, name, next_value)
        if isinstance(raw, dict):
            raw[name] = next_value
""",
        """    for name, next_value in updates.items():
        if next_value is _UNSET:
            continue
        if message_mod is not None:
            setattr(message_mod, name, next_value)
        if isinstance(raw, dict):
            raw[name] = next_value

    # Older stdlib layout (docs-pinned runner 1jb45aa8...): there is no
    # genlayer.message module, and gl.message is a NamedTuple frozen at
    # import time. Update the underlying raw dict and rebuild gl.message
    # so contracts see the current sender/value.
    try:
        from genlayer._internal import msg as _msg_mod
        from genlayer.gl import MessageType as _MessageType
        from genlayer.py.types import u256 as _u256

        raw_dict = getattr(_msg_mod, "message_raw", None)
        if isinstance(raw_dict, dict):
            for name, next_value in updates.items():
                if next_value is _UNSET:
                    continue
                raw_dict[name] = next_value
            import genlayer.gl as _gl_mod

            _gl_mod.message = _MessageType(
                contract_address=raw_dict["contract_address"],
                sender_address=raw_dict["sender_address"],
                origin_address=raw_dict["origin_address"],
                value=_u256(raw_dict["value"]),
                chain_id=_u256(raw_dict["chain_id"]),
            )
    except (ImportError, KeyError, TypeError):
        pass
""",
        "sdk_compat.sync_message_context old-layout branch",
    )
    _patch(
        loader,
        """    try:
        import genlayer.vm as gl_vm
    except ImportError:
        return

    if getattr(gl_vm, '_direct_mode_patched', False):
        return
""",
        """    try:
        import genlayer.vm as gl_vm
    except ImportError:
        try:
            # Older stdlib layout (docs-pinned runner 1jb45aa8...): the vm
            # module lives under genlayer.gl.vm, not genlayer.vm.
            import genlayer.gl.vm as gl_vm
        except ImportError:
            return

    if getattr(gl_vm, '_direct_mode_patched', False):
        return
""",
        "loader._patch_run_nondet_for_direct_mode vm fallback",
    )
    _patch(
        loader,
        """    gl_vm.run_nondet = _direct_run_nondet
    gl_vm.run_nondet_default = _direct_run_nondet_default
    gl_vm._direct_mode_patched = True
""",
        """    gl_vm.run_nondet = _direct_run_nondet
    gl_vm.run_nondet_default = _direct_run_nondet_default
    # run_nondet_unsafe has the same (leader_fn, validator_fn) shape and is
    # the recommended variant; without this, contracts using it never
    # capture their validator in direct mode (run_validator() finds none).
    if hasattr(gl_vm, "run_nondet_unsafe"):
        gl_vm.run_nondet_unsafe = _direct_run_nondet
        gl_vm.run_nondet_unsafe.lazy = _lazy_run_nondet
    gl_vm._direct_mode_patched = True
""",
        "loader run_nondet_unsafe patch",
    )
    _patch(
        loader,
        """    except ImportError:
        pass

    try:
        from genlayer.storage import Root
""",
        """    except ImportError:
        pass

    # Fallback for the older stdlib layout (docs-pinned runner 1jb45aa8...):
    # storage lives under genlayer.py.storage, and inmem_allocate() performs
    # the same allocate + init sequence as the v0.3 path above.
    try:
        from genlayer.py.storage import inmem_allocate

        return inmem_allocate(contract_cls, *args, **kwargs)
    except ImportError:
        pass

    try:
        from genlayer.storage import Root
""",
        "loader._allocate_contract inmem_allocate fallback",
    )
    _patch(
        vm,
        """        if 'genlayer.message' not in sys.modules:
            return
""",
        """        # v0.3 layout has a genlayer.message module; the older layout
        # (docs-pinned runner) only has genlayer.gl — proceed if either
        # is loaded, since sync_message_context handles both.
        if 'genlayer.message' not in sys.modules and 'genlayer.gl' not in sys.modules:
            return
""",
        "vm._refresh_gl_message guard",
    )
    _patch(
        vm,
        """        stored_result, leader_fn, validator_fn = self._captured_validators[index]

        import genlayer.vm as gl_vm
""",
        """        stored_result, leader_fn, validator_fn = self._captured_validators[index]

        try:
            import genlayer.vm as gl_vm
        except ImportError:
            # Older stdlib layout (docs-pinned runner): vm lives under
            # genlayer.gl.vm.
            import genlayer.gl.vm as gl_vm
""",
        "vm.run_validator import fallback",
    )
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
