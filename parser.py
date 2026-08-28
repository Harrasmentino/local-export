"""Compatibility launcher: use the new fresh-only local exporter."""
from local_export import main

if __name__ == '__main__':
    raise SystemExit(main())
