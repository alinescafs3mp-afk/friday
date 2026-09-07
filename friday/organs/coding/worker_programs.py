"""Code-owned programs for the Coding namespace; never generated from user text.

PROBE and BUILD do not import or execute uploaded modules. TEST does, including
when discovery finds no test cases, so the default runner admits it only inside
a proved aggregate cgroup-v2 tree. ORACLE is the Friday-owned independent
behavior program; it also requires that tree. Execute/run of uploaded programs
stay fail-closed.
"""

from friday.organs.coding.behavior_oracle import oracle_worker_program

PROBE = (
    "import os,sys;"
    "work,export,*hazards=sys.argv[1:];"
    "sys.exit(3 if not (os.path.isdir(work) and os.path.isdir(export)) else "
    "1 if any(os.path.exists(path) for path in hazards) else 0)"
)

MAX_LOOP_PY_FILES = 4096

BUILD = (
    "import pathlib,py_compile,sys\n"
    "root=pathlib.Path(sys.argv[1])\n"
    "if not root.is_dir():\n"
    "    raise SystemExit(3)\n"
    "root=root.resolve()\n"
    "files=[]\n"
    "for path in root.rglob('*.py'):\n"
    "    if not path.is_file():\n"
    "        continue\n"
    "    resolved=path.resolve()\n"
    "    try:\n"
    "        resolved.relative_to(root)\n"
    "    except ValueError:\n"
    "        raise SystemExit(4)\n"
    "    files.append(path)\n"
    f"    if len(files)>{MAX_LOOP_PY_FILES}:\n"
    "        raise SystemExit(5)\n"
    "if not files:\n"
    "    raise SystemExit(2)\n"
    "for path in files:\n"
    "    py_compile.compile(str(path), doraise=True)\n"
    "raise SystemExit(0)\n"
)

TEST = (
    "import sys,unittest\n"
    "root=sys.argv[1]\n"
    "loader=unittest.defaultTestLoader\n"
    "suite=loader.discover(root, pattern='test*.py', top_level_dir=root)\n"
    "count=suite.countTestCases()\n"
    "if count==0:\n"
    "    raise SystemExit(2)\n"
    "result=unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)\n"
    "raise SystemExit(0 if result.wasSuccessful() else 1)\n"
)

ORACLE = oracle_worker_program()
