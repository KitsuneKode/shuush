"""Run voxtype_pp_gate's CLI from its cached bytecode.

A script Python runs directly is compiled on every start (~13 ms); an
imported module is compiled once and cached in __pycache__.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voxtype_pp_gate  # noqa: E402

sys.exit(voxtype_pp_gate.main(sys.argv))
