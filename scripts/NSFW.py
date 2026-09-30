#!/usr/bin/env python3
"""
FaceFusion NSFW Detection Disabling Patch Script
================================================
Purpose:
    Applies an in-place patch to the FaceFusion source code to disable NSFW detection and completely skip NSFW model downloads.
    Designed to be invoked repeatedly by CI (idempotent):
        - Already patched -> Skips, avoids duplicate insertions (solving duplicate return issues)
        - Key function signature not matched -> Returns non-zero, causing CI failure and alerting (upstream refactoring likely)
        - Normal case -> Replaces the entire function body with a clean stub, leaving no dead code

Patch Contents (content_analyser.py):
        - detect_nsfw            -> return False   (Critical: NSFW detection itself)
        - analyse_frame          -> return False   (Defensive: returns False anyway when detect_nsfw is disabled)
        - pre_check              -> return True    (Defensive: skips downloads triggered by pre_check)
        - collect_model_downloads -> return {},{}  (Defensive: provides an empty set to get_inference_pool, preventing None.get crashes)
        - create_static_model_set -> return {}     (Defensive: blocks the real download path of --force-download)
Patch Contents (core.py):
        - common_pre_check       -> return True    (Critical: bypasses content_analyser source code hash verification)

Arguments:
    None (operates on facefusion/content_analyser.py and facefusion/core.py in the sibling/sub directory)

Return Values:
    0 = All patches applied successfully (or already in the target state)
    2 = At least one key function not matched, manual intervention required
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Repository root: Script is located under scripts/, so root = parent of parent of script's parent
ROOT = Path(__file__).resolve().parent.parent
CONTENT_ANALYSER = ROOT / "facefusion" / "content_analyser.py"
CORE = ROOT / "facefusion" / "core.py"

# Matches a top-level function body: After the signature line, consecutive "indented lines + newline" or "empty lines + newline",
# until the next non-indented non-empty line appears (such as the next top-level def / class).
# This completely consumes the entire function body (including internal empty lines), leaving no dead code after replacement.
_FUNC_BODY = r"(?:[ \t]+[^\n]*\n|[ \t]*\n)*"


def _replace_func_body(text: str, signature_pattern: str, new_body: str) -> tuple[str, int]:
	"""
	Purpose: Replaces the entire function body of functions matching signature_pattern with new_body.

	Parameters:
		text              - Original file text
		signature_pattern - Regular expression string matching the function signature (including trailing newline),
		                    must be enclosed in a pair of capturing parentheses (repl restores signature via \1)
		new_body          - Replacement function body text (including indentation and newline)

	Returns:
		(Modified text, match count)
		Match count = 0 means signature not found;
		If text after replacement is identical to original, it's already in target state (idempotent hit).
	"""
	pattern = r"(" + signature_pattern + r")" + _FUNC_BODY
	repl = r"\1" + new_body
	new_text, n = re.subn(pattern, repl, text, count=1)
	return new_text, n


def patch_content_analyser(path: Path) -> str:
	"""
	Purpose: Patches content_analyser.py.
		Critical (Missing -> CI failure):
			- detect_nsfw             -> return False   (NSFW detection itself)
		Defensive (Missing -> warning only):
			- analyse_frame           -> return False   (Upper-level entry point, returns False anyway when detect_nsfw is disabled)
			- pre_check               -> return True    (Skips download path triggered by pre_check)
			- collect_model_downloads -> return {}, {}  (Provides an empty set to get_inference_pool, preventing None.get crashes)
			- create_static_model_set -> return {}      (Blocks the real download path of --force-download)

	Parameters:
		path - Path to content_analyser.py

	Returns:
		"patched"   = Modified in this run
		"already"   = Already in target state, no modifications (idempotent)
		"not_found" = Critical function detect_nsfw not matched, upstream might have been refactored
	"""
	text = path.read_text(encoding="utf-8")
	original = text

	# Patch specifications: (Function name, signature regex, replacement body, is_critical)
	# Signature regex only matches "signature line + trailing newline", function body is consumed by _FUNC_BODY.
	# Critical = True missing functions cause CI failure; False missing only warns.
	# Note: collect_model_downloads / create_static_model_set use generic return type [^\n:]+,
	#       to accommodate non-bool annotations like Tuple[...] / ModelSet and resist upstream type renames.
	patch_specs = [
		("detect_nsfw",             r"def detect_nsfw\s*\([^)]*\)\s*->\s*bool\s*:\n",            "\treturn False\n\n",  True),
		("analyse_frame",           r"def analyse_frame\s*\([^)]*\)\s*->\s*bool\s*:\n",          "\treturn False\n\n",  False),
		("pre_check",               r"def pre_check\s*\(\s*\)\s*->\s*bool\s*:\n",               "\treturn True\n\n",   False),
		("collect_model_downloads", r"def collect_model_downloads\s*\([^)]*\)\s*->\s*[^\n:]+:\n", "\treturn {}, {}\n\n", False),
		("create_static_model_set", r"def create_static_model_set\s*\([^)]*\)\s*->\s*[^\n:]+:\n", "\treturn {}\n\n",     False),
	]

	# Apply each patch sequentially, recording respective status (patched / already / not_found)
	statuses: dict[str, str] = {}
	for func_name, sig_pattern, new_body, _is_critical in patch_specs:
		before = text
		text, n = _replace_func_body(text, sig_pattern, new_body)
		if not n:
			statuses[func_name] = "not_found"
		elif text == before:
			statuses[func_name] = "already"
		else:
			statuses[func_name] = "patched"

	# Critical function detect_nsfw missing -> Do not write to disk, return directly to trigger CI warning/failure
	if statuses["detect_nsfw"] == "not_found":
		print(f"[FAIL] {path.name}: def detect_nsfw not matched (Has upstream refactored?)")
		return "not_found"

	# Write to disk only if modified
	if text != original:
		path.write_text(text, encoding="utf-8")

	# Status summary: detect_nsfw=patched, analyse_frame=already, ...
	summary = ", ".join(f"{name}={statuses[name]}" for name, *_ in patch_specs)
	print(f"{'[ok]  ' if text != original else '[skip]'} {path.name}: {summary}")

	# Defensive function missing -> Warn (does not affect return value)
	for func_name, _sig, _body, is_critical in patch_specs:
		if not is_critical and statuses[func_name] == "not_found":
			print(f"[warn] {path.name}: def {func_name} not matched (Defensive patch did not take effect)")

	# If any function was actually modified this time -> "patched", otherwise "already"
	changed = any(statuses[name] == "patched" for name, *_ in patch_specs)
	return "patched" if changed else "already"


def patch_core(path: Path) -> str:
	"""
	Purpose: Patches core.py.
		common_pre_check -> return True, bypasses content_analyser source code hash verification
		(Otherwise, once content_analyser.py is modified, the hash calculated by inspect.getsource will change and cause pre_check to fail)

	Parameters:
		path - Path to core.py

	Returns:
		"patched" / "already" / "not_found" (same meanings as in patch_content_analyser)
	"""
	text = path.read_text(encoding="utf-8")
	original = text

	text, n = _replace_func_body(
		text,
		r"def common_pre_check\s*\(\s*\)\s*->\s*bool\s*:\n",
		"\treturn True\n\n",
	)

	if not n:
		print(f"[FAIL] {path.name}: def common_pre_check not matched (Has upstream refactored?)")
		return "not_found"

	if text == original:
		print(f"[skip] {path.name}: common_pre_check is already constantly True")
		return "already"

	path.write_text(text, encoding="utf-8")
	print(f"[ok] {path.name}: common_pre_check → return True (Bypasses content_analyser hash verification)")
	return "patched"


def main() -> int:
	"""
	Purpose: Entry point, applies patches to both files sequentially.

	Returns:
		0 = Success (or already in target state)
		2 = Critical function missing (CI should fail and open an issue)
	"""
	if not CONTENT_ANALYSER.is_file() or not CORE.is_file():
		print("Error: Please run this script from the FaceFusion repository root directory", file=sys.stderr)
		return 2

	s1 = patch_content_analyser(CONTENT_ANALYSER)
	s2 = patch_core(CORE)

	# Any critical function not matched -> Non-zero exit, causing CI failure and opening an issue
	if s1 == "not_found" or s2 == "not_found":
		print("\n[FAIL] Patches were not fully applied. Please manually check if upstream has refactored related functions.", file=sys.stderr)
		return 2

	if s1 == "patched" or s2 == "patched":
		print("\nPatching completed. Recommended local command: python -m py_compile facefusion/content_analyser.py facefusion/core.py")
		return 0

	print("\nNo modifications made (already in disabled state)")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
