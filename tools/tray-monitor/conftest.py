import sys, os
# Ensure the tray-monitor directory is importable so `import monitor` (and its
# sibling `import settings`) work when pytest is run from anywhere.
sys.path.insert(0, os.path.dirname(__file__))
