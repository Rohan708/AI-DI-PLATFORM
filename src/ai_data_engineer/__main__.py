"""``python -m ai_data_engineer ...`` is the same as the ``aide`` command.

Useful where the generated ``aide.exe`` launcher is blocked (e.g. Windows Smart App Control).
"""

from ai_data_engineer.cli import main

raise SystemExit(main())
