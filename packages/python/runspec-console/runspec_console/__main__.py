"""``python -m runspec_console`` → the console entry point.

Tracebacks and logs go to the launching terminal, so this is the form to use
when troubleshooting. For a windowed, no-console launch use the
``runspec-console-app`` gui-script (it runs under ``pythonw`` on Windows), or the
branded gui-script a white-label wrapper ships.
"""

from .app import main

if __name__ == "__main__":
    main()
