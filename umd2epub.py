#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""便捷入口: python umd2epub.py 书.umd

逻辑都在 umd2epub/ 包里, 本文件只是让命令行短一点。
"""
from __future__ import annotations

import sys

from umd2epub.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
