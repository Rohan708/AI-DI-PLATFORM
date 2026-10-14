"""``python -m lumida ...`` is the same as the ``lumida`` command.

Useful where the generated ``lumida.exe`` launcher is blocked (e.g. Windows Smart App Control).
"""

from lumida.cli import main

raise SystemExit(main())
