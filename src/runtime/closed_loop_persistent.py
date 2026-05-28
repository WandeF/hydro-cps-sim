#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entry point. Prefer: python -m src.runtime.persistent_closed_loop."""
from __future__ import annotations

from .persistent_closed_loop import main


if __name__ == "__main__":
    raise SystemExit(main())
