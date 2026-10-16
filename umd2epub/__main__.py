# -*- coding: utf-8 -*-
"""支持 python -m umd2epub 调用。"""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
